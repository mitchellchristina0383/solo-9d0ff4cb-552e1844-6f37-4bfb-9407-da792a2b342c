"""纽约时区与 DST（夏令时）排程工具。

规则：
- 春季跳时（gap）中不存在的当地时刻，顺延到跳时后的第一个有效分钟；
- 秋季重复（fold）的当地时刻，取第一次出现（fold=0）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc


class InvalidTimeError(ValueError):
    """用户输入的 HH:MM 不合法。"""


def parse_hhmm(value: str) -> tuple[int, int]:
    """解析 HH:MM（24 小时制），失败抛 InvalidTimeError。"""
    if not isinstance(value, str):
        raise InvalidTimeError("时刻必须是 HH:MM 字符串")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise InvalidTimeError("时刻格式应为 HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise InvalidTimeError("时刻必须是数字") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise InvalidTimeError("时刻超出范围（00:00–23:59）")
    return hour, minute


def is_valid_local_naive(naive: datetime, tz=NY_TZ) -> bool:
    """该 naive 当地时间在该时区是否真实存在（往返校验）。"""
    aware = naive.replace(tzinfo=tz, fold=0)
    roundtrip = aware.astimezone(UTC).astimezone(tz)
    return roundtrip.replace(tzinfo=None) == naive


def planned_instant(local_date, hhmm: str, tz=NY_TZ) -> datetime:
    """计算某当地日期、某 HH:MM 巡检时刻对应的真实绝对时间（带时区）。

    返回的 datetime 带 tzinfo；并通过 note 字段说明 DST 处理：
    - "normal"：普通时刻；
    - "ambiguous_first"：秋季重复时刻，取第一次；
    - "gap_deferred"：春季跳过的时刻，顺延到第一个有效分钟。
    """
    hour, minute = parse_hhmm(hhmm)
    naive = datetime(
        local_date.year, local_date.month, local_date.day, hour, minute
    )
    aware = naive.replace(tzinfo=tz, fold=0)
    roundtrip = aware.astimezone(UTC).astimezone(tz)
    if roundtrip.replace(tzinfo=None) == naive:
        # fold=0 在秋季重复时天然表示第一次出现。
        note = "ambiguous_first" if aware.fold == 0 and _is_ambiguous(naive, tz) else "normal"
        return aware, note

    # 春季 gap：不存在的当地时间，逐分钟向后找第一个有效分钟。
    for step in range(1, 24 * 60):
        candidate_naive = naive + timedelta(minutes=step)
        candidate = candidate_naive.replace(tzinfo=tz, fold=0)
        rt = candidate.astimezone(UTC).astimezone(tz)
        if rt.replace(tzinfo=None) == candidate_naive:
            return candidate, "gap_deferred"
    raise RuntimeError(f"{local_date} {hhmm} 之后找不到有效分钟")


def _is_ambiguous(naive: datetime, tz) -> bool:
    """fold=0 与 fold=1 解析出不同 UTC 偏移，即为秋季重复时刻。"""
    first = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    second = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    return first != second


def to_utc_iso(aware: datetime) -> str:
    return aware.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
