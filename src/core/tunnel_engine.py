from sshtunnel import SSHTunnelForwarder
import paramiko
import socket
import os

from src.core.logger import get_logger
from src.core.constants import DEFAULT_LOCAL_HOST
from src.core.ssh_host_trust import (
    SshHostKeyCheck,
    SshHostKeyTrustStore,
    SshHostTrustStoreError,
)

logger = get_logger('tunnel_engine')


class _SshHostTrustRequiredError(Exception):
    """Internal safe-to-display trust failure."""


def probe_ssh_host_key(host: str, port: int, timeout: int):
    """Read an SSH server key before authentication."""
    connected_socket = None
    transport = None
    try:
        connected_socket = socket.create_connection(
            (host, int(port)), timeout=timeout
        )
        transport = paramiko.Transport(connected_socket)
        transport.start_client(timeout=timeout)
        return transport.get_remote_server_key()
    finally:
        if transport is not None:
            transport.close()
        if connected_socket is not None:
            connected_socket.close()


class TunnelEngine:
    def __init__(self, trust_store=None, host_key_probe=None):
        self.active_tunnels = {}  # { tunnel_id: server_object or None(직접 연결) }
        self.tunnel_configs = {}  # { tunnel_id: config } - 연결 정보 저장용
        self.trust_store = (
            trust_store
            if trust_store is not None
            else SshHostKeyTrustStore()
        )
        self._host_key_probe = (
            host_key_probe
            if host_key_probe is not None
            else probe_ssh_host_key
        )

    def inspect_ssh_server(
        self, config: dict, timeout: int = 5
    ) -> SshHostKeyCheck:
        host = config.get('bastion_host')
        port = int(config.get('bastion_port', 22) or 22)
        server_key = self._host_key_probe(host, port, timeout)
        return self.trust_store.check(host, port, server_key)

    def approve_ssh_server(self, check: SshHostKeyCheck) -> None:
        self.trust_store.approve(check)

    def _require_trusted_host_key(self, config: dict, timeout: int = 5):
        host = config.get('bastion_host')
        port = int(config.get('bastion_port', 22) or 22)
        server_key = self._host_key_probe(host, port, timeout)
        try:
            check = self.trust_store.check(host, port, server_key)
        except SshHostTrustStoreError as exc:
            raise _SshHostTrustRequiredError(
                "SSH 호스트 신뢰 저장소를 읽을 수 없어 연결을 차단했습니다."
            ) from exc

        if check.status == "approval_required":
            raise _SshHostTrustRequiredError(
                "SSH 서버 신뢰 승인이 필요합니다.\n"
                f"서버: {check.host}:{check.port}\n"
                f"키 형식: {check.key_type}\n"
                f"지문: {check.fingerprint_sha256}"
            )
        if check.status == "changed":
            raise _SshHostTrustRequiredError(
                "SSH 서버 호스트 키가 변경되어 연결을 차단했습니다.\n"
                f"서버: {check.host}:{check.port}\n"
                f"이전 지문: {check.previous_fingerprint_sha256}\n"
                f"현재 지문: {check.fingerprint_sha256}"
            )
        return server_key

    def is_port_available(self, port: int) -> bool:
        """포트가 사용 가능한지 확인"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.bind(('0.0.0.0', port))
            s.close()
            return True
        except OSError:
            return False

    def _load_private_key(self, key_path):
        """
        SSH 키를 명시적으로 로드합니다.
        순서: RSA -> Ed25519 -> ECDSA -> (DSS는 paramiko 3.x 미지원)
        """
        key_path = os.path.expanduser(key_path)

        # 1. 키 파일 존재 확인
        if not os.path.exists(key_path):
            raise FileNotFoundError("키 파일을 찾을 수 없습니다.")

        # 모든 시도에 대한 로그 수집
        attempt_logs = []

        # 2. 여러 키 타입으로 로드 시도
        # Paramiko는 OpenSSH 포맷일 경우 RSAKey로 로드하려 하면 실패할 수 있음
        # 따라서 범용적인 PKey 로딩을 시도하거나 순차적으로 시도

        key_classes = [
            ("RSA", paramiko.RSAKey),
            ("Ed25519", paramiko.Ed25519Key),
            ("ECDSA", paramiko.ECDSAKey),
        ]
        # paramiko 3.x에서 DSSKey(DSA) 지원이 제거됨 - 필요시에만 추가
        if hasattr(paramiko, 'DSSKey'):
            key_classes.append(("DSS", paramiko.DSSKey))

        for key_name, k_cls in key_classes:
            try:
                # 암호가 있는 키라면 password 인자가 필요하지만, 일단 없는 것으로 가정
                key = k_cls.from_private_key_file(key_path)
                logger.info(f"SSH 키 로드 성공: {key_name} 형식")
                return key
            except paramiko.ssh_exception.PasswordRequiredException:
                raise Exception("키 파일에 비밀번호(Passphrase)가 걸려있습니다. 현재 버전은 비밀번호를 지원하지 않습니다.")
            except Exception as e:
                safe_error = str(e).replace(key_path, "[redacted]")
                attempt_logs.append(
                    f"  - {key_name}: {type(e).__name__}: {safe_error}"
                )
                continue

        # 3. 모든 시도가 실패했을 때
        # cryptography 라이브러리가 없으면 OpenSSH 포맷을 못 읽을 수 있음
        error_details = "\n".join(attempt_logs)
        raise Exception(
            f"키 파일을 인식할 수 없습니다.\n"
            f"시도한 키 형식별 에러:\n{error_details}\n\n"
            f"💡 OpenSSH 포맷인 경우 'pip install cryptography' 필요"
        )

    def _build_forwarder(
        self,
        config,
        local_bind_address,
        pkey_obj,
        ssh_host_key,
        set_keepalive=None,
    ):
        """SSHTunnelForwarder 공통 kwargs 조립 (모듈 전역 SSHTunnelForwarder 참조 필수)

        Args:
            config: 터널 설정 (bastion_host/bastion_port/bastion_user/remote_host/remote_port)
            local_bind_address: 로컬 바인드 주소 튜플
            pkey_obj: 이미 로드된 SSH 키 객체
            ssh_host_key: 현재 연결에서 검증한 SSH 서버 공개 키
            set_keepalive: keepalive 간격(초). None이면 kwarg 자체를 생략(라이브러리 기본값 유지)

        Returns:
            생성된 (미시작) SSHTunnelForwarder 인스턴스
        """
        kwargs = dict(
            ssh_username=config['bastion_user'],
            ssh_pkey=pkey_obj,  # 경로 대신 키 객체 전달
            ssh_host_key=ssh_host_key,
            remote_bind_address=(config['remote_host'], int(config['remote_port'])),
            local_bind_address=local_bind_address,
        )
        if set_keepalive is not None:
            kwargs['set_keepalive'] = set_keepalive

        return SSHTunnelForwarder(
            (config['bastion_host'], int(config['bastion_port'])),
            **kwargs,
        )

    def start_tunnel(self, config, check_port: bool = True):
        """SSH 터널 또는 직접 연결 시작

        Args:
            config: 터널 설정
            check_port: 포트 충돌 체크 여부 (자동 연결 시 사용)

        Returns:
            (success, message) 튜플
        """
        tunnel_id = config['id']

        # 이미 실행 중인지 확인
        if tunnel_id in self.active_tunnels:
            if config.get('connection_mode') == 'direct':
                return True, "이미 연결 중입니다."
            elif self.active_tunnels[tunnel_id] and self.active_tunnels[tunnel_id].is_active:
                return True, "이미 실행 중입니다."

        # 직접 연결 모드
        if config.get('connection_mode') == 'direct':
            self.active_tunnels[tunnel_id] = None  # 터널 객체 없음 (직접 연결)
            self.tunnel_configs[tunnel_id] = config
            logger.info(f"직접 연결 모드: {config['name']} -> {config['remote_host']}:{config['remote_port']}")
            return True, f"직접 연결: {config['remote_host']}:{config['remote_port']}"

        # SSH 터널 모드 - 포트 충돌 체크
        if check_port:
            local_port = int(config.get('local_port', 0))
            if local_port > 0 and not self.is_port_available(local_port):
                return False, f"포트 {local_port}이(가) 이미 사용 중입니다."

        # SSH 터널 모드
        return self._start_ssh_tunnel(config)

    def _start_ssh_tunnel(self, config):
        """SSH 터널 시작 (내부 메서드)"""
        tunnel_id = config['id']
        connection_logs = []

        try:
            ssh_host_key = self._require_trusted_host_key(config, timeout=5)
            connection_logs.append(f"🚀 터널 시작 시도: {config['name']}")
            connection_logs.append(f"   Bastion: {config['bastion_user']}@{config['bastion_host']}:{config['bastion_port']}")
            connection_logs.append(f"   Target: {config['remote_host']}:{config['remote_port']}")
            connection_logs.append(f"   Local Port: {config['local_port']}")

            for log in connection_logs:
                logger.debug(log)

            # 키 객체 직접 로드
            connection_logs.append("SSH 키 로드 시도...")
            logger.debug("SSH 키 로드 시도...")
            pkey_obj = self._load_private_key(config['bastion_key'])
            connection_logs.append("✅ SSH 키 로드 성공")

            connection_logs.append("SSH 터널 생성 중...")
            logger.debug("SSH 터널 생성 중...")
            server = self._build_forwarder(
                config,
                local_bind_address=('0.0.0.0', int(config['local_port'])),
                pkey_obj=pkey_obj,
                ssh_host_key=ssh_host_key,
                set_keepalive=30.0,
            )

            connection_logs.append("터널 연결 시작...")
            logger.debug("터널 연결 시작...")
            server.start()
            self.active_tunnels[tunnel_id] = server
            self.tunnel_configs[tunnel_id] = config
            logger.info(f"터널 연결 성공! (Local {config['local_port']} -> Remote {config['remote_host']})")
            return True, "연결 성공"

        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__

            # 상세 에러 로그 구성
            full_error = "❌ 터널 연결 실패\n"
            full_error += f"에러 타입: {error_type}\n"
            full_error += f"에러 메시지: {error_msg}\n\n"
            full_error += "📋 연결 시도 로그:\n"
            full_error += "\n".join(connection_logs)

            logger.error(full_error)
            return False, full_error

    def stop_tunnel(self, tunnel_id):
        """터널 종료"""
        if tunnel_id in self.active_tunnels:
            try:
                server = self.active_tunnels[tunnel_id]
                if server is not None:  # SSH 터널인 경우만 stop 호출
                    server.stop()
                del self.active_tunnels[tunnel_id]
                if tunnel_id in self.tunnel_configs:
                    del self.tunnel_configs[tunnel_id]
                logger.info(f"터널 종료됨: {tunnel_id}")
                return True
            except Exception as e:
                logger.warning(f"터널 종료 중 오류: {e}")
        return False

    def is_running(self, tunnel_id):
        """터널/연결이 활성화 상태인지 확인"""
        if tunnel_id in self.active_tunnels:
            server = self.active_tunnels[tunnel_id]
            if server is None:  # 직접 연결 모드
                return True
            return server.is_active
        return False

    def get_connection_info(self, tunnel_id):
        """실제 연결할 호스트/포트 반환"""
        if tunnel_id not in self.tunnel_configs:
            return None, None

        config = self.tunnel_configs[tunnel_id]
        if config.get('connection_mode') == 'direct':
            return config['remote_host'], int(config['remote_port'])
        else:
            return DEFAULT_LOCAL_HOST, int(config['local_port'])

    def create_temp_tunnel(self, config):
        """
        테스트용 임시 터널 생성 (local_port=0으로 자동 할당)
        반환: (success, temp_server, error_msg)
        """
        # 직접 연결 모드인 경우 터널 불필요
        if config.get('connection_mode') == 'direct':
            return True, None, ""

        try:
            ssh_host_key = self._require_trusted_host_key(config, timeout=5)
            # SSH 키 로드
            pkey_obj = self._load_private_key(config['bastion_key'])

            # 임시 터널 생성 (포트 자동 할당)
            temp_server = self._build_forwarder(
                config,
                local_bind_address=(DEFAULT_LOCAL_HOST, 0),  # 0 = 자동 할당
                pkey_obj=pkey_obj,
                ssh_host_key=ssh_host_key,
            )

            temp_server.start()
            logger.debug(f"임시 터널 생성: localhost:{temp_server.local_bind_port} -> {config['remote_host']}:{config['remote_port']}")
            return True, temp_server, ""

        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            return False, None, error_msg

    def close_temp_tunnel(self, temp_server):
        """임시 터널 종료"""
        if temp_server:
            try:
                temp_server.stop()
                logger.debug("임시 터널 종료됨")
            except Exception as e:
                logger.warning(f"임시 터널 종료 중 오류: {e}")

    def get_temp_tunnel_port(self, temp_server):
        """임시 터널의 로컬 포트 반환"""
        if temp_server:
            return temp_server.local_bind_port
        return None

    def test_target_reachable_from_bastion(self, config, timeout: int = 5):
        """Bastion에서 Target DB 포트로 direct-tcpip 채널을 열 수 있는지 확인합니다."""
        if config.get('connection_mode') == 'direct':
            return self._test_direct_connection(config)

        client = None
        channel = None
        target_host = config.get('remote_host')
        target_port = int(config.get('remote_port', 0) or 0)
        bastion_host = config.get('bastion_host')
        bastion_port = int(config.get('bastion_port', 22) or 22)
        bastion_user = config.get('bastion_user')

        try:
            ssh_host_key = self._require_trusted_host_key(config, timeout=timeout)
            pkey_obj = self._load_private_key(config['bastion_key'])
            client = paramiko.SSHClient()
            host_key_name = (
                bastion_host
                if bastion_port == 22
                else f"[{bastion_host}]:{bastion_port}"
            )
            client.get_host_keys().add(
                host_key_name, ssh_host_key.get_name(), ssh_host_key
            )
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                hostname=bastion_host,
                port=bastion_port,
                username=bastion_user,
                pkey=pkey_obj,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
            )

            transport = client.get_transport()
            if not transport or not transport.is_active():
                return False, "Bastion SSH transport가 활성 상태가 아닙니다."

            channel = transport.open_channel(
                "direct-tcpip",
                (target_host, target_port),
                (DEFAULT_LOCAL_HOST, 0),
                timeout=timeout,
            )
            return True, f"Bastion에서 Target DB 포트 도달 성공: {target_host}:{target_port}"

        except paramiko.ssh_exception.ChannelException as e:
            return False, (
                f"Bastion에서 Target DB 포트로 SSH 채널을 열지 못했습니다.\n"
                f"대상: {target_host}:{target_port}\n"
                f"원인: {type(e).__name__}: {str(e)}"
            )
        except socket.timeout as e:
            return False, (
                f"Bastion에서 Target DB 포트 연결이 시간 초과되었습니다.\n"
                f"대상: {target_host}:{target_port}\n"
                f"원인: {type(e).__name__}: {str(e)}"
            )
        except Exception as e:
            return False, (
                f"Bastion에서 Target DB 포트 도달성 확인 실패\n"
                f"대상: {target_host}:{target_port}\n"
                f"원인: {type(e).__name__}: {str(e)}"
            )
        finally:
            if channel:
                try:
                    channel.close()
                except Exception:
                    pass
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def get_active_tunnels(self):
        """활성화된 터널/연결 목록 반환 (DB Export용)"""
        result = []
        for tunnel_id, server in self.active_tunnels.items():
            if tunnel_id in self.tunnel_configs:
                config = self.tunnel_configs[tunnel_id]
                host, port = self.get_connection_info(tunnel_id)
                result.append({
                    'id': tunnel_id,
                    'tunnel_id': tunnel_id,  # DB 연결 다이얼로그에서 자격 증명 조회용
                    'name': config.get('name', 'Unknown'),
                    'host': host,
                    'port': port,
                    'mode': config.get('connection_mode', 'ssh_tunnel')
                })
        return result

    def stop_all(self):
        ids = list(self.active_tunnels.keys())
        for tunnel_id in ids:
            self.stop_tunnel(tunnel_id)

    def test_connection(self, config):
        """테스트 연결"""
        # 직접 연결 모드인 경우
        if config.get('connection_mode') == 'direct':
            return self._test_direct_connection(config)

        # SSH 터널 모드
        return self._test_ssh_tunnel_connection(config)

    def _test_direct_connection(self, config):
        """직접 연결 테스트"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((config['remote_host'], int(config['remote_port'])))
            s.close()
            return True, f"✅ 직접 연결 성공: {config['remote_host']}:{config['remote_port']}"
        except Exception as e:
            return False, f"❌ 직접 연결 실패\n원인: {str(e)}"

    def _test_ssh_tunnel_connection(self, config):
        """SSH 터널 연결 테스트"""
        temp_server = None
        connection_logs = []

        try:
            ssh_host_key = self._require_trusted_host_key(config, timeout=5)
            connection_logs.append("📋 연결 테스트 시작")
            connection_logs.append(f"   Bastion: {config.get('bastion_user', 'N/A')}@{config.get('bastion_host', 'N/A')}:{config.get('bastion_port', 'N/A')}")
            connection_logs.append(f"   Target: {config.get('remote_host', 'N/A')}:{config.get('remote_port', 'N/A')}")

            if not config.get('bastion_key'):
                return False, "❌ SSH 키 파일 경로가 비어있습니다."

            # 키 객체 직접 로드 (테스트 시에도 동일하게 적용)
            connection_logs.append("🔑 SSH 키 로드 시도...")
            pkey_obj = self._load_private_key(config['bastion_key'])
            connection_logs.append("✅ SSH 키 로드 성공")

            connection_logs.append("🔗 임시 SSH 터널 생성 중...")
            temp_server = self._build_forwarder(
                config,
                local_bind_address=(DEFAULT_LOCAL_HOST, 0),
                pkey_obj=pkey_obj,
                ssh_host_key=ssh_host_key,
            )

            connection_logs.append("🚀 Bastion Host 연결 시도...")
            temp_server.start()
            bastion_msg = "✅ 1. Bastion Host 연결 성공"
            connection_logs.append(bastion_msg)

            connection_logs.append("🔗 Bastion에서 Target DB 포트 연결 시도...")
            target_success, target_msg = self.test_target_reachable_from_bastion(config, timeout=5)
            if target_success:
                db_msg = "✅ 2. Target DB 포트 도달 성공"
                connection_logs.append(f"{db_msg}\n{target_msg}")
            else:
                db_msg = f"❌ 2. Target DB 연결 실패\n원인: {target_msg}"
                connection_logs.append(db_msg)
                logs_summary = "\n".join(connection_logs)
                return False, f"{bastion_msg}\n{db_msg}\n\n📋 전체 로그:\n{logs_summary}"

            return True, f"{bastion_msg}\n{db_msg}\n\n모든 연결이 정상입니다!"

        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            connection_logs.append(f"❌ 실패: {error_type}: {error_msg}")

            logs_summary = "\n".join(connection_logs)
            return False, f"❌ 1. Bastion Host 연결 실패\n에러 타입: {error_type}\n원인: {error_msg}\n\n📋 전체 로그:\n{logs_summary}"

        finally:
            if temp_server:
                temp_server.stop()
