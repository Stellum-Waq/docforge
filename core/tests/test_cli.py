"""命令行接口的测试。

这里刻意分成两层：

* **纯函数层**（参数解析、文件展开、分发判定）—— 快、无副作用，覆盖边界值；
* **端到端层** —— 真的调用 ``main()`` 跑一个任务，验证退出码、产物与参数透传。

其中 ``test_repeated_runs_share_singleton_manager`` 是一个**回归测试**：
CLI 的 watch 模式会在同一进程里连续跑多批任务，而 ``JobManager`` 是模块级单例，
它的线程池一旦 shutdown 就不能再提交任务。第一版实现每批都 ``asyncio.run()``，
结果第二批直接报 ``cannot schedule new futures after shutdown``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from docforge import cli
from docforge.actions import get_action, list_actions

# --------------------------------------------------------------------------- #
# 参数解析                                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("text=机密", ("text", "机密")),
        ("count=12", ("count", 12)),
        ("scale=1.5", ("scale", 1.5)),
        ("tile=true", ("tile", True)),
        ("tile=YES", ("tile", True)),
        ("tile=on", ("tile", True)),
        ("tile=false", ("tile", False)),
        ("tile=No", ("tile", False)),
        ("tile=off", ("tile", False)),
        # 尾部空格会被去掉，但值本身保留原样
        (" text = 中文 值 ", ("text", "中文 值")),
        # 以 0 开头的编号必须保持字符串，否则 "007" 会变成 7
        ("code=007", ("code", "007")),
        ("code=0", ("code", 0)),
        # 含 = 的值只按第一个 = 切分
        ("expr=a=b", ("expr", "a=b")),
        ("empty=", ("empty", "")),
    ],
)
def test_parse_param_infers_types(raw: str, expected: tuple[str, object]) -> None:
    assert cli.parse_param(raw) == expected


@pytest.mark.parametrize("raw", ["", "text", "=value", "  =  "])
def test_parse_param_rejects_malformed_input(raw: str) -> None:
    """缺 ``=`` 或空参数名都要报错 —— 静默忽略参数是最难排查的失效方式。"""
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_param(raw)


# --------------------------------------------------------------------------- #
# 文件展开                                                                      #
# --------------------------------------------------------------------------- #

def test_collect_files_expands_directories_recursively(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "sub" / "b.png").write_bytes(b"x")

    files = cli.collect_files([str(tmp_path)])
    assert sorted(Path(f).name for f in files) == ["a.png", "b.png"]


def test_collect_files_accepts_mixed_inputs_and_order(tmp_path: Path) -> None:
    single = tmp_path / "solo.png"
    single.write_bytes(b"x")
    folder = tmp_path / "dir"
    folder.mkdir()
    (folder / "in.png").write_bytes(b"x")

    files = cli.collect_files([str(single), str(folder)])
    assert files[0] == str(single)
    assert len(files) == 2


def test_collect_files_skips_missing_paths_without_raising(tmp_path: Path) -> None:
    files = cli.collect_files([str(tmp_path / "nope.png")])
    assert files == []


# --------------------------------------------------------------------------- #
# 分发判定：不能破坏 Electron 拉起内核的方式                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # 不带子命令 → 启动内核服务（Electron 依赖这条路径）
        (["docforge"], False),
        # uvicorn/PyInstaller 可能追加的开关不应被当作子命令
        (["docforge", "--host", "127.0.0.1"], False),
        (["docforge", "run", "a.png"], True),
        (["docforge", "actions"], True),
        (["docforge", "watch", "./in"], True),
        (["docforge", "selfcheck"], True),
        (["docforge", "serve"], True),
        (["docforge", "version"], True),
        (["docforge", "--version"], True),
        (["docforge", "-h"], True),
        (["docforge", "--help"], True),
    ],
)
def test_is_cli_invocation(argv: list[str], expected: bool) -> None:
    assert cli.is_cli_invocation(argv) is expected


def test_every_declared_command_has_a_parser() -> None:
    """COMMANDS 里声明了但没建 parser 的子命令会让 argparse 直接报错退出。"""
    parser = cli.build_parser()
    minimal = {
        "actions": [],
        "run": ["image.watermark", "x.png"],
        "watch": ["./in", "--action", "image.watermark"],
        "pipeline": ["x.png", "--step", "image.watermark"],
        "presets": ["list"],
        "selfcheck": [],
        "serve": [],
        "version": [],
    }
    assert set(minimal) == set(cli.COMMANDS)

    for name, extra in minimal.items():
        # 未注册的子命令会触发 SystemExit(2)
        args = parser.parse_args([name, *extra])
        assert args.command == name
        assert callable(args.func)


# --------------------------------------------------------------------------- #
# 端到端：actions / version / help                                             #
# --------------------------------------------------------------------------- #

def test_cmd_actions_json_lists_every_registered_action(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["docforge", "actions", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    ids = {item["id"] for item in payload}
    assert ids == {spec.id for spec in list_actions()}
    # 聚合标记必须透出，否则脚本无法判断"多个输入 → 单个输出"
    merge = next(item for item in payload if item["id"] == "pdf.merge")
    assert merge["aggregate"] is True
    assert "params" in merge


def test_cmd_actions_text_groups_by_domain(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["docforge", "actions"]) == 0
    out = capsys.readouterr().out
    assert "[image]" in out
    assert "[pdf]" in out
    assert "pdf.merge" in out
    assert "（聚合）" in out


def test_version_flag_and_subcommand_agree(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["docforge", "--version"]) == 0
    flag_out = capsys.readouterr().out

    assert cli.main(["docforge", "version"]) == 0
    sub_out = capsys.readouterr().out

    assert flag_out == sub_out
    assert "文枢 DocForge" in flag_out


def test_no_subcommand_prints_help_instead_of_starting_server(capsys: pytest.CaptureFixture[str]) -> None:
    """``main()`` 无子命令时只打印帮助；真正启动服务的是 ``serve_main()``。"""
    assert cli.main(["docforge"]) == 0
    assert "usage" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------- #
# 端到端：run                                                                   #
# --------------------------------------------------------------------------- #

def _make_png(path: Path, size: tuple[int, int] = (240, 160)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (30, 34, 52)).save(path)
    return path


def test_run_watermarks_a_single_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _make_png(tmp_path / "in" / "photo.png")
    out_dir = tmp_path / "out"

    code = cli.main([
        "docforge", "run", "image.watermark", str(source),
        "-o", str(out_dir), "--param", "text=绝密", "--param", "opacity=0.4",
    ])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    produced = list(out_dir.glob("*.png"))
    assert len(produced) == 1
    # 水印是真的画上去了，而不是原样复制
    assert produced[0].read_bytes() != source.read_bytes()


def test_run_expands_directory_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_png(tmp_path / "in" / "a.png")
    _make_png(tmp_path / "in" / "nested" / "b.png")
    out_dir = tmp_path / "out"

    assert cli.main(["docforge", "run", "image.watermark", str(tmp_path / "in"), "-o", str(out_dir)]) == 0
    capsys.readouterr()
    assert len(list(out_dir.glob("*.png"))) == 2


def test_run_json_output_is_machine_readable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _make_png(tmp_path / "in" / "a.png")
    out_dir = tmp_path / "out"

    assert cli.main(["docforge", "run", "image.watermark", str(source), "-o", str(out_dir), "--json"]) == 0

    out = capsys.readouterr().out
    # 前面有人类可读的进度行，JSON 从第一个 "{" 开始
    payload = json.loads(out[out.index("{"):])
    assert payload["action"] == "image.watermark"
    assert payload["status"] == "succeeded"
    assert payload["totalTasks"] == 1
    assert payload["failedTasks"] == 0


def test_run_rejects_unknown_action(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["docforge", "run", "no.such.action", "x.png"]) == 2
    assert "可用动作" in capsys.readouterr().err


def test_run_reports_extension_mismatch_as_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """全部文件类型不匹配时必须报错退出，而不是"成功但什么都没做"。"""
    source = _make_png(tmp_path / "a.png")
    code = cli.main(["docforge", "run", "pdf.info", str(source), "-o", str(tmp_path / "out")])

    assert code == 2
    assert "不接受这些文件类型" in capsys.readouterr().err


def test_run_skips_mismatched_files_but_processes_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_png(tmp_path / "in" / "photo.png")
    (tmp_path / "in" / "notes.txt").write_text("hi", encoding="utf-8")
    out_dir = tmp_path / "out"

    assert cli.main(["docforge", "run", "image.watermark", str(tmp_path / "in"), "-o", str(out_dir)]) == 0
    err = capsys.readouterr().err
    assert "格式不匹配" in err
    assert len(list(out_dir.glob("*.png"))) == 1


def test_aggregate_action_needs_at_least_two_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import pymupdf

    pdf = tmp_path / "one.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(pdf))
    doc.close()

    assert cli.main(["docforge", "run", "pdf.merge", str(pdf), "-o", str(tmp_path / "out")]) == 2
    assert "至少需要 2 个文件" in capsys.readouterr().err


def test_aggregate_action_merges_two_pdfs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import pymupdf

    sources = []
    for index, pages in enumerate((2, 3)):
        path = tmp_path / f"src{index}.pdf"
        doc = pymupdf.open()
        for _ in range(pages):
            doc.new_page()
        doc.save(str(path))
        doc.close()
        sources.append(str(path))

    out_dir = tmp_path / "out"
    assert cli.main(["docforge", "run", "pdf.merge", *sources, "-o", str(out_dir)]) == 0
    capsys.readouterr()

    merged = list(out_dir.glob("*.pdf"))
    assert len(merged) == 1
    doc = pymupdf.open(str(merged[0]))
    try:
        assert doc.page_count == 5
    finally:
        doc.close()


def test_invalid_param_format_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _make_png(tmp_path / "a.png")
    assert cli.main(["docforge", "run", "image.watermark", str(source), "--param", "oops"]) == 2
    assert "key=value" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 端到端：selfcheck                                                            #
# --------------------------------------------------------------------------- #

def test_selfcheck_json_has_expected_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["docforge", "selfcheck", "--json"]) == 0

    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert isinstance(payload["items"], list)
    assert payload["items"]
    assert {"okCount", "warnCount", "failCount"} <= set(payload)


# --------------------------------------------------------------------------- #
# 端到端：pipeline                                                              #
# --------------------------------------------------------------------------- #

def test_parse_step_spec_supports_inline_params() -> None:
    assert cli.parse_step_spec("pdf.compress") == {"action": "pdf.compress", "params": {}}
    assert cli.parse_step_spec("pdf.watermark:text=机密,opacity=0.3") == {
        "action": "pdf.watermark",
        "params": {"text": "机密", "opacity": 0.3},
    }


def test_parse_step_spec_rejects_missing_action() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_step_spec(":text=x")


def test_build_steps_requires_at_least_one_step() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli._build_steps([], None)


def test_build_steps_step_param_targets_a_specific_step() -> None:
    steps = cli._build_steps(
        ["pdf.watermark:text=初始", "pdf.compress"],
        ["1:text=覆盖后", "2:subset_fonts=false"],
    )
    assert steps[0]["params"]["text"] == "覆盖后"
    assert steps[1]["params"]["subset_fonts"] is False


@pytest.mark.parametrize("raw", ["0:text=x", "5:text=x", "abc:text=x"])
def test_build_steps_step_param_rejects_out_of_range(raw: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli._build_steps(["image.watermark", "image.watermark"], [raw])


def test_pipeline_requires_a_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _make_png(tmp_path / "a.png")
    # argparse 的 required=True 会以 SystemExit(2) 退出
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["docforge", "pipeline", str(source)])
    assert excinfo.value.code == 2


def test_pipeline_rejects_incompatible_first_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """类型链在开工前就该被挡住，而不是等到任务跑起来才失败。"""
    source = _make_png(tmp_path / "a.png")
    code = cli.main([
        "docforge", "pipeline", str(source), "-o", str(tmp_path / "out"),
        "--step", "pdf.watermark:text=x",
    ])
    assert code == 2
    assert "不接受 .png" in capsys.readouterr().err


def test_pipeline_rejects_aggregate_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import pymupdf

    source = tmp_path / "a.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(source))
    doc.close()

    assert cli.main([
        "docforge", "pipeline", str(source), "-o", str(tmp_path / "out"), "--step", "pdf.merge",
    ]) == 2
    assert "聚合动作" in capsys.readouterr().err


def test_pipeline_runs_two_steps_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _make_png(tmp_path / "in" / "photo.png")
    out_dir = tmp_path / "out"

    code = cli.main([
        "docforge", "pipeline", str(source), "-o", str(out_dir),
        "--step", "image.watermark:text=第一步,opacity=0.5",
        "--step", "image.watermark:text=第二步,opacity=0.5",
    ])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "1. 图片加水印" in captured.out
    assert "2. 图片加水印" in captured.out

    produced = list(out_dir.glob("*.png"))
    assert len(produced) == 1
    # 输出目录里只有最终产物，中间文件不该出现
    assert len(list(out_dir.iterdir())) == 1
    assert produced[0].read_bytes() != source.read_bytes()


def test_pipeline_keeps_the_real_output_extension(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """以「文档转 PDF」收尾时产物必须是 .pdf，不能沿用源文件的 .docx。"""
    from docx import Document

    source = tmp_path / "in" / "report.docx"
    source.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_paragraph("流水线扩展名测试")
    doc.save(str(source))

    out_dir = tmp_path / "out"
    assert cli.main([
        "docforge", "pipeline", str(source), "-o", str(out_dir), "--step", "doc.to_pdf",
    ]) == 0
    capsys.readouterr()

    produced = list(out_dir.iterdir())
    assert [p.name for p in produced] == ["report.pdf"]


# --------------------------------------------------------------------------- #
# 编码：Windows 控制台默认 GBK，CLI 的 ✓ ✗ → 会让 print 直接抛异常            #
# --------------------------------------------------------------------------- #

def test_configure_stdio_is_idempotent_and_safe(capsys: pytest.CaptureFixture[str]) -> None:
    cli.configure_stdio()
    cli.configure_stdio()
    print("✓ 符号仍然可打印")  # 不抛异常即可


@pytest.mark.parametrize("command", [["selfcheck"], ["actions"], ["--version"]])
def test_cli_survives_a_gbk_console(command: list[str], tmp_path: Path) -> None:
    """**回归测试**：控制台编码为 GBK 时，CLI 不能崩。

    Windows 控制台默认 cp936，编不出 ``✓``；没有这层保护时 ``print`` 会抛
    ``UnicodeEncodeError`` 把整个命令打崩 —— 批量处理跑到一半崩掉、
    只留下半批产物。实测：``docforge selfcheck`` 直接 traceback 退出，
    ``docforge pipeline`` 产物已生成却返回退出码 1。
    """
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        # 刻意用 GBK，并清掉可能让子进程"侥幸能用"的覆盖
        "PYTHONIOENCODING": "gbk",
        "DOCFORGE_DATA_DIR": str(tmp_path / "data"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "docforge", *command],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )

    assert "UnicodeEncodeError" not in result.stderr, result.stderr[-800:]
    assert "Traceback" not in result.stderr, result.stderr[-800:]
    assert result.returncode == 0, f"{command} 退出码 {result.returncode}\n{result.stderr[-800:]}"


# --------------------------------------------------------------------------- #
# 回归：单例任务管理器必须能跨批次复用                                          #
# --------------------------------------------------------------------------- #

def test_repeated_runs_share_singleton_manager(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """连续两次独立运行都必须成功。

    第一版实现里 ``JobManager.shutdown()`` 把线程池永久关掉了，而管理器是
    模块级单例，于是第二次运行必然抛 ``cannot schedule new futures after shutdown``。
    """
    sources = [_make_png(tmp_path / "in" / f"{i}.png") for i in range(2)]

    for index, source in enumerate(sources):
        out_dir = tmp_path / f"out{index}"
        code = cli.main(["docforge", "run", "image.watermark", str(source), "-o", str(out_dir)])
        captured = capsys.readouterr()
        assert code == 0, f"第 {index + 1} 次运行失败：{captured.out}\n{captured.err}"
        assert len(list(out_dir.glob("*.png"))) == 1


def test_run_job_twice_in_one_loop_after_shutdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """同一个事件循环里 shutdown 之后仍能再次 start —— 这是 watch 的前提。"""
    source = _make_png(tmp_path / "a.png")

    async def scenario() -> list[int]:
        codes = []
        for index in range(3):
            codes.append(
                await cli._run_job(
                    "image.watermark",
                    [str(source)],
                    str(tmp_path / f"o{index}"),
                    {"text": "复用"},
                    suffix="",
                    concurrency=2,
                    conflict="rename",
                    quiet=True,
                )
            )
        return codes

    codes = asyncio.run(scenario())
    capsys.readouterr()
    assert codes == [0, 0, 0]


# --------------------------------------------------------------------------- #
# 端到端：watch                                                                #
# --------------------------------------------------------------------------- #

def test_watch_processes_files_arriving_in_later_waves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """watch 必须能持续处理**后续**出现的文件（回归上面那个线程池问题）。"""
    watch_dir = tmp_path / "inbox"
    watch_dir.mkdir()
    out_dir = tmp_path / "out"
    parser = cli.build_parser()

    async def scenario() -> int:
        args = parser.parse_args([
            "watch", str(watch_dir), "--action", "image.watermark",
            "--param", "text=监听", "--interval", "0.5", "-o", str(out_dir),
        ])
        spec = get_action(args.action)
        stop = asyncio.Event()
        task = asyncio.create_task(
            cli._watch_loop(spec, watch_dir, out_dir, {"text": "监听"}, {"png"}, args, stop)
        )

        try:
            # 第一波：watch 启动前就已存在的文件
            _make_png(watch_dir / "wave1.png")
            assert await _wait_for_count(out_dir, 1)

            # 第二波：运行中出现的新文件，走的是同一批已 shutdown 过的管理器
            _make_png(watch_dir / "wave2.png")
            assert await _wait_for_count(out_dir, 2)

            # 同名文件被重写（大小/时间变了）应当被当作新文件重新处理
            (watch_dir / "wave2.png").write_bytes(_png_bytes((320, 200)))
            assert await _wait_for_count(out_dir, 3)
        finally:
            stop.set()
        return await task

    assert asyncio.run(scenario()) == 0
    capsys.readouterr()


def test_watch_excludes_its_own_output_directory(tmp_path: Path) -> None:
    """产物不能落回监听范围，否则会被反复处理成死循环。"""
    watch_dir = tmp_path / "inbox"
    watch_dir.mkdir()
    out_dir = watch_dir / "已处理"          # 默认输出目录就在监听目录之内
    parser = cli.build_parser()
    args = parser.parse_args(["watch", str(watch_dir), "--action", "image.watermark", "-o", str(out_dir)])

    seen: list[str] = []
    original = cli._execute_job

    async def spy(action_id, files, output_dir, params, **kwargs):  # type: ignore[no-untyped-def]
        seen.extend(files)
        # 造一个产物落在输出目录里，模拟真实处理结果
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        _make_png(out_path / "产物.png")
        return 0

    async def scenario() -> None:
        cli._execute_job = spy  # type: ignore[assignment]
        stop = asyncio.Event()
        try:
            spec = get_action("image.watermark")
            task = asyncio.create_task(
                cli._watch_loop(spec, watch_dir, out_dir, {}, {"png"}, args, stop)
            )
            _make_png(watch_dir / "输入.png")
            for _ in range(60):
                await asyncio.sleep(0.1)
                if seen:
                    break
            stop.set()
            await task
        finally:
            cli._execute_job = original  # type: ignore[assignment]

    asyncio.run(scenario())
    assert [Path(f).name for f in seen] == ["输入.png"]


def _png_bytes(size: tuple[int, int]) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", size, (60, 20, 20)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _wait_for_count(directory: Path, expected: int, timeout: float = 30.0) -> bool:
    """等待输出目录里出现足够多的产物（轮询而不是固定 sleep，避免慢机器上误报）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if directory.is_dir() and len(list(directory.glob("*.png"))) >= expected:
            return True
        await asyncio.sleep(0.2)
    return False
