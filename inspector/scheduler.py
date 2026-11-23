"""巡检调度器：按纽约当地时间每天一次检查服务卷可用字节数。

关键语义：
- 读盘在锁外执行，因此读盘期间仍可通过网页修改巡检时刻；
- 已开始的巡检保留原计划时间，未开始的巡检立即按新时刻重排；
- 重启后补做当天已到计划时间但没有结果的巡检；
  重启前已开始（running）的巡检仍沿用原来的计划时间；
- 同一当地日期只保留一份结果（见 Storage.save_result）；
- 支持演练时钟：now() = 真实 UTC + 偏移量，偏移量持久化。
"""
from __future__ import annotations

import os
import shutil
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import timeutil
from .storage import Storage

# 读盘函数签名：(volume_path) -> 可用字节数；失败抛异常。
DiskReader = Callable[[str], int]


def default_disk_reader(volume_path: str) -> int:
    """shutil.disk_usage 读取目标挂载点可用字节数。"""
    return shutil.disk_usage(volume_path).free


class Scheduler:
    def __init__(self, storage: Storage,
                 volume_path: str,
                 disk_reader: DiskReader = default_disk_reader,
                 read_delay: float = 0.0,
                 poll_interval: float = 1.0,
                 clock: Callable[[], datetime] = datetime.utcnow):
        self.storage = storage
        self.volume_path = volume_path
        self.disk_reader = disk_reader
        self.read_delay = read_delay
        self.poll_interval = poll_interval
        self._clock = clock  # 返回 naive UTC，便于测试注入
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread: Optional[threading.Thread] = None
        # 正在执行读盘的当地日期（内存态，重启即清空）。
        self._running_date: Optional[str] = None
        self._running_planned_utc: Optional[str] = None
        self._running_check_time: Optional[str] = None

    # ---- 演练时钟 -------------------------------------------------------
    def now(self) -> datetime:
        """当前演练时间（naive UTC）。"""
        offset = float(self.storage.get_setting("clock_offset_seconds") or 0)
        return self._clock() + timedelta(seconds=offset)

    def now_aware_utc(self) -> datetime:
        return self.now().replace(tzinfo=timeutil.UTC)

    def now_ny(self) -> datetime:
        return self.now_aware_utc().astimezone(timeutil.NY_TZ)

    def set_clock(self, dt_utc: datetime) -> None:
        """把演练时钟直接设定到某个 UTC 绝对时刻。"""
        if dt_utc.tzinfo is not None:
            dt_utc = dt_utc.astimezone(timeutil.UTC).replace(tzinfo=None)
        offset = (dt_utc - self._clock()).total_seconds()
        with self.lock:
            self.storage.set_setting("clock_offset_seconds", str(offset))

    def advance_clock(self, seconds: float) -> None:
        """在当前演练时间基础上推进若干秒。"""
        with self.lock:
            offset = float(self.storage.get_setting("clock_offset_seconds") or 0)
            self.storage.set_setting("clock_offset_seconds",
                                     str(offset + seconds))

    def reset_clock(self) -> None:
        with self.lock:
            self.storage.set_setting("clock_offset_seconds", "0")

    # ---- 巡检时刻设置 ---------------------------------------------------
    def set_check_time(self, hhmm: str) -> None:
        """校验并保存每日巡检时刻；当天未开始的计划立即重排。"""
        timeutil.parse_hhmm(hhmm)  # 仅校验
        with self.lock:
            self.storage.set_setting("check_time", hhmm)
            self._replan_today_locked()

    def get_check_time(self) -> str:
        return self.storage.get_setting("check_time")

    def _replan_today_locked(self) -> None:
        """当天没有结果、且没有正在读盘时，按当前时刻重建当天计划。"""
        today = self.now_ny().date().isoformat()
        if self._running_date == today:
            return  # 已开始的巡检保留原计划时间
        if self.storage.get_result(today) is not None:
            return  # 当天已有结果，不再重排
        hhmm = self.get_check_time()
        planned, note = timeutil.planned_instant(
            self.now_ny().date(), hhmm
        )
        self.storage.upsert_plan(
            today, hhmm, timeutil.to_utc_iso(planned), note, "pending"
        )

    # ---- 主循环与单次 tick ----------------------------------------------
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(
            target=self._loop, name="inspection-scheduler", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def wake(self) -> None:
        """设置变更后唤醒循环，避免等待下一个轮询周期。"""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # 调度线程不能因单次异常退出
                import logging
                logging.getLogger("inspector").exception("scheduler tick 失败")
            self._wake.wait(timeout=self.poll_interval)
            self._wake.clear()

    def tick(self) -> None:
        """一次调度检查，独立成方法便于单元测试直接驱动。"""
        with self.lock:
            now = self.now_aware_utc()
            today = now.astimezone(timeutil.NY_TZ).date().isoformat()

            # 没有计划则建计划（含重启后、日期切换后）。
            plan = self.storage.get_plan(today)
            if plan is None:
                self._replan_today_locked()
                plan = self.storage.get_plan(today)
            if plan is None:
                return

            # 重启恢复：DB 里仍是 running 但本进程没有在读，
            # 视为上次中断 —— 沿用原计划时间重新执行。
            interrupted = (
                plan["status"] == "running"
                and self._running_date != today
            )
            due = now >= timeutil.from_iso(plan["planned_utc"])
            if not (due or interrupted):
                return
            if self.storage.get_result(today) is not None:
                return  # 已有结果（同一当地日期只记一份）

            # 标记开始，固化计划时间。
            if interrupted and plan.get("started_utc"):
                # 已开始过的巡检：保留原始开始时间，仅重跑读盘。
                started_utc = plan["started_utc"]
            else:
                started_utc = timeutil.to_utc_iso(now)
            self.storage.mark_running(today, started_utc)
            self._running_date = today
            self._running_planned_utc = plan["planned_utc"]
            self._running_check_time = plan["check_time"]
            snapshot = dict(plan)

        # 读盘放在锁外：期间网页仍可修改时刻。
        if self.read_delay:
            # 可配置延迟，方便演练“读盘期间改时刻”。
            if self._stop.wait(timeout=self.read_delay):
                return
        finished_utc = timeutil.to_utc_iso(self.now_aware_utc())
        available: Optional[int] = None
        error: Optional[str] = None
        try:
            available = int(self.disk_reader(self.volume_path))
        except Exception as exc:  # 读盘失败记录错误，不抛出
            error = f"{type(exc).__name__}: {exc}"

        with self.lock:
            if self._running_date != today:
                return  # 极端情况下已被其他流程取代，放弃写回
            self.storage.save_result(
                local_date=today,
                check_time=self._running_check_time,
                planned_utc=self._running_planned_utc,
                started_utc=snapshot.get("started_utc") or started_utc,
                finished_utc=finished_utc,
                available_bytes=available,
                error=error,
            )
            self.storage.mark_finished(
                today, "error" if error else "done", finished_utc
            )
            self._running_date = None
            self._running_planned_utc = None
            self._running_check_time = None

    # ---- 给网页的只读状态 ------------------------------------------------
    def status(self) -> dict:
        with self.lock:
            now_ny = self.now_ny()
            today = now_ny.date().isoformat()
            plan = self.storage.get_plan(today)
            result = self.storage.get_result(today)
            return {
                "now_ny": now_ny,
                "today": today,
                "check_time": self.get_check_time(),
                "plan": plan,
                "result": result,
                "reading": self._running_date == today,
                "volume_path": self.volume_path,
                "clock_offset_seconds": float(
                    self.storage.get_setting("clock_offset_seconds") or 0
                ),
                "recent": self.storage.list_recent_results(),
            }
