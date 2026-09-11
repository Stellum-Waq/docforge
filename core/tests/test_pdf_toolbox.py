"""PDF 工具箱动作的测试。

这里同时验证了「聚合动作」这条新增的通路：PDF 合并是典型的多文件进、
单文件出，如果走"每个文件一个任务"的老模型，用户会拿到一堆中间产物。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pymupdf
import pytest

from docforge.actions import ActionContext, ActionError, get_action, list_actions
from docforge.jobs import JobRequest
from docforge.jobs import manager as manager_module


def _make_pdf(path: Path, pages: int = 3, label: str = "doc") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 80), f"{label} page {index + 1}", fontsize=18, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


def _run(action_id: str, source: Path, out_dir: Path, params: dict, name: str, files: list[str] | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    ctx = ActionContext(
        job_id="test",
        task_id="t",
        file_path=str(source),
        output_path=str(target),
        params={**params, **({"__files": files} if files else {})},
    )
    result = get_action(action_id).handler(ctx)
    assert result.output_path
    return Path(result.output_path)


# --------------------------------------------------------------------------- #
# 动作注册                                                                      #
# --------------------------------------------------------------------------- #

def test_pdf_actions_are_registered() -> None:
    ids = {spec.id for spec in list_actions()}
    for expected in (
        "pdf.watermark",
        "pdf.merge",
        "pdf.split",
        "pdf.rotate",
        "pdf.compress",
        "pdf.extract_pages",
        "pdf.to_images",
        "pdf.info",
    ):
        assert expected in ids, f"缺少动作 {expected}"


def test_merge_is_marked_aggregate() -> None:
    """合并必须声明为聚合动作，否则会按文件逐个建任务、产出多份无意义结果。"""
    assert get_action("pdf.merge").aggregate is True
    assert get_action("pdf.watermark").aggregate is False


# --------------------------------------------------------------------------- #
# 合并                                                                          #
# --------------------------------------------------------------------------- #

def test_merge_combines_pdfs_in_order(tmp_path: Path) -> None:
    a = _make_pdf(tmp_path / "a.pdf", 2, "Alpha")
    b = _make_pdf(tmp_path / "b.pdf", 3, "Beta")

    out = _run(
        "pdf.merge",
        a,
        tmp_path / "out",
        {"add_bookmarks": True},
        "merged.pdf",
        files=[str(a), str(b)],
    )

    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 5
        # 顺序必须保持：先 Alpha 后 Beta
        assert "Alpha" in doc[0].get_text()
        assert "Alpha" in doc[1].get_text()
        assert "Beta" in doc[2].get_text()
        toc = doc.get_toc()
        assert len(toc) == 2
        assert toc[0][1] == "a"
        assert toc[1][1] == "b"
    finally:
        doc.close()


def test_merge_skips_broken_file_but_keeps_going(tmp_path: Path) -> None:
    """一个坏文件不该让整次合并失败 —— 这在实际办公场景里很常见。"""
    good1 = _make_pdf(tmp_path / "g1.pdf", 2, "One")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    good2 = _make_pdf(tmp_path / "g2.pdf", 1, "Two")

    out = _run(
        "pdf.merge",
        good1,
        tmp_path / "out",
        {},
        "merged.pdf",
        files=[str(good1), str(broken), str(good2)],
    )

    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 3, "两个正常文件的内容都应保留"
    finally:
        doc.close()


def test_merge_requires_at_least_two_files(tmp_path: Path) -> None:
    a = _make_pdf(tmp_path / "a.pdf", 1)
    with pytest.raises(ActionError, match="至少需要 2 个"):
        _run("pdf.merge", a, tmp_path / "out", {}, "merged.pdf", files=[str(a)])


# --------------------------------------------------------------------------- #
# 拆分                                                                          #
# --------------------------------------------------------------------------- #

def test_split_every_n_pages(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", 5)
    out = _run(
        "pdf.split",
        src,
        tmp_path / "out",
        {"split_mode": "every", "pages_per_file": 2},
        "doc.pdf",
    )

    parts = sorted(out.parent.glob("doc_*.pdf"))
    assert len(parts) == 3, f"5 页按每 2 页拆应得 3 个文件，实际 {parts}"
    counts = []
    for part in parts:
        doc = pymupdf.open(str(part))
        counts.append(doc.page_count)
        doc.close()
    assert counts == [2, 2, 1]


def test_split_by_custom_ranges(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", 6)
    out = _run(
        "pdf.split",
        src,
        tmp_path / "out",
        {"split_mode": "ranges", "ranges": "1-2,3-4,5-6"},
        "doc.pdf",
    )

    parts = sorted(out.parent.glob("doc_*.pdf"))
    assert len(parts) == 3
    for part in parts:
        doc = pymupdf.open(str(part))
        assert doc.page_count == 2
        doc.close()


def test_split_with_invalid_ranges_reports_readable_error(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", 3)
    with pytest.raises(ActionError, match="页码范围为空或不合法"):
        _run("pdf.split", src, tmp_path / "out", {"split_mode": "ranges", "ranges": "abc"}, "doc.pdf")


# --------------------------------------------------------------------------- #
# 旋转                                                                          #
# --------------------------------------------------------------------------- #

def test_rotate_selected_pages(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", 3)
    out = _run("pdf.rotate", src, tmp_path / "out", {"angle": "90", "pages": "1,3"}, "rotated.pdf")

    doc = pymupdf.open(str(out))
    try:
        assert doc[0].rotation == 90
        assert doc[1].rotation == 0
        assert doc[2].rotation == 90
    finally:
        doc.close()


def test_rotate_accumulates_on_existing_rotation(tmp_path: Path) -> None:
    """在已有旋转角基础上叠加，不能把用户原本的旋转覆盖掉。"""
    src = tmp_path / "pre-rotated.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.set_rotation(90)
    doc.save(str(src))
    doc.close()

    out = _run("pdf.rotate", src, tmp_path / "out", {"angle": "90", "pages": "all"}, "r.pdf")
    doc = pymupdf.open(str(out))
    try:
        assert doc[0].rotation == 180
    finally:
        doc.close()


def test_rotate_rejects_non_multiple_of_90(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", 1)
    with pytest.raises(ActionError, match="90 的倍数"):
        _run("pdf.rotate", src, tmp_path / "out", {"angle": "45"}, "rotated.pdf")


# --------------------------------------------------------------------------- #
# 提取页                                                                        #
# --------------------------------------------------------------------------- #

def test_extract_pages_produces_subset(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", 5, "Src")
    out = _run("pdf.extract_pages", src, tmp_path / "out", {"pages": "2,4"}, "extracted.pdf")

    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 2
        assert "page 2" in doc[0].get_text()
        assert "page 4" in doc[1].get_text()
    finally:
        doc.close()


def test_extract_pages_out_of_range_reports_error(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", 2)
    with pytest.raises(ActionError, match="没有匹配到任何页面"):
        _run("pdf.extract_pages", src, tmp_path / "out", {"pages": "9-20"}, "x.pdf")


# --------------------------------------------------------------------------- #
# 转图片                                                                        #
# --------------------------------------------------------------------------- #

def test_to_images_png(tmp_path: Path) -> None:
    from PIL import Image

    src = _make_pdf(tmp_path / "doc.pdf", 3)
    out = _run(
        "pdf.to_images",
        src,
        tmp_path / "out",
        {"image_format": "png", "dpi": 72, "pages": "all"},
        "doc.png",
    )

    # result.output_path 指向第一张图片，因此 out.parent 就是图片目录
    images = sorted(out.parent.glob("*.png"))
    assert len(images) == 3, f"应导出 3 张，实际 {[p.name for p in images]}"
    with Image.open(images[0]) as image:
        # A4 在 72 DPI 下约 595×842
        assert image.width == pytest.approx(595, abs=3)
        assert image.height == pytest.approx(842, abs=3)


def test_to_images_jpeg_has_no_alpha_channel(tmp_path: Path) -> None:
    """JPEG 不支持透明通道，必须转成 RGB 再存，否则 PyMuPDF 会报错。"""
    from PIL import Image

    src = _make_pdf(tmp_path / "doc.pdf", 1)
    out = _run(
        "pdf.to_images",
        src,
        tmp_path / "out",
        {"image_format": "jpg", "dpi": 72, "quality": 85},
        "doc.jpg",
    )

    images = sorted(out.parent.glob("*.jpg"))
    assert len(images) == 1
    with Image.open(images[0]) as image:
        assert image.mode == "RGB"


# --------------------------------------------------------------------------- #
# 文档信息                                                                      #
# --------------------------------------------------------------------------- #

def test_info_reports_structure(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", 4, "Report")
    out = _run("pdf.info", src, tmp_path / "out", {}, "doc.json")

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["pageCount"] == 4
    assert payload["totalCharacters"] > 0
    assert len(payload["pages"]) == 4
    assert payload["pages"][0]["likelyScanned"] is False
    assert payload["sizeBytes"] > 0


def test_info_flags_scanned_pages(tmp_path: Path) -> None:
    """纯图片页没有文字层，应被标记为疑似扫描件（提示用户先做 OCR）。"""
    from PIL import Image
    import io

    image = Image.new("RGB", (600, 800), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    src = tmp_path / "scan.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=buffer.getvalue())
    doc.save(str(src))
    doc.close()

    out = _run("pdf.info", src, tmp_path / "out", {}, "scan.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["scannedPageCount"] == 1


# --------------------------------------------------------------------------- #
# 压缩                                                                          #
# --------------------------------------------------------------------------- #

def test_compress_produces_valid_pdf(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "doc.pdf", 3)
    out = _run("pdf.compress", src, tmp_path / "out", {}, "compressed.pdf")

    assert out.is_file()
    doc = pymupdf.open(str(out))
    try:
        assert doc.page_count == 3
        assert "page 1" in doc[0].get_text()
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 队列层：聚合动作                                                              #
# --------------------------------------------------------------------------- #

@pytest.fixture()
async def manager():
    instance = manager_module.JobManager()
    manager_module._manager = instance
    await instance.start()
    try:
        yield instance
    finally:
        await instance.shutdown()
        manager_module._manager = None


async def _wait(job_id: str, timeout: float = 60.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = manager_module.get_manager().get(job_id)
        if job and job.status in {"succeeded", "failed", "cancelled"}:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("任务未在超时内完成")


async def test_merge_through_queue_creates_single_task(manager, tmp_path: Path) -> None:
    """聚合动作在整个队列里只应产生**一个**任务、**一个**产物。"""
    files = [str(_make_pdf(tmp_path / f"part{i}.pdf", 2, f"P{i}")) for i in range(4)]
    out_dir = tmp_path / "merged"

    job = manager.create_job(
        JobRequest(
            action="pdf.merge",
            files=files,
            output_dir=str(out_dir),
            params={},
        )
    )
    assert len(job.tasks) == 1, f"聚合动作应只建 1 个任务，实际 {len(job.tasks)}"

    manager.start_job(job)
    settled = await _wait(job.id)

    assert settled.status == "succeeded"
    assert settled.failed == 0

    produced = list(out_dir.glob("*.pdf"))
    assert len(produced) == 1, f"应只产出 1 个文件，实际 {[p.name for p in produced]}"

    doc = pymupdf.open(str(produced[0]))
    try:
        assert doc.page_count == 8, "4 个文件 × 2 页 = 8 页"
    finally:
        doc.close()


async def test_aggregate_output_name_uses_template(manager, tmp_path: Path) -> None:
    files = [str(_make_pdf(tmp_path / f"p{i}.pdf", 1)) for i in range(3)]
    out_dir = tmp_path / "out"

    job = manager.create_job(
        JobRequest(action="pdf.merge", files=files, output_dir=str(out_dir), params={})
    )
    manager.start_job(job)
    await _wait(job.id)

    names = [p.name for p in out_dir.glob("*.pdf")]
    assert len(names) == 1
    assert "3" in names[0], f"输出名应包含文件数，实际 {names[0]}"
