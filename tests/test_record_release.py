"""
scripts/record_release.py 단위/통합 테스트.

핵심 계약: record_release 가 생성하는 발행 행은 test_current_status_docs.py 의
test_current_status_release_publication_rows_are_well_formed 와 동일한 형식
검증을 통과해야 한다(두 축이 상호운용). 또한 Verification Log 표의 구분선
바로 다음(최신 우선)에 삽입되고, 같은 버전 중복은 거부해야 한다.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from record_release import build_row, insert_row  # noqa: E402

CLI = str(SCRIPTS_DIR / "record_release.py")

SAMPLE_DOC = (
    "# TunnelForge Current Status\n"
    "\n"
    "Current shipping version: `v2.4.3`\n"
    "\n"
    "## Verification Log\n"
    "\n"
    "| Date | Scope | Command | Result | Notes |\n"
    "| --- | --- | --- | --- | --- |\n"
    "| 2026-07-29 | `v2.4.2` protected publication closure / TF-STATUS-097 | "
    "PR #253 merge; run `30419408852` | merge commit "
    "`1d9305c826f11ebe6c808ec501953ed1339152d9`; 10/10 assets have GitHub "
    "digests | `v2.4.2` is stable/latest. |\n"
    "\n"
    "## Existing Status And Planning Documents\n"
    "\n"
    "tail\n"
)


def _kwargs(**over):
    base = dict(
        version="2.4.3",
        pr=258,
        pr_runs=["31000000001", "31000000002"],
        tag_run="31000100001",
        release_run="31000200001",
        merge_commit="a" * 40,
        date="2026-08-01",
        tf_status="TF-STATUS-098",
    )
    base.update(over)
    return base


def _assert_well_formed(row: str):
    """test_current_status_release_publication_rows_are_well_formed 와 동일한 검증."""
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert len(cells) >= 5
    scope = cells[1]
    assert "publication" in scope
    assert re.search(r"`v\d+\.\d+\.\d+`", scope)
    assert re.search(r"PR #\d+", row)
    assert re.search(r"\b\d{8,}\b", row)
    assert re.search(r"\b[0-9a-f]{40}\b", row)
    assert "asset" in row and "digest" in row


class TestBuildRow:
    def test_row_is_well_formed(self):
        _assert_well_formed(build_row(**_kwargs()))

    def test_row_well_formed_without_tf_status(self):
        _assert_well_formed(build_row(**_kwargs(tf_status=None)))

    def test_single_pr_run(self):
        row = build_row(**_kwargs(pr_runs=["31000000009"]))
        _assert_well_formed(row)
        assert "`31000000009`" in row
        assert " and " not in row.split("passed the required")[0].split("PR runs")[1]

    def test_two_pr_runs_joined_with_and(self):
        row = build_row(**_kwargs(pr_runs=["31000000001", "31000000002"]))
        assert "`31000000001` and `31000000002`" in row

    def test_scope_carries_version_and_status(self):
        row = build_row(**_kwargs())
        assert "`v2.4.3` protected publication closure / TF-STATUS-098" in row
        assert "TF-STATUS-098 is closed." in row

    def test_no_status_suffix_when_absent(self):
        row = build_row(**_kwargs(tf_status=None))
        assert "TF-STATUS" not in row


class TestInsertRow:
    def test_inserts_after_separator_newest_first(self):
        row = build_row(**_kwargs())
        out = insert_row(SAMPLE_DOC, row, "2.4.3")
        lines = out.splitlines()
        sep_idx = next(i for i, ln in enumerate(lines) if re.match(r"^\|\s*-{2,}", ln))
        # 삽입 행이 구분선 바로 다음(가장 최신)이어야 한다
        assert "`v2.4.3`" in lines[sep_idx + 1]
        # 기존 v2.4.2 행은 그 아래에 남는다
        v242_idx = next(i for i, ln in enumerate(lines) if "`v2.4.2`" in ln and ln.startswith("| 2026"))
        assert sep_idx + 1 < v242_idx

    def test_inserted_row_passes_format_guard(self):
        row = build_row(**_kwargs())
        out = insert_row(SAMPLE_DOC, row, "2.4.3")
        # Verification Log 섹션에서 발행 행을 추려 well-formed 검증
        section = out.split("## Verification Log", 1)[1].split("\n## ", 1)[0]
        pub_rows = [
            ln for ln in section.splitlines()
            if ln.startswith("| ") and "publication" in ln
            and re.search(r"`v\d+\.\d+\.\d+`", ln.split("|")[2])
        ]
        assert len(pub_rows) == 2
        for r in pub_rows:
            _assert_well_formed(r)

    def test_duplicate_version_refused(self):
        row = build_row(**_kwargs(version="2.4.2"))
        with pytest.raises(ValueError, match="이미"):
            insert_row(SAMPLE_DOC, row, "2.4.2")

    def test_missing_verification_log_raises(self):
        with pytest.raises(ValueError, match="Verification Log"):
            insert_row("# Doc\n\nno log here\n", build_row(**_kwargs()), "2.4.3")

    def test_preserves_tail_sections(self):
        out = insert_row(SAMPLE_DOC, build_row(**_kwargs()), "2.4.3")
        assert "## Existing Status And Planning Documents" in out
        assert out.rstrip().endswith("tail")


class TestCLI:
    @pytest.mark.parametrize('help_only', [False, True])
    def test_redirected_output_is_utf8_with_legacy_locale(self, monkeypatch, tmp_path, help_only):
        monkeypatch.setenv('PYTHONIOENCODING', 'cp949')
        args = ['--help'] if help_only else self._valid_args(tmp_path / 'missing.md')
        result = subprocess.run(
            [sys.executable, CLI, *args], capture_output=True,
        )
        assert result.returncode == (0 if help_only else 1)
        output = result.stdout if help_only else result.stderr
        expected = '릴리스' if help_only else '문서를 찾을 수 없습니다'
        assert expected in output.decode('utf-8')

    def run(self, *args, cwd=None):
        result = subprocess.run(
            [sys.executable, CLI, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=cwd,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def _valid_args(self, doc: Path):
        return [
            "--version", "2.4.3", "--pr", "258",
            "--pr-run", "31000000001", "--pr-run", "31000000002",
            "--tag-run", "31000100001", "--release-run", "31000200001",
            "--merge-commit", "a" * 40, "--date", "2026-08-01",
            "--tf-status", "TF-STATUS-098", "--doc", str(doc),
        ]

    def test_dry_run_outputs_row_no_write(self, tmp_path):
        doc = tmp_path / "current_status.md"
        doc.write_text(SAMPLE_DOC, encoding="utf-8")
        code, stdout, _ = self.run(*self._valid_args(doc), "--dry-run")
        assert code == 0
        assert "`v2.4.3`" in stdout
        assert doc.read_text(encoding="utf-8") == SAMPLE_DOC

    def test_real_insert(self, tmp_path):
        doc = tmp_path / "current_status.md"
        doc.write_text(SAMPLE_DOC, encoding="utf-8")
        code, _, stderr = self.run(*self._valid_args(doc))
        assert code == 0, stderr
        content = doc.read_text(encoding="utf-8")
        assert content.count("publication closure") == 2
        _assert_well_formed(
            next(ln for ln in content.splitlines() if "`v2.4.3`" in ln and ln.startswith("| "))
        )

    def test_real_insert_keeps_lf_line_endings(self, tmp_path):
        doc = tmp_path / "current_status.md"
        doc.write_bytes(SAMPLE_DOC.encode("utf-8"))
        code, _, stderr = self.run(*self._valid_args(doc))
        assert code == 0, stderr
        assert b"\r\n" not in doc.read_bytes()

    def test_duplicate_exit_nonzero(self, tmp_path):
        doc = tmp_path / "current_status.md"
        doc.write_text(SAMPLE_DOC, encoding="utf-8")
        args = self._valid_args(doc)
        # version 을 기존 2.4.2 로 바꿔 중복 유발
        args[args.index("2.4.3")] = "2.4.2"
        code, _, stderr = self.run(*args)
        assert code == 1
        assert "이미" in stderr

    @pytest.mark.parametrize("flag,bad", [
        ("--version", "2.4"),
        ("--merge-commit", "xyz"),
        ("--tag-run", "abc"),
        ("--date", "2026/08/01"),
        ("--tf-status", "TF-98"),
    ])
    def test_validation_errors_exit_nonzero(self, tmp_path, flag, bad):
        doc = tmp_path / "current_status.md"
        doc.write_text(SAMPLE_DOC, encoding="utf-8")
        args = self._valid_args(doc)
        args[args.index(flag) + 1] = bad
        code, _, stderr = self.run(*args)
        assert code == 1
        assert "ERROR" in stderr
