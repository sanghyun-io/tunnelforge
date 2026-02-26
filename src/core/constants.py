"""중앙 상수 모듈

MySQL 관련 기본값과 시스템 스키마 목록을 한 곳에서 관리합니다.
"""

# MySQL 기본 포트
DEFAULT_MYSQL_PORT = 3306

# SSH 터널의 로컬 바인드 호스트 (localhost)
DEFAULT_LOCAL_HOST = '127.0.0.1'

# MySQL 시스템 스키마 목록 (사용자 데이터베이스 목록 조회 시 제외 대상)
SYSTEM_SCHEMAS = frozenset({
    'information_schema',
    'mysql',
    'performance_schema',
    'sys',
})
