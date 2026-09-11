"""图片水印动作 —— 对应需求 5「给照片加水印」。

覆盖设计文档 §1-D 的全部要点：
  * 文字水印 / 图片 Logo 水印
  * 九宫格定位 + 自由坐标 + 平铺（防盗图）
  * 智能适配：水印尺寸按原图短边比例缩放，小图不糊、大图不飘
  * **EXIF 方向自动校正后再打水印**（这是最常见的坑：手机上拍的照片
    带 Orientation 标记，不先摆正会导致水印横竖颠倒）
  * 描边 / 阴影，保证水印在深浅背景上都可读
  * 每张唯一水印（模板变量，可批量溯源）
  * 保留 / 剥离 EXIF（隐私）
  * 输出格式与质量可调
"""

from __future__ import annotations

import datetime as _dt
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .base import ActionContext, ActionError, ActionSpec, TaskResult, atomic_write, register

# --------------------------------------------------------------------------- #
# 字体解析                                                                      #
# --------------------------------------------------------------------------- #

# 按优先级排列的字体候选。Windows 自带，无需随包分发字体文件（省体积、免授权问题）。
_CJK_FONTS = ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "Deng.ttf", "simsun.ttc")
_LATIN_FONTS = ("segoeui.ttf", "arial.ttf", "calibri.ttf", "tahoma.ttf")

# 出现这些区间的字符就认为需要 CJK 字体，否则会渲染成方块
_CJK_RANGES = ((0x2E80, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF), (0x3000, 0x303F))


def _needs_cjk(text: str) -> bool:
    return any(any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in text)


@lru_cache(maxsize=32)
def _font_path(candidates: tuple[str, ...]) -> str | None:
    fonts_dir = Path("C:/Windows/Fonts")
    for name in candidates:
        candidate = fonts_dir / name
        if candidate.is_file():
            return str(candidate)
    # 非 Windows 或字体缺失时，退回 Pillow 内置位图字体（虽然小，但至少不崩）
    return None


@lru_cache(maxsize=128)
def _load_font(size: int, cjk: bool, explicit: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载字体。带缓存 —— truetype 解析有开销，批量处理上千张图时差别明显。"""
    if explicit:
        try:
            return ImageFont.truetype(explicit, size)
        except OSError as err:
            raise ActionError(f"无法加载指定字体 {explicit}：{err}") from err

    path = _font_path(_CJK_FONTS if cjk else _LATIN_FONTS)
    if path is None:
        # 兜底：Pillow 内置字体不支持 size 参数，单独处理
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


# --------------------------------------------------------------------------- #
# 参数解析                                                                      #
# --------------------------------------------------------------------------- #

_POSITIONS = (
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
    "custom", "tile",
)

# 扩展名归一化：把 jpeg→jpg、tiff→tif，后续只处理 5 种规范形式
_EXT_ALIASES = {
    "jpg": "jpg", "jpeg": "jpg", "png": "png",
    "webp": "webp", "bmp": "bmp", "tif": "tif", "tiff": "tif",
}


def _expand_template(template: str, ctx: ActionContext, index: int) -> str:
    """展开水印文本里的模板变量，实现"每张唯一水印"。

    用 format_map + defaultdict 而不是 str.format，这样模板里出现未知变量
    （例如用户手误写了 {foo}）时保留原样，而不是抛 KeyError 让整批任务挂掉。
    """
    source = Path(ctx.file_path)
    now = _dt.datetime.now()

    values: dict[str, Any] = {
        "filename": source.name,
        "stem": source.stem,
        "ext": source.suffix.lstrip("."),
        "index": index,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "year": now.strftime("%Y"),
        "month": now.strftime("%m"),
        "day": now.strftime("%d"),
    }

    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, IndexError):
        # 花括号不配对等语法错误：原样返回，不打断批处理
        return template


def _hex_to_rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) == 8:
        try:
            return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), int(text[6:8], 16))
        except ValueError:
            pass
    if len(text) == 6:
        try:
            return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), alpha)
        except ValueError:
            pass
    return (255, 255, 255, alpha)


def _anchor_for(position: str) -> str:
    return {
        "top-left": "la", "top-center": "ma", "top-right": "ra",
        "middle-left": "lm", "center": "mm", "middle-right": "rm",
        "bottom-left": "ld", "bottom-center": "md", "bottom-right": "rd",
    }.get(position, "rd")


# --------------------------------------------------------------------------- #
# 水印图块构建                                                                  #
# --------------------------------------------------------------------------- #

def _build_text_tile(
    text: str,
    *,
    font_size: int,
    color: str,
    opacity: float,
    rotation: float,
    stroke: bool,
    stroke_color: str,
    stroke_width: int,
    shadow: bool,
    font_path: str | None,
) -> Image.Image:
    """把文字渲染成一张带透明通道的小图，并按要求旋转。"""
    font = _load_font(font_size, _needs_cjk(text), font_path)

    padding = max(8, font_size // 3)
    stroke_w = max(1, stroke_width) if stroke else 0
    # 旋转后外接矩形会变大，留出足够画布避免被裁掉
    pad = padding + stroke_w + (font_size if shadow else 0)
    # 用 textbbox 精确测量，比 textlength 更能处理多行与描边
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = probe.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke_w, align="center")
    text_w = max(1, bbox[2] - bbox[0])
    text_h = max(1, bbox[3] - bbox[1])

    tile = Image.new("RGBA", (text_w + pad * 2, text_h + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)

    fill = _hex_to_rgba(color, int(round(255 * opacity)))
    origin = (pad - bbox[0], pad - bbox[1])

    if shadow:
        offset = max(1, font_size // 22)
        draw.multiline_text(
            (origin[0] + offset, origin[1] + offset),
            text,
            font=font,
            fill=(0, 0, 0, int(round(120 * opacity))),
            align="center",
            stroke_width=stroke_w,
            stroke_fill=(0, 0, 0, int(round(120 * opacity))),
        )

    draw.multiline_text(
        origin,
        text,
        font=font,
        fill=fill,
        align="center",
        stroke_width=stroke_w,
        stroke_fill=_hex_to_rgba(stroke_color, int(round(220 * opacity))),
    )

    if abs(rotation) > 0.01:
        tile = tile.rotate(
            rotation,
            resample=Image.Resampling.BICUBIC,
            expand=True,
            # 用自身做填充，避免旋转后出现黑色直角
            fillcolor=(0, 0, 0, 0),
        )
    return tile


def _build_image_tile(
    logo_path: str,
    *,
    target_width: int,
    opacity: float,
    rotation: float,
) -> Image.Image:
    """把 Logo 图片缩放成图块。"""
    path = Path(logo_path)
    if not path.is_file():
        raise ActionError(f"水印图片不存在：{logo_path}")

    try:
        logo = Image.open(path)
        logo = ImageOps.exif_transpose(logo)
        logo = logo.convert("RGBA")
    except OSError as err:
        raise ActionError(f"无法读取水印图片 {path.name}：{err}") from err

    target_width = max(8, target_width)
    ratio = target_width / max(1, logo.width)
    target_height = max(8, int(round(logo.height * ratio)))
    logo = logo.resize((target_width, target_height), Image.Resampling.LANCZOS)

    # 通过缩放 alpha 通道实现整体透明度，比 blend 更精确
    if opacity < 1.0:
        alpha = logo.getchannel("A").point(lambda v: int(v * opacity))
        logo.putalpha(alpha)

    if abs(rotation) > 0.01:
        logo = logo.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(0, 0, 0, 0))
    return logo


def _composite(base: Image.Image, tile: Image.Image, x: int, y: int) -> None:
    """把图块叠加到基底上。Pillow 的 alpha_composite 会自行裁剪越界部分。"""
    base.alpha_composite(tile, dest=(int(x), int(y)))


# --------------------------------------------------------------------------- #
# 主处理流程                                                                    #
# --------------------------------------------------------------------------- #

def apply_watermark(ctx: ActionContext, index: int = 0) -> TaskResult:
    params = ctx.params
    source = Path(ctx.file_path)

    ctx.report(5, "读取图片")
    try:
        with Image.open(source) as opened:
            # EXIF 必须在转置之前取出；转置后 Orientation 已被应用
            exif = None
            if ctx.bool_param("keep_exif", True):
                try:
                    exif = opened.getexif()
                except Exception:
                    exif = None

            ctx.raise_if_cancelled()

            # 关键：先按 EXIF 摆正，再打水印，否则水印会跟着图片一起歪
            image = ImageOps.exif_transpose(opened) if ctx.bool_param("auto_orient", True) else opened.copy()
            image = image.convert("RGBA")
            # 转置后方向已烘焙进像素，必须把 Orientation 标记清掉，否则看图软件会二次旋转
            if exif is not None and 0x0112 in exif:
                del exif[0x0112]
    except ActionError:
        raise
    except OSError as err:
        raise ActionError(f"无法打开图片（可能已损坏或格式不支持）：{err}") from err

    width, height = image.size
    short_side = min(width, height)

    mode = ctx.str_param("mode", "text")
    opacity = max(0.0, min(1.0, ctx.float_param("opacity", 0.45)))
    rotation = ctx.float_param("rotation", -30.0)
    position = ctx.str_param("position", "bottom-right")
    if position not in _POSITIONS:
        position = "bottom-right"
    margin_ratio = max(0.0, ctx.float_param("margin_ratio", 0.03))

    ctx.raise_if_cancelled()
    ctx.report(30, "生成水印图层")

    if mode == "image":
        logo_path = ctx.str_param("image_path", "")
        if not logo_path:
            raise ActionError("图片水印模式缺少水印图片路径")
        tile = _build_image_tile(
            logo_path,
            target_width=int(short_side * max(0.01, ctx.float_param("scale_ratio", 0.22))),
            opacity=opacity,
            rotation=rotation,
        )
    else:
        template = ctx.str_param("text", "机密")
        text = _expand_template(template, ctx, index) if ctx.bool_param("unique_per_file", False) else template
        if not text.strip():
            raise ActionError("水印文字为空")
        font_size = max(10, int(short_side * max(0.005, ctx.float_param("font_size_ratio", 0.045))))
        tile = _build_text_tile(
            text,
            font_size=font_size,
            color=ctx.str_param("color", "#FFFFFF"),
            opacity=opacity,
            rotation=rotation,
            stroke=ctx.bool_param("stroke", True),
            stroke_color=ctx.str_param("stroke_color", "#000000"),
            stroke_width=ctx.int_param("stroke_width", max(1, font_size // 18)),
            shadow=ctx.bool_param("shadow", False),
            font_path=(ctx.str_param("font_path") or None),
        )

    ctx.raise_if_cancelled()
    ctx.report(60, "叠加水印")

    margin = int(short_side * margin_ratio)
    tile_w, tile_h = tile.size

    if position == "tile":
        gap_ratio = max(0.05, ctx.float_param("tile_gap_ratio", 0.35))
        step_x = max(20, int(tile_w * (1 + gap_ratio)))
        step_y = max(20, int(tile_h * (1 + gap_ratio)))

        # 平铺时先把所有图块叠到一张全尺寸透明层上，再一次性合成。
        # 逐个直接合成到底图会在重叠处反复混合、出现深色斑点。
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        # 从负偏移开始，保证边缘也铺满（否则四角会有空白）
        y = -tile_h // 2
        row = 0
        while y < height + tile_h:
            # 奇偶行错开半格，形成更自然的砖砌排布
            x_start = -tile_w // 2 + (step_x // 2 if row % 2 else 0)
            x = x_start
            while x < width + tile_w:
                layer.alpha_composite(tile, dest=(x, y))
                x += step_x
            y += step_y
            row += 1
            ctx.raise_if_cancelled()
        _composite(image, layer, 0, 0)
        layer.close()
    elif position == "custom":
        x = int(width * ctx.float_param("x_ratio", 0.5) - tile_w / 2)
        y = int(height * ctx.float_param("y_ratio", 0.5) - tile_h / 2)
        _composite(image, tile, x, y)
    else:
        # 九宫格定位。anchor 形如 "rd"（right-bottom）：
        #   anchor[0] ∈ {l, m, r} 控制水平，anchor[1] ∈ {a, m, d} 控制垂直
        anchor = _anchor_for(position)
        if anchor[0] == "l":
            px = margin
        elif anchor[0] == "r":
            px = width - margin - tile_w
        else:
            px = (width - tile_w) // 2

        if anchor[1] == "a":
            py = margin
        elif anchor[1] == "d":
            py = height - margin - tile_h
        else:
            py = (height - tile_h) // 2

        _composite(image, tile, px, py)

    tile.close()

    ctx.raise_if_cancelled()
    ctx.report(80, "保存结果")

    # ---- 决定最终输出格式 ----
    # 顺序至关重要：**必须先定死格式，再决定要不要拍平透明通道**。
    # 反过来写（先按源扩展名判断、最后才按输出扩展名保存）会导致
    # "判定为 PNG 所以不拍平，却按 JPEG 保存" → RGBA 写 JPEG 直接崩溃。
    normalized = _EXT_ALIASES

    fmt_key = ctx.str_param("output_format", "same").lower()
    if fmt_key in normalized:
        out_ext = normalized[fmt_key]
    elif fmt_key == "same":
        src_ext = normalized.get(source.suffix.lstrip(".").lower())
        out_ext = src_ext or "png"
    else:
        out_ext = normalized.get(source.suffix.lstrip(".").lower()) or "png"

    target = Path(ctx.output_path)
    if target.suffix.lstrip(".").lower() != out_ext:
        # 扩展名与内容不一致时以内容为准改写路径，否则下游看图软件会认错格式
        target = target.with_suffix(f".{out_ext}")
        ctx.output_path = str(target)

    pil_format = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "bmp": "BMP", "tif": "TIFF"}[out_ext]

    # JPEG / BMP 不支持透明通道，必须拍平到白底，否则报错或出现黑块。
    # TIFF / PNG / WebP 都能带 alpha，保持原样。
    if pil_format in {"JPEG", "BMP"}:
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
        final = flat
    else:
        final = image

    quality = max(1, min(100, ctx.int_param("quality", 92)))
    save_kwargs: dict[str, Any] = {"optimize": True}
    if pil_format == "JPEG":
        save_kwargs.update(quality=quality, subsampling=0, progressive=True)
    elif pil_format == "WEBP":
        save_kwargs.update(quality=quality, method=5)
    elif pil_format == "PNG":
        save_kwargs.update(compress_level=6)
    elif pil_format == "TIFF":
        save_kwargs = {"compression": "tiff_deflate"}

    # 保留 EXIF（DPI 等也一并带上）；用户选择剥离时不传即可
    if exif is not None and pil_format in {"JPEG", "WEBP", "PNG"} and len(exif):
        save_kwargs["exif"] = exif.tobytes()
    if "dpi" in (image.info or {}):
        save_kwargs["dpi"] = image.info["dpi"]

    def _write(tmp: Path) -> None:
        final.save(tmp, format=pil_format, **save_kwargs)

    try:
        atomic_write(target, _write)
    except ActionError:
        raise
    except OSError as err:
        raise ActionError(f"写入输出文件失败：{err}") from err
    finally:
        final.close()
        if final is not image:
            image.close()

    ctx.report(100, "完成")
    return TaskResult(output_path=str(target), message=f"{width}×{height} → {pil_format}")


def _handler(ctx: ActionContext) -> TaskResult:
    # index 由队列层通过 params 里的隐藏字段传入，用于 {index} 模板变量
    return apply_watermark(ctx, index=ctx.int_param("__index", 0))


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["text", "image"], "default": "text", "title": "水印类型"},
        "text": {"type": "string", "default": "机密", "title": "水印文字", "description": "支持 {filename} {date} {index} 等变量"},
        "image_path": {"type": "string", "default": "", "title": "水印图片"},
        "font_size_ratio": {"type": "number", "default": 0.045, "minimum": 0.005, "maximum": 0.4, "title": "字号比例"},
        "scale_ratio": {"type": "number", "default": 0.22, "minimum": 0.01, "maximum": 1.0, "title": "Logo 宽度比例"},
        "color": {"type": "string", "default": "#FFFFFF", "title": "文字颜色"},
        "opacity": {"type": "number", "default": 0.45, "minimum": 0, "maximum": 1, "title": "不透明度"},
        "rotation": {"type": "number", "default": -30, "minimum": -180, "maximum": 180, "title": "旋转角度"},
        "position": {"type": "string", "enum": list(_POSITIONS), "default": "bottom-right", "title": "位置"},
        "x_ratio": {"type": "number", "default": 0.5, "minimum": 0, "maximum": 1, "title": "自定义 X"},
        "y_ratio": {"type": "number", "default": 0.5, "minimum": 0, "maximum": 1, "title": "自定义 Y"},
        "margin_ratio": {"type": "number", "default": 0.03, "minimum": 0, "maximum": 0.3, "title": "边距比例"},
        "tile_gap_ratio": {"type": "number", "default": 0.35, "minimum": 0.05, "maximum": 3, "title": "平铺间距"},
        "stroke": {"type": "boolean", "default": True, "title": "描边"},
        "stroke_color": {"type": "string", "default": "#000000", "title": "描边颜色"},
        "stroke_width": {"type": "integer", "default": 2, "minimum": 0, "maximum": 20, "title": "描边宽度"},
        "shadow": {"type": "boolean", "default": False, "title": "阴影"},
        "auto_orient": {"type": "boolean", "default": True, "title": "按 EXIF 自动摆正"},
        "keep_exif": {"type": "boolean", "default": True, "title": "保留 EXIF"},
        "unique_per_file": {"type": "boolean", "default": False, "title": "每张唯一水印"},
        "output_format": {"type": "string", "enum": ["same", "jpg", "png", "webp"], "default": "same", "title": "输出格式"},
        "quality": {"type": "integer", "default": 92, "minimum": 1, "maximum": 100, "title": "输出质量"},
    },
    "required": ["mode"],
}


register(
    ActionSpec(
        id="image.watermark",
        label="图片加水印",
        domain="image",
        handler=_handler,
        # 含 HEIC/HEIF/AVIF：pillow-heif 已在 `docforge.imaging` 里注册，
        # 因此 iPhone 照片可以直接处理。能力清单必须与实际能做的事一致 ——
        # 少列一个格式，界面就会把用户拖进来的照片当成"不是图片"。
        accepts=(
            "jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff",
            "heic", "heif", "avif",
        ),
        output_ext=None,  # 跟随设置，保持原扩展名
        description="批量为照片添加文字或 Logo 水印，支持平铺防盗图与每张唯一水印",
        params_schema=PARAMS_SCHEMA,
    )
)
