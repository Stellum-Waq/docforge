"""PyInstaller 打包入口。

单独写一个入口脚本（而不是直接打包 ``docforge/__main__.py``）是为了：

* 处理 PyInstaller 的 ``sys._MEIPASS`` 路径问题 —— 冻结后包的搜索路径与源码运行时不同
* 调用 ``multiprocessing.freeze_support()``：PyInstaller 冻结的程序若不调用它，
  子进程会重新执行整个程序，形成进程爆炸（这里虽然没直接用多进程，
  但某些第三方库会，属于必要的防御）
* 保留一个干净的"源码直接跑"入口：``python core/run_core.py`` 与打包后行为一致
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


def _prepare_frozen_path() -> None:
    """冻结环境下把打包目录加入模块搜索路径。

    PyInstaller onedir 模式会把所有东西解到 ``sys._MEIPASS``（即 ``_internal`` 目录），
    其中包含了收集进来的 ``docforge`` 包和第三方依赖。正常情况下 PyInstaller 的
    引导代码已经处理好了，这里只是兜底，避免某些收集方式下找不到包。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and meipass not in sys.path:
        sys.path.insert(0, meipass)

    # 让工作目录落在可执行文件旁边，方便排查相对路径问题
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if str(exe_dir) not in sys.path:
            sys.path.insert(0, str(exe_dir))


def main() -> int:
    _prepare_frozen_path()

    from docforge.__main__ import main as core_main

    return core_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
