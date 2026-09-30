"""SSH 서버 신원 확인 (TF-STATUS-110): trust-on-first-use.

처음 보는 호스트 키는 사용자가 지문을 확인한 뒤에만 저장하고, 저장된 키와 다르면
연결을 차단한다(``ssh_host_key_changed``). 교체는 ``refresh_host_key`` 를 호출하는
명시적 "호스트 키 갱신" 동작으로만 가능하다.
"""
import base64
import hashlib
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import paramiko


@dataclass(frozen=True)
class HostKeyPrompt:
    host: str
    port: int
    key_type: str
    fingerprint: str


class SshTrustError(Exception):
    code = ""

    def __init__(self, text: str):
        super().__init__(f"{text} (error_code={self.code})" if self.code else text)


class SshHostKeyUnknown(SshTrustError):
    code = "ssh_host_key_unknown"

    def __init__(self, prompt: HostKeyPrompt):
        self.host, self.port = prompt.host, prompt.port
        self.key_type, self.fingerprint = prompt.key_type, prompt.fingerprint
        super().__init__(
            f"처음 접속하는 SSH 서버입니다: {prompt.host}:{prompt.port}\n"
            f"호스트 키({prompt.key_type}) 지문: {prompt.fingerprint}\n"
            "지문을 확인하고 신뢰한 뒤에만 연결할 수 있습니다."
        )


class SshHostKeyChanged(SshTrustError):
    code = "ssh_host_key_changed"

    def __init__(self, host: str, port: int, stored_fingerprint: str, fingerprint: str):
        self.host, self.port = host, port
        self.stored_fingerprint, self.fingerprint = stored_fingerprint, fingerprint
        super().__init__(
            f"SSH 서버의 호스트 키가 저장된 값과 다릅니다: {host}:{port}\n"
            f"저장된 지문: {stored_fingerprint}\n현재 지문: {fingerprint}\n"
            "중간자 공격일 수 있어 연결을 차단했습니다. 서버 교체가 확실할 때만 "
            "터널 설정의 '호스트 키 갱신'을 사용하세요."
        )


class SshPassphraseRequired(SshTrustError):
    def __init__(self, key_path: str):
        super().__init__(f"개인키가 비밀번호(Passphrase)로 보호되어 있습니다: {key_path}")


class SshPassphraseInvalid(SshTrustError):
    def __init__(self, key_path: str):
        super().__init__(f"개인키 비밀번호가 올바르지 않습니다: {key_path}")


def host_id(host: str, port) -> str:
    return f"{str(host).strip().lower()}:{int(port)}"


def fingerprint_of(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def entry_for_key(key: paramiko.PKey) -> dict:
    return {
        "key_type": key.get_name(),
        "key_b64": base64.b64encode(key.asbytes()).decode("ascii"),
        "fingerprint": fingerprint_of(key),
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def decode_key(entry: dict) -> paramiko.PKey:
    return paramiko.PKey.from_type_string(entry["key_type"], base64.b64decode(entry["key_b64"]))


def probe_host_key(host: str, port, timeout: float = 10) -> paramiko.PKey:
    """인증 없이 SSH 핸드셰이크만 수행해 서버 호스트 키를 얻는다."""
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        return transport.get_remote_server_key()
    finally:
        transport.close()


def verify_host_key(
    host: str,
    port,
    store,
    confirmer: Optional[Callable[[HostKeyPrompt], bool]] = None,
    timeout: float = 10,
) -> paramiko.PKey:
    """서버 키를 저장소와 대조한다. 신뢰할 수 있는 키(PKey)를 반환하거나 예외를 던진다.

    store: ``get_known_host(host, port)`` / ``save_known_host(host, port, entry)`` 제공 객체.
    """
    key = probe_host_key(host, port, timeout)
    fingerprint = fingerprint_of(key)
    stored = store.get_known_host(host, port)
    if stored is None:
        prompt = HostKeyPrompt(host, int(port), key.get_name(), fingerprint)
        if confirmer is None or not confirmer(prompt):
            raise SshHostKeyUnknown(prompt)
        store.save_known_host(host, port, entry_for_key(key))
        return key
    if stored.get("fingerprint") != fingerprint or stored.get("key_type") != key.get_name():
        raise SshHostKeyChanged(host, int(port), str(stored.get("fingerprint", "")), fingerprint)
    return key


def refresh_host_key(host: str, port, store, timeout: float = 10) -> HostKeyPrompt:
    """명시적 '키 갱신': 현재 서버 키로 저장값을 교체한다 (호출 전에 사용자가 확인해야 한다)."""
    key = probe_host_key(host, port, timeout)
    store.save_known_host(host, port, entry_for_key(key))
    return HostKeyPrompt(host, int(port), key.get_name(), fingerprint_of(key))
