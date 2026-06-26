from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_main_window_export_import_labels_match_rust_core_implementation():
    source = (PROJECT_ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")

    stale_shell_terms = [
        "Shell Export",
        "Shell Import",
        "_context_shell_export",
        "_context_shell_import",
    ]

    for term in stale_shell_terms:
        assert term not in source

    assert "Rust DB Core Export" in source
    assert "Rust DB Core Import" in source
    assert "_context_rust_core_export" in source
    assert "_context_rust_core_import" in source
