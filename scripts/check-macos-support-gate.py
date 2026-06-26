#!/usr/bin/env python3
"""Verify the macOS support tracking gate before closing M6."""

from __future__ import annotations

import argparse
import glob
import hashlib
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
MANUAL_MACOS_WORKFLOW = "macOS App Validation"
MANUAL_MACOS_WORKFLOW_EVENT = "workflow_dispatch"
MANUAL_MACOS_VERIFY_STEP = "Verify signed and notarized artifacts"
MANUAL_MACOS_REQUIRED_ARCHES = ("arm64", "x86_64")


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


def repo_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return ROOT / path


def resolve_path_arg(values: list[str] | None, label: str) -> tuple[Path | None, bool]:
    if not values:
        return None, True

    paths: list[Path] = []
    for value in values:
        if glob.has_magic(value):
            pattern = value if Path(value).is_absolute() else str(ROOT / value)
            matches = [Path(path) for path in glob.glob(pattern)]
            if not matches:
                fail(f"no {label} matched pattern: {value}")
                return None, False
            paths.extend(matches)
        else:
            paths.append(repo_path(Path(value)))

    if len(paths) == 1:
        return paths[0], True

    existing = [path for path in paths if path.exists()]
    if not existing:
        fail(f"no {label} candidates exist: {', '.join(str(path) for path in paths)}")
        return None, False

    selected = max(existing, key=lambda path: path.stat().st_mtime)
    ok(f"selected newest {label}: {selected}")
    return selected, True


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


def local_head_sha() -> str | None:
    result = run(["git", "rev-parse", "HEAD"])
    if result.returncode != 0:
        fail("could not resolve local HEAD SHA")
        return None
    return result.stdout.strip()


def check_report_git_sha(report: Path, expected_sha: str, expected_label: str) -> bool:
    report_sha = report_path_value(report, "- Git SHA:")
    if not report_sha or report_sha == "unknown":
        fail("manual validation report must include a concrete Git SHA")
        return False

    report_sha = report_sha.lower()
    expected_sha = expected_sha.lower()
    if expected_sha.startswith(report_sha) or report_sha.startswith(expected_sha):
        ok(f"manual validation report Git SHA matches {expected_label}: {report_sha}")
        return True

    fail(f"manual validation report Git SHA {report_sha} does not match {expected_label} {expected_sha}")
    return False


def check_report_artifact_workflow_run(report: Path, expected_run_id: str, expected_label: str) -> bool:
    report_run_id = report_path_value(report, "- Artifact workflow run:")
    if not report_run_id:
        fail("manual validation report must include an Artifact workflow run id")
        return False

    if report_run_id == str(expected_run_id):
        ok(f"manual validation report Artifact workflow run matches {expected_label}: {report_run_id}")
        return True

    fail(
        f"manual validation report Artifact workflow run {report_run_id} "
        f"does not match {expected_label} {expected_run_id}"
    )
    return False


def default_bundle_for_report(report: Path) -> Path:
    return ROOT / "build" / f"macos-manual-validation-evidence-{report.stem}.zip"


def evidence_manifest_name(report: Path) -> str:
    return f"macos-manual-validation-evidence-{report.stem}.sha256"


def evidence_bundle_checksum_path(bundle: Path) -> Path:
    return Path(f"{bundle}.sha256")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_evidence_bundle(report: Path, bundle: Path) -> bool:
    if not bundle.is_file():
        fail(f"evidence bundle not found: {bundle}")
        return False

    checksum_path = evidence_bundle_checksum_path(bundle)
    if not checksum_path.is_file():
        fail(f"evidence bundle checksum not found: {checksum_path}")
        return False

    bundle_bytes = bundle.read_bytes()
    expected_bundle_checksum = f"{sha256_hex(bundle_bytes)}  {bundle.name}\n"
    if checksum_path.read_text(encoding="utf-8") != expected_bundle_checksum:
        fail(f"evidence bundle checksum does not match {bundle}")
        return False

    smoke_log_value = report_path_value(report, "- Smoke log:")
    if not smoke_log_value:
        fail("manual validation report does not include a smoke log path")
        return False

    smoke_log = resolve_report_relative(smoke_log_value)
    if not smoke_log.is_file():
        fail(f"smoke log referenced by report is missing: {smoke_log}")
        return False

    system_evidence_value = report_path_value(report, "- System evidence log:")
    if not system_evidence_value:
        fail("manual validation report does not include a system evidence log path")
        return False

    system_evidence_log = resolve_report_relative(system_evidence_value)
    if not system_evidence_log.is_file():
        fail(f"system evidence log referenced by report is missing: {system_evidence_log}")
        return False

    manifest_name = evidence_manifest_name(report)
    expected_names = sorted([report.name, smoke_log.name, system_evidence_log.name, manifest_name])
    try:
        with zipfile.ZipFile(bundle) as archive:
            names = sorted(archive.namelist())
            if names != expected_names:
                fail(f"evidence bundle must contain exactly {expected_names}, found {names}")
                return False
            report_bytes = report.read_bytes()
            smoke_log_bytes = smoke_log.read_bytes()
            system_evidence_log_bytes = system_evidence_log.read_bytes()
            if archive.read(report.name) != report_bytes:
                fail(f"evidence bundle report does not match {report}")
                return False
            if archive.read(smoke_log.name) != smoke_log_bytes:
                fail(f"evidence bundle smoke log does not match {smoke_log}")
                return False
            if archive.read(system_evidence_log.name) != system_evidence_log_bytes:
                fail(f"evidence bundle system evidence log does not match {system_evidence_log}")
                return False
            expected_manifest = (
                f"{sha256_hex(report_bytes)}  {report.name}\n"
                f"{sha256_hex(smoke_log_bytes)}  {smoke_log.name}\n"
                f"{sha256_hex(system_evidence_log_bytes)}  {system_evidence_log.name}\n"
            )
            if archive.read(manifest_name).decode("utf-8") != expected_manifest:
                fail(f"evidence bundle manifest does not match report/log hashes: {manifest_name}")
                return False
    except zipfile.BadZipFile:
        fail(f"evidence bundle is not a valid zip file: {bundle}")
        return False

    ok(f"evidence bundle is complete with manifest and checksum: {bundle}")
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
            ok(f"#{FINAL_ISSUE} is still open; close it after attaching the completed report, smoke log, and system evidence log")
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
            "headRefOid,isDraft,mergeStateStatus,state,statusCheckRollup",
        ]
    )
    passed = True
    pr_state = pr.get("state")

    if skip_checks:
        ok("PR merge state skipped by request")
    elif pr_state == "MERGED":
        ok(f"PR #{PR_NUMBER} is merged; merge-state cleanliness is no longer required")
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

    if pr_state == "MERGED":
        ok(f"PR #{PR_NUMBER} is merged")
    elif pr["isDraft"]:
        ok(f"PR #{PR_NUMBER} is still draft while final real-Mac evidence is pending")
    else:
        ok(f"PR #{PR_NUMBER} is ready for review")

    return passed


def check_manual_macos_validation_workflow(repo: str) -> tuple[bool, str | None]:
    head_sha = pr_head_sha(repo)
    runs = gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            MANUAL_MACOS_WORKFLOW,
            "--event",
            MANUAL_MACOS_WORKFLOW_EVENT,
            "--limit",
            "20",
            "--json",
            "databaseId,headSha,status,conclusion,url",
        ]
    )

    matching_runs = [
        run
        for run in runs
        if run.get("headSha") == head_sha
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    ]
    if not matching_runs:
        fail(
            f"no successful manual {MANUAL_MACOS_WORKFLOW} {MANUAL_MACOS_WORKFLOW_EVENT} "
            f"run found for PR head {head_sha}"
        )
        return False, None

    run_id = str(matching_runs[0]["databaseId"])
    run = gh_json(
        [
            "run",
            "view",
            run_id,
            "--repo",
            repo,
            "--json",
            "jobs,url",
        ]
    )
    jobs = run.get("jobs", [])
    passed = True

    for arch in MANUAL_MACOS_REQUIRED_ARCHES:
        job = next((job for job in jobs if arch in job.get("name", "")), None)
        if job is None:
            fail(f"manual macOS validation run {run_id} is missing {arch} job")
            passed = False
            continue

        if job.get("status") != "completed" or job.get("conclusion") != "success":
            fail(
                f"manual macOS validation {arch} job is not green: "
                f"status={job.get('status')} conclusion={job.get('conclusion')}"
            )
            passed = False
            continue

        verify_step = next(
            (step for step in job.get("steps", []) if step.get("name") == MANUAL_MACOS_VERIFY_STEP),
            None,
        )
        if verify_step is None:
            fail(f"manual macOS validation {arch} job is missing {MANUAL_MACOS_VERIFY_STEP!r} step")
            passed = False
            continue

        if verify_step.get("status") != "completed" or verify_step.get("conclusion") != "success":
            fail(
                f"manual macOS validation {arch} signing/notarization step is not green: "
                f"status={verify_step.get('status')} conclusion={verify_step.get('conclusion')}"
            )
            passed = False
        else:
            ok(f"manual macOS signing/notarization workflow passed for {arch}")

    if passed:
        ok(f"manual macOS signing/notarization workflow passed: {run['url']}")

    return passed, run_id


def pr_head_sha(repo: str) -> str:
    pr = gh_json(
        [
            "pr",
            "view",
            str(PR_NUMBER),
            "--repo",
            repo,
            "--json",
            "headRefOid",
        ]
    )
    return pr["headRefOid"]


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
        nargs="+",
        help="Path or glob for build/macos-manual-validation-report-*.md. Multiple matches select the newest file.",
    )
    parser.add_argument(
        "--bundle",
        nargs="+",
        help="Path or glob for build/macos-manual-validation-evidence-*.zip. Multiple matches select the newest file.",
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
    report_arg, report_arg_ok = resolve_path_arg(args.report, "manual validation report")
    bundle_arg, bundle_arg_ok = resolve_path_arg(args.bundle, "evidence bundle")
    passed = report_arg_ok and bundle_arg_ok and passed

    if args.final:
        report = report_arg or newest_report()
        if report is None:
            fail("no macOS manual validation report found under build/")
            passed = False
        else:
            passed = check_manual_report(report) and passed
            bundle = bundle_arg or default_bundle_for_report(report)
            passed = check_evidence_bundle(report, bundle) and passed
            if args.skip_github:
                head_sha = local_head_sha()
                if head_sha:
                    passed = check_report_git_sha(report, head_sha, "local HEAD") and passed
    elif report_arg:
        passed = check_manual_report(report_arg) and passed
        if bundle_arg:
            passed = check_evidence_bundle(report_arg, bundle_arg) and passed
    elif bundle_arg:
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
                if args.final:
                    if report is not None:
                        passed = check_report_git_sha(report, pr_head_sha(repo), "PR head") and passed
                    manual_workflow_passed, manual_run_id = check_manual_macos_validation_workflow(repo)
                    passed = manual_workflow_passed and passed
                    if report is not None and manual_run_id is not None:
                        passed = (
                            check_report_artifact_workflow_run(
                                report,
                                manual_run_id,
                                "manual macOS workflow run",
                            )
                            and passed
                        )
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
