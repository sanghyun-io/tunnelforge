from sshtunnel import SSHTunnelForwarder
import paramiko
import socket
import os

class TunnelEngine:
    def __init__(self):
        self.active_tunnels = {}  # { tunnel_id: server_object or None(직접 연결) }
        self.tunnel_configs = {}  # { tunnel_id: config } - 연결 정보 저장용

    def _load_private_key(self, key_path):
        """
        SSH 키를 명시적으로 로드합니다.
        순서: RSA -> Ed25519 -> ECDSA -> (DSS는 paramiko 3.x 미지원)
        """
        key_path = os.path.expanduser(key_path)
        
        # 1. 키 파일 존재 확인
        if not os.path.exists(key_path):
            raise FileNotFoundError(f"키 파일을 찾을 수 없습니다: {key_path}")

        last_exception = None
        
        # 2. 여러 키 타입으로 로드 시도
        # Paramiko는 OpenSSH 포맷일 경우 RSAKey로 로드하려 하면 실패할 수 있음
        # 따라서 범용적인 PKey 로딩을 시도하거나 순차적으로 시도
        
        key_classes = [
            paramiko.RSAKey,
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
        ]
        # paramiko 3.x에서 DSSKey(DSA) 지원이 제거됨 - 필요시에만 추가
        if hasattr(paramiko, 'DSSKey'):
            key_classes.append(paramiko.DSSKey)

        for k_cls in key_classes:
            try:
                # 암호가 있는 키라면 password 인자가 필요하지만, 일단 없는 것으로 가정
                return k_cls.from_private_key_file(key_path)
            except paramiko.ssh_exception.PasswordRequiredException:
                raise Exception("키 파일에 비밀번호(Passphrase)가 걸려있습니다. 현재 버전은 비밀번호를 지원하지 않습니다.")
            except Exception as e:
                last_exception = e
                continue
        
        # 3. 모든 시도가 실패했을 때
        # cryptography 라이브러리가 없으면 OpenSSH 포맷을 못 읽을 수 있음
        raise Exception(f"키 파일을 인식할 수 없습니다 (OpenSSH 포맷인 경우 'pip install cryptography' 필요).\n마지막 에러: {last_exception}")

    def start_tunnel(self, config):
        """SSH 터널 또는 직접 연결 시작"""
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
            print(f"🔗 직접 연결 모드: {config['name']} -> {config['remote_host']}:{config['remote_port']}")
            return True, f"직접 연결: {config['remote_host']}:{config['remote_port']}"

        # SSH 터널 모드
        return self._start_ssh_tunnel(config)

    def _start_ssh_tunnel(self, config):
        """SSH 터널 시작 (내부 메서드)"""
        tid = config['id']
        try:
            print(f"🚀 터널 시작 시도: {config['name']}")

            # 키 객체 직접 로드
            pkey_obj = self._load_private_key(config['bastion_key'])

            server = SSHTunnelForwarder(
                (config['bastion_host'], int(config['bastion_port'])),
                ssh_username=config['bastion_user'],
                ssh_pkey=pkey_obj,  # 경로 대신 키 객체 전달
                remote_bind_address=(config['remote_host'], int(config['remote_port'])),
                local_bind_address=('0.0.0.0', int(config['local_port'])),
                set_keepalive=30.0
            )

            server.start()
            self.active_tunnels[tid] = server
            self.tunnel_configs[tid] = config
            print(f"✅ 터널 연결 성공! (Local {config['local_port']} -> Remote {config['remote_host']})")
            return True, "연결 성공"

        except Exception as e:
            error_msg = str(e)
            print(f"❌ 터널 연결 실패: {error_msg}")
            return False, error_msg

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
                print(f"🛑 터널 종료됨: {tid}")
                return True
            except Exception as e:
                print(f"⚠️ 터널 종료 중 오류: {e}")
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
            return '127.0.0.1', int(config['local_port'])

    def get_active_tunnels(self):
        """활성화된 터널/연결 목록 반환 (DB Export용)"""
        result = []
        for tid, server in self.active_tunnels.items():
            if tid in self.tunnel_configs:
                config = self.tunnel_configs[tid]
                host, port = self.get_connection_info(tid)
                result.append({
                    'id': tid,
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
        try:
            if not config.get('bastion_key'):
                return False, "❌ SSH 키 파일 경로가 비어있습니다."

            # 키 객체 직접 로드 (테스트 시에도 동일하게 적용)
            pkey_obj = self._load_private_key(config['bastion_key'])

            temp_server = SSHTunnelForwarder(
                (config['bastion_host'], int(config['bastion_port'])),
                ssh_username=config['bastion_user'],
                ssh_pkey=pkey_obj,  # 경로 대신 키 객체 전달
                remote_bind_address=(config['remote_host'], int(config['remote_port'])),
                local_bind_address=('127.0.0.1', 0)
            )

            temp_server.start()
            bastion_msg = "✅ 1. Bastion Host 연결 성공"

            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect(('127.0.0.1', temp_server.local_bind_port))
                s.close()
                db_msg = "✅ 2. Target DB 포트 도달 성공"
            except Exception as e:
                db_msg = f"❌ 2. Target DB 연결 실패\n원인: {str(e)}"
                return False, f"{bastion_msg}\n{db_msg}"

            return True, f"{bastion_msg}\n{db_msg}\n\n모든 연결이 정상입니다!"

        except Exception as e:
            return False, f"❌ 1. Bastion Host 연결 실패\n원인: {str(e)}"

        finally:
            if temp_server:
                temp_server.stop()