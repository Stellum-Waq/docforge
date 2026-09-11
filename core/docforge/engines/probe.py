"""引擎能力探测。

设计文档 §2.4 的引擎路由表要求"启动时探测 + 优雅降级"，本模块就是那层探测。
原则：
  * 探测必须快（< 1s）且绝不抛异常 —— 任何一个探测器失败都不能拖垮内核启动。
  * 探测结果带 TTL 缓存；Office 状态在运行期不会变，缓存 10 分钟足够。
  * 只做"是否存在"的静态探测，不真的去实例化 COM 对象（那会拉起 WINWORD.EXE，
    既慢又会留下残留进程）。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path

from pydantic import BaseModel

from ..config import get_settings


class EngineProbe(BaseModel):
    """单个引擎的可用性描述，与 TypeScript 侧 EngineProbe 保持字段一致。"""

    id: str
    label: str
    domain: str          # office | pdf | image | ocr | archive
    available: bool
    preferred: bool = False
    fidelity: int = 0    # 保真度 0-100，路由排序依据
    detail: str | None = None
    reason: str | None = None


# --------------------------------------------------------------------------- #
# 单引擎探测                                                                    #
# --------------------------------------------------------------------------- #

def _module_available(name: str) -> bool:
    """不真正导入，只查 spec —— 避免加载重依赖拖慢启动。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _find_executable(candidates: list[str], names: list[str]) -> str | None:
    for path in candidates:
        p = Path(path)
        if p.is_file():
            return str(p)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _office_exe(app: str) -> str | None:
    """在 Office 的常见安装位置里找可执行文件。

    Office 2021/365 的 Click-to-Run 安装路径形如
    ``C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE``，
    但也可能存在 32 位或 MSI 安装路径，因此逐一尝试。
    """
    if sys.platform != "win32":
        return None

    names = {
        "word": "WINWORD.EXE",
        "excel": "EXCEL.EXE",
        "powerpoint": "POWERPNT.EXE",
    }
    exe = names[app]

    roots: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if not base:
            continue
        roots += [
            Path(base) / "Microsoft Office" / "root" / "Office16",
            Path(base) / "Microsoft Office" / "Office16",
            Path(base) / "Microsoft Office" / "Office15",
            Path(base) / "Microsoft Office 15" / "ClientX64",
            Path(base) / "Microsoft Office" / "root" / "Office15",
        ]
    roots += [Path("C:/Program Files/Microsoft Office/root/Office16")]

    for root in roots:
        candidate = root / exe
        if candidate.is_file():
            return str(candidate)
    return None


def _probe_office(app: str, label: str) -> EngineProbe:
    """探测 Office COM 引擎。这是本机保真度最高的转换路径。"""
    exe = _office_exe(app)
    if exe:
        return EngineProbe(
            id=f"office.{app}.com",
            label=label,
            domain="office",
            available=True,
            fidelity=100,
            detail=exe,
        )
    reason = (
        "未检测到 Microsoft Office"
        if sys.platform == "win32"
        else "COM 自动化仅在 Windows 上可用"
    )
    return EngineProbe(
        id=f"office.{app}.com",
        label=label,
        domain="office",
        available=False,
        fidelity=100,
        reason=reason,
    )


def _probe_libreoffice() -> EngineProbe:
    candidates: list[str] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            candidates.append(str(Path(base) / "LibreOffice" / "program" / "soffice.exe"))
    candidates += [
        "/usr/bin/soffice",
        "/usr/local/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    found = _find_executable(candidates, ["soffice", "soffice.exe"])
    if found:
        return EngineProbe(
            id="office.libreoffice",
            label="LibreOffice (headless)",
            domain="office",
            available=True,
            fidelity=82,
            detail=found,
        )
    return EngineProbe(
        id="office.libreoffice",
        label="LibreOffice (headless)",
        domain="office",
        available=False,
        fidelity=82,
        reason="未安装 LibreOffice；无 Office 环境下将退回内置渲染引擎",
    )


def _probe_builtin_render() -> EngineProbe:
    """内置兜底渲染引擎：docx/xlsx → HTML → Chromium printToPDF。

    永远可用（Chromium 随 Electron 一起分发），因此它是"最后一道防线"。
    """
    return EngineProbe(
        id="office.builtin.render",
        label="内置渲染引擎 (Chromium)",
        domain="office",
        available=True,
        fidelity=55,
        detail="无需外部依赖，保真度低于 Office COM，适合简单文档",
    )


def _probe_pdf_engines() -> list[EngineProbe]:
    probes: list[EngineProbe] = []

    # PyMuPDF 1.28 起 `fitz` 是已弃用的别名，新代码统一用 `pymupdf` 模块名。
    # 这里同时探测两者，兼容旧版本环境。
    pymupdf = _module_available("pymupdf") or _module_available("fitz")
    probes.append(
        EngineProbe(
            id="pdf.pymupdf",
            label="PyMuPDF",
            domain="pdf",
            available=pymupdf,
            fidelity=100,
            detail="水印 / 合并 / 拆分 / 压缩 / 渲染 / 双层 PDF 的主力引擎" if pymupdf else None,
            reason=None if pymupdf else "未安装 PyMuPDF",
        )
    )

    pypdf = _module_available("pypdf")
    # 注意：pypdf 目前**没有任何动作在用**（PDF 加密/权限功能尚未实现）。
    # 因此这里不把它列进能力矩阵 —— 探测器展示的是"用户能用的能力"，
    # 列一个装了也没人调用的库，对用户是误导。
    # 等到实现 PDF 加密/解密时再把它加回来，并确保真的被调用。
    _ = pypdf

    p2d = _module_available("pdf2docx")
    probes.append(
        EngineProbe(
            id="pdf.pdf2docx",
            label="pdf2docx",
            domain="pdf",
            available=p2d,
            fidelity=88,
            detail="PDF → Word 版面还原（表格/图片/分栏）" if p2d else None,
            reason=None if p2d else "未安装 pdf2docx（按需安装以启用 PDF→Word 高保真模式）",
        )
    )
    return probes


def _probe_image_engines() -> list[EngineProbe]:
    probes: list[EngineProbe] = []

    pillow = _module_available("PIL")
    probes.append(
        EngineProbe(
            id="image.pillow",
            label="Pillow",
            domain="image",
            available=pillow,
            fidelity=90,
            detail="格式转换 / 缩放 / 水印合成" if pillow else None,
            reason=None if pillow else "未安装 Pillow",
        )
    )

    heif = _module_available("pillow_heif")
    probes.append(
        EngineProbe(
            id="image.heif",
            label="pillow-heif",
            domain="image",
            available=heif,
            fidelity=90,
            detail="HEIC / HEIF 支持（iPhone 照片）" if heif else None,
            reason=None if heif else "未安装 pillow-heif，暂不支持 HEIC/HEIF 输入",
        )
    )
    cv2 = _module_available("cv2")
    probes.append(
        EngineProbe(
            id="image.opencv",
            label="OpenCV",
            domain="image",
            available=cv2,
            fidelity=90,
            detail="去噪 / 透视摆正 / 表格线检测" if cv2 else None,
            reason=None if cv2 else "未安装 opencv-python-headless，扫描件增强功能受限",
        )
    )
    return probes


def _probe_ocr_engines() -> list[EngineProbe]:
    probes: list[EngineProbe] = []

    rapid = _module_available("rapidocr_onnxruntime") and _module_available("onnxruntime")
    probes.append(
        EngineProbe(
            id="ocr.rapidocr",
            label="RapidOCR (本地离线)",
            domain="ocr",
            available=rapid,
            fidelity=78,
            detail="完全离线 · CPU 可跑 · 无需 PyTorch" if rapid else None,
            reason=None
            if rapid
            else "未安装本地 OCR 组件（rapidocr-onnxruntime + onnxruntime）",
        )
    )

    paddle = _module_available("paddleocr")
    probes.append(
        EngineProbe(
            id="ocr.paddleocr",
            label="PaddleOCR (本地增强)",
            domain="ocr",
            available=paddle,
            fidelity=88,
            detail="表格结构 + 版面分析能力更强" if paddle else None,
            reason=None if paddle else "未安装 PaddleOCR（可选增强包）",
        )
    )

    # 云端引擎：只要有 API Key 即可用，无需本地依赖
    settings = get_settings()
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY")) or (settings.data_dir / "secrets.json").exists()
    probes.append(
        EngineProbe(
            id="ocr.deepseek.vision",
            label="DeepSeek Vision (云端)",
            domain="ocr",
            available=has_key,
            fidelity=95,
            detail="deepseek-flash 多模态 · 自适应切片 · 复杂表格与手写效果最好" if has_key else None,
            reason=None if has_key else "尚未配置 DeepSeek API Key（在「设置 → OCR 引擎」中填写）",
        )
    )
    return probes


# --------------------------------------------------------------------------- #
# 汇总探测（带 TTL 缓存）                                                        #
# --------------------------------------------------------------------------- #

_cache: tuple[float, list[EngineProbe]] | None = None
_CACHE_TTL_SEC = 600.0


def probe_engines(force: bool = False) -> list[EngineProbe]:
    """探测全部引擎，并标注每个能力域的首选引擎（preferred=True）。"""
    global _cache
    now = time.monotonic()
    if not force and _cache and (now - _cache[0]) < _CACHE_TTL_SEC:
        return _cache[1]

    probes: list[EngineProbe] = [
        _probe_office("word", "Microsoft Word (COM)"),
        _probe_office("excel", "Microsoft Excel (COM)"),
        _probe_office("powerpoint", "Microsoft PowerPoint (COM)"),
        _probe_libreoffice(),
        _probe_builtin_render(),
        *_probe_pdf_engines(),
        *_probe_image_engines(),
        *_probe_ocr_engines(),
    ]

    # 每个域内挑出"可用且保真度最高"的引擎作为首选，供路由表使用
    best: dict[str, EngineProbe] = {}
    for probe in probes:
        if not probe.available:
            continue
        current = best.get(probe.domain)
        if current is None or probe.fidelity > current.fidelity:
            best[probe.domain] = probe
    for probe in probes:
        probe.preferred = best.get(probe.domain) is probe

    _cache = (now, probes)
    return probes
