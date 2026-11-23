"""SQLite 持久层：设置、当日计划、巡检结果。

所有写操作在同一把进程锁内完成（配合 scheduler 的 RLock），
SQLite 自身打开 check_same_thread=False 以便调度线程与 Web 线程共用。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    local_date   TEXT PRIMARY KEY,   -- 纽约当地日期 YYYY-MM-DD
    check_time   TEXT NOT NULL,      -- 当时使用的 HH:MM
    planned_utc  TEXT NOT NULL,      -- 计划绝对时间（UTC ISO）
    dst_note     TEXT NOT NULL,      -- normal / gap_deferred / ambiguous_first
    status       TEXT NOT NULL,      -- pending / running / done / error
    started_utc  TEXT,
    finished_utc TEXT
);
CREATE TABLE IF NOT EXISTS results (
    local_date     TEXT PRIMARY KEY, -- 同一当地日期只记一份结果
    check_time     TEXT NOT NULL,
    planned_utc    TEXT NOT NULL,
    started_utc    TEXT,
    finished_utc   TEXT NOT NULL,
    available_bytes INTEGER,         -- 成功时填写
    error          TEXT              -- 失败时填写
);
"""

DEFAULT_CHECK_TIME = "09:00"


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        if self.get_setting("check_time") is None:
            self.set_setting("check_time", DEFAULT_CHECK_TIME)
        if self.get_setting("clock_offset_seconds") is None:
            self.set_setting("clock_offset_seconds", "0")

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # ---- plans ----------------------------------------------------------
    def get_plan(self, local_date: str):
        row = self.conn.execute(
            "SELECT * FROM plans WHERE local_date=?", (local_date,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_plan(self, local_date: str, check_time: str, planned_utc: str,
                    dst_note: str, status: str = "pending",
                    started_utc: Optional[str] = None,
                    finished_utc: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO plans(local_date, check_time, planned_utc, dst_note,"
            " status, started_utc, finished_utc) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(local_date) DO UPDATE SET "
            "check_time=excluded.check_time, planned_utc=excluded.planned_utc, "
            "dst_note=excluded.dst_note, status=excluded.status, "
            "started_utc=excluded.started_utc, finished_utc=excluded.finished_utc",
            (local_date, check_time, planned_utc, dst_note, status,
             started_utc, finished_utc),
        )
        self.conn.commit()

    def mark_running(self, local_date: str, started_utc: str) -> None:
        self.conn.execute(
            "UPDATE plans SET status='running', started_utc=? WHERE local_date=?",
            (started_utc, local_date),
        )
        self.conn.commit()

    def mark_finished(self, local_date: str, status: str,
                      finished_utc: str) -> None:
        self.conn.execute(
            "UPDATE plans SET status=?, finished_utc=? WHERE local_date=?",
            (status, finished_utc, local_date),
        )
        self.conn.commit()

    # ---- results --------------------------------------------------------
    def save_result(self, local_date: str, check_time: str,
                    planned_utc: str, started_utc: Optional[str],
                    finished_utc: str,
                    available_bytes: Optional[int],
                    error: Optional[str]) -> None:
        # INSERT OR REPLACE：同一当地日期始终只保留一份结果。
        self.conn.execute(
            "INSERT OR REPLACE INTO results(local_date, check_time, planned_utc,"
            " started_utc, finished_utc, available_bytes, error) "
            "VALUES(?,?,?,?,?,?,?)",
            (local_date, check_time, planned_utc, started_utc, finished_utc,
             available_bytes, error),
        )
        self.conn.commit()

    def get_result(self, local_date: str):
        row = self.conn.execute(
            "SELECT * FROM results WHERE local_date=?", (local_date,)
        ).fetchone()
        return dict(row) if row else None

    def list_recent_results(self, limit: int = 14):
        rows = self.conn.execute(
            "SELECT * FROM results ORDER BY local_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.conn.close()
