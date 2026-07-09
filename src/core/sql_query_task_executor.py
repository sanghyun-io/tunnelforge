"""
스케줄 SQL 쿼리 작업 실행기
- 멀티 쿼리 파싱 및 순차 실행 (SELECT → CSV/JSON, DML → commit)
- 결과 파일 저장 및 오래된 결과 정리 (보관 정책 적용)
"""
import csv
import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Tuple

from src.core.logger import get_logger
from src.core.retention_policy import select_paths_for_retention
from src.core.schedule_config import ScheduleConfig
from src.core.sql_query_classifier import classify_sql_statement
from src.core.sql_statement_parser import parse_sql_statements

logger = get_logger(__name__)


class SqlQueryTaskExecutor:
    """스케줄 SQL 쿼리 실행 + 결과 저장 + 보관 정책 적용"""

    def __init__(self, resolve_connection: Callable, connector_factory: Callable, log_writer):
        """
        Args:
            resolve_connection: schedule -> (resolved, error_message) 콜백
            connector_factory: create_rust_db_connector 호환 시그니처의 커넥터 생성 콜백
                (BackupScheduler._make_connector를 경유해야 monkeypatch가 반영된다 -
                이 executor에서 create_rust_db_connector를 직접 import하지 않는다)
            log_writer: ExecutionLogWriter 인스턴스 (log_execution 메서드 제공)
        """
        self.resolve_connection = resolve_connection
        self.connector_factory = connector_factory
        self.log_writer = log_writer

    def execute(self, schedule: ScheduleConfig) -> Tuple[bool, str]:
        """SQL 쿼리 실행

        Returns:
            (success, message)
        """
        logger.info(f"SQL 쿼리 실행 시작: {schedule.name}")

        try:
            resolved, error_msg = self.resolve_connection(schedule)
            if error_msg:
                logger.error(error_msg)
                self.log_writer.log_execution(schedule, False, error_msg)
                return False, error_msg

            # DB 연결 (복호화된 자격 증명 사용)
            connector = self.connector_factory(
                resolved.engine,
                resolved.host,
                resolved.port,
                resolved.user,
                resolved.password,
                schedule.schema if schedule.schema else None,
                schema=schedule.schema if resolved.engine == 'postgresql' else "",
            )

            success, msg = connector.connect()
            if not success:
                error_msg = f"DB 연결 실패: {msg}"
                logger.error(error_msg)
                self.log_writer.log_execution(schedule, False, error_msg)
                return False, error_msg

            try:
                # 멀티 쿼리 파싱 (세미콜론으로 구분)
                queries = self.parse_queries(schedule.sql_query)
                if not queries:
                    error_msg = "실행할 SQL 쿼리가 없습니다."
                    logger.error(error_msg)
                    self.log_writer.log_execution(schedule, False, error_msg)
                    return False, error_msg

                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                results_saved = []
                affected_rows_total = 0

                for idx, query in enumerate(queries):
                    query_result = self.execute_single(
                        connector, schedule, query, timestamp, idx
                    )
                    if not query_result['success']:
                        error_msg = f"쿼리 실행 실패 (#{idx + 1}): {query_result['error']}"
                        logger.error(error_msg)
                        self.log_writer.log_execution(schedule, False, error_msg)
                        return False, error_msg

                    if query_result.get('file_path'):
                        results_saved.append(query_result['file_path'])
                    if query_result.get('affected_rows'):
                        affected_rows_total += query_result['affected_rows']

                # 결과 파일 정리 (보관 정책)
                self._cleanup_old_results(schedule)

                # last_run 업데이트
                schedule.last_run = datetime.now().isoformat()

                # 결과 메시지 생성
                if results_saved:
                    message = f"SQL 실행 완료: {len(queries)}개 쿼리, 결과 파일 {len(results_saved)}개 저장"
                else:
                    message = f"SQL 실행 완료: {len(queries)}개 쿼리, {affected_rows_total}행 영향"

                logger.info(message)
                self.log_writer.log_execution(schedule, True, message)
                return True, message

            finally:
                connector.disconnect()

        except Exception as e:
            error_msg = f"SQL 쿼리 실행 오류: {str(e)}"
            logger.exception(error_msg)
            self.log_writer.log_execution(schedule, False, error_msg)
            return False, error_msg

    def parse_queries(self, sql_text: str) -> List[str]:
        """SQL 텍스트를 개별 쿼리로 파싱."""
        return parse_sql_statements(sql_text)

    def execute_single(
        self,
        connector,
        schedule: ScheduleConfig,
        query: str,
        timestamp: str,
        query_index: int
    ) -> Dict[str, Any]:
        """단일 쿼리 실행

        Returns:
            {
                'success': bool,
                'error': str (실패 시),
                'file_path': str (결과셋 저장 시),
                'row_count': int (결과셋인 경우),
                'affected_rows': int (DML 실행 시)
            }
        """
        try:
            with connector.connection.cursor() as cursor:
                # 타임아웃 설정 (MySQL 8.0+)
                endpoint = getattr(getattr(connector, "connection", None), "endpoint", None)
                engine_name = getattr(endpoint, "engine", "mysql")
                if schedule.query_timeout > 0:
                    try:
                        if engine_name == "postgresql":
                            cursor.execute(f"SET statement_timeout = {schedule.query_timeout * 1000}")
                        else:
                            cursor.execute(
                                f"SET SESSION MAX_EXECUTION_TIME = {schedule.query_timeout * 1000}"
                            )
                    except Exception:
                        # 엔진별 statement timeout 미지원 시 무시
                        pass

                # 쿼리 실행
                cursor.execute(query)

                # 결과셋 여부는 cursor.description으로만 판단한다 (SELECT 접두사 문자열 검사 금지 -
                # WITH(CTE), SHOW, DESC 등도 결과셋을 반환하지만 'SELECT'로 시작하지 않는다).
                classification = classify_sql_statement(query)
                has_result_set = cursor.description is not None
                logger.debug(
                    f"쿼리 분류: leading_keyword={classification.leading_keyword}, "
                    f"has_result_set={has_result_set}"
                )

                if has_result_set:
                    columns = [desc[0] for desc in cursor.description] if cursor.description is not None else []
                    rows = cursor.fetchall()

                    if schedule.result_format != 'none':
                        # 0행이어도 헤더만 있는 결과 파일을 저장한다.
                        file_path = self._save_query_result(
                            schedule, columns, rows, timestamp, query_index
                        )
                        return {
                            'success': True,
                            'file_path': file_path,
                            'row_count': len(rows)
                        }
                    return {'success': True, 'row_count': len(rows)}
                else:
                    # DML (INSERT, UPDATE, DELETE)
                    connector.connection.commit()
                    return {
                        'success': True,
                        'affected_rows': cursor.rowcount
                    }

        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def _save_query_result(
        self,
        schedule: ScheduleConfig,
        columns: List[str],
        rows: List[Dict[str, Any]],
        timestamp: str,
        query_index: int
    ) -> str:
        """쿼리 결과를 파일로 저장

        Returns:
            저장된 파일 경로
        """
        # 출력 디렉토리
        output_dir = schedule.get_result_output_path()
        os.makedirs(output_dir, exist_ok=True)

        # 파일명 생성
        filename_base = schedule.result_filename_pattern.format(
            name=schedule.name,
            timestamp=timestamp,
            date=timestamp[:8]  # YYYYMMDD
        )

        # 멀티 쿼리인 경우 suffix 추가
        if query_index > 0:
            filename_base = f"{filename_base}_{query_index + 1:02d}"

        # 확장자
        ext = 'csv' if schedule.result_format == 'csv' else 'json'
        filename = f"{filename_base}.{ext}"
        file_path = os.path.join(output_dir, filename)

        # 저장
        if schedule.result_format == 'csv':
            self._save_as_csv(file_path, columns, rows)
        else:
            self._save_as_json(file_path, columns, rows)

        logger.info(f"쿼리 결과 저장: {file_path} ({len(rows)}행)")
        return file_path

    def _save_as_csv(
        self,
        file_path: str,
        columns: List[str],
        rows: List[Dict[str, Any]]
    ):
        """CSV 형식으로 저장"""
        with open(file_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def _save_as_json(
        self,
        file_path: str,
        columns: List[str],
        rows: List[Dict[str, Any]]
    ):
        """JSON 형식으로 저장"""
        # datetime 객체 직렬화 처리
        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            if hasattr(obj, '__str__'):
                return str(obj)
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        data = {
            'columns': columns,
            'row_count': len(rows),
            'generated_at': datetime.now().isoformat(),
            'data': rows
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=json_serializer)

    def _cleanup_old_results(self, schedule: ScheduleConfig):
        """오래된 결과 파일 정리 (보관 정책 적용)"""
        try:
            output_dir = schedule.get_result_output_path()
            if not os.path.exists(output_dir):
                return

            # 파일명 패턴에서 prefix 추출
            prefix = schedule.name

            # 결과 파일 목록
            result_files = []
            for name in os.listdir(output_dir):
                if not name.startswith(prefix):
                    continue

                full_path = os.path.join(output_dir, name)
                if not os.path.isfile(full_path):
                    continue

                # csv 또는 json 파일만
                if not (name.endswith('.csv') or name.endswith('.json')):
                    continue

                # 파일 수정 시간
                mtime = datetime.fromtimestamp(os.path.getmtime(full_path))
                result_files.append((full_path, mtime))

            if not result_files:
                return

            to_delete = select_paths_for_retention(
                result_files, schedule.result_retention_days, schedule.result_retention_count
            )

            # 삭제 실행
            for path in to_delete:
                try:
                    os.remove(path)
                    logger.info(f"오래된 결과 파일 삭제: {path}")
                except Exception as e:
                    logger.error(f"결과 파일 삭제 실패: {path} - {e}")

        except Exception as e:
            logger.error(f"결과 파일 정리 오류: {e}")
