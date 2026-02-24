"""
MySQL Login Path Manager (.mylogin.cnf 직접 조작)

mysql_config_editor는 Windows에서 패스워드를 콘솔 Win32 API로 읽기 때문에
stdin 파이프가 통하지 않습니다. 대신 Python cryptography 라이브러리로
.mylogin.cnf 파일을 직접 읽고 써서 subprocess 없이 동작합니다.

파일 포맷 (MySQL 공식 binary format):
  [4 bytes] unused (0x00000000, 읽기 시 무시)
  [20 bytes] random login_key
  [줄 단위 반복]:
    [4 bytes uint32 LE] ciphertext 길이
    [ciphertext 길이 bytes] AES-128-ECB + PKCS#7 패딩으로 암호화된 1줄

AES 키 = login_key 바이트를 16바이트에 XOR 누적한 값
패딩 방식: PKCS#7 (mysql_config_editor / mysql CLI와 호환)
"""
import os
import struct
import secrets
import threading
import configparser
from typing import Dict, Tuple

from src.core.logger import get_logger

logger = get_logger('mysql_login_path')

_PREFIX = 'tf_'
_KEY_LEN = 20        # login key 길이 (고정)
_HEADER_LEN = 4      # 파일 앞 4바이트 key_len (uint32 LE, = _KEY_LEN)
_KEY_OFFSET = _HEADER_LEN          # key 시작 위치
_DATA_OFFSET = _KEY_OFFSET + _KEY_LEN  # 데이터 섹션 시작 위치 (= 24)

# Windows: %APPDATA%\MySQL\.mylogin.cnf
# Unix:    ~/.mylogin.cnf
if os.name == 'nt':
    _MYLOGIN_CNF = os.path.join(
        os.environ.get('APPDATA', os.path.expanduser('~')),
        'MySQL', '.mylogin.cnf',
    )
else:
    _MYLOGIN_CNF = os.path.join(os.path.expanduser('~'), '.mylogin.cnf')


# ---------------------------------------------------------------------------
# 저수준 파일 I/O
# ---------------------------------------------------------------------------

def _derive_aes_key(login_key: bytes) -> bytes:
    """login_key(20B) → AES-128 키(16B) (MySQL 공식 방식)"""
    aes_key = bytearray(16)
    for i, b in enumerate(login_key):
        aes_key[i % 16] ^= b
    return bytes(aes_key)


def _read_cnf(path: str = _MYLOGIN_CNF) -> Tuple[bytes, str]:
    """
    .mylogin.cnf 복호화.

    파일 포맷:
      [4 bytes]  unused (읽기 시 무시)
      [20 bytes] login key
      [줄 단위 반복]:
        [4 bytes uint32 LE] ciphertext 길이
        [ciphertext 길이 bytes] AES-128-ECB + PKCS#7 패딩 암호화된 1줄

    PKCS#7 unpadding 시도 후 실패하면 legacy NULL padding으로 fallback.

    Returns:
        (login_key, ini_text) — 파일 없으면 (새 랜덤 키, '')
    """
    if not os.path.exists(path):
        return secrets.token_bytes(_KEY_LEN), ''

    with open(path, 'rb') as f:
        raw = f.read()

    if len(raw) < _DATA_OFFSET:
        return secrets.token_bytes(_KEY_LEN), ''

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    login_key = raw[_KEY_OFFSET:_DATA_OFFSET]
    aes_key = _derive_aes_key(login_key)

    offset = _DATA_OFFSET
    lines = []

    while offset + 4 <= len(raw):
        cipher_len = struct.unpack_from('<I', raw, offset)[0]
        offset += 4
        ciphertext = raw[offset:offset + cipher_len]
        offset += cipher_len

        try:
            cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
            dec = cipher.decryptor()
            padded_plain = dec.update(ciphertext) + dec.finalize()
            # PKCS#7 unpadding 시도, 실패 시 legacy NULL padding fallback
            try:
                unpadder = PKCS7(128).unpadder()
                plain = unpadder.update(padded_plain) + unpadder.finalize()
            except ValueError:
                plain = padded_plain.rstrip(b'\x00')
            lines.append(plain.decode('utf-8', errors='replace'))
        except Exception as e:
            # 포맷 불일치(이전 버전, 손상 등) 시 해당 청크만 건너뜀
            logger.warning(f"MySQL login path 청크 복호화 실패 (건너뜀): {e}")
            continue

    return login_key, ''.join(lines)


def _write_cnf(login_key: bytes, ini_text: str, path: str = _MYLOGIN_CNF):
    """INI 텍스트를 줄 단위로 AES-128-ECB + PKCS#7 패딩으로 암호화하여 .mylogin.cnf 저장.

    mysql_config_editor / mysql CLI와 호환되는 공식 포맷으로 기록합니다.
    원자적 쓰기: temp 파일에 먼저 쓴 뒤 os.replace()로 교체하여
    쓰기 중단 시 기존 파일을 보존합니다.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    aes_key = _derive_aes_key(login_key)

    # 줄 끝 정규화 및 마지막 개행 보장
    text = ini_text.replace('\r\n', '\n').replace('\r', '\n')
    if text and not text.endswith('\n'):
        text += '\n'

    # dirname guard: 부모 디렉토리가 없으면 생성
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    tmp_path = path + '.tmp'
    try:
        with open(tmp_path, 'wb') as f:
            f.write(b'\x00' * _HEADER_LEN)  # 4바이트 unused (MySQL 공식: 0)
            f.write(login_key)

            # 줄 단위 개별 암호화 (PKCS#7 패딩: mysql_config_editor 호환)
            for line in text.splitlines(keepends=True):
                plain = line.encode('utf-8')
                padder = PKCS7(128).padder()
                padded = padder.update(plain) + padder.finalize()

                enc = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
                ciphertext = enc.update(padded) + enc.finalize()

                f.write(struct.pack('<I', len(ciphertext)))
                f.write(ciphertext)

        # 원자적 교체: rename 기반으로 기존 파일 보호
        try:
            os.replace(tmp_path, path)
        except OSError:
            # Windows에서 os.replace()가 WinError 5로 실패할 수 있음
            # (MoveFileExW MOVEFILE_REPLACE_EXISTING 권한 문제)
            # fallback: remove + rename
            if os.path.exists(path):
                os.remove(path)
            os.rename(tmp_path, path)
    except Exception:
        # 쓰기 실패 시 temp 파일 정리
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError as cleanup_err:
            logger.debug(f"임시 파일 정리 실패: {cleanup_err}")
        raise

    if os.name != 'nt':
        os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# INI 파싱/빌드
# ---------------------------------------------------------------------------

def _parse_ini(ini_text: str) -> Dict[str, Dict[str, str]]:
    parser = configparser.RawConfigParser()
    parser.read_string(ini_text)
    return {s: dict(parser.items(s)) for s in parser.sections()}


def _build_ini(sections: Dict[str, Dict[str, str]]) -> str:
    lines = []
    for section, values in sections.items():
        lines.append(f'[{section}]')
        for key, value in values.items():
            lines.append(f'{key} = {value}')
        lines.append('')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 공개 클래스
# ---------------------------------------------------------------------------

class MysqlLoginPathManager:
    """
    .mylogin.cnf 직접 조작으로 MySQL 로그인 경로 관리.

    사용 예:
        mgr = MysqlLoginPathManager()
        mgr.register(3307, '127.0.0.1', 'root', 'secret')
        # → mysql --login-path=tf_3307

        mgr.remove(3307)
        mgr.cleanup_all_tf_paths()  # tf_ 전체 제거
    """

    def __init__(self, cnf_path: str = None):
        # in-process 동시 접근 보호 (파일 수준 원자성은 _write_cnf의 os.replace()로 보장)
        self._lock = threading.Lock()
        self._cnf_path = cnf_path or _MYLOGIN_CNF

    def is_available(self) -> bool:
        """cryptography 라이브러리 사용 가능 여부 (의존성으로 항상 True)"""
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher  # noqa
            return True
        except ImportError:
            return False

    @staticmethod
    def get_login_path_name(port: int) -> str:
        """포트 번호 → 로그인 경로 이름 (예: 3307 → tf_3307)"""
        return f"{_PREFIX}{port}"

    def register(
        self,
        port: int,
        host: str,
        user: str,
        password: str,
    ) -> Tuple[bool, str]:
        """로그인 경로 등록 (없으면 추가, 있으면 덮어쓰기)

        Returns:
            (True, login_path_name) 또는 (False, error_message)
        """
        if not user or not password:
            return False, "DB 자격 증명이 없어 로그인 경로를 등록하지 않습니다."

        login_path = self.get_login_path_name(port)
        try:
            with self._lock:
                login_key, ini_text = _read_cnf(path=self._cnf_path)
                sections = _parse_ini(ini_text)
                sections[login_path] = {
                    'host': host,
                    'user': user,
                    'password': password,
                    'port': str(port),
                }
                _write_cnf(login_key, _build_ini(sections), path=self._cnf_path)
            logger.info(f"MySQL 로그인 경로 등록: {login_path} ({host}:{port})")
            return True, login_path
        except Exception as e:
            return False, str(e)

    def remove(self, port: int) -> Tuple[bool, str]:
        """로그인 경로 제거

        Returns:
            (True, login_path_name) 또는 (False, error_message)
        """
        login_path = self.get_login_path_name(port)
        try:
            with self._lock:
                login_key, ini_text = _read_cnf(path=self._cnf_path)
                sections = _parse_ini(ini_text)
                if login_path not in sections:
                    return True, login_path  # 이미 없음
                del sections[login_path]
                _write_cnf(login_key, _build_ini(sections), path=self._cnf_path)
            logger.info(f"MySQL 로그인 경로 제거: {login_path}")
            return True, login_path
        except Exception as e:
            return False, str(e)

    def cleanup_all_tf_paths(self) -> int:
        """tf_ 접두어를 가진 모든 로그인 경로 제거

        앱 정상 종료 또는 시작 시 잔류 경로 정리용.

        Returns:
            제거된 경로 수
        """
        try:
            with self._lock:
                login_key, ini_text = _read_cnf(path=self._cnf_path)
                sections = _parse_ini(ini_text)
                tf_keys = [s for s in sections if s.startswith(_PREFIX)]
                if not tf_keys:
                    return 0
                for k in tf_keys:
                    del sections[k]
                    logger.info(f"MySQL 로그인 경로 정리: {k}")
                _write_cnf(login_key, _build_ini(sections), path=self._cnf_path)
                return len(tf_keys)
        except Exception as e:
            logger.warning(f"MySQL 로그인 경로 cleanup 실패: {e}")
            return 0
