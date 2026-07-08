"""
스토리지 엔진 규칙 모듈

MySQL 8.0 → 8.4 업그레이드 시 스토리지 엔진 관련 호환성 검사 규칙.
- S10: MyISAM 엔진
- S11: ARCHIVE 엔진
- S12: BLACKHOLE 엔진
- S13: FEDERATED 엔진
- S14: 파티션 공유 테이블스페이스
- S15: 비네이티브 파티셔닝
- S16: 비InnoDB 엔진에 FK 사용 (이슈 #63)
"""

import re
from typing import Dict, List, Optional, Callable, TYPE_CHECKING

from ..migration_constants import (
    IssueType,
    CompatibilityIssue,
    STORAGE_ENGINE_STATUS,
    ENGINE_POLICIES,
    INVALID_ENGINE_FK_PATTERN,
)

if TYPE_CHECKING:
    from ..db_connector import MySQLConnector


class StorageRules:
    """스토리지 엔진 규칙 모음"""

    def __init__(self, connector: Optional['MySQLConnector'] = None):
        self.connector = connector
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    @staticmethod
    def _engine_policy(engine: str) -> Dict[str, str]:
        """엔진별 정책(severity, suggestion) 조회 (대소문자 무시).

        migration_analyzer.py와 동일한 ENGINE_POLICIES를 단일 소스로 사용한다.
        목록에 없는 엔진은 기본 정책으로 대체한다.
        """
        for policy_engine, policy in ENGINE_POLICIES.items():
            if policy_engine.lower() == engine.lower():
                return policy
        return {"severity": "warning", "suggestion": "ENGINE=InnoDB로 변경 권장"}

    # ================================================================
    # S10-S13: deprecated 스토리지 엔진 검사 (라이브 DB)
    # ================================================================
    def check_deprecated_engines(self, schema: str) -> List[CompatibilityIssue]:
        """deprecated 스토리지 엔진 사용 확인"""
        if not self.connector:
            return []

        self._log("🔍 deprecated 스토리지 엔진 검사 중...")
        issues = []

        deprecated_engines = STORAGE_ENGINE_STATUS['deprecated']
        engines_str = ', '.join(f"'{e}'" for e in deprecated_engines)

        query = f"""
        SELECT TABLE_NAME, ENGINE
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s
            AND TABLE_TYPE = 'BASE TABLE'
            AND ENGINE IN ({engines_str})
        """
        tables = self.connector.execute(query, (schema,))

        for table in tables:
            engine = table['ENGINE']
            policy = self._engine_policy(engine)

            issues.append(CompatibilityIssue(
                issue_type=IssueType.DEPRECATED_ENGINE,
                severity=policy['severity'],
                location=f"{schema}.{table['TABLE_NAME']}",
                description=f"deprecated 스토리지 엔진: {engine}",
                suggestion=policy['suggestion'],
                table_name=table['TABLE_NAME']
            ))

        if issues:
            self._log(f"  ⚠️ deprecated 엔진 {len(issues)}개 발견")
        else:
            self._log("  ✅ deprecated 엔진 없음")

        return issues

    # ================================================================
    # deprecated 엔진 검사 (덤프 파일)
    # ================================================================
    def check_deprecated_engines_in_sql(self, content: str, location: str) -> List[CompatibilityIssue]:
        """SQL 파일에서 deprecated 스토리지 엔진 사용 확인"""
        issues = []

        deprecated_engines = STORAGE_ENGINE_STATUS['deprecated']

        for engine in deprecated_engines:
            # ENGINE=MyISAM 또는 ENGINE = MyISAM 패턴
            pattern = re.compile(
                rf'\bENGINE\s*=\s*{re.escape(engine)}\b',
                re.IGNORECASE
            )
            policy = self._engine_policy(engine)

            for match in pattern.finditer(content):
                # 테이블명 추출 시도
                before = content[max(0, match.start() - 200):match.start()]
                table_match = re.search(r'CREATE\s+TABLE\s+`?(\w+)`?', before, re.IGNORECASE)
                table_name = table_match.group(1) if table_match else "unknown"

                issues.append(CompatibilityIssue(
                    issue_type=IssueType.DEPRECATED_ENGINE,
                    severity=policy['severity'],
                    location=location,
                    description=f"{engine} 엔진 사용 (deprecated): {table_name}",
                    suggestion=policy['suggestion'],
                    table_name=table_name
                ))

        return issues

    # ================================================================
    # S14: 파티션 공유 테이블스페이스 검사 (라이브 DB)
    # ================================================================
    def check_partition_shared_tablespace(self, schema: str) -> List[CompatibilityIssue]:
        """공유 테이블스페이스의 파티션 테이블 확인"""
        if not self.connector:
            return []

        self._log("🔍 파티션 테이블스페이스 검사 중...")
        issues = []

        query = """
        SELECT DISTINCT TABLE_NAME, TABLESPACE_NAME
        FROM INFORMATION_SCHEMA.PARTITIONS
        WHERE TABLE_SCHEMA = %s
            AND TABLESPACE_NAME IS NOT NULL
            AND TABLESPACE_NAME NOT IN ('innodb_file_per_table', 'innodb_system')
            AND TABLESPACE_NAME NOT LIKE '%%.ibd'
        """
        partitions = self.connector.execute(query, (schema,))

        for p in partitions:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.PARTITION_ISSUE,
                severity="warning",
                location=f"{schema}.{p['TABLE_NAME']}",
                description=f"파티션이 공유 테이블스페이스 사용: {p['TABLESPACE_NAME']}",
                suggestion="file-per-table 테이블스페이스로 마이그레이션 권장",
                table_name=p['TABLE_NAME']
            ))

        if issues:
            self._log(f"  ⚠️ 공유 테이블스페이스 파티션 {len(issues)}개 발견")
        else:
            self._log("  ✅ 공유 테이블스페이스 파티션 없음")

        return issues

    # ================================================================
    # S15: 비네이티브 파티셔닝 검사 (덤프 파일)
    # ================================================================
    def check_partition_non_native(self, content: str, location: str) -> List[CompatibilityIssue]:
        """비네이티브 파티셔닝 사용 확인"""
        issues = []

        # ha_partition 엔진 사용 감지 (구버전 MySQL에서 사용)
        pattern = re.compile(r'\bha_partition\b', re.IGNORECASE)

        for match in pattern.finditer(content):
            issues.append(CompatibilityIssue(
                issue_type=IssueType.PARTITION_ISSUE,
                severity="error",
                location=location,
                description="비네이티브 파티셔닝(ha_partition) 사용",
                suggestion="네이티브 파티셔닝으로 마이그레이션 필요"
            ))

        return issues

    # ================================================================
    # S16: 비InnoDB 엔진에 FK 사용 검사 (덤프 파일, 이슈 #63)
    # ================================================================
    def check_invalid_engine_fk(self, content: str, location: str) -> List[CompatibilityIssue]:
        """비InnoDB 엔진(MyISAM, MEMORY, ARCHIVE)을 사용하는 테이블에 FK 정의 확인"""
        issues = []

        # CREATE TABLE 단위로 분리하여 검사
        # FK와 비InnoDB 엔진이 동일 CREATE TABLE 구문 내에 있는지 확인
        create_table_pattern = re.compile(
            r'CREATE\s+TABLE\s+`?(\w+)`?\s*\([^;]+?\)\s*[^;]*ENGINE\s*=\s*(MyISAM|MEMORY|ARCHIVE|CSV)[^;]*;',
            re.IGNORECASE | re.DOTALL
        )

        for match in create_table_pattern.finditer(content):
            table_body = match.group(0)
            table_name = match.group(1)
            engine = match.group(2).upper()

            # 해당 CREATE TABLE 내에 FOREIGN KEY가 있는지 확인
            if re.search(r'\bFOREIGN\s+KEY\b', table_body, re.IGNORECASE):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.INVALID_ENGINE_FK,
                    severity="error",
                    location=location,
                    description=f"비InnoDB 엔진({engine})을 사용하는 테이블 '{table_name}'에 FOREIGN KEY 정의",
                    suggestion=f"ALTER TABLE `{table_name}` ENGINE=InnoDB 또는 FOREIGN KEY 제거",
                    table_name=table_name
                ))

        return issues

    # ================================================================
    # 엔진별 통계 조회 (정보성)
    # ================================================================
    def get_engine_statistics(self, schema: str) -> dict:
        """스키마의 스토리지 엔진 사용 통계"""
        if not self.connector:
            return {}

        query = """
        SELECT ENGINE, COUNT(*) as table_count
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s
            AND TABLE_TYPE = 'BASE TABLE'
        GROUP BY ENGINE
        ORDER BY table_count DESC
        """
        result = self.connector.execute(query, (schema,))

        stats = {}
        for row in result:
            stats[row['ENGINE'] or 'None'] = row['table_count']

        return stats

    # ================================================================
    # 통합 검사 메서드
    # ================================================================
    def check_all_live_db(self, schema: str) -> List[CompatibilityIssue]:
        """라이브 DB의 모든 스토리지 엔진 검사 실행"""
        if not self.connector:
            return []

        issues = []
        issues.extend(self.check_deprecated_engines(schema))
        issues.extend(self.check_partition_shared_tablespace(schema))
        return issues

    def check_all_sql_content(self, content: str, location: str) -> List[CompatibilityIssue]:
        """SQL 파일 내용의 모든 스토리지 엔진 검사 실행"""
        issues = []
        issues.extend(self.check_deprecated_engines_in_sql(content, location))
        issues.extend(self.check_partition_non_native(content, location))
        # 신규 규칙 (이슈 #63)
        issues.extend(self.check_invalid_engine_fk(content, location))
        return issues
