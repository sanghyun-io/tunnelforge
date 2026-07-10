from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_GATE_PATH = PROJECT_ROOT / ".github" / "workflows" / "version-gate.yml"


def load_version_gate():
    workflow = yaml.safe_load(VERSION_GATE_PATH.read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 resolver parses the GitHub Actions `on` key as True.
    if True in workflow and "on" not in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def version_gate_job_text(job_name):
    workflow_text = VERSION_GATE_PATH.read_text(encoding="utf-8")
    job_start = workflow_text.index(f"  {job_name}:")
    next_job = workflow_text.find("\n  ", job_start + 1)
    while next_job != -1 and workflow_text[next_job + 3:next_job + 4] == " ":
        next_job = workflow_text.find("\n  ", next_job + 1)
    return workflow_text[job_start:] if next_job == -1 else workflow_text[job_start:next_job]


def test_version_gate_exposes_required_regression_jobs():
    jobs = load_version_gate()["jobs"]

    assert "rust-core-regression-gate" in jobs
    assert "python-regression" in jobs


def test_rust_core_regression_gate_contract_is_preserved():
    job = load_version_gate()["jobs"]["rust-core-regression-gate"]
    job_text = version_gate_job_text("rust-core-regression-gate")

    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 5
    assert "./scripts/rust-core-regression-gate.ps1" in job_text


def test_python_regression_runs_full_suite_with_built_core():
    job = load_version_gate()["jobs"]["python-regression"]
    job_text = version_gate_job_text("python-regression")

    assert job["runs-on"] == "windows-latest"
    assert job["timeout-minutes"] == 20
    assert job["env"]["QT_QPA_PLATFORM"] == "offscreen"
    assert str(job["env"]["PYTHONUTF8"]) == "1"
    assert "${{ github.event.pull_request.head.sha }}" in job_text
    assert "actions/setup-python@v6" in job_text
    assert 'python-version: "3.12"' in job_text
    assert "rustc --version" in job_text
    assert "cargo --version" in job_text
    assert 'pip install -e ".[dev]"' in job_text
    assert "cargo build --manifest-path migration_core/Cargo.toml --release" in job_text
    assert "pytest -q" in job_text
