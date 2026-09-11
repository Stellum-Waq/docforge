"""多步骤流水线。

## 为什么需要它

办公场景里真正费时间的从来不是"转一次格式"，而是一串固定动作：
**扫描件 → 可搜索 PDF → 加水印 → 压缩 → 提取前两页**。
单独做每一步都只要点几下，但每天都要点一遍、还要自己把上一步的产物
再拖进下一步，很快就会变成"算了还是手动搞"。

流水线把这一串动作描述成一份有序的步骤清单，一次执行到底。

## 设计取舍

* **不自建执行引擎**：每一步直接调用动作注册表里已有的 handler。
  新注册一个动作就自动能进流水线，不需要在流水线里再登记一次；
  两边的行为也不可能不一致。
* **中间产物放临时目录**：用户只关心最终结果。如果中间文件写在输出目录里，
  用户会拿到一堆"看起来像结果但其实是半成品"的文件，还得自己分辨删哪些。
  临时目录在处理结束时（无论成败）整棵删掉。
* **执行前先校验整条链**：第 3 步才发现类型对不上，等于白跑前面两步
  （可能还包括昂贵的 OCR）。因此开工前就把每一步的输入/输出类型串一遍，
  不匹配就直接报错并说明是第几步。
* **拒绝聚合动作**：合并/拼接这类动作需要"一批文件"作为输入，
  而流水线是单文件逐条流动的，硬塞进去语义不清。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import (
    ActionCancelled,
    ActionContext,
    ActionError,
    ActionSpec,
    TaskResult,
    register,
    release_output_path,
    resolve_output_path,
)

#: 步骤清单在 params 里的键名。
#: 用双下划线前缀与真实动作参数区分开 —— 动作自己不会声明这种名字的参数，
#: 因此不可能撞车，也便于在日志里一眼认出"这不是动作参数"。
STEPS_KEY = "__steps"

#: 单条流水线的步骤数上限。上限存在的意义不是技术限制，而是保护：
#: 一条 20 步的流水线几乎肯定是配错了，跑起来会浪费大量时间。
MAX_STEPS = 12


@dataclass(frozen=True)
class PipelineStep:
    """流水线中的一步：一个动作 + 它的参数。"""

    action: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "params": dict(self.params)}


def parse_steps(raw: Any) -> list[PipelineStep]:
    """把外部传入的步骤清单解析成 :class:`PipelineStep`。

    只做结构校验（是不是列表、每步有没有 action）；类型链的校验在
    :func:`validate_chain` 里做，因为那需要知道源文件的扩展名。
    """
    if not isinstance(raw, list) or not raw:
        raise ActionError("流水线至少要有一个步骤")

    if len(raw) > MAX_STEPS:
        raise ActionError(f"流水线最多 {MAX_STEPS} 步（当前 {len(raw)} 步）")

    steps: list[PipelineStep] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, PipelineStep):
            steps.append(item)
            continue
        if not isinstance(item, dict):
            raise ActionError(f"第 {index} 步格式不对：应该是 {{action, params}} 对象")
        action_id = str(item.get("action") or "").strip()
        if not action_id:
            raise ActionError(f"第 {index} 步没有指定动作")
        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise ActionError(f"第 {index} 步的 params 必须是对象")
        steps.append(PipelineStep(action_id, dict(params)))

    return steps


def _step_spec(step: PipelineStep, index: int) -> ActionSpec:
    from .base import get_action

    try:
        spec = get_action(step.action)
    except ActionError:
        raise ActionError(f"第 {index} 步的动作不存在：{step.action}") from None
    if spec.id == "pipeline":
        raise ActionError(f"第 {index} 步不能是流水线本身（不允许嵌套）")
    if spec.aggregate:
        raise ActionError(
            f"第 {index} 步「{spec.label}」是聚合动作，需要一批文件作为输入，"
            "不能放进流水线"
        )
    return spec


def validate_chain(source: str, steps: list[PipelineStep]) -> list[ActionSpec]:
    """把整条链的输入/输出类型串一遍，提前发现问题。

    返回每一步的 :class:`ActionSpec`，避免执行时再查一次。

    这里刻意保守：只有动作**显式声明了** ``output_ext`` 才认为类型会变，
    否则按"同源"处理（图片水印就是这种：输入 png 输出还是 png）。
    真实扩展名仍以 handler 返回的 ``output_path`` 为准 —— 有些动作会按参数
    换扩展名（例如「图片提取文字」选了 Word 输出）。所以这只是**预检**，
    不是运行时约束。
    """
    specs: list[ActionSpec] = []
    current_ext = Path(source).suffix.lstrip(".").lower()

    for index, step in enumerate(steps, start=1):
        spec = _step_spec(step, index)

        if spec.accepts and current_ext not in spec.accepts:
            previous = "源文件" if index == 1 else f"第 {index - 1} 步的输出"
            raise ActionError(
                f"第 {index} 步「{spec.label}」不接受 .{current_ext or '未知'}："
                f"{previous}是 .{current_ext or '未知'}，而这一步需要 "
                f"{'、'.join('.' + e for e in spec.accepts)}"
            )

        specs.append(spec)
        if spec.output_ext:
            current_ext = spec.output_ext

    return specs


def describe_chain(source: str, steps: list[PipelineStep]) -> list[str]:
    """生成人类可读的步骤说明，供 CLI / 界面在执行前展示。"""
    specs = validate_chain(source, steps)
    lines: list[str] = []
    ext = Path(source).suffix.lstrip(".").lower()
    for index, (step, spec) in enumerate(zip(steps, specs), start=1):
        out_ext = spec.output_ext or ext
        lines.append(f"{index}. {spec.label}  .{ext or '?'} → .{out_ext}")
        ext = out_ext
    return lines


def run_chain(
    source: str,
    steps: list[PipelineStep],
    output_dir: str,
    *,
    final_target: str | None = None,
    suffix: str = "",
    conflict: str = "rename",
    log: Any = None,
    cancel: Any = None,
    report: Any = None,
) -> TaskResult:
    """对**一个文件**执行整条流水线，返回最终产物。

    :param final_target: 最后一步的落盘路径。由调用方（任务队列）给出时，
        **必须原样使用** —— 队列已经按重名策略解析过这个名字，这里再解析一次
        会绕过重名保护：批处理里两个同名文件会算出一模一样的输出名，
        后写的静默覆盖先写的。
    """
    specs = validate_chain(source, steps)
    origin = Path(source)

    def emit(message: str) -> None:
        if log:
            log(message)

    work_root = Path(tempfile.mkdtemp(prefix="docforge-pipeline-"))
    release_target: str | None = None

    try:
        current = str(origin)
        total = len(steps)

        for index, (step, spec) in enumerate(zip(steps, specs), start=1):
            if cancel is not None and cancel.is_set():
                raise ActionCancelled("任务已被用户取消")

            last = index == total
            if last and final_target:
                target = Path(final_target)
                target.parent.mkdir(parents=True, exist_ok=True)
            elif last:
                target = resolve_output_path(
                    current,
                    output_dir,
                    suffix=suffix,
                    ext=spec.output_ext,
                    policy=conflict,
                )
                release_target = str(target)
            else:
                # 中间产物用固定名字放进临时目录：那里只有这一条流水线在用，
                # 不可能重名，也就不需要走重名消解
                ext = spec.output_ext or Path(current).suffix.lstrip(".").lower()
                target = work_root / f"step{index:02d}.{ext}" if ext else work_root / f"step{index:02d}"

            emit(f"[{index}/{total}] {spec.label}")

            ctx = ActionContext(
                job_id="pipeline",
                task_id=f"step{index}",
                file_path=current,
                output_path=str(target),
                params=dict(step.params),
                _report=(
                    (lambda progress, message, i=index: report((i - 1 + progress / 100) * 100 / total, message))
                    if report
                    else None
                ),
                _log=log,
                _cancel=cancel,
            )

            try:
                result = spec.handler(ctx)
            except ActionCancelled:
                # 取消不是失败，原样抛出去让队列标记为"已取消"
                raise
            except ActionError as err:
                # 统一带上步序号：动作自己的报错（例如"页码范围没有匹配到任何页面"）
                # 单看是清楚的，但放进 5 步流水线里就完全不知道是哪一步出的问题
                raise ActionError(f"第 {index} 步「{spec.label}」失败：{err}") from err
            except Exception as err:  # noqa: BLE001 - 归因到具体步骤
                raise ActionError(f"第 {index} 步「{spec.label}」失败：{err}") from err

            if result.skipped:
                raise ActionError(f"第 {index} 步「{spec.label}」跳过了该文件：{result.message or '无原因说明'}")

            produced = result.output_path or str(target)
            if not Path(produced).exists():
                raise ActionError(f"第 {index} 步「{spec.label}」没有产出文件")

            current = produced

        if report:
            report(100.0, "完成")

        final = Path(current)
        steps_text = " → ".join(s.label for s in specs)
        return TaskResult(output_path=str(final), message=f"{steps_text}")
    finally:
        # 中间产物一律清理，失败路径也一样 —— 否则临时目录会越积越多
        shutil.rmtree(work_root, ignore_errors=True)
        if release_target is not None and not Path(release_target).exists():
            release_output_path(release_target)


def _pipeline_output_ext(params: dict[str, Any]) -> str | None:
    """流水线的输出扩展名 = 最后一步的 ``output_ext``。

    任务队列要在开工前就定好输出文件名，所以这个值必须能**纯靠参数**算出来
    （不能等跑完再问）。算不出来就返回 None，表示"与输入同源"。
    """
    try:
        steps = parse_steps(params.get(STEPS_KEY))
    except ActionError:
        return None
    last = steps[-1]
    if last.action == "pipeline":
        return None
    from .base import get_action

    try:
        return get_action(last.action).output_ext
    except ActionError:
        return None


def _pipeline_handler(ctx: ActionContext) -> TaskResult:
    steps = parse_steps(ctx.params.get(STEPS_KEY))
    # parse_steps 里已经做过结构校验，这里再过一遍类型链，让错误在执行前暴露
    validate_chain(ctx.file_path, steps)

    return run_chain(
        ctx.file_path,
        steps,
        str(Path(ctx.output_path).parent),
        # 队列已经按重名策略解析好名字了，最后一步必须原样落到这个路径上
        final_target=ctx.output_path,
        log=ctx.log,
        cancel=ctx._cancel,
        report=ctx.report,
    )


register(
    ActionSpec(
        id="pipeline",
        label="多步骤流水线",
        domain="pipeline",
        handler=_pipeline_handler,
        # 接受任意文件：具体限制由第一步决定，提前在 accepts 里写死反而会
        # 把"第一步可以处理 pdf"的文件挡在外面
        accepts=(),
        output_ext=None,
        output_ext_fn=_pipeline_output_ext,
        description="按顺序执行多个动作，上一步的输出直接作为下一步的输入",
        params_schema={},
    )
)
