"""本地服务的鉴权层。

安全模型（对应设计文档 §2.4）：
  1. 服务只绑定 127.0.0.1 —— 局域网内其他机器无法访问。
  2. 每次启动生成一个随机 Token，通过 stdout 握手交给 Electron 主进程，
     再由 preload 注入渲染进程。Token 不落盘。
  3. 所有 /api/* 接口都要求 ``Authorization: Bearer <token>``，
     使用 :func:`hmac.compare_digest` 做常数时间比较，避免时序侧信道。

即便本机上的其他程序探测到了端口，没有 Token 也无法调用任何能力。
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Header, HTTPException, status

# 进程级一次性 Token。模块导入时生成，随进程退出而失效。
_API_TOKEN: str = secrets.token_urlsafe(32)


def get_token() -> str:
    return _API_TOKEN


async def require_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI 依赖：校验 Bearer Token。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Authorization: Bearer <token> 头",
            headers={"WWW-Authenticate": "Bearer"},
        )

    provided = authorization[len("Bearer ") :].strip()
    if not hmac.compare_digest(provided, _API_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token 无效",
        )
