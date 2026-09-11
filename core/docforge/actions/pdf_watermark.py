"""PDF 加水印动作 —— 对应需求 4。

## 与图片水印的关键差异

PDF 水印用的是**矢量文字**（PyMuPDF 的 ``insert_text``），而不是先渲染成位图再贴上去。
好处是任意缩放都清晰、文件体积小得多、而且文字仍然**可被复制与检索** ——
把"机密"渲染成图片贴上去，用户就没法选中它了。

## 旋转页面的坑

实测发现：在 ``rotation=90`` 的页面上按 ``page.rect`` 的可见坐标插入文字，
**部分内容会落到页面外**（插入类 API 的坐标语义在旋转页面上有歧义）。
因此这里采用"临时归零旋转"策略：

1. 记下原始旋转角，``page.set_rotation(0)``
2. 此时 ``rect`` 与 ``mediabox`` 一致，坐标无歧义，按它布局
3. 打完水印恢复原旋转角

水印是写进页面内容流的，会随页面一起旋转显示，视觉效果完全正确。
这套策略对 0/90/180/270 四种旋转都实测通过。

## 任意角度旋转

``insert_text`` 的 ``rotate`` 参数只接受 90° 的倍数。任意角度靠 ``morph``
参数实现 —— 它接收 (固定点, 变换矩阵)，把文字绕指定点旋转。
这里把固定点设为水印中心，因此定位逻辑可以统一按"中心点"来算。
"""

from __future__ import annotations

import datetime as _dt
import io
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageOps

from ..pdf.geometry import expand_template, parse_page_range, placement_centers, rotated_extent
from .base import ActionContext, ActionError, ActionSpec, TaskResult, atomic_write, register

#: 内置字体。china-s 是 PDF 标准 CJK 字体之一，随阅读器提供，无需嵌入字体文件，
#: 因此生成的 PDF 不会因为嵌字体而变胖。
FONT_CJK = "china-s"
FONT_LATIN = "helv"

#: 宽高比的经验修正：文字基线到视觉中心的偏移，约为字号的这个比例。
#: 用它把"基线坐标"换算成"视觉中心"，否则水印看起来会偏下。
_BASELINE_TO_CENTER = 0.32


def _hex_to_rgb(value: str, default: tuple[float, float, float] = (1, 1, 1)) -> tuple[float, float, float]:
    """把 #RRGGBB 转成 PyMuPDF 需要的 0–1 浮点三元组。"""
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return default
    try:
        return (int(text[0:2], 16) / 255, int(text[2:4], 16) / 255, int(text[4:6], 16) / 255)
    except ValueError:
        return default


def _needs_cjk(text: str) -> bool:
    ranges = ((0x2E80, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF), (0x3000, 0x303F))
    return any(any(lo <= ord(ch) <= hi for lo, hi in ranges) for ch in text)


def _pick_font(text: str, explicit: str) -> str:
    if explicit:
        return explicit
    return FONT_CJK if _needs_cjk(text) else FONT_LATIN


def _build_logo_tile(
    logo_path: str,
    *,
    target_width: float,
    target_dpi: int,
    opacity: float,
    rotation: float,
) -> bytes:
    """把 Logo 处理成可插入 PDF 的 PNG 字节流。

    PDF 里的图片是有分辨率上限的位图，所以按目标物理尺寸 × DPI 来栅格化，
    保证打印时也够清晰。
    """
    path = Path(logo_path)
    if not path.is_file():
        raise ActionError(f"水印图片不存在：{logo_path}")

    try:
        with Image.open(path) as opened:
            logo = ImageOps.exif_transpose(opened).convert("RGBA")
    except OSError as err:
        raise ActionError(f"无法读取水印图片 {path.name}：{err}") from err

    # 目标像素宽度 = 物理宽度(点) / 72 * DPI
    pixel_width = max(8, int(round(target_width / 72 * max(72, target_dpi))))
    ratio = pixel_width / max(1, logo.width)
    logo = logo.resize((pixel_width, max(1, int(round(logo.height * ratio)))), Image.Resampling.LANCZOS)

    if opacity < 1.0:
        alpha = logo.getchannel("A").point(lambda v: int(v * max(0.0, min(1.0, opacity))))
        logo.putalpha(alpha)

    if abs(rotation) > 0.01:
        logo = logo.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(0, 0, 0, 0))

    buffer = io.BytesIO()
    logo.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _stamp_text(
    page: pymupdf.Page,
    text: str,
    *,
    fontsize: float,
    font: str,
    color: tuple[float, float, float],
    stroke_color: tuple[float, float, float],
    stroke_width: float,
    opacity: float,
    angle: float,
    centers: list[tuple[float, float]],
    overlay: bool,
) -> None:
    """把文字水印盖到页面上。"""
    width = pymupdf.get_text_length(text, fontname=font, fontsize=fontsize)
    # 基线相对视觉中心的偏移，用来把"中心点"换算成 insert_text 需要的基线起点
    baseline_shift = fontsize * _BASELINE_TO_CENTER
    matrix = pymupdf.Matrix(angle) if abs(angle) > 0.01 else None

    for cx, cy in centers:
        point = pymupdf.Point(cx - width / 2, cy + baseline_shift)
        try:
            page.insert_text(
                point,
                text,
                fontsize=fontsize,
                fontname=font,
                color=stroke_color,
                fill=color,
                # 2 = 先填充再描边；不描边时用 0（纯填充）
                render_mode=2 if stroke_width > 0 else 0,
                border_width=max(0.05, stroke_width),
                # 绕水印中心旋转
                morph=(pymupdf.Point(cx, cy), matrix) if matrix else None,
                fill_opacity=opacity,
                stroke_opacity=opacity,
                overlay=overlay,
            )
        except Exception as err:  # noqa: BLE001 - 字体/编码问题要归因到具体原因
            raise ActionError(f"写入水印文字失败（字体 {font}）：{err}") from err


def _stamp_image(
    page: pymupdf.Page,
    stream: bytes,
    *,
    width: float,
    height: float,
    angle: float,
    centers: list[tuple[float, float]],
    overlay: bool,
) -> None:
    """把 Logo 水印盖到页面上。

    Logo 的旋转已经在栅格化阶段烘焙进像素，这里只需按旋转后的外接矩形摆放。
    """
    extent = rotated_extent(width, height, angle)
    for cx, cy in centers:
        rect = pymupdf.Rect(
            cx - extent.width / 2,
            cy - extent.height / 2,
            cx + extent.width / 2,
            cy + extent.height / 2,
        )
        try:
            page.insert_image(rect, stream=stream, keep_proportion=True, overlay=overlay)
        except Exception as err:  # noqa: BLE001
            raise ActionError(f"写入水印图片失败：{err}") from err


def apply_pdf_watermark(
    source: Path,
    params: dict[str, Any],
    *,
    log=None,
) -> dict[str, Any]:
    """给 PDF 加水印，返回**未保存的**文档对象。

    抽成独立函数是为了让**预览接口能复用同一份代码** —— 预览与正式输出
    必须同源，否则"所见即所得"就是空话。因此这里只负责"改内容"，
    保存由调用方决定（正式任务存盘、预览直接渲染）。

    返回：``{"pages": 已处理页数, "document": 文档对象, "total": 总页数}``。
    调用方**必须**负责关闭 ``document``。
    """
    try:
        doc = pymupdf.open(str(source))
    except Exception as err:  # noqa: BLE001
        raise ActionError(f"无法打开 PDF（可能已损坏或加密）：{err}") from err

    try:
        if doc.needs_pass:
            raise ActionError("该 PDF 已加密，请先在「PDF 工具箱」中解密后再加水印")

        total = doc.page_count
        if total == 0:
            raise ActionError("PDF 没有任何页面")

        pages = parse_page_range(str(params.get("pages", "all")), total)
        if not pages:
            raise ActionError(f"页码范围「{params.get('pages')}」没有匹配到任何页面（共 {total} 页）")

        mode = str(params.get("mode", "text"))
        opacity = _clamp(_to_float(params.get("opacity"), 0.35), 0.02, 1.0)
        angle = _to_float(params.get("rotation"), -35.0)
        position = str(params.get("position", "center"))
        margin_ratio = _clamp(_to_float(params.get("margin_ratio"), 0.05), 0.0, 0.3)
        tile_gap = _to_float(params.get("tile_gap_ratio"), 0.6)
        # overlay=False 把水印放到内容下方，适合做"底纹"而不遮挡正文
        overlay = bool(params.get("overlay", True))

        fontsize_ratio = _clamp(_to_float(params.get("font_size_ratio"), 0.07), 0.01, 0.5)
        scale_ratio = _clamp(_to_float(params.get("scale_ratio"), 0.28), 0.02, 1.0)
        stroke_width = max(0.0, _to_float(params.get("stroke_width"), 0.6))
        if not bool(params.get("stroke", True)):
            stroke_width = 0.0

        logo_stream: bytes | None = None
        logo_size: tuple[float, float] = (0.0, 0.0)

        if mode == "image":
            logo_path = str(params.get("image_path") or "")
            if not logo_path:
                raise ActionError("图片水印模式缺少水印图片路径")
            # 目标宽度按首屏页面短边计算，各页尺寸不一致时也能保持视觉比例一致
            first_rect = doc[pages[0]].rect
            target_width = min(first_rect.width, first_rect.height) * scale_ratio
            logo_stream = _build_logo_tile(
                logo_path,
                target_width=target_width,
                target_dpi=int(_to_float(params.get("logo_dpi"), 300)),
                opacity=opacity,
                rotation=angle,
            )
            with Image.open(io.BytesIO(logo_stream)) as probe:
                ratio = target_width / max(1, probe.width)
                logo_size = (target_width, probe.height * ratio)

        template = str(params.get("text") or "机密")
        unique_per_file = bool(params.get("unique_per_file", False))
        font_override = str(params.get("font_name") or "")
        color = _hex_to_rgb(str(params.get("color") or "#FFFFFF"))
        stroke_color = _hex_to_rgb(str(params.get("stroke_color") or "#000000"), (0, 0, 0))

        now = _dt.datetime.now()
        base_values = {
            "filename": source.name,
            "stem": source.stem,
            "pages": total,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "year": now.strftime("%Y"),
            "month": now.strftime("%m"),
            "day": now.strftime("%d"),
        }

        stamped = 0
        for page_index in pages:
            page = doc[page_index]

            # 临时归零旋转：让坐标语义无歧义（见模块文档）
            original_rotation = page.rotation
            if original_rotation:
                page.set_rotation(0)
            rect = page.rect

            values = {**base_values, "page": page_index + 1, "index": page_index + 1}

            if mode == "image" and logo_stream is not None:
                centers = placement_centers(
                    rect.width,
                    rect.height,
                    item_width=logo_size[0],
                    item_height=logo_size[1],
                    position=position,
                    margin_ratio=margin_ratio,
                    x_ratio=_to_float(params.get("x_ratio"), 0.5),
                    y_ratio=_to_float(params.get("y_ratio"), 0.5),
                    tile_gap_ratio=tile_gap,
                    angle=angle,
                )
                _stamp_image(
                    page,
                    logo_stream,
                    width=logo_size[0],
                    height=logo_size[1],
                    angle=angle,
                    centers=centers,
                    overlay=overlay,
                )
            else:
                text = expand_template(template, values) if (unique_per_file or "{" in template) else template
                if not text.strip():
                    raise ActionError("水印文字为空")
                font = _pick_font(text, font_override)
                fontsize = max(4.0, min(rect.width, rect.height) * fontsize_ratio)
                text_width = pymupdf.get_text_length(text, fontname=font, fontsize=fontsize)
                text_height = fontsize * 1.2

                centers = placement_centers(
                    rect.width,
                    rect.height,
                    item_width=text_width,
                    item_height=text_height,
                    position=position,
                    margin_ratio=margin_ratio,
                    x_ratio=_to_float(params.get("x_ratio"), 0.5),
                    y_ratio=_to_float(params.get("y_ratio"), 0.5),
                    tile_gap_ratio=tile_gap,
                    angle=angle,
                )
                _stamp_text(
                    page,
                    text,
                    fontsize=fontsize,
                    font=font,
                    color=color,
                    stroke_color=stroke_color,
                    stroke_width=stroke_width,
                    opacity=opacity,
                    angle=angle,
                    centers=centers,
                    overlay=overlay,
                )

            if original_rotation:
                page.set_rotation(original_rotation)
            stamped += 1

        if log:
            log(f"已为 {stamped} 页添加水印")

        # 写入元数据水印（不可见但可溯源），对应设计文档 §1-C 的"元数据水印"
        if bool(params.get("metadata_mark", False)):
            mark = expand_template(str(params.get("metadata_text") or "DocForge"), base_values)
            doc.set_metadata({**(doc.metadata or {}), "subject": mark, "producer": "文枢 DocForge"})

        return {"pages": stamped, "document": doc, "total": total}
    except Exception:
        doc.close()
        raise


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["text", "image"], "default": "text", "title": "水印类型"},
        "text": {
            "type": "string",
            "default": "机密",
            "title": "水印文字",
            "description": "支持 {page} {pages} {filename} {date} 等变量，每页可不同",
        },
        "image_path": {"type": "string", "default": "", "title": "水印图片路径"},
        "font_size_ratio": {
            "type": "number", "default": 0.07, "minimum": 0.01, "maximum": 0.4,
            "title": "字号比例", "description": "占页面短边的比例",
        },
        "scale_ratio": {
            "type": "number", "default": 0.28, "minimum": 0.02, "maximum": 1.0,
            "title": "Logo 宽度比例",
        },
        "color": {"type": "string", "default": "#C81E1E", "title": "文字颜色"},
        "opacity": {"type": "number", "default": 0.35, "minimum": 0.02, "maximum": 1, "title": "不透明度"},
        "rotation": {"type": "number", "default": -35, "minimum": -180, "maximum": 180, "title": "旋转角度"},
        "position": {"type": "string", "enum": list(("top-left", "top-center", "top-right", "middle-left", "center", "middle-right", "bottom-left", "bottom-center", "bottom-right", "custom", "tile")), "default": "center", "title": "位置"},
        "x_ratio": {"type": "number", "default": 0.5, "minimum": 0, "maximum": 1, "title": "自定义 X"},
        "y_ratio": {"type": "number", "default": 0.5, "minimum": 0, "maximum": 1, "title": "自定义 Y"},
        "margin_ratio": {"type": "number", "default": 0.05, "minimum": 0, "maximum": 0.3, "title": "边距比例"},
        "tile_gap_ratio": {"type": "number", "default": 0.6, "minimum": 0.05, "maximum": 3, "title": "平铺间距"},
        "stroke": {"type": "boolean", "default": True, "title": "文字描边"},
        "stroke_color": {"type": "string", "default": "#000000", "title": "描边颜色"},
        "stroke_width": {"type": "number", "default": 0.6, "minimum": 0, "maximum": 5, "title": "描边宽度"},
        "pages": {"type": "string", "default": "all", "title": "页码范围", "description": "all / 1-3 / 1,5,7 / 2-"},
        "overlay": {"type": "boolean", "default": True, "title": "覆盖在内容之上", "description": "关闭则作为底纹放在正文下方"},
        "unique_per_file": {"type": "boolean", "default": False, "title": "使用模板变量"},
        "font_name": {"type": "string", "default": "", "title": "字体名", "description": "留空自动选择（中文用 china-s，西文用 helv）"},
        "logo_dpi": {"type": "integer", "default": 300, "minimum": 72, "maximum": 600, "title": "Logo 栅格化 DPI"},
        "metadata_mark": {"type": "boolean", "default": False, "title": "写入元数据水印", "description": "在 PDF 元数据中写入标记，不可见但可溯源"},
        "metadata_text": {"type": "string", "default": "内部文件", "title": "元数据水印内容"},
    },
    "required": ["mode"],
}


def _handler(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    target = Path(ctx.output_path)

    ctx.report(5, "打开 PDF")
    ctx.raise_if_cancelled()

    result = apply_pdf_watermark(source, ctx.params, log=ctx.log)
    doc = result["document"]

    ctx.report(85, "保存结果")
    ctx.raise_if_cancelled()

    try:
        def _write(tmp: Path) -> None:
            # garbage=3 清理无用对象，deflate=True 压缩内容流。
            # 加水印会产生大量新对象，不做这一步文件会明显变大。
            doc.save(str(tmp), garbage=3, deflate=True, clean=True)

        atomic_write(target, _write)
    except Exception as err:  # noqa: BLE001
        raise ActionError(f"保存 PDF 失败：{err}") from err
    finally:
        doc.close()

    ctx.report(100, "完成")
    return TaskResult(
        output_path=str(target),
        message=f"{result['pages']}/{result['total']} 页 · {ctx.str_param('mode')} 水印",
    )


register(
    ActionSpec(
        id="pdf.watermark",
        label="PDF 加水印",
        domain="pdf",
        handler=_handler,
        accepts=("pdf",),
        output_ext="pdf",
        description="给 PDF 添加文字或 Logo 水印，支持平铺、任意角度、页码范围、每页不同内容",
        params_schema=PARAMS_SCHEMA,
    )
)
