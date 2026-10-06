"""TF_LIVE_REQUIRED=1 turns a skipped test into a failure (tests/conftest.py), like the Rust live tests."""
import os
import subprocess
import sys
import uuid
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _run_skipping_test(live_required):
    probe = TESTS_DIR / f"test_zz_live_guard_probe_{uuid.uuid4().hex[:8]}.py"
    probe.write_text("import pytest\n\n@pytest.mark.skip(reason='no live server')\ndef test_live():\n    pass\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "TF_LIVE_REQUIRED"}
    if live_required:
        env["TF_LIVE_REQUIRED"] = "1"
    try:
        return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(probe)],
                              cwd=TESTS_DIR.parent, env=env, capture_output=True, text=True, timeout=120)
    finally:
        probe.unlink()


def test_skip_is_a_failure_only_when_live_tests_are_required():
    required = _run_skipping_test(True)
    # skipif marks skip during setup, which pytest reports as an error rather than a failure.
    assert required.returncode != 0 and ("1 failed" in required.stdout or "1 error" in required.stdout), required.stdout
    assert "skipped while TF_LIVE_REQUIRED=1" in required.stdout
    optional = _run_skipping_test(False)
    assert optional.returncode == 0 and "1 skipped" in optional.stdout, optional.stdout
