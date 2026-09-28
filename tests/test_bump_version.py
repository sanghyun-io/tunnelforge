"""
scripts/versioning.py 및 scripts/bump_version.py 단위 테스트

테스트 범위:
- 버전 파싱 (정상/에러)
- 버전 bump 계산 (정상/엣지케이스/에러)
- 파일 쓰기 (version.py, pyproject.toml)
- dry-run 모드
- CLI 통합 (subprocess)
"""

import re
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
    read_status_marker,
    read_version,
    sync_installer,
    sync_pyproject,
    sync_status_marker,
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


@pytest.fixture
def installer_file(tmp_path):
    """정상적인 Inno Setup(.iss) 형태의 임시 파일."""
    f = tmp_path / "TunnelForge.iss"
    f.write_text(
        '#define MyAppName "TunnelForge"\n'
        '#define MyAppVersion "1.11.0"\n'
        '#define MyAppPublisher "sanghyun-io"\n\n'
        '[Setup]\nAppVersion={#MyAppVersion}\n'
        'OutputBaseFilename=TunnelForge-Setup-{#MyAppVersion}\n',
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
# sync_installer 테스트
# ─────────────────────────────────────────────

class TestSyncInstaller:
    def test_updates_version(self, installer_file):
        sync_installer(installer_file, "1.11.1")
        content = installer_file.read_text(encoding='utf-8')
        assert '#define MyAppVersion "1.11.1"' in content

    def test_preserves_other_defines(self, installer_file):
        sync_installer(installer_file, "1.11.1")
        content = installer_file.read_text(encoding='utf-8')
        assert '#define MyAppName "TunnelForge"' in content
        assert '#define MyAppPublisher "sanghyun-io"' in content
        # {#MyAppVersion} 참조는 그대로 보존되어야 한다 (값만 바뀜)
        assert 'AppVersion={#MyAppVersion}' in content
        assert 'OutputBaseFilename=TunnelForge-Setup-{#MyAppVersion}' in content

    def test_does_not_duplicate(self, installer_file):
        sync_installer(installer_file, "1.11.1")
        content = installer_file.read_text(encoding='utf-8')
        assert content.count('#define MyAppVersion "1.11.1"') == 1

    def test_minor_bump_version(self, installer_file):
        sync_installer(installer_file, "2.1.0")
        content = installer_file.read_text(encoding='utf-8')
        assert '#define MyAppVersion "2.1.0"' in content

    def test_missing_define_raises(self, tmp_path):
        f = tmp_path / "bad.iss"
        f.write_text('#define MyAppName "X"\n', encoding='utf-8')
        with pytest.raises(ValueError):
            sync_installer(f, "1.0.0")

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sync_installer(tmp_path / "nonexistent.iss", "1.0.0")


# ─────────────────────────────────────────────
# sync_status_marker / read_status_marker 테스트
# ─────────────────────────────────────────────

class TestStatusMarker:
    @pytest.fixture
    def status_doc(self, tmp_path):
        f = tmp_path / "current_status.md"
        f.write_text(
            "# TunnelForge Current Status\n\n"
            "Last reviewed: 2026-07-29\n\n"
            "Current shipping version: `v2.4.2` <!-- managed by bot -->\n\n"
            "## Summary\n\nbody\n",
            encoding='utf-8',
        )
        return f

    def test_read_marker(self, status_doc):
        assert read_status_marker(status_doc) == "2.4.2"

    def test_sync_updates_marker(self, status_doc):
        sync_status_marker(status_doc, "2.4.3")
        assert "Current shipping version: `v2.4.3`" in status_doc.read_text(encoding='utf-8')

    def test_sync_preserves_annotation_and_body(self, status_doc):
        sync_status_marker(status_doc, "2.5.0")
        content = status_doc.read_text(encoding='utf-8')
        assert "<!-- managed by bot -->" in content
        assert "Last reviewed: 2026-07-29" in content
        assert "## Summary" in content

    def test_read_after_sync(self, status_doc):
        sync_status_marker(status_doc, "3.0.0")
        assert read_status_marker(status_doc) == "3.0.0"

    def test_sync_does_not_duplicate(self, status_doc):
        sync_status_marker(status_doc, "2.4.3")
        content = status_doc.read_text(encoding='utf-8')
        assert content.count("Current shipping version:") == 1

    def test_missing_marker_raises(self, tmp_path):
        f = tmp_path / "no_marker.md"
        f.write_text("# Status\n\nno marker\n", encoding='utf-8')
        with pytest.raises(ValueError, match="Current shipping version"):
            sync_status_marker(f, "1.0.0")
        with pytest.raises(ValueError, match="Current shipping version"):
            read_status_marker(f)

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sync_status_marker(tmp_path / "nonexistent.md", "1.0.0")


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

    @pytest.mark.parametrize('args, stream, expected', [
        (['--help'], 'stdout', '버전'),
        (['--bump-type', 'patch', '--dry-run'], 'stderr', '현재 버전'),
    ])
    def test_redirected_output_is_utf8_with_legacy_locale(self, monkeypatch, args, stream, expected):
        monkeypatch.setenv('PYTHONIOENCODING', 'cp949')
        result = subprocess.run(
            [sys.executable, self.CLI, *args], capture_output=True,
            cwd=self.PROJECT_ROOT,
        )
        assert result.returncode == 0
        assert expected in getattr(result, stream).decode('utf-8')

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

    def current_version(self):
        version_file = Path(self.PROJECT_ROOT) / "src" / "version.py"
        content = version_file.read_text(encoding='utf-8')
        match = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)[\'"]', content)
        assert match is not None
        return tuple(int(part) for part in match.group(1).split('.'))

    def test_dry_run_exit_zero(self):
        code, stdout, _ = self.run_cli("--bump-type", "patch", "--dry-run")
        assert code == 0

    def test_dry_run_stdout_format(self):
        _, stdout, _ = self.run_cli("--bump-type", "patch", "--dry-run")
        assert stdout.startswith("new_version=")

    def test_dry_run_patch(self):
        _, stdout, _ = self.run_cli("--bump-type", "patch", "--dry-run")
        major, minor, patch = self.current_version()
        assert f"new_version={major}.{minor}.{patch + 1}" in stdout

    def test_dry_run_minor(self):
        _, stdout, _ = self.run_cli("--bump-type", "minor", "--dry-run")
        major, minor, _ = self.current_version()
        assert f"new_version={major}.{minor + 1}.0" in stdout

    def test_dry_run_major(self):
        _, stdout, _ = self.run_cli("--bump-type", "major", "--dry-run")
        major, _, _ = self.current_version()
        assert f"new_version={major + 1}.0.0" in stdout

    def test_dry_run_no_file_modification(self):
        """dry-run 시 src/version.py가 수정되지 않아야 한다."""
        version_path = Path(self.PROJECT_ROOT) / "src" / "version.py"
        original = version_path.read_text(encoding='utf-8')
        self.run_cli("--bump-type", "patch", "--dry-run")
        assert version_path.read_text(encoding='utf-8') == original

    def test_invalid_bump_type_exit_nonzero(self):
        code, _, _ = self.run_cli("--bump-type", "invalid")
        assert code != 0

    def test_real_bump_syncs_all_four_files(self, tmp_path):
        """실제 bump 시 version.py / pyproject.toml / .iss / current_status.md 마커가
        모두 동기화되어야 한다.

        워크트리 실제 파일(특히 docs/current_status.md)을 건드리지 않도록 모든
        대상 경로를 임시 파일로 전달한다. --status-doc 을 명시하지 않으면
        기본값(docs/current_status.md)이 잡히므로 반드시 임시 경로를 넘긴다.
        """
        vf = tmp_path / "version.py"
        vf.write_text('__version__ = "2.1.0"\n', encoding='utf-8')
        pf = tmp_path / "pyproject.toml"
        pf.write_text('[project]\nname = "x"\nversion = "2.1.0"\n', encoding='utf-8')
        isf = tmp_path / "TunnelForge.iss"
        isf.write_text('#define MyAppVersion "2.1.0"\n', encoding='utf-8')
        doc = tmp_path / "current_status.md"
        doc.write_text(
            "# Status\n\nCurrent shipping version: `v2.1.0`\n\n## Summary\n",
            encoding='utf-8',
        )

        code, stdout, stderr = self.run_cli(
            "--bump-type", "patch",
            "--version-file", str(vf),
            "--pyproject-file", str(pf),
            "--installer-file", str(isf),
            "--status-doc", str(doc),
        )
        assert code == 0, stderr
        assert "new_version=2.1.1" in stdout
        assert '__version__ = "2.1.1"' in vf.read_text(encoding='utf-8')
        assert 'version = "2.1.1"' in pf.read_text(encoding='utf-8')
        assert '#define MyAppVersion "2.1.1"' in isf.read_text(encoding='utf-8')
        assert "Current shipping version: `v2.1.1`" in doc.read_text(encoding='utf-8')

    def test_real_bump_missing_status_marker_is_fail_soft(self, tmp_path):
        """current_status.md 에 마커가 없어도 bump 는 실패하지 않는다(fail-soft).

        마커 부재/불일치의 강제는 test_current_status_docs.py 의 coupling 테스트가
        담당하고, bump 자체는 릴리스를 막지 않아야 한다.
        """
        vf = tmp_path / "version.py"
        vf.write_text('__version__ = "3.0.0"\n', encoding='utf-8')
        doc = tmp_path / "current_status.md"
        doc.write_text("# Status\n\nno marker here\n", encoding='utf-8')

        code, stdout, stderr = self.run_cli(
            "--bump-type", "minor",
            "--version-file", str(vf),
            "--pyproject-file", str(tmp_path / "absent.toml"),
            "--installer-file", str(tmp_path / "absent.iss"),
            "--status-doc", str(doc),
        )
        assert code == 0, stderr
        assert "new_version=3.1.0" in stdout
        assert "건너뜀" in stderr  # WARN skip message
        assert doc.read_text(encoding='utf-8') == "# Status\n\nno marker here\n"

    def test_help_exit_zero(self):
        code, _, _ = self.run_cli("--help")
        assert code == 0

    def test_missing_bump_type_exit_nonzero(self):
        code, _, _ = self.run_cli()
        assert code != 0
