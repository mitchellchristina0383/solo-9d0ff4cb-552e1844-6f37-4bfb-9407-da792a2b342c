import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from inspector import timeutil
from inspector.scheduler import Scheduler
from inspector.storage import Storage

UTC = timezone.utc


class MutableClock:
    """可手动拨动的时钟，返回 naive UTC。"""

    def __init__(self, start_utc: datetime):
        self.now = start_utc.astimezone(UTC).replace(tzinfo=None)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class BlockingReader:
    """可阻塞的读盘器，用来制造“读盘期间”窗口。"""

    def __init__(self, value=123456, fail=False):
        self.value = value
        self.fail = fail
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=30)
        if self.fail:
            raise FileNotFoundError(f"no such volume: {path}")
        return self.value


def run_tick_in_thread(sch):
    t = threading.Thread(target=sch.tick, daemon=True)
    t.start()
    return t


def make_scheduler(start_utc, reader=None, read_delay=0.0, check_time="09:00"):
    tmpdir = tempfile.mkdtemp()
    storage = Storage(os.path.join(tmpdir, "test.db"))
    clock = MutableClock(start_utc)
    sch = Scheduler(
        storage=storage,
        volume_path="/volume",
        disk_reader=reader or (lambda p: 1000),
        read_delay=read_delay,
        poll_interval=0.01,
        clock=clock,
    )
    storage.set_setting("check_time", check_time)
    return sch, storage, clock, tmpdir


class SchedulerPlanTest(unittest.TestCase):
    def test_runs_at_planned_time_and_records_one_result(self):
        # 纽约 2026-06-15 09:00 EDT = 13:00Z
        sch, storage, clock, _ = make_scheduler(datetime(2026, 6, 15, 12, 0))
        sch.tick()
        plan = storage.get_plan("2026-06-15")
        self.assertEqual(plan["status"], "pending")
        self.assertEqual(
            timeutil.from_iso(plan["planned_utc"]),
            datetime(2026, 6, 15, 13, 0, tzinfo=UTC),
        )

        clock.advance(3600)  # 13:00Z，到点
        sch.tick()
        result = storage.get_result("2026-06-15")
        self.assertIsNotNone(result)
        self.assertIsNone(result["error"])
        self.assertEqual(result["available_bytes"], 1000)
        self.assertEqual(storage.get_plan("2026-06-15")["status"], "done")

        # 再 tick 也不会产生第二份结果
        clock.advance(60)
        sch.tick()
        self.assertEqual(len(storage.list_recent_results()), 1)

    def test_disk_failure_records_error(self):
        reader = BlockingReader(fail=True)
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 6, 15, 13, 0), reader=reader
        )
        t = run_tick_in_thread(sch)
        self.assertTrue(reader.entered.wait(5))
        reader.release.set()
        t.join(timeout=5)
        result = storage.get_result("2026-06-15")
        self.assertIsNotNone(result)
        self.assertIsNone(result["available_bytes"])
        self.assertIn("FileNotFoundError", result["error"])
        self.assertEqual(storage.get_plan("2026-06-15")["status"], "error")

    def test_change_time_while_reading_keeps_old_plan(self):
        reader = BlockingReader(value=999)
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 6, 15, 13, 0), reader=reader
        )
        # 到点触发读盘，读盘阻塞中（tick 在后台线程，模拟调度线程）
        t = run_tick_in_thread(sch)
        self.assertTrue(reader.entered.wait(5))
        self.assertEqual(sch._running_date, "2026-06-15")
        original_plan = storage.get_plan("2026-06-15")
        self.assertEqual(original_plan["check_time"], "09:00")
        self.assertEqual(original_plan["status"], "running")

        # 读盘期间把时刻改成 11:00
        sch.set_check_time("11:00")
        plan_during = storage.get_plan("2026-06-15")
        self.assertEqual(plan_during["check_time"], "09:00", "已开始的巡检保留原计划")
        self.assertEqual(plan_during["status"], "running")

        reader.release.set()
        t.join(timeout=5)
        result = storage.get_result("2026-06-15")
        self.assertEqual(result["available_bytes"], 999)
        self.assertEqual(result["check_time"], "09:00")
        self.assertEqual(
            result["planned_utc"], original_plan["planned_utc"],
            "结果记录的是原计划时间",
        )

    def test_change_time_before_start_reschedules(self):
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 6, 15, 12, 0), check_time="09:00"
        )
        sch.tick()
        self.assertEqual(
            storage.get_plan("2026-06-15")["check_time"], "09:00"
        )
        # 未开始，改成 08:30（=12:30Z）
        sch.set_check_time("08:30")
        plan = storage.get_plan("2026-06-15")
        self.assertEqual(plan["check_time"], "08:30")
        self.assertEqual(plan["status"], "pending")
        self.assertEqual(
            timeutil.from_iso(plan["planned_utc"]),
            datetime(2026, 6, 15, 12, 30, tzinfo=UTC),
        )

        # 若新时刻已过，下一次 tick 立即补做
        clock.advance(2000)
        sch.tick()
        self.assertIsNotNone(storage.get_result("2026-06-15"))

    def test_restart_makes_up_due_pending_plan(self):
        sch, storage, clock, tmpdir = make_scheduler(
            datetime(2026, 6, 15, 12, 0)
        )
        sch.tick()  # 生成 pending 计划，13:00Z 执行
        db_path = storage.path
        storage.close()

        # 重启：新的 Scheduler 打开同一个 DB，时钟已到 14:00Z
        clock.advance(2 * 3600)
        storage2 = Storage(db_path)
        sch2 = Scheduler(storage2, "/volume", disk_reader=lambda p: 2000,
                         clock=clock)
        sch2.tick()
        result = storage2.get_result("2026-06-15")
        self.assertIsNotNone(result, "重启后补做当天已到时但没有结果的巡检")
        self.assertEqual(result["available_bytes"], 2000)
        # 计划时间仍是原来的 13:00Z
        self.assertEqual(
            timeutil.from_iso(result["planned_utc"]),
            datetime(2026, 6, 15, 13, 0, tzinfo=UTC),
        )

    def test_restart_reruns_interrupted_read_with_original_planned_time(self):
        reader1 = BlockingReader(value=1)
        sch, storage, clock, tmpdir = make_scheduler(
            datetime(2026, 6, 15, 13, 0), reader=reader1
        )
        t = run_tick_in_thread(sch)
        self.assertTrue(reader1.entered.wait(5))
        db_path = storage.path
        plan_before = storage.get_plan("2026-06-15")
        self.assertEqual(plan_before["status"], "running")
        storage.close()  # 模拟进程在读盘中被杀死
        reader1.release.set()
        t.join(timeout=5)

        # 重启：DB 里 plan 仍是 running
        storage2 = Storage(db_path)
        sch2 = Scheduler(storage2, "/volume", disk_reader=lambda p: 3000,
                         clock=clock)
        sch2.tick()  # 识别中断并沿用原计划重新执行
        result = storage2.get_result("2026-06-15")
        self.assertIsNotNone(result)
        self.assertEqual(result["available_bytes"], 3000)
        self.assertEqual(
            result["planned_utc"], plan_before["planned_utc"],
            "已开始过的巡检仍用原计划时间",
        )
        self.assertEqual(
            result["started_utc"], plan_before["started_utc"],
            "中断续跑保留原始开始时间",
        )

    def test_restart_skips_date_that_already_has_result(self):
        sch, storage, clock, tmpdir = make_scheduler(
            datetime(2026, 6, 15, 13, 0), reader=lambda p: 7
        )
        sch.tick()
        self.assertIsNotNone(storage.get_result("2026-06-15"))
        db_path = storage.path
        storage.close()

        storage2 = Storage(db_path)
        calls = []
        sch2 = Scheduler(storage2, "/volume",
                         disk_reader=lambda p: calls.append(1) or 8,
                         clock=clock)
        sch2.tick()
        self.assertEqual(calls, [], "当天已有结果，重启不重复读盘")

    def test_spring_forward_day_uses_deferred_instant(self):
        # 巡检时刻设为 02:30（gap 内），计划应落在 03:00 EDT = 07:00Z
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 3, 8, 6, 0), check_time="02:30"
        )
        sch.tick()
        plan = storage.get_plan("2026-03-08")
        self.assertEqual(plan["dst_note"], "gap_deferred")
        self.assertEqual(
            timeutil.from_iso(plan["planned_utc"]),
            datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
        )
        clock.advance(3600)  # 07:00Z
        sch.tick()
        self.assertIsNotNone(storage.get_result("2026-03-08"))

    def test_fall_back_day_uses_first_occurrence(self):
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 11, 1, 4, 0), check_time="01:30"
        )
        sch.tick()
        plan = storage.get_plan("2026-11-01")
        self.assertEqual(plan["dst_note"], "ambiguous_first")
        self.assertEqual(
            timeutil.from_iso(plan["planned_utc"]),
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        )

    def test_drill_clock_advance_and_set_persist(self):
        sch, storage, clock, _ = make_scheduler(datetime(2026, 6, 15, 12, 0))
        sch.advance_clock(90)
        self.assertEqual(
            sch.now(), clock.now + timedelta(seconds=90)
        )
        sch.set_clock(datetime(2026, 6, 15, 20, 0, tzinfo=UTC))
        self.assertEqual(
            sch.now_aware_utc(),
            datetime(2026, 6, 15, 20, 0, tzinfo=UTC),
        )
        # 偏移已落库
        self.assertNotEqual(storage.get_setting("clock_offset_seconds"), "0")
        sch.reset_clock()
        self.assertEqual(sch.now(), clock.now)

    def test_new_day_creates_new_plan_and_result_row(self):
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 6, 15, 13, 0), reader=lambda p: 1
        )
        sch.tick()
        self.assertIsNotNone(storage.get_result("2026-06-15"))
        # 拨到次日 14:00Z（纽约 10:00，已过 09:00）
        clock.advance(25 * 3600)
        sch.tick()
        self.assertIsNotNone(storage.get_plan("2026-06-16"))
        self.assertIsNotNone(storage.get_result("2026-06-16"))
        self.assertEqual(len(storage.list_recent_results()), 2)


if __name__ == "__main__":
    unittest.main()
