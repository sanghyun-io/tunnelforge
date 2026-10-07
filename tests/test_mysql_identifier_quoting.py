"""MySQL SQL 생성기가 식별자 안의 백틱을 escape하는지 검증 (백틱 포함 이름에서 SQL이 깨지던 문제)."""
from src.core.db_core_dbapi_shim import quote_mysql_ident

TRICKY = "we`ird"


def test_quote_mysql_ident_doubles_backticks():
    assert quote_mysql_ident(TRICKY) == "`we``ird`"
    assert quote_mysql_ident("plain") == "`plain`"


# 스키마 비교 동기화 SQL / FK 재생성 SQL / 고아 조회 SQL 의 식별자 escape 는
# Rust schema_compare / upgrade_fix / upgrade_analyze 테스트가 검증한다.
