#!/usr/bin/env python3
"""Verify the macOS support tracking gate before closing M6."""

from __future__ import annotations

import argparse
import glob
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MILESTONE_ISSUES = {
    110: "CLOSED",
    111: "CLOSED",
    112: "CLOSED",
    113: "CLOSED",
    114: "CLOSED",
    115: "CLOSED",
}
FINAL_ISSUE = 116
PR_NUMBER = 117


def run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)


def ok(message: str) -> None:
    print(f"OK: {message}")


def newest_report() -> Path | None:
    reports = sorted(
        (Path(path) for path in glob.glob(str(ROOT / "build" / "macos-manual-validation-report-*.md"))),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None


def bash_path(path: Path) -> str:
    if sys.platform == "win32" and path.is_absolute():
        resolved = path.resolve()
        drive = resolved.drive.rstrip(":").lower()
        rest = resolved.as_posix()[2:]
        return f"/mnt/{drive}{rest}"
    return path.as_posix()


def report_path_value(report: Path, prefix: str) -> str:
    for line in report.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def resolve_report_relative(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return ROOT / path


def check_manual_report(report: Path) -> bool:
    if shutil.which("bash") is None:
        fail("bash is required to run scripts/macos-manual-validation-report.sh --check-complete")
        return False

    result = run(
        [
            "bash",
            "scripts/macos-manual-validation-report.sh",
            "--check-complete",
            bash_path(report),
        ]
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        fail(f"manual validation report is incomplete: {report}")
        return False

    ok(f"manual validation report is complete: {report}")
    return True


def default_bundle_for_report(report: Path) -> Path:
    return ROOT / "build" / f"macos-manual-validation-evidence-{report.stem}.zip"


def check_evidence_bundle(report: Path, bundle: Path) -> bool:
    if not bundle.is_file():
        fail(f"evidence bundle not found: {bundle}")
        return False

    smoke_log_value = report_path_value(report, "- Smoke log:")
    if not smoke_log_value:
        fail("manual validation report does not include a smoke log path")
        return False

    smoke_log = resolve_report_relative(smoke_log_value)
    if not smoke_log.is_file():
        fail(f"smoke log referenced by report is missing: {smoke_log}")
        return False

    expected_names = sorted([report.name, smoke_log.name])
    try:
        with zipfile.ZipFile(bundle) as archive:
            names = sorted(archive.namelist())
            if names != expected_names:
                fail(f"evidence bundle must contain exactly {expected_names}, found {names}")
                return False
            if archive.read(report.name) != report.read_bytes():
                fail(f"evidence bundle report does not match {report}")
                return False
            if archive.read(smoke_log.name) != smoke_log.read_bytes():
                fail(f"evidence bundle smoke log does not match {smoke_log}")
                return False
    except zipfile.BadZipFile:
        fail(f"evidence bundle is not a valid zip file: {bundle}")
        return False

    ok(f"evidence bundle is complete: {bundle}")
    return True


def gh_json(command: list[str]) -> dict:
    result = run(["gh", *command], check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh command failed")
    return json.loads(result.stdout)


def resolve_repo(explicit_repo: str | None) -> str:
    if explicit_repo:
        return explicit_repo

    data = gh_json(["repo", "view", "--json", "nameWithOwner"])
    return data["nameWithOwner"]


def check_issues(repo: str, final: bool) -> bool:
    passed = True
    for issue_number, expected_state in MILESTONE_ISSUES.items():
        issue = gh_json(
            [
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "state,milestone,title",
            ]
        )
        if issue["state"] != expected_state:
            fail(f"#{issue_number} must be {expected_state.lower()}, found {issue['state'].lower()}")
            passed = False
        else:
            ok(f"#{issue_number} is {expected_state.lower()}")

    final_issue = gh_json(
        [
            "issue",
            "view",
            str(FINAL_ISSUE),
            "--repo",
            repo,
            "--json",
            "state,milestone,title",
        ]
    )
    milestone_title = (final_issue.get("milestone") or {}).get("title", "")
    if "macOS Support M6" not in milestone_title:
        fail(f"#{FINAL_ISSUE} is not assigned to the M6 milestone")
        passed = False
    else:
        ok(f"#{FINAL_ISSUE} is assigned to {milestone_title}")

    if final:
        if final_issue["state"] == "OPEN":
            ok(f"#{FINAL_ISSUE} is still open; close it after attaching the completed report and smoke log")
        elif final_issue["state"] == "CLOSED":
            ok(f"#{FINAL_ISSUE} is closed")
        else:
            fail(f"#{FINAL_ISSUE} has unexpected state {final_issue['state']}")
            passed = False
    elif final_issue["state"] != "OPEN":
        fail(f"#{FINAL_ISSUE} should remain open until real-Mac validation evidence is attached")
        passed = False
    else:
        ok(f"#{FINAL_ISSUE} remains open for final real-Mac validation")

    return passed


def check_pr(repo: str, skip_checks: bool) -> bool:
    pr = gh_json(
        [
            "pr",
            "view",
            str(PR_NUMBER),
            "--repo",
            repo,
            "--json",
            "headRefOid,isDraft,mergeStateStatus,statusCheckRollup",
        ]
    )
    passed = True

    if skip_checks:
        ok("PR merge state skipped by request")
    elif pr["mergeStateStatus"] != "CLEAN":
        fail(f"PR #{PR_NUMBER} merge state is {pr['mergeStateStatus']}, expected CLEAN")
        passed = False
    else:
        ok(f"PR #{PR_NUMBER} merge state is clean")

    if skip_checks:
        ok("PR status checks skipped by request")
    else:
        for check in pr["statusCheckRollup"]:
            name = check.get("name", "<unnamed>")
            status = check.get("status")
            conclusion = check.get("conclusion")
            if conclusion == "SKIPPED":
                ok(f"PR check skipped: {name}")
                continue
            if status != "COMPLETED" or conclusion != "SUCCESS":
                fail(f"PR check not green: {name} status={status} conclusion={conclusion}")
                passed = False
            else:
                ok(f"PR check green: {name}")

    if pr["isDraft"]:
        ok(f"PR #{PR_NUMBER} is still draft while final real-Mac evidence is pending")
    else:
        ok(f"PR #{PR_NUMBER} is ready for review")

    return passed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the macOS support milestone, PR, and manual validation gate."
    )
    parser.add_argument(
        "--final",
        action="store_true",
        help="Require a completed real-Mac manual validation report and smoke log.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Path to build/macos-manual-validation-report-*.md.",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        help="Path to build/macos-manual-validation-evidence-*.zip.",
    )
    parser.add_argument(
        "--repo",
        help="GitHub repository in owner/name form. Defaults to gh repo view.",
    )
    parser.add_argument(
        "--skip-github",
        action="store_true",
        help="Only check local report/log evidence.",
    )
    parser.add_argument(
        "--skip-pr-checks",
        action="store_true",
        help="Skip PR merge-state and status-rollup checks. Useful when running as a PR check itself.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    passed = True

    if args.final:
        report = args.report or newest_report()
        if report is None:
            fail("no macOS manual validation report found under build/")
            passed = False
        else:
            passed = check_manual_report(report) and passed
            bundle = args.bundle or default_bundle_for_report(report)
            passed = check_evidence_bundle(report, bundle) and passed
    elif args.report:
        passed = check_manual_report(args.report) and passed
        if args.bundle:
            passed = check_evidence_bundle(args.report, args.bundle) and passed
    elif args.bundle:
        fail("--bundle requires --report unless --final resolves a report automatically")
        passed = False
    else:
        ok("manual validation report is optional unless --final is used")

    if not args.skip_github:
        if shutil.which("gh") is None:
            fail("gh is required unless --skip-github is used")
            passed = False
        else:
            try:
                repo = resolve_repo(args.repo)
                passed = check_issues(repo, args.final) and passed
                passed = check_pr(repo, args.skip_pr_checks) and passed
            except RuntimeError as exc:
                fail(str(exc))
                passed = False

    if passed:
        ok("macOS support gate checks passed")
        return 0

    fail("macOS support gate checks failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
