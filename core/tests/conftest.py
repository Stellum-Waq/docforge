"""pytest 全局配置。

测试必须把数据目录重定向到仓库内的临时路径：
  * 默认的 %APPDATA%\\DocForge 在受限环境（CI、受控终端）中未必可写
  * 测试不应该污染开发者本机的真实任务历史与缓存

必须在导入 docforge 之前设置环境变量，因为 Settings 是 lru_cache 单例。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_TEST_DATA_DIR = Path(__file__).parent / ".pytest-data"
os.environ["DOCFORGE_DATA_DIR"] = str(_TEST_DATA_DIR)
# 测试进程没有 Electron 父进程，显式清掉避免父进程看门狗误判退出
os.environ.pop("DOCFORGE_PARENT_PID", None)

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_data() -> None:
    yield
    # 测试进程和内核不一样：它不会走 __main__ 的退出清理路径。
    # 用例里真实拉起过 Office COM 的，如果不在收尾时回收，
    # 就会在开发机上攒下一堆无主的 WINWORD.EXE。
    try:
        from docforge.engines.com import shutdown_all

        shutdown_all()
    except Exception:  # noqa: BLE001 - 没有 pywin32 的环境直接跳过
        pass
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
