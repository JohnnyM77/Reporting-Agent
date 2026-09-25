from __future__ import annotations

import sys
from pathlib import Path


def sally_root() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def ensure_repo_root_on_path() -> None:
    """Sally runs from sunday-sally/ (``python -m src.main``); the repo-root
    packages she uses (``shared``, ``wally``) need the root on sys.path."""
    root = str(repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def resolve_existing_path(raw_path: str | None, *, base_dirs: list[Path]) -> Path:
    if not raw_path:
        raise ValueError("Path value is required")

    candidate = Path(raw_path)
    if candidate.is_absolute() and candidate.exists():
        return candidate

    search = [Path.cwd(), *base_dirs]
    for base in search:
        p = (base / candidate).resolve()
        if p.exists():
            return p

    return (base_dirs[0] / candidate).resolve()


def resolve_output_root(base_output_root: str) -> Path:
    p = Path(base_output_root)
    if p.is_absolute():
        return p
    return (sally_root() / p).resolve()
