"""内核生命周期控制。

存在的理由很具体：Electron 停止内核时如果直接 ``TerminateProcess``
（Windows 上 ``child.kill()`` 就是这个语义），**Python 的清理代码一行都不会执行** ——
用 Office COM 转换过文档留下的 WINWORD.EXE 就此变成无主进程，
用户看不到窗口、却在后台占内存，还可能锁住文档导致下次转换失败。

因此内核提供一个优雅退出通道：先请求、等它自己收拾干净，超时了再强杀。
"""

from __future__ import annotations

import threading
from typing import Any

_shutdown_event = threading.Event()
_server: Any = None
_lock = threading.Lock()


def set_server(server: Any) -> None:
    """由进程入口注入 uvicorn Server 实例，用于触发它的优雅退出。"""
    global _server
    with _lock:
        _server = server


def request_shutdown() -> None:
    """请求内核退出（会先执行清理）。"""
    with _lock:
        server = _server
    if server is not None:
        # 让 uvicorn 停止接受新连接并跑完 lifespan 收尾
        server.should_exit = True
    _shutdown_event.set()


def shutdown_requested() -> bool:
    return _shutdown_event.is_set()
