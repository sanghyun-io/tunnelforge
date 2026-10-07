"""MySQL SQL 생성기가 식별자 안의 백틱을 escape하는지 검증 (백틱 포함 이름에서 SQL이 깨지던 문제)."""
from src.core.db_core_dbapi_shim import quote_mysql_ident
from src.core.migration_analysis_models import OrphanRecord
from src.ui.dialogs.migration_dialogs import build_orphan_select_sql

TRICKY = "we`ird"


def test_quote_mysql_ident_doubles_backticks():
    assert quote_mysql_ident(TRICKY) == "`we``ird`"
    assert quote_mysql_ident("plain") == "`plain`"


def test_generators_escape_backticks_in_names():
    orphan = OrphanRecord(child_table=TRICKY, child_column="c`1", parent_table="p", parent_column="id", orphan_count=1)
    assert "`we``ird`" in build_orphan_select_sql(orphan, "app") and "`c``1`" in build_orphan_select_sql(orphan, "app")

    # 스키마 비교 동기화 SQL / FK 재생성 SQL 의 식별자 escape 는 Rust schema_compare / upgrade_fix 테스트가 검증한다.
