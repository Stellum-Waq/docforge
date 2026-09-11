"""参数预览接口。

**为什么预览要放在内核里，而不是前端用 Canvas 自己画？**

因为"所见即所得"的前提是预览与真实输出出自同一套代码。如果前端用 Canvas 复刻一遍
水印排版逻辑，两边只要有像素级差异（字体度量、描边算法、旋转插值），用户就会在
"预览好看、结果不对"之间反复试错 —— 这恰恰是最消耗信任的体验。

所以这里的做法是：把原图按比例缩小，**用完全相同的动作代码**渲染一遍，把结果图
直接返回给前端。参数中的尺寸都是相对比例（字号/边距占短边百分比），因此缩小后的
预览与全尺寸输出在视觉上是一致的。

代价是一次 IPC + 一次内核算力，但预览图被限制在 720px 宽，实际开销很低。
"""

from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from ..actions import ActionContext, ActionError, get_action
from ..config import get_settings
from ..ocr import OcrError, OcrOptions, recognize
from .security import require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

# 预览最大宽度。太小看不清水印细节，太大则每次调参都要等，720 是实测的平衡点。
_MAX_PREVIEW_WIDTH = 1600


class PreviewBody(BaseModel):
    filePath: str = Field(..., description="源图绝对路径")
    params: dict = Field(default_factory=dict)
    maxWidth: int = Field(720, ge=120, le=_MAX_PREVIEW_WIDTH)


@router.post("/preview/image-watermark")
async def preview_image_watermark(body: PreviewBody) -> Response:
    settings = get_settings()
    source = Path(body.filePath)

    if not source.is_file():
        raise HTTPException(status_code=400, detail=f"源图不存在：{source}")

    token = uuid.uuid4().hex
    scaled_path = settings.temp_dir / f"preview-src-{token}.png"
    out_path = settings.temp_dir / f"preview-out-{token}.png"

    try:
        # 1) 缩小源图。EXIF 方向先摆正，否则预览会比输出多转一次
        try:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGBA")
        except OSError as err:
            raise HTTPException(status_code=400, detail=f"无法打开图片：{err}") from err

        max_width = min(body.maxWidth, _MAX_PREVIEW_WIDTH)
        if image.width > max_width:
            ratio = max_width / image.width
            image = image.resize(
                (max_width, max(1, int(round(image.height * ratio)))),
                Image.Resampling.LANCZOS,
            )
        image.save(scaled_path, format="PNG")

        # 2) 用与真实输出完全相同的动作代码渲染
        spec = get_action("image.watermark")
        params = dict(body.params)
        # 预览强制输出 PNG：中间结果不需要有损压缩，也避免格式转换掩盖问题
        params["output_format"] = "png"
        params.pop("__index", None)

        ctx = ActionContext(
            job_id="preview",
            task_id=token,
            file_path=str(scaled_path),
            output_path=str(out_path),
            params=params,
        )
        spec.handler(ctx)

        if not out_path.is_file():
            raise HTTPException(status_code=500, detail="预览渲染未产生输出")

        return Response(
            content=out_path.read_bytes(),
            media_type="image/png",
            headers={
                # 预览是随参数变化的，绝不能被缓存，否则改参数看不到变化
                "Cache-Control": "no-store, max-age=0",
                "X-Preview-Width": str(image.width),
            },
        )
    except ActionError as err:
        # 参数非法（空文字、缺图等）属于用户可修正的问题，回 400 并给出可读原因
        raise HTTPException(status_code=400, detail=str(err)) from err
    finally:
        for path in (scaled_path, out_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


class ThumbnailBody(BaseModel):
    filePath: str = Field(..., description="源图绝对路径")
    maxWidth: int = Field(480, ge=64, le=1600)


@router.post("/preview/thumbnail")
async def preview_thumbnail(body: ThumbnailBody) -> Response:
    """返回原图缩略图（PNG）。

    渲染进程在开发模式下页面来源是 http://localhost，无法直接加载 ``file://``
    图片（会被 webSecurity 拦掉）。走内核生成缩略图既绕开了这个限制，
    又顺带统一处理了 EXIF 方向与 HEIC 等特殊格式。
    """
    source = Path(body.filePath)
    if not source.is_file():
        raise HTTPException(status_code=400, detail=f"文件不存在：{source}")

    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGBA")
    except OSError as err:
        raise HTTPException(status_code=400, detail=f"无法打开图片：{err}") from err

    if image.width > body.maxWidth:
        ratio = body.maxWidth / image.width
        image = image.resize(
            (body.maxWidth, max(1, int(round(image.height * ratio)))),
            Image.Resampling.LANCZOS,
        )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)

    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# --------------------------------------------------------------------------- #
# OCR 试识别                                                                     #
# --------------------------------------------------------------------------- #

class OcrTestBody(BaseModel):
    filePath: str = Field(..., description="源图绝对路径")
    engine: str = Field("local", description="local / cloud / auto")
    mode: str = Field("text", description="text / layout / table / formula / card")
    tiling: str = Field("auto")
    detail: str = Field("original")
    language: str = Field("auto")
    maxChars: int = Field(20000, ge=200, le=200000)


@router.post("/preview/ocr-test")
async def preview_ocr(body: OcrTestBody) -> dict:
    """对单个文件试识别，返回文本与元信息，**不产出文件**。

    存在的意义是"先试一张，满意再批量"：OCR 的参数（引擎、切片策略、模式）
    对结果影响很大。如果只能批量跑完才知道效果，用户会浪费大量时间，
    云端模式下还会白白花钱。因此提供一个低成本的单张预览。

    识别是 CPU/网络密集型，这里用 ``asyncio.to_thread`` 丢到线程池，
    避免阻塞 FastAPI 的事件循环（否则预览期间整个内核都会失去响应）。
    """
    source = Path(body.filePath)
    if not source.is_file():
        raise HTTPException(status_code=400, detail=f"文件不存在：{source}")

    options = OcrOptions(
        mode=body.mode,
        language=body.language,
        tiling=body.tiling,
        detail=body.detail,
        extract_tables=body.mode == "table",
    )

    try:
        result = await asyncio.to_thread(recognize, source, options, preference=body.engine)
    except OcrError as err:
        # 引擎不可用/识别失败都属于用户可修正的问题，回 400 并给出可读原因
        raise HTTPException(status_code=400, detail=str(err)) from err

    text = result.text
    truncated = len(text) > body.maxChars
    if truncated:
        text = text[: body.maxChars]

    return {
        "ok": True,
        "engine": result.engine,
        "text": text,
        "truncated": truncated,
        "lineCount": len(result.blocks),
        "width": result.width,
        "height": result.height,
        "tiles": result.tiles,
        "elapsedMs": result.elapsed_ms,
        "averageConfidence": round(result.average_confidence, 3),
        "warnings": result.warnings,
        "usage": result.usage,
        "tables": result.raw.get("tables") or [],
    }


# --------------------------------------------------------------------------- #
# PDF 预览                                                                      #
# --------------------------------------------------------------------------- #

class PdfMetaBody(BaseModel):
    filePath: str = Field(..., description="PDF 绝对路径")


class PdfPageBody(BaseModel):
    filePath: str
    page: int = Field(1, ge=1, description="1 起的页码")
    params: dict = Field(default_factory=dict, description="与正式任务相同的参数")
    maxWidth: int = Field(900, ge=200, le=2400)


@router.post("/preview/pdf-meta")
async def preview_pdf_meta(body: PdfMetaBody) -> dict:
    """返回 PDF 的页数与各页尺寸，供界面画页选择器。"""
    import pymupdf

    source = Path(body.filePath)
    if not source.is_file():
        raise HTTPException(status_code=400, detail=f"文件不存在：{source}")

    try:
        doc = pymupdf.open(str(source))
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"无法打开 PDF：{err}") from err

    try:
        if doc.needs_pass:
            raise HTTPException(status_code=400, detail="该 PDF 已加密，请先解密")

        pages = [
            {
                "page": index + 1,
                "width": round(page.rect.width, 1),
                "height": round(page.rect.height, 1),
                "rotation": page.rotation,
                # 文字很少且有图片，通常说明是扫描件，界面可以提示先做 OCR
                "likelyScanned": len(page.get_text().strip()) < 20 and len(page.get_images(full=True)) > 0,
            }
            for index, page in enumerate(doc)
        ]
        return {"pageCount": doc.page_count, "pages": pages}
    finally:
        doc.close()


@router.post("/preview/pdf-watermark")
async def preview_pdf_watermark(body: PdfPageBody) -> Response:
    """渲染"已加水印的某一页"，供界面实时预览。

    关键点：这里调用的是**与正式任务完全相同的** ``apply_pdf_watermark``，
    只是把页码范围限定到预览的那一页。因此不存在"预览和输出不一致"的可能 ——
    如果前端自己用 Canvas 复刻一遍排版，字体度量与坐标舍入的细微差异
    迟早会让用户看到"预览好看、结果不对"。
    """
    import pymupdf

    from ..actions.pdf_watermark import apply_pdf_watermark

    source = Path(body.filePath)
    if not source.is_file():
        raise HTTPException(status_code=400, detail=f"文件不存在：{source}")

    try:
        doc = pymupdf.open(str(source))
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"无法打开 PDF：{err}") from err

    try:
        total = doc.page_count
        if total == 0:
            raise HTTPException(status_code=400, detail="PDF 没有任何页面")
        page_index = max(0, min(total - 1, body.page - 1))
        doc.close()

        # 只对预览的这一页应用水印；其余参数与正式任务一致
        params = dict(body.params)
        params["pages"] = str(page_index + 1)

        result = apply_pdf_watermark(source, params)
        preview_doc = result["document"]

        try:
            page = preview_doc[page_index]
            # 预览按目标宽度反推 DPI，避免整页按 300 DPI 渲染造成几百毫秒的等待
            width_pt = max(1.0, page.rect.width)
            dpi = max(48, min(200, int(body.maxWidth / width_pt * 72)))
            pix = page.get_pixmap(dpi=dpi)
            data = pix.tobytes("png")
            pix = None
        finally:
            preview_doc.close()

        return Response(
            content=data,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Page-Count": str(total),
                "X-Page": str(page_index + 1),
            },
        )
    except HTTPException:
        raise
    except Exception as err:  # noqa: BLE001 - 参数问题属于用户可修正的错误
        raise HTTPException(status_code=400, detail=f"预览失败：{err}") from err
