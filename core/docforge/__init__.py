"""文枢 DocForge — 文档处理内核。

该包以独立的本地服务进程运行，仅监听 127.0.0.1，由 Electron 主进程通过
``python -m docforge`` 拉起，并通过随机端口 + 一次性 Bearer Token 通信。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
