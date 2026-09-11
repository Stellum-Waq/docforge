"""命令行接口。

## 为什么要有 CLI

桌面应用解决的是"我偶尔转几个文件"，但办公场景里还有另一类需求：
**"每天把收件箱里的附件批量转一遍"**。这种活应该交给脚本和计划任务，
而不是让人每天点一遍界面。CLI 让同一套内核能被自动化调度复用。

## 设计取舍

* **直接复用内核的任务队列**，不另写一套处理逻辑。并发、进度、失败隔离、
  输出重名策略全都和界面里完全一致 —— 两边行为不一样是最难排查的问题。
* **默认命令保持兼容**：``python -m docforge``（不带子命令）仍然是启动内核服务，
  因为 Electron 就是靠这条命令拉起内核的。子命令只在显式给出时才生效。
* **不引入 watchdog**：监听文件夹用轮询实现。轮询在网络盘与容器挂载点上
  比基于事件的方案可靠得多（那些方案常常收不到事件），代价是最多延迟一个轮询周期。

用法示例：
    docforge actions
    docforge run image.watermark ./photos -o ./out --param mode=text --param text=机密
    docforge run image.watermark ./photos --preset 公章
    docforge run pdf.searchable scan.pdf -o ./out --json
    docforge watch ./收件箱 --action doc.to_pdf -o ./转换结果
    docforge pipeline scan.pdf --step pdf.searchable --step pdf.watermark:text=机密 --step pdf.compress
    docforge presets save image.watermark 公章 --param text=公司公章 --param opacity=0.3
    docforge presets list
    docforge selfcheck
    docforge serve
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

#: 需要走 CLI（而不是启动内核服务）的子命令
COMMANDS = ("actions", "run", "watch", "pipeline", "presets", "selfcheck", "serve", "version")


# --------------------------------------------------------------------------- #
# 参数解析辅助                                                                  #
# --------------------------------------------------------------------------- #

def configure_stdio() -> None:
    """让命令行输出在 Windows 控制台上不会因为编码问题直接崩掉。

    Windows 控制台默认是 GBK（cp936），而 CLI 会打印 ``✓`` ``✗`` ``→`` 这类
    符号 —— GBK 编不出来，``print`` 直接抛 ``UnicodeEncodeError``，
    **整个命令崩掉**。用户在 cmd 里手敲 ``docforge run ...`` 时不会先设置
    ``PYTHONIOENCODING``，所以这个坑一定会踩到。

    两步处理：

    1. 把控制台输出代码页切到 UTF-8（65001），中文与符号都能正常显示；
    2. 所有输出流一律设成 ``errors="replace"`` —— 万一某台机器上第 1 步
       不生效，最差也只是把个别符号显示成 ``?``，而不是让命令中途崩掉。
       批量处理跑到一半崩掉、只留下半批产物，比符号显示不全糟糕得多。
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001 - 没有真实控制台时忽略
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 已被重定向/包装过的流
            pass


def parse_param(raw: str) -> tuple[str, Any]:
    """把 ``key=value`` 解析成 (键, 值)，并尽量还原成合适的类型。

    命令行里所有东西都是字符串，而动作的参数有布尔/数字/字符串之分。
    这里做一次"看起来像什么就转成什么"的推断，避免用户为了传一个 ``true``
    还要去查 JSON 语法。

    键和值两侧的空白都会被去掉（``--param " text = 机密 "`` 与
    ``--param "text=机密"`` 等价）；值内部的空格原样保留。
    """
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"参数格式应为 key=value，收到：{raw}")
    key, _, value = raw.partition("=")
    key = key.strip()
    value = value.strip()
    # 空键必须报错：否则参数被静默忽略，用户只会看到"没生效"却找不到原因
    if not key:
        raise argparse.ArgumentTypeError(f"参数名不能为空：{raw}")

    lowered = value.strip().lower()
    if lowered in ("true", "yes", "on"):
        return key, True
    if lowered in ("false", "no", "off"):
        return key, False
    # 保留以 0 开头的编号（如 "007"）为字符串，避免被转成 7
    if not (len(value) > 1 and value.startswith("0") and value[1].isdigit()):
        try:
            return key, int(value)
        except ValueError:
            pass
        try:
            return key, float(value)
        except ValueError:
            pass
    return key, value


def collect_files(inputs: list[str], recursive: bool = True) -> list[str]:
    """把命令行给出的文件/目录展开成文件列表。"""
    files: list[str] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            files.extend(str(p) for p in sorted(path.glob(pattern)) if p.is_file())
        elif path.is_file():
            files.append(str(path))
        else:
            print(f"警告：路径不存在，已跳过 {item}", file=sys.stderr)
    return files


# --------------------------------------------------------------------------- #
# 命令实现                                                                      #
# --------------------------------------------------------------------------- #

def cmd_actions(args: argparse.Namespace) -> int:
    from .actions import list_actions

    specs = list_actions()
    if args.json:
        print(json.dumps(
            [
                {
                    "id": s.id,
                    "label": s.label,
                    "domain": s.domain,
                    "accepts": list(s.accepts),
                    "outputExt": s.output_ext,
                    "aggregate": s.aggregate,
                    "description": s.description,
                    "params": {
                        key: {"type": prop.get("type"), "default": prop.get("default")}
                        for key, prop in (s.params_schema.get("properties") or {}).items()
                    },
                }
                for s in specs
            ],
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    by_domain: dict[str, list] = {}
    for spec in specs:
        by_domain.setdefault(spec.domain, []).append(spec)

    for domain, items in by_domain.items():
        print(f"\n[{domain}]")
        for spec in items:
            mark = "（聚合）" if spec.aggregate else ""
            print(f"  {spec.id:<20} {spec.label}{mark}")
            print(f"  {'':<20} {spec.description}")
            accepted = ", ".join(f".{e}" for e in spec.accepts) if spec.accepts else "任意文件"
            print(f"  {'':<20} 接受：{accepted}    输出：.{spec.output_ext or '同源'}")
            props = spec.params_schema.get("properties") or {}
            if props:
                summary = ", ".join(f"{k}={v.get('default')!r}" for k, v in list(props.items())[:6])
                print(f"  {'':<20} 参数：{summary}{' …' if len(props) > 6 else ''}")
            print()
    return 0


async def _execute_job(
    action_id: str,
    files: list[str],
    output_dir: str,
    params: dict[str, Any],
    *,
    suffix: str,
    concurrency: int,
    conflict: str,
    quiet: bool,
    json_output: bool = False,
) -> int:
    """在**已经启动**的任务管理器上跑一次任务。

    与 :func:`_run_job` 分开是因为 watch 模式要在同一个事件循环里连续跑很多批：
    每批都 ``asyncio.run()`` 会反复新建/销毁事件循环和线程池，第一批之后
    线程池就处于 shutdown 状态，第二批必然失败。
    """
    from .jobs import JobRequest, get_manager
    from .actions import ActionError

    manager = get_manager()

    try:
        job = manager.create_job(
            JobRequest(
                action=action_id,
                files=files,
                output_dir=output_dir,
                params=params,
                concurrency=concurrency,
                suffix=suffix,
                conflict_policy=conflict,
            )
        )
    except ActionError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2

    # 订阅事件流，把进度打到终端。CLI 场景下不需要花哨的刷新，
    # 一行一个文件的结果最实用（也能直接重定向到日志文件）。
    queue = manager.subscribe()

    async def report() -> None:
        while True:
            event = await queue.get()
            if event.get("type") == "task.updated":
                task = event["task"]
                if task["status"] in ("succeeded", "failed", "skipped", "cancelled"):
                    icon = {"succeeded": "✓", "failed": "✗", "skipped": "-", "cancelled": "·"}[task["status"]]
                    detail = task.get("error") or task.get("message") or ""
                    line = f"  {icon} {task['fileName']}"
                    if detail:
                        line += f"  {detail}"
                    print(line, flush=True)
            elif event.get("type") == "log" and not quiet:
                print(f"    · {event.get('line')}", flush=True)

    reporter = asyncio.create_task(report())
    try:
        manager.start_job(job)

        while job.status in ("queued", "running", "paused"):
            await asyncio.sleep(0.15)
    finally:
        reporter.cancel()
        try:
            await reporter
        except asyncio.CancelledError:
            pass
        manager.unsubscribe(queue)

    print()
    print(f"结果：{job.status}  完成 {job.completed}/{len(job.tasks)}  失败 {job.failed}")

    if json_output:
        print(json.dumps(job.to_dict(), ensure_ascii=False, indent=2))

    # 部分失败也返回非 0，便于脚本判断（0 全部成功 / 1 有失败 / 2 参数错误）
    return 0 if job.status == "succeeded" and job.failed == 0 else 1


async def _run_job(
    action_id: str,
    files: list[str],
    output_dir: str,
    params: dict[str, Any],
    *,
    suffix: str,
    concurrency: int,
    conflict: str,
    quiet: bool,
    json_output: bool = False,
) -> int:
    """单次任务的完整生命周期：启动管理器 → 跑任务 → 收尾。"""
    from .jobs import get_manager

    manager = get_manager()
    await manager.start()
    try:
        return await _execute_job(
            action_id,
            files,
            output_dir,
            params,
            suffix=suffix,
            concurrency=concurrency,
            conflict=conflict,
            quiet=quiet,
            json_output=json_output,
        )
    finally:
        await manager.shutdown()


def _load_preset_params(action_id: str, name: str) -> dict[str, Any]:
    """读取预设里的参数字典。找不到就抛 ValueError（由调用方转成退出码 2）。"""
    async def load() -> dict[str, Any] | None:
        from .storage.db import get_db

        db = get_db()
        await db.connect()
        try:
            return await db.find_preset(action_id, name)
        finally:
            await db.close()

    preset = asyncio.run(load())
    if preset is None:
        raise ValueError(f"没有找到预设「{name}」（用 `docforge presets list` 查看已有预设）")
    return dict(preset["params"])


def _build_params(spec_id: str, preset: str | None, raw_params: list[str] | None) -> dict[str, Any]:
    """合并「预设 + 命令行参数」，命令行优先。

    优先级刻意设计成命令行覆盖预设：这样用户可以把 90% 固定不变的参数
    存成预设，只在每次运行时临时改那一两个会变的（比如水印文案）。
    反过来的话预设会变成"没法微调的模板"，实际用起来会很别扭。
    """
    params: dict[str, Any] = {}
    if preset:
        params.update(_load_preset_params(spec_id, preset))

    for raw in raw_params or []:
        key, value = parse_param(raw)
        params[key] = value

    return params


def cmd_run(args: argparse.Namespace) -> int:
    from .actions import ActionError, get_action

    try:
        spec = get_action(args.action)
    except ActionError as err:
        print(f"错误：{err}", file=sys.stderr)
        print("可用动作请用 `docforge actions` 查看", file=sys.stderr)
        return 2

    files = collect_files(args.inputs)
    if not files:
        print("错误：没有找到可处理的文件", file=sys.stderr)
        return 2

    if spec.accepts:
        accepted = [f for f in files if spec.accepts_file(f)]
        rejected = len(files) - len(accepted)
        if rejected:
            print(f"提示：{rejected} 个文件格式不匹配，将被跳过", file=sys.stderr)
        files = accepted
        if not files:
            print(f"错误：{spec.label} 不接受这些文件类型（需要 {', '.join(spec.accepts)}）", file=sys.stderr)
            return 2

    if spec.aggregate and len(files) < 2:
        print(f"错误：{spec.label} 是聚合动作，至少需要 2 个文件", file=sys.stderr)
        return 2

    params: dict[str, Any] = {}
    try:
        params = _build_params(spec.id, args.preset, args.param)
    except ValueError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2
    except argparse.ArgumentTypeError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2

    output_dir = args.out or str(Path.cwd() / "docforge-output")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    print(f"{spec.label}：{len(files)} 个文件 → {output_dir}")
    return asyncio.run(
        _run_job(
            spec.id,
            files,
            output_dir,
            params,
            suffix=args.suffix or "",
            concurrency=args.concurrency,
            conflict=args.conflict,
            quiet=args.quiet,
            json_output=args.json_output,
        )
    )


def cmd_selfcheck(args: argparse.Namespace) -> int:
    from .api.routes import self_check

    report = asyncio.run(self_check())
    status_mark = {"ok": "✓", "warn": "!", "fail": "✗"}

    for item in report.items:
        print(f"  {status_mark[item.status]} {item.label:<22} {item.detail}")
        if item.impact:
            print(f"      └─ 影响：{item.impact}")

    print()
    print(
        f"就绪：{'是' if report.ready else '否'}  通过 {report.ok_count} / 降级 {report.warn_count} / 不可用 {report.fail_count}"
    )

    if args.json:
        print(json.dumps(report.model_dump(by_alias=True), ensure_ascii=False, indent=2))

    return 0 if report.ready else 1


async def _watch_loop(
    spec: Any,
    watch_dir: Path,
    output_dir: Path,
    params: dict[str, Any],
    exts: set[str],
    args: argparse.Namespace,
    stop_event: asyncio.Event | None = None,
) -> int:
    """监听主循环。

    整个会话共用一个事件循环和一个任务管理器 —— watch 是常驻进程，
    每批任务都重建事件循环会让线程池进入 shutdown 状态而无法恢复。

    ``stop_event`` 供测试与内嵌调用提前优雅退出（命令行下用 Ctrl+C）。
    """
    from .jobs import get_manager

    manager = get_manager()
    await manager.start()

    # 已处理过的文件记录在内存里。用 (路径, 大小, 修改时间) 做键，
    # 这样"同名但被重新写入的文件"会被视为新文件。
    seen: set[tuple[str, int, float]] = set()

    async def run_batch(fresh: list[str], stamp: str) -> None:
        print(f"[{stamp}] 发现 {len(fresh)} 个新文件", flush=True)
        # 一个批次一个任务：合并等聚合动作才能正常工作
        code = await _execute_job(
            spec.id,
            fresh,
            str(output_dir),
            params,
            suffix=args.suffix or "",
            concurrency=args.concurrency,
            conflict=args.conflict,
            quiet=True,
        )
        if code not in (0, 1):
            print(f"[{stamp}] 批次处理返回码 {code}", flush=True)

        # 聚合动作会把整批合成一个产物，处理完就清空记录，
        # 否则后续新文件会与旧文件一起被重复合并
        if spec.aggregate:
            seen.clear()

    try:
        while not (stop_event is not None and stop_event.is_set()):
            # 已处理目录自身要被排除，否则产物会被再次处理，形成死循环
            candidates = [
                p
                for p in sorted(watch_dir.rglob("*"))
                if p.is_file()
                and p.suffix.lstrip(".").lower() in exts
                and output_dir not in p.parents
            ]

            fresh: list[str] = []
            for path in candidates:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                key = (str(path), stat.st_size, stat.st_mtime)
                if key in seen:
                    continue
                seen.add(key)
                fresh.append(str(path))

            if fresh:
                await run_batch(fresh, time.strftime("%H:%M:%S"))

            if stop_event is not None:
                # 可被提前唤醒的等待，测试里不用干等一个完整轮询周期
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=max(0.5, args.interval))
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(max(0.5, args.interval))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n已停止监听")
        return 0
    finally:
        await manager.shutdown()

    # 通过 stop_event 优雅退出（测试或内嵌调用）
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """监听文件夹：出现新文件就自动处理。

    实现用的是轮询而不是文件系统事件 —— 网络盘与容器挂载点上基于事件的
    方案经常收不到通知，而"漏处理"比"晚两秒处理"严重得多。
    """
    from .actions import ActionError, get_action

    try:
        spec = get_action(args.action)
    except ActionError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2

    watch_dir = Path(args.directory)
    if not watch_dir.is_dir():
        print(f"错误：目录不存在 {watch_dir}", file=sys.stderr)
        return 2

    output_dir = Path(args.out) if args.out else watch_dir / "已处理"
    output_dir.mkdir(parents=True, exist_ok=True)

    params: dict[str, Any] = {}
    try:
        params = _build_params(spec.id, args.preset, args.param)
    except ValueError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2
    except argparse.ArgumentTypeError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2

    patterns = [p.strip().lower().lstrip(".") for p in (args.exts or "").split(",") if p.strip()]
    exts = set(patterns) if patterns else set(spec.accepts)

    print(f"监听 {watch_dir}")
    print(f"  动作：{spec.label}")
    print(f"  类型：{', '.join('.' + e for e in sorted(exts))}")
    print(f"  输出：{output_dir}")
    print(f"  间隔：{args.interval} 秒    按 Ctrl+C 停止\n", flush=True)

    try:
        return asyncio.run(_watch_loop(spec, watch_dir, output_dir, params, exts, args))
    except KeyboardInterrupt:
        print("\n已停止监听")
        return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """显式启动内核服务。与不带子命令的默认行为一致。"""
    from .__main__ import serve_main

    return serve_main()


def cmd_version(args: argparse.Namespace) -> int:
    _print_version()
    return 0


# --------------------------------------------------------------------------- #
# 流水线                                                                        #
# --------------------------------------------------------------------------- #

def parse_step_spec(raw: str) -> dict[str, Any]:
    """解析 ``--step`` 的值：``动作[:k=v,k=v]``。

    逗号分隔的写法读起来最顺（``pdf.watermark:text=机密,opacity=0.3``）。
    但值里本身带逗号时会有歧义，所以还提供 ``--step-param N:k=v``
    作为无歧义的写法 —— 两种都支持，用户按需要选。
    """
    action_id, _, params_text = raw.partition(":")
    action_id = action_id.strip()
    if not action_id:
        raise argparse.ArgumentTypeError(f"--step 缺少动作名：{raw}")

    params: dict[str, Any] = {}
    for chunk in params_text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, value = parse_param(chunk)
        params[key] = value

    return {"action": action_id, "params": params}


def _build_steps(step_specs: list[str], step_params: list[str] | None) -> list[dict[str, Any]]:
    if not step_specs:
        raise argparse.ArgumentTypeError("流水线至少需要一个 --step")

    steps = [parse_step_spec(raw) for raw in step_specs]

    for raw in step_params or []:
        index_text, _, chunk = raw.partition(":")
        try:
            index = int(index_text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--step-param 的格式应为 步序号:key=value，收到：{raw}"
            ) from None
        if not 1 <= index <= len(steps):
            raise argparse.ArgumentTypeError(
                f"--step-param 指向第 {index} 步，但流水线只有 {len(steps)} 步"
            )
        key, value = parse_param(chunk)
        steps[index - 1]["params"][key] = value

    return steps


def cmd_pipeline(args: argparse.Namespace) -> int:
    """对一批文件执行同一条多步骤流水线。"""
    from .actions import ActionError, PipelineStep, describe_chain, validate_chain

    files = collect_files(args.inputs)
    if not files:
        print("错误：没有找到可处理的文件", file=sys.stderr)
        return 2

    try:
        raw_steps = _build_steps(args.step, args.step_params)
    except argparse.ArgumentTypeError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2

    steps = [PipelineStep(item["action"], item["params"]) for item in raw_steps]

    # 每条流水线都先按第一个文件预检一遍整条链，把类型不匹配的问题挡在开工前
    try:
        for path in files:
            validate_chain(path, steps)
    except ActionError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2

    output_dir = args.out or str(Path.cwd() / "docforge-output")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    print(f"流水线：{len(steps)} 步 × {len(files)} 个文件 → {output_dir}")
    for line in describe_chain(files[0], steps):
        print(f"  {line}")
    print()

    return asyncio.run(
        _run_job(
            "pipeline",
            files,
            output_dir,
            {"__steps": raw_steps},
            suffix=args.suffix or "",
            concurrency=args.concurrency,
            conflict=args.conflict,
            quiet=args.quiet,
            json_output=args.json_output,
        )
    )


# --------------------------------------------------------------------------- #
# 参数预设                                                                      #
# --------------------------------------------------------------------------- #

async def _preset_list(action: str | None) -> list[dict[str, Any]]:
    from .storage.db import get_db

    db = get_db()
    await db.connect()
    try:
        return await db.list_presets(action)
    finally:
        await db.close()


def cmd_presets(args: argparse.Namespace) -> int:
    """列出 / 保存 / 删除参数预设。"""
    from .actions import ActionError, get_action

    if args.preset_action == "list":
        presets = asyncio.run(_preset_list(args.action))
        if args.json:
            print(json.dumps(presets, ensure_ascii=False, indent=2))
            return 0
        if not presets:
            print("还没有保存任何预设。用 `docforge presets save <动作> <名字> --param k=v` 创建。")
            return 0

        current = None
        for preset in presets:
            if preset["action"] != current:
                current = preset["action"]
                print(f"\n[{current}]")
            summary = ", ".join(f"{k}={v!r}" for k, v in list(preset["params"].items())[:6])
            print(f"  {preset['name']:<16} {summary}{' …' if len(preset['params']) > 6 else ''}")
        print()
        return 0

    if args.preset_action == "save":
        try:
            spec = get_action(args.action)
        except ActionError as err:
            print(f"错误：{err}", file=sys.stderr)
            return 2

        name = (args.name or "").strip()
        if not name:
            print("错误：预设名不能为空", file=sys.stderr)
            return 2

        params: dict[str, Any] = {}
        for raw in args.param or []:
            try:
                key, value = parse_param(raw)
            except argparse.ArgumentTypeError as err:
                print(f"错误：{err}", file=sys.stderr)
                return 2
            params[key] = value

        if not params:
            print("错误：至少需要一个 --param key=value，否则预设是空的", file=sys.stderr)
            return 2

        async def save() -> dict[str, Any]:
            from .storage.db import get_db

            db = get_db()
            await db.connect()
            try:
                return await db.save_preset(spec.id, name, params)
            finally:
                await db.close()

        preset = asyncio.run(save())
        print(f"已保存预设「{preset['name']}」→ {spec.label}（{len(params)} 个参数）")
        return 0

    if args.preset_action == "delete":
        async def remove() -> bool:
            from .storage.db import get_db

            db = get_db()
            await db.connect()
            try:
                if args.id:
                    return await db.delete_preset(args.id)
                # 允许用 "动作 名字" 删除，比让用户去查 id 友好得多
                if not args.name:
                    return False
                found = await db.find_preset(args.action, args.name)
                return await db.delete_preset(found["id"]) if found else False
            finally:
                await db.close()

        ok = asyncio.run(remove())
        if not ok:
            print("错误：没有找到匹配的预设（可用 `docforge presets list` 查看）", file=sys.stderr)
            return 2

        print("已删除")
        return 0

    print(f"错误：未知的 presets 子命令 {args.preset_action}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- #
# 入口                                                                          #
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docforge",
        description="文枢 DocForge 命令行工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  docforge actions\n"
            "  docforge run image.watermark ./photos -o ./out --param text=机密\n"
            "  docforge run pdf.searchable scan.pdf -o ./out\n"
            "  docforge run image.watermark ./photos --preset 公章\n"
            "  docforge watch ./收件箱 --action doc.to_pdf\n"
            "  docforge presets save image.watermark 公章 --param text=公司公章 --param opacity=0.3\n"
            "  docforge presets list\n"
            "  docforge selfcheck\n"
            "  docforge serve          # 启动内核服务（Electron 用这个）\n"
        ),
    )
    parser.add_argument("--version", action="store_true", help="显示版本")
    sub = parser.add_subparsers(dest="command")

    p_actions = sub.add_parser("actions", help="列出所有可用动作及其参数")
    p_actions.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    p_actions.set_defaults(func=cmd_actions)

    p_run = sub.add_parser("run", help="对文件或目录执行一个动作")
    p_run.add_argument("action", help="动作 id，例如 image.watermark")
    p_run.add_argument("inputs", nargs="+", help="文件或目录（目录会递归展开）")
    p_run.add_argument("-o", "--out", help="输出目录（默认 ./docforge-output）")
    p_run.add_argument("--param", action="append", metavar="KEY=VALUE", help="动作参数，可重复")
    p_run.add_argument("--preset", help="先套用这个预设，再用 --param 覆盖其中的个别项")
    p_run.add_argument("--suffix", help="输出文件名追加的后缀")
    p_run.add_argument("-j", "--concurrency", type=int, default=4, help="并发数（默认 4）")
    p_run.add_argument("--conflict", choices=["rename", "overwrite", "skip"], default="rename",
                       help="重名处理方式（默认自动重命名）")
    p_run.add_argument("--json", action="store_true", dest="json_output", help="输出任务结果 JSON")
    p_run.add_argument("-q", "--quiet", action="store_true", help="不打印动作内部日志")
    p_run.set_defaults(func=cmd_run)

    p_watch = sub.add_parser("watch", help="监听文件夹，新文件出现即自动处理")
    p_watch.add_argument("directory", help="要监听的目录")
    p_watch.add_argument("--action", required=True, help="要执行的动作 id")
    p_watch.add_argument("-o", "--out", help="输出目录（默认为监听目录下的「已处理」）")
    p_watch.add_argument("--param", action="append", metavar="KEY=VALUE", help="动作参数，可重复")
    p_watch.add_argument("--preset", help="先套用这个预设，再用 --param 覆盖其中的个别项")
    p_watch.add_argument("--suffix", help="输出文件名后缀")
    p_watch.add_argument("--exts", help="只处理这些扩展名，逗号分隔（默认取动作声明的类型）")
    p_watch.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数（默认 2）")
    p_watch.add_argument("-j", "--concurrency", type=int, default=4)
    p_watch.add_argument("--conflict", choices=["rename", "overwrite", "skip"], default="rename")
    p_watch.set_defaults(func=cmd_watch)

    p_pipe = sub.add_parser(
        "pipeline",
        help="多步骤流水线：上一步的产物直接作为下一步的输入",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  docforge pipeline scan.pdf -o ./out \\\n"
            "      --step pdf.searchable:dpi=300 \\\n"
            "      --step pdf.watermark:text=机密,opacity=0.35 \\\n"
            "      --step pdf.compress\n"
            "  # 值里带逗号时用无歧义写法\n"
            "  docforge pipeline scan.pdf --step pdf.watermark \\\n"
            "      --step-param 1:text=第一页,第二页\n"
        ),
    )
    p_pipe.add_argument("inputs", nargs="+", help="文件或目录（目录会递归展开）")
    p_pipe.add_argument(
        "--step",
        action="append",
        required=True,
        metavar="ACTION[:K=V,K=V]",
        help="流水线的一步，按顺序重复给出",
    )
    p_pipe.add_argument(
        "--step-param",
        action="append",
        dest="step_params",
        metavar="N:K=V",
        help="给第 N 步补参数（值里含逗号时用这个）",
    )
    p_pipe.add_argument("-o", "--out", help="输出目录（默认 ./docforge-output）")
    p_pipe.add_argument("--suffix", help="输出文件名追加的后缀")
    p_pipe.add_argument("-j", "--concurrency", type=int, default=4, help="并发数（默认 4）")
    p_pipe.add_argument("--conflict", choices=["rename", "overwrite", "skip"], default="rename")
    p_pipe.add_argument("--json", action="store_true", dest="json_output", help="输出任务结果 JSON")
    p_pipe.add_argument("-q", "--quiet", action="store_true", help="不打印动作内部日志")
    p_pipe.set_defaults(func=cmd_pipeline)

    p_check = sub.add_parser("selfcheck", help="检查本机环境与可用能力")
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_selfcheck)

    p_presets = sub.add_parser("presets", help="管理参数预设（把调好的一套参数存下来复用）")
    preset_sub = p_presets.add_subparsers(dest="preset_action", required=True)

    pp_list = preset_sub.add_parser("list", help="列出预设")
    pp_list.add_argument("--action", help="只看某个动作的预设")
    pp_list.add_argument("--json", action="store_true")
    pp_list.set_defaults(func=cmd_presets)

    pp_save = preset_sub.add_parser("save", help="保存（同名即覆盖）")
    pp_save.add_argument("action", help="动作 id")
    pp_save.add_argument("name", help="预设名")
    pp_save.add_argument("--param", action="append", metavar="KEY=VALUE", required=True)
    pp_save.set_defaults(func=cmd_presets)

    pp_del = preset_sub.add_parser("delete", help="删除预设")
    pp_del.add_argument("action", nargs="?", help="动作 id（与 name 搭配使用）")
    pp_del.add_argument("name", nargs="?", help="预设名")
    pp_del.add_argument("--id", help="直接按预设 id 删除")
    pp_del.set_defaults(func=cmd_presets)

    p_serve = sub.add_parser("serve", help="启动内核服务（Electron 集成用）")
    p_serve.set_defaults(func=cmd_serve)

    p_version = sub.add_parser("version", help="显示内核版本")
    p_version.set_defaults(func=cmd_version)

    return parser


def is_cli_invocation(argv: list[str]) -> bool:
    """判断这次调用是不是 CLI 子命令。

    不带子命令时走原来的"启动内核服务"路径 —— Electron 正是靠
    ``python -m docforge`` 拉起内核的，不能因为加了 CLI 就破坏它。
    """
    if len(argv) <= 1:
        return False
    first = argv[1]
    return first in COMMANDS or first in ("-h", "--help", "--version")


def _print_version() -> None:
    from . import __version__

    print(f"文枢 DocForge 内核 v{__version__}")
    print(f"Python {sys.version.split()[0]} · {os.name}")


def main(argv: list[str] | None = None) -> int:
    configure_stdio()

    argv = list(sys.argv if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv[1:])

    if getattr(args, "version", False):
        _print_version()
        return 0

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    return int(args.func(args))
