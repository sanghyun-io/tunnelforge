from pathlib import Path

from src.core.path_safety import safe_output_dir


def test_safe_output_dir_keeps_parent_reference_inside_base(tmp_path):
    output = Path(safe_output_dir(str(tmp_path), "..")).resolve()

    assert output.is_relative_to(tmp_path.resolve())
    assert output != tmp_path.parent.resolve()
    assert output.name.startswith("export_")


def test_safe_output_dir_replaces_unsafe_filename_characters(tmp_path):
    output = Path(safe_output_dir(str(tmp_path), 'prod:db/table\\name*?"<>|')).resolve()

    assert output == tmp_path.resolve() / "prod_db_table_name______"


def test_safe_output_dir_expands_user_base_and_returns_string(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))

    output = Path(safe_output_dir("~", "dump")).resolve()

    assert output == home.resolve() / "dump"
