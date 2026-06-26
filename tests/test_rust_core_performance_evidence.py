import importlib.util
import json
from pathlib import Path

import pytest


def _load_validator():
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate-rust-core-performance-evidence.py"
    spec = importlib.util.spec_from_file_location("validate_rust_core_performance_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, events):
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def test_validate_performance_evidence_accepts_required_success_results(tmp_path):
    validator = _load_validator()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _write_jsonl(
        evidence_dir / "perf_pg_mysql_1m_migrate.jsonl",
        [{"event": "result", "success": True, "rows_copied": 1_000_000}],
    )
    _write_jsonl(
        evidence_dir / "perf_pg_mysql_1m_verify.jsonl",
        [{"event": "result", "success": True, "mismatches": []}],
    )
    _write_jsonl(
        evidence_dir / "perf_stress_10m_resume.jsonl",
        [{"event": "result", "success": True, "state": {"tables": [{"rows_copied": 10_000_000}]}}],
    )
    _write_jsonl(
        evidence_dir / "perf_stress_10m_verify.jsonl",
        [{"event": "result", "success": True, "mismatches": []}],
    )

    summary = validator.validate_evidence_dir(evidence_dir)

    assert summary["checked"] == 4
    assert summary["total_rows_proven"] == 11_000_000


def test_validate_performance_evidence_rejects_missing_result(tmp_path):
    validator = _load_validator()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    for filename in validator.REQUIRED_EVIDENCE:
        _write_jsonl(evidence_dir / filename, [{"event": "phase", "success": True}])

    with pytest.raises(validator.EvidenceError, match="missing successful result"):
        validator.validate_evidence_dir(evidence_dir)


def test_regression_gate_uses_archived_performance_evidence_validator():
    gate = Path(__file__).resolve().parents[1] / "scripts" / "rust-core-regression-gate.ps1"
    text = gate.read_text(encoding="utf-8")

    assert "validate-rust-core-performance-evidence.py" in text
    assert "reports/rust_core_performance" in text
    assert "migration_core/target/perf_pg_mysql_1m_migrate.jsonl" not in text
