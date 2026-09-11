"""任务队列内核。

所有功能都跑在这一层之上，因此它必须把下面这些事一次性做对：

* **并发**：用线程池而非进程池。Pillow / PyMuPDF / ONNX 的重活都会释放 GIL，
  线程已能拿到真实并行；而进程池需要序列化参数与结果、无法共享字体与模型缓存、
  取消也不及时。这是刻意取舍，不是偷懒。
* **进度**：动作在工作线程里回报进度，必须通过 ``call_soon_threadsafe``
  切回事件循环，且要节流 —— 否则上千个文件的进度事件会把 UI 淹掉。
* **取消 / 暂停**：取消要能在耗时循环里及时生效；暂停只是不再派发新文件，
  已在跑的文件跑完即可，不能半途丢弃产物。
* **失败隔离**：单个坏文件绝不能拖垮整批任务，必须把错误归因到具体文件。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..actions import (
    ActionCancelled,
    ActionContext,
    ActionError,
    get_action,
    release_output_path,
    resolve_output_path,
)
from ..actions.base import TaskResult
from ..storage.db import get_db

# 单个任务的进度事件最小间隔，避免刷爆 UI
_PROGRESS_THROTTLE_SEC = 0.08
# 内存里保留的已完成任务数（更早的从 SQLite 查）
_MAX_INMEMORY_JOBS = 60
# 单个文件的最长处理时间。超过说明卡死了（常见于滤镜/超大图），必须放弃
_TASK_TIMEOUT_SEC = 600.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskState:
    id: str
    job_id: str
    file_path: str
    file_name: str
    index: int
    status: str = "queued"
    progress: float = 0.0
    duration_ms: int | None = None
    output_path: str | None = None
    error: str | None = None
    message: str = ""
    #: 聚合动作的整批输入文件。普通动作为 None，表示"只处理 file_path 这一个"
    input_files: list[str] | None = None
    _last_emit: float = field(default=0.0, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "jobId": self.job_id,
            "filePath": self.file_path,
            "fileName": self.file_name,
            "status": self.status,
            "progress": round(self.progress, 1),
            "durationMs": self.duration_ms,
            "outputPath": self.output_path,
            "error": self.error,
            "message": self.message,
        }


@dataclass
class JobState:
    id: str
    action: str
    action_label: str
    output_dir: str
    params: dict[str, Any]
    tasks: list[TaskState]
    status: str = "queued"
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    concurrency: int = 4
    suffix: str = ""
    conflict_policy: str = "rename"
    preserve_tree: bool = False
    root_dir: str | None = None

    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    resume_event: asyncio.Event | None = field(default=None, repr=False)

    @property
    def completed(self) -> int:
        return sum(1 for t in self.tasks if t.status in {"succeeded", "skipped"})

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tasks if t.status == "failed")

    @property
    def progress(self) -> float:
        if not self.tasks:
            return 0.0
        total = 0.0
        for task in self.tasks:
            if task.status in {"succeeded", "skipped", "failed", "cancelled"}:
                total += 100.0
            else:
                total += task.progress
        return round(total / len(self.tasks), 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "actionLabel": self.action_label,
            "status": self.status,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "totalTasks": len(self.tasks),
            "completedTasks": self.completed,
            "failedTasks": self.failed,
            "progress": self.progress,
            "outputDir": self.output_dir,
            "error": self.error,
            "concurrency": self.concurrency,
        }


@dataclass
class JobRequest:
    action: str
    files: list[str]
    output_dir: str
    params: dict[str, Any] = field(default_factory=dict)
    concurrency: int = 4
    suffix: str = ""
    conflict_policy: str = "rename"
    preserve_tree: bool = False
    root_dir: str | None = None


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._order: deque[str] = deque()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._pool: ThreadPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running: dict[str, asyncio.Task[None]] = {}
        self._db = get_db()

    def _ensure_pool(self) -> ThreadPoolExecutor:
        """惰性创建/重建工作线程池。

        线程池一旦 shutdown 就不能再提交任务，而 ``JobManager`` 是模块级单例
        （CLI 的 watch 模式、开发时的热重启都会在同一个进程里反复 start/shutdown），
        所以 shutdown 之后必须能重新建一个，否则第二批任务会直接报
        ``cannot schedule new futures after shutdown``。
        """
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="docforge-worker")
        return self._pool

    # ------------------------------------------------------------------ #
    # 生命周期                                                            #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ensure_pool()
        await self._db.connect()

    async def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            job.cancel_event.set()
        for task in list(self._running.values()):
            task.cancel()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        await self._db.close()

        # 顺手回收 Office 进程。
        #
        # 任务管理器是这个进程里"驱动引擎干活"的那一层，它收工的时候理应把引擎
        # 也放下。内核退出路径本来会调用 shutdown_all()，但如果只依赖那一处，
        # 就会漏掉"进程还活着、只是不再处理任务"的场景 —— 测试进程、以及将来
        # 任何内嵌复用的场景都会留下无主的 WINWORD.EXE（实测：跑完
        # test_doc_convert + test_pipeline 后残留 1 个）。
        #
        # 走惰性导入：没有 pywin32 的环境（非 Windows）不该因为这条清理路径
        # 而导入失败。
        try:
            from ..engines.com import shutdown_all

            shutdown_all()
        except Exception:  # noqa: BLE001 - 清理失败不该影响关停流程
            pass

    # ------------------------------------------------------------------ #
    # 事件订阅（供 SSE 推送）                                             #
    # ------------------------------------------------------------------ #

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4096)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def emit(self, event: dict[str, Any]) -> None:
        """向所有订阅者广播事件。队列满时丢弃最旧事件而不是阻塞 ——
        进度事件是可丢的，卡住队列反而会拖慢任务本身。"""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ------------------------------------------------------------------ #
    # 任务创建与控制                                                       #
    # ------------------------------------------------------------------ #

    def create_job(self, request: JobRequest) -> JobState:
        spec = get_action(request.action)

        files = [f for f in request.files if f]
        if not files:
            raise ActionError("没有可处理的文件")

        rejected = [f for f in files if not spec.accepts_file(f)]
        accepted = [f for f in files if spec.accepts_file(f)]

        job_id = uuid.uuid4().hex

        if spec.aggregate and accepted:
            # 聚合动作（例如 PDF 合并）：整批文件是**一个**任务。
            # 若按文件逐个建任务，用户会拿到一堆无意义的中间产物，
            # 而且并发执行还会互相争抢同一个输出文件。
            tasks = [
                TaskState(
                    id=uuid.uuid4().hex,
                    job_id=job_id,
                    file_path=accepted[0],
                    file_name=f"{len(accepted)} 个文件",
                    index=0,
                    input_files=list(accepted),
                )
            ]
        else:
            tasks = [
                TaskState(
                    id=uuid.uuid4().hex,
                    job_id=job_id,
                    file_path=path,
                    file_name=Path(path).name,
                    index=index,
                )
                for index, path in enumerate(accepted)
            ]

        job = JobState(
            id=job_id,
            action=spec.id,
            action_label=spec.label,
            output_dir=request.output_dir,
            params=dict(request.params),
            tasks=tasks,
            concurrency=max(1, min(16, request.concurrency)),
            suffix=request.suffix,
            conflict_policy=request.conflict_policy,
            preserve_tree=request.preserve_tree,
            root_dir=request.root_dir,
        )
        job.resume_event = asyncio.Event()
        job.resume_event.set()

        # 不支持的文件类型直接标为跳过，并在结果表里说明原因，而不是静默丢弃
        for path in rejected:
            tasks.append(
                TaskState(
                    id=uuid.uuid4().hex,
                    job_id=job_id,
                    file_path=path,
                    file_name=Path(path).name,
                    index=-1,
                    status="skipped",
                    progress=100.0,
                    error=f"该任务不接受 .{Path(path).suffix.lstrip('.')} 格式的文件",
                )
            )

        self._jobs[job_id] = job
        self._order.append(job_id)
        self._evict_old()

        self.emit({"type": "job.created", "job": job.to_dict()})
        return job

    def start_job(self, job: JobState) -> None:
        """把任务排入后台执行。返回后调用方可以立即响应 HTTP。"""
        self._running[job.id] = asyncio.create_task(self._run_job(job))

    async def cancel(self, job_id: str) -> JobState | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        job.cancel_event.set()
        # 唤醒可能卡在暂停状态的 worker，否则它们永远不会看到取消标志
        if job.resume_event:
            job.resume_event.set()
        return job

    async def pause(self, job_id: str) -> JobState | None:
        job = self._jobs.get(job_id)
        if job is None or job.resume_event is None:
            return None
        job.resume_event.clear()
        job.status = "paused"
        self.emit({"type": "job.updated", "job": job.to_dict()})
        return job

    async def resume(self, job_id: str) -> JobState | None:
        job = self._jobs.get(job_id)
        if job is None or job.resume_event is None:
            return None
        job.resume_event.set()
        job.status = "running"
        self.emit({"type": "job.updated", "job": job.to_dict()})
        return job

    def get(self, job_id: str) -> JobState | None:
        return self._jobs.get(job_id)

    def list_active(self) -> list[dict[str, Any]]:
        return [job.to_dict() for job in self._jobs.values() if job.status in {"queued", "running", "paused"}]

    def get_tasks(self, job_id: str) -> list[dict[str, Any]]:
        job = self._jobs.get(job_id)
        return [t.to_dict() for t in job.tasks] if job else []

    def _evict_old(self) -> None:
        while len(self._order) > _MAX_INMEMORY_JOBS:
            oldest = self._order.popleft()
            job = self._jobs.get(oldest)
            if job and job.status in {"running", "paused", "queued"}:
                self._order.append(oldest)  # 还在跑的不能丢
                break
            self._jobs.pop(oldest, None)

    # ------------------------------------------------------------------ #
    # 执行                                                                #
    # ------------------------------------------------------------------ #

    async def _run_job(self, job: JobState) -> None:
        job.status = "running"
        job.started_at = _now_iso()
        self.emit({"type": "job.updated", "job": job.to_dict()})

        semaphore = asyncio.Semaphore(job.concurrency)

        async def run_one(task: TaskState) -> None:
            if task.status == "skipped":  # 类型不匹配，创建时已标记
                return
            async with semaphore:
                if job.cancel_event.is_set():
                    task.status = "cancelled"
                    task.progress = 100.0
                    self._emit_task(job, task, force=True)
                    return
                # 暂停：在这里等，已开工的文件不受影响
                if job.resume_event is not None:
                    await job.resume_event.wait()
                if job.cancel_event.is_set():
                    task.status = "cancelled"
                    task.progress = 100.0
                    self._emit_task(job, task, force=True)
                    return
                await self._execute_task(job, task)

        try:
            await asyncio.gather(*(run_one(t) for t in job.tasks))
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        finally:
            if job.status != "cancelled":
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                elif job.failed and job.completed == 0:
                    job.status = "failed"
                    job.error = "全部文件处理失败"
                elif job.failed:
                    job.status = "succeeded"  # 部分成功仍算完成，失败项在结果表里单独标红
                    job.error = f"{job.failed} 个文件处理失败"
                else:
                    job.status = "succeeded"
            job.finished_at = _now_iso()
            self.emit({"type": "job.updated", "job": job.to_dict()})
            self.emit({"type": "job.finished", "job": job.to_dict()})
            await self._persist(job)
            self._running.pop(job.id, None)

    async def _execute_task(self, job: JobState, task: TaskState) -> None:
        spec = get_action(job.action)
        loop = asyncio.get_running_loop()
        started = time.monotonic()

        task.status = "running"
        self._emit_task(job, task, force=True)

        # 解析输出路径。动作若需要改变扩展名，可在 handler 里覆盖 ctx.output_path
        try:
            if spec.aggregate:
                # 聚合动作没有"当前文件"这个说法，输出名用动作自带的模板
                name = spec.aggregate_name.replace("{count}", str(len(task.input_files or [])))
                name = name.replace("{date}", datetime.now().strftime("%Y-%m-%d"))
                output_path = resolve_output_path(
                    str(Path(job.output_dir) / name),
                    job.output_dir,
                    suffix=job.suffix,
                    ext=spec.output_ext,
                    policy=job.conflict_policy,
                )
            else:
                output_path = resolve_output_path(
                    task.file_path,
                    job.output_dir,
                    suffix=job.suffix,
                    # 动态解析：流水线的输出类型取决于最后一步
                    ext=spec.resolve_output_ext(job.params),
                    policy=job.conflict_policy,
                    preserve_tree=job.preserve_tree,
                    root=job.root_dir,
                )
        except Exception as err:  # noqa: BLE001 - 路径问题归因到文件
            self._fail(job, task, f"无法确定输出路径：{err}")
            return

        # 失败/取消时必须释放路径预定，否则这个名字会被永久占住，
        # 用户重试时只能拿到「文件 (2).ext」这种莫名其妙的编号。
        try:
            if job.conflict_policy == "skip" and output_path.exists():
                task.status = "skipped"
                task.progress = 100.0
                task.output_path = str(output_path)
                task.duration_ms = 0
                task.message = "输出已存在，跳过"
                self._emit_task(job, task, force=True)
                return

            params = dict(job.params)
            # 隐藏字段：供 {index} 之类的模板变量使用
            params["__index"] = task.index + 1
            # 聚合动作把整批文件传进参数，handler 通过 ctx.files 读取
            if task.input_files:
                params["__files"] = task.input_files

            ctx = ActionContext(
                job_id=job.id,
                task_id=task.id,
                file_path=task.file_path,
                output_path=str(output_path),
                params=params,
                _report=lambda progress, message, t=task, j=job: self._on_progress(j, t, progress, message),
                _log=lambda message, t=task, j=job: self._on_log(j, t, message),
                _cancel=job.cancel_event,
            )

            try:
                result: TaskResult = await asyncio.wait_for(
                    loop.run_in_executor(self._ensure_pool(), spec.handler, ctx),
                    timeout=_TASK_TIMEOUT_SEC,
                )
            except ActionCancelled:
                task.status = "cancelled"
                task.progress = 100.0
                task.duration_ms = int((time.monotonic() - started) * 1000)
                self._emit_task(job, task, force=True)
                return
            except ActionError as err:
                self._fail(job, task, str(err), started)
                return
            except asyncio.TimeoutError:
                self._fail(job, task, f"处理超时（超过 {int(_TASK_TIMEOUT_SEC)} 秒）", started)
                return
            except Exception as err:  # noqa: BLE001 - 兜底：任何异常都只影响这一个文件
                self._fail(job, task, f"{type(err).__name__}: {err}", started)
                return

            # handler 可能改写了输出路径（例如按 output_format 换了扩展名）
            task.output_path = result.output_path or str(output_path)
            task.status = "skipped" if result.skipped else "succeeded"
            task.progress = 100.0
            task.message = result.message
            task.duration_ms = int((time.monotonic() - started) * 1000)
            self._emit_task(job, task, force=True)
        finally:
            # 成功时文件已落盘，释放后名字不会被抢走（下次解析会看到文件已存在）；
            # 失败、跳过或取消时释放，让重试能拿回干净的文件名。
            release_output_path(output_path)

    def _fail(self, job: JobState, task: TaskState, message: str, started: float | None = None) -> None:
        task.status = "failed"
        task.progress = 100.0
        task.error = message
        if started is not None:
            task.duration_ms = int((time.monotonic() - started) * 1000)
        self._emit_task(job, task, force=True)
        self._on_log(job, task, f"失败：{message}")

    # ------------------------------------------------------------------ #
    # 线程 → 事件循环 的回调桥接                                           #
    # ------------------------------------------------------------------ #

    def _on_progress(self, job: JobState, task: TaskState, progress: float, message: str) -> None:
        task.progress = progress
        if message:
            task.message = message
        self._emit_task(job, task)

    def _on_log(self, job: JobState, task: TaskState, message: str) -> None:
        event = {
            "type": "log",
            "jobId": job.id,
            "taskId": task.id,
            "fileName": task.file_name,
            "line": message,
        }
        self._dispatch(event)

    def _emit_task(self, job: JobState, task: TaskState, force: bool = False) -> None:
        """节流后广播任务进度。

        handler 在工作线程里跑，因此必须用 call_soon_threadsafe 切回事件循环，
        直接操作 asyncio.Queue 会引发竞态。
        """
        now = time.monotonic()
        if not force and (now - task._last_emit) < _PROGRESS_THROTTLE_SEC:
            return
        task._last_emit = now
        self._dispatch(
            {
                "type": "task.updated",
                "jobId": job.id,
                "task": task.to_dict(),
                "job": job.to_dict(),
            }
        )

    def _dispatch(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            self.emit(event)
        else:
            loop.call_soon_threadsafe(self.emit, event)

    async def _persist(self, job: JobState) -> None:
        try:
            await self._db.save_job(job.to_dict())
            await self._db.save_tasks(job.id, [t.to_dict() for t in job.tasks])
        except Exception:  # noqa: BLE001 - 历史落盘失败不该影响任务结果
            pass


_manager: JobManager | None = None


def get_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
