import unittest
from datetime import datetime, timezone

from inspector.timeutil import (
    NY_TZ,
    UTC,
    InvalidTimeError,
    parse_hhmm,
    planned_instant,
)


class TimeutilTest(unittest.TestCase):
    def test_parse_hhmm(self):
        self.assertEqual(parse_hhmm("09:00"), (9, 0))
        self.assertEqual(parse_hhmm("23:59"), (23, 59))
        for bad in ("9", "24:00", "12:60", "ab:cd", ""):
            with self.assertRaises(InvalidTimeError):
                parse_hhmm(bad)

    def test_normal_day(self):
        # 2026-06-15 09:00 纽约（EDT，UTC-4）= 13:00Z
        d = datetime(2026, 6, 15).date()
        instant, note = planned_instant(d, "09:00")
        self.assertEqual(note, "normal")
        self.assertEqual(
            instant.astimezone(UTC),
            datetime(2026, 6, 15, 13, 0, tzinfo=UTC),
        )

    def test_spring_forward_gap_deferred_to_first_valid_minute(self):
        # 2026-03-08 02:00 -> 03:00 跳时；02:30 不存在，
        # 顺延到第一个有效分钟 03:00 EDT（UTC-4）= 07:00Z
        d = datetime(2026, 3, 8).date()
        instant, note = planned_instant(d, "02:30")
        self.assertEqual(note, "gap_deferred")
        self.assertEqual(
            instant.astimezone(UTC),
            datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
        )
        self.assertEqual(instant.strftime("%Y-%m-%d %H:%M"), "2026-03-08 03:00")
        self.assertEqual(instant.utcoffset().total_seconds(), -4 * 3600)

    def test_spring_forward_exactly_three_o_clock_is_valid(self):
        d = datetime(2026, 3, 8).date()
        instant, note = planned_instant(d, "03:00")
        self.assertEqual(note, "normal")
        self.assertEqual(
            instant.astimezone(UTC),
            datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
        )

    def test_fall_back_ambiguous_takes_first_occurrence(self):
        # 2026-11-01 02:00 -> 01:00 回退；01:30 出现两次。
        # 第一次为 EDT（UTC-4）= 05:30Z；第二次为 EST = 06:30Z。
        d = datetime(2026, 11, 1).date()
        instant, note = planned_instant(d, "01:30")
        self.assertEqual(note, "ambiguous_first")
        self.assertEqual(
            instant.astimezone(UTC),
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        )

    def test_fall_back_first_o_clock_still_normal(self):
        d = datetime(2026, 11, 1).date()
        instant, note = planned_instant(d, "00:30")
        self.assertEqual(note, "normal")
        self.assertEqual(
            instant.astimezone(UTC),
            datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
        )


if __name__ == "__main__":
    unittest.main()
