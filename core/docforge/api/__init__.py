"""API 层：FastAPI 路由与安全依赖。"""

from .security import require_token

__all__ = ["require_token"]
