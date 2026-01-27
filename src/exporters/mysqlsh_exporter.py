"""
MySQL Shell 기반 병렬 Export/Import
- 멀티스레드 병렬 처리
- FK 의존성 자동 분석 및 처리
- 전체 스키마 / 일부 테이블 지원
"""
import os
import re
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
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None
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
            detail_callback: 상세 진행 정보 콜백 (percent, mb_done, mb_total, speed)
            table_status_callback: 테이블별 상태 콜백 (table_name, status, message)
            raw_output_callback: mysqlsh 실시간 출력 콜백

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
            output_dir_escaped = output_dir.replace('\\', '/')
            js_code = f"""
util.dumpSchemas(["{schema}"], "{output_dir_escaped}", {{
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
                table_progress_callback=table_progress_callback,
                detail_callback=detail_callback,
                table_status_callback=table_status_callback,
                raw_output_callback=raw_output_callback
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
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None
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
            detail_callback: 상세 진행 정보 콜백 (percent, mb_done, mb_total, speed)
            table_status_callback: 테이블별 상태 콜백 (table_name, status, message)
            raw_output_callback: mysqlsh 실시간 출력 콜백

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
            output_dir_escaped = output_dir.replace('\\', '/')

            # mysqlsh 명령 구성
            js_code = f"""
util.dumpTables("{schema}", {tables_json}, "{output_dir_escaped}", {{
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
                table_progress_callback=table_progress_callback,
                detail_callback=detail_callback,
                table_status_callback=table_status_callback,
                raw_output_callback=raw_output_callback
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
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        mysqlsh 명령 실행 (테이블별 진행률 모니터링 지원 + 실시간 stdout 파싱)

        Args:
            js_code: 실행할 JavaScript 코드
            progress_callback: 일반 메시지 콜백
            output_dir: 출력 폴더 (모니터링용)
            schema: 스키마명 (모니터링용)
            tables: 테이블 목록 (모니터링용)
            table_progress_callback: 테이블별 진행률 콜백 (current, total, table_name)
            detail_callback: 상세 진행 정보 콜백 (percent, mb_done, mb_total, speed)
            table_status_callback: 테이블별 상태 콜백 (table_name, status, message)
            raw_output_callback: mysqlsh 실시간 출력 콜백
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
            process = None

            if output_dir and schema and tables and table_progress_callback:
                monitor_thread = threading.Thread(
                    target=self._monitor_export_progress,
                    args=(output_dir, schema, tables, table_progress_callback, table_status_callback, stop_monitor),
                    daemon=True
                )
                monitor_thread.start()

            # Popen으로 실행 (실시간 출력 읽기 + 모니터링 병행)
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                universal_newlines=True
            )

            # 실시간 stdout 파싱 (Export 진행률)
            completed_tables_set = set()
            last_percent = 0

            while True:
                line = process.stdout.readline()

                if not line and process.poll() is not None:
                    break

                if line:
                    stripped_line = line.strip()
                    timestamp = datetime.now().strftime("%H:%M:%S")

                    # 콘솔 디버깅 출력
                    print(f"[mysqlsh export] {stripped_line}")

                    # 실시간 출력 콜백
                    if raw_output_callback:
                        raw_output_callback(f"[{timestamp}] {stripped_line}")

                    # --- 패턴 1: 상세 진행 정보 파싱 ---
                    # Export 예: "4 thds dumping - 27% (2.24M rows / ~8.23M rows), 25.39K rows/s, 6.60 MB/s uncompressed"
                    # 퍼센트만 파싱 (rows 정보는 데이터 크기로 변환 불가)
                    percent_match = re.search(r'dumping.*?(\d+)%', stripped_line)
                    if percent_match and detail_callback:
                        percent = int(percent_match.group(1))

                        # 속도 파싱 (MB/s, KB/s 등 - "uncompressed" 앞의 속도)
                        speed_match = re.search(r'([0-9.]+)\s*([KMGT]?B)/s\s+uncompressed', stripped_line)
                        speed_str = "0 B/s"
                        if speed_match:
                            speed_str = f"{speed_match.group(1)} {speed_match.group(2)}/s"

                        # 진행률이 증가한 경우에만 콜백 호출 (중복 방지)
                        if percent > last_percent:
                            detail_callback({
                                'percent': percent,
                                'mb_done': 0,  # Export는 rows만 표시하므로 0으로
                                'mb_total': 0,
                                'speed': speed_str
                            })
                            last_percent = percent

                    # --- 패턴 2: 테이블 완료 감지 ---
                    # 예: "Writing DDL for table `schema`.`table_name`"
                    # 예: "Writing data for table `schema`.`table_name`"
                    table_match = re.search(r"`([^`]+)`\.`([^`]+)`", stripped_line)
                    if table_match and tables and table_status_callback:
                        table_name = table_match.group(2)

                        if table_name in tables:
                            # "Writing" 패턴인 경우 loading 상태로
                            if "Writing" in stripped_line or "dumping" in stripped_line.lower():
                                if table_name not in completed_tables_set:
                                    table_status_callback(table_name, 'loading', '')
                            # "done" 패턴인 경우 완료 상태로
                            elif "done" in stripped_line.lower():
                                if table_name not in completed_tables_set:
                                    completed_tables_set.add(table_name)
                                    table_status_callback(table_name, 'done', '')

            # 완료 대기
            rc = process.poll()
            if rc is None:
                process.wait(timeout=3600)
                rc = process.returncode

            stdout = ""
            stderr = ""

            # 모니터링 종료
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=2)

            if rc == 0:
                # 최종 진행률 100% 표시
                if detail_callback:
                    detail_callback({
                        'percent': 100,
                        'mb_done': 0,
                        'mb_total': 0,
                        'speed': '0 B/s'
                    })

                # 모든 테이블 완료 상태로 업데이트
                if tables and table_status_callback:
                    for table in tables:
                        if table not in completed_tables_set:
                            table_status_callback(table, 'done', '')

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
        table_status_callback: Optional[Callable[[str, str, str], None]],
        stop_event: threading.Event
    ):
        """
        출력 폴더를 모니터링하여 테이블별 Export 진행률 추적

        mysqlsh는 데이터 export 시 {schema}@{table}@@{chunk}.zst 파일 생성
        (.json/.sql은 초반에 일괄 생성되므로 완료 판정에 부적합)

        Args:
            output_dir: 출력 디렉토리
            schema: 스키마명
            tables: 테이블 목록
            callback: 테이블별 진행률 콜백 (current, total, table_name)
            table_status_callback: 테이블별 상태 콜백 (table_name, status, message)
            stop_event: 중지 이벤트
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

        # 모든 테이블을 pending 상태로 초기화
        if table_status_callback:
            for table in tables:
                table_status_callback(table, 'pending', '')

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
                            # 테이블 완료 상태 업데이트
                            if table_status_callback:
                                table_status_callback(table_name, 'done', '')

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
                            # 테이블 완료 상태 업데이트
                            if table_status_callback:
                                table_status_callback(table_name, 'done', '')
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

    def _analyze_dump_metadata(self, dump_dir: str) -> Optional[Dict]:
        """
        Dump 메타데이터 분석 - 테이블별 Chunk 정보 추출

        Args:
            dump_dir: Dump 디렉토리 경로

        Returns:
            {
                'chunk_counts': {'table_name': chunk_count, ...},
                'table_sizes': {'table_name': bytes, ...},
                'total_bytes': int,
                'schema': str
            }
            또는 None (메타데이터 파일이 없는 경우)
        """
        try:
            done_json_path = os.path.join(dump_dir, '@.done.json')

            if not os.path.exists(done_json_path):
                return None

            with open(done_json_path, 'r', encoding='utf-8') as f:
                done_data = json.load(f)

            # chunkFileBytes에서 테이블별 chunk 수 계산
            chunk_counts = {}  # {'df_subs': 81, 'df_call_logs': 8, ...}
            chunk_file_bytes = done_data.get('chunkFileBytes', {})

            for chunk_file in chunk_file_bytes.keys():
                # "dataflare@df_subs@15.tsv.zst" 또는 "dataflare@df_subs@@0.tsv.zst" 형식
                if '@' in chunk_file:
                    parts = chunk_file.split('@')
                    if len(parts) >= 3:
                        # schema@table@chunk 또는 schema@table@@chunk 형식
                        table_name = parts[1]
                        chunk_counts[table_name] = chunk_counts.get(table_name, 0) + 1

            # tableDataBytes에서 테이블별 크기 추출
            table_data_bytes = done_data.get('tableDataBytes', {})
            table_sizes = {}
            schema = None

            # tableDataBytes 구조: {'schema_name': {'table_name': bytes, ...}}
            for schema_name, tables in table_data_bytes.items():
                schema = schema_name  # 스키마명 저장
                for table_name, size_bytes in tables.items():
                    table_sizes[table_name] = size_bytes

            total_bytes = done_data.get('dataBytes', 0)

            return {
                'chunk_counts': chunk_counts,
                'table_sizes': table_sizes,
                'total_bytes': total_bytes,
                'schema': schema or ''
            }

        except Exception as e:
            # 메타데이터 분석 실패 시 None 반환 (기존 동작 유지)
            return None

    def import_dump(
        self,
        input_dir: str,
        target_schema: Optional[str] = None,
        threads: int = 4,
        import_mode: str = "replace",
        timezone_sql: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None,
        retry_tables: Optional[List[str]] = None,
        metadata_callback: Optional[Callable[[dict], None]] = None
    ) -> Tuple[bool, str, dict]:
        """
        Dump 파일 Import (3가지 모드 지원)

        Args:
            input_dir: Dump 디렉토리 경로
            target_schema: 대상 스키마 (None이면 원본 스키마 사용)
            threads: 병렬 스레드 수
            import_mode: Import 모드
                - "merge": 병합 (기존 데이터 유지)
                - "replace": 전체 교체 (모든 객체 재생성, resetProgress=true)
                - "recreate": 완전 재생성 (스키마 DROP 후 재생성)
            progress_callback: 진행 상황 콜백
            detail_callback: 상세 진행 정보 콜백 (percent, mb_done, mb_total, rows_sec)
            table_status_callback: 테이블별 상태 콜백 (table_name, status, message)
            raw_output_callback: mysqlsh 실시간 출력 콜백
            retry_tables: 재시도할 테이블 목록 (선택적)
            metadata_callback: 메타데이터 분석 결과 콜백 (chunk_counts, table_sizes 등)

        Returns:
            (성공여부, 메시지, 테이블별 결과 dict)
        """
        # 테이블별 Import 결과 추적
        import_results: dict = {}
        try:
            # Dump 메타데이터 분석 (@.done.json)
            dump_metadata = self._analyze_dump_metadata(input_dir)
            if dump_metadata and progress_callback:
                total_size_gb = dump_metadata['total_bytes'] / (1024 * 1024 * 1024)
                large_tables = [
                    (name, size) for name, size in dump_metadata['table_sizes'].items()
                    if size > 100_000_000  # 100MB 이상
                ]
                large_tables.sort(key=lambda x: -x[1])

                progress_callback(f"📊 Dump 메타데이터 분석 완료")
                progress_callback(f"  └─ 전체 데이터 크기: {total_size_gb:.2f} GB")

                if large_tables:
                    progress_callback(f"  └─ 대용량 테이블 ({len(large_tables)}개):")
                    for name, size in large_tables[:5]:  # 상위 5개만 표시
                        size_mb = size / (1024 * 1024)
                        chunk_count = dump_metadata['chunk_counts'].get(name, 1)
                        progress_callback(f"     • {name}: {size_mb:.1f} MB ({chunk_count} chunks)")

            # 메타데이터 콜백 호출 (UI로 전달)
            if dump_metadata and metadata_callback:
                metadata_callback(dump_metadata)

            # Export 메타데이터 확인 (_export_metadata.json)
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

            # 재시도 모드인 경우 테이블 목록 필터링
            if retry_tables:
                tables_to_import = [t for t in tables_to_import if t in retry_tables]
                if progress_callback:
                    progress_callback(f"🔄 재시도 모드: {len(tables_to_import)}개 테이블만 Import")

            # 테이블 상태 초기화 (pending 상태로)
            for table in tables_to_import:
                import_results[table] = {'status': 'pending', 'message': ''}
                if table_status_callback:
                    table_status_callback(table, 'pending', '')

            # 타임존 패치 (Asia/Seoul -> +09:00)
            if progress_callback:
                progress_callback("타임존 보정 중... (Asia/Seoul -> +09:00)")

            patched_count = self._patch_timezone_in_dump(input_dir, progress_callback)
            if patched_count > 0 and progress_callback:
                progress_callback(f"✅ {patched_count}개 SQL 파일 타임존 보정 완료")

            # 대상 스키마 결정
            final_target_schema = target_schema or source_schema
            if not final_target_schema:
                return False, "대상 스키마를 지정할 수 없습니다.", import_results

            # Import 모드별 처리
            if import_mode == "recreate":
                # 완전 재생성: 스키마 DROP 후 재생성
                if progress_callback:
                    progress_callback(f"⚠️ 스키마 '{final_target_schema}' 완전 재생성 중...")

                drop_schema_success, drop_schema_msg = self._drop_and_recreate_schema(
                    final_target_schema,
                    progress_callback
                )

                if not drop_schema_success:
                    return False, f"스키마 재생성 실패: {drop_schema_msg}", import_results

            elif import_mode == "replace":
                # 전체 교체: 모든 객체 (테이블, 뷰, 프로시저, 이벤트) 삭제 후 재생성
                if progress_callback:
                    progress_callback(f"🔄 전체 교체 모드 시작")
                    progress_callback(f"  └─ {len(tables_to_import)}개 테이블, View/Procedure/Event 삭제 예정")

                # 1. 테이블 삭제
                if tables_to_import:
                    drop_success, drop_msg = self._drop_existing_tables(
                        final_target_schema,
                        tables_to_import,
                        progress_callback
                    )
                    if not drop_success:
                        return False, f"테이블 삭제 실패: {drop_msg}", import_results
                
                # 2. View, Procedure, Event 삭제
                drop_objects_success, drop_objects_msg = self._drop_all_objects(
                    final_target_schema,
                    progress_callback
                )
                if not drop_objects_success:
                    return False, f"객체 삭제 실패: {drop_objects_msg}", import_results

            elif import_mode == "merge":
                # 병합: 기존 데이터 유지, 새 것만 추가
                if progress_callback:
                    progress_callback(f"증분 병합 모드: 기존 데이터 유지")

            else:
                return False, f"알 수 없는 Import 모드: {import_mode}", import_results

            if progress_callback:
                progress_callback(f"DDL + Data Import 시작 (스레드: {threads}, 모드: {import_mode})")

            # loadDump 옵션 구성 (모드별)
            options = [
                f"threads: {threads}",
                "loadDdl: true",  # DDL(테이블 구조) 로드
                "loadData: true",  # Data 로드
                "showProgress: true"
            ]

            # 모드별 옵션
            if import_mode == "replace":
                # 전체 교체: resetProgress로 View/Procedure/Event도 재생성
                options.append("resetProgress: true")
                options.append("ignoreExistingObjects: false")
            elif import_mode == "merge":
                # 병합: 기존 객체 무시
                options.append("resetProgress: false")
                options.append("ignoreExistingObjects: true")
            elif import_mode == "recreate":
                # 완전 재생성: 스키마가 비어있으므로 기본 설정
                options.append("resetProgress: true")
                options.append("ignoreExistingObjects: false")

            if target_schema:
                options.append(f'schema: "{target_schema}"')

            options_str = ", ".join(options)
            input_dir_escaped = input_dir.replace('\\', '/')

            # mysqlsh 명령 구성 (local_infile 활성화 필요)
            # Timezone 설정이 있으면 util.loadDump 이전에 실행
            timezone_cmd = f'session.runSql("{timezone_sql}");' if timezone_sql else ""

            js_code = f"""
                session.runSql("SET GLOBAL local_infile = ON");
                {timezone_cmd}
                util.loadDump("{input_dir_escaped}", {{
                    {options_str}
                }});
            """

            print(js_code)

            # Import 실행 (실시간 진행률 파싱)
            success, msg, import_results = self._run_mysqlsh_import(
                js_code,
                progress_callback,
                tables_to_import,
                table_progress_callback,
                detail_callback,
                table_status_callback,
                raw_output_callback,
                import_results
            )

            if success and progress_callback:
                progress_callback(f"✅ Import 완료 (DDL + Data, 모드: {import_mode})")

            return success, msg, import_results

        except Exception as e:
            return False, f"Import 오류: {str(e)}", import_results

    def _drop_and_recreate_schema(
        self,
        schema: str,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        스키마 완전 재생성 (DROP + CREATE)

        Args:
            schema: 스키마명
            progress_callback: 진행 콜백

        Returns:
            (성공여부, 메시지)
        """
        try:
            if progress_callback:
                progress_callback(f"🗑️ 스키마 '{schema}' DROP 중...")

            js_code = f"""
session.runSql("DROP DATABASE IF EXISTS `{schema}`");
session.runSql("CREATE DATABASE `{schema}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci");
"""

            success, msg = self._run_mysqlsh(js_code, progress_callback)

            if success and progress_callback:
                progress_callback(f"  └─ ✅ 스키마 '{schema}' 재생성 완료")

            return success, msg

        except Exception as e:
            return False, f"스키마 재생성 오류: {str(e)}"

    def _drop_all_objects(
        self,
        schema: str,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        스키마의 모든 View, Procedure, Event 삭제

        Args:
            schema: 스키마명
            progress_callback: 진행 콜백

        Returns:
            (성공여부, 메시지)
        """
        try:
            if progress_callback:
                progress_callback(f"🗑️ View/Procedure/Function/Event 삭제 중...")

            # Views, Procedures, Events 조회 및 삭제
            js_code = f"""
// Views 삭제
var views = session.runSql("SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA = '{schema}'").fetchAll();
for (var i = 0; i < views.length; i++) {{
    var viewName = views[i][0];
    session.runSql("DROP VIEW IF EXISTS `{schema}`.`" + viewName + "`");
}}

// Procedures 삭제
var procedures = session.runSql("SELECT ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = '{schema}' AND ROUTINE_TYPE = 'PROCEDURE'").fetchAll();
for (var i = 0; i < procedures.length; i++) {{
    var procName = procedures[i][0];
    session.runSql("DROP PROCEDURE IF EXISTS `{schema}`.`" + procName + "`");
}}

// Functions 삭제
var functions = session.runSql("SELECT ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = '{schema}' AND ROUTINE_TYPE = 'FUNCTION'").fetchAll();
for (var i = 0; i < functions.length; i++) {{
    var funcName = functions[i][0];
    session.runSql("DROP FUNCTION IF EXISTS `{schema}`.`" + funcName + "`");
}}

// Events 삭제
var events = session.runSql("SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA = '{schema}'").fetchAll();
for (var i = 0; i < events.length; i++) {{
    var eventName = events[i][0];
    session.runSql("DROP EVENT IF EXISTS `{schema}`.`" + eventName + "`");
}}
"""

            success, msg = self._run_mysqlsh(js_code, progress_callback)

            if success and progress_callback:
                progress_callback(f"  └─ ✅ View/Procedure/Event 삭제 완료")

            return success, msg

        except Exception as e:
            return False, f"객체 삭제 오류: {str(e)}"

    def _patch_timezone_in_dump(self, input_dir: str, progress_callback: Optional[Callable[[str], None]] = None) -> int:
        """
        Dump 파일 내의 Asia/Seoul 타임존을 +09:00으로 보정
        (타켓 서버에 타임존 정보가 없는 경우 발생하는 오류 방지)

        Args:
            input_dir: Dump 디렉토리 경로
            progress_callback: 진행 콜백

        Returns:
            보정된 파일 개수
        """
        patched_count = 0
        try:
            sql_files = glob_module.glob(os.path.join(input_dir, "*.sql"))

            if progress_callback:
                progress_callback(f"  └─ {len(sql_files)}개 SQL 파일 스캔 중...")

            for file_path in sql_files:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    if "'Asia/Seoul'" in content:
                        new_content = content.replace("'Asia/Seoul'", "'+09:00'")
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                        patched_count += 1
                except Exception:
                    continue

            return patched_count
        except Exception:
            return 0

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
            if progress_callback:
                progress_callback(f"🗑️ 테이블 삭제 시작 ({len(tables)}개)...")

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
                progress_callback(f"  └─ ✅ {len(tables)}개 테이블 삭제 완료")

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
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None,
        import_results: Optional[dict] = None
    ) -> Tuple[bool, str, dict]:
        """
        Import용 mysqlsh 명령 실행 (실시간 출력 파싱)

        Args:
            js_code: 실행할 JavaScript 코드
            progress_callback: 일반 메시지 콜백
            tables: Import할 테이블 목록 (진행률 표시용)
            table_progress_callback: 테이블별 진행률 콜백
            detail_callback: 상세 진행 정보 콜백 (percent, mb_done, mb_total, rows_sec, speed)
            table_status_callback: 테이블별 상태 콜백 (table_name, status, message)
            raw_output_callback: mysqlsh 실시간 출력 콜백
            import_results: 테이블별 결과 dict (수정됨)

        Returns:
            (성공여부, 메시지, 테이블별 결과 dict)
        """
        if import_results is None:
            import_results = {}

        process = None
        last_completed_count = 0
        error_messages = []
        current_loading_table = None

        try:
            cmd = [
                "mysqlsh",
                "--uri", self.config.get_uri(),
                "--js",
                "-e", js_code
            ]

            if progress_callback:
                progress_callback(f"mysqlsh 실행 중...")

            # Popen으로 실행하여 실시간 출력 읽기
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                universal_newlines=True
            )

            total_tables = len(tables) if tables else 0

            while True:
                line = process.stdout.readline()

                if not line and process.poll() is not None:
                    break

                if line:
                    stripped_line = line.strip()
                    timestamp = datetime.now().strftime("%H:%M:%S")

                    # 콘솔 디버깅 출력
                    print(f"[mysqlsh] {stripped_line}")

                    # 실시간 출력 콜백 (UI에 전달)
                    if raw_output_callback:
                        raw_output_callback(f"[{timestamp}] {stripped_line}")

                    # --- 패턴 1: 상세 진행 정보 파싱 ---
                    # 예: "1 thds loading | 92% (88.95 MB / 96.69 MB), 1.5 MB/s (285.00 rows/s), 5 / 6 tables done"
                    detail_match = re.search(
                        r'(\d+)%\s*\(([0-9.]+)\s*([KMGT]?B)\s*/\s*([0-9.]+)\s*([KMGT]?B)\)',
                        stripped_line
                    )
                    if detail_match and detail_callback:
                        percent = int(detail_match.group(1))
                        mb_done = float(detail_match.group(2))
                        unit_done = detail_match.group(3)
                        mb_total = float(detail_match.group(4))
                        unit_total = detail_match.group(5)

                        # 단위 변환 (MB로 통일)
                        def to_mb(value, unit):
                            if unit == 'KB':
                                return value / 1024
                            elif unit == 'GB':
                                return value * 1024
                            elif unit == 'TB':
                                return value * 1024 * 1024
                            return value  # B 또는 MB

                        mb_done = to_mb(mb_done, unit_done)
                        mb_total = to_mb(mb_total, unit_total)

                        # rows/s 파싱
                        rows_match = re.search(r'([0-9.]+)\s*[Kk]?\s*rows?/s', stripped_line)
                        rows_sec = 0
                        if rows_match:
                            rows_sec = float(rows_match.group(1))
                            if 'K' in stripped_line[rows_match.start():rows_match.end()].upper():
                                rows_sec *= 1000

                        # 속도 파싱 (MB/s, KB/s 등)
                        speed_match = re.search(r'([0-9.]+)\s*([KMGT]?B)/s', stripped_line)
                        speed_str = "0 B/s"
                        if speed_match:
                            speed_str = f"{speed_match.group(1)} {speed_match.group(2)}/s"

                        detail_callback({
                            'percent': percent,
                            'mb_done': round(mb_done, 2),
                            'mb_total': round(mb_total, 2),
                            'rows_sec': int(rows_sec),
                            'speed': speed_str
                        })

                    # --- 패턴 2: 테이블 완료 수 파싱 ---
                    # 예: "5 / 6 tables done"
                    table_done_match = re.search(r'(\d+)\s*/\s*(\d+)\s*tables?\s*done', stripped_line, re.IGNORECASE)
                    if table_done_match and tables:
                        current_count = int(table_done_match.group(1))
                        total_in_log = int(table_done_match.group(2))

                        # 새로 완료된 테이블이 있는지 확인
                        if current_count > last_completed_count:
                            # 새로 완료된 테이블들 상태 업데이트
                            for i in range(last_completed_count, min(current_count, len(tables))):
                                table_name = tables[i]
                                import_results[table_name] = {'status': 'done', 'message': ''}
                                if table_status_callback:
                                    table_status_callback(table_name, 'done', '')

                            last_completed_count = current_count

                        # 현재 로딩 중인 테이블 표시
                        if current_count < len(tables):
                            loading_table = tables[current_count]
                            if loading_table != current_loading_table:
                                current_loading_table = loading_table
                                import_results[loading_table] = {'status': 'loading', 'message': ''}
                                if table_status_callback:
                                    table_status_callback(loading_table, 'loading', '')

                        if table_progress_callback:
                            table_name = tables[current_count - 1] if current_count > 0 else "..."
                            table_progress_callback(current_count, total_in_log, table_name)

                    # --- 패턴 3: 테이블 로딩 시작 감지 ---
                    # 예: "Loading DDL and Data from ... for table `schema`.`table_name`"
                    loading_match = re.search(r"Loading.*`(\w+)`\.`(\w+)`", stripped_line)
                    if loading_match and tables:
                        table_name = loading_match.group(2)
                        if table_name in import_results:
                            import_results[table_name] = {'status': 'loading', 'message': ''}
                            if table_status_callback:
                                table_status_callback(table_name, 'loading', '')

                    # --- 패턴 4: 에러 감지 ---
                    # 예: "ERROR: ...", "[ERROR] ...", "Error: ..."
                    error_match = re.search(r'(?:ERROR|Error|\[ERROR\])[:\s]+(.+)', stripped_line, re.IGNORECASE)
                    if error_match:
                        error_msg = error_match.group(1).strip()
                        error_messages.append(error_msg)

                        # 테이블 관련 에러인지 확인
                        table_error_match = re.search(r"`(\w+)`\.`(\w+)`", error_msg)
                        if table_error_match:
                            error_table = table_error_match.group(2)
                            if error_table in import_results:
                                import_results[error_table] = {'status': 'error', 'message': error_msg}
                                if table_status_callback:
                                    table_status_callback(error_table, 'error', error_msg)

                        if progress_callback:
                            progress_callback(f"❌ 에러: {error_msg}")

                    # --- 패턴 5: Deadlock 감지 ---
                    if 'deadlock' in stripped_line.lower():
                        error_messages.append(f"Deadlock detected: {stripped_line}")
                        if progress_callback:
                            progress_callback(f"⚠️ Deadlock 감지: {stripped_line}")

                    # --- 패턴 6: Warning 감지 ---
                    warning_match = re.search(r'(?:WARNING|Warning|\[WARNING\])[:\s]+(.+)', stripped_line, re.IGNORECASE)
                    if warning_match:
                        if progress_callback:
                            progress_callback(f"⚠️ 경고: {warning_match.group(1).strip()}")

            # 프로세스 종료 대기
            rc = process.poll()
            if rc is None:
                process.wait(timeout=3600)
                rc = process.returncode

            if rc == 0:
                # 최종 진행률 100% 표시
                if tables and table_progress_callback and total_tables > 0:
                    table_progress_callback(total_tables, total_tables, tables[-1])

                # 모든 테이블 완료 상태로 업데이트
                for table in tables:
                    if import_results.get(table, {}).get('status') != 'error':
                        import_results[table] = {'status': 'done', 'message': ''}
                        if table_status_callback:
                            table_status_callback(table, 'done', '')

                return True, "성공", import_results
            else:
                # 실패 시 pending 상태인 테이블들을 error로 변경
                error_summary = "; ".join(error_messages[:3]) if error_messages else "알 수 없는 오류"
                for table in tables:
                    if import_results.get(table, {}).get('status') in ('pending', 'loading'):
                        import_results[table] = {'status': 'error', 'message': error_summary}
                        if table_status_callback:
                            table_status_callback(table, 'error', error_summary)

                return False, f"mysqlsh 실행 실패: {error_summary}", import_results

        except subprocess.TimeoutExpired:
            if process:
                process.kill()
            # 타임아웃 시 pending/loading 테이블들을 error로 변경
            for table in (tables or []):
                if import_results.get(table, {}).get('status') in ('pending', 'loading'):
                    import_results[table] = {'status': 'error', 'message': '작업 시간 초과'}
                    if table_status_callback:
                        table_status_callback(table, 'error', '작업 시간 초과')
            return False, "작업 시간 초과 (1시간)", import_results
        except Exception as e:
            # 예외 발생 시 pending/loading 테이블들을 error로 변경
            for table in (tables or []):
                if import_results.get(table, {}).get('status') in ('pending', 'loading'):
                    import_results[table] = {'status': 'error', 'message': str(e)}
                    if table_status_callback:
                        table_status_callback(table, 'error', str(e))
            return False, str(e), import_results


class TableProgressTracker:
    """테이블별 Import 진행상황 추적"""

    def __init__(self, metadata: Optional[Dict]):
        """
        Args:
            metadata: _analyze_dump_metadata()의 반환값
        """
        if metadata:
            self.chunk_counts = metadata.get('chunk_counts', {})
            self.table_sizes = metadata.get('table_sizes', {})
            self.total_bytes = metadata.get('total_bytes', 0)
        else:
            self.chunk_counts = {}
            self.table_sizes = {}
            self.total_bytes = 0

        self.completed_tables: Set[str] = set()

    def estimate_loading_tables(
        self,
        loaded_bytes: int,
        completed_tables: List[str]
    ) -> List[Tuple[str, int, int]]:
        """
        현재 로딩 중인 테이블 추정

        Args:
            loaded_bytes: 현재까지 로딩된 바이트 수
            completed_tables: 완료된 테이블 목록

        Returns:
            [(table_name, size_bytes, chunk_count), ...] (상위 4개, 크기 큰 순)
        """
        # 완료된 테이블들의 bytes 합계
        self.completed_tables = set(completed_tables)
        completed_bytes = sum(
            self.table_sizes.get(t, 0) for t in self.completed_tables
        )

        # 남은 bytes
        remaining_bytes = loaded_bytes - completed_bytes

        # 대용량 테이블 중 미완료된 테이블 찾기 (10MB 이상)
        loading_candidates = [
            (
                table,
                self.table_sizes.get(table, 0),
                self.chunk_counts.get(table, 1)
            )
            for table in self.table_sizes.keys()
            if table not in self.completed_tables and self.table_sizes.get(table, 0) > 10_000_000
        ]

        # 크기 큰 순으로 정렬하여 상위 4개 반환
        loading_candidates.sort(key=lambda x: -x[1])
        return loading_candidates[:4]

    def get_table_info(self, table_name: str) -> Tuple[int, int]:
        """
        테이블 정보 조회

        Returns:
            (size_bytes, chunk_count)
        """
        return (
            self.table_sizes.get(table_name, 0),
            self.chunk_counts.get(table_name, 1)
        )

    def format_size(self, size_bytes: int) -> str:
        """바이트를 읽기 쉬운 형식으로 변환"""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


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
    import_mode: str = "replace",
    progress_callback: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str, dict]:
    """
    Dump Import (간편 함수)

    Args:
        import_mode: Import 모드
            - "merge": 병합 (기존 데이터 유지)
            - "replace": 전체 교체 (모든 객체 재생성, resetProgress=true)
            - "recreate": 완전 재생성 (스키마 DROP 후 재생성)
    """
    config = MySQLShellConfig(host, port, user, password)
    importer = MySQLShellImporter(config)
    return importer.import_dump(
        input_dir,
        target_schema,
        threads,
        import_mode=import_mode,
        progress_callback=progress_callback
    )
