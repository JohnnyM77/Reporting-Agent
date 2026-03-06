from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path


def _env_nonempty(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


LOW_THRESHOLD_PCT = float(_env_nonempty("WALLY_LOW_THRESHOLD_PCT", "5.0"))

STANDARD_WATCHLISTS = [
    "watchlists/tii_watchlist.yaml",
    "watchlists/jm_watchlist.yaml",
    "watchlists/aussie_tech_watchlist.yaml",
]
TII75_WATCHLIST = "watchlists/tii75_watchlist.yaml"

TII75_ANCHOR_ISO_WEEK = _env_int("TII75_ANCHOR_ISO_WEEK", 1)


@dataclass(frozen=True)
class EmailSettings:
    email_from: str
    email_to: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str


@dataclass(frozen=True)
class RunContext:
    run_dt: dt.datetime
    output_root: Path


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def build_run_context() -> RunContext:
    return RunContext(run_dt=now_utc(), output_root=Path("outputs") / now_utc().date().isoformat())


def load_email_settings() -> EmailSettings:
    # Reuse Bob's names where possible, with fallbacks.
    email_from = _env_nonempty("EMAIL_FROM") or _env_nonempty("EMAIL_USER")
    email_to = _env_nonempty("EMAIL_TO")
    smtp_user = _env_nonempty("SMTP_USER") or email_from or ""
    smtp_password = _env_nonempty("SMTP_PASS") or _env_nonempty("EMAIL_APP_PASSWORD") or ""
    smtp_host = _env_nonempty("SMTP_HOST", "smtp.gmail.com")
    smtp_port = _env_int("SMTP_PORT", 465)

    missing = [
        name
        for name, value in {
            "EMAIL_FROM or EMAIL_USER": email_from,
            "EMAIL_TO": email_to,
            "SMTP_USER or EMAIL_FROM": smtp_user,
            "SMTP_PASS or EMAIL_APP_PASSWORD": smtp_password,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing email settings: {', '.join(missing)}")

    return EmailSettings(
        email_from=email_from or "",
        email_to=email_to or "",
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
    )


def should_run_tii75(today: dt.date, force: bool = False) -> bool:
    if force:
        return True
    iso_week = today.isocalendar().week
    return (iso_week - TII75_ANCHOR_ISO_WEEK) % 2 == 0
