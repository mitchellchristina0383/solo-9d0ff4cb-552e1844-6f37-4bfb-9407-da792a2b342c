"""SQLite 持久层：设置、当日计划、巡检结果。

所有写操作在同一把进程锁内完成（配合 scheduler 的 RLock），
SQLite 自身打开 check_same_thread=False 以便调度线程与 Web 线程共用。
"""
from __future__ import annotations

import os
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

    def list_pending_plans(self):
        """所有仍处于 pending 的计划（按当地日期排序）。

        running / done / error 的计划不在其中：已开始或已完成的巡检
        不随时刻修改而重排。
        """
        rows = self.conn.execute(
            "SELECT * FROM plans WHERE status='pending' ORDER BY local_date"
        ).fetchall()
        return [dict(r) for r in rows]

    def apply_check_time_change(self, hhmm: str, replans) -> None:
        """原子地保存新巡检时刻并重排所有待执行计划。

        replans 为 (local_date, check_time, planned_utc, dst_note) 元组序列，
        调用方已保证其中只包含“当天尚无计划”或“仍为 pending”的日期。
        任一 SQL 失败时整个事务回滚：设置与待执行计划都保持原值。
        """
        # 测试/演练钩子：FAIL_TRIGGER_PATH 指向的文件存在时，直接拒绝写入，
        # 用来端到端验证“保存失败提示且状态不变”。默认未配置则永不触发。
        trigger = os.environ.get("FAIL_TRIGGER_PATH")
        if trigger and os.path.exists(trigger):
            raise sqlite3.OperationalError(
                "injected failure: fail trigger file present"
            )
        with self.conn:  # 正常退出提交，抛异常自动回滚
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES('check_time', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (hhmm,),
            )
            if getattr(self, "fail_next_check_time_change", False):
                # 测试钩子：在同一事务内制造失败，验证整体回滚。
                self.conn.execute("SELECT fail_on_purpose()")
            for local_date, check_time, planned_utc, dst_note in replans:
                # 冲突时只在计划仍为 pending 时覆盖；WHERE 不成立则静默跳过，
                # 绝不改动 running/done/error 计划固化下来的时刻。
                self.conn.execute(
                    "INSERT INTO plans(local_date, check_time, planned_utc,"
                    " dst_note, status, started_utc, finished_utc) "
                    "VALUES(?,?,?,?,'pending',NULL,NULL) "
                    "ON CONFLICT(local_date) DO UPDATE SET "
                    "check_time=excluded.check_time, "
                    "planned_utc=excluded.planned_utc, "
                    "dst_note=excluded.dst_note "
                    "WHERE plans.status='pending'",
                    (local_date, check_time, planned_utc, dst_note),
                )

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
