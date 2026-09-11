"""图片 → 文字动作（对应需求 2「提取文字」）。

覆盖三种产出形态：
  * ``text``   —— 纯文字，按阅读顺序
  * ``layout`` —— 保留版面结构的 Markdown（标题层级、列表、表格）
  * ``formula``—— 公式转 LaTeX

引擎由 ``engine`` 参数决定：``local``（默认，离线）／``cloud``（DeepSeek Vision）／
``auto``。具体路由与降级逻辑在 :mod:`docforge.ocr.router`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..ocr import OcrError, OcrOptions, recognize
from .base import (
    ActionContext,
    ActionError,
    ActionSpec,
    TaskResult,
    atomic_write,
    register,
    summarize_with_warnings,
)

#: 各输出格式对应的扩展名与写出器
OUTPUT_FORMATS = {
    "txt": ".txt",
    "md": ".md",
    "json": ".json",
    "docx": ".docx",
}

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "engine": {
            "type": "string",
            "enum": ["local", "cloud", "auto"],
            "default": "local",
            "title": "识别引擎",
            "description": "local=本地离线（隐私最佳、零费用）；cloud=DeepSeek Vision 高精度；auto=自动",
        },
        "mode": {
            "type": "string",
            "enum": ["text", "layout", "formula"],
            "default": "text",
            "title": "识别模式",
            "description": "text=纯文字；layout=保留版面的 Markdown；formula=公式转 LaTeX",
        },
        "output_format": {
            "type": "string",
            "enum": list(OUTPUT_FORMATS),
            "default": "txt",
            "title": "输出格式",
        },
        "language": {
            "type": "string",
            "enum": ["auto", "zh", "en", "ja", "ko"],
            "default": "auto",
            "title": "语言提示",
        },
        "tiling": {
            "type": "string",
            "enum": ["auto", "off", "always"],
            "default": "auto",
            "title": "切片策略",
            "description": "auto=按字号自动决定（推荐）；off=整图识别；always=强制切片",
        },
        "detail": {
            "type": "string",
            "enum": ["original", "high", "low", "auto"],
            "default": "original",
            "title": "云端图片细节",
            "description": "low 会把图压到 512×512，更快更省但小字会丢",
        },
        "include_boxes": {
            "type": "boolean",
            "default": False,
            "title": "输出位置信息",
            "description": "在 JSON 中包含每个文字块的坐标",
        },
        "append_filename_header": {
            "type": "boolean",
            "default": False,
            "title": "添加文件名标题",
        },
    },
    "required": ["engine", "mode"],
}


def _render_text(result, ctx: ActionContext) -> str:
    text = result.text
    if ctx.bool_param("append_filename_header", False):
        text = f"# {Path(ctx.file_path).name}\n\n{text}"
    return text


def _write_docx(path: Path, result, ctx: ActionContext) -> None:
    """生成 Word 文档。

    layout 模式下按行首的 ``#`` 判断标题层级 —— 模型被要求输出 Markdown，
    因此这里做一个轻量解析即可，不需要完整的 Markdown 解析器。
    """
    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError as err:  # pragma: no cover
        raise ActionError("未安装 python-docx，无法输出 Word 文档") from err

    document = Document()
    style = document.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(11)

    if ctx.bool_param("append_filename_header", False):
        document.add_heading(Path(ctx.file_path).name, level=1)

    for line in result.text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ctx.str_param("mode") == "layout" and stripped.startswith("#"):
            level = min(6, len(stripped) - len(stripped.lstrip("#")))
            document.add_heading(stripped.lstrip("#").strip(), level=level)
        else:
            document.add_paragraph(stripped)

    document.save(path)


def _serialize_json(result, ctx: ActionContext) -> str:
    payload: dict[str, Any] = {
        "file": Path(ctx.file_path).name,
        "engine": result.engine,
        "width": result.width,
        "height": result.height,
        "lineCount": len(result.blocks),
        "averageConfidence": round(result.average_confidence, 3),
        "elapsedMs": result.elapsed_ms,
        "tiles": result.tiles,
        "usage": result.usage,
        "warnings": result.warnings,
        "text": result.text,
    }
    if ctx.bool_param("include_boxes", False):
        payload["blocks"] = [b.to_dict() for b in result.blocks]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _handler(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    engine_pref = ctx.str_param("engine", "local")
    mode = ctx.str_param("mode", "text")
    if mode not in {"text", "layout", "formula"}:
        mode = "text"

    options = OcrOptions(
        mode=mode,
        language=ctx.str_param("language", "auto"),
        tiling=ctx.str_param("tiling", "auto"),
        detail=ctx.str_param("detail", "original"),
        allow_fallback=ctx.bool_param("allow_fallback", True),
    )

    ctx.report(2, "准备识别")

    try:
        result = recognize(
            source,
            options,
            preference=engine_pref,
            progress=lambda value, message: ctx.report(2 + value * 0.86, message),
        )
    except OcrError as err:
        raise ActionError(str(err)) from err

    ctx.raise_if_cancelled()
    ctx.report(90, "写出结果")

    if not result.blocks:
        # 识别不到内容不算失败，但必须让用户知道，否则会以为程序坏了
        raise ActionError("未识别到任何文字，请确认图片清晰且确实包含文本")

    output_format = ctx.str_param("output_format", "txt")
    if output_format not in OUTPUT_FORMATS:
        output_format = "txt"

    target = Path(ctx.output_path)
    if target.suffix.lower() != OUTPUT_FORMATS[output_format]:
        target = target.with_suffix(OUTPUT_FORMATS[output_format])
        ctx.output_path = str(target)

    try:
        if output_format == "docx":
            atomic_write(target, lambda tmp: _write_docx(tmp, result, ctx))
        elif output_format == "json":
            payload = _serialize_json(result, ctx)
            atomic_write(target, lambda tmp: tmp.write_text(payload, encoding="utf-8"))
        else:
            payload = _render_text(result, ctx)
            atomic_write(target, lambda tmp: tmp.write_text(payload, encoding="utf-8"))
    except ActionError:
        raise
    except OSError as err:
        raise ActionError(f"写入输出文件失败：{err}") from err

    ctx.report(100, "完成")

    summary = f"{len(result.blocks)} 行 · {result.engine.split('.')[-1]}"
    if result.tiles > 1:
        summary += f" · {result.tiles} 切片"
    if result.usage.get("estimatedCostUsd"):
        summary += f" · ${result.usage['estimatedCostUsd']:.4f}"

    return TaskResult(
        output_path=str(target),
        message=summarize_with_warnings(ctx, summary, result.warnings),
    )


register(
    ActionSpec(
        id="image.ocr",
        label="图片提取文字",
        domain="ocr",
        handler=_handler,
        accepts=("jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff", "heic", "heif", "avif"),
        output_ext="txt",
        description="用本地离线引擎或 DeepSeek Vision 识别图片文字，可输出纯文本 / Markdown / Word / JSON",
        params_schema=PARAMS_SCHEMA,
    )
)
