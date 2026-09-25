"""Where Harry Hindsight keeps personal data, and the SQLite behind it.

The repo is public. Case files hold position details and notes about
Johnny's tendencies, so they must never land in a tracked path. Two
backends:

- ``local``: ``hindsight_data/`` at the repo root (gitignored). Dev and tests.
- ``private_repo``: a checkout of a separate private repo, at
  ``HINDSIGHT_PRIVATE_REPO_DIR``. The workflow commits it back after a run.

Both refuse to start if the directory would be tracked by the public repo.
A misconfigured ``private_repo`` fails loudly; it never falls back to local.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from .config import REPO_ROOT

T = TypeVar("T", bound=BaseModel)

TABLES = (
    "company",
    "position",
    "thesis_snapshot",
    "seven_powers_assessment",
    "bias_assessment",
    "hindsight_event",
    "hindsight_report",
    "hindsight_question",
    "hindsight_warning",
    "hindsight_outcome",
    "autopsy",
    "johnny_response",
    "seen_event",
    "kv",
)


class StoreConfigError(RuntimeError):
    """The store is not configured safely. Nothing has been written."""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _ignored_by_public_repo(path: Path, repo_root: Path) -> bool:
    """True if git would ignore ``path`` in the public repo."""
    probe = path / "probe.txt"
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", str(probe)],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _assert_not_tracked(path: Path, repo_root: Path) -> None:
    if _is_within(path, repo_root) and (repo_root / ".git").exists():
        if not _ignored_by_public_repo(path, repo_root):
            raise StoreConfigError(
                f"Refusing to use {path} as the Hindsight store: it sits inside the "
                f"public repo and is not gitignored. Personal data would be committed."
            )


class Store:
    """Filesystem layout + SQLite. Create via :func:`open_store`."""

    def __init__(self, root: Path, backend: str):
        self.root = root
        self.backend = backend
        for sub in ("case_files", "reports", "profile", "runs", "data", "emit"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        self.db_path = root / "hindsight.db"
        self.conn = sqlite3.connect(self.db_path)
        for table in TABLES:
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "id TEXT PRIMARY KEY, ticker TEXT, created_at TEXT, status TEXT, data TEXT)"
            )
        self.conn.commit()

    # -- generic record access --------------------------------------------

    def put(self, table: str, record: BaseModel) -> None:
        d = record.model_dump(mode="json", by_alias=True)
        self.conn.execute(
            f"INSERT OR REPLACE INTO {table} (id, ticker, created_at, status, data) VALUES (?,?,?,?,?)",
            (d.get("id") or d.get("event_id"), d.get("ticker", ""), d.get("created_at") or d.get("timestamp", ""),
             d.get("status", ""), json.dumps(d)),
        )
        self.conn.commit()

    def get(self, table: str, model: Type[T], record_id: str) -> T | None:
        row = self.conn.execute(f"SELECT data FROM {table} WHERE id=?", (record_id,)).fetchone()
        return model.model_validate(json.loads(row[0])) if row else None

    def query(self, table: str, model: Type[T], ticker: str | None = None, status: str | None = None) -> list[T]:
        sql, args = f"SELECT data FROM {table} WHERE 1=1", []
        if ticker is not None:
            sql += " AND ticker=?"
            args.append(ticker.upper())
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at"
        return [model.model_validate(json.loads(r[0])) for r in self.conn.execute(sql, args).fetchall()]

    def set_status(self, table: str, record_id: str, status: str) -> None:
        row = self.conn.execute(f"SELECT data FROM {table} WHERE id=?", (record_id,)).fetchone()
        if not row:
            return
        d = json.loads(row[0])
        d["status"] = status
        self.conn.execute(f"UPDATE {table} SET status=?, data=? WHERE id=?", (status, json.dumps(d), record_id))
        self.conn.commit()

    # -- dedup ---------------------------------------------------------------

    def seen(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen_event WHERE id=?", (key,)).fetchone() is not None

    def mark_seen(self, key: str, ticker: str, report_id: str, when: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO seen_event (id, ticker, created_at, status, data) VALUES (?,?,?,?,?)",
            (key, ticker, when, "SEEN", json.dumps({"report_id": report_id})),
        )
        self.conn.commit()

    # -- small key/value state (triage baselines, schedule markers) ----------

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT data FROM kv WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kv (id, ticker, created_at, status, data) VALUES (?,?,?,?,?)",
            (key, "", "", "", json.dumps(value)),
        )
        self.conn.commit()

    # -- files ---------------------------------------------------------------

    def write_text(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def append_text(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def close(self) -> None:
        self.conn.close()


def open_store(cfg: dict | None = None, repo_root: Path = REPO_ROOT) -> Store:
    cfg = cfg or {}
    store_cfg = cfg.get("store", {})
    backend = (os.environ.get("HINDSIGHT_STORE") or store_cfg.get("backend") or "local").strip()

    if backend == "local":
        root = Path(os.environ.get("HINDSIGHT_DATA_DIR") or repo_root / store_cfg.get("local_dir", "hindsight_data"))
        _assert_not_tracked(root, repo_root)
        root.mkdir(parents=True, exist_ok=True)
        return Store(root, backend)

    if backend == "private_repo":
        raw = os.environ.get("HINDSIGHT_PRIVATE_REPO_DIR", "").strip()
        if not raw:
            raise StoreConfigError(
                "HINDSIGHT_STORE=private_repo but HINDSIGHT_PRIVATE_REPO_DIR is not set. "
                "Check out the private repo (secret HINDSIGHT_PRIVATE_REPO_TOKEN) and point "
                "HINDSIGHT_PRIVATE_REPO_DIR at it. Refusing to fall back to a tracked path."
            )
        root = Path(raw)
        if not root.is_dir() or not (root / ".git").exists():
            raise StoreConfigError(
                f"HINDSIGHT_STORE=private_repo but {root} is not a git checkout. "
                "The private repo checkout step failed or HINDSIGHT_PRIVATE_REPO_TOKEN is missing. "
                "Nothing was written."
            )
        _assert_not_tracked(root, repo_root)
        return Store(root, backend)

    raise StoreConfigError(f"Unknown HINDSIGHT_STORE backend {backend!r}; use 'local' or 'private_repo'.")


