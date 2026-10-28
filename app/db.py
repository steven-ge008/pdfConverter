"""任务元数据的 SQLite 持久化。

单镜像不依赖外部 Redis/MQ：队列在内存，状态与结果索引入 SQLite，
容器重启后可识别残留任务并标记为失败，避免调用方无限轮询。
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from .core.settings import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    result_path TEXT,
    error       TEXT,
    pages       INTEGER,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at);
"""


class JobStore:
    """SQLite 连接按线程隔离（worker 在线程池中执行同步转换）。"""

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            self._local.conn = conn
        return conn

    def create(self, job_id: str, kind: str, payload: dict) -> None:
        now = time.time()
        with self._write_lock, self._conn() as conn:
            conn.execute(
                'INSERT INTO jobs (id, kind, status, payload, created_at, updated_at)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                (job_id, kind, 'pending', json.dumps(payload, ensure_ascii=False), now, now),
            )

    def update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        fields['updated_at'] = time.time()
        if 'payload' in fields and isinstance(fields['payload'], dict):
            fields['payload'] = json.dumps(fields['payload'], ensure_ascii=False)
        cols = ', '.join(f'{k} = ?' for k in fields)
        with self._write_lock, self._conn() as conn:
            conn.execute(f'UPDATE jobs SET {cols} WHERE id = ?', (*fields.values(), job_id))

    def get(self, job_id: str):
        with self._conn() as conn:
            row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['payload'] = json.loads(data['payload'])
        return data

    def mark_orphans_failed(self) -> int:
        """启动时调用：上次进程退出遗留的 pending/running 任务标记为失败。"""
        with self._write_lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, updated_at = ?"
                " WHERE status IN ('pending', 'running')",
                ('服务重启导致任务中断，请重新提交', time.time()),
            )
            return cur.rowcount

    def expired(self, ttl: int) -> list:
        cutoff = time.time() - ttl
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT id, result_path FROM jobs WHERE updated_at < ?', (cutoff,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, job_id: str) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute('DELETE FROM jobs WHERE id = ?', (job_id,))


store = None


def init_store() -> JobStore:
    global store
    store = JobStore(settings.DATA_DIR / 'jobs.db')
    return store
