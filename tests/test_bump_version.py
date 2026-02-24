"""
scripts/versioning.py 및 scripts/bump_version.py 단위 테스트

테스트 범위:
- 버전 파싱 (정상/에러)
- 버전 bump 계산 (정상/엣지케이스/에러)
- 파일 쓰기 (version.py, pyproject.toml)
- dry-run 모드
- CLI 통합 (subprocess)
"""

import subprocess
import sys
from pathlib import Path

import pytest

# scripts 디렉토리를 sys.path에 추가
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from versioning import (
    bump_version,
    compare_versions,
    read_version,
    sync_pyproject,
    write_version,
)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture
def version_file(tmp_path):
    """정상적인 src/version.py 형태의 임시 파일."""
    f = tmp_path / "version.py"
    f.write_text(
        '"""버전 정보"""\n\n__version__ = "1.11.0"\n__app_name__ = "TunnelForge"\n',
        encoding='utf-8'
    )
    return f


@pytest.fixture
def pyproject_file(tmp_path):
    """정상적인 pyproject.toml 형태의 임시 파일."""
    f = tmp_path / "pyproject.toml"
    f.write_text(
        '[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "tunnelforge"\nversion = "1.11.0"\ndescription = "Test"\n',
        encoding='utf-8'
    )
    return f


# ─────────────────────────────────────────────
# read_version 파싱 테스트
# ─────────────────────────────────────────────

class TestReadVersion:
    def test_normal(self, tmp_path):
        f = tmp_path / "version.py"
        f.write_text('__version__ = "1.11.0"\n', encoding='utf-8')
        assert read_version(f) == "1.11.0"

    def test_single_quote(self, tmp_path):
        f = tmp_path / "version.py"
        f.write_text("__version__ = '1.11.0'\n", encoding='utf-8')
        assert read_version(f) == "1.11.0"

    def test_trailing_space(self, tmp_path):
        f = tmp_path / "version.py"
        f.write_text('__version__ = "1.11.0"   \n', encoding='utf-8')
        assert read_version(f) == "1.11.0"

    def test_with_other_content(self, version_file):
        assert read_version(version_file) == "1.11.0"

    def test_missing_version_key(self, tmp_path):
        f = tmp_path / "version.py"
        f.write_text('APP_NAME = "TunnelForge"\n', encoding='utf-8')
        with pytest.raises(ValueError, match="__version__"):
            read_version(f)

    def test_empty_file(self, tmp_path):
        f = tmp_path / "version.py"
        f.write_text('', encoding='utf-8')
        with pytest.raises(ValueError):
            read_version(f)

    def test_invalid_format(self, tmp_path):
        f = tmp_path / "version.py"
        f.write_text('__version__ = abc\n', encoding='utf-8')
        with pytest.raises(ValueError):
            read_version(f)

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_version(tmp_path / "nonexistent.py")


# ─────────────────────────────────────────────
# bump_version 계산 테스트
# ─────────────────────────────────────────────

class TestBumpVersion:
    @pytest.mark.parametrize("version,bump_type,expected", [
        ("1.11.0", "patch", "1.11.1"),
        ("1.11.0", "minor", "1.12.0"),
        ("1.11.0", "major", "2.0.0"),
        ("0.0.0",  "patch", "0.0.1"),
        ("0.0.0",  "minor", "0.1.0"),
        ("0.0.0",  "major", "1.0.0"),
        ("99.99.99", "patch", "99.99.100"),
        ("1.0.0",  "major", "2.0.0"),
        ("1.9.0",  "minor", "1.10.0"),
    ])
    def test_bump(self, version, bump_type, expected):
        assert bump_version(version, bump_type) == expected

    def test_minor_resets_patch(self):
        assert bump_version("1.11.5", "minor") == "1.12.0"

    def test_major_resets_minor_and_patch(self):
        assert bump_version("1.11.5", "major") == "2.0.0"

    def test_invalid_bump_type(self):
        with pytest.raises(ValueError, match="bump_type"):
            bump_version("1.0.0", "invalid")

    def test_invalid_version_format(self):
        with pytest.raises(ValueError):
            bump_version("not-a-version", "patch")

    def test_two_part_version_raises(self):
        with pytest.raises(ValueError):
            bump_version("1.0", "patch")


# ─────────────────────────────────────────────
# write_version 파일 쓰기 테스트
# ─────────────────────────────────────────────

class TestWriteVersion:
    def test_updates_version(self, version_file):
        write_version(version_file, "1.11.1")
        content = version_file.read_text(encoding='utf-8')
        assert '__version__ = "1.11.1"' in content

    def test_preserves_other_fields(self, version_file):
        write_version(version_file, "1.11.1")
        content = version_file.read_text(encoding='utf-8')
        assert '__app_name__ = "TunnelForge"' in content

    def test_preserves_docstring(self, version_file):
        write_version(version_file, "1.11.1")
        content = version_file.read_text(encoding='utf-8')
        assert '"""버전 정보"""' in content

    def test_read_after_write(self, version_file):
        write_version(version_file, "2.0.0")
        assert read_version(version_file) == "2.0.0"

    def test_no_version_key_raises(self, tmp_path):
        f = tmp_path / "version.py"
        f.write_text('APP_NAME = "X"\n', encoding='utf-8')
        with pytest.raises(ValueError):
            write_version(f, "1.0.0")

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            write_version(tmp_path / "nonexistent.py", "1.0.0")


# ─────────────────────────────────────────────
# sync_pyproject 테스트
# ─────────────────────────────────────────────

class TestSyncPyproject:
    def test_updates_version(self, pyproject_file):
        sync_pyproject(pyproject_file, "1.11.1")
        content = pyproject_file.read_text(encoding='utf-8')
        assert 'version = "1.11.1"' in content

    def test_preserves_other_fields(self, pyproject_file):
        sync_pyproject(pyproject_file, "1.11.1")
        content = pyproject_file.read_text(encoding='utf-8')
        assert 'name = "tunnelforge"' in content
        assert 'description = "Test"' in content
        assert '[build-system]' in content

    def test_does_not_duplicate_version(self, pyproject_file):
        sync_pyproject(pyproject_file, "1.11.1")
        content = pyproject_file.read_text(encoding='utf-8')
        assert content.count('version = "1.11.1"') == 1

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sync_pyproject(tmp_path / "nonexistent.toml", "1.0.0")


# ─────────────────────────────────────────────
# compare_versions 테스트
# ─────────────────────────────────────────────

class TestCompareVersions:
    @pytest.mark.parametrize("v1,v2,expected", [
        ("1.11.1", "1.11.0", 1),
        ("1.11.0", "1.11.0", 0),
        ("1.10.0", "1.11.0", -1),
        ("2.0.0",  "1.99.99", 1),
        ("v1.0.0", "1.0.0", 0),   # v 접두사 허용
    ])
    def test_compare(self, v1, v2, expected):
        assert compare_versions(v1, v2) == expected


# ─────────────────────────────────────────────
# CLI 통합 테스트 (subprocess)
# ─────────────────────────────────────────────

class TestBumpVersionCLI:
    """scripts/bump_version.py를 subprocess로 실행하는 통합 테스트."""

    CLI = str(Path(__file__).parent.parent / "scripts" / "bump_version.py")
    PROJECT_ROOT = str(Path(__file__).parent.parent)

    def run_cli(self, *args):
        """CLI 실행 후 (returncode, stdout, stderr) 반환."""
        result = subprocess.run(
            [sys.executable, self.CLI, *args],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=self.PROJECT_ROOT,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def test_dry_run_exit_zero(self):
        code, stdout, _ = self.run_cli("--bump-type", "patch", "--dry-run")
        assert code == 0

    def test_dry_run_stdout_format(self):
        _, stdout, _ = self.run_cli("--bump-type", "patch", "--dry-run")
        assert stdout.startswith("new_version=")

    def test_dry_run_patch(self):
        _, stdout, _ = self.run_cli("--bump-type", "patch", "--dry-run")
        # 현재 버전(1.11.0)의 patch 결과
        assert "new_version=1.11.1" in stdout

    def test_dry_run_minor(self):
        _, stdout, _ = self.run_cli("--bump-type", "minor", "--dry-run")
        assert "new_version=1.12.0" in stdout

    def test_dry_run_major(self):
        _, stdout, _ = self.run_cli("--bump-type", "major", "--dry-run")
        assert "new_version=2.0.0" in stdout

    def test_dry_run_no_file_modification(self):
        """dry-run 시 src/version.py가 수정되지 않아야 한다."""
        version_path = Path(self.PROJECT_ROOT) / "src" / "version.py"
        original = version_path.read_text(encoding='utf-8')
        self.run_cli("--bump-type", "patch", "--dry-run")
        assert version_path.read_text(encoding='utf-8') == original

    def test_invalid_bump_type_exit_nonzero(self):
        code, _, _ = self.run_cli("--bump-type", "invalid")
        assert code != 0

    def test_help_exit_zero(self):
        code, _, _ = self.run_cli("--help")
        assert code == 0

    def test_missing_bump_type_exit_nonzero(self):
        code, _, _ = self.run_cli()
        assert code != 0
