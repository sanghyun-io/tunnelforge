"""연결 신뢰 정책 (TF-STATUS-110): DB TLS 검증 정책과 안정 오류 코드.

- 프로필의 ``db_tls_mode`` / ``db_tls_ca_file`` 을 ``TlsPolicy`` 로 해석한다.
- 터널이 시작될 때 실제 접속 주소(host:port)에 정책을 등록하고, ``DbEndpoint`` 가
  생성될 때 그 주소에 맞는 정책을 자동으로 채운다. 그래서 커넥터/익스포터/SQL 에디터
  등 모든 DbEndpoint 생성 지점이 별도 배선 없이 같은 TLS 정책을 전달받는다.
"""
import ipaddress
import re
import threading
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

TLS_DISABLE = "disable"
TLS_VERIFY_CA = "verify_ca"
TLS_VERIFY_FULL = "verify_full"
TLS_MODES = (TLS_DISABLE, TLS_VERIFY_CA, TLS_VERIFY_FULL)

TLS_MODE_LABELS = {
    TLS_VERIFY_FULL: "인증서 + 호스트명 검증 (권장)",
    TLS_VERIFY_CA: "인증서(CA 체인)만 검증",
    TLS_DISABLE: "검증 안 함 (암호화 없음)",
}

ERROR_MESSAGES = {
    "tls_verification_failed": (
        "DB 서버 인증서를 검증하지 못했습니다. 인증서가 만료되었거나, 신뢰할 수 없거나, "
        "호스트명이 일치하지 않습니다. CA 파일과 TLS 설정을 확인하세요."
    ),
    "tls_unavailable": "DB 서버가 TLS를 제공하지 않습니다. 서버의 TLS 설정을 확인하세요.",
    "ssh_host_key_unknown": "처음 접속하는 SSH 서버입니다. 지문을 확인한 뒤 신뢰해야 연결할 수 있습니다.",
    "ssh_host_key_changed": (
        "SSH 서버의 호스트 키가 저장된 값과 다릅니다. 중간자 공격일 수 있어 연결을 차단했습니다. "
        "서버가 정상적으로 교체된 것이 확실할 때만 터널 설정에서 '호스트 키 갱신'을 사용하세요."
    ),
}

_ERROR_CODE_RE = re.compile(r"\(error_code=([a-z_]+)\)")


@dataclass(frozen=True)
class TlsPolicy:
    mode: str = TLS_DISABLE
    ca_file: str = ""
    server_name: str = ""


def extract_error_code(message: Optional[str]) -> Optional[str]:
    """``... (error_code=xxx)`` 꼬리표에서 안정 오류 코드를 꺼낸다."""
    match = _ERROR_CODE_RE.search(message or "")
    return match.group(1) if match else None


def friendly_error_message(code: Optional[str]) -> Optional[str]:
    return ERROR_MESSAGES.get(code or "")


def is_loopback_host(host: Optional[str]) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_tls_mode(connection_mode: str, host: str) -> str:
    """새 프로필의 기본값. 직접 연결 + loopback 만 비검증 허용 (SSH 터널은 예외 아님)."""
    if connection_mode == "direct" and is_loopback_host(host):
        return TLS_DISABLE
    return TLS_VERIFY_FULL


def resolve_tls_policy(config: Mapping) -> TlsPolicy:
    """프로필 -> TlsPolicy. 저장된 설정이 없는 레거시 프로필은 자동 승격하지 않고 disable."""
    mode = config.get("db_tls_mode")
    if mode not in TLS_MODES or mode == TLS_DISABLE:
        return TlsPolicy()
    server_name = str(config.get("db_tls_server_name") or "").strip()
    if not server_name and config.get("connection_mode", "ssh_tunnel") != "direct":
        # 터널 경유 시 host 는 로컬 포워딩 포트라서 인증서 이름과 다르다.
        server_name = str(config.get("remote_host") or "").strip()
    return TlsPolicy(mode, str(config.get("db_tls_ca_file") or "").strip(), server_name)


def insecure_connection_warning(config: Mapping) -> Optional[str]:
    """검증 없는 연결이면 상시 표시할 경고 문구, 아니면 None."""
    if resolve_tls_policy(config).mode != TLS_DISABLE:
        return None
    connection_mode = config.get("connection_mode", "ssh_tunnel")
    if connection_mode == "direct" and is_loopback_host(config.get("remote_host") or "127.0.0.1"):
        return None
    return "DB 연결이 암호화/서버 신원 검증 없이 이루어집니다. 프로필의 TLS 설정을 '검증'으로 바꾸세요."


_registry: Dict[Tuple[str, int], TlsPolicy] = {}
_registry_lock = threading.Lock()


def _key(host: str, port) -> Tuple[str, int]:
    return (str(host or "").strip().lower(), int(port))


def register_endpoint_tls(host: str, port, policy: TlsPolicy) -> None:
    with _registry_lock:
        if policy.mode == TLS_DISABLE:
            _registry.pop(_key(host, port), None)
        else:
            _registry[_key(host, port)] = policy


def unregister_endpoint_tls(host: str, port) -> None:
    with _registry_lock:
        _registry.pop(_key(host, port), None)


def lookup_endpoint_tls(host: str, port) -> TlsPolicy:
    try:
        key = _key(host, port)
    except (TypeError, ValueError):
        return TlsPolicy()
    with _registry_lock:
        return _registry.get(key, TlsPolicy())


def clear_registered_tls() -> None:
    with _registry_lock:
        _registry.clear()
