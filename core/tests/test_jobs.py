"""任务队列内核的测试。

队列层是所有功能的地基，这里重点验证那几个"错了会很难查"的性质：
  * 单个坏文件必须只影响它自己，不能拖垮整批
  * 不支持的文件类型要**显式跳过并给出原因**，而不是静默消失
  * 进度事件必须真的被推出来（否则 UI 一直转圈）
  * 取消要能生效，且不能留下半成品文件
  * 历史必须落盘，重启后仍可查
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image

from docforge.jobs import JobRequest, get_manager
from docforge.jobs import manager as manager_module


# --------------------------------------------------------------------------- #
# 夹具                                                                          #
# --------------------------------------------------------------------------- #

@pytest.fixture()
async def manager():
    """每个测试一个全新的 JobManager，避免测试间互相污染。"""
    instance = manager_module.JobManager()
    manager_module._manager = instance
    await instance.start()
    try:
        yield instance
    finally:
        await instance.shutdown()
        manager_module._manager = None


def _images(tmp_path: Path, count: int = 3) -> list[str]:
    paths = []
    for i in range(count):
        p = tmp_path / "src" / f"img{i}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (200 + i * 10, 150), (30 + i * 20, 90, 160)).save(p)
        paths.append(str(p))
    return paths


async def _wait(job_id: str, timeout: float = 30.0):
    mgr = get_manager()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = mgr.get(job_id)
        if job and job.status in {"succeeded", "failed", "cancelled"}:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务 {job_id} 在 {timeout}s 内未结束")


async def _run_job(manager, files, params=None, **kwargs):
    request = JobRequest(
        action="image.watermark",
        files=files,
        output_dir=kwargs.pop("output_dir"),
        params=params or {"mode": "text", "text": "机密", "position": "center", "opacity": 0.6},
        **kwargs,
    )
    job = manager.create_job(request)
    manager.start_job(job)
    return await _wait(job.id)


# --------------------------------------------------------------------------- #
# 正常路径                                                                      #
# --------------------------------------------------------------------------- #

async def test_batch_watermark_succeeds(manager, tmp_path: Path) -> None:
    files = _images(tmp_path, 3)
    out_dir = tmp_path / "out"

    job = await _run_job(manager, files, output_dir=str(out_dir))

    assert job.status == "succeeded"
    assert job.completed == 3 and job.failed == 0
    assert job.progress == 100.0

    produced = sorted(out_dir.glob("*.png"))
    assert len(produced) == 3
    for path in produced:
        assert path.stat().st_size > 0


async def test_suffix_applied_to_output_names(manager, tmp_path: Path) -> None:
    files = _images(tmp_path, 1)
    out_dir = tmp_path / "out"

    await _run_job(manager, files, output_dir=str(out_dir), suffix="_水印")

    assert (out_dir / "img0_水印.png").is_file()


async def test_tasks_report_duration_and_output(manager, tmp_path: Path) -> None:
    files = _images(tmp_path, 2)
    job = await _run_job(manager, files, output_dir=str(tmp_path / "out"))

    for task in job.tasks:
        assert task.status == "succeeded"
        assert task.output_path and Path(task.output_path).is_file()
        assert task.duration_ms is not None and task.duration_ms >= 0


# --------------------------------------------------------------------------- #
# 失败隔离                                                                      #
# --------------------------------------------------------------------------- #

async def test_one_corrupt_file_does_not_break_the_batch(manager, tmp_path: Path) -> None:
    """核心性质：坏文件只失败它自己，其余照常完成。"""
    files = _images(tmp_path, 3)
    broken = tmp_path / "src" / "broken.png"
    broken.write_bytes(b"this is definitely not a png")
    files.insert(1, str(broken))

    job = await _run_job(manager, files, output_dir=str(tmp_path / "out"))

    assert job.status == "succeeded"  # 部分失败仍算完成
    assert job.completed == 3
    assert job.failed == 1

    failed = [t for t in job.tasks if t.status == "failed"]
    assert len(failed) == 1
    assert failed[0].file_name == "broken.png"
    assert "无法打开图片" in (failed[0].error or "")
    # 失败项也要有可读原因，且其他文件确实产出了
    assert len(list((tmp_path / "out").glob("*.png"))) == 3


async def test_unsupported_extension_is_skipped_with_reason(manager, tmp_path: Path) -> None:
    good = _images(tmp_path, 1)
    doc = tmp_path / "src" / "notes.txt"
    doc.write_text("hello", encoding="utf-8")

    job = await _run_job(manager, good + [str(doc)], output_dir=str(tmp_path / "out"))

    skipped = [t for t in job.tasks if t.status == "skipped"]
    assert len(skipped) == 1
    assert skipped[0].file_name == "notes.txt"
    assert "不接受" in (skipped[0].error or "")


async def test_invalid_params_fail_only_that_action(manager, tmp_path: Path) -> None:
    files = _images(tmp_path, 1)
    job = await _run_job(
        manager,
        files,
        params={"mode": "text", "text": "   "},  # 空水印文字
        output_dir=str(tmp_path / "out"),
    )
    assert job.failed == 1
    assert "水印文字为空" in (job.tasks[0].error or "")


# --------------------------------------------------------------------------- #
# 进度事件                                                                      #
# --------------------------------------------------------------------------- #

async def test_progress_events_are_emitted(manager, tmp_path: Path) -> None:
    """没有进度事件，UI 就只会一直转圈。"""
    queue = manager.subscribe()
    files = _images(tmp_path, 2)
    await _run_job(manager, files, output_dir=str(tmp_path / "out"))

    types: list[str] = []
    while not queue.empty():
        event = queue.get_nowait()
        types.append(event["type"])

    assert "job.created" in types
    assert "task.updated" in types
    assert "job.finished" in types


async def test_progress_reaches_hundred(manager, tmp_path: Path) -> None:
    files = _images(tmp_path, 2)
    job = await _run_job(manager, files, output_dir=str(tmp_path / "out"))
    assert all(t.progress == 100.0 for t in job.tasks)


# --------------------------------------------------------------------------- #
# 取消与冲突策略                                                                #
# --------------------------------------------------------------------------- #

async def test_cancel_marks_remaining_tasks(manager, tmp_path: Path) -> None:
    files = _images(tmp_path, 8)
    request = JobRequest(
        action="image.watermark",
        files=files,
        output_dir=str(tmp_path / "out"),
        params={"mode": "text", "text": "X", "position": "tile", "tile_gap_ratio": 0.1},
        concurrency=1,
    )
    job = manager.create_job(request)
    manager.start_job(job)
    await asyncio.sleep(0.02)
    await manager.cancel(job.id)

    settled = await _wait(job.id)
    assert settled.status == "cancelled"
    # 不允许有任务停留在 running/queued 状态
    assert not [t for t in settled.tasks if t.status in {"running", "queued"}]


async def test_skip_policy_leaves_existing_output_untouched(manager, tmp_path: Path) -> None:
    files = _images(tmp_path, 1)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    existing = out_dir / "img0.png"
    existing.write_bytes(b"pre-existing")

    job = await _run_job(manager, files, output_dir=str(out_dir), conflict_policy="skip")

    assert job.tasks[0].status == "skipped"
    assert existing.read_bytes() == b"pre-existing"


async def test_rename_policy_creates_second_file(manager, tmp_path: Path) -> None:
    files = _images(tmp_path, 1)
    out_dir = tmp_path / "out"

    await _run_job(manager, files, output_dir=str(out_dir))
    await _run_job(manager, files, output_dir=str(out_dir))

    names = sorted(p.name for p in out_dir.glob("*.png"))
    assert names == ["img0 (2).png", "img0.png"]


async def test_preserve_tree_keeps_structure(manager, tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "项目A" / "合同"
    nested.mkdir(parents=True)
    src = nested / "scan.png"
    Image.new("RGB", (120, 90), "#334455").save(src)

    out_dir = tmp_path / "out"
    request = JobRequest(
        action="image.watermark",
        files=[str(src)],
        output_dir=str(out_dir),
        params={"mode": "text", "text": "副本"},
        preserve_tree=True,
        root_dir=str(root),
    )
    job = manager.create_job(request)
    manager.start_job(job)
    await _wait(job.id)

    assert (out_dir / "项目A" / "合同" / "scan.png").is_file()


# --------------------------------------------------------------------------- #
# 历史落盘                                                                      #
# --------------------------------------------------------------------------- #

async def test_history_is_persisted(manager, tmp_path: Path) -> None:
    files = _images(tmp_path, 1)
    job = await _run_job(manager, files, output_dir=str(tmp_path / "out"))

    from docforge.storage.db import get_db

    db = get_db()
    jobs = await db.list_jobs(limit=20)
    assert any(j["id"] == job.id for j in jobs)

    tasks = await db.get_job_tasks(job.id)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "succeeded"
    assert tasks[0]["outputPath"]


async def test_empty_file_list_is_rejected(manager, tmp_path: Path) -> None:
    from docforge.actions import ActionError

    with pytest.raises(ActionError, match="没有可处理的文件"):
        manager.create_job(
            JobRequest(action="image.watermark", files=[], output_dir=str(tmp_path))
        )


# --------------------------------------------------------------------------- #
# 输出路径预定的并发安全                                                        #
# --------------------------------------------------------------------------- #

async def test_concurrent_same_name_files_all_produce_output(manager, tmp_path: Path) -> None:
    """同名文件并发处理时，绝不能互相覆盖。

    这是很常见的真实场景：用户从多个文件夹拖入名字相同的文件
    （例如好几个「扫描件.png」）。如果输出路径只靠 exists() 判断，
    两个任务会**同时**算出同一个名字，后写的静默覆盖先写的 ——
    用户会莫名少文件且没有任何报错。
    """
    files: list[str] = []
    for index in range(5):
        directory = tmp_path / f"卷{index}"
        directory.mkdir(parents=True)
        path = directory / "扫描件.png"  # 故意全部同名
        Image.new("RGB", (160, 120), (30 * index, 90, 160)).save(path)
        files.append(str(path))

    out_dir = tmp_path / "out"
    job = await _run_job(manager, files, output_dir=str(out_dir), concurrency=5)

    assert job.status == "succeeded"
    assert job.completed == 5

    produced = sorted(p.name for p in out_dir.glob("*.png"))
    assert len(produced) == 5, f"5 个同名文件必须产出 5 份结果，实际：{produced}"
    assert "扫描件.png" in produced
    assert "扫描件 (5).png" in produced

    # 每份内容必须不同，否则说明确实发生了覆盖
    contents = {p.read_bytes() for p in out_dir.glob("*.png")}
    assert len(contents) == 5, "输出内容出现重复，说明发生了覆盖"


async def test_failed_task_releases_reserved_output_name(manager, tmp_path: Path) -> None:
    """失败的任务必须释放占用的文件名，否则重试只能拿到「文件 (2)」这种莫名其妙的编号。"""
    out_dir = tmp_path / "out"

    broken = tmp_path / "a" / "photo.png"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not an image at all")

    job1 = await _run_job(manager, [str(broken)], output_dir=str(out_dir))
    assert job1.failed == 1
    assert not list(out_dir.glob("*.png")), "失败任务不该留下产物"

    # 同名但内容正常的文件 → 应当拿回干净的名字
    good = tmp_path / "b" / "photo.png"
    good.parent.mkdir(parents=True)
    Image.new("RGB", (120, 90), "#334455").save(good)

    job2 = await _run_job(manager, [str(good)], output_dir=str(out_dir))
    assert job2.status == "succeeded"
    assert (out_dir / "photo.png").is_file()
    assert not (out_dir / "photo (2).png").exists()
