"""OCR 引擎路由与降级。

对应设计文档 §1-G3。上层动作只调用 :func:`recognize`，不关心背后用的是哪个引擎：

* **local** —— 只用本地离线引擎（默认）。完全离线、零费用、文件不出本机。
* **cloud** —— 用 DeepSeek Vision。引擎内部会自动先做本地版面分析再切片，
  因此仍然是"混合模式"；失败时按设置降级到本地。
* **auto** —— 有云端密钥就用云端，否则用本地。

隐私策略：命中了用户配置的敏感关键词的文件**强制走本地**，无论用户选了什么。
这类文件一旦上传就收不回来，宁可识别质量差一点。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from .base import OcrEngine, OcrError, OcrOptions, OcrResult, OcrUnavailable, ProgressCallback
from .deepseek import get_engine as get_cloud_engine
from .local import get_engine as get_local_engine

#: 引擎偏好
PREFERENCES = ("local", "cloud", "auto")


@dataclass
class RouteTrace:
    """记录实际用了哪些引擎，便于界面解释"为什么这次走了云端"。"""

    used: str = ""
    attempted: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"used": self.used, "attempted": self.attempted, "notes": self.notes}


def all_engines() -> list[OcrEngine]:
    return [get_local_engine(), get_cloud_engine()]


def local_engines() -> list[OcrEngine]:
    return [engine for engine in all_engines() if engine.offline]


def cloud_engines() -> list[OcrEngine]:
    return [engine for engine in all_engines() if not engine.offline]


def _settings():
    from ..security.secrets import load_secrets

    return load_secrets()


def is_cloud_allowed() -> tuple[bool, str | None]:
    """云端识别是否被允许（总开关 + 密钥）。"""
    values = _settings()
    if not values.get("cloud_ocr_enabled", False):
        return False, "云端识别未启用（默认关闭，需在设置中显式开启）"
    return True, None


def matches_sensitive_pattern(path: Path | str) -> str | None:
    """检查文件名是否命中敏感关键词。返回命中的模式，未命中返回 None。"""
    values = _settings()
    patterns = values.get("sensitive_patterns") or []
    if not isinstance(patterns, list):
        return None

    name = Path(path).name.lower()
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        if fnmatch.fnmatch(name, pattern.strip().lower()):
            return pattern
    return None


def select_engines(
    image_path: Path | str,
    options: OcrOptions,
    preference: str = "local",
) -> tuple[list[OcrEngine], list[str]]:
    """按偏好与可用性排出引擎尝试顺序，并给出说明。"""
    notes: list[str] = []
    preference = preference if preference in PREFERENCES else "local"

    # 隐私优先：命中敏感关键词就彻底禁掉云端
    hit = matches_sensitive_pattern(image_path)
    if hit:
        notes.append(f"文件名命中敏感规则「{hit}」，已强制使用本地引擎，文件不会上传")
        return [e for e in local_engines() if e.availability()[0]], notes

    allowed = set(options.allowed_engines or [])

    def filter_allowed(engines: list[OcrEngine]) -> list[OcrEngine]:
        if not allowed:
            return engines
        return [e for e in engines if e.id in allowed]

    available_local = [e for e in filter_allowed(local_engines()) if e.availability()[0]]
    available_cloud: list[OcrEngine] = []

    cloud_ok, cloud_reason = is_cloud_allowed()
    if preference in ("cloud", "auto") and cloud_ok:
        available_cloud = [e for e in filter_allowed(cloud_engines()) if e.availability()[0]]
    elif preference in ("cloud", "auto") and not cloud_ok:
        notes.append(f"云端引擎不可用：{cloud_reason}")

    if preference == "local":
        ordered = available_local
    elif preference == "cloud":
        ordered = available_cloud + (available_local if options.allow_fallback else [])
        if not available_cloud and available_local and options.allow_fallback:
            notes.append("云端引擎不可用，已自动降级为本地离线识别")
    else:  # auto
        ordered = available_cloud + available_local
        if available_cloud:
            notes.append("已按自动策略选用云端高精度识别")
        elif available_local:
            notes.append("云端不可用，使用本地离线识别")

    return ordered, notes


def recognize(
    image_path: Path | str,
    options: OcrOptions,
    *,
    preference: str = "local",
    progress: ProgressCallback | None = None,
) -> OcrResult:
    """按路由顺序尝试识别，自动降级，并把过程记录进结果的 warnings。"""
    engines, notes = select_engines(image_path, options, preference)

    if not engines:
        raise OcrUnavailable(
            "没有可用的 OCR 引擎。请安装本地 OCR 组件（rapidocr-onnxruntime + onnxruntime），"
            "或在设置中配置并启用 DeepSeek API Key。"
        )

    trace = RouteTrace(notes=list(notes))
    last_error: Exception | None = None

    for index, engine in enumerate(engines):
        trace.attempted.append(engine.id)
        try:
            result = engine.recognize(image_path, options, progress)
            trace.used = engine.id
            result.warnings = list(dict.fromkeys([*trace.notes, *result.warnings]))
            return result
        except (OcrUnavailable, OcrError) as err:
            last_error = err
            if index < len(engines) - 1:
                trace.notes.append(f"{engine.label} 失败（{err}），正在切换到下一个引擎")

    raise last_error or OcrError("所有 OCR 引擎均失败")


def engine_status() -> list[dict[str, object]]:
    """给界面用的引擎可用性列表。"""
    cloud_ok, cloud_reason = is_cloud_allowed()
    status: list[dict[str, object]] = []

    for engine in all_engines():
        available, reason = engine.availability()
        if not engine.offline and not cloud_ok:
            available, reason = False, cloud_reason
        status.append(
            {
                "id": engine.id,
                "label": engine.label,
                "offline": engine.offline,
                "fidelity": engine.fidelity,
                "available": available,
                "reason": reason,
            }
        )
    return status
