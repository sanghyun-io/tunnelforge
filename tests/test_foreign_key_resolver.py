"""
ForeignKeyResolver / OrphanRecordInfo 신규 경로(src.core.foreign_key_resolver) 테스트
"""
from unittest.mock import MagicMock


def test_import_path_from_core_module():
    """새 경로에서 ForeignKeyResolver/OrphanRecordInfo를 직접 import할 수 있다."""
    from src.core.foreign_key_resolver import ForeignKeyResolver, OrphanRecordInfo

    assert ForeignKeyResolver is not None
    assert OrphanRecordInfo is not None


def test_rust_dump_exporter_reexports_same_symbols():
    """rust_dump_exporter의 re-export가 core 모듈의 심볼과 동일 객체를 가리킨다."""
    from src.core.foreign_key_resolver import ForeignKeyResolver as CoreResolver
    from src.core.foreign_key_resolver import OrphanRecordInfo as CoreOrphanInfo
    from src.exporters.rust_dump_exporter import ForeignKeyResolver as ReexportedResolver
    from src.exporters.rust_dump_exporter import OrphanRecordInfo as ReexportedOrphanInfo

    assert CoreResolver is ReexportedResolver
    assert CoreOrphanInfo is ReexportedOrphanInfo


def test_get_all_dependencies():
    """전체 FK 의존성 조회"""
    from src.core.foreign_key_resolver import ForeignKeyResolver

    mock_connector = MagicMock()
    mock_connector.execute.return_value = [
        {'TABLE_NAME': 'posts', 'REFERENCED_TABLE_NAME': 'users'},
        {'TABLE_NAME': 'comments', 'REFERENCED_TABLE_NAME': 'posts'},
    ]

    resolver = ForeignKeyResolver(mock_connector)
    deps = resolver.get_all_dependencies('blog')

    assert deps == {'posts': {'users'}, 'comments': {'posts'}}


def test_generate_orphan_query_contains_join_and_where():
    """generate_orphan_query가 스키마/테이블/컬럼을 포함한 유효한 조회 쿼리를 생성한다."""
    from src.core.foreign_key_resolver import ForeignKeyResolver

    resolver = ForeignKeyResolver(MagicMock())
    query = resolver.generate_orphan_query('blog', 'posts', 'user_id', 'users', 'id')

    assert "FROM `blog`.`posts` c" in query
    assert "LEFT JOIN `blog`.`users` p ON c.`user_id` = p.`id`" in query
    assert "WHERE c.`user_id` IS NOT NULL AND p.`id` IS NULL" in query


def test_find_orphan_records_uses_shared_join_fragment_for_count_and_sample():
    """find_orphan_records의 count/sample 쿼리가 동일한 JOIN/WHERE 조건을 공유한다."""
    from src.core.foreign_key_resolver import ForeignKeyResolver

    mock_connector = MagicMock()
    mock_connector.execute.side_effect = [
        [{'TABLE_NAME': 'posts', 'COLUMN_NAME': 'user_id',
          'REFERENCED_TABLE_NAME': 'users', 'REFERENCED_COLUMN_NAME': 'id',
          'CONSTRAINT_NAME': 'fk_posts_users'}],
        [{'cnt': 2}],
        [{'orphan_value': '10'}, {'orphan_value': '11'}],
    ]

    resolver = ForeignKeyResolver(mock_connector)
    results = resolver.find_orphan_records('blog')

    assert len(results) == 1
    info = results[0]
    assert info.orphan_count == 2
    assert info.sample_values == ['10', '11']

    count_query = mock_connector.execute.call_args_list[1].args[0]
    sample_query = mock_connector.execute.call_args_list[2].args[0]
    assert "FROM `blog`.`posts` c" in count_query
    assert "FROM `blog`.`posts` c" in sample_query
    assert "WHERE c.`user_id` IS NOT NULL AND p.`id` IS NULL" in count_query
    assert "WHERE c.`user_id` IS NOT NULL AND p.`id` IS NULL" in sample_query
    assert "LIMIT 5" in sample_query
    assert "LIMIT" not in count_query


def test_get_all_orphan_queries_contains_fk_relation_alias():
    """get_all_orphan_queries가 각 FK별로 집계 쿼리를 생성한다."""
    from src.core.foreign_key_resolver import ForeignKeyResolver

    mock_connector = MagicMock()
    mock_connector.execute.return_value = [
        {'TABLE_NAME': 'posts', 'COLUMN_NAME': 'user_id',
         'REFERENCED_TABLE_NAME': 'users', 'REFERENCED_COLUMN_NAME': 'id',
         'CONSTRAINT_NAME': 'fk_posts_users'},
    ]

    resolver = ForeignKeyResolver(mock_connector)
    sql = resolver.get_all_orphan_queries('blog')

    assert "posts.user_id" in sql
    assert "AS fk_relation" in sql
    assert "AS orphan_count" in sql
    assert "FROM `blog`.`posts` c" in sql
