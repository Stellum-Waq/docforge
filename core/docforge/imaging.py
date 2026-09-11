"""图像能力引导。

## 为什么需要这个模块

Pillow 默认**读不了 HEIC/HEIF**（iPhone 的默认照片格式），需要额外注册
``pillow_heif`` 提供的打开器。而"注册"这个动作只有真正执行了才生效 ——
仅仅 `pip install pillow-heif` 是不够的。

这带来两个后果：

1. **功能承诺落空**：图片水印与 OCR 的 accepts 里都写着 ``heic``/``heif``，
   但用户拖进一张 iPhone 照片会直接报"无法打开图片"。
2. **打包后行为不一致**：PyInstaller 只收集被静态 import 的模块，
   没人 import 的 ``pillow_heif`` 不会进包 —— 开发环境里明明可用，
   装出来的程序却不行（实测就是这样）。

因此这里统一在**内核启动时**注册一次，问题一次性解决；
同时 spec 里把 ``pillow_heif`` 列进 hiddenimports，保证它真的被打进包里。
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_registered: bool | None = None


def ensure_heif_support() -> bool:
    """注册 HEIF/HEIC 打开器。返回是否可用。

    幂等且线程安全：内核启动时会调一次，动作里也可以放心再调。
    """
    global _registered

    with _lock:
        if _registered is not None:
            return _registered

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            _registered = True
        except Exception:  # noqa: BLE001 - 缺依赖或注册失败都只是少一种格式
            _registered = False

        return _registered


def heif_available() -> bool:
    """只查询可用性，不产生副作用（供引擎探测使用）。"""
    import importlib.util

    return importlib.util.find_spec("pillow_heif") is not None
