"""动作（Action）抽象层。

设计要点
--------
所有功能 —— 无论是"图片加水印""PDF 转 Word"还是"图片转 Excel" —— 都统一表达为一个
**动作**：接收一批文件 + 一组参数，对每个文件产出一个输出文件。

这样做的好处：
  1. 任务队列、进度上报、失败重试、批量结果表只需实现一次，所有功能免费获得。
  2. UI 可以读取 ``params_schema`` 自动生成参数表单，新增功能不用改前端。
  3. 新增能力 = 新增一个 ``@register`` 的函数，没有框架性改动。

约定：
  * ``handler`` 必须是纯函数式的：只通过 ``ctx`` 读输入、写输出，不保留跨任务状态。
  * 单文件失败必须抛 :class:`ActionError`，由队列层捕获并归因到该文件，
    绝不能因为一个坏文件让整批任务失败。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

class ActionError(Exception):
    """动作执行中的可预期失败（文件损坏、参数非法等），会被归因到单个文件。"""


@dataclass
class TaskResult:
    """单个文件的处理结果。"""

    output_path: str | None = None
    skipped: bool = False
    message: str = ""


@dataclass
class ActionContext:
    """一次「单文件处理」的上下文。handler 通过它与队列层通信。"""

    job_id: str
    task_id: str
    file_path: str
    output_path: str
    params: dict[str, Any]
    _report: Callable[[float, str], None] | None = None
    _log: Callable[[str], None] | None = None
    _cancel: threading.Event | None = None
    _progress: float = 0.0

    # -- 与队列层通信 -------------------------------------------------- #

    def report(self, progress: float, message: str = "") -> None:
        """上报进度（0-100）。频繁调用是安全的，队列层会自行节流。"""
        self._progress = max(0.0, min(100.0, progress))
        if self._report:
            self._report(self._progress, message)

    def log(self, message: str) -> None:
        """写一条用户可见的日志。"""
        if self._log:
            self._log(message)

    @property
    def progress(self) -> float:
        return self._progress

    def raise_if_cancelled(self) -> None:
        """在耗时循环里周期调用，让"取消任务"能真正及时生效。"""
        if self._cancel and self._cancel.is_set():
            raise ActionCancelled("任务已被用户取消")

    # -- 参数读取辅助 -------------------------------------------------- #

    def param(self, key: str, default: Any = None) -> Any:
        value = self.params.get(key, default)
        return default if value is None else value

    def float_param(self, key: str, default: float) -> float:
        try:
            return float(self.params.get(key, default))
        except (TypeError, ValueError):
            return default

    def int_param(self, key: str, default: int) -> int:
        try:
            return int(self.params.get(key, default))
        except (TypeError, ValueError):
            return default

    def bool_param(self, key: str, default: bool) -> bool:
        value = self.params.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def str_param(self, key: str, default: str = "") -> str:
        value = self.params.get(key, default)
        return default if value is None else str(value)

    # -- 聚合动作的输入文件列表 ---------------------------------------- #

    @property
    def files(self) -> list[str]:
        """本任务的**全部**输入文件。

        普通动作一次只处理一个文件，这里返回只含当前文件的单元素列表；
        聚合动作（例如 PDF 合并）则返回整批文件。
        让两种动作用同一个属性读取输入，handler 里就不需要区分两种情况。
        """
        files = self.params.get("__files")
        if isinstance(files, list) and files:
            return [str(item) for item in files]
        return [self.file_path]


class ActionCancelled(Exception):
    """任务被取消。与 ActionError 区分开，因为它不是失败。"""


class ProgressFn(Protocol):
    def __call__(self, progress: float, message: str) -> None: ...


ActionHandler = Callable[[ActionContext], TaskResult]


@dataclass(frozen=True)
class ActionSpec:
    """一个动作的元数据 + 实现。"""

    id: str
    label: str
    domain: str
    handler: ActionHandler
    accepts: tuple[str, ...] = ()
    output_ext: str | None = None
    description: str = ""
    params_schema: dict[str, Any] = field(default_factory=dict)
    #: 聚合动作：把整批文件当作**一个**输入，只产出一个结果。
    #: 例如「PDF 合并」——它天然是多文件进、单文件出，
    #: 硬套"每个文件一个输出"的模型会让用户拿到一堆无意义的中间产物。
    aggregate: bool = False
    #: 聚合动作的输出文件名模板，可用 {stem} {count} {date} 等变量
    aggregate_name: str = "合并结果"
    #: 输出扩展名按参数动态决定时的钩子。
    #:
    #: 绝大多数动作的输出类型是固定的，写 ``output_ext`` 就够了。但**流水线**
    #: 不是：它的输出类型取决于最后一步是什么。若不动态计算，队列会在开工前
    #: 就按源文件扩展名定好输出名 —— 一条以「文档转 PDF」收尾的流水线会产出
    #: 一个内容是 PDF、后缀却是 .docx 的文件，用户双击打不开，还很难想明白
    #: 是哪里出的问题。
    output_ext_fn: Callable[[dict[str, Any]], str | None] | None = None

    def accepts_file(self, path: str) -> bool:
        if not self.accepts:
            return True
        ext = Path(path).suffix.lstrip(".").lower()
        return ext in self.accepts

    def resolve_output_ext(self, params: dict[str, Any]) -> str | None:
        """计算本次任务实际的输出扩展名（动态钩子失败时退回静态值）。"""
        if self.output_ext_fn is not None:
            try:
                return self.output_ext_fn(params) or self.output_ext
            except Exception:  # noqa: BLE001 - 钩子只是优化输出名，不该让任务失败
                return self.output_ext
        return self.output_ext


_REGISTRY: dict[str, ActionSpec] = {}


def register(spec: ActionSpec) -> ActionSpec:
    if spec.id in _REGISTRY:
        raise ValueError(f"动作 id 重复注册：{spec.id}")
    _REGISTRY[spec.id] = spec
    return spec


def get_action(action_id: str) -> ActionSpec:
    try:
        return _REGISTRY[action_id]
    except KeyError:
        raise ActionError(f"未知的动作类型：{action_id}") from None


def list_actions() -> list[ActionSpec]:
    return sorted(_REGISTRY.values(), key=lambda s: (s.domain, s.id))


# --------------------------------------------------------------------------- #
# 输出路径与原子写入                                                            #
# --------------------------------------------------------------------------- #

CONFLICT_POLICIES = ("rename", "overwrite", "skip")

#: 已被本进程"预定"的输出路径。
#:
#: 为什么需要它：批量任务并发处理时，多个同名文件（例如从不同文件夹拖入的
#: 多个「扫描件.pdf」）会各自独立地计算输出路径。若只检查文件系统是否存在，
#: 两个任务可能**同时**算出同一个名字，于是后写的静默覆盖先写的 ——
#: 用户会莫名其妙少文件，而且没有任何报错。这里把"解析"与"占用"合并成一个
#: 原子操作，从根上消除这个竞态。
_RESERVED: set[str] = set()
_RESERVE_LOCK = threading.Lock()


def release_output_path(path: str | Path) -> None:
    """释放输出路径预定。

    任务失败或被取消时必须调用，否则一个失败的任务会永久"占着"这个名字，
    用户重试时只能拿到 ``文件 (2).ext`` 这种莫名其妙的编号。
    """
    with _RESERVE_LOCK:
        _RESERVED.discard(str(Path(path)))


def _reserve(candidate: Path) -> bool:
    """尝试占用一个路径。返回 False 表示已被其他任务预定。"""
    key = str(candidate)
    with _RESERVE_LOCK:
        if key in _RESERVED:
            return False
        _RESERVED.add(key)
        return True


def resolve_output_path(
    src: str,
    output_dir: str,
    *,
    suffix: str = "",
    ext: str | None = None,
    policy: str = "rename",
    preserve_tree: bool = False,
    root: str | None = None,
) -> Path:
    """计算输出路径并按冲突策略消解重名，**同时占用该路径**。

    注意这个函数有副作用：返回的路径已被登记为"本任务所有"。任务结束后
    无论成功还是失败都应调用 :func:`release_output_path`（成功时文件已存在，
    释放后不会被其他任务抢走；失败时释放让重试能拿回干净的名字）。

    几个容易踩的点：
      * ``ext`` 为 None 时保持原扩展名
      * ``policy='rename'`` 用 ``名字 (2).ext`` 的 Windows 习惯，而不是加时间戳
      * ``preserve_tree`` 保留源目录结构，批量处理嵌套目录时不会互相覆盖
      * **并发安全**：仅靠 ``Path.exists()`` 判断是不够的，见 ``_RESERVED`` 的说明
    """
    source = Path(src)
    out_root = Path(output_dir)

    if preserve_tree and root:
        try:
            relative_parent = source.parent.relative_to(root)
            target_dir = out_root / relative_parent
        except ValueError:
            target_dir = out_root
    else:
        target_dir = out_root

    target_dir.mkdir(parents=True, exist_ok=True)

    final_ext = f".{(ext or source.suffix.lstrip('.')).lower().lstrip('.')}"
    stem = f"{source.stem}{suffix}"
    candidate = target_dir / f"{stem}{final_ext}"

    if policy == "overwrite":
        _reserve(candidate)
        return candidate

    if not candidate.exists() and _reserve(candidate):
        return candidate

    if policy == "skip":
        # 跳过策略下名字被占就说明确实已有产物，原样返回让上层标记为跳过
        return candidate

    # rename：找到第一个既不存在也没被预定的序号
    for index in range(2, 10_000):
        alternative = target_dir / f"{stem} ({index}){final_ext}"
        if alternative.exists():
            continue
        if _reserve(alternative):
            return alternative
    raise ActionError(f"无法为 {source.name} 找到可用的输出文件名")


def atomic_write(target: Path, writer: Callable[[Path], None]) -> None:
    """先写临时文件再改名，避免中途失败留下半成品文件。

    用户看到输出目录里出现一个文件，就意味着它一定是完整的 ——
    这对批量处理很重要，否则半成品混在结果里很难分辨。
    """
    tmp = target.with_name(f".{target.name}.docforge-tmp{target.suffix}")
    try:
        writer(tmp)
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise ActionError("生成的结果为空文件")
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
