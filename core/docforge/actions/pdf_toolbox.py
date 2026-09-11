"""PDF 工具箱动作集 —— 对应设计文档 §1-H 的「PDF 工具箱」。

包含：合并 / 拆分 / 旋转 / 压缩 / 提取页 / 转图片 / 文档信息。

## 关于「合并」为什么要单独的聚合模式

合并是**多文件进、单文件出**。如果硬套"每个文件一个输出"的模型，用户会拿到
一堆无意义的中间产物，而且并发执行时多个任务还会争抢同一个输出文件。
因此动作注册表支持 ``aggregate=True``：整批文件被当成一个任务，
handler 通过 ``ctx.files`` 拿到全部输入。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymupdf

from ..pdf.geometry import parse_page_range
from .base import ActionContext, ActionError, ActionSpec, TaskResult, atomic_write, register

# 打开 PDF 的统一入口：把 PyMuPDF 的异常翻译成用户能看懂的话
def _open(path: Path) -> pymupdf.Document:
    if not path.is_file():
        raise ActionError(f"文件不存在：{path}")
    try:
        doc = pymupdf.open(str(path))
    except Exception as err:  # noqa: BLE001
        raise ActionError(f"无法打开 PDF（可能已损坏）：{err}") from err
    if doc.needs_pass:
        doc.close()
        raise ActionError("该 PDF 已加密，请先用「PDF 解密」处理")
    return doc


def _save(doc: pymupdf.Document, target: Path, **extra: Any) -> None:
    """统一保存：开启垃圾回收与流压缩，否则处理后的文件往往比原文件还大。"""
    kwargs: dict[str, Any] = {"garbage": 4, "deflate": True, "clean": True}
    kwargs.update(extra)

    def _write(tmp: Path) -> None:
        doc.save(str(tmp), **kwargs)

    atomic_write(target, _write)


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 合并（聚合动作）                                                              #
# --------------------------------------------------------------------------- #

def _handler_merge(ctx: ActionContext) -> TaskResult:
    files = ctx.files
    if len(files) < 2:
        raise ActionError("合并至少需要 2 个 PDF 文件")

    merged = pymupdf.open()
    total_pages = 0
    failed: list[str] = []

    try:
        for index, raw in enumerate(files):
            ctx.raise_if_cancelled()
            path = Path(raw)
            try:
                source = pymupdf.open(str(path))
            except Exception as err:  # noqa: BLE001
                # 单个文件坏掉不该让整次合并失败 —— 记下来继续，最后一起告知
                failed.append(f"{path.name}（{err}）")
                continue

            if source.needs_pass:
                failed.append(f"{path.name}（已加密）")
                source.close()
                continue

            if bool(ctx.params.get("add_bookmarks", True)):
                # 用文件名加一层书签，合并后还能快速定位到每个源文件
                toc_entry = [1, path.stem, index + 1]
                merged.insert_pdf(source)
                merged.set_toc((merged.get_toc() or []) + [toc_entry])
            else:
                merged.insert_pdf(source)

            total_pages += source.page_count
            source.close()
            ctx.report(10 + 75 * (index + 1) / len(files), f"已合并 {index + 1}/{len(files)}")

        if total_pages == 0:
            raise ActionError("所有文件都无法读取，合并失败")

        target = Path(ctx.output_path)
        _save(merged, target)
    finally:
        merged.close()

    ctx.report(100, "完成")
    message = f"{len(files) - len(failed)} 个文件 · {total_pages} 页"
    if failed:
        message += f" · 跳过 {len(failed)} 个"
        ctx.log("以下文件被跳过：" + "；".join(failed))
    return TaskResult(output_path=str(Path(ctx.output_path)), message=message)


register(
    ActionSpec(
        id="pdf.merge",
        label="PDF 合并",
        domain="pdf",
        handler=_handler_merge,
        accepts=("pdf",),
        output_ext="pdf",
        aggregate=True,
        aggregate_name="合并结果_{count}个文件",
        description="把多个 PDF 按顺序合并成一个，可选为每个源文件生成书签",
        params_schema={
            "type": "object",
            "properties": {
                "add_bookmarks": {
                    "type": "boolean",
                    "default": True,
                    "title": "生成书签",
                    "description": "用文件名作为书签，合并后可快速跳转到各个源文件",
                }
            },
        },
    )
)


# --------------------------------------------------------------------------- #
# 拆分                                                                          #
# --------------------------------------------------------------------------- #

def _handler_split(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    doc = _open(source)
    produced: list[Path] = []

    try:
        total = doc.page_count
        mode = ctx.str_param("split_mode", "every")
        # 拆分会产生多个文件，统一放进以源文件名命名的子目录，避免污染输出目录
        base = Path(ctx.output_path)
        out_dir = base.parent / f"{base.stem}"
        out_dir.mkdir(parents=True, exist_ok=True)

        if mode == "ranges":
            groups: list[tuple[str, list[int]]] = []
            for chunk in ctx.str_param("ranges", "").replace("，", ",").split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                indices = parse_page_range(chunk, total)
                if indices:
                    groups.append((chunk.replace("-", "_"), indices))
            if not groups:
                raise ActionError("页码范围为空或不合法，例如应写成 1-3,4-6,7")
        else:
            size = max(1, _to_int(ctx.params.get("pages_per_file"), 1))
            groups = [
                (f"{start + 1}-{min(start + size, total)}", list(range(start, min(start + size, total))))
                for start in range(0, total, size)
            ]

        for index, (label, indices) in enumerate(groups):
            ctx.raise_if_cancelled()
            part = pymupdf.open()
            try:
                for page_index in indices:
                    part.insert_pdf(doc, from_page=page_index, to_page=page_index)
                target = out_dir / f"{base.stem}_{label}.pdf"
                _save(part, target)
                produced.append(target)
            finally:
                part.close()
            ctx.report(10 + 85 * (index + 1) / len(groups), f"已生成 {index + 1}/{len(groups)}")
    finally:
        doc.close()

    ctx.report(100, "完成")
    # 多产出动作把 output_path 指向第一个文件，数量写在 message 里
    return TaskResult(
        output_path=str(produced[0]) if produced else None,
        message=f"拆分为 {len(produced)} 个文件，输出目录 {produced[0].parent.name if produced else '—'}",
    )


register(
    ActionSpec(
        id="pdf.split",
        label="PDF 拆分",
        domain="pdf",
        handler=_handler_split,
        accepts=("pdf",),
        output_ext="pdf",
        description="按每 N 页或指定页码范围把 PDF 拆成多个文件",
        params_schema={
            "type": "object",
            "properties": {
                "split_mode": {
                    "type": "string",
                    "enum": ["every", "ranges"],
                    "default": "every",
                    "title": "拆分方式",
                    "description": "every=每 N 页一个文件；ranges=按自定义页码范围",
                },
                "pages_per_file": {
                    "type": "integer",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 500,
                    "title": "每个文件页数",
                },
                "ranges": {
                    "type": "string",
                    "default": "1-3,4-6",
                    "title": "页码范围",
                    "description": "每段生成一个文件，例如 1-3,4-6,7",
                },
            },
        },
    )
)


# --------------------------------------------------------------------------- #
# 旋转                                                                          #
# --------------------------------------------------------------------------- #

def _handler_rotate(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    doc = _open(source)
    try:
        total = doc.page_count
        pages = parse_page_range(ctx.str_param("pages", "all"), total)
        angle = _to_int(ctx.params.get("angle"), 90)
        if angle % 90 != 0:
            raise ActionError("旋转角度必须是 90 的倍数（PDF 页面只支持 0/90/180/270）")
        if not pages:
            raise ActionError("页码范围没有匹配到任何页面")

        for page_index in pages:
            ctx.raise_if_cancelled()
            page = doc[page_index]
            # += 而不是 =：在已有旋转角基础上叠加，避免把用户原本的旋转抹掉
            page.set_rotation((page.rotation + angle) % 360)

        _save(doc, Path(ctx.output_path))
    finally:
        doc.close()

    ctx.report(100, "完成")
    return TaskResult(
        output_path=str(Path(ctx.output_path)),
        message=f"{len(pages)}/{total} 页旋转 {angle}°",
    )


register(
    ActionSpec(
        id="pdf.rotate",
        label="PDF 旋转页面",
        domain="pdf",
        handler=_handler_rotate,
        accepts=("pdf",),
        output_ext="pdf",
        description="把指定页面顺时针旋转 90/180/270 度",
        params_schema={
            "type": "object",
            "properties": {
                "angle": {
                    "type": "string",
                    "enum": ["90", "180", "270"],
                    "default": "90",
                    "title": "旋转角度",
                },
                "pages": {
                    "type": "string",
                    "default": "all",
                    "title": "页码范围",
                    "description": "all / 1-3 / 1,5,7",
                },
            },
        },
    )
)


# --------------------------------------------------------------------------- #
# 压缩                                                                          #
# --------------------------------------------------------------------------- #

def _handler_compress(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    doc = _open(source)
    original_size = source.stat().st_size

    try:
        # 字体子集化：只保留实际用到的字形，对中文字体尤其有效
        if bool(ctx.params.get("subset_fonts", True)):
            try:
                doc.subset_fonts()
            except Exception as err:  # noqa: BLE001 - 缺 fontTools 等情况，降级继续
                ctx.log(f"字体子集化跳过：{err}")

        # 图片降采样。PyMuPDF 在较新版本才提供该能力，因此做能力探测，
        # 老版本上自动跳过而不是报错。
        quality = _to_int(ctx.params.get("image_quality"), 75)
        dpi_target = _to_int(ctx.params.get("dpi_target"), 150)
        rewrite = getattr(doc, "rewrite_images", None)
        if callable(rewrite) and bool(ctx.params.get("shrink_images", True)):
            try:
                rewrite(dpi_threshold=dpi_target * 2, dpi_target=dpi_target, quality=quality)
            except Exception as err:  # noqa: BLE001
                ctx.log(f"图片降采样跳过（当前 PyMuPDF 版本可能不支持）：{err}")

        ctx.report(70, "写入压缩结果")
        target = Path(ctx.output_path)
        _save(doc, target, garbage=4, deflate=True, clean=True, linear=bool(ctx.params.get("linearize", False)))
    finally:
        doc.close()

    new_size = Path(ctx.output_path).stat().st_size
    ratio = (1 - new_size / original_size) * 100 if original_size else 0

    ctx.report(100, "完成")
    return TaskResult(
        output_path=str(Path(ctx.output_path)),
        message=f"{original_size // 1024}KB → {new_size // 1024}KB（缩小 {ratio:.0f}%）",
    )


register(
    ActionSpec(
        id="pdf.compress",
        label="PDF 压缩",
        domain="pdf",
        handler=_handler_compress,
        accepts=("pdf",),
        output_ext="pdf",
        description="通过字体子集化、图片降采样与流压缩减小 PDF 体积",
        params_schema={
            "type": "object",
            "properties": {
                "shrink_images": {"type": "boolean", "default": True, "title": "压缩内嵌图片"},
                "dpi_target": {
                    "type": "integer", "default": 150, "minimum": 72, "maximum": 600,
                    "title": "图片目标 DPI", "description": "150 适合屏幕阅读，300 适合打印",
                },
                "image_quality": {
                    "type": "integer", "default": 75, "minimum": 30, "maximum": 95,
                    "title": "图片质量",
                },
                "subset_fonts": {"type": "boolean", "default": True, "title": "字体子集化"},
                "linearize": {
                    "type": "boolean", "default": False,
                    "title": "Web 优化（线性化）", "description": "支持浏览器边下边看，文件会略大",
                },
            },
        },
    )
)


# --------------------------------------------------------------------------- #
# 提取页                                                                        #
# --------------------------------------------------------------------------- #

def _handler_extract(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    doc = _open(source)
    try:
        total = doc.page_count
        pages = parse_page_range(ctx.str_param("pages", "1"), total)
        if not pages:
            raise ActionError(f"页码范围「{ctx.str_param('pages')}」没有匹配到任何页面（共 {total} 页）")

        result = pymupdf.open()
        try:
            for page_index in pages:
                result.insert_pdf(doc, from_page=page_index, to_page=page_index)
            _save(result, Path(ctx.output_path))
        finally:
            result.close()
    finally:
        doc.close()

    ctx.report(100, "完成")
    return TaskResult(
        output_path=str(Path(ctx.output_path)),
        message=f"提取 {len(pages)}/{total} 页",
    )


register(
    ActionSpec(
        id="pdf.extract_pages",
        label="PDF 提取页面",
        domain="pdf",
        handler=_handler_extract,
        accepts=("pdf",),
        output_ext="pdf",
        description="把指定页面提取成一个新的 PDF",
        params_schema={
            "type": "object",
            "properties": {
                "pages": {
                    "type": "string",
                    "default": "1",
                    "title": "页码范围",
                    "description": "all / 1-3 / 1,5,7 / 3-",
                }
            },
            "required": ["pages"],
        },
    )
)


# --------------------------------------------------------------------------- #
# 转图片                                                                        #
# --------------------------------------------------------------------------- #

def _handler_to_images(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    doc = _open(source)
    produced: list[Path] = []

    try:
        total = doc.page_count
        pages = parse_page_range(ctx.str_param("pages", "all"), total) or list(range(total))
        dpi = max(36, min(600, _to_int(ctx.params.get("dpi"), 150)))
        fmt = ctx.str_param("image_format", "png").lower()
        if fmt not in ("png", "jpg", "jpeg"):
            fmt = "png"

        base = Path(ctx.output_path)
        out_dir = base.parent / f"{base.stem}_图片"
        out_dir.mkdir(parents=True, exist_ok=True)

        for order, page_index in enumerate(pages):
            ctx.raise_if_cancelled()
            page = doc[page_index]
            pix = page.get_pixmap(dpi=dpi, alpha=(fmt == "png"))

            target = out_dir / f"{base.stem}_第{page_index + 1}页.{fmt}"
            if fmt == "png":
                pix.save(str(target))
            else:
                # JPEG 不支持透明通道，必须转成 RGB 再存，否则 PyMuPDF 会报错
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                pix.save(str(target), jpg_quality=_to_int(ctx.params.get("quality"), 90))
            pix = None
            produced.append(target)

            ctx.report(10 + 85 * (order + 1) / len(pages), f"已导出 {order + 1}/{len(pages)} 页")
    finally:
        doc.close()

    ctx.report(100, "完成")
    return TaskResult(
        output_path=str(produced[0]) if produced else None,
        message=f"导出 {len(produced)} 张图片（{dpi} DPI，{fmt.upper()}）",
    )


register(
    ActionSpec(
        id="pdf.to_images",
        label="PDF 转图片",
        domain="pdf",
        handler=_handler_to_images,
        accepts=("pdf",),
        output_ext="png",
        description="把 PDF 页面渲染成 PNG / JPEG 图片",
        params_schema={
            "type": "object",
            "properties": {
                "image_format": {
                    "type": "string", "enum": ["png", "jpg"], "default": "png", "title": "图片格式",
                },
                "dpi": {
                    "type": "integer", "default": 150, "minimum": 36, "maximum": 600,
                    "title": "渲染 DPI", "description": "72=屏幕预览，150=清晰，300=打印级",
                },
                "quality": {
                    "type": "integer", "default": 90, "minimum": 30, "maximum": 100,
                    "title": "JPEG 质量",
                },
                "pages": {"type": "string", "default": "all", "title": "页码范围"},
            },
        },
    )
)


# --------------------------------------------------------------------------- #
# 文档信息                                                                      #
# --------------------------------------------------------------------------- #

def _handler_info(ctx: ActionContext) -> TaskResult:
    source = Path(ctx.file_path)
    doc = _open(source)

    try:
        pages_info = []
        total_chars = 0
        total_images = 0

        for index, page in enumerate(doc):
            text = page.get_text()
            images = page.get_images(full=True)
            total_chars += len(text.strip())
            total_images += len(images)
            pages_info.append(
                {
                    "page": index + 1,
                    "width": round(page.rect.width, 1),
                    "height": round(page.rect.height, 1),
                    "rotation": page.rotation,
                    "characters": len(text.strip()),
                    "images": len(images),
                    # 文字极少通常说明是扫描件，需要先做 OCR
                    "likelyScanned": len(text.strip()) < 20 and len(images) > 0,
                }
            )

        metadata = {k: v for k, v in (doc.metadata or {}).items() if v}
        payload = {
            "file": source.name,
            "sizeBytes": source.stat().st_size,
            "pageCount": doc.page_count,
            "encrypted": bool(doc.needs_pass),
            "metadata": metadata,
            "totalCharacters": total_chars,
            "totalImages": total_images,
            "scannedPageCount": sum(1 for p in pages_info if p["likelyScanned"]),
            "pages": pages_info,
        }
    finally:
        doc.close()

    target = Path(ctx.output_path)
    if target.suffix.lower() != ".json":
        target = target.with_suffix(".json")
        ctx.output_path = str(target)

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    atomic_write(target, lambda tmp: tmp.write_text(text, encoding="utf-8"))

    ctx.report(100, "完成")
    scanned = payload["scannedPageCount"]
    message = f"{payload['pageCount']} 页 · {total_chars} 字符 · {total_images} 张图"
    if scanned:
        message += f" · {scanned} 页疑似扫描件"
    return TaskResult(output_path=str(target), message=message)


register(
    ActionSpec(
        id="pdf.info",
        label="PDF 文档信息",
        domain="pdf",
        handler=_handler_info,
        accepts=("pdf",),
        output_ext="json",
        description="导出 PDF 的页数、尺寸、元数据、文字与图片统计，并识别疑似扫描页",
        params_schema={"type": "object", "properties": {}},
    )
)
