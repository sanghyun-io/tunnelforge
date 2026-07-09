"""중앙 상수 모듈

MySQL 관련 기본값과 시스템 스키마 목록을 한 곳에서 관리합니다.
"""

# MySQL 기본 포트
DEFAULT_MYSQL_PORT = 3306

# SSH 터널의 로컬 바인드 호스트 (localhost)
DEFAULT_LOCAL_HOST = '127.0.0.1'

# DB 연결 기본값
DEFAULT_DB_USER = 'root'
DEFAULT_DB_ENGINE = 'mysql'

# UI log caps
MAX_VISIBLE_LOG_LINES = 500
MAX_LOG_ENTRIES = 500

# Dialog table status icons
TABLE_STATUS_ICONS = {
    'pending': '⏳',
    'loading': '🔄',
    'done': '✅',
    'error': '❌',
}

# MySQL 시스템 스키마 목록 (사용자 데이터베이스 목록 조회 시 제외 대상)
SYSTEM_SCHEMAS = frozenset({
    'information_schema',
    'mysql',
    'performance_schema',
    'sys',
    'ndbinfo',
})
