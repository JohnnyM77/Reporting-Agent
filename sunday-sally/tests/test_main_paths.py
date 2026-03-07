from pathlib import Path

from src.pathing import repo_root, resolve_existing_path, resolve_output_root, sally_root


def test_resolve_existing_path_finds_repo_tickers_from_sunday_dir():
    resolved = resolve_existing_path("tickers.yaml", base_dirs=[repo_root(), sally_root()])
    assert resolved.name == "tickers.yaml"
    assert resolved.exists()


def test_resolve_output_root_is_under_sally_root_for_relative_path():
    out = resolve_output_root("data/outputs")
    assert out == (sally_root() / "data/outputs").resolve()


def test_resolve_existing_path_prefers_absolute_when_exists(tmp_path: Path):
    f = tmp_path / "x.yaml"
    f.write_text("a: 1", encoding="utf-8")
    resolved = resolve_existing_path(str(f), base_dirs=[Path.cwd()])
    assert resolved == f
