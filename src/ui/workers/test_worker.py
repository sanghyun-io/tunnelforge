"""
연결 테스트 및 SQL 실행 Worker 클래스
"""
import os
import subprocess
import tempfile
from enum import Enum
from PyQt6.QtCore import QThread, pyqtSignal


class TestType(Enum):
    """테스트 유형"""
    TUNNEL_ONLY = "tunnel"      # SSH 터널만 테스트
    DB_ONLY = "db"              # DB 인증만 테스트 (터널 경유)
    INTEGRATED = "integrated"   # 터널 + DB 통합 테스트


class ConnectionTestWorker(QThread):
    """연결 테스트 Worker"""
    progress = pyqtSignal(str)          # 진행 메시지
    finished = pyqtSignal(bool, str)    # (성공여부, 결과메시지)

    def __init__(self, test_type: TestType, tunnel_config: dict,
                 tunnel_engine, config_manager, parent=None):
        super().__init__(parent)
        self.test_type = test_type
        self.config = tunnel_config
        self.engine = tunnel_engine
        self.config_mgr = config_manager

    def run(self):
        try:
            if self.test_type == TestType.TUNNEL_ONLY:
                self._test_tunnel()
            elif self.test_type == TestType.DB_ONLY:
                self._test_db()
            else:
                self._test_integrated()
        except Exception as e:
            self.finished.emit(False, f"테스트 중 오류 발생: {str(e)}")

    def _test_tunnel(self):
        """SSH 터널 연결만 테스트"""
        self.progress.emit("🔗 SSH 터널 연결 테스트 중...")
        success, msg = self.engine.test_connection(self.config)
        self.finished.emit(success, msg)

    def _test_db(self):
        """DB 인증 테스트 (터널 경유)"""
        tid = self.config.get('id')
        is_direct = self.config.get('connection_mode') == 'direct'
        temp_server = None
        connector = None
        result_success = False
        result_msg = ""

        try:
            # DB 자격 증명 확인
            db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)
            if not db_user:
                result_success = False
                result_msg = "❌ DB 자격 증명이 저장되어 있지 않습니다.\n터널 설정에서 DB 사용자/비밀번호를 저장해주세요."
                return

            # 연결 정보 결정
            if is_direct:
                # 직접 연결 모드
                host = self.config.get('remote_host') or '127.0.0.1'
                port = int(self.config['remote_port'])
                self.progress.emit(f"🔗 직접 연결 모드: {host}:{port}")
            elif self.engine.is_running(tid):
                # 이미 활성화된 터널 사용
                host, port = self.engine.get_connection_info(tid)
                self.progress.emit(f"🔗 활성 터널 사용: localhost:{port}")
            else:
                self.progress.emit("🔎 Bastion → Target DB 포트 도달성 확인 중...")
                reachable, reach_msg = self.engine.test_target_reachable_from_bastion(self.config)
                if not reachable:
                    result_success = False
                    result_msg = f"❌ Target DB 포트 도달 실패\n\n{reach_msg}\n\n{self._aws_reachability_hint()}"
                    return
                self.progress.emit(f"✅ {reach_msg}")

                # 임시 터널 생성
                self.progress.emit("🔗 임시 SSH 터널 생성 중...")
                success, temp_server, error = self.engine.create_temp_tunnel(self.config)
                if not success:
                    result_success = False
                    result_msg = f"❌ SSH 터널 생성 실패\n{error}"
                    return

                host = '127.0.0.1'
                port = self.engine.get_temp_tunnel_port(temp_server)
                self.progress.emit(f"✅ 임시 터널 생성됨: localhost:{port}")

            engine = self._resolve_db_engine(host, port)
            connector = self._create_connector(engine, host, port, db_user, db_password)
            engine_label = self._engine_label(engine)
            self.progress.emit(f"🔐 {engine_label} 인증 테스트 중... ({db_user}@{host}:{port})")
            success, msg = connector.connect()

            if success:
                # 기본 스키마 검증 (있는 경우)
                default_schema = self.config.get('default_schema')
                if default_schema:
                    self.progress.emit(f"📂 스키마 '{default_schema}' 존재 확인 중...")
                    schema_exists = connector.schema_exists(default_schema)
                    if not schema_exists:
                        connector.disconnect()
                        result_success = False
                        result_msg = f"⚠️ DB 인증 성공, 스키마 없음\n\n스키마 '{default_schema}'가 존재하지 않습니다.\n\n사용자: {db_user}\n호스트: {host}:{port}"
                        return

                connector.disconnect()
                result_success = True
                result_msg = f"✅ DB 인증 성공!\n\n사용자: {db_user}\n호스트: {host}:{port}"
                result_msg += f"\n엔진: {engine_label}"
                if default_schema:
                    result_msg += f"\n스키마: {default_schema}"
            else:
                result_success = False
                result_msg = f"❌ DB 인증 실패\n\n{msg}"

        finally:
            # 연결 정리
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    pass

            # 임시 터널 정리 (finished 전에 실행)
            if temp_server:
                self.engine.close_temp_tunnel(temp_server)

            # 모든 정리 후 결과 전달
            self.finished.emit(result_success, result_msg)

    def _test_integrated(self):
        """통합 테스트 (터널 + DB)"""
        tid = self.config.get('id')
        is_direct = self.config.get('connection_mode') == 'direct'
        temp_server = None
        connector = None
        results = []
        result_success = False
        result_msg = ""

        try:
            # 1. 터널 테스트 (직접 연결 모드가 아닌 경우)
            if not is_direct:
                self.progress.emit("🔗 [1/2] SSH 터널 연결 테스트 중...")
                tunnel_success, tunnel_msg = self.engine.test_connection(self.config)

                if tunnel_success:
                    results.append("✅ 1. SSH 터널 연결 성공")
                else:
                    result_success = False
                    result_msg = f"❌ SSH 터널 연결 실패\n\n{tunnel_msg}"
                    return
            else:
                results.append("✅ 1. 직접 연결 모드 (SSH 터널 불필요)")

            # 2. DB 인증 테스트
            self.progress.emit("🔐 [2/2] DB 인증 테스트 중...")

            db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)
            if not db_user:
                results.append("⚠️ 2. DB 인증 테스트 건너뜀 (자격 증명 없음)")
                result_success = True
                result_msg = "\n".join(results) + "\n\n💡 DB 테스트를 위해 터널 설정에서 자격 증명을 저장해주세요."
                return

            # 연결 정보 결정
            if is_direct:
                host = self.config.get('remote_host') or '127.0.0.1'
                port = int(self.config['remote_port'])
            elif self.engine.is_running(tid):
                host, port = self.engine.get_connection_info(tid)
            else:
                self.progress.emit("🔎 Bastion → Target DB 포트 도달성 확인 중...")
                reachable, reach_msg = self.engine.test_target_reachable_from_bastion(self.config)
                if not reachable:
                    results.append(f"❌ 2. Target DB 포트 도달 실패: {reach_msg}")
                    result_success = False
                    result_msg = "\n".join(results) + f"\n\n{self._aws_reachability_hint()}"
                    return
                self.progress.emit(f"✅ {reach_msg}")

                # 임시 터널 생성
                success, temp_server, error = self.engine.create_temp_tunnel(self.config)
                if not success:
                    results.append(f"❌ 2. DB 테스트 실패 (터널 생성 오류: {error})")
                    result_success = False
                    result_msg = "\n".join(results)
                    return
                host = '127.0.0.1'
                port = self.engine.get_temp_tunnel_port(temp_server)

            engine = self._resolve_db_engine(host, port)
            connector = self._create_connector(engine, host, port, db_user, db_password)
            success, msg = connector.connect()

            if success:
                results.append(f"✅ 2. {self._engine_label(engine)} DB 인증 성공 ({db_user}@{host}:{port})")
                result_success = True
                result_msg = "\n".join(results) + "\n\n🎉 모든 테스트 통과!"
            else:
                results.append(f"❌ 2. DB 인증 실패: {msg}")
                result_success = False
                result_msg = "\n".join(results)

        finally:
            # 연결 정리
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    pass

            # 임시 터널 정리 (finished 전에 실행)
            if temp_server:
                self.engine.close_temp_tunnel(temp_server)

            # 모든 정리 후 결과 전달
            self.finished.emit(result_success, result_msg)

    def _resolve_db_engine(self, host: str, port: int) -> str:
        engine = self.config.get('db_engine')
        if engine in ('mysql', 'postgresql'):
            return engine
        raise ValueError("DB Engine을 먼저 선택해주세요. 연결 설정에서 MySQL 또는 PostgreSQL을 명시해야 합니다.")

    def _create_connector(self, engine: str, host: str, port: int, user: str, password: str):
        from src.core.db_core_service import RustDbConnector

        if engine == 'postgresql':
            database = self.config.get('default_database') or 'postgres'
            schema = self.config.get('default_schema') or ''
            return RustDbConnector(engine, host, port, user, password, database, schema)
        database = self.config.get('default_database') or self.config.get('default_schema')
        return RustDbConnector(engine, host, port, user, password, database or '')

    def _engine_label(self, engine: str) -> str:
        return 'PostgreSQL' if engine == 'postgresql' else 'MySQL'

    def _aws_reachability_hint(self) -> str:
        return (
            "AWS 점검 포인트:\n"
            "- RDS Security Group 인바운드 5432 소스가 Bastion의 Security Group 또는 private IP인지 확인\n"
            "- Bastion Security Group 아웃바운드가 RDS 5432로 허용되는지 확인\n"
            "- RDS와 Bastion이 같은 VPC/피어링/라우팅 경로에 있는지 확인\n"
            "- NACL이 5432 및 응답 ephemeral port를 막지 않는지 확인\n"
            "- RDS 엔드포인트와 포트, 기본 DB 이름이 맞는지 확인"
        )


class SQLExecutionWorker(QThread):
    """SQL 파일 실행 Worker"""
    progress = pyqtSignal(str)          # 진행 메시지
    output = pyqtSignal(str)            # SQL 실행 출력
    finished = pyqtSignal(bool, str)    # (성공여부, 결과메시지)

    def __init__(self, sql_file: str, host: str, port: int,
                 user: str, password: str, database: str = None, parent=None):
        super().__init__(parent)
        self.sql_file = sql_file
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database

    def run(self):
        temp_cnf = None
        try:
            # mysql CLI 존재 확인
            self.progress.emit("🔍 mysql CLI 확인 중...")
            if not self._check_mysql_cli():
                self.finished.emit(False,
                    "❌ mysql CLI를 찾을 수 없습니다.\n\n"
                    "MySQL Client가 설치되어 있고 PATH에 등록되어 있는지 확인해주세요.\n"
                    "- Windows: MySQL Installer에서 MySQL Server 설치\n"
                    "- Mac: brew install mysql-client\n"
                    "- Linux: apt install mysql-client")
                return

            # 임시 설정 파일 생성 (비밀번호 보안)
            self.progress.emit("🔐 임시 설정 파일 생성 중...")
            temp_cnf = self._create_temp_cnf()

            # SQL 파일 실행
            self.progress.emit(f"🚀 SQL 실행 중: {os.path.basename(self.sql_file)}")

            cmd = ['mysql', f'--defaults-file={temp_cnf}']
            if self.database:
                cmd.append(self.database)

            # SQL 파일을 stdin으로 전달
            with open(self.sql_file, 'r', encoding='utf-8') as f:
                sql_content = f.read()

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            stdout, stderr = process.communicate(input=sql_content, timeout=300)

            # 출력 전달
            if stdout:
                self.output.emit(stdout)
            if stderr:
                self.output.emit(f"[stderr] {stderr}")

            if process.returncode == 0:
                self.finished.emit(True, "✅ SQL 실행 완료!")
            else:
                self.finished.emit(False, f"❌ SQL 실행 실패 (exit code: {process.returncode})\n\n{stderr}")

        except subprocess.TimeoutExpired:
            self.finished.emit(False, "❌ SQL 실행 시간 초과 (5분)")
        except Exception as e:
            self.finished.emit(False, f"❌ SQL 실행 중 오류: {str(e)}")
        finally:
            # 임시 파일 정리
            if temp_cnf and os.path.exists(temp_cnf):
                try:
                    os.remove(temp_cnf)
                except Exception:
                    pass

    def _check_mysql_cli(self) -> bool:
        """mysql CLI 존재 여부 확인"""
        try:
            result = subprocess.run(
                ['mysql', '--version'],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def _create_temp_cnf(self) -> str:
        """임시 MySQL 설정 파일 생성 (비밀번호 노출 방지)"""
        fd, path = tempfile.mkstemp(suffix='.cnf', prefix='mysql_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write("[client]\n")
                f.write(f"host={self.host}\n")
                f.write(f"port={self.port}\n")
                f.write(f"user={self.user}\n")
                f.write(f"password={self.password}\n")
        except Exception:
            os.close(fd)
            raise
        return path
