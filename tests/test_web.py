import os
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


if __name__ == "__main__":
    unittest.main()
