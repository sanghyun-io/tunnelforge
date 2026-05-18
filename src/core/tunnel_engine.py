from sshtunnel import SSHTunnelForwarder
import paramiko
import socket
import os

from src.core.logger import get_logger
from src.core.constants import DEFAULT_LOCAL_HOST

logger = get_logger('tunnel_engine')


class TunnelEngine:
    def __init__(self):
        self.active_tunnels = {}  # { tunnel_id: server_object or None(직접 연결) }
        self.tunnel_configs = {}  # { tunnel_id: config } - 연결 정보 저장용

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
            raise FileNotFoundError(f"키 파일을 찾을 수 없습니다: {key_path}")

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
                attempt_logs.append(f"  - {key_name}: {type(e).__name__}: {str(e)}")
                continue

        # 3. 모든 시도가 실패했을 때
        # cryptography 라이브러리가 없으면 OpenSSH 포맷을 못 읽을 수 있음
        error_details = "\n".join(attempt_logs)
        raise Exception(
            f"키 파일을 인식할 수 없습니다.\n"
            f"키 파일: {key_path}\n"
            f"시도한 키 형식별 에러:\n{error_details}\n\n"
            f"💡 OpenSSH 포맷인 경우 'pip install cryptography' 필요"
        )

    def start_tunnel(self, config, check_port: bool = True):
        """SSH 터널 또는 직접 연결 시작

        Args:
            config: 터널 설정
            check_port: 포트 충돌 체크 여부 (자동 연결 시 사용)

        Returns:
            (success, message) 튜플
        """
        tid = config['id']

        # 이미 실행 중인지 확인
        if tid in self.active_tunnels:
            if config.get('connection_mode') == 'direct':
                return True, "이미 연결 중입니다."
            elif self.active_tunnels[tid] and self.active_tunnels[tid].is_active:
                return True, "이미 실행 중입니다."

        # 직접 연결 모드
        if config.get('connection_mode') == 'direct':
            self.active_tunnels[tid] = None  # 터널 객체 없음 (직접 연결)
            self.tunnel_configs[tid] = config
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
        tid = config['id']
        connection_logs = []

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
            server = SSHTunnelForwarder(
                (config['bastion_host'], int(config['bastion_port'])),
                ssh_username=config['bastion_user'],
                ssh_pkey=pkey_obj,  # 경로 대신 키 객체 전달
                remote_bind_address=(config['remote_host'], int(config['remote_port'])),
                local_bind_address=('0.0.0.0', int(config['local_port'])),
                set_keepalive=30.0
            )

            connection_logs.append("터널 연결 시작...")
            logger.debug("터널 연결 시작...")
            server.start()
            self.active_tunnels[tid] = server
            self.tunnel_configs[tid] = config
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

    def stop_tunnel(self, tid):
        """터널 종료"""
        if tid in self.active_tunnels:
            try:
                server = self.active_tunnels[tid]
                if server is not None:  # SSH 터널인 경우만 stop 호출
                    server.stop()
                del self.active_tunnels[tid]
                if tid in self.tunnel_configs:
                    del self.tunnel_configs[tid]
                logger.info(f"터널 종료됨: {tid}")
                return True
            except Exception as e:
                logger.warning(f"터널 종료 중 오류: {e}")
        return False

    def is_running(self, tid):
        """터널/연결이 활성화 상태인지 확인"""
        if tid in self.active_tunnels:
            server = self.active_tunnels[tid]
            if server is None:  # 직접 연결 모드
                return True
            return server.is_active
        return False

    def get_connection_info(self, tid):
        """실제 연결할 호스트/포트 반환"""
        if tid not in self.tunnel_configs:
            return None, None

        config = self.tunnel_configs[tid]
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
            # SSH 키 로드
            pkey_obj = self._load_private_key(config['bastion_key'])

            # 임시 터널 생성 (포트 자동 할당)
            temp_server = SSHTunnelForwarder(
                (config['bastion_host'], int(config['bastion_port'])),
                ssh_username=config['bastion_user'],
                ssh_pkey=pkey_obj,
                remote_bind_address=(config['remote_host'], int(config['remote_port'])),
                local_bind_address=(DEFAULT_LOCAL_HOST, 0)  # 0 = 자동 할당
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
            pkey_obj = self._load_private_key(config['bastion_key'])
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
        for tid, server in self.active_tunnels.items():
            if tid in self.tunnel_configs:
                config = self.tunnel_configs[tid]
                host, port = self.get_connection_info(tid)
                result.append({
                    'id': tid,
                    'tunnel_id': tid,  # DB 연결 다이얼로그에서 자격 증명 조회용
                    'name': config.get('name', 'Unknown'),
                    'host': host,
                    'port': port,
                    'mode': config.get('connection_mode', 'ssh_tunnel')
                })
        return result

    def stop_all(self):
        ids = list(self.active_tunnels.keys())
        for tid in ids:
            self.stop_tunnel(tid)

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
            temp_server = SSHTunnelForwarder(
                (config['bastion_host'], int(config['bastion_port'])),
                ssh_username=config['bastion_user'],
                ssh_pkey=pkey_obj,  # 경로 대신 키 객체 전달
                remote_bind_address=(config['remote_host'], int(config['remote_port'])),
                local_bind_address=(DEFAULT_LOCAL_HOST, 0)
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
