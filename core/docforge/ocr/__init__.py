"""OCR 双引擎层（对应需求 8）。

对外只暴露一个门面 :func:`recognize`，调用方不需要知道背后是本地离线引擎
还是 DeepSeek Vision 云端引擎，也不需要关心切片、合并、降级与缓存。
"""

from .base import (
    OcrEngine,
    OcrError,
    OcrOptions,
    OcrResult,
    OcrUnavailable,
    TextBlock,
    guess_language_hint,
)
from .router import (
    PREFERENCES,
    all_engines,
    engine_status,
    is_cloud_allowed,
    matches_sensitive_pattern,
    recognize,
    select_engines,
)
from .tiling import (
    MIN_READABLE_LINE_PX,
    MODEL_MAX_SIDE,
    Tile,
    estimate_line_height,
    merge_tile_blocks,
    model_scale,
    plan_tiles,
    sort_reading_order,
)

__all__ = [
    "MIN_READABLE_LINE_PX",
    "MODEL_MAX_SIDE",
    "PREFERENCES",
    "OcrEngine",
    "OcrError",
    "OcrOptions",
    "OcrResult",
    "OcrUnavailable",
    "TextBlock",
    "Tile",
    "all_engines",
    "engine_status",
    "estimate_line_height",
    "guess_language_hint",
    "is_cloud_allowed",
    "matches_sensitive_pattern",
    "merge_tile_blocks",
    "model_scale",
    "plan_tiles",
    "recognize",
    "select_engines",
    "sort_reading_order",
]
