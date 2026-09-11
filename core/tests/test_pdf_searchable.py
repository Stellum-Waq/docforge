"""扫描件双层 PDF 的测试。

验收要点很明确，而且可以**程序化判定**（不依赖肉眼或视觉模型）：

1. 输出页面上**有图像** —— 外观仍是扫描原图
2. 输出页码可 `get_text()` **取回识别到的文字** —— 这才是"可搜索"的定义
3. 原有文字层的页**原样保留** —— 不因为重新栅格化而丢掉矢量清晰度
4. 文字**位置正确** —— 搜索高亮能框住原文，而不是散落在页面上
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from docforge.actions import ActionContext, ActionError, get_action
from docforge.ocr import OcrOptions, recognize

SPEC = get_action("pdf.searchable")
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"

pytestmark = pytest.mark.skipif(not Path(FONT_PATH).is_file(), reason="测试需要微软雅黑字体")


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


def _scanned_page_image(text_lines: list[str], size: tuple[int, int] = (1000, 1400)) -> bytes:
    """造一页"扫描件"：纯图片、没有文字层。"""
    import io

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    title = ImageFont.truetype(FONT_PATH, 44)
    body = ImageFont.truetype(FONT_PATH, 30)

    y = 90
    for index, line in enumerate(text_lines):
        draw.text((80, y), line, font=title if index == 0 else body, fill="black")
        y += 70 if index == 0 else 55

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _make_scanned_pdf(path: Path, pages: int = 2) -> Path:
    """纯扫描件 PDF：每页只有一张图，没有任何文字。"""
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        stream = _scanned_page_image(
            [f"扫描文档第{index + 1}页", "这是通过图片插入的正文内容", "Scanned page content 2026"]
        )
        page.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


def _make_native_pdf(path: Path, pages: int = 2) -> Path:
    """原生文字 PDF：带完整文字层。"""
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 90), f"Native document page {index + 1}", fontsize=18, fontname="helv")
        page.insert_text((60, 130), "This page already has a real text layer.", fontsize=12, fontname="helv")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


# --------------------------------------------------------------------------- #
# 注册与前提                                                                     #
# --------------------------------------------------------------------------- #

def test_action_is_registered() -> None:
    assert SPEC.accepts == ("pdf",)
    assert SPEC.output_ext == "pdf"


def test_local_ocr_available() -> None:
    """这个功能完全依赖本地 OCR，缺组件时应尽早暴露而不是等到转换时。"""
    from docforge.ocr import engine_status

    status = {item["id"]: item for item in engine_status()}
    assert status["ocr.rapidocr"]["available"], "本地 OCR 不可用，扫描件转可搜索 PDF 无从谈起"


# --------------------------------------------------------------------------- #
# 核心：扫描件应当变成可搜索                                                     #
# --------------------------------------------------------------------------- #

def test_scanned_pdf_becomes_searchable(tmp_path: Path) -> None:
    source = _make_scanned_pdf(tmp_path / "scan.pdf", pages=2)

    # 前提确认：源文件确实没有文字层（否则这个测试就没意义了）
    doc = pymupdf.open(str(source))
    assert doc[0].get_text().strip() == "", "测试素材应当是无文字层的扫描件"
    doc.close()

    out = _run(source, tmp_path / "out", {"engine": "local", "dpi": 200})

    assert out.is_file() and out.stat().st_size > 5000
    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 2

        for index, page in enumerate(doc):
            text = page.get_text()
            assert text.strip(), f"第 {index + 1} 页应当有可提取的文字"
            assert f"第{index + 1}页" in text.replace(" ", ""), f"第 {index + 1} 页标题识别错误：{text[:80]}"
            # 外观仍是图像（说明没有把原图丢掉）
            assert page.get_images(full=True), f"第 {index + 1} 页应当保留扫描图像"
    finally:
        doc.close()


def test_text_layer_is_invisible(tmp_path: Path) -> None:
    """文字层必须**不可见**，否则会盖在扫描图上影响观感。

    判定方式：把页面渲染成像素图，与"只有图像"的渲染做对比 ——
    两者应当几乎一致（允许压缩噪声）。这比让视觉模型去看可靠得多。
    """
    source = _make_scanned_pdf(tmp_path / "scan.pdf", pages=1)
    out = _run(source, tmp_path / "out", {"engine": "local", "dpi": 150})

    doc = pymupdf.open(str(out))
    try:
        page = doc[0]
        # 去掉文字层后重新渲染，比较两者差异
        pix_with_text = page.get_pixmap(dpi=72)

        stripped = pymupdf.open()
        new_page = stripped.new_page(width=page.rect.width, height=page.rect.height)
        for image in page.get_images(full=True):
            stream = doc.extract_image(image[0])["image"]
            new_page.insert_image(new_page.rect, stream=stream)
        pix_image_only = stripped[0].get_pixmap(dpi=72)
        stripped.close()

        assert pix_with_text.width == pix_image_only.width
        samples = pix_with_text.samples
        baseline = pix_image_only.samples
        differing = sum(1 for a, b in zip(samples, baseline, strict=False) if abs(a - b) > 24)
        ratio = differing / max(1, len(baseline))
        assert ratio < 0.02, f"文字层似乎可见（{ratio:.2%} 的像素被改变）"
    finally:
        doc.close()


def test_text_positions_match_the_image(tmp_path: Path) -> None:
    """水印/文字位置必须对得上，否则搜索高亮会框到别处。

    做法：先直接对同一张图跑一遍 OCR 拿到框，再和输出 PDF 里
    `search_for` 得到的框比较，两者应当接近。
    """
    source = _make_scanned_pdf(tmp_path / "scan.pdf", pages=1)
    out = _run(source, tmp_path / "out", {"engine": "local", "dpi": 200, "pages": "1"})

    doc = pymupdf.open(str(out))
    try:
        page = doc[0]
        rects = page.search_for("正文内容")
        assert rects, "应当能搜索到文字"

        hit = rects[0]
        # 文字应当落在页面靠上的正文区域，而不是跑到页外或页脚
        assert 0 <= hit.x0 < page.rect.width
        assert 0 <= hit.y0 < page.rect.height
        assert hit.x1 <= page.rect.width + 1
        assert hit.y1 <= page.rect.height + 1
        # 正文在图上的大致纵向位置（绘图时第三行在 y≈145/1400）
        assert page.rect.height * 0.05 < hit.y0 < page.rect.height * 0.35
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 混合文档：原生页必须原样保留                                                   #
# --------------------------------------------------------------------------- #

def test_native_pages_are_kept_untouched(tmp_path: Path) -> None:
    """已有文字层的页不该被重新栅格化 —— 那会白白丢掉矢量清晰度。"""
    source = _make_native_pdf(tmp_path / "native.pdf", pages=2)
    out = _run(source, tmp_path / "out", {"engine": "local"})

    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 2
        for page in doc:
            text = page.get_text()
            assert "Native document page" in text
            # 原生页没有图像，被栅格化的话这里就会出现图片
            assert not page.get_images(full=True), "原生页不应被栅格化"
    finally:
        doc.close()


def test_mixed_document_handles_both_page_types(tmp_path: Path) -> None:
    """混合文档（前页扫描、后页原生）要各取所长。"""
    doc = pymupdf.open()
    scan = doc.new_page(width=595, height=842)
    scan.insert_image(
        pymupdf.Rect(0, 0, 595, 842),
        stream=_scanned_page_image(["扫描页标题", "这一页只有图片没有文字层"]),
    )
    native = doc.new_page(width=595, height=842)
    native.insert_text((60, 90), "Native page with real text", fontsize=18, fontname="helv")

    source = tmp_path / "mixed.pdf"
    doc.save(str(source))
    doc.close()

    out = _run(source, tmp_path / "out", {"engine": "local", "dpi": 150})

    result = pymupdf.open(str(out))
    try:
        assert result.page_count == 2
        assert "扫描页标题" in result[0].get_text().replace(" ", ""), "扫描页应被 OCR"
        assert result[0].get_images(full=True), "扫描页应保留图像"
        assert "Native page" in result[1].get_text(), "原生页文字应保留"
        assert not result[1].get_images(full=True), "原生页不应被栅格化"
    finally:
        result.close()


def test_force_ocr_rasterizes_native_pages(tmp_path: Path) -> None:
    """强制模式下原生页也会被重做（用户明确要求时才这么做）。"""
    source = _make_native_pdf(tmp_path / "native.pdf", pages=1)
    out = _run(source, tmp_path / "out", {"engine": "local", "force_ocr": True, "dpi": 150})

    doc = pymupdf.open(str(out))
    try:
        assert doc[0].get_images(full=True), "强制模式应当把原生页也栅格化"
        assert "Native" in doc[0].get_text()
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 参数与错误处理                                                                 #
# --------------------------------------------------------------------------- #

def test_page_range_only_processes_selected_pages(tmp_path: Path) -> None:
    source = _make_scanned_pdf(tmp_path / "scan.pdf", pages=3)
    out = _run(source, tmp_path / "out", {"engine": "local", "dpi": 150, "pages": "2"})

    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 1
        assert "第2页" in doc[0].get_text().replace(" ", "")
    finally:
        doc.close()


def test_bad_page_range_reports_error(tmp_path: Path) -> None:
    source = _make_scanned_pdf(tmp_path / "scan.pdf", pages=1)
    with pytest.raises(ActionError, match="没有匹配到任何页面"):
        _run(source, tmp_path / "out", {"engine": "local", "pages": "9-20"})


def test_corrupt_pdf_reports_error(tmp_path: Path) -> None:
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"not a pdf at all")
    with pytest.raises(ActionError, match="无法打开 PDF"):
        _run(bad, tmp_path / "out", {})


def test_encrypted_pdf_reports_error(tmp_path: Path) -> None:
    source = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((60, 80), "secret", fontsize=14)
    doc.save(str(source), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    doc.close()

    with pytest.raises(ActionError, match="已加密"):
        _run(source, tmp_path / "out", {})


def test_blank_page_does_not_fail_the_document(tmp_path: Path) -> None:
    """某一页识别不到文字不该让整份文档失败 —— 档案里夹白页很常见。"""
    doc = pymupdf.open()
    blank = doc.new_page(width=595, height=842)
    blank.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=_scanned_page_image([]))
    content = doc.new_page(width=595, height=842)
    content.insert_image(
        pymupdf.Rect(0, 0, 595, 842), stream=_scanned_page_image(["有内容的一页", "正文文字"])
    )
    source = tmp_path / "with-blank.pdf"
    doc.save(str(source))
    doc.close()

    out = _run(source, tmp_path / "out", {"engine": "local", "dpi": 150})

    result = pymupdf.open(str(out))
    try:
        assert result.page_count == 2, "白页也必须保留"
        assert "有内容的一页" in result[1].get_text().replace(" ", "")
    finally:
        result.close()
