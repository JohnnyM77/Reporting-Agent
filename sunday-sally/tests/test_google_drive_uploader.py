from pathlib import Path

from src.google_drive_uploader import _build_drive_filename


def test_build_drive_filename_flattens_relative_path():
    root = Path('/tmp/run')
    p = Path('/tmp/run/ABC/source_docs/announcement_index.json')
    name = _build_drive_filename(root, p, run_label='SundaySally_2026-01-04')
    assert name == 'SundaySally_2026-01-04__ABC__source_docs__announcement_index.json'
