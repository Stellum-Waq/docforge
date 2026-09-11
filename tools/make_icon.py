"""生成应用图标（.ico / .png）。

图标用代码生成而不是塞一张位图进仓库，好处是**随时可调**：
配色跟着设计令牌走，改一个数值就能重新出一套全尺寸图标。

设计上取"极光渐变圆环 + 枢字"：圆环呼应界面的发光边缘，中间一个字在小尺寸下
也认得出来（16px 的应用栏图标最容易糊，字形比图形更耐缩）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "apps" / "desktop" / "build"

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# 与前端设计令牌保持一致
ABYSS = (5, 7, 13)
CYAN = (34, 211, 238)
VIOLET = (168, 85, 247)
MAGENTA = (240, 171, 252)

FONT_PATH = "C:/Windows/Fonts/msyhbd.ttc"
FALLBACK_FONT = "C:/Windows/Fonts/msyh.ttc"

# Windows 图标需要的尺寸
SIZES = (256, 128, 64, 48, 32, 24, 16)
#: 超采样倍数：先按 4 倍画再缩，边缘才不会有锯齿
SUPERSAMPLE = 4


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _gradient(size: int) -> Image.Image:
    """左上青 → 右下紫的斜向渐变。"""
    gradient = Image.new("RGB", (size, size))
    pixels = gradient.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1)) if size > 1 else 0.0
            color = _lerp(CYAN, VIOLET, t) if t < 0.6 else _lerp(VIOLET, MAGENTA, (t - 0.6) / 0.4)
            pixels[x, y] = color
    return gradient


def _rounded_mask(size: int, radius_ratio: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * radius_ratio), fill=255
    )
    return mask


def _ring_mask(size: int, width_ratio: float, inset_ratio: float) -> Image.Image:
    """圆环遮罩：用于给图标描一圈极光渐变边框。"""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    inset = int(size * inset_ratio)
    border = max(2, int(size * width_ratio))
    draw.ellipse((inset, inset, size - inset, size - inset), fill=255)
    draw.ellipse(
        (inset + border, inset + border, size - inset - border, size - inset - border), fill=0
    )
    return mask


def _base_plate(size: int) -> Image.Image:
    """深空底 + 圆角 + 极光圆环。"""
    plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    background = Image.new("RGBA", (size, size), (*ABYSS, 255))
    plate.paste(background, (0, 0), _rounded_mask(size, 0.22))

    ring = _gradient(size).convert("RGBA")
    plate.paste(ring, (0, 0), _ring_mask(size, 0.035, 0.085))

    return plate


def _draw_glyph(plate: Image.Image, size: int) -> Image.Image:
    """在中间写"枢"字，用白色 + 极光描边保证小尺寸下也清晰。"""
    font_path = FONT_PATH if Path(FONT_PATH).is_file() else FALLBACK_FONT
    if not Path(font_path).is_file():
        return plate

    # 字号按高度比例给，留出圆环的空间
    font_size = int(size * 0.46)
    try:
        font = ImageFont.truetype(font_path, font_size)
    except OSError:
        return plate

    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    text = "枢"
    box = draw.textbbox((0, 0), text, font=font)
    x = (size - (box[2] - box[0])) / 2 - box[0]
    y = (size - (box[3] - box[1])) / 2 - box[1]

    # 先描边再加发光，避免细笔画在深色底上糊掉
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255), stroke_width=max(1, size // 90), stroke_fill=(*CYAN, 220))

    plate.alpha_composite(layer)
    return plate


def render(size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    plate = _base_plate(big)
    plate = _draw_glyph(plate, big)
    return plate.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 主图（electron-builder 也接受 PNG 作为其它平台图标）
    master = render(512)
    png_path = OUT_DIR / "icon.png"
    master.save(png_path)
    print(f"已生成 {png_path}  ({png_path.stat().st_size // 1024} KB)")

    # 多尺寸 ICO：Windows 会在不同场景（任务栏、开始菜单、文件关联）
    # 选取不同尺寸，只给一张 256 会被强行缩放而导致模糊
    frames = [render(size) for size in SIZES]
    ico_path = OUT_DIR / "icon.ico"
    frames[0].save(
        ico_path,
        format="ICO",
        sizes=[(size, size) for size in SIZES],
        append_images=frames[1:],
    )
    print(f"已生成 {ico_path}  ({ico_path.stat().st_size // 1024} KB，含 {len(SIZES)} 种尺寸)")

    # 顺带输出一张给托盘用的小图
    tray_path = OUT_DIR / "tray.png"
    render(64).save(tray_path)
    print(f"已生成 {tray_path}")


if __name__ == "__main__":
    main()
