"""SQLite 持久化层。

只做两件事：**任务历史落盘** 与 **OCR/转换结果缓存**（缓存用于避免重复调用付费
API，对应设计文档 §1-G3 的成本控制）。刻意不引入 ORM —— 表结构简单，
aiosqlite + 手写 SQL 更透明、启动更快。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import get_settings

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    action          TEXT NOT NULL,
    action_label    TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    total_tasks     INTEGER NOT NULL DEFAULT 0,
    completed_tasks INTEGER NOT NULL DEFAULT 0,
    failed_tasks    INTEGER NOT NULL DEFAULT 0,
    output_dir      TEXT NOT NULL DEFAULT '',
    error           TEXT,
    params_json     TEXT
);

CREATE TABLE IF NOT EXISTS job_tasks (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    file_path   TEXT NOT NULL,
    file_name   TEXT NOT NULL,
    status      TEXT NOT NULL,
    duration_ms INTEGER,
    output_path TEXT,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_tasks_job ON job_tasks(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS result_cache (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0
);

-- 参数预设：把一套调好的参数存下来复用。
-- (action_id, name) 唯一 —— 同一个动作下同名即覆盖，避免用户存出
-- 一堆看不出区别的"水印1 / 水印2 / 水印3"。
CREATE TABLE IF NOT EXISTS presets (
    id          TEXT PRIMARY KEY,
    action_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    params_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(action_id, name)
);

CREATE INDEX IF NOT EXISTS idx_presets_action ON presets(action_id, name);
"""


class Database:
    """轻量异步数据库封装。

    连接是惰性建立的：内核启动时不应该因为数据库慢而拖慢握手。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or get_settings().db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.executescript(SCHEMA)
            await self._conn.commit()
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- 任务历史 ------------------------------------------------------- #

    async def save_job(self, job: dict[str, Any]) -> None:
        conn = await self.connect()
        await conn.execute(
            """
            INSERT INTO jobs (id, action, action_label, status, created_at, started_at,
                              finished_at, total_tasks, completed_tasks, failed_tasks,
                              output_dir, error, params_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                total_tasks=excluded.total_tasks,
                completed_tasks=excluded.completed_tasks,
                failed_tasks=excluded.failed_tasks,
                error=excluded.error
            """,
            (
                job["id"],
                job["action"],
                job["actionLabel"],
                job["status"],
                job["createdAt"],
                job.get("startedAt"),
                job.get("finishedAt"),
                job.get("totalTasks", 0),
                job.get("completedTasks", 0),
                job.get("failedTasks", 0),
                job.get("outputDir", ""),
                job.get("error"),
                json.dumps(job.get("params") or {}, ensure_ascii=False),
            ),
        )
        await conn.commit()

    async def save_tasks(self, job_id: str, tasks: list[dict[str, Any]]) -> None:
        conn = await self.connect()
        await conn.executemany(
            """
            INSERT INTO job_tasks (id, job_id, file_path, file_name, status,
                                   duration_ms, output_path, error)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                duration_ms=excluded.duration_ms,
                output_path=excluded.output_path,
                error=excluded.error
            """,
            [
                (
                    t["id"],
                    job_id,
                    t["filePath"],
                    t["fileName"],
                    t["status"],
                    t.get("durationMs"),
                    t.get("outputPath"),
                    t.get("error"),
                )
                for t in tasks
            ],
        )
        await conn.commit()

    async def list_jobs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        conn = await self.connect()
        cursor = await conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "action": r["action"],
                "actionLabel": r["action_label"],
                "status": r["status"],
                "createdAt": r["created_at"],
                "startedAt": r["started_at"],
                "finishedAt": r["finished_at"],
                "totalTasks": r["total_tasks"],
                "completedTasks": r["completed_tasks"],
                "failedTasks": r["failed_tasks"],
                "outputDir": r["output_dir"],
                "error": r["error"],
            }
            for r in rows
        ]

    async def get_job_tasks(self, job_id: str) -> list[dict[str, Any]]:
        conn = await self.connect()
        cursor = await conn.execute(
            "SELECT * FROM job_tasks WHERE job_id = ? ORDER BY rowid", (job_id,)
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "jobId": r["job_id"],
                "filePath": r["file_path"],
                "fileName": r["file_name"],
                "status": r["status"],
                "durationMs": r["duration_ms"],
                "outputPath": r["output_path"],
                "error": r["error"],
            }
            for r in rows
        ]

    async def clear_history(self) -> None:
        conn = await self.connect()
        await conn.execute("DELETE FROM jobs")
        await conn.commit()

    # -- 结果缓存（省 API 费用）----------------------------------------- #

    async def cache_get(self, key: str) -> Any | None:
        conn = await self.connect()
        cursor = await conn.execute("SELECT value_json FROM result_cache WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is None:
            return None
        await conn.execute("UPDATE result_cache SET hits = hits + 1 WHERE key = ?", (key,))
        await conn.commit()
        return json.loads(row["value_json"])

    async def cache_put(self, key: str, value: Any, created_at: str) -> None:
        conn = await self.connect()
        await conn.execute(
            "INSERT OR REPLACE INTO result_cache (key, value_json, created_at, hits) VALUES (?,?,?,0)",
            (key, json.dumps(value, ensure_ascii=False), created_at),
        )
        await conn.commit()

    async def cache_stats(self) -> dict[str, int]:
        conn = await self.connect()
        cursor = await conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(hits), 0) AS h FROM result_cache")
        row = await cursor.fetchone()
        return {"entries": row["n"] if row else 0, "hits": row["h"] if row else 0}

    # -- 参数预设 ------------------------------------------------------- #

    async def list_presets(self, action_id: str | None = None) -> list[dict[str, Any]]:
        conn = await self.connect()
        if action_id:
            cursor = await conn.execute(
                "SELECT * FROM presets WHERE action_id = ? ORDER BY name COLLATE NOCASE",
                (action_id,),
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM presets ORDER BY action_id, name COLLATE NOCASE"
            )
        rows = await cursor.fetchall()
        return [_preset_row(r) for r in rows]

    async def get_preset(self, preset_id: str) -> dict[str, Any] | None:
        conn = await self.connect()
        cursor = await conn.execute("SELECT * FROM presets WHERE id = ?", (preset_id,))
        row = await cursor.fetchone()
        return _preset_row(row) if row else None

    async def find_preset(self, action_id: str, name: str) -> dict[str, Any] | None:
        conn = await self.connect()
        cursor = await conn.execute(
            "SELECT * FROM presets WHERE action_id = ? AND name = ?", (action_id, name)
        )
        row = await cursor.fetchone()
        return _preset_row(row) if row else None

    async def save_preset(
        self, action_id: str, name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """同名即覆盖（保留原 id），返回落库后的记录。"""
        import uuid
        from datetime import datetime, timezone

        conn = await self.connect()
        now = datetime.now(timezone.utc).isoformat()
        existing = await self.find_preset(action_id, name)
        preset_id = existing["id"] if existing else uuid.uuid4().hex
        created = existing["createdAt"] if existing else now

        await conn.execute(
            """
            INSERT INTO presets (id, action_id, name, params_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                params_json = excluded.params_json,
                updated_at  = excluded.updated_at
            """,
            (preset_id, action_id, name, json.dumps(params, ensure_ascii=False), created, now),
        )
        await conn.commit()

        saved = await self.get_preset(preset_id)
        assert saved is not None
        return saved

    async def delete_preset(self, preset_id: str) -> bool:
        conn = await self.connect()
        cursor = await conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        await conn.commit()
        return cursor.rowcount > 0


def _preset_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "action": row["action_id"],
        "name": row["name"],
        "params": json.loads(row["params_json"] or "{}"),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db
