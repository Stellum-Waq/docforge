"""切片规划与结果合并的测试。

这一层是纯计算逻辑，也是"云端 OCR 能不能真正可用"的关键，因此测得比较细。
"""

from __future__ import annotations

import pytest
from PIL import Image

from docforge.ocr.base import TextBlock
from docforge.ocr.tiling import (
    MAX_TILES,
    MIN_READABLE_LINE_PX,
    MODEL_MAX_SIDE,
    Tile,
    estimate_line_height,
    max_tile_side,
    merge_tile_blocks,
    model_scale,
    plan_tiles,
    plan_tiles_for_image,
    readable_line_height,
    sort_reading_order,
)


# --------------------------------------------------------------------------- #
# 模型缩放模型                                                                  #
# --------------------------------------------------------------------------- #

def test_small_image_is_not_scaled() -> None:
    assert model_scale(800, 600) == 1.0
    assert model_scale(MODEL_MAX_SIDE, MODEL_MAX_SIDE) == 1.0


def test_large_image_is_scaled_down() -> None:
    assert model_scale(2600, 2600) == pytest.approx(0.5, abs=0.01)
    # 长边与面积两个约束都生效，取更严格的那个
    assert model_scale(4000, 3000) < 0.5


def test_readable_line_height_matches_intuition() -> None:
    """300 DPI A4 扫描件（行高约 42px）缩放后依然清晰可读。"""
    height = readable_line_height(42, 2480, 3508)
    assert height >= MIN_READABLE_LINE_PX


# --------------------------------------------------------------------------- #
# 切片规划                                                                      #
# --------------------------------------------------------------------------- #

def test_big_text_needs_no_tiling() -> None:
    """字够大就整图识别 —— 不盲目按尺寸切图，既省钱又避免切断文本行。"""
    tiles = plan_tiles(2480, 3508, line_height=42)
    assert len(tiles) == 1
    assert tiles[0].box == (0, 0, 2480, 3508)


def test_small_text_forces_tiling() -> None:
    """密集小字的大图必须切片，否则缩放后字高低于可读阈值。"""
    tiles = plan_tiles(4000, 3000, line_height=12)
    assert len(tiles) > 1

    # 每块切片经模型缩放后，行高仍应达到可读阈值
    for tile in tiles:
        scaled = readable_line_height(12, tile.width, tile.height)
        assert scaled >= MIN_READABLE_LINE_PX - 0.01


def test_max_tile_side_derivation() -> None:
    # L ≤ MAX_SIDE · h / MIN_LINE_PX
    assert max_tile_side(12) == MODEL_MAX_SIDE
    assert max_tile_side(24) == MODEL_MAX_SIDE * 2
    assert max_tile_side(6) == MODEL_MAX_SIDE // 2


def test_cores_partition_the_image_exactly() -> None:
    """核心区必须**无缝且不重叠**地铺满整图 —— 否则文字块会归属错乱或丢失。"""
    tiles = plan_tiles(4000, 3000, line_height=10)
    assert len(tiles) > 1

    covered = 0
    for tile in tiles:
        x0, y0, x1, y1 = tile.core
        covered += (x1 - x0) * (y1 - y0)

    assert covered == 4000 * 3000


def test_tiles_stay_inside_image_and_overlap() -> None:
    tiles = plan_tiles(4000, 3000, line_height=10)

    for tile in tiles:
        x0, y0, x1, y1 = tile.box
        assert 0 <= x0 < x1 <= 4000
        assert 0 <= y0 < y1 <= 3000
        # 裁剪区域必须完整包含核心区
        cx0, cy0, cx1, cy1 = tile.core
        assert x0 <= cx0 and y0 <= cy0 and x1 >= cx1 and y1 >= cy1

    # 至少有一块切片比核心区大，说明重叠带真的存在
    assert any(tile.box != tile.core for tile in tiles)


def test_mode_off_forces_single_tile() -> None:
    tiles = plan_tiles(6000, 6000, line_height=5, mode="off")
    assert len(tiles) == 1


def test_mode_always_forces_tiling() -> None:
    tiles = plan_tiles(4000, 3000, line_height=40, mode="always")
    assert len(tiles) > 1


def test_tile_count_is_capped() -> None:
    """极小字号（例如整页缩略图）不应该把图切成几百块。"""
    tiles = plan_tiles(8000, 8000, line_height=2)
    assert len(tiles) <= MAX_TILES


def test_unknown_line_height_uses_conservative_estimate() -> None:
    tiles = plan_tiles(4000, 3000, line_height=0)
    assert len(tiles) >= 1
    for tile in tiles:
        assert tile.width > 0 and tile.height > 0


def test_invalid_size_raises() -> None:
    from docforge.ocr.base import OcrError

    with pytest.raises(OcrError):
        plan_tiles(0, 100)


def test_plan_tiles_for_image_reads_real_size(tmp_path) -> None:
    path = tmp_path / "big.png"
    Image.new("RGB", (3000, 2000), "white").save(path)

    tiles = plan_tiles_for_image(path, line_height=10)
    assert len(tiles) > 1
    assert sum((t.core[2] - t.core[0]) * (t.core[3] - t.core[1]) for t in tiles) == 3000 * 2000


# --------------------------------------------------------------------------- #
# 行高估计                                                                      #
# --------------------------------------------------------------------------- #

def test_estimate_line_height_uses_median() -> None:
    """用中位数而不是平均值：标题、印章等异常高的框不该影响判断。

    行高集合为 [20, 20, 20, 200]：中位数是 20，平均数是 65。
    采用中位数能完全忽略那个 200 的异常框。
    """
    blocks = [
        TextBlock("正文一", (0, 0, 100, 20)),
        TextBlock("正文二", (0, 30, 100, 50)),
        TextBlock("正文三", (0, 60, 100, 80)),
        TextBlock("巨大的标题", (0, 100, 900, 300)),  # 行高 200，会把平均值带偏到 65
    ]
    assert estimate_line_height(blocks) == 20


def test_estimate_line_height_empty() -> None:
    assert estimate_line_height([]) == 0.0


# --------------------------------------------------------------------------- #
# 跨切片合并                                                                    #
# --------------------------------------------------------------------------- #

def test_merge_removes_duplicates_from_overlap() -> None:
    """重叠带里的同一行字被两块切片都识别到时，只能保留一份。"""
    left = Tile(0, 0, 120, 100, (0, 0, 100, 100))
    right = Tile(100, 0, 120, 100, (100, 0, 220, 100))

    # "你好" 位于 x=90..110，横跨两块切片的交界，两边都会识别到
    blocks = [
        (left, [TextBlock("你好", (90, 40, 110, 60), 0.9)]),
        (right, [TextBlock("你好", (0, 40, 20, 60), 0.95)]),  # 切片内坐标 → 全局 (100,40,120,60)
    ]

    merged = merge_tile_blocks(blocks)
    assert len(merged) == 1
    assert merged[0].text == "你好"


def test_merge_keeps_distinct_blocks() -> None:
    left = Tile(0, 0, 120, 100, (0, 0, 100, 100))
    right = Tile(100, 0, 120, 100, (100, 0, 220, 100))

    blocks = [
        (left, [TextBlock("左边", (5, 10, 60, 30))]),
        (right, [TextBlock("右边", (10, 50, 70, 70))]),
    ]

    merged = merge_tile_blocks(blocks)
    texts = {b.text for b in merged}
    assert texts == {"左边", "右边"}


def test_merge_converts_to_global_coordinates() -> None:
    tile = Tile(500, 300, 200, 200, (500, 300, 700, 500))
    merged = merge_tile_blocks([(tile, [TextBlock("块", (10, 20, 60, 50))])])
    assert merged[0].box == (510, 320, 560, 350)


def test_merge_single_tile_passthrough() -> None:
    tile = Tile(0, 0, 100, 100, (0, 0, 100, 100))
    blocks = [
        TextBlock("甲", (0, 0, 20, 20), 0.8),
        TextBlock("乙", (0, 30, 20, 50), 0.9),
    ]
    merged = merge_tile_blocks([(tile, blocks)])
    assert len(merged) == 2


def test_merge_dedups_identical_text_at_same_place_in_one_tile() -> None:
    tile = Tile(0, 0, 100, 100, (0, 0, 100, 100))
    blocks = [
        TextBlock("重复", (10, 10, 60, 30), 0.7),
        TextBlock("重复", (11, 11, 61, 31), 0.9),
    ]
    merged = merge_tile_blocks([(tile, blocks)])
    assert len(merged) == 1
    assert merged[0].confidence == 0.9  # 保留置信度更高的那个


# --------------------------------------------------------------------------- #
# 阅读顺序                                                                      #
# --------------------------------------------------------------------------- #

def test_reading_order_groups_rows() -> None:
    """同一行内 y 有微小差异时，顺序仍应按 x 排列而不是被 y 打乱。"""
    blocks = [
        TextBlock("右", (200, 12, 260, 32)),
        TextBlock("左", (10, 10, 60, 30)),
        TextBlock("中", (100, 14, 150, 34)),
        TextBlock("第二行", (10, 60, 120, 80)),
    ]
    ordered = sort_reading_order(blocks)
    assert [b.text for b in ordered] == ["左", "中", "右", "第二行"]


def test_reading_order_empty() -> None:
    assert sort_reading_order([]) == []


def test_reading_order_handles_multiline_document() -> None:
    blocks = [
        TextBlock("标题", (100, 0, 300, 40)),
        TextBlock("第一行左", (10, 60, 90, 80)),
        TextBlock("第一行右", (110, 60, 190, 80)),
        TextBlock("第二行", (10, 110, 190, 130)),
    ]
    ordered = sort_reading_order(blocks)
    assert [b.text for b in ordered] == ["标题", "第一行左", "第一行右", "第二行"]
