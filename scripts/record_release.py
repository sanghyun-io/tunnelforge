#!/usr/bin/env python3
"""
TunnelForge - record_release CLI

릴리스 발행 후 docs/current_status.md 의 Verification Log 에 well-formed 한
발행(publication) 행을 한 번에 append 하는 헬퍼.

배경:
    예전에는 릴리스마다 run ID/다이제스트 리터럴을 손으로 옮겨 적고, 그 값을
    하드코딩한 새 pytest 함수를 매번 작성해야 했다(순환 + 토일). 이제
    test_current_status_docs.py 는 발행 행을 리터럴이 아니라 *형식*으로 검증하며
    (test_current_status_release_publication_rows_are_well_formed), 이 스크립트가
    그 형식에 맞는 행을 생성해 넣는다. 릴리스 후 명령 한 번이면 원장이 최신으로
    유지된다.

Usage:
    python scripts/record_release.py \
        --version 2.4.3 --pr 258 \
        --pr-run 31000000001 --pr-run 31000000002 \
        --tag-run 31000100001 --release-run 31000200001 \
        --merge-commit 1d9305c826f11ebe6c808ec501953ed1339152d9 \
        --date 2026-08-01 --tf-status TF-STATUS-098

    # 미리보기 (파일 수정 없음)
    python scripts/record_release.py ... --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_RE = re.compile(r"^\d{8,}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TF_STATUS_RE = re.compile(r"^TF-STATUS-\d+$")

VERIFICATION_LOG_HEADING = "## Verification Log"
SEPARATOR_RE = re.compile(r"^\|\s*-{2,}\s*(\|\s*-{2,}\s*)+\|\s*$")


def _join_runs(pr_runs: list[str]) -> str:
    """PR run ID 목록을 백틱으로 감싸 자연어로 잇는다."""
    quoted = [f"`{r}`" for r in pr_runs]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} and {quoted[1]}"
    return f"{', '.join(quoted[:-1])}, and {quoted[-1]}"


def build_row(
    *,
    version: str,
    pr: int,
    pr_runs: list[str],
    tag_run: str,
    release_run: str,
    merge_commit: str,
    date: str,
    tf_status: str | None = None,
) -> str:
    """test_current_status_release_publication_rows_are_well_formed 를 통과하는
    Verification Log 행 문자열을 생성한다(끝에 개행 없음)."""
    status_suffix = f" / {tf_status}" if tf_status else ""
    status_closed = f" {tf_status} is closed." if tf_status else ""
    scope = f"`v{version}` protected publication closure{status_suffix}"
    command = (
        f"PR #{pr} protected checks and merge; "
        f"approved `create-release-tag.yml` run `{tag_run}`; "
        f"approved `release.yml` run `{release_run}`; "
        "annotated-tag object/peeled-commit inspection; "
        "draft asset/digest and checksum-sidecar inspection; "
        "stable/latest publication and live `UpdateChecker`"
    )
    result = (
        f"PR runs {_join_runs(pr_runs)} passed the required Python, Rust Core, "
        "version, support-tracking, and internal/external macOS arm64/x86_64 "
        f"gates; merge commit `{merge_commit}`; tag peels to that exact commit; "
        "release run built and verified Windows plus unsigned macOS "
        "arm64/x86_64 artifacts; all 10 release assets have GitHub SHA-256 "
        "digests and all four macOS sidecars match"
    )
    notes = (
        f"`v{version}` is stable/latest at "
        f"`https://github.com/sanghyun-io/tunnelforge/releases/tag/v{version}`."
        f"{status_closed}"
    )
    return f"| {date} | {scope} | {command} | {result} | {notes} |"


def insert_row(doc_text: str, row: str, version: str) -> str:
    """Verification Log 표 구분선 바로 다음(최신 우선)에 row 를 삽입한 새 문서
    텍스트를 반환한다.

    Raises:
        ValueError: Verification Log/구분선을 찾지 못했거나, 해당 버전의 발행
            행이 이미 존재할 때.
    """
    lines = doc_text.splitlines(keepends=True)

    # Verification Log 섹션 범위
    start = next(
        (i for i, ln in enumerate(lines) if ln.rstrip("\n") == VERIFICATION_LOG_HEADING),
        None,
    )
    if start is None:
        raise ValueError(f"'{VERIFICATION_LOG_HEADING}' 섹션을 찾을 수 없습니다")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )

    section = lines[start:end]
    # 이미 같은 버전의 발행 행이 있으면 거부(멱등/오기입 방지)
    version_backtick = f"`v{version}`"
    for ln in section:
        if ln.startswith("| ") and "publication" in ln and version_backtick in ln:
            raise ValueError(
                f"이미 `v{version}` 발행 행이 Verification Log 에 존재합니다"
            )

    sep_idx = next(
        (
            start + off
            for off, ln in enumerate(section)
            if SEPARATOR_RE.match(ln.rstrip("\n"))
        ),
        None,
    )
    if sep_idx is None:
        raise ValueError("Verification Log 표의 구분선(| --- | ... |)을 찾을 수 없습니다")

    new_line = row if row.endswith("\n") else row + "\n"
    lines.insert(sep_idx + 1, new_line)
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="릴리스 발행 행을 current_status.md Verification Log 에 추가",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", required=True, help="릴리스 버전 (X.Y.Z)")
    parser.add_argument("--pr", required=True, type=int, help="릴리스 PR 번호")
    parser.add_argument(
        "--pr-run", dest="pr_runs", action="append", required=True,
        help="PR 워크플로 run ID (반복 지정, 최소 1개)",
    )
    parser.add_argument("--tag-run", required=True, help="create-release-tag.yml run ID")
    parser.add_argument("--release-run", required=True, help="release.yml run ID")
    parser.add_argument(
        "--merge-commit", required=True, help="40자 머지 커밋 SHA (소문자 hex)"
    )
    parser.add_argument("--date", required=True, help="발행 날짜 (YYYY-MM-DD)")
    parser.add_argument(
        "--tf-status", default=None, help="닫을 이슈 ID (예: TF-STATUS-098, 선택)"
    )
    parser.add_argument(
        "--doc", default="docs/current_status.md", help="대상 문서 경로"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="파일 수정 없이 생성될 행만 출력"
    )

    args = parser.parse_args()

    errors = []
    if not VERSION_RE.match(args.version):
        errors.append(f"--version 은 X.Y.Z 형식이어야 합니다: {args.version!r}")
    if not COMMIT_RE.match(args.merge_commit):
        errors.append(
            f"--merge-commit 은 40자 소문자 hex 여야 합니다: {args.merge_commit!r}"
        )
    if not DATE_RE.match(args.date):
        errors.append(f"--date 는 YYYY-MM-DD 형식이어야 합니다: {args.date!r}")
    for label, value in [
        ("--tag-run", args.tag_run),
        ("--release-run", args.release_run),
    ]:
        if not RUN_RE.match(value):
            errors.append(f"{label} 은 8자리 이상 숫자여야 합니다: {value!r}")
    for value in args.pr_runs:
        if not RUN_RE.match(value):
            errors.append(f"--pr-run 은 8자리 이상 숫자여야 합니다: {value!r}")
    if args.tf_status is not None and not TF_STATUS_RE.match(args.tf_status):
        errors.append(
            f"--tf-status 는 TF-STATUS-### 형식이어야 합니다: {args.tf_status!r}"
        )
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    row = build_row(
        version=args.version,
        pr=args.pr,
        pr_runs=args.pr_runs,
        tag_run=args.tag_run,
        release_run=args.release_run,
        merge_commit=args.merge_commit,
        date=args.date,
        tf_status=args.tf_status,
    )

    if args.dry_run:
        print(row)
        return 0

    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    doc_path = project_root / args.doc if not Path(args.doc).is_absolute() else Path(args.doc)
    if not doc_path.exists():
        print(f"ERROR: 문서를 찾을 수 없습니다: {doc_path}", file=sys.stderr)
        return 1

    try:
        new_text = insert_row(doc_path.read_text(encoding="utf-8"), row, args.version)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    doc_path.write_text(new_text, encoding="utf-8")
    print(f"[OK] `v{args.version}` 발행 행을 {doc_path} 에 추가했습니다", file=sys.stderr)
    print(row)
    return 0


if __name__ == "__main__":
    # Pipes and CI consumers use UTF-8; keep interactive terminal settings.
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty():
            stream.reconfigure(encoding='utf-8')
    sys.exit(main())
