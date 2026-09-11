"""PDF 处理层。

提供页面级几何计算与渲染能力，供各 PDF 动作（水印、工具箱、双层 PDF）复用。
"""

from .geometry import (
    POSITIONS,
    Box,
    expand_template,
    parse_page_range,
    placement_centers,
    rotated_extent,
)

__all__ = [
    "POSITIONS",
    "Box",
    "expand_template",
    "parse_page_range",
    "placement_centers",
    "rotated_extent",
]
