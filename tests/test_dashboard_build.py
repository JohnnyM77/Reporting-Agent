"""scripts/build_dashboard.py end to end, against the committed docs/data and
against an empty data dir. Section rendering lives in dashboard/; the I/O
stays in the script (other tests monkeypatch build_dashboard.DATA_DIR)."""

from __future__ import annotations

import importlib
import importlib.util
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_SECTIONS = ["bob", "slinger", "us", "wally", "sally", "harry", "theo", "ned", "transcripts"]


def _bd():
    spec = importlib.util.spec_from_file_location("bd_build", REPO_ROOT / "scripts" / "build_dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("with_data", [True, False])
def test_build_writes_every_section(tmp_path, monkeypatch, with_data):
    bd = _bd()
    data = tmp_path / "data"
    if with_data:
        shutil.copytree(REPO_ROOT / "docs" / "data", data)
    else:
        data.mkdir()
    monkeypatch.setattr(bd, "DATA_DIR", data)
    monkeypatch.setattr(bd, "DOCS_DIR", tmp_path)
    bd.build_dashboard()
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>") and html.rstrip().endswith("</html>")
    for name in ("Bob the Bot", "Wally", "Sally", "Harry Hindsight", "Theo", "Ned"):
        assert name in html


def test_each_agent_has_its_own_section_module():
    import dashboard.sections as sections_pkg

    for agent in AGENT_SECTIONS:
        mod = importlib.import_module(f"{sections_pkg.__name__}.{agent}")
        assert callable(getattr(mod, f"_{agent}_section")), agent


def test_section_modules_do_no_file_io():
    """Loading and history files stay in the script, where tests can redirect
    DATA_DIR; a section that opened docs/data itself would escape that."""
    for path in (REPO_ROOT / "dashboard").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "DATA_DIR" not in src and "write_text" not in src, path


