"""FastAPI 应用装配。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api.routes import router as system_router
from .api.routes_jobs import router as jobs_router
from .api.routes_preview import router as preview_router
from .api.routes_presets import router as presets_router
from .api.routes_settings import router as settings_router
from .config import get_settings
from .jobs import get_manager


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动任务队列并建立数据库连接；退出时确保 worker 线程与连接被回收。"""
    # 注册 HEIC/HEIF 打开器。必须在内核启动时做一次 —— 只装 pillow-heif
    # 是不够的，不注册的话 Pillow 依然读不了 iPhone 照片，而图片水印与 OCR
    # 的 accepts 里都声明了 .heic，等于承诺了做不到的事。
    from .imaging import ensure_heif_support

    ensure_heif_support()

    manager = get_manager()
    await manager.start()
    try:
        yield
    finally:
        await manager.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=f"{settings.app_name} 内核",
        version=__version__,
        docs_url=None,      # 本地服务不暴露交互文档，减少攻击面
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # 渲染进程在开发期来源是 http://localhost:5173，生产期是 file://（Origin 为 null），
    # 因此需要放开 CORS。这在本场景下是安全的：
    #   * 服务只绑 127.0.0.1，外部无法访问
    #   * 所有接口都要求随机 Bearer Token，且不依赖 Cookie（allow_credentials=False）
    #   * 浏览器中的恶意页面既不知道随机端口，也拿不到 Token
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(system_router)
    app.include_router(jobs_router)
    app.include_router(preview_router)
    app.include_router(settings_router)
    app.include_router(presets_router)
    return app
