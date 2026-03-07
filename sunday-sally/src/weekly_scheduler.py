from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def run_window_info(timezone: str = "Asia/Singapore") -> dict:
    now_tz = dt.datetime.now(ZoneInfo(timezone))
    return {
        "run_time_local": now_tz.isoformat(),
        "weekday_local": now_tz.strftime("%A"),
        "timezone": timezone,
    }
