from __future__ import annotations

from pathlib import Path


def sally_root() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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


def bob_tickers_path() -> Path:
    """Canonical portfolio file used by Bob at repository root."""
    return (repo_root() / "tickers.yaml").resolve()
