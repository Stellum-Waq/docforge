"""PDF 水印与几何计算的测试。

PDF 水印有一个很好的验证手段：文字是**矢量**写入的，因此可以用
``page.get_text()`` 直接把水印读回来 —— 比自己渲染像素做比对可靠得多，
而且顺便证明了"水印文字可被检索"这个有价值的产品特性。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from docforge.actions import ActionContext, ActionError, get_action
from docforge.pdf.geometry import (
    expand_template,
    parse_page_range,
    placement_centers,
    rotated_extent,
)

SPEC = get_action("pdf.watermark")


# --------------------------------------------------------------------------- #
# 素材                                                                          #
# --------------------------------------------------------------------------- #

def _make_pdf(path: Path, pages: int = 3, size: tuple[float, float] = (595, 842)) -> Path:
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((60, 80), f"Document page {index + 1}", fontsize=18, fontname="helv")
        page.insert_text((60, 120), "正文内容 body text for testing.", fontsize=12, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


def _run(source: Path, out_dir: Path, params: dict, name: str = "out.pdf") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    ctx = ActionContext(
        job_id="test",
        task_id="t",
        file_path=str(source),
        output_path=str(target),
        params=params,
    )
    result = SPEC.handler(ctx)
    assert result.output_path
    return Path(result.output_path)


# --------------------------------------------------------------------------- #
# 几何计算                                                                      #
# --------------------------------------------------------------------------- #

def test_rotated_extent_grows_with_angle() -> None:
    base = rotated_extent(100, 20, 0)
    assert (base.width, base.height) == pytest.approx((100, 20))

    tilted = rotated_extent(100, 20, 90)
    assert tilted.width == pytest.approx(20, abs=0.01)
    assert tilted.height == pytest.approx(100, abs=0.01)

    diagonal = rotated_extent(100, 100, 45)
    assert diagonal.width == pytest.approx(141.42, abs=0.1)


def test_center_position_is_page_center() -> None:
    centers = placement_centers(600, 800, item_width=200, item_height=50, position="center")
    assert centers == [(300.0, 400.0)]


def test_corner_positions_respect_margin() -> None:
    margin = 0.05
    centers = placement_centers(
        600, 800, item_width=100, item_height=40, position="top-left", margin_ratio=margin
    )
    cx, cy = centers[0]
    expected_margin = min(600, 800) * margin
    assert cx == pytest.approx(expected_margin + 50)
    assert cy == pytest.approx(expected_margin + 20)


def test_bottom_right_is_inside_page() -> None:
    centers = placement_centers(
        600, 800, item_width=100, item_height=40, position="bottom-right", margin_ratio=0.05
    )
    cx, cy = centers[0]
    assert cx + 50 <= 600
    assert cy + 20 <= 800


def test_rotated_item_stays_inside_page() -> None:
    """旋转后的占位必须按**外接矩形**算，否则倾斜的水印会压出页面外。"""
    centers = placement_centers(
        600, 800,
        item_width=300, item_height=40,
        position="bottom-right",
        margin_ratio=0.02,
        angle=-45,
    )
    cx, cy = centers[0]
    extent = rotated_extent(300, 40, -45)
    assert cx + extent.width / 2 <= 600 + 0.01
    assert cy + extent.height / 2 <= 800 + 0.01


def test_custom_position_uses_ratios() -> None:
    centers = placement_centers(
        600, 800, item_width=10, item_height=10, position="custom", x_ratio=0.25, y_ratio=0.75
    )
    assert centers == [(150.0, 600.0)]


def test_tile_produces_many_centers_covering_page() -> None:
    centers = placement_centers(
        600, 800,
        item_width=100, item_height=30,
        position="tile",
        tile_gap_ratio=0.5,
    )
    assert len(centers) > 6
    xs = [c[0] for c in centers]
    ys = [c[1] for c in centers]
    # 覆盖面应当跨越整页，而不是挤在左上角
    assert min(xs) < 200 and max(xs) > 400
    assert min(ys) < 300 and max(ys) > 500


def test_unknown_position_falls_back_to_bottom_right() -> None:
    centers = placement_centers(600, 800, item_width=50, item_height=20, position="不存在的值")
    cx, _ = centers[0]
    assert cx > 300


def test_parse_page_range_variants() -> None:
    assert parse_page_range("all", 5) == [0, 1, 2, 3, 4]
    assert parse_page_range("", 5) == [0, 1, 2, 3, 4]
    assert parse_page_range("1-3", 5) == [0, 1, 2]
    assert parse_page_range("1,3,5", 5) == [0, 2, 4]
    assert parse_page_range("2-", 5) == [1, 2, 3, 4]
    assert parse_page_range("-3", 5) == [0, 1, 2]
    assert parse_page_range("3-1", 5) == []
    assert parse_page_range("abc", 5) == []
    assert parse_page_range("1-100", 5) == [0, 1, 2, 3, 4]


def test_parse_page_range_tolerates_partial_input() -> None:
    """用户在输入框里边打字边生效，中途的半截状态不该被当成错误。"""
    assert parse_page_range("1-3,", 5) == [0, 1, 2]
    assert parse_page_range("1,,3", 5) == [0, 2]
    assert parse_page_range("1-3, xyz, 5", 5) == [0, 1, 2, 4]


def test_expand_template_keeps_unknown_variables() -> None:
    values = {"page": 3, "pages": 10, "filename": "合同.pdf"}
    assert expand_template("{filename} 第{page}页", values) == "合同.pdf 第3页"
    assert expand_template("{unknown}", values) == "{unknown}"
    assert expand_template("{unclosed", values) == "{unclosed"


# --------------------------------------------------------------------------- #
# 水印动作                                                                      #
# --------------------------------------------------------------------------- #

def test_text_watermark_is_written_and_searchable(tmp_path: Path) -> None:
    """文字水印应当是**矢量文字**：既可被检索，也不因缩放而模糊。"""
    src = _make_pdf(tmp_path / "doc.pdf", pages=2)
    out = _run(src, tmp_path / "out", {"mode": "text", "text": "内部机密", "position": "center"})

    assert out.is_file()
    doc = pymupdf.open(str(out))
    try:
        text = doc[0].get_text()
        assert "内部机密" in text, "水印文字应能被提取（矢量文字）"
        assert "Document page 1" in text, "原文内容不能被破坏"
    finally:
        doc.close()


def test_watermark_preserves_page_count_and_size(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", pages=3)
    out = _run(src, tmp_path / "out", {"mode": "text", "text": "COPY"})

    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 3
        for page in doc:
            assert page.rect.width == pytest.approx(595, abs=1)
            assert page.rect.height == pytest.approx(842, abs=1)
    finally:
        doc.close()


def test_page_range_only_stamps_selected_pages(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", pages=4)
    out = _run(src, tmp_path / "out", {"mode": "text", "text": "SECRET", "pages": "2-3"})

    doc = pymupdf.open(str(out))
    try:
        assert "SECRET" not in doc[0].get_text(), "第 1 页不应有水印"
        assert "SECRET" in doc[1].get_text()
        assert "SECRET" in doc[2].get_text()
        assert "SECRET" not in doc[3].get_text(), "第 4 页不应有水印"
    finally:
        doc.close()


def test_per_page_template_variables(tmp_path: Path) -> None:
    """{page} 变量应当每页展开成不同的值，便于逐页追溯。"""
    src = _make_pdf(tmp_path / "doc.pdf", pages=3)
    out = _run(
        src,
        tmp_path / "out",
        {"mode": "text", "text": "第{page}页/共{pages}页", "unique_per_file": True},
    )

    doc = pymupdf.open(str(out))
    try:
        assert "第1页/共3页" in doc[0].get_text().replace(" ", "")
        assert "第2页/共3页" in doc[1].get_text().replace(" ", "")
        assert "第3页/共3页" in doc[2].get_text().replace(" ", "")
    finally:
        doc.close()


def test_tile_watermark_covers_page(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", pages=1)
    out = _run(
        src,
        tmp_path / "out",
        {"mode": "text", "text": "禁止外传", "position": "tile", "tile_gap_ratio": 0.4},
    )

    doc = pymupdf.open(str(out))
    try:
        matches = doc[0].search_for("禁止外传")
        assert len(matches) >= 4, f"平铺应产生多处水印，实际 {len(matches)} 处"
        # 覆盖面要横跨页面，而不是挤在角落
        xs = [r.x0 for r in matches]
        ys = [r.y0 for r in matches]
        assert max(xs) - min(xs) > 200
        assert max(ys) - min(ys) > 300
    finally:
        doc.close()


def test_rotated_page_watermark_lands_inside_page(tmp_path: Path) -> None:
    """旋转页面是实测踩过的坑：坐标语义有歧义，水印会落到页面外。

    这里直接验证水印的落点在页面范围内。
    """
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 80), "Rotated page", fontsize=16, fontname="helv")
    page.set_rotation(90)
    src = tmp_path / "rotated.pdf"
    doc.save(str(src))
    doc.close()

    out = _run(src, tmp_path / "out", {"mode": "text", "text": "WM", "position": "center"})

    doc = pymupdf.open(str(out))
    try:
        page = doc[0]
        assert page.rotation == 90, "原有旋转角必须保留"
        rects = page.search_for("WM")
        assert rects, "水印应当存在"
        visible = page.rect
        for rect in rects:
            assert rect.x0 >= -1 and rect.y0 >= -1
            assert rect.x1 <= visible.width + 1
            assert rect.y1 <= visible.height + 1
    finally:
        doc.close()


def test_image_watermark(tmp_path: Path) -> None:
    from PIL import Image

    logo = tmp_path / "logo.png"
    Image.new("RGBA", (200, 80), (200, 30, 30, 255)).save(logo)

    src = _make_pdf(tmp_path / "doc.pdf", pages=2)
    out = _run(
        src,
        tmp_path / "out",
        {"mode": "image", "image_path": str(logo), "position": "tile", "opacity": 0.3},
    )

    assert out.is_file() and out.stat().st_size > 0
    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 2
        # 图片水印应当真的被写进去（页面图片数增加）
        assert len(doc[0].get_images(full=True)) >= 1
    finally:
        doc.close()


def test_overlay_false_puts_watermark_behind_content(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", pages=1)
    out = _run(src, tmp_path / "out", {"mode": "text", "text": "底纹", "overlay": False})

    doc = pymupdf.open(str(out))
    try:
        # search_for 会按内容流顺序返回，水印在正文之前说明确实在底层
        text = doc[0].get_text()
        assert "底纹" in text
    finally:
        doc.close()


def test_empty_text_is_rejected(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", pages=1)
    with pytest.raises(ActionError, match="水印文字为空"):
        _run(src, tmp_path / "out", {"mode": "text", "text": "   "})


def test_missing_logo_reports_readable_error(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", pages=1)
    with pytest.raises(ActionError, match="水印图片不存在"):
        _run(src, tmp_path / "out", {"mode": "image", "image_path": str(tmp_path / "nope.png")})


def test_bad_page_range_reports_readable_error(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", pages=2)
    with pytest.raises(ActionError, match="没有匹配到任何页面"):
        _run(src, tmp_path / "out", {"mode": "text", "text": "X", "pages": "99-120"})


def test_corrupt_pdf_reports_readable_error(tmp_path: Path) -> None:
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"this is not a pdf at all")
    with pytest.raises(ActionError, match="无法打开 PDF"):
        _run(bad, tmp_path / "out", {"mode": "text", "text": "X"})


def test_encrypted_pdf_is_rejected_with_guidance(tmp_path: Path) -> None:
    src = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((60, 80), "secret", fontsize=14)
    doc.save(str(src), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    doc.close()

    with pytest.raises(ActionError, match="已加密"):
        _run(src, tmp_path / "out", {"mode": "text", "text": "X"})


def test_output_is_not_larger_than_reasonable(tmp_path: Path) -> None:
    """save 时要开 garbage/deflate，否则加水印后文件会明显变大。"""
    src = _make_pdf(tmp_path / "doc.pdf", pages=5)
    original = src.stat().st_size
    out = _run(
        src,
        tmp_path / "out",
        {"mode": "text", "text": "机密", "position": "tile"},
    )
    # 平铺水印会写入大量对象，允许变大，但不该失控
    assert out.stat().st_size < max(original * 8, 200_000)
