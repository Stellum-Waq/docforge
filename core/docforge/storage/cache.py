"""同步结果缓存。

## 为什么需要一份"同步"实现

云端 OCR 的结果缓存是为了**省 API 费用**：同一个文件重复处理时必须零计费。
但动作处理器跑在线程池里（不是事件循环），而 ``aiosqlite`` 需要事件循环，
两者对不上。

因此这里用标准库 ``sqlite3`` 再开一条同步连接，与 ``aiosqlite`` 共用同一个
数据库文件。SQLite 开启 WAL 后支持多连接并发读写，这样处理既简单又不会
把异步链路拖进线程。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import get_settings

#: 缓存条目上限，超出后按最久未命中淘汰。防止长期使用把磁盘吃满。
MAX_ENTRIES = 5000
#: 单条缓存的体积上限（字符数）。超大结果（例如整本扫描件）不进缓存。
MAX_VALUE_CHARS = 4_000_000


class SyncCache:
    """线程安全的同步缓存。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or get_settings().db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._ready = False
        self._ready_lock = threading.Lock()

    # ------------------------------------------------------------------ #

    def _conn(self) -> sqlite3.Connection:
        """每个线程一条连接。SQLite 连接不能跨线程共享。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path), timeout=10.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        with self._ready_lock:
            if self._ready:
                return
            conn = self._conn()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS result_cache (
                    key         TEXT PRIMARY KEY,
                    value_json  TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    hits        INTEGER NOT NULL DEFAULT 0,
                    last_hit_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON result_cache(created_at)")
            conn.commit()
            self._ready = True

    # ------------------------------------------------------------------ #

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            self._ensure_schema()
            conn = self._conn()
            row = conn.execute("SELECT value_json FROM result_cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            with self._write_lock:
                conn.execute(
                    "UPDATE result_cache SET hits = hits + 1, last_hit_at = ? WHERE key = ?",
                    (datetime.now(timezone.utc).isoformat(), key),
                )
                conn.commit()
            return json.loads(row["value_json"])
        except (sqlite3.Error, json.JSONDecodeError):
            # 缓存故障绝不能影响主流程 —— 最坏情况就是重新调一次云端
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        try:
            self._ensure_schema()
            payload = json.dumps(value, ensure_ascii=False)
            if len(payload) > MAX_VALUE_CHARS:
                return
            conn = self._conn()
            with self._write_lock:
                conn.execute(
                    """
                    INSERT INTO result_cache (key, value_json, created_at, hits)
                    VALUES (?,?,?,0)
                    ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                    """,
                    (key, payload, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                self._evict(conn)
        except (sqlite3.Error, TypeError, ValueError):
            pass

    def _evict(self, conn: sqlite3.Connection) -> None:
        """超出上限时淘汰最久未被命中的条目。"""
        row = conn.execute("SELECT COUNT(*) AS n FROM result_cache").fetchone()
        if row is None or row["n"] <= MAX_ENTRIES:
            return
        excess = row["n"] - MAX_ENTRIES
        conn.execute(
            """
            DELETE FROM result_cache WHERE key IN (
                SELECT key FROM result_cache
                ORDER BY COALESCE(last_hit_at, created_at) ASC
                LIMIT ?
            )
            """,
            (excess,),
        )
        conn.commit()

    def stats(self) -> dict[str, int]:
        try:
            self._ensure_schema()
            row = self._conn().execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(hits), 0) AS h FROM result_cache"
            ).fetchone()
            return {"entries": row["n"] if row else 0, "hits": row["h"] if row else 0}
        except sqlite3.Error:
            return {"entries": 0, "hits": 0}

    def clear(self) -> None:
        try:
            self._ensure_schema()
            conn = self._conn()
            with self._write_lock:
                conn.execute("DELETE FROM result_cache")
                conn.commit()
        except sqlite3.Error:
            pass


_cache: SyncCache | None = None
_cache_lock = threading.Lock()


def get_sync_cache() -> SyncCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = SyncCache()
    return _cache


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["SyncCache", "get_sync_cache", "now_iso", "time"]
