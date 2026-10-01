from sshtunnel import SSHTunnelForwarder
import paramiko
import socket
import os
import threading
from contextlib import closing

from src.core import ssh_trust
from src.core.connection_trust import (
    register_endpoint_tls, resolve_tls_policy, unregister_endpoint_tls,
)
from src.core.logger import get_logger
from src.core.constants import DEFAULT_LOCAL_HOST

logger = get_logger('tunnel_engine')


class TunnelEngine:
    def __init__(self, known_hosts=None):
        self.active_tunnels = {}  # { tunnel_id: server_object or None(직접 연결) }
        self.tunnel_configs = {}  # { tunnel_id: config } - 연결 정보 저장용
        # SSH 신원 확인 (TF-STATUS-110). known_hosts: get_known_host/save_known_host 제공 객체
        # (None이면 ConfigManager를 지연 생성). confirmer가 없으면 처음 보는 호스트 키는 거부한다.
        self._known_hosts = known_hosts
        self.host_key_confirmer = None   # (HostKeyPrompt) -> bool
        self.passphrase_provider = None  # (key_path, retry) -> Optional[str]
        self._passphrases = {}           # 세션 메모리 전용. 절대 디스크/config에 쓰지 않는다.
        self._temp_endpoints = {}        # id(temp_server) -> (host, port)
        # 무인(예약) 실행 스레드에서는 사용자에게 묻지 않는다: 스레드별 플래그 (UI 스레드의 대화형 흐름과 분리)
        self._unattended = threading.local()

    @property
    def known_hosts(self):
        if self._known_hosts is None:
            from src.core.config_manager import ConfigManager
            self._known_hosts = ConfigManager()
        return self._known_hosts

    def _is_unattended(self):
        return bool(getattr(self._unattended, 'active', False))

    def start_tunnel_unattended(self, config, check_port: bool = True):
        """예약 백업처럼 사람이 없는 실행용 시작.

        처음 보는 SSH 호스트 키는 자동 수락하지 않고 실패하며(ssh_host_key_unknown), 비밀번호가 필요한
        개인키는 묻지 않고 실패한다. 세션 메모리의 캐시된 비밀번호도 쓰지 않는다. 호출한 스레드에서만 적용된다.
        """
        self._unattended.active = True
        try:
            return self.start_tunnel(config, check_port)
        finally:
            self._unattended.active = False

    def _verified_host_key(self, config):
        """Bastion 호스트 키를 TOFU 정책으로 검증하고 신뢰된 키를 반환한다."""
        return ssh_trust.verify_host_key(
            config['bastion_host'], int(config['bastion_port']),
            self.known_hosts, None if self._is_unattended() else self.host_key_confirmer,
        )

    def probe_bastion_fingerprint(self, host, port):
        """UI가 '호스트 키 갱신' 전에 현재 서버 지문을 보여주기 위한 조회 (저장하지 않음)."""
        key = ssh_trust.probe_host_key(host, int(port))
        return ssh_trust.HostKeyPrompt(host, int(port), key.get_name(), ssh_trust.fingerprint_of(key))

    def refresh_host_key(self, host, port):
        """명시적 '호스트 키 갱신' — 사용자가 새 지문을 확인한 뒤에만 호출한다."""
        return ssh_trust.refresh_host_key(host, int(port), self.known_hosts)

    def _register_db_tls(self, config, host, port, owner=None):
        register_endpoint_tls(host, port, resolve_tls_policy(config), owner or config['id'])

    def is_port_available(self, port: int) -> bool:
        """포트가 사용 가능한지 확인"""
        try:
            with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
                s.settimeout(1)
                s.bind((DEFAULT_LOCAL_HOST, port))
            return True
        except OSError:
            return False

    def _read_key(self, key_path, passphrase):
        """키 파일을 형식별로 읽는다. -> (key or None, 암호화 여부, 시도 로그)"""
        attempt_logs = []
        encrypted = False
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
                key = k_cls.from_private_key_file(key_path, password=passphrase)
                logger.info(f"SSH 키 로드 성공: {key_name} 형식")
                return key, False, attempt_logs
            except paramiko.ssh_exception.PasswordRequiredException:
                encrypted = True
            except Exception as e:
                attempt_logs.append(f"  - {key_name}: {type(e).__name__}: {str(e)}")
        return None, encrypted, attempt_logs

    def _load_private_key(self, key_path):
        """
        SSH 키를 명시적으로 로드합니다.
        순서: RSA -> Ed25519 -> ECDSA -> (DSS는 paramiko 3.x 미지원)
        암호화된 키는 passphrase_provider로 비밀번호를 물어 세션 메모리에만 보관한다.
        """
        key_path = os.path.expanduser(key_path)

        # 1. 키 파일 존재 확인
        if not os.path.exists(key_path):
            raise FileNotFoundError(f"키 파일을 찾을 수 없습니다: {key_path}")

        cache_id = os.path.abspath(key_path)
        cached = None if self._is_unattended() else self._passphrases.get(cache_id)
        key, encrypted, attempt_logs = self._read_key(key_path, cached)
        if key is not None:
            return key
        if cached is not None and not encrypted:
            # 캐시된 비밀번호가 틀려서 실패했는지 확인 (키가 실제로 암호화돼 있는지)
            _, encrypted, _ = self._read_key(key_path, None)

        if not encrypted:
            # cryptography 라이브러리가 없으면 OpenSSH 포맷을 못 읽을 수 있음
            error_details = "\n".join(attempt_logs)
            raise Exception(
                f"키 파일을 인식할 수 없습니다.\n"
                f"키 파일: {key_path}\n"
                f"시도한 키 형식별 에러:\n{error_details}\n\n"
                f"💡 OpenSSH 포맷인 경우 'pip install cryptography' 필요"
            )

        # 2. 암호화된 키: 비밀번호를 물어본다 (최대 3회, 저장하지 않음)
        self._passphrases.pop(cache_id, None)
        retry = cached is not None
        for _ in range(3):
            provider = None if self._is_unattended() else self.passphrase_provider
            passphrase = provider(key_path, retry) if provider else None
            if passphrase is None:
                raise ssh_trust.SshPassphraseRequired(key_path)
            key, _, _ = self._read_key(key_path, passphrase)
            if key is not None:
                self._passphrases[cache_id] = passphrase
                return key
            retry = True
        raise ssh_trust.SshPassphraseInvalid(key_path)

    @staticmethod
    def _describe_ssh_error(error):
        """SSH 핸드셰이크에서 고정한 호스트 키와 서버 키가 달라진 경우 안정 코드를 붙인다."""
        text = str(error)
        if 'Bad host key' in text or isinstance(error, paramiko.BadHostKeyException):
            return f"{text} (error_code={ssh_trust.SshHostKeyChanged.code})"
        return text

    def _build_forwarder(self, config, local_bind_address, pkey_obj, set_keepalive=None):
        """SSHTunnelForwarder 공통 kwargs 조립 (모듈 전역 SSHTunnelForwarder 참조 필수)

        Args:
            config: 터널 설정 (bastion_host/bastion_port/bastion_user/remote_host/remote_port)
            local_bind_address: 로컬 바인드 주소 튜플
            pkey_obj: 이미 로드된 SSH 키 객체
            set_keepalive: keepalive 간격(초). None이면 kwarg 자체를 생략(라이브러리 기본값 유지)

        Returns:
            생성된 (미시작) SSHTunnelForwarder 인스턴스
        """
        kwargs = dict(
            ssh_username=config['bastion_user'],
            ssh_pkey=pkey_obj,  # 경로 대신 키 객체 전달
            remote_bind_address=(config['remote_host'], int(config['remote_port'])),
            local_bind_address=local_bind_address,
            # 프로브로 검증한 키를 고정해, 검증 이후 서버 키가 바뀌면 핸드셰이크가 실패한다.
            ssh_host_key=self._verified_host_key(config),
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
            if not self.stop_tunnel(tunnel_id):
                return False, "기존 터널을 종료하지 못했습니다. 다시 시도해주세요."

        # 직접 연결 모드
        if config.get('connection_mode') == 'direct':
            self.active_tunnels[tunnel_id] = None  # 터널 객체 없음 (직접 연결)
            self.tunnel_configs[tunnel_id] = config
            self._register_db_tls(config, config['remote_host'], config['remote_port'])
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
        server = None

        try:
            connection_logs.append(f"🚀 터널 시작 시도: {config['name']}")
            connection_logs.append(f"   Bastion: {config['bastion_user']}@{config['bastion_host']}:{config['bastion_port']}")
            connection_logs.append(f"   Target: {config['remote_host']}:{config['remote_port']}")
            connection_logs.append(f"   Local Port: {config['local_port']}")
            connection_logs.append(f"   SSH Key: {config['bastion_key']}")

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
                local_bind_address=(DEFAULT_LOCAL_HOST, int(config['local_port'])),
                pkey_obj=pkey_obj,
                set_keepalive=30.0,
            )

            connection_logs.append("터널 연결 시작...")
            logger.debug("터널 연결 시작...")
            server.start()
            self.active_tunnels[tunnel_id] = server
            self.tunnel_configs[tunnel_id] = config
            self._register_db_tls(config, DEFAULT_LOCAL_HOST, server.local_bind_port)
            logger.info(f"터널 연결 성공! (Local {config['local_port']} -> Remote {config['remote_host']})")
            return True, "연결 성공"

        except Exception as e:
            self.close_temp_tunnel(server)
            error_msg = self._describe_ssh_error(e)
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
                config = self.tunnel_configs.get(tunnel_id)
                if server is not None:  # SSH 터널인 경우만 stop 호출
                    port = getattr(server, 'local_bind_port', None)
                    server.stop()
                    if port:
                        unregister_endpoint_tls(DEFAULT_LOCAL_HOST, port, tunnel_id)
                elif config:
                    unregister_endpoint_tls(config['remote_host'], config['remote_port'], tunnel_id)
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
            server = self.active_tunnels.get(tunnel_id)
            if server is None or not server.is_active:
                return None, None
            return DEFAULT_LOCAL_HOST, server.local_bind_port

    def create_temp_tunnel(self, config):
        """
        테스트용 임시 터널 생성 (local_port=0으로 자동 할당)
        반환: (success, temp_server, error_msg)
        """
        # 직접 연결 모드인 경우 터널 불필요
        if config.get('connection_mode') == 'direct':
            self._register_db_tls(config, config['remote_host'], config['remote_port'], f"temp:{config['id']}")
            return True, None, ""

        temp_server = None
        try:
            # SSH 키 로드
            pkey_obj = self._load_private_key(config['bastion_key'])

            # 임시 터널 생성 (포트 자동 할당)
            temp_server = self._build_forwarder(
                config,
                local_bind_address=(DEFAULT_LOCAL_HOST, 0),  # 0 = 자동 할당
                pkey_obj=pkey_obj,
            )

            temp_server.start()
            owner = f"temp:{id(temp_server)}"
            self._register_db_tls(config, DEFAULT_LOCAL_HOST, temp_server.local_bind_port, owner)
            self._temp_endpoints[id(temp_server)] = (DEFAULT_LOCAL_HOST, temp_server.local_bind_port, owner)
            logger.debug(f"임시 터널 생성: localhost:{temp_server.local_bind_port} -> {config['remote_host']}:{config['remote_port']}")
            return True, temp_server, ""

        except Exception as e:
            self.close_temp_tunnel(temp_server)
            error_msg = f"{type(e).__name__}: {self._describe_ssh_error(e)}"
            return False, None, error_msg

    def close_temp_tunnel(self, temp_server):
        """임시 터널 종료"""
        if temp_server:
            endpoint = self._temp_endpoints.pop(id(temp_server), None)
            if endpoint:
                unregister_endpoint_tls(*endpoint[:2], endpoint[2])
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
            pkey_obj = self._load_private_key(config['bastion_key'])
            host_key = self._verified_host_key(config)
            client = paramiko.SSHClient()
            # 검증된 키만 신뢰한다: 목록에 없는 키는 RejectPolicy가 거부한다.
            # paramiko 조회 이름: 22번 포트는 호스트명, 그 외는 [host]:port
            known_name = bastion_host if bastion_port == 22 else f"[{bastion_host}]:{bastion_port}"
            client.get_host_keys().add(known_name, host_key.get_name(), host_key)
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
            if tunnel_id in self.tunnel_configs and self.is_running(tunnel_id):
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
            with closing(socket.create_connection(
                (config['remote_host'], int(config['remote_port'])), timeout=5
            )):
                pass
            return True, f"✅ 직접 연결 성공: {config['remote_host']}:{config['remote_port']}"
        except Exception as e:
            return False, f"❌ 직접 연결 실패\n원인: {str(e)}"

    def _test_ssh_tunnel_connection(self, config):
        """SSH 터널 연결 테스트"""
        temp_server = None
        connection_logs = []

        try:
            connection_logs.append("📋 연결 테스트 시작")
            connection_logs.append(f"   Bastion: {config.get('bastion_user', 'N/A')}@{config.get('bastion_host', 'N/A')}:{config.get('bastion_port', 'N/A')}")
            connection_logs.append(f"   Target: {config.get('remote_host', 'N/A')}:{config.get('remote_port', 'N/A')}")
            connection_logs.append(f"   SSH Key: {config.get('bastion_key', 'N/A')}")

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
            self.close_temp_tunnel(temp_server)
