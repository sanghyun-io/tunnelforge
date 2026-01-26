"""
MySQL Shell 기반 병렬 Export/Import
- 멀티스레드 병렬 처리
- FK 의존성 자동 분석 및 처리
- 전체 스키마 / 일부 테이블 지원
"""
import os
import subprocess
import shutil
import json
import threading
import time
import glob as glob_module
from datetime import datetime
from typing import List, Dict, Set, Tuple, Callable, Optional
from dataclasses import dataclass

from src.core.db_connector import MySQLConnector


@dataclass
class MySQLShellConfig:
    """MySQL Shell 연결 설정"""
    host: str
    port: int
    user: str
    password: str

    def get_uri(self) -> str:
        """mysqlsh URI 형식 반환"""
        return f"{self.user}:{self.password}@{self.host}:{self.port}"

    def get_masked_uri(self) -> str:
        """비밀번호 마스킹된 URI"""
        return f"{self.user}:****@{self.host}:{self.port}"


class MySQLShellChecker:
    """MySQL Shell 설치 확인"""

    @staticmethod
    def check_installation() -> Tuple[bool, str, Optional[str]]:
        """
        mysqlsh 설치 확인

        Returns:
            (설치여부, 메시지, 버전)
        """
        try:
            result = subprocess.run(
                ["mysqlsh", "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                version = result.stdout.strip()
                return True, version, version
            else:
                return False, "mysqlsh 실행 실패", None

        except FileNotFoundError:
            return False, "mysqlsh가 설치되어 있지 않습니다.", None
        except subprocess.TimeoutExpired:
            return False, "mysqlsh 버전 확인 시간 초과", None
        except Exception as e:
            return False, f"오류: {str(e)}", None

    @staticmethod
    def get_install_guide() -> str:
        """설치 가이드 반환"""
        return """
MySQL Shell 설치 방법:

[Windows]
1. https://dev.mysql.com/downloads/shell/ 에서 다운로드
2. MySQL Shell 8.x Windows (x86, 64-bit) MSI Installer 선택
3. 설치 후 PATH에 자동 추가됨

[macOS]
brew install mysql-shell

[Linux (Ubuntu/Debian)]
sudo apt-get install mysql-shell

[Linux (RHEL/CentOS)]
sudo yum install mysql-shell
"""


class ForeignKeyResolver:
    """FK 의존성 분석 및 해결"""

    def __init__(self, connector: MySQLConnector):
        self.connector = connector

    def get_all_dependencies(self, schema: str) -> Dict[str, Set[str]]:
        """
        스키마 내 모든 FK 의존성 조회

        Returns:
            { table: set(참조하는 부모 테이블들) }
        """
        query = """
        SELECT TABLE_NAME, REFERENCED_TABLE_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
        """
        rows = self.connector.execute(query, (schema,))

        deps = {}
        for row in rows:
            table = row['TABLE_NAME']
            ref_table = row['REFERENCED_TABLE_NAME']
            if table != ref_table:  # 자기 참조 제외
                if table not in deps:
                    deps[table] = set()
                deps[table].add(ref_table)

        return deps

    def resolve_required_tables(
        self,
        selected_tables: List[str],
        schema: str
    ) -> Tuple[List[str], List[str]]:
        """
        선택된 테이블에 필요한 FK 부모 테이블 자동 추가

        Args:
            selected_tables: 사용자가 선택한 테이블 목록
            schema: 스키마명

        Returns:
            (전체 필요 테이블 목록, 자동 추가된 테이블 목록)
        """
        all_deps = self.get_all_dependencies(schema)

        required = set(selected_tables)
        added = []

        # 재귀적으로 부모 테이블 추가
        changed = True
        while changed:
            changed = False
            for table in list(required):
                if table in all_deps:
                    for parent in all_deps[table]:
                        if parent not in required:
                            required.add(parent)
                            added.append(parent)
                            changed = True

        # 정렬된 목록 반환
        return sorted(list(required)), sorted(added)


class MySQLShellExporter:
    """MySQL Shell 기반 Export"""

    def __init__(self, config: MySQLShellConfig):
        self.config = config
        self._connector: Optional[MySQLConnector] = None

    def _get_connector(self) -> MySQLConnector:
        """내부 연결 관리"""
        if self._connector is None:
            self._connector = MySQLConnector(
                self.config.host,
                self.config.port,
                self.config.user,
                self.config.password
            )
            self._connector.connect()
        return self._connector

    def _cleanup(self):
        """연결 정리"""
        if self._connector:
            self._connector.disconnect()
            self._connector = None

    def export_full_schema(
        self,
        schema: str,
        output_dir: str,
        threads: int = 4,
        compression: str = "zstd",
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Tuple[bool, str]:
        """
        전체 스키마 Export (병렬 처리)

        Args:
            schema: 스키마명
            output_dir: 출력 디렉토리
            threads: 병렬 스레드 수
            compression: 압축 방식 (zstd, gzip, none)
            progress_callback: 진행 상황 콜백 (msg)
            table_progress_callback: 테이블별 진행률 콜백 (current, total, table_name)

        Returns:
            (성공여부, 메시지)
        """
        try:
            # 테이블 목록 가져오기 (진행률 모니터링용)
            tables = []
            if table_progress_callback:
                if progress_callback:
                    progress_callback("테이블 목록 조회 중...")
                connector = self._get_connector()
                tables = connector.get_tables(schema)

            # 기존 출력 디렉토리가 있으면 삭제 후 새로 생성
            if os.path.exists(output_dir):
                if progress_callback:
                    progress_callback(f"기존 폴더 삭제 중: {output_dir}")
                shutil.rmtree(output_dir)
                # Windows에서 삭제가 완전히 완료될 때까지 대기
                wait_count = 0
                while os.path.exists(output_dir) and wait_count < 20:
                    time.sleep(0.1)
                    wait_count += 1

            # mysqlsh가 직접 디렉토리를 생성하도록 부모 디렉토리만 확인
            parent_dir = os.path.dirname(output_dir)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)

            if progress_callback:
                if tables:
                    progress_callback(f"스키마 '{schema}' Export 시작 ({len(tables)}개 테이블, 스레드: {threads})")
                else:
                    progress_callback(f"스키마 '{schema}' Export 시작 (스레드: {threads})")

            # mysqlsh 명령 구성
            js_code = f"""
util.dumpSchemas(["{schema}"], "{output_dir.replace('\\', '/')}", {{
    threads: {threads},
    compression: "{compression}",
    chunking: true,
    bytesPerChunk: "64M",
    showProgress: true
}});
"""

            success, msg = self._run_mysqlsh(
                js_code,
                progress_callback,
                output_dir=output_dir,
                schema=schema,
                tables=tables if tables else None,
                table_progress_callback=table_progress_callback
            )

            if success:
                # Export 성공 후 메타데이터 파일 생성
                self._write_metadata(output_dir, schema, "full", tables)
                if progress_callback:
                    progress_callback(f"✅ Export 완료: {output_dir}")

            return success, msg

        except Exception as e:
            return False, f"Export 오류: {str(e)}"

    def export_tables(
        self,
        schema: str,
        tables: List[str],
        output_dir: str,
        threads: int = 4,
        compression: str = "zstd",
        include_fk_parents: bool = True,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Tuple[bool, str, List[str]]:
        """
        선택된 테이블만 Export (FK 의존성 자동 처리)

        Args:
            schema: 스키마명
            tables: 내보낼 테이블 목록
            output_dir: 출력 디렉토리
            threads: 병렬 스레드 수
            compression: 압축 방식
            include_fk_parents: FK 부모 테이블 자동 포함 여부
            progress_callback: 진행 상황 콜백 (msg)
            table_progress_callback: 테이블별 진행률 콜백 (current, total, table_name)

        Returns:
            (성공여부, 메시지, 실제 Export된 테이블 목록)
        """
        try:
            added_tables = []
            final_tables = tables.copy()

            # FK 부모 테이블 자동 추가
            if include_fk_parents:
                if progress_callback:
                    progress_callback("FK 의존성 분석 중...")

                connector = self._get_connector()
                resolver = ForeignKeyResolver(connector)
                final_tables, added_tables = resolver.resolve_required_tables(tables, schema)

                if added_tables and progress_callback:
                    progress_callback(f"FK 의존성으로 {len(added_tables)}개 테이블 추가: {', '.join(added_tables)}")

            # 기존 출력 디렉토리가 있으면 삭제 후 새로 생성
            if os.path.exists(output_dir):
                if progress_callback:
                    progress_callback(f"기존 폴더 삭제 중: {output_dir}")
                shutil.rmtree(output_dir)
                # Windows에서 삭제가 완전히 완료될 때까지 대기
                wait_count = 0
                while os.path.exists(output_dir) and wait_count < 20:
                    time.sleep(0.1)
                    wait_count += 1

            # mysqlsh가 직접 디렉토리를 생성하도록 부모 디렉토리만 확인
            parent_dir = os.path.dirname(output_dir)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)

            if progress_callback:
                progress_callback(f"{len(final_tables)}개 테이블 Export 시작 (스레드: {threads})")

            # 테이블 목록 JSON 형식으로 변환
            tables_json = json.dumps(final_tables)

            # mysqlsh 명령 구성
            js_code = f"""
util.dumpTables("{schema}", {tables_json}, "{output_dir.replace('\\', '/')}", {{
    threads: {threads},
    compression: "{compression}",
    chunking: true,
    bytesPerChunk: "64M",
    showProgress: true
}});
"""

            success, msg = self._run_mysqlsh(
                js_code,
                progress_callback,
                output_dir=output_dir,
                schema=schema,
                tables=final_tables,
                table_progress_callback=table_progress_callback
            )

            if success:
                # Export 성공 후 메타데이터 파일 생성
                self._write_metadata(output_dir, schema, "partial", final_tables, added_tables)
                if progress_callback:
                    progress_callback(f"✅ {len(final_tables)}개 테이블 Export 완료")
                return True, f"{len(final_tables)}개 테이블 Export 완료", final_tables
            else:
                return False, msg, []

        except Exception as e:
            return False, f"Export 오류: {str(e)}", []
        finally:
            self._cleanup()

    def _run_mysqlsh(
        self,
        js_code: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        output_dir: Optional[str] = None,
        schema: Optional[str] = None,
        tables: Optional[List[str]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Tuple[bool, str]:
        """
        mysqlsh 명령 실행 (테이블별 진행률 모니터링 지원)

        Args:
            js_code: 실행할 JavaScript 코드
            progress_callback: 일반 메시지 콜백
            output_dir: 출력 폴더 (모니터링용)
            schema: 스키마명 (모니터링용)
            tables: 테이블 목록 (모니터링용)
            table_progress_callback: 테이블별 진행률 콜백 (current, total, table_name)
        """
        try:
            # mysqlsh 명령 구성
            cmd = [
                "mysqlsh",
                "--uri", self.config.get_uri(),
                "--js",
                "-e", js_code
            ]

            if progress_callback:
                progress_callback(f"mysqlsh 실행: {self.config.get_masked_uri()}")

            # 테이블별 진행률 모니터링 설정
            stop_monitor = threading.Event()
            monitor_thread = None

            if output_dir and schema and tables and table_progress_callback:
                monitor_thread = threading.Thread(
                    target=self._monitor_export_progress,
                    args=(output_dir, schema, tables, table_progress_callback, stop_monitor),
                    daemon=True
                )
                monitor_thread.start()

            # Popen으로 실행 (모니터링과 병행)
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # 완료 대기
            stdout, stderr = process.communicate(timeout=3600)

            # 모니터링 종료
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=2)

            if process.returncode == 0:
                return True, "성공"
            else:
                error_msg = stderr or stdout or "알 수 없는 오류"
                return False, error_msg

        except subprocess.TimeoutExpired:
            stop_monitor.set()
            if process:
                process.kill()
            return False, "작업 시간 초과 (1시간)"
        except Exception as e:
            stop_monitor.set()
            return False, str(e)

    def _monitor_export_progress(
        self,
        output_dir: str,
        schema: str,
        tables: List[str],
        callback: Callable[[int, int, str], None],
        stop_event: threading.Event
    ):
        """
        출력 폴더를 모니터링하여 테이블별 Export 진행률 추적

        mysqlsh는 데이터 export 시 {schema}@{table}@@{chunk}.zst 파일 생성
        (.json/.sql은 초반에 일괄 생성되므로 완료 판정에 부적합)
        """
        total = len(tables)
        completed_tables = set()
        tables_set = set(tables)  # 빠른 조회용

        # 폴더 삭제 완료 대기 (최대 5초)
        wait_count = 0
        while wait_count < 50:
            if not os.path.exists(output_dir):
                break
            time.sleep(0.1)
            wait_count += 1

        # 모니터링 시작 시 기존 데이터 파일에서 테이블명 추출 (baseline)
        baseline_tables = set()
        if os.path.exists(output_dir):
            existing = glob_module.glob(os.path.join(output_dir, f"{schema}@*@@*.zst"))
            for f in existing:
                filename = os.path.basename(f)
                if "@@" in filename:
                    table_part = filename.split("@@")[0]
                    table_name = table_part[len(f"{schema}@"):]
                    baseline_tables.add(table_name)

        # mysqlsh가 폴더를 생성할 때까지 대기 (최대 10초)
        folder_wait = 0
        while not stop_event.is_set() and folder_wait < 100:
            if os.path.exists(output_dir):
                break
            time.sleep(0.1)
            folder_wait += 1

        while not stop_event.is_set():
            try:
                # 데이터 파일 (.zst) 확인
                pattern = os.path.join(output_dir, f"{schema}@*@@*.zst")
                data_files = glob_module.glob(pattern)

                for data_file in data_files:
                    filename = os.path.basename(data_file)

                    # {schema}@{table}@@{chunk}.zst 형식에서 테이블명 추출
                    if "@@" in filename:
                        table_part = filename.split("@@")[0]
                        table_name = table_part[len(f"{schema}@"):]

                        # baseline 테이블은 무시
                        if table_name in baseline_tables:
                            continue

                        if table_name in tables_set and table_name not in completed_tables:
                            completed_tables.add(table_name)
                            callback(len(completed_tables), total, table_name)

                # 모든 테이블 완료 확인
                if len(completed_tables) >= total:
                    break

                time.sleep(0.15)

            except Exception:
                pass

        # 최종 정리: 빈 테이블 처리 (데이터 파일 없이 .json만 있는 경우)
        if len(completed_tables) < total:
            time.sleep(0.3)
            try:
                # .json 파일로 모든 테이블 확인
                json_pattern = os.path.join(output_dir, f"{schema}@*.json")
                json_files = glob_module.glob(json_pattern)

                for json_file in json_files:
                    filename = os.path.basename(json_file)
                    if filename.startswith(f"{schema}@") and filename.endswith(".json"):
                        table_name = filename[len(f"{schema}@"):-5]

                        if table_name in baseline_tables:
                            continue

                        if table_name in tables_set and table_name not in completed_tables:
                            # 빈 테이블로 간주하여 완료 처리
                            completed_tables.add(table_name)
                            callback(len(completed_tables), total, table_name)
            except Exception:
                pass

    def _write_metadata(
        self,
        output_dir: str,
        schema: str,
        export_type: str,
        tables: List[str],
        added_tables: List[str] = None
    ):
        """Export 메타데이터 파일 생성"""
        metadata = {
            "export_time": datetime.now().isoformat(),
            "schema": schema,
            "type": export_type,
            "tables": tables,
            "added_fk_tables": added_tables or [],
            "source": f"{self.config.host}:{self.config.port}"
        }

        filepath = os.path.join(output_dir, "_export_metadata.json")
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)


class MySQLShellImporter:
    """MySQL Shell 기반 Import"""

    def __init__(self, config: MySQLShellConfig):
        self.config = config

    def import_dump(
        self,
        input_dir: str,
        target_schema: Optional[str] = None,
        threads: int = 4,
        drop_existing_tables: bool = True,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Dump 파일 Import (DDL + Data 완전 교체)

        Args:
            input_dir: Dump 디렉토리 경로
            target_schema: 대상 스키마 (None이면 원본 스키마 사용)
            threads: 병렬 스레드 수
            drop_existing_tables: 기존 테이블 삭제 후 재생성 여부 (기본: True)
            progress_callback: 진행 상황 콜백

        Returns:
            (성공여부, 메시지)
        """
        try:
            # 메타데이터 확인
            metadata_path = os.path.join(input_dir, "_export_metadata.json")
            metadata = None
            source_schema = None
            tables_to_import = []

            if os.path.exists(metadata_path):
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                source_schema = metadata.get('schema')
                tables_to_import = metadata.get('tables', [])
                if progress_callback:
                    progress_callback(f"메타데이터 확인: {source_schema} ({metadata.get('type')}) - {len(tables_to_import)}개 테이블")

            # 대상 스키마 결정
            final_target_schema = target_schema or source_schema
            if not final_target_schema:
                return False, "대상 스키마를 지정할 수 없습니다."

            # 기존 테이블 삭제 (옵션)
            if drop_existing_tables and tables_to_import:
                if progress_callback:
                    progress_callback(f"기존 테이블 삭제 중... ({len(tables_to_import)}개)")

                drop_success, drop_msg = self._drop_existing_tables(
                    final_target_schema,
                    tables_to_import,
                    progress_callback
                )

                if not drop_success:
                    return False, f"기존 테이블 삭제 실패: {drop_msg}"

            if progress_callback:
                progress_callback(f"DDL + Data Import 시작 (스레드: {threads})")

            # loadDump 옵션 구성 (DDL + Data 모두 로드)
            options = [
                f"threads: {threads}",
                "loadDdl: true",  # DDL(테이블 구조) 로드
                "loadData: true",  # Data 로드
                "ignoreExistingObjects: false",  # 기존 객체 있으면 에러 (이미 DROP했으므로)
                "resetProgress: true",
                "showProgress: true"
            ]

            if target_schema:
                options.append(f'schema: "{target_schema}"')

            options_str = ", ".join(options)

            # mysqlsh 명령 구성 (local_infile 활성화 필요)
            js_code = f"""
session.runSql("SET GLOBAL local_infile = ON");
util.loadDump("{input_dir.replace('\\', '/')}", {{
    {options_str}
}});
"""

            # Import 실행 (실시간 진행률 파싱)
            success, msg = self._run_mysqlsh_import(
                js_code,
                progress_callback,
                tables_to_import,
                table_progress_callback
            )

            if success and progress_callback:
                progress_callback(f"✅ Import 완료 (DDL + Data)")

            return success, msg

        except Exception as e:
            return False, f"Import 오류: {str(e)}"

    def _drop_existing_tables(
        self,
        schema: str,
        tables: List[str],
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Import 전에 기존 테이블 삭제 (FK 의존성 순서 고려)

        Args:
            schema: 스키마명
            tables: 삭제할 테이블 목록
            progress_callback: 진행 콜백

        Returns:
            (성공여부, 메시지)
        """
        try:
            # JSON 배열로 테이블 목록 생성
            tables_json = json.dumps(tables)

            # JavaScript로 FK 체크 비활성화 후 각 테이블 삭제
            js_code = f"""
session.runSql("SET FOREIGN_KEY_CHECKS = 0");
var tables = {tables_json};
for (var i = 0; i < tables.length; i++) {{
    session.runSql("DROP TABLE IF EXISTS `{schema}`.`" + tables[i] + "`");
}}
session.runSql("SET FOREIGN_KEY_CHECKS = 1");
"""

            success, msg = self._run_mysqlsh(js_code, progress_callback)

            if success and progress_callback:
                progress_callback(f"✅ {len(tables)}개 테이블 삭제 완료")

            return success, msg

        except Exception as e:
            return False, f"테이블 삭제 오류: {str(e)}"

    def _run_mysqlsh(
        self,
        js_code: str,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """mysqlsh 명령 실행"""
        try:
            cmd = [
                "mysqlsh",
                "--uri", self.config.get_uri(),
                "--js",
                "-e", js_code
            ]

            if progress_callback:
                progress_callback(f"mysqlsh 실행 중...")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600
            )

            if result.returncode == 0:
                return True, "성공"
            else:
                error_msg = result.stderr or result.stdout or "알 수 없는 오류"
                return False, error_msg

        except subprocess.TimeoutExpired:
            return False, "작업 시간 초과 (1시간)"
        except Exception as e:
            return False, str(e)

    def _run_mysqlsh_import(
        self,
        js_code: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        tables: Optional[List[str]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Import용 mysqlsh 명령 실행 (실시간 출력 파싱)

        Args:
            js_code: 실행할 JavaScript 코드
            progress_callback: 일반 메시지 콜백
            tables: Import할 테이블 목록 (진행률 표시용)
            table_progress_callback: 테이블별 진행률 콜백
        """
        try:
            cmd = [
                "mysqlsh",
                "--uri", self.config.get_uri(),
                "--js",
                "-e", js_code
            ]

            if progress_callback:
                progress_callback(f"mysqlsh Import 실행 중...")

            # Popen으로 실행하여 실시간 출력 읽기
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # 라인 버퍼링
                universal_newlines=True
            )

            # 진행률 추적
            completed_tables = set()
            total_tables = len(tables) if tables else 0

            # stdout과 stderr를 실시간으로 읽기
            import re
            stdout_lines = []
            stderr_lines = []

            while True:
                # stdout에서 한 줄 읽기
                line = process.stdout.readline()
                if not line:
                    break

                stdout_lines.append(line)

                # 테이블 로딩 패턴 감지: "X thds loading | 100% (123.45 MB / 123.45 MB), 0 B/s, 2 / 6 tables done"
                # 또는 "X thds loading - YY% (X MB / Y MB), Z MB/s, M / N tables done"
                table_done_match = re.search(r'(\d+)\s*/\s*(\d+)\s*tables?\s*done', line, re.IGNORECASE)
                if table_done_match and tables and table_progress_callback:
                    current = int(table_done_match.group(1))
                    total = int(table_done_match.group(2))

                    # 테이블명은 알 수 없으므로 순서대로 가정
                    if current <= len(tables):
                        table_name = tables[current - 1] if current > 0 else "..."
                        table_progress_callback(current, total, table_name)

            # stderr 읽기
            stderr_output = process.stderr.read()
            if stderr_output:
                stderr_lines.append(stderr_output)

            # 프로세스 종료 대기
            process.wait(timeout=3600)

            if process.returncode == 0:
                # 최종 진행률 100% 표시
                if tables and table_progress_callback and total_tables > 0:
                    table_progress_callback(total_tables, total_tables, tables[-1])
                return True, "성공"
            else:
                error_msg = stderr_output or "알 수 없는 오류"
                return False, error_msg

        except subprocess.TimeoutExpired:
            if process:
                process.kill()
            return False, "작업 시간 초과 (1시간)"
        except Exception as e:
            return False, str(e)


# 편의 함수
def check_mysqlsh() -> Tuple[bool, str]:
    """mysqlsh 설치 확인 (간편 함수)"""
    installed, msg, _ = MySQLShellChecker.check_installation()
    return installed, msg


def export_schema(
    host: str,
    port: int,
    user: str,
    password: str,
    schema: str,
    output_dir: str,
    threads: int = 4,
    progress_callback: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str]:
    """
    전체 스키마 Export (간편 함수)
    """
    config = MySQLShellConfig(host, port, user, password)
    exporter = MySQLShellExporter(config)
    return exporter.export_full_schema(schema, output_dir, threads, progress_callback=progress_callback)


def export_tables(
    host: str,
    port: int,
    user: str,
    password: str,
    schema: str,
    tables: List[str],
    output_dir: str,
    threads: int = 4,
    include_fk_parents: bool = True,
    progress_callback: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str, List[str]]:
    """
    선택된 테이블 Export (간편 함수)
    """
    config = MySQLShellConfig(host, port, user, password)
    exporter = MySQLShellExporter(config)
    return exporter.export_tables(
        schema, tables, output_dir, threads,
        include_fk_parents=include_fk_parents,
        progress_callback=progress_callback
    )


def import_dump(
    host: str,
    port: int,
    user: str,
    password: str,
    input_dir: str,
    target_schema: Optional[str] = None,
    threads: int = 4,
    drop_existing_tables: bool = True,
    progress_callback: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str]:
    """
    Dump Import (간편 함수)

    Args:
        drop_existing_tables: 기존 테이블 삭제 후 재생성 (기본: True)
    """
    config = MySQLShellConfig(host, port, user, password)
    importer = MySQLShellImporter(config)
    return importer.import_dump(
        input_dir,
        target_schema,
        threads,
        drop_existing_tables=drop_existing_tables,
        progress_callback=progress_callback
    )
