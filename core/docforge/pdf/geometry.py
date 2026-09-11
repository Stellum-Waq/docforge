"""PDF 水印的位置与平铺几何计算。

抽成独立模块的原因：**预览与正式输出必须用同一套布局代码**。
M3 里的图片水印就是这么做的（预览走服务端渲染），这里沿用同样的思路 ——
如果前端自己算一遍坐标，只要有一像素偏差，用户就会看到"预览和结果不一样"。

坐标系约定：PyMuPDF 的页面坐标，原点在左上角，y 轴向下，单位为点（1/72 英寸）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: 九宫格 + 平铺 + 自定义
POSITIONS = (
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
    "custom", "tile",
)


@dataclass(frozen=True)
class Box:
    """一个旋转后的外接矩形。"""

    width: float
    height: float


def rotated_extent(width: float, height: float, angle_deg: float) -> Box:
    """计算矩形旋转后的外接矩形尺寸。

    水印通常要旋转，而定位必须按**旋转后的**占位来算，否则倾斜的水印会
    压到页面边缘之外。
    """
    radians = math.radians(angle_deg)
    cos_a = abs(math.cos(radians))
    sin_a = abs(math.sin(radians))
    return Box(
        width=width * cos_a + height * sin_a,
        height=width * sin_a + height * cos_a,
    )


def placement_centers(
    page_width: float,
    page_height: float,
    *,
    item_width: float,
    item_height: float,
    position: str = "bottom-right",
    margin_ratio: float = 0.04,
    x_ratio: float = 0.5,
    y_ratio: float = 0.5,
    tile_gap_ratio: float = 0.35,
    angle: float = 0.0,
) -> list[tuple[float, float]]:
    """返回水印中心点的列表（页面坐标）。

    统一返回"中心点"而不是"锚点"或"左上角"，因为旋转是绕中心进行的 ——
    用中心点作为唯一约定，调用方就不必再各自推导，避免两处算法不一致。
    """
    ext = rotated_extent(item_width, item_height, angle)
    half_w = ext.width / 2
    half_h = ext.height / 2
    margin = min(page_width, page_height) * max(0.0, margin_ratio)

    if position == "tile":
        return _tile_centers(page_width, page_height, ext, tile_gap_ratio)

    if position == "custom":
        return [(page_width * x_ratio, page_height * y_ratio)]

    if position not in POSITIONS:
        position = "bottom-right"

    horizontal = position.split("-")[1] if "-" in position else "center"
    if position.startswith("top"):
        vertical = "top"
    elif position.startswith("bottom"):
        vertical = "bottom"
    else:
        vertical = "middle"

    if horizontal == "left":
        cx = margin + half_w
    elif horizontal == "right":
        cx = page_width - margin - half_w
    else:
        cx = page_width / 2

    if vertical == "top":
        cy = margin + half_h
    elif vertical == "bottom":
        cy = page_height - margin - half_h
    else:
        cy = page_height / 2

    return [(cx, cy)]


def _tile_centers(
    page_width: float,
    page_height: float,
    extent: Box,
    gap_ratio: float,
) -> list[tuple[float, float]]:
    """平铺排布。

    步长按旋转后外接矩形加间距计算，并把行列错开半格，形成砖砌效果 ——
    整齐的网格反而更容易被抹除（对齐的图案一眼就能看出规律）。
    """
    step_x = max(20.0, extent.width * (1 + max(0.05, gap_ratio)))
    step_y = max(20.0, extent.height * (1 + max(0.05, gap_ratio)))

    cols = max(1, math.ceil(page_width / step_x))
    rows = max(1, math.ceil(page_height / step_y))

    # 居中铺满：让整片水印在页面上均匀分布，而不是从左上角硬排
    offset_x = (page_width - (cols - 1) * step_x) / 2
    offset_y = (page_height - (rows - 1) * step_y) / 2

    centers: list[tuple[float, float]] = []
    for row in range(rows):
        # 偶数行错开半格
        stagger = step_x / 2 if row % 2 else 0.0
        for col in range(cols):
            cx = offset_x + col * step_x + stagger
            cy = offset_y + row * step_y
            # 错开后可能超出右边界，裁掉明显在页外的点，避免无谓的绘制
            if -extent.width / 2 <= cx <= page_width + extent.width / 2:
                centers.append((cx, cy))

    return centers or [(page_width / 2, page_height / 2)]


def expand_template(template: str, values: dict[str, object]) -> str:
    """展开水印文本里的模板变量。

    未知变量保留原样而不是抛错 —— 用户手误写了 ``{foo}`` 不该让整批任务失败。
    """

    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, IndexError):
        # 花括号不配对等语法错误：原样返回，不打断批处理
        return template


def parse_page_range(spec: str, total: int) -> list[int]:
    """解析页码范围字符串，返回 0 起的页索引列表。

    支持 ``all`` / ``1-3`` / ``1,3,5`` / ``2-`` / ``-4`` / 混合写法。
    看不懂的片段会被忽略而不是报错 —— 用户在输入框里边打字边生效，
    中途的半截状态不该直接弹错误。
    """
    if total <= 0:
        return []
    text = (spec or "all").strip().lower()
    if text in ("", "all", "*"):
        return list(range(total))

    selected: set[int] = set()
    for chunk in text.replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue

        if "-" in chunk:
            left, _, right = chunk.partition("-")
            try:
                start = int(left) if left.strip() else 1
                end = int(right) if right.strip() else total
            except ValueError:
                continue
            start = max(1, start)
            end = min(total, end)
            if start <= end:
                selected.update(range(start - 1, end))
        else:
            try:
                index = int(chunk)
            except ValueError:
                continue
            if 1 <= index <= total:
                selected.add(index - 1)

    return sorted(selected)
