import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from inspector.app import create_app
from inspector.scheduler import Scheduler
from inspector.storage import Storage


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.storage = Storage(os.path.join(self.tmpdir, "web.db"))
        self.clock_now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

        def clock():
            return self.clock_now.replace(tzinfo=None)

        self.sch = Scheduler(
            self.storage, "/volume",
            disk_reader=lambda p: 555,
            clock=clock,
        )
        self.app = create_app(scheduler=self.sch)
        self.client = self.app.test_client()

    def test_index_shows_plan(self):
        self.sch.tick()  # 生成当天计划
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("2026-06-15", body)
        self.assertIn("09:00", body)
        self.assertIn("待执行", body)

    def test_set_check_time_via_form(self):
        resp = self.client.post("/check-time",
                                data={"check_time": "08:30"},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.storage.get_setting("check_time"), "08:30")
        plan = self.storage.get_plan("2026-06-15")
        self.assertEqual(plan["check_time"], "08:30")

    def test_invalid_check_time_flashes_error(self):
        resp = self.client.post("/check-time",
                                data={"check_time": "99:99"},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("时刻无效", resp.get_data(as_text=True))
        self.assertEqual(self.storage.get_setting("check_time"), "09:00")

    def test_drill_clock_controls(self):
        self.client.post("/clock/advance", data={"seconds": "3600"})
        # 12:00Z + 1h = 13:00Z，恰好到计划时间
        self.sch.tick()
        result = self.storage.get_result("2026-06-15")
        self.assertIsNotNone(result)
        self.assertEqual(result["available_bytes"], 555)

        resp = self.client.get("/")
        self.assertIn("555", resp.get_data(as_text=True))

        self.client.post("/clock/set",
                         data={"utc_time": "2026-06-15T20:00"})
        self.assertEqual(
            self.sch.now_aware_utc(),
            datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc),
        )
        self.client.post("/clock/reset")
        self.assertEqual(
            self.sch.now_aware_utc(),
            self.clock_now,
        )

    def test_read_error_displayed_on_page(self):
        def failing_reader(path):
            raise PermissionError("volume not mounted")

        self.sch.disk_reader = failing_reader
        self.client.post("/clock/advance", data={"seconds": "3600"})
        self.sch.tick()
        resp = self.client.get("/")
        body = resp.get_data(as_text=True)
        self.assertIn("读盘失败", body)
        self.assertIn("PermissionError", body)
        self.assertIn("volume not mounted", body)

    def test_cross_day_change_time_then_back_shows_new_plan(self):
        """首页试跨日改时刻：拨到次日改 11:00，拨回当天显示新计划。"""
        self.sch.tick()  # 06-15 计划 09:00
        # 拨到次日并保存新时刻
        self.clock_now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
        resp = self.client.post("/check-time",
                                data={"check_time": "11:00"},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("已更新为 11:00", resp.get_data(as_text=True))
        self.assertEqual(self.storage.get_plan("2026-06-16")["check_time"],
                         "11:00")

        # 拨回当天：首页计划必须是 11:00，而不是旧的 09:00
        self.clock_now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("11:00", body)
        plan = self.storage.get_plan("2026-06-15")
        self.assertEqual(plan["check_time"], "11:00")
        # 11:00 EDT = 15:00Z
        self.assertIn("2026-06-15T15:00:00+00:00", plan["planned_utc"])

    def test_save_failure_flashes_and_keeps_old_values(self):
        """保存失败：首页提示失败，设置和待执行计划保持原值。"""
        self.sch.tick()
        original_plan = dict(self.storage.get_plan("2026-06-15"))

        class FailingStorage:
            def __init__(self, inner):
                self.inner = inner
                self.path = inner.path

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def apply_check_time_change(self, hhmm, replans):
                raise sqlite3.OperationalError("simulated disk failure")

        self.sch.storage = FailingStorage(self.storage)
        try:
            resp = self.client.post("/check-time",
                                    data={"check_time": "11:00"},
                                    follow_redirects=True)
        finally:
            self.sch.storage = self.storage
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("保存失败", body)
        self.assertIn("保持原值", body)
        # 设置未变
        self.assertEqual(self.storage.get_setting("check_time"), "09:00")
        # 待执行计划未变
        plan = self.storage.get_plan("2026-06-15")
        self.assertEqual(plan["check_time"], original_plan["check_time"])
        self.assertEqual(plan["planned_utc"], original_plan["planned_utc"])
        self.assertEqual(plan["status"], "pending")
        # 输入框仍显示原时刻
        self.assertIn('value="09:00"', body)


if __name__ == "__main__":
    unittest.main()
