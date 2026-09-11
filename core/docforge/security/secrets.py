"""密钥安全存储。

## 为什么在 Python 侧用 DPAPI，而不是让 Electron 的 safeStorage 加密后传进来

设计文档原本写的是 Electron ``safeStorage``。两者底层都是 Windows DPAPI（按用户账户
加解密），安全性等价。但实际使用密钥的是 Python 内核，如果由 Electron 加密保管，
就意味着**每次调用云端 OCR 都要把明文密钥经 IPC 传一遍**，反而扩大了暴露面。

因此改为：内核直接用 ctypes 调用 ``crypt32.dll`` 的 CryptProtectData / CryptUnprotectData。
优点是不引入 pywin32 依赖、密钥明文只在真正要用它的那一瞬间存在于内存里，
而且 Electron 侧完全不需要知道密钥内容。

非 Windows 平台没有 DPAPI，退化为仅设置文件权限的明文存储，并**明确告知用户**
（而不是假装安全）。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from ..config import get_settings

SECRETS_FILE = "secrets.dat"

# 需要保护的字段名（只保护敏感项，其余配置明文存，便于用户排查）
SENSITIVE_KEYS = {"deepseek_api_key"}

# DPAPI 的附加熵：进一步限定只有本应用能解开
_ENTROPY = b"docforge.v1.secret"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _protect(data: bytes) -> bytes:
    """用 DPAPI 加密。仅当前用户可解密。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    blob_in = _blob(data)
    blob_entropy = _blob(_ENTROPY)
    blob_out = _DataBlob()

    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, ctypes.byref(blob_entropy), None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(f"DPAPI 加密失败，错误码 {ctypes.GetLastError()}")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    blob_in = _blob(data)
    blob_entropy = _blob(_ENTROPY)
    blob_out = _DataBlob()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, ctypes.byref(blob_entropy), None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(f"DPAPI 解密失败，错误码 {ctypes.GetLastError()}")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _secrets_path() -> Path:
    return get_settings().data_dir / SECRETS_FILE


def encryption_available() -> bool:
    return _dpapi_available()


def load_secrets() -> dict[str, Any]:
    """读取全部配置（敏感字段自动解密）。文件不存在时返回空字典。"""
    path = _secrets_path()
    if not path.is_file():
        return {}

    try:
        raw = path.read_bytes()
    except OSError:
        return {}

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in SENSITIVE_KEYS and isinstance(value, str) and value:
            if value.startswith("dpapi:"):
                try:
                    result[key] = _unprotect(bytes.fromhex(value[len("dpapi:") :])).decode("utf-8")
                except (OSError, ValueError):
                    # 换用户/换机器后解不开是正常情况，静默丢弃并让用户重填
                    result[key] = ""
            else:
                result[key] = value
        else:
            result[key] = value
    return result


def save_secrets(values: dict[str, Any]) -> None:
    """写入配置。敏感字段在 Windows 上用 DPAPI 加密后以 hex 存储。"""
    path = _secrets_path()
    existing = load_secrets()
    existing.update(values)

    payload: dict[str, Any] = {}
    for key, value in existing.items():
        if key in SENSITIVE_KEYS and isinstance(value, str) and value:
            if _dpapi_available():
                payload[key] = "dpapi:" + _protect(value.encode("utf-8")).hex()
            else:
                payload[key] = value
        else:
            payload[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if not _dpapi_available():
        # 没有 DPAPI 时至少把权限收紧到仅本人可读
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass

    tmp.replace(path)


def get_secret(key: str, default: str = "") -> str:
    """按优先级取密钥：环境变量 → 加密存储 → 默认值。

    环境变量优先是为了方便 CI 与临时联调，不必改动用户配置。
    """
    env_name = key.upper()
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value

    value = load_secrets().get(key)
    return value if isinstance(value, str) and value else default


def mask_secret(value: str) -> str:
    """给界面展示用的掩码，绝不回传明文。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 8}{value[-4:]}"
