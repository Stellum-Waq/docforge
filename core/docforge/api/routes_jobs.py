"""任务相关接口：动作发现、任务创建、进度流、历史记录。

进度推送用 SSE 而不是 WebSocket：这里是**单向**的服务端推送，SSE 更简单、
自带断线重连、且能穿过所有代理。Electron 主进程订阅后再转发给渲染进程，
渲染进程因此完全不需要接触内核 Token（设计文档 §2.4 的安全模型）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..actions import ActionError, list_actions
from ..jobs import JobRequest, get_manager
from ..storage.db import get_db
from .security import require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

# SSE 心跳间隔：防止中间层因长时间无数据而断开连接
_HEARTBEAT_SEC = 15.0


class CreateJobBody(BaseModel):
    action: str = Field(..., description="动作 id，例如 image.watermark")
    files: list[str] = Field(..., min_length=1, description="待处理文件的绝对路径")
    outputDir: str = Field("", description="输出目录；留空则用内核默认输出目录")
    params: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = Field(4, ge=1, le=16)
    suffix: str = Field("", description="输出文件名追加的后缀，如 _已处理")
    conflictPolicy: str = Field("rename", pattern="^(rename|overwrite|skip)$")
    preserveTree: bool = Field(False, description="保留源目录结构")
    rootDir: str | None = Field(None, description="preserveTree 时的相对基准目录")


# --------------------------------------------------------------------------- #
# 动作发现                                                                      #
# --------------------------------------------------------------------------- #

@router.get("/actions")
async def get_actions() -> dict[str, Any]:
    """返回所有已注册动作及其参数 Schema，UI 据此自动生成参数表单。"""
    return {
        "actions": [
            {
                "id": spec.id,
                "label": spec.label,
                "domain": spec.domain,
                "description": spec.description,
                "accepts": list(spec.accepts),
                "outputExt": spec.output_ext,
                "paramsSchema": spec.params_schema,
                # 聚合动作是"多文件进、单文件出"（例如 PDF 合并）。
                # 界面需要据此改变交互（只建一个任务、不显示并发数），
                # 因此必须在元数据里暴露，而不是让前端硬编码动作 id。
                "aggregate": spec.aggregate,
            }
            for spec in list_actions()
        ]
    }


# --------------------------------------------------------------------------- #
# 任务创建与控制                                                                 #
# --------------------------------------------------------------------------- #

@router.post("/jobs", status_code=201)
async def create_job(body: CreateJobBody) -> dict[str, Any]:
    manager = get_manager()

    from ..config import get_settings

    output_dir = body.outputDir.strip() or str(get_settings().default_output_dir)

    request = JobRequest(
        action=body.action,
        files=body.files,
        output_dir=output_dir,
        params=body.params,
        concurrency=body.concurrency,
        suffix=body.suffix,
        conflict_policy=body.conflictPolicy,
        preserve_tree=body.preserveTree,
        root_dir=body.rootDir,
    )

    try:
        job = manager.create_job(request)
    except ActionError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    manager.start_job(job)
    return {"job": job.to_dict(), "tasks": [t.to_dict() for t in job.tasks]}


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    manager = get_manager()
    return {"jobs": manager.list_active()}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    manager = get_manager()
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已从内存中淘汰")
    return {"job": job.to_dict(), "tasks": manager.get_tasks(job_id)}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = await get_manager().cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"job": job.to_dict()}


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict[str, Any]:
    job = await get_manager().pause(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"job": job.to_dict()}


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict[str, Any]:
    job = await get_manager().resume(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"job": job.to_dict()}


# --------------------------------------------------------------------------- #
# 进度事件流（SSE）                                                             #
# --------------------------------------------------------------------------- #

@router.get("/jobs/events/stream")
async def job_events(request: Request) -> StreamingResponse:
    manager = get_manager()

    async def generator():
        queue = manager.subscribe()
        try:
            # 先发一个 ready，让前端确认流已建立（避免"界面没反应"的困惑）
            yield f"event: ready\ndata: {json.dumps({'ok': True})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    # 注释行心跳，不触发前端事件回调
                    yield ": keep-alive\n\n"
                    continue
                payload = json.dumps(event, ensure_ascii=False)
                yield f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 之类中间层的缓冲（虽然是本机，保持习惯）
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# 历史记录                                                                      #
# --------------------------------------------------------------------------- #

@router.get("/jobs/history/list")
async def job_history(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    db = get_db()
    jobs = await db.list_jobs(limit=max(1, min(500, limit)), offset=max(0, offset))
    return {"jobs": jobs, "cache": await db.cache_stats()}


@router.get("/jobs/history/{job_id}/tasks")
async def job_history_tasks(job_id: str) -> dict[str, Any]:
    return {"tasks": await get_db().get_job_tasks(job_id)}


@router.delete("/jobs/history")
async def clear_history() -> dict[str, Any]:
    await get_db().clear_history()
    return {"ok": True}
