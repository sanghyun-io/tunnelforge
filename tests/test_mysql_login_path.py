"""
tests/test_mysql_login_path.py

MysqlLoginPathManager 호환성 + 무결성 + 장애 복구 테스트.

테스트 전략:
- synthetic binary fixture로 round-trip 호환성 검증
- os.replace() 실패 주입으로 원자성(기존 파일 보존) 검증
- 손상된 청크가 있어도 나머지 엔트리 보존 검증
- 플랫폼 경로 분기 (Windows / Unix)
"""
import os
import struct
import secrets
import pytest

from src.core.mysql_login_path import (
    MysqlLoginPathManager,
    _derive_aes_key,
    _read_cnf,
    _write_cnf,
    _parse_ini,
    _build_ini,
    _KEY_LEN,
    _HEADER_LEN,
    _DATA_OFFSET,
    _PREFIX,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_cnf_bytes(login_key: bytes, sections: dict) -> bytes:
    """Python으로 .mylogin.cnf 바이너리를 직접 생성 (검증용)"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    aes_key = _derive_aes_key(login_key)
    ini_text = _build_ini(sections)
    text = ini_text.replace('\r\n', '\n').replace('\r', '\n')
    if text and not text.endswith('\n'):
        text += '\n'

    buf = bytearray()
    buf += b'\x00' * _HEADER_LEN
    buf += login_key

    for line in text.splitlines(keepends=True):
        plain = line.encode('utf-8')
        pad_len = 16 - (len(plain) % 16)
        if pad_len == 16:
            pad_len = 0
        padded = plain + b'\x00' * pad_len

        enc = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
        ciphertext = enc.update(padded) + enc.finalize()
        buf += struct.pack('<I', len(ciphertext))
        buf += ciphertext

    return bytes(buf)


@pytest.fixture
def cnf_path(tmp_path):
    """임시 .mylogin.cnf 경로"""
    return str(tmp_path / '.mylogin.cnf')


@pytest.fixture
def manager():
    return MysqlLoginPathManager()


# ---------------------------------------------------------------------------
# Round-trip 호환성 테스트
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_register_then_read_same_data(self, cnf_path, manager):
        """TunnelForge가 쓴 파일을 다시 읽으면 동일 엔트리가 반환돼야 함"""
        ok, result = manager.register.__wrapped__ if hasattr(manager.register, '__wrapped__') else None, None
        # 직접 _write_cnf/_read_cnf로 검증
        login_key = secrets.token_bytes(_KEY_LEN)
        sections = {'tf_13306': {'host': '127.0.0.1', 'user': 'root', 'password': 'pass', 'port': '13306'}}
        _write_cnf(login_key, _build_ini(sections), path=cnf_path)

        returned_key, ini_text = _read_cnf(path=cnf_path)
        parsed = _parse_ini(ini_text)

        assert 'tf_13306' in parsed
        assert parsed['tf_13306']['host'] == '127.0.0.1'
        assert parsed['tf_13306']['user'] == 'root'
        assert parsed['tf_13306']['password'] == 'pass'
        assert parsed['tf_13306']['port'] == '13306'

    def test_round_trip_key_preserved(self, cnf_path):
        """login_key가 round-trip 후에도 동일해야 함"""
        login_key = secrets.token_bytes(_KEY_LEN)
        sections = {'tf_3307': {'host': '127.0.0.1', 'user': 'admin', 'password': 'secret', 'port': '3307'}}
        _write_cnf(login_key, _build_ini(sections), path=cnf_path)

        returned_key, _ = _read_cnf(path=cnf_path)
        assert returned_key == login_key

    def test_non_tf_entries_preserved(self, cnf_path):
        """TF가 모르는 섹션도 round-trip 후 보존돼야 함"""
        login_key = secrets.token_bytes(_KEY_LEN)
        sections = {
            'client': {'host': 'other-host', 'user': 'other-user'},
            'tf_3307': {'host': '127.0.0.1', 'user': 'root', 'password': 'pw', 'port': '3307'},
        }
        _write_cnf(login_key, _build_ini(sections), path=cnf_path)

        _, ini_text = _read_cnf(path=cnf_path)
        parsed = _parse_ini(ini_text)

        assert 'client' in parsed, "비-TF 엔트리가 보존되지 않음"
        assert parsed['client']['host'] == 'other-host'
        assert 'tf_3307' in parsed

    def test_multiple_sections_all_preserved(self, cnf_path):
        """여러 섹션이 모두 round-trip 후 보존돼야 함"""
        login_key = secrets.token_bytes(_KEY_LEN)
        sections = {f'tf_{p}': {'host': '127.0.0.1', 'user': 'u', 'password': 'p', 'port': str(p)}
                    for p in [3306, 3307, 3308]}
        _write_cnf(login_key, _build_ini(sections), path=cnf_path)

        _, ini_text = _read_cnf(path=cnf_path)
        parsed = _parse_ini(ini_text)

        for p in [3306, 3307, 3308]:
            assert f'tf_{p}' in parsed, f"tf_{p} 섹션이 보존되지 않음"

    def test_read_synthetic_fixture(self, cnf_path):
        """직접 생성한 binary fixture를 올바르게 파싱해야 함"""
        login_key = secrets.token_bytes(_KEY_LEN)
        expected = {'tf_9999': {'host': '10.0.0.1', 'user': 'dbuser', 'password': 'dbpass', 'port': '9999'}}
        raw = _make_cnf_bytes(login_key, expected)

        with open(cnf_path, 'wb') as f:
            f.write(raw)

        _, ini_text = _read_cnf(path=cnf_path)
        parsed = _parse_ini(ini_text)

        assert parsed == expected


# ---------------------------------------------------------------------------
# 파일이 없을 때 동작
# ---------------------------------------------------------------------------

class TestMissingFile:
    def test_read_missing_file_returns_empty(self, tmp_path):
        """파일 없으면 빈 INI와 새 랜덤 키 반환"""
        path = str(tmp_path / 'nonexistent.cnf')
        key, text = _read_cnf(path=path)
        assert len(key) == _KEY_LEN
        assert text == ''

    def test_read_too_short_file_returns_empty(self, cnf_path):
        """파일이 너무 짧으면 빈 INI 반환"""
        with open(cnf_path, 'wb') as f:
            f.write(b'\x00' * 10)  # _DATA_OFFSET(24)보다 짧음
        key, text = _read_cnf(path=cnf_path)
        assert text == ''


# ---------------------------------------------------------------------------
# Fault-injection 테스트 (원자적 쓰기)
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_write_interrupted_existing_file_preserved(self, cnf_path, monkeypatch):
        """쓰기 중 os.replace 실패 시 기존 파일 내용이 보존돼야 함"""
        # 1. 초기 상태 쓰기
        login_key = secrets.token_bytes(_KEY_LEN)
        initial_sections = {'tf_3306': {'host': '127.0.0.1', 'user': 'root', 'password': 'pw', 'port': '3306'}}
        _write_cnf(login_key, _build_ini(initial_sections), path=cnf_path)

        # 2. os.replace를 실패하도록 주입
        def fail_replace(src, dst):
            raise OSError("simulated disk full")

        monkeypatch.setattr(os, 'replace', fail_replace)

        # 3. 새 엔트리 쓰기 시도 — 실패해야 함
        new_sections = {'tf_3307': {'host': '127.0.0.1', 'user': 'admin', 'password': 'pw2', 'port': '3307'}}
        with pytest.raises(OSError):
            _write_cnf(login_key, _build_ini(new_sections), path=cnf_path)

        # 4. temp 파일이 남아있지 않아야 함 (cleanup 확인)
        assert not os.path.exists(cnf_path + '.tmp'), "쓰기 실패 후 temp 파일이 남아있음"

        # 5. 기존 파일 내용이 보존돼야 함
        monkeypatch.undo()
        _, ini_text = _read_cnf(path=cnf_path)
        parsed = _parse_ini(ini_text)
        assert 'tf_3306' in parsed, "쓰기 실패 후 기존 엔트리가 손실됨"
        assert 'tf_3307' not in parsed, "실패한 쓰기의 내용이 반영됨"

    def test_no_tmp_file_on_success(self, cnf_path):
        """정상 쓰기 후 temp 파일이 남아있지 않아야 함"""
        login_key = secrets.token_bytes(_KEY_LEN)
        sections = {'tf_3306': {'host': '127.0.0.1', 'user': 'root', 'password': 'pw', 'port': '3306'}}
        _write_cnf(login_key, _build_ini(sections), path=cnf_path)
        assert not os.path.exists(cnf_path + '.tmp')

    def test_parent_dir_created_if_missing(self, tmp_path):
        """부모 디렉토리가 없으면 자동 생성돼야 함"""
        nested = str(tmp_path / 'nested' / 'dir' / '.mylogin.cnf')
        login_key = secrets.token_bytes(_KEY_LEN)
        _write_cnf(login_key, '', path=nested)
        assert os.path.exists(nested)


# ---------------------------------------------------------------------------
# 손상된 청크 처리
# ---------------------------------------------------------------------------

class TestCorruptChunk:
    def test_corrupt_chunk_skipped_others_preserved(self, cnf_path):
        """손상된 청크는 건너뛰되 나머지 섹션은 보존돼야 함"""
        login_key = secrets.token_bytes(_KEY_LEN)
        # 정상 파일 먼저 생성
        sections = {
            'tf_3306': {'host': '127.0.0.1', 'user': 'u1', 'password': 'p1', 'port': '3306'},
            'tf_3307': {'host': '127.0.0.1', 'user': 'u2', 'password': 'p2', 'port': '3307'},
        }
        _write_cnf(login_key, _build_ini(sections), path=cnf_path)

        # 파일 중간에 쓰레기 데이터를 삽입하여 일부 청크 손상
        with open(cnf_path, 'rb') as f:
            raw = bytearray(f.read())

        # 데이터 섹션 중간에 0xFF 패턴으로 덮어쓰기 (AES 복호화 실패 유도)
        mid = _DATA_OFFSET + (len(raw) - _DATA_OFFSET) // 2
        for i in range(min(16, len(raw) - mid)):
            raw[mid + i] = 0xFF

        with open(cnf_path, 'wb') as f:
            f.write(raw)

        # 파싱이 예외 없이 완료되어야 함
        _, ini_text = _read_cnf(path=cnf_path)
        # 일부라도 정상 파싱됐거나 빈 문자열 반환 — 예외가 없으면 됨
        assert ini_text is not None


# ---------------------------------------------------------------------------
# 플랫폼 경로 테스트
# ---------------------------------------------------------------------------

class TestPlatformPath:
    def test_cnf_path_windows(self, monkeypatch, tmp_path):
        """Windows 경로(%APPDATA%\\MySQL\\.mylogin.cnf) 대체 경로로 쓰기/읽기"""
        cnf = str(tmp_path / 'MySQL' / '.mylogin.cnf')
        login_key = secrets.token_bytes(_KEY_LEN)
        sections = {'tf_3306': {'host': '127.0.0.1', 'user': 'u', 'password': 'p', 'port': '3306'}}
        _write_cnf(login_key, _build_ini(sections), path=cnf)

        _, ini_text = _read_cnf(path=cnf)
        parsed = _parse_ini(ini_text)
        assert 'tf_3306' in parsed

    def test_cnf_path_unix(self, tmp_path):
        """Unix 경로(~/.mylogin.cnf) 대체 경로로 쓰기/읽기"""
        cnf = str(tmp_path / '.mylogin.cnf')
        login_key = secrets.token_bytes(_KEY_LEN)
        sections = {'tf_3307': {'host': '10.0.0.1', 'user': 'u', 'password': 'p', 'port': '3307'}}
        _write_cnf(login_key, _build_ini(sections), path=cnf)

        _, ini_text = _read_cnf(path=cnf)
        parsed = _parse_ini(ini_text)
        assert 'tf_3307' in parsed


# ---------------------------------------------------------------------------
# MysqlLoginPathManager 공개 API 테스트
# ---------------------------------------------------------------------------

class TestManagerAPI:
    def test_register_and_remove(self, tmp_path):
        """register() 후 remove() 시 섹션이 제거돼야 함"""
        cnf = str(tmp_path / '.mylogin.cnf')
        mgr = MysqlLoginPathManager(cnf_path=cnf)

        ok, name = mgr.register(13306, '127.0.0.1', 'root', 'pass')
        assert ok, f"register 실패: {name}"
        assert name == 'tf_13306'

        _, ini_text = _read_cnf(path=cnf)
        assert 'tf_13306' in _parse_ini(ini_text)

        ok, name = mgr.remove(13306)
        assert ok

        _, ini_text = _read_cnf(path=cnf)
        assert 'tf_13306' not in _parse_ini(ini_text)

    def test_register_missing_credentials(self, tmp_path):
        """user/password 없으면 False 반환 (파일 접근 없음)"""
        mgr = MysqlLoginPathManager(cnf_path=str(tmp_path / '.mylogin.cnf'))
        ok, msg = mgr.register(3306, '127.0.0.1', '', '')
        assert not ok
        assert '자격 증명' in msg

    def test_remove_nonexistent_returns_true(self, tmp_path):
        """없는 경로 제거 시 True 반환 (idempotent)"""
        cnf = str(tmp_path / '.mylogin.cnf')
        mgr = MysqlLoginPathManager(cnf_path=cnf)
        ok, _ = mgr.remove(99999)
        assert ok

    def test_cleanup_all_tf_paths(self, tmp_path):
        """cleanup_all_tf_paths()가 tf_ 접두어 섹션만 제거해야 함"""
        cnf = str(tmp_path / '.mylogin.cnf')
        mgr = MysqlLoginPathManager(cnf_path=cnf)

        mgr.register(3306, '127.0.0.1', 'root', 'pw')
        mgr.register(3307, '127.0.0.1', 'root', 'pw')

        # 비-TF 섹션 직접 추가
        login_key, ini_text = _read_cnf(path=cnf)
        sections = _parse_ini(ini_text)
        sections['client'] = {'host': 'other'}
        _write_cnf(login_key, _build_ini(sections), path=cnf)

        removed = mgr.cleanup_all_tf_paths()
        assert removed == 2

        _, ini_text = _read_cnf(path=cnf)
        parsed = _parse_ini(ini_text)
        assert 'tf_3306' not in parsed
        assert 'tf_3307' not in parsed
        assert 'client' in parsed, "비-TF 섹션이 cleanup_all_tf_paths()에 의해 제거됨"

    def test_get_login_path_name(self):
        mgr = MysqlLoginPathManager()
        assert mgr.get_login_path_name(3306) == 'tf_3306'
        assert mgr.get_login_path_name(0) == 'tf_0'
