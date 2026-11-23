import os
import sqlite3
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

    def test_change_time_on_another_date_replans_pending_dates(self):
        """值班场景：拨到次日改时刻，再拨回当天，计划与触发都以新时刻为准。"""
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 6, 15, 12, 0), check_time="09:00"
        )
        sch.tick()  # 当天 09:00 -> 13:00Z
        # 拨到次日（纽约 06-16 08:00），改时刻为 11:00
        clock.advance(24 * 3600)
        sch.set_check_time("11:00")
        sch.tick()
        self.assertEqual(storage.get_plan("2026-06-16")["check_time"], "11:00")
        self.assertEqual(
            timeutil.from_iso(storage.get_plan("2026-06-16")["planned_utc"]),
            datetime(2026, 6, 16, 15, 0, tzinfo=UTC),
        )

        # 拨回当天：首页状态里的计划已按新时刻重排
        clock.now = datetime(2026, 6, 15, 12, 0)
        st = sch.status()
        self.assertEqual(st["check_time"], "11:00")
        self.assertEqual(st["plan"]["check_time"], "11:00")
        self.assertEqual(
            timeutil.from_iso(st["plan"]["planned_utc"]),
            datetime(2026, 6, 15, 15, 0, tzinfo=UTC),
        )
        # 旧时刻 13:00Z 不应再触发
        clock.advance(3600)
        sch.tick()
        self.assertIsNone(storage.get_result("2026-06-15"))
        # 新时刻 15:00Z 才触发
        clock.advance(2 * 3600)
        sch.tick()
        result = storage.get_result("2026-06-15")
        self.assertIsNotNone(result)
        self.assertEqual(result["check_time"], "11:00")
        self.assertEqual(
            timeutil.from_iso(result["planned_utc"]),
            datetime(2026, 6, 15, 15, 0, tzinfo=UTC),
        )

    def test_set_time_replans_all_pending_but_keeps_started_and_done(self):
        reader = BlockingReader(value=77)
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 6, 15, 13, 0), reader=reader
        )
        # 当天到点开始读盘并阻塞
        t = run_tick_in_thread(sch)
        self.assertTrue(reader.entered.wait(5))
        self.assertEqual(storage.get_plan("2026-06-15")["status"], "running")
        # 拨到次日，通过 status 生成一条 pending 计划（不能 tick：
        # 次日 09:00 已到点，tick 会再次进入同一个阻塞读盘器）
        clock.advance(24 * 3600)
        sch.status()
        self.assertEqual(storage.get_plan("2026-06-16")["status"], "pending")

        # 改时刻：running 的 06-15 不动，pending 的 06-16 重排
        sch.set_check_time("07:30")
        plan_started = storage.get_plan("2026-06-15")
        self.assertEqual(plan_started["check_time"], "09:00")
        self.assertEqual(plan_started["status"], "running")
        self.assertEqual(storage.get_plan("2026-06-16")["check_time"],
                         "07:30")

        reader.release.set()
        t.join(timeout=5)
        result = storage.get_result("2026-06-15")
        self.assertEqual(result["check_time"], "09:00",
                         "已开始的巡检结果仍记原时刻")
        # 已有结果的日期再改时刻也不被覆盖
        sch.set_check_time("06:00")
        self.assertEqual(storage.get_plan("2026-06-15")["check_time"],
                         "09:00")
        self.assertEqual(storage.get_plan("2026-06-16")["check_time"],
                         "06:00")

    def test_restart_then_change_time_keeps_interrupted_plan(self):
        """重启后计划停在 running，即使先改时刻也照原计划补做。"""
        reader1 = BlockingReader(value=1)
        sch, storage, clock, tmpdir = make_scheduler(
            datetime(2026, 6, 15, 13, 0), reader=reader1
        )
        t = run_tick_in_thread(sch)
        self.assertTrue(reader1.entered.wait(5))
        db_path = storage.path
        plan_before = storage.get_plan("2026-06-15")
        storage.close()  # 读盘中进程被杀
        reader1.release.set()
        t.join(timeout=5)

        storage2 = Storage(db_path)
        sch2 = Scheduler(storage2, "/volume", disk_reader=lambda p: 4242,
                         clock=clock)
        # 重启后、补做前先改时刻：中断计划必须保留原时刻
        sch2.set_check_time("11:00")
        plan_after = storage2.get_plan("2026-06-15")
        self.assertEqual(plan_after["check_time"],
                         plan_before["check_time"])
        self.assertEqual(plan_after["planned_utc"],
                         plan_before["planned_utc"])
        self.assertEqual(plan_after["started_utc"],
                         plan_before["started_utc"])
        self.assertEqual(plan_after["status"], "running")

        sch2.tick()  # 按原计划补做
        result = storage2.get_result("2026-06-15")
        self.assertIsNotNone(result)
        self.assertEqual(result["available_bytes"], 4242)
        self.assertEqual(result["planned_utc"], plan_before["planned_utc"])
        self.assertEqual(result["started_utc"], plan_before["started_utc"])

    def test_set_time_storage_failure_keeps_setting_and_plans(self):
        """保存失败时设置与待执行计划都保持原值，并向上抛异常。"""
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 6, 15, 12, 0), check_time="09:00"
        )
        sch.tick()
        original_plan = dict(storage.get_plan("2026-06-15"))

        # 事务写设置成功后、提交前注入失败：验证整笔事务回滚。
        storage.fail_next_check_time_change = True
        with self.assertRaises(sqlite3.OperationalError):
            sch.set_check_time("11:00")

        self.assertEqual(storage.get_setting("check_time"), "09:00")
        plan = storage.get_plan("2026-06-15")
        self.assertEqual(plan["check_time"], original_plan["check_time"])
        self.assertEqual(plan["planned_utc"], original_plan["planned_utc"])
        self.assertEqual(plan["status"], "pending")

    def test_status_builds_today_plan_after_date_switch_without_tick(self):
        """切换纽约当地日期后，不经过 tick 首页也能看到当天计划。"""
        sch, storage, clock, _ = make_scheduler(
            datetime(2026, 6, 15, 12, 0), check_time="09:00"
        )
        sch.tick()
        clock.advance(48 * 3600)  # 跳到 06-17
        st = sch.status()
        self.assertEqual(st["today"], "2026-06-17")
        self.assertIsNotNone(st["plan"])
        self.assertEqual(st["plan"]["check_time"], "09:00")
        self.assertEqual(st["plan"]["local_date"], "2026-06-17")


if __name__ == "__main__":
    unittest.main()
