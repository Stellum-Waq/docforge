"""内核进程入口：``python -m docforge``。

启动顺序：
  1. 选一个空闲端口（不写死，避免与其他程序冲突）
  2. 起 uvicorn（仅绑 127.0.0.1）
  3. 等到 HTTP 真正可服务后，向 stdout 打印握手行：
         @@DOCFORGE_READY@@ {"port": <port>, "token": "<token>"}
     Electron 主进程解析这一行拿到端口与 Token。
  4. 启动父进程看门狗：Electron 崩了/被强杀时，内核必须自己退出，
     否则会在用户机器上留下无主的 python.exe。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

import uvicorn

from . import __version__
from .api.security import get_token
from .app import create_app
from .config import get_settings
from .lifecycle import set_server, shutdown_requested

READY_MARKER = "@@DOCFORGE_READY@@"


def _find_free_port(host: str) -> int:
    """向操作系统申请一个空闲端口。

    注：bind 后立刻 close 存在极小的竞态窗口，但在本机单用户场景下可忽略；
    相比写死端口带来的冲突风险，这个方案更稳。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _cleanup_office_processes() -> None:
    """回收 Office COM 进程。

    内核退出前**必须**调用。COM 自动化最容易留下的后遗症就是无主的
    WINWORD.EXE：用户看不到窗口，进程却在后台占内存，还可能锁住文档
    导致下次转换失败。
    """
    try:
        from .engines.com import shutdown_all

        shutdown_all()
    except BaseException:  # noqa: BLE001 - 清理失败不该阻塞退出
        pass


def _start_parent_watchdog(parent_pid: int) -> None:
    """父进程消失时自动退出，防止产生孤儿 python.exe。

    注意这里**不能**直接 ``os._exit`` —— 必须先清理 Office 进程。
    Electron 若被强杀（任务管理器结束进程、崩溃），我们只能靠这条路径
    把 Word 收拾干净；否则用户会看到一堆看不见的 WINWORD.EXE 留着。
    """
    try:
        import psutil

        parent = psutil.Process(parent_pid)
    except Exception:
        return

    def watch() -> None:
        while True:
            try:
                if not parent.is_running():
                    break
            except Exception:
                break
            time.sleep(1.0)
        _cleanup_office_processes()
        os._exit(0)

    threading.Thread(target=watch, name="parent-watchdog", daemon=True).start()


def serve_main() -> int:
    """启动内核服务（Electron 通过 ``python -m docforge`` 走的就是这条路）。"""
    settings = get_settings()
    settings.port = _find_free_port(settings.host)
    token = get_token()

    app = create_app()
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
        # 内核是单机小服务，保持极简
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    # 把 server 交给 lifecycle，``POST /api/shutdown`` 才能触发优雅退出
    set_server(server)

    if settings.parent_pid:
        _start_parent_watchdog(settings.parent_pid)

    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    # 等 uvicorn 真正开始 accept 连接，再把握手信息交给 Electron
    deadline = time.monotonic() + 30.0
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)

    if not server.started:
        print("内核启动失败：uvicorn 未能在 30s 内就绪", file=sys.stderr, flush=True)
        return 1

    print(
        f"{READY_MARKER} {json.dumps({'port': settings.port, 'token': token})}",
        flush=True,
    )
    print(
        f"[docforge] v{__version__} 内核就绪 · http://{settings.host}:{settings.port}"
        f" · 数据目录 {settings.data_dir}",
        flush=True,
    )

    try:
        # 只要不是被显式请求退出，就持续等待 uvicorn 线程
        while thread.is_alive() and not shutdown_requested():
            thread.join(timeout=1.0)

        if shutdown_requested() and thread.is_alive():
            # 给 uvicorn 一点时间跑完 lifespan 收尾
            thread.join(timeout=10.0)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=5.0)
    finally:
        # 无论走哪条退出路径，都必须回收 Office 进程
        _cleanup_office_processes()

    return 0


def main() -> int:
    """入口分发。

    不带子命令时启动内核服务 —— 这是 Electron 拉起内核的方式，必须保持兼容；
    只有显式给出 CLI 子命令（``run`` / ``actions`` / ``watch`` …）才走命令行。
    """
    from .cli import is_cli_invocation, main as cli_main

    if is_cli_invocation(sys.argv):
        return cli_main()
    return serve_main()


if __name__ == "__main__":
    raise SystemExit(main())
