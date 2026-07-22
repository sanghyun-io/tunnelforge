#!/usr/bin/env python
"""Validate One-Click real-execution evidence v2."""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("validate_oneclick_dry_run", PROJECT_ROOT / "scripts" / "validate-oneclick-dry-run-evidence.py")
_base = importlib.util.module_from_spec(_spec); assert _spec.loader is not None; _spec.loader.exec_module(_base)
EvidenceError = _base.EvidenceError
def validate_report(report_path): return _base.validate_mutation_report(json.loads(Path(report_path).read_text(encoding="utf-8")), 138)
def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("report", nargs="?", default="reports/oneclick_readiness/oneclick-real-execution-evidence.json")
    try: print(validate_report(parser.parse_args().report))
    except (OSError, EvidenceError) as exc: print(f"One-Click evidence failed: {exc}"); return 1
    return 0
if __name__ == "__main__": raise SystemExit(main())
