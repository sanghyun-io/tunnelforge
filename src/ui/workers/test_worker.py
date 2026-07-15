"""
연결 테스트 및 SQL 실행 Worker 클래스
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple
from PyQt6.QtCore import QThread, pyqtSignal


class TestType(Enum):
    """테스트 유형"""
    __test__ = False
    TUNNEL_ONLY = "tunnel"      # SSH 터널만 테스트
    DB_ONLY = "db"              # DB 인증만 테스트 (터널 경유)
    INTEGRATED = "integrated"   # 터널 + DB 통합 테스트


@dataclass
class _ResolvedConnection:
    host: str
    port: int
    temp_server: object = None


@dataclass
class _ConnectionFailure:
    kind: str
    message: str


class ConnectionTestWorker(QThread):
    """연결 테스트 Worker"""
    progress = pyqtSignal(str)          # 진행 메시지
    # ⛔ 이름을 "finished"로 두면 QThread 내장 finished() 시그널(스레드가 실제로
    # 정지했을 때 발화)을 shadow해서 더 이상 접근할 수 없게 된다. 결과 전달용
    # 시그널은 별도 이름(test_finished)을 쓰고, 호출자는 worker 참조 해제를
    # 반드시 내장 finished()를 받은 뒤에만 하도록 한다(WP-3.9 Finding 1).
    test_finished = pyqtSignal(bool, str)    # (성공여부, 결과메시지)

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
            self.test_finished.emit(False, f"테스트 중 오류 발생: {str(e)}")

    def _test_tunnel(self):
        """SSH 터널 연결만 테스트"""
        self.progress.emit("🔗 SSH 터널 연결 테스트 중...")
        success, msg = self.engine.test_connection(self.config)
        self.test_finished.emit(success, msg)

    def _test_db(self):
        """DB 인증 테스트 (터널 경유)"""
        tid = self.config.get('id')
        resolved = None
        connector = None
        result_success = False
        result_msg = ""

        try:
            resolved, failure = self._resolve_connection(announce_connection=True)
            if failure:
                result_success = False
                if failure.kind == "target_unreachable":
                    result_msg = f"❌ Target DB 포트 도달 실패\n\n{failure.message}\n\n{self._aws_reachability_hint()}"
                else:
                    result_msg = f"❌ SSH 터널 생성 실패\n{failure.message}"
                return

            # SSH 신뢰/연결 확인이 끝난 뒤에만 DB 자격 증명을 읽는다.
            db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)
            if not db_user:
                result_success = False
                result_msg = "❌ DB 자격 증명이 저장되어 있지 않습니다.\n터널 설정에서 DB 사용자/비밀번호를 저장해주세요."
                return

            engine = self._resolve_db_engine(resolved.host, resolved.port)
            connector = self._create_connector(engine, resolved.host, resolved.port, db_user, db_password)
            engine_label = self._engine_label(engine)
            self.progress.emit(f"🔐 {engine_label} 인증 테스트 중... ({db_user}@{resolved.host}:{resolved.port})")
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
                        result_msg = f"⚠️ DB 인증 성공, 스키마 없음\n\n스키마 '{default_schema}'가 존재하지 않습니다.\n\n사용자: {db_user}\n호스트: {resolved.host}:{resolved.port}"
                        return

                connector.disconnect()
                result_success = True
                result_msg = f"✅ DB 인증 성공!\n\n사용자: {db_user}\n호스트: {resolved.host}:{resolved.port}"
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
            if resolved and resolved.temp_server:
                self.engine.close_temp_tunnel(resolved.temp_server)

            # 모든 정리 후 결과 전달
            self.test_finished.emit(result_success, result_msg)

    def _test_integrated(self):
        """통합 테스트 (터널 + DB)"""
        tid = self.config.get('id')
        is_direct = self.config.get('connection_mode') == 'direct'
        resolved = None
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

            resolved, failure = self._resolve_connection(announce_connection=False)
            if failure:
                result_success = False
                if failure.kind == "target_unreachable":
                    results.append(f"❌ 2. Target DB 포트 도달 실패: {failure.message}")
                    result_msg = "\n".join(results) + f"\n\n{self._aws_reachability_hint()}"
                else:
                    results.append(f"❌ 2. DB 테스트 실패 (터널 생성 오류: {failure.message})")
                    result_msg = "\n".join(results)
                return

            db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)
            if not db_user:
                results.append("⚠️ 2. DB 인증 테스트 건너뜀 (자격 증명 없음)")
                result_success = True
                result_msg = "\n".join(results) + "\n\n💡 DB 테스트를 위해 터널 설정에서 자격 증명을 저장해주세요."
                return

            engine = self._resolve_db_engine(resolved.host, resolved.port)
            connector = self._create_connector(engine, resolved.host, resolved.port, db_user, db_password)
            success, msg = connector.connect()

            if success:
                results.append(f"✅ 2. {self._engine_label(engine)} DB 인증 성공 ({db_user}@{resolved.host}:{resolved.port})")
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
            if resolved and resolved.temp_server:
                self.engine.close_temp_tunnel(resolved.temp_server)

            # 모든 정리 후 결과 전달
            self.test_finished.emit(result_success, result_msg)

    def _resolve_connection(
        self, *, announce_connection: bool
    ) -> Tuple[Optional[_ResolvedConnection], Optional[_ConnectionFailure]]:
        tid = self.config.get('id')
        is_direct = self.config.get('connection_mode') == 'direct'

        if is_direct:
            host = self.config.get('remote_host') or '127.0.0.1'
            port = int(self.config['remote_port'])
            if announce_connection:
                self.progress.emit(f"🔗 직접 연결 모드: {host}:{port}")
            return _ResolvedConnection(host, port), None

        if self.engine.is_running(tid):
            host, port = self.engine.get_connection_info(tid)
            if announce_connection:
                self.progress.emit(f"🔗 활성 터널 사용: localhost:{port}")
            return _ResolvedConnection(host, port), None

        self.progress.emit("🔎 Bastion → Target DB 포트 도달성 확인 중...")
        reachable, reach_msg = self.engine.test_target_reachable_from_bastion(self.config)
        if not reachable:
            return None, _ConnectionFailure("target_unreachable", reach_msg)
        self.progress.emit(f"✅ {reach_msg}")

        if announce_connection:
            self.progress.emit("🔗 임시 SSH 터널 생성 중...")
        success, temp_server, error = self.engine.create_temp_tunnel(self.config)
        if not success:
            return None, _ConnectionFailure("temp_tunnel_failed", error)

        port = self.engine.get_temp_tunnel_port(temp_server)
        if announce_connection:
            self.progress.emit(f"✅ 임시 터널 생성됨: localhost:{port}")
        return _ResolvedConnection('127.0.0.1', port, temp_server), None

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

from src.ui.workers.sql_execution_worker import SQLExecutionWorker
