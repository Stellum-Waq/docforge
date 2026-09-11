"""扫描件 → 双层可搜索 PDF。

## 什么是"双层 PDF"

页面外观是扫描原图（保留原始观感与印章笔迹），但在其**下方叠了一层不可见的
文字**。用阅读器看是图片，用搜索、复制、Ctrl+F 时文字又都在 ——
这是档案数字化的标准做法，也让扫描件能进入全文检索体系。

## 实现要点

* **不可见文字**用 PyMuPDF 的 ``render_mode=3``（既不填充也不描边）。
  它写进内容流、能被提取，但完全看不见。
* **每页独立判断**：已有文字层的页（数字原生页）**原样保留**，只对真正
  没有文字层的页做栅格化 + OCR。混合文档（前几页扫描、后几页原生）
  因此能各取所长 —— 原生页不会因为重新栅格化而丢掉矢量清晰度。
* **栅格化重建而非原位叠加**：扫描页重建为一个等尺寸新页（图像 + 文字层）。
  这样做避开了 PDF 页面旋转带来的坐标歧义（``/Rotate`` 非 0 时，
  可见坐标与内容流坐标不一致，原位插入的文字会跑到页面外 —— 实测踩过）。
* 文字块按 OCR 给出的**外接矩形**投放，字号由矩形高度推算，
  因此搜索高亮能精确框住原文位置，而不是散落在页面上。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pymupdf

from ..config import get_settings
from ..ocr import OcrError, OcrOptions, recognize
from ..pdf.geometry import parse_page_range
from .base import ActionContext, ActionError, ActionSpec, TaskResult, atomic_write, register

#: 判定"这一页已经有文字层"的字数阈值。
#: 原生页通常有几百字，扫描页只有 0～几十字（页码水印之类），20 字足够区分。
DEFAULT_MIN_TEXT_CHARS = 20

#: 内置简体中文字体。用它写文字层无需嵌入字体文件，
#: 因此不会因为文字层而让输出 PDF 变大。
FONT_CJK = "china-s"
FONT_LATIN = "helv"


def _needs_cjk(text: str) -> bool:
    ranges = ((0x2E80, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF), (0x3000, 0x303F))
    return any(any(lo <= ord(ch) <= hi for lo, hi in ranges) for ch in text)


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_searchable_pdf(
    source: Path,
    params: dict[str, Any],
    *,
    progress=None,
    log=None,
) -> tuple[pymupdf.Document, dict[str, Any]]:
    """核心实现，返回未保存的文档与统计信息。

    抽成独立函数是为了让 CLI 与 HTTP 两条入口共用同一份逻辑。
    """
    try:
        doc = pymupdf.open(str(source))
    except Exception as err:  # noqa: BLE001
        raise ActionError(f"无法打开 PDF（可能已损坏或加密）：{err}") from err

    if doc.needs_pass:
        doc.close()
        raise ActionError("该 PDF 已加密，请先用「PDF 工具箱」解密")

    total = doc.page_count
    if total == 0:
        doc.close()
        raise ActionError("PDF 没有任何页面")

    pages = parse_page_range(str(params.get("pages", "all")), total)
    if not pages:
        doc.close()
        raise ActionError(f"页码范围「{params.get('pages')}」没有匹配到任何页面（共 {total} 页）")

    dpi = max(72, min(500, _to_int(params.get("dpi"), 200)))
    min_chars = max(0, _to_int(params.get("min_text_chars"), DEFAULT_MIN_TEXT_CHARS))
    force_ocr = bool(params.get("force_ocr", False))
    engine = str(params.get("engine", "local"))

    options = OcrOptions(
        mode="text",
        language=str(params.get("language", "auto")),
        tiling=str(params.get("tiling", "auto")),
        detail=str(params.get("detail", "original")),
    )

    out = pymupdf.open()
    scale = 72.0 / dpi  # 像素 → PDF 点
    temp_dir = get_settings().temp_dir
    stats = {"ocr_pages": 0, "kept_pages": 0, "text_blocks": 0, "empty_pages": 0}
    warnings: list[str] = []

    try:
        for order, page_index in enumerate(pages):
            page = doc[page_index]
            existing = page.get_text().strip()

            # 已经有文字层的页：原样搬运，保住矢量清晰度与体积优势
            if len(existing) >= min_chars and not force_ocr:
                out.insert_pdf(doc, from_page=page_index, to_page=page_index)
                stats["kept_pages"] += 1
                if progress:
                    progress(
                        5 + 90 * (order + 1) / len(pages),
                        f"{order + 1}/{len(pages)} 页已有文字层，原样保留",
                    )
                continue

            if progress:
                progress(5 + 90 * order / len(pages), f"识别第 {order + 1}/{len(pages)} 页")

            pix = page.get_pixmap(dpi=dpi)
            width_pt = pix.width * scale
            height_pt = pix.height * scale

            # 扫描页重建为新页：图像打底 + 不可见文字层。
            # 新页 rotation=0，因此坐标不存在歧义。
            new_page = out.new_page(width=width_pt, height=height_pt)
            new_page.insert_image(new_page.rect, pixmap=pix)

            with tempfile.NamedTemporaryFile(dir=temp_dir, suffix=".png", delete=False) as handle:
                handle.write(pix.tobytes("png"))
                temp_path = Path(handle.name)
            pix = None

            try:
                result = recognize(temp_path, options, preference=engine)
            except OcrError as err:
                # 单页识别失败不该让整份文档失败：保留图像页，只是没有文字层
                warnings.append(f"第 {page_index + 1} 页识别失败：{err}")
                if log:
                    log(f"第 {page_index + 1} 页 OCR 失败：{err}")
                stats["empty_pages"] += 1
                continue
            finally:
                temp_path.unlink(missing_ok=True)

            blocks_written = 0
            blocks_failed = 0
            for block in result.blocks:
                text = block.text.strip()
                if not text:
                    continue

                x0, y0, x1, y1 = block.box
                if (x1 - x0) <= 2 or (y1 - y0) <= 2:
                    continue

                box_height = (y1 - y0) * scale
                # 字号取框高的 0.8 —— 目的是让文字层在位置上贴合原图，
                # 使搜索高亮能框住原文，而不是追求与原文像素级一致。
                fontsize = max(3.0, box_height * 0.8)
                fontname = FONT_CJK if _needs_cjk(text) else FONT_LATIN

                # 基线放在框底部略上方（留出西文的降部空间）
                baseline = pymupdf.Point(x0 * scale, y1 * scale - box_height * 0.18)

                # 这里**刻意用 insert_text 而不是 insert_textbox**：
                # insert_textbox 要求文本必须能塞进给定的矩形，塞不下时
                # 返回负值并且**什么都不写** —— 实测按框高推算的字号经常
                # 刚好越界，于是整页文字层全部静默丢失。
                # insert_text 只按基线投放，没有"必须放得下"的约束，稳定得多。
                try:
                    new_page.insert_text(
                        baseline,
                        text,
                        fontsize=fontsize,
                        fontname=fontname,
                        # 3 = 既不填充也不描边 → 完全不可见，但仍可被提取
                        render_mode=3,
                    )
                    blocks_written += 1
                except Exception:  # noqa: BLE001 - 个别块失败不影响整页
                    blocks_failed += 1

            stats["ocr_pages"] += 1
            stats["text_blocks"] += blocks_written
            stats["blocks_failed"] = stats.get("blocks_failed", 0) + blocks_failed
            if blocks_written == 0:
                stats["empty_pages"] += 1
                warnings.append(f"第 {page_index + 1} 页未能写入任何文字层")

        if out.page_count == 0:
            raise ActionError("没有生成任何页面")
    except Exception:
        out.close()
        raise
    finally:
        doc.close()

    return out, {**stats, "total": total, "processed": len(pages), "warnings": warnings}


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "engine": {
            "type": "string",
            "enum": ["local", "cloud", "auto"],
            "default": "local",
            "title": "识别引擎",
            "description": "local=本地离线（档案场景推荐，文件不出本机）；cloud=DeepSeek Vision",
        },
        "dpi": {
            "type": "integer",
            "default": 200,
            "minimum": 72,
            "maximum": 500,
            "title": "栅格化 DPI",
            "description": "150=体积小，200=均衡（推荐），300=清晰但文件大",
        },
        "pages": {"type": "string", "default": "all", "title": "页码范围", "description": "all / 1-3 / 1,5,7"},
        "force_ocr": {
            "type": "boolean",
            "default": False,
            "title": "强制重新识别所有页",
            "description": "已有文字层的页也重新 OCR（会丢失矢量清晰度，一般不需要）",
        },
        "min_text_chars": {
            "type": "integer",
            "default": DEFAULT_MIN_TEXT_CHARS,
            "minimum": 0,
            "maximum": 500,
            "title": "判定已有文字层的字数",
            "description": "页面文字超过该字数就认为它是原生页，原样保留",
        },
        "language": {"type": "string", "enum": ["auto", "zh", "en"], "default": "auto", "title": "语言提示"},
        "tiling": {"type": "string", "enum": ["auto", "off", "always"], "default": "auto", "title": "切片策略"},
    },
    "required": ["engine"],
}


def _handler(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    target = Path(ctx.output_path)

    ctx.report(3, "分析文档")
    ctx.raise_if_cancelled()

    doc, stats = build_searchable_pdf(
        source,
        ctx.params,
        progress=lambda value, message: ctx.report(value, message),
        log=ctx.log,
    )

    ctx.report(95, "写入结果")
    ctx.raise_if_cancelled()

    try:
        atomic_write(target, lambda tmp: doc.save(str(tmp), garbage=3, deflate=True, clean=True))
    except Exception as err:  # noqa: BLE001
        raise ActionError(f"保存 PDF 失败：{err}") from err
    finally:
        doc.close()

    ctx.report(100, "完成")
    for warning in stats["warnings"]:
        ctx.log(warning)

    summary = f"识别 {stats['ocr_pages']} 页 · 保留 {stats['kept_pages']} 页 · {stats['text_blocks']} 个文字块"
    if stats["empty_pages"]:
        summary += f" · {stats['empty_pages']} 页未识别到文字"
    return TaskResult(output_path=str(target), message=summary)


register(
    ActionSpec(
        id="pdf.searchable",
        label="扫描件转可搜索 PDF",
        domain="ocr",
        handler=_handler,
        accepts=("pdf",),
        output_ext="pdf",
        description="给扫描件叠加不可见文字层：外观仍是原图，但文字可搜索、可复制、可检索",
        params_schema=PARAMS_SCHEMA,
    )
)
