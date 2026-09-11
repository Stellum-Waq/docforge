"""健康检查与系统能力探测接口。

这两个接口是前端"首次启动自检向导"和"引擎就绪状态面板"的数据来源。
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from ..config import get_settings
from ..engines.probe import EngineProbe, probe_engines
from .security import require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

_START_MONOTONIC = time.monotonic()
_START_WALL = datetime.now(timezone.utc)


class CamelModel(BaseModel):
    """统一以 camelCase 输出，与 TypeScript 侧的字段命名保持一致。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CoreHealth(CamelModel):
    phase: str
    version: str
    pid: int
    uptime_sec: float
    platform: str
    python: str
    jobs_handled: int
    message: str | None = None


class OsInfo(CamelModel):
    platform: str
    release: str
    arch: str
    hostname: str


class CpuInfo(CamelModel):
    cores: int
    physical_cores: int
    model: str


class MemoryInfo(CamelModel):
    total_bytes: int
    available_bytes: int


class DiskInfo(CamelModel):
    output_free_bytes: int


class SystemCapabilities(CamelModel):
    os: OsInfo
    cpu: CpuInfo
    memory: MemoryInfo
    disk: DiskInfo
    engines: list[EngineProbe]
    local_ocr_ready: bool
    cloud_ocr_configured: bool
    probed_at: str
    default_output_dir: str
    data_dir: str


@router.get("/health", response_model=CoreHealth)
async def health() -> CoreHealth:
    """内核存活探针。Electron 主进程在握手后立即调用它确认真实可用。"""
    settings = get_settings()
    return CoreHealth(
        phase="ready",
        version=settings.version,
        pid=os.getpid(),
        uptime_sec=round(time.monotonic() - _START_MONOTONIC, 3),
        platform=platform.platform(),
        python=f"Python {sys.version.split()[0]}",
        jobs_handled=0,
    )


class SelfCheckItem(CamelModel):
    """一条自检结果。"""

    id: str
    label: str
    status: str  # ok | warn | fail
    detail: str
    #: 该检查项不通过时对用户意味着什么（缺失时留空）
    impact: str | None = None


class SelfCheckReport(CamelModel):
    items: list[SelfCheckItem]
    ok_count: int
    warn_count: int
    fail_count: int
    #: 是否具备完成全部核心功能的条件（关键项无 fail）
    ready: bool


@router.get("/system/selfcheck", response_model=SelfCheckReport)
async def self_check() -> SelfCheckReport:
    """首次启动自检。

    存在的意义是回答用户最关心的那个问题：**"我这台机器上它能干什么？"**
    与其让用户一个个功能去试、撞到报错才知道缺东西，不如一次性把
    环境状况、缺失组件、以及缺失会有什么影响都摆清楚。

    状态分三档：
      * ``ok``   该能力可用
      * ``warn`` 可选能力缺失，功能会降级但仍可用
      * ``fail`` 关键能力缺失，相关功能不可用
    """
    import shutil as _shutil
    import sys as _sys

    from ..security.secrets import encryption_available, get_secret

    settings = get_settings()
    engines = {engine.id: engine for engine in probe_engines()}
    items: list[SelfCheckItem] = []

    def engine_state(engine_id: str, label: str, impact: str, *, critical: bool = False) -> None:
        engine = engines.get(engine_id)
        if engine is None:
            return
        if engine.available:
            items.append(SelfCheckItem(id=engine_id, label=label, status="ok", detail="可用"))
        else:
            items.append(
                SelfCheckItem(
                    id=engine_id,
                    label=label,
                    status="fail" if critical else "warn",
                    detail=engine.reason or "不可用",
                    impact=impact,
                )
            )

    # ---- 1. 内核自身 ----
    items.append(
        SelfCheckItem(
            id="core.runtime",
            label="处理内核",
            status="ok",
            detail=f"Python {_sys.version.split()[0]} · {settings.version}",
        )
    )

    # ---- 2. 数据目录可写 ----
    if settings.data_dir_warning:
        items.append(
            SelfCheckItem(
                id="storage.data_dir",
                label="数据目录",
                status="warn",
                detail=settings.data_dir_warning,
                impact="任务历史与配置会保存在备用目录，功能不受影响",
            )
        )
    else:
        items.append(
            SelfCheckItem(
                id="storage.data_dir",
                label="数据目录",
                status="ok",
                detail=str(settings.data_dir),
            )
        )

    # ---- 3. 输出目录可写 ----
    try:
        settings.default_output_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.default_output_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        items.append(
            SelfCheckItem(
                id="storage.output_dir",
                label="输出目录可写",
                status="ok",
                detail=str(settings.default_output_dir),
            )
        )
    except OSError as err:
        items.append(
            SelfCheckItem(
                id="storage.output_dir",
                label="输出目录可写",
                status="fail",
                detail=f"无法写入：{err}",
                impact="转换结果无处保存，请在界面上另选输出目录",
            )
        )

    # ---- 4. 磁盘空间 ----
    try:
        free_bytes = _shutil.disk_usage(settings.data_dir).free
        free_gb = free_bytes / 1024**3
        status = "ok" if free_gb >= 2 else "warn" if free_gb >= 0.5 else "fail"
        items.append(
            SelfCheckItem(
                id="storage.disk",
                label="磁盘空间",
                status=status,
                detail=f"可用 {free_gb:.1f} GB",
                impact=None if status == "ok" else "批量处理大文件时可能空间不足",
            )
        )
    except OSError:
        pass

    # ---- 5. 中文字体（图片水印需要）----
    font_dir = Path("C:/Windows/Fonts")
    cjk_fonts = ["msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"]
    found_font = next((name for name in cjk_fonts if (font_dir / name).is_file()), None)
    items.append(
        SelfCheckItem(
            id="render.cjk_font",
            label="中文字体",
            status="ok" if found_font else "warn",
            detail=found_font or "未找到常用中文字体",
            impact=None if found_font else "图片水印的中文可能显示为方块",
        )
    )

    # ---- 6. 文档转换引擎 ----
    com_available = any(
        engines.get(f"office.{app}.com", None) and engines[f"office.{app}.com"].available
        for app in ("word", "excel", "powerpoint")
    )
    libre = engines.get("office.libreoffice")
    if com_available:
        items.append(
            SelfCheckItem(
                id="office.com",
                label="Microsoft Office",
                status="ok",
                detail="已检测到，文档转换使用最高保真度引擎",
            )
        )
    elif libre is not None and libre.available:
        items.append(
            SelfCheckItem(
                id="office.libreoffice",
                label="LibreOffice",
                status="ok",
                detail="已检测到，文档转换使用 LibreOffice 引擎",
            )
        )
    else:
        items.append(
            SelfCheckItem(
                id="office.none",
                label="Office 转换引擎",
                status="warn",
                detail="未检测到 Office 或 LibreOffice",
                impact="Word/Excel/PPT 转 PDF 将使用内置渲染，版式保真度较低（文字内容仍完整）",
            )
        )

    # ---- 7. PDF 处理 ----
    engine_state("pdf.pymupdf", "PDF 处理引擎", "PDF 相关功能不可用", critical=True)
    engine_state("pdf.pdf2docx", "PDF 转 Word 引擎", "PDF 转 Word 不可用；请安装 pdf2docx")

    # ---- 8. 图像处理 ----
    engine_state("image.pillow", "图像处理引擎", "图片水印等功能不可用", critical=True)
    engine_state("image.heif", "HEIC 支持", "iPhone 照片（.heic）无法读取")

    # ---- 9. OCR ----
    engine_state(
        "ocr.rapidocr",
        "本地离线 OCR",
        "文字提取与图片转 Excel 不可用；请安装 rapidocr-onnxruntime + onnxruntime",
        critical=True,
    )
    if get_secret("deepseek_api_key"):
        items.append(
            SelfCheckItem(
                id="ocr.cloud",
                label="DeepSeek 云端 OCR",
                status="ok",
                detail="已配置 API Key",
            )
        )
    else:
        items.append(
            SelfCheckItem(
                id="ocr.cloud",
                label="DeepSeek 云端 OCR",
                status="warn",
                detail="未配置 API Key",
                impact="复杂表格与手写识别只能使用本地引擎，精度略低（本地引擎完全可用）",
            )
        )

    # ---- 10. 密钥存储安全性 ----
    items.append(
        SelfCheckItem(
            id="security.keyring",
            label="密钥加密存储",
            status="ok" if encryption_available() else "warn",
            detail="Windows DPAPI（仅当前用户可解密）" if encryption_available() else "当前平台不支持 DPAPI，密钥以明文保存",
            impact=None if encryption_available() else "建议只使用本地离线 OCR",
        )
    )

    ok_count = sum(1 for item in items if item.status == "ok")
    warn_count = sum(1 for item in items if item.status == "warn")
    fail_count = sum(1 for item in items if item.status == "fail")

    return SelfCheckReport(
        items=items,
        ok_count=ok_count,
        warn_count=warn_count,
        fail_count=fail_count,
        ready=fail_count == 0,
    )


@router.post("/shutdown")
async def shutdown() -> dict[str, object]:
    """请求内核优雅退出。

    Electron 关闭应用时会先调这个接口，让内核有机会回收 Office COM 进程，
    而不是直接 TerminateProcess 把 WINWORD.EXE 留在用户机器上。
    """
    from ..lifecycle import request_shutdown

    request_shutdown()
    return {"ok": True, "message": "内核正在优雅退出"}


@router.get("/system/capabilities", response_model=SystemCapabilities)
async def capabilities() -> SystemCapabilities:
    """探测系统资源与全部可用引擎，供前端展示与引擎路由决策。"""
    settings = get_settings()
    engines = probe_engines()

    # 输出目录所在卷的可用空间：任务开始前用它做磁盘空间预检
    try:
        settings.default_output_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(settings.default_output_dir).free
    except OSError:
        free = shutil.disk_usage(settings.data_dir).free

    cpu_model = platform.processor() or "unknown"
    try:
        physical = psutil.cpu_count(logical=False) or 0
    except Exception:
        physical = 0

    def available(engine_id: str) -> bool:
        return any(e.id == engine_id and e.available for e in engines)

    return SystemCapabilities(
        os=OsInfo(
            platform=platform.system(),
            release=platform.release(),
            arch=platform.machine(),
            hostname=socket.gethostname(),
        ),
        cpu=CpuInfo(
            cores=psutil.cpu_count(logical=True) or 0,
            physical_cores=physical,
            model=cpu_model,
        ),
        memory=MemoryInfo(
            total_bytes=psutil.virtual_memory().total,
            available_bytes=psutil.virtual_memory().available,
        ),
        disk=DiskInfo(output_free_bytes=free),
        engines=engines,
        local_ocr_ready=available("ocr.rapidocr") or available("ocr.paddleocr"),
        cloud_ocr_configured=available("ocr.deepseek.vision"),
        probed_at=datetime.now(timezone.utc).isoformat(),
        default_output_dir=str(settings.default_output_dir),
        data_dir=str(settings.data_dir),
    )
