"""
scripts/smart_release.py 의 versioning.py 통합(dedup) 검증 테스트

CC-219: smart_release.py 가 compare_versions/get_local_version/bump_version/
update_version_file 을 재구현하지 않고 scripts/versioning.py 의 함수를 그대로
import 해서 쓰는지 락킹한다. smart_release.main() 은 git remote/GitHub API/
사용자 input 에 의존해 오프라인 유닛테스트가 불가능하므로, 여기서는 import
레벨 dedup 만 검증한다.
"""

import inspect
import sys
from pathlib import Path

# scripts 디렉토리를 sys.path에 추가 (test_bump_version.py와 동일 패턴)
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import smart_release
import versioning


class TestSmartReleaseUsesVersioningModule:
    """smart_release가 versioning.py의 함수를 재구현 없이 그대로 재사용하는지 확인."""

    def test_read_version_is_shared(self):
        assert smart_release.read_version is versioning.read_version

    def test_write_version_is_shared(self):
        assert smart_release.write_version is versioning.write_version

    def test_compare_versions_is_shared(self):
        assert smart_release.compare_versions is versioning.compare_versions

    def test_bump_version_is_shared(self):
        assert smart_release.bump_version is versioning.bump_version

    def test_sync_pyproject_is_shared(self):
        assert smart_release.sync_pyproject is versioning.sync_pyproject

    def test_sync_installer_is_shared(self):
        assert smart_release.sync_installer is versioning.sync_installer


class TestSmartReleaseNoReimplementation:
    """중복 재구현 함수가 재발하지 않았는지 소스 레벨에서 검증."""

    def test_get_local_version_removed(self):
        source = inspect.getsource(smart_release)
        assert "def get_local_version" not in source

    def test_update_version_file_removed(self):
        source = inspect.getsource(smart_release)
        assert "def update_version_file" not in source
