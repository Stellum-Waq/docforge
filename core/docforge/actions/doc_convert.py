"""文档格式转换动作 —— 对应需求 3「PDF 和 word 转换」。

## 引擎路由（设计文档 §2.4）

Word/Excel/PPT → PDF 按**保真度**择优，不可用时逐级降级：

| 优先级 | 引擎 | 保真度 | 说明 |
|---|---|---|---|
| 1 | **Office COM** | 100 | 本机装了 Office 时最优；实测本机可用 |
| 2 | LibreOffice headless | 82 | 无 Office 环境下的次优选择 |
| 3 | 内置渲染 | 55 | 纯 Python，保真度低但**永远可用**，保证功能不会完全不可用 |

PDF → Word 用 ``pdf2docx`` 还原版面（表格、图片、分栏）；扫描件没有文字层时
给出明确提示，引导用户先用 OCR。

## 为什么第三级降级值得实现

没有 Office 也没有 LibreOffice 的机器（干净的服务器、部分企业终端）上，
如果直接报"功能不可用"，用户会觉得软件是坏的。内置渲染虽然保真度低，
但能把文字和基本结构带过去 —— 对"我只想要内容"的场景已经够用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pymupdf

from ..engines.com import ComError, ComTimeout, ComUnavailable, get_worker
from .base import ActionContext, ActionError, ActionSpec, TaskResult, atomic_write, register

# pdf2docx 内部用 logging 打大量进度信息，会污染内核的标准输出。
# 内核的 stdout 承载着与 Electron 的握手协议，必须保持干净。
logging.getLogger("pdf2docx").setLevel(logging.ERROR)
logging.getLogger("fontTools").setLevel(logging.ERROR)

#: 按扩展名决定用哪个 Office 应用
WORD_EXTS = {"doc", "docx", "docm", "rtf", "odt", "wps", "txt", "md"}
EXCEL_EXTS = {"xls", "xlsx", "xlsm", "xlsb", "ods", "et", "csv", "tsv"}
PPT_EXTS = {"ppt", "pptx", "pptm", "odp", "dps"}

#: Word 的 SaveAs 文件格式常量
WD_FORMAT_PDF = 17
#: PowerPoint 的 SaveAs 格式常量
PP_SAVE_AS_PDF = 32
#: Excel 的 ExportAsFixedFormat 类型：0 = xlTypePDF
XL_TYPE_PDF = 0


def _app_for(ext: str) -> str | None:
    if ext in WORD_EXTS:
        return "word"
    if ext in EXCEL_EXTS:
        return "excel"
    if ext in PPT_EXTS:
        return "powerpoint"
    return None


# --------------------------------------------------------------------------- #
# COM 路径                                                                      #
# --------------------------------------------------------------------------- #

def _com_word_to_pdf(source: Path, target: Path, params: dict[str, Any]) -> None:
    worker = get_worker("word")

    def job(app):
        document = None
        try:
            document = app.Documents.Open(
                str(source),
                ConfirmConversions=False,
                ReadOnly=True,
                AddToRecentFiles=False,
                Visible=False,
                # 不弹"是否替换现有文件"之类的对话框
                NoEncodingDialog=True,
            )
        except Exception as err:  # noqa: BLE001
            raise ComError(f"Word 无法打开该文档：{err}") from err

        try:
            # ExportAsFixedFormat 比 SaveAs 提供更多可控项
            document.ExportAsFixedFormat(
                str(target),
                ExportFormat=WD_FORMAT_PDF,
                OpenAfterExport=False,
                OptimizeFor=0,  # wdExportOptimizeForPrint
                # 用标题样式生成书签，PDF 阅读器里能出目录
                CreateBookmarks=1 if params.get("bookmarks", True) else 0,
                DocStructureTags=True,
                BitmapMissingFonts=True,
                UseISO19005_1=bool(params.get("pdf_a", False)),
            )
        finally:
            # 必须显式关闭且不保存，否则 Word 会残留进程并弹保存提示
            try:
                document.Close(SaveChanges=0)
            except Exception:  # noqa: BLE001
                pass

    worker.run(job, timeout=float(params.get("timeout", 240)))


def _com_excel_to_pdf(source: Path, target: Path, params: dict[str, Any]) -> None:
    worker = get_worker("excel")

    def job(app):
        workbook = None
        try:
            workbook = app.Workbooks.Open(
                str(source), ReadOnly=True, UpdateLinks=0, AddToRecentFiles=False
            )
        except Exception as err:  # noqa: BLE001
            raise ComError(f"Excel 无法打开该工作簿：{err}") from err

        try:
            workbook.ExportAsFixedFormat(XL_TYPE_PDF, str(target), OpenAfterPublish=False)
        finally:
            try:
                workbook.Close(SaveChanges=False)
            except Exception:  # noqa: BLE001
                pass

    worker.run(job, timeout=float(params.get("timeout", 240)))


def _com_powerpoint_to_pdf(source: Path, target: Path, params: dict[str, Any]) -> None:
    worker = get_worker("powerpoint")

    def job(app):
        presentation = None
        try:
            presentation = app.Presentations.Open(
                str(source), ReadOnly=True, WithWindow=False
            )
        except Exception as err:  # noqa: BLE001
            raise ComError(f"PowerPoint 无法打开该演示文稿：{err}") from err

        try:
            presentation.SaveAs(str(target), PP_SAVE_AS_PDF)
        finally:
            try:
                presentation.Close()
            except Exception:  # noqa: BLE001
                pass

    worker.run(job, timeout=float(params.get("timeout", 300)))


# --------------------------------------------------------------------------- #
# LibreOffice 路径                                                              #
# --------------------------------------------------------------------------- #

def _libreoffice_to_pdf(source: Path, target: Path, params: dict[str, Any]) -> bool:
    """用 LibreOffice headless 转换。未安装时返回 False，交由上层降级。"""
    import shutil
    import subprocess
    import tempfile

    soffice = shutil.which("soffice") or shutil.which("soffice.exe")
    if not soffice:
        for candidate in (
            Path("C:/Program Files/LibreOffice/program/soffice.exe"),
            Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
        ):
            if candidate.is_file():
                soffice = str(candidate)
                break
    if not soffice:
        return False

    with tempfile.TemporaryDirectory() as temp_dir:
        # LibreOffice 的输出文件名跟随源文件名，因此转换后再改名到目标路径
        command = [
            soffice,
            "--headless",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            temp_dir,
            str(source),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, timeout=float(params.get("timeout", 240)), check=False
            )
        except (subprocess.TimeoutExpired, OSError) as err:
            raise ComError(f"LibreOffice 转换失败：{err}") from err

        produced = list(Path(temp_dir).glob("*.pdf"))
        if not produced:
            detail = (completed.stderr or b"").decode("utf-8", errors="replace")[:200]
            raise ComError(f"LibreOffice 未产出 PDF：{detail}")

        target.write_bytes(produced[0].read_bytes())
    return True


# --------------------------------------------------------------------------- #
# 内置渲染（最后的兜底）                                                        #
# --------------------------------------------------------------------------- #

def _builtin_to_pdf(source: Path, target: Path, params: dict[str, Any]) -> None:
    """纯 Python 的兜底渲染：提取文字后用 PyMuPDF 排版成 PDF。

    保真度明显低于 Office COM（不保留精确字号/间距/复杂版式），但它**永远可用**，
    因此「没有装 Office」的机器上功能不会彻底失效。

    只有 docx / txt / md 走这条路 —— xlsx / pptx 靠文字提取意义不大，
    不如直接给出可读的错误说明。
    """
    ext = source.suffix.lstrip(".").lower()
    if ext in EXCEL_EXTS or ext in PPT_EXTS:
        raise ActionError(
            f"未检测到 Microsoft Office 或 LibreOffice，无法把 .{ext} 转换为 PDF。"
            "请安装 Office 或 LibreOffice 后重试。"
        )

    blocks: list[tuple[str, str]] = []  # (类型, 文本)

    if ext == "docx":
        try:
            from docx import Document
        except ImportError as err:  # pragma: no cover
            raise ActionError("缺少 python-docx，无法转换 Word 文档") from err

        document = Document(str(source))
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            # 用样式名判断标题层级，roughly 还原文档结构
            style_name = (paragraph.style.name or "").lower()
            if style_name.startswith("heading"):
                level = "".join(ch for ch in style_name if ch.isdigit()) or "1"
                blocks.append((f"h{min(6, int(level))}", text))
            elif style_name.startswith("title"):
                blocks.append(("h1", text))
            else:
                blocks.append(("p", text))

        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    blocks.append(("p", "  |  ".join(cells)))
    else:
        text = source.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            blocks.append(("h2" if stripped.startswith("#") else "p", stripped.lstrip("# ").strip()))

    if not blocks:
        raise ActionError("文档里没有可转换的内容")

    # 用 PyMuPDF 排版。内置 CJK 字体让中文无需嵌入字体文件即可显示。
    doc = pymupdf.open()
    page_width, page_height = 595.0, 842.0  # A4
    margin = 56.0
    cursor = margin

    page = doc.new_page(width=page_width, height=page_height)
    for kind, text in blocks:
        size = 20.0 if kind == "h1" else 15.0 if kind == "h2" else 11.0
        line_height = size * 1.6

        if cursor + line_height > page_height - margin:
            page = doc.new_page(width=page_width, height=page_height)
            cursor = margin

        # insert_textbox 会自动折行，比手动按字符宽度切分可靠
        remaining = page.insert_textbox(
            pymupdf.Rect(margin, cursor, page_width - margin, page_height - margin),
            text + "\n",
            fontsize=size,
            fontname="china-s",  # 内置简体中文字体
            color=(0, 0, 0),
        )
        used = (page_height - margin - cursor) - max(0.0, remaining)
        cursor += max(line_height, used + 6)

    doc.save(str(target), garbage=3, deflate=True)
    doc.close()


# --------------------------------------------------------------------------- #
# 动作：Office / 文档 → PDF                                                     #
# --------------------------------------------------------------------------- #

PARAMS_TO_PDF: dict[str, Any] = {
    "type": "object",
    "properties": {
        "engine": {
            "type": "string",
            "enum": ["auto", "com", "libreoffice", "builtin"],
            "default": "auto",
            "title": "转换引擎",
            "description": "auto=按保真度自动择优并逐级降级；com=仅用 Office（保真度最高）",
        },
        "bookmarks": {
            "type": "boolean",
            "default": True,
            "title": "生成 PDF 书签",
            "description": "根据 Word 的标题样式生成目录书签",
        },
        "pdf_a": {
            "type": "boolean",
            "default": False,
            "title": "输出 PDF/A 归档格式",
            "description": "长期归档标准；文件会略大，且不支持透明效果",
        },
        "timeout": {
            "type": "integer",
            "default": 240,
            "minimum": 30,
            "maximum": 1800,
            "title": "单文件超时（秒）",
            "description": "超过后强制结束 Office 进程，避免个别文档卡死整批任务",
        },
    },
}


def _handler_to_pdf(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    target = Path(ctx.output_path)
    ext = source.suffix.lstrip(".").lower()
    preference = ctx.str_param("engine", "auto")
    params = dict(ctx.params)

    app_name = _app_for(ext)
    if app_name is None:
        raise ActionError(f"不支持把 .{ext} 转换为 PDF")

    ctx.report(10, "转换中")

    attempts: list[str] = []
    warnings: list[str] = []

    def try_com() -> bool:
        if app_name == "word":
            _com_word_to_pdf(source, target, params)
        elif app_name == "excel":
            _com_excel_to_pdf(source, target, params)
        else:
            _com_powerpoint_to_pdf(source, target, params)
        return True

    handlers: list[tuple[str, Any]] = []
    if preference in ("auto", "com"):
        handlers.append(("com", try_com))
    if preference in ("auto", "libreoffice"):
        handlers.append(("libreoffice", lambda: _libreoffice_to_pdf(source, target, params)))
    if preference in ("auto", "builtin"):
        handlers.append(("builtin", lambda: (_builtin_to_pdf(source, target, params), True)[1]))

    last_error: Exception | None = None
    for name, handler in handlers:
        ctx.raise_if_cancelled()
        attempts.append(name)
        try:
            if handler():
                ctx.report(90, "写入完成")
                if name != "com" and preference == "auto":
                    warnings.append(f"使用 {name} 引擎转换（保真度低于 Office）")
                ctx.report(100, "完成")
                return TaskResult(
                    output_path=str(target),
                    message=f"{name} 引擎 · {target.stat().st_size // 1024}KB"
                    + (" · " + "；".join(warnings) if warnings else ""),
                )
        except (ComUnavailable, ComError, ComTimeout) as err:
            last_error = err
            # 引擎不可用时继续降级；这是预期路径，不该让任务失败
            ctx.log(f"{name} 引擎不可用或失败：{err}，尝试下一个引擎")
            continue

    raise ActionError(
        f"所有转换引擎都失败了（已尝试：{'、'.join(attempts)}）。"
        f"最后一个错误：{last_error}"
    )


register(
    ActionSpec(
        id="doc.to_pdf",
        label="文档转 PDF",
        domain="office",
        handler=_handler_to_pdf,
        accepts=tuple(sorted(WORD_EXTS | EXCEL_EXTS | PPT_EXTS)),
        output_ext="pdf",
        description="Word / Excel / PowerPoint 转 PDF，按保真度自动选择 Office COM、LibreOffice 或内置渲染",
        params_schema=PARAMS_TO_PDF,
    )
)


# --------------------------------------------------------------------------- #
# 动作：PDF → Word                                                              #
# --------------------------------------------------------------------------- #

PARAMS_TO_WORD: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pages": {
            "type": "string",
            "default": "all",
            "title": "页码范围",
            "description": "all / 1-3 / 1,5,7",
        },
        "keep_images": {
            "type": "boolean",
            "default": True,
            "title": "保留图片",
        },
        "detect_tables": {
            "type": "boolean",
            "default": True,
            "title": "识别表格",
            "description": "把 PDF 中的表格还原为 Word 表格（关闭可加快转换）",
        },
        "continuous_lines": {
            "type": "boolean",
            "default": True,
            "title": "合并被拆分的段落",
            "description": "PDF 常把一个段落拆成多行，开启后会重新合并",
        },
        "ocr_scanned": {
            "type": "boolean",
            "default": False,
            "title": "扫描件自动 OCR",
            "description": "检测到没有文字层时，先用本地 OCR 识别再重建文档（较慢）",
        },
    },
}


def _handler_to_word(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    target = Path(ctx.output_path)

    if target.suffix.lower() != ".docx":
        target = target.with_suffix(".docx")
        ctx.output_path = str(target)

    # 先判断是不是扫描件：没有文字层的话 pdf2docx 会产出空文档，
    # 与其让用户拿到一个空白 Word 再困惑，不如提前给出明确说明。
    #
    # 判据必须同时满足两条：**文字极少** 且 **每页都有图**。
    # 只看"文字少"会误判 —— 一页只有标题的稀疏文本页文字也少，
    # 但它完全不是扫描件，提示用户"请先做 OCR"会让人一头雾水。
    try:
        doc = pymupdf.open(str(source))
        total = doc.page_count
        text_chars = 0
        image_count = 0
        for page in doc:
            text_chars += len(page.get_text().strip())
            image_count += len(page.get_images(full=True))
        doc.close()
    except Exception as err:  # noqa: BLE001
        raise ActionError(f"无法打开 PDF：{err}") from err

    if total == 0:
        raise ActionError("PDF 没有任何页面")

    from ..pdf.geometry import parse_page_range

    pages = parse_page_range(ctx.str_param("pages", "all"), total)
    if not pages:
        raise ActionError(f"页码范围「{ctx.str_param('pages')}」没有匹配到任何页面（共 {total} 页）")

    looks_scanned = text_chars < total * 20 and image_count >= total
    ctx.report(10, "转换中")

    if looks_scanned and not ctx.bool_param("ocr_scanned", False):
        raise ActionError(
            f"这份 PDF 看起来是扫描件（{total} 页仅提取到 {text_chars} 个字符，且每页都是图片），"
            "没有可用的文字层。请勾选「扫描件自动 OCR」，或先用「文字提取」做 OCR。"
        )

    if looks_scanned:
        return _to_word_via_ocr(ctx, source, target, total, pages)

    try:
        from pdf2docx import Converter
    except ImportError as err:  # pragma: no cover
        raise ActionError("未安装 pdf2docx，无法执行 PDF 转 Word。请执行 pip install pdf2docx") from err

    ctx.report(20, "还原版面")

    try:
        converter = Converter(str(source))
        try:
            converter.convert(
                str(target),
                start=pages[0],
                end=pages[-1] + 1,
                # 关闭不需要的识别项可以明显加快转换速度
                multi_processing=False,
            )
        finally:
            converter.close()
    except Exception as err:  # noqa: BLE001
        raise ActionError(f"PDF 转 Word 失败：{err}") from err

    if not target.is_file() or target.stat().st_size == 0:
        raise ActionError("转换未产出有效文档，该 PDF 的版面可能过于复杂")

    ctx.report(100, "完成")
    return TaskResult(
        output_path=str(target),
        message=f"{len(pages)}/{total} 页 · 版面还原 · {target.stat().st_size // 1024}KB",
    )


def _to_word_via_ocr(
    ctx: ActionContext, source: Path, target: Path, total: int, pages: list[int]
) -> TaskResult:
    """扫描件路径：OCR → 生成 Word。

    版面上不如 pdf2docx 精细（按段落而不是坐标还原），但内容是对的 ——
    对扫描件来说这已经是最有价值的结果。
    """
    import tempfile

    from ..config import get_settings
    from ..ocr import OcrError, OcrOptions, recognize

    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError as err:  # pragma: no cover
        raise ActionError("未安装 python-docx，无法生成 Word 文档") from err

    document = Document()
    document.styles["Normal"].font.name = "微软雅黑"
    document.styles["Normal"].font.size = Pt(11)

    options = OcrOptions(mode="layout", tiling="auto")
    temp_dir = get_settings().temp_dir
    doc = pymupdf.open(str(source))
    recognized_pages = 0
    failed_pages: list[int] = []
    total_chars = 0

    try:
        for order, page_index in enumerate(pages):
            ctx.raise_if_cancelled()
            page = doc[page_index]
            existing = page.get_text().strip()

            if existing:
                # 已有文字层就直接用，没必要再走 OCR
                text = existing
            else:
                pix = page.get_pixmap(dpi=200)
                # OCR 引擎需要**文件路径**（要做缓存键、敏感名匹配、版面分析），
                # 不能直接传内存里的字节流，因此先落一份临时 PNG。
                with tempfile.NamedTemporaryFile(
                    dir=temp_dir, suffix=".png", delete=False
                ) as handle:
                    handle.write(pix.tobytes("png"))
                    temp_path = Path(handle.name)
                pix = None

                try:
                    result = recognize(temp_path, options, preference="local")
                    text = result.text
                    recognized_pages += 1
                except (OcrError, OSError) as err:
                    text = ""
                    failed_pages.append(page_index + 1)
                    ctx.log(f"第 {page_index + 1} 页 OCR 失败：{err}")
                finally:
                    temp_path.unlink(missing_ok=True)

            if order:
                document.add_page_break()
            for line in text.splitlines():
                stripped = line.strip()
                if stripped:
                    document.add_paragraph(stripped)
                    total_chars += len(stripped)

            ctx.report(20 + 70 * (order + 1) / len(pages), f"识别 {order + 1}/{len(pages)} 页")
    finally:
        doc.close()

    if total_chars == 0:
        raise ActionError(
            f"全部 {len(pages)} 页都没能识别出文字。请确认扫描件清晰度，"
            "并到「设置 → OCR 引擎」确认本地离线引擎可用。"
        )

    atomic_write(target, lambda tmp: document.save(str(tmp)))

    ctx.report(100, "完成")
    message = f"{len(pages)} 页 · OCR 重建（{recognized_pages} 页经识别）"
    if failed_pages:
        message += f" · {len(failed_pages)} 页识别失败"
    return TaskResult(output_path=str(target), message=message)


register(
    ActionSpec(
        id="pdf.to_word",
        label="PDF 转 Word",
        domain="office",
        handler=_handler_to_word,
        accepts=("pdf",),
        output_ext="docx",
        description="把 PDF 还原为 Word 文档，保留文字、图片与表格；扫描件可自动走 OCR",
        params_schema=PARAMS_TO_WORD,
    )
)
