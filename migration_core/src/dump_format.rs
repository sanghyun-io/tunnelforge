use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use mysql::prelude::Queryable;
use crate::*;

/// 이 평균 행 크기(바이트) 이상인 넓은 테이블에서는, 직전 실행에서 학습된 chunk_rows 프로파일이
/// 바이트 목표 기반 추정보다 실측에 가깝다고 보고 학습값을 신뢰한다.
const LEARNED_PROFILE_LARGE_ROW_BYTES: u64 = 4_096;

pub fn table_dependency_order(schema: &NormalizedSchema) -> Vec<String> {
    let (ordered, _) = table_dependency_order_indices(schema);
    ordered
        .into_iter()
        .map(|index| schema.tables[index].name.clone())
        .collect()
}

pub fn dependency_ordered_schema(schema: &NormalizedSchema) -> NormalizedSchema {
    let (ordered, _) = table_dependency_order_indices(schema);
    NormalizedSchema {
        tables: ordered
            .into_iter()
            .map(|index| schema.tables[index].clone())
            .collect(),
    }
}

pub(crate) fn dependency_ordered_dump_tables(
    schema: &NormalizedSchema,
    tables: Vec<DumpTableManifest>,
) -> Vec<DumpTableManifest> {
    let mut by_name = tables
        .into_iter()
        .map(|table| (table.name.clone(), table))
        .collect::<BTreeMap<_, _>>();
    let mut ordered = Vec::new();
    for table_name in table_dependency_order(schema) {
        if let Some(table) = by_name.remove(&table_name) {
            ordered.push(table);
        }
    }
    ordered.extend(by_name.into_values());
    ordered
}

fn table_dependency_order_indices(schema: &NormalizedSchema) -> (Vec<usize>, Vec<String>) {
    let table_count = schema.tables.len();
    if table_count <= 1 {
        return ((0..table_count).collect(), Vec::new());
    }

    let table_index = schema
        .tables
        .iter()
        .enumerate()
        .map(|(index, table)| (table.name.clone(), index))
        .collect::<BTreeMap<_, _>>();
    let mut dependents = vec![Vec::<usize>::new(); table_count];
    let mut seen_edges = BTreeSet::new();
    let mut indegree = vec![0_usize; table_count];

    for (child_index, table) in schema.tables.iter().enumerate() {
        for fk in &table.foreign_keys {
            let Some(parent_index) = table_index.get(&fk.referenced_table).copied() else {
                continue;
            };
            if parent_index == child_index {
                continue;
            }
            if seen_edges.insert((parent_index, child_index)) {
                dependents[parent_index].push(child_index);
                indegree[child_index] += 1;
            }
        }
    }

    let mut ready = VecDeque::new();
    for (index, degree) in indegree.iter().enumerate() {
        if *degree == 0 {
            ready.push_back(index);
        }
    }

    let mut ordered = Vec::with_capacity(table_count);
    while let Some(index) = ready.pop_front() {
        ordered.push(index);
        dependents[index].sort_unstable();
        for child_index in &dependents[index] {
            indegree[*child_index] -= 1;
            if indegree[*child_index] == 0 {
                ready.push_back(*child_index);
            }
        }
    }

    if ordered.len() == table_count {
        return (ordered, Vec::new());
    }

    let ordered_set = ordered.iter().copied().collect::<BTreeSet<_>>();
    let cyclic = (0..table_count)
        .filter(|index| !ordered_set.contains(index))
        .map(|index| schema.tables[index].name.clone())
        .collect::<Vec<_>>();
    for index in 0..table_count {
        if !ordered_set.contains(&index) {
            ordered.push(index);
        }
    }
    (ordered, cyclic)
}

pub(crate) fn parse_schema(value: &Value) -> Result<NormalizedSchema, serde_json::Error> {
    serde_json::from_value(value.clone())
}

pub(crate) fn parse_options(payload: &Value) -> MigrationOptions {
    payload
        .get("execution_options")
        .and_then(|value| serde_json::from_value::<MigrationOptions>(value.clone()).ok())
        .unwrap_or(MigrationOptions {
            mode: default_mode(),
            chunk_size: default_chunk_size(),
            cancel_after_chunks: None,
            cleanup_before_migrate: false,
        })
}

pub(crate) fn string_list(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|item| !item.is_empty())
                .map(ToString::to_string)
                .collect()
        })
        .unwrap_or_default()
}

pub(crate) fn safe_dump_component(value: &str) -> String {
    let mut safe = String::new();
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.') {
            safe.push(ch);
        } else {
            safe.push('_');
        }
    }
    if safe.is_empty() {
        "table".to_string()
    } else {
        safe
    }
}

pub(crate) fn single_numeric_primary_key(table: &NormalizedTable) -> Option<&str> {
    let primary_columns = table
        .columns
        .iter()
        .filter(|column| column.primary_key)
        .collect::<Vec<_>>();
    if primary_columns.len() != 1 {
        return None;
    }
    let column = primary_columns[0];
    if is_integer_key_type(&column.type_name) {
        Some(column.name.as_str())
    } else {
        None
    }
}

fn is_integer_key_type(type_name: &str) -> bool {
    let type_name = type_name.trim().to_ascii_lowercase();
    type_name.starts_with("tinyint")
        || type_name.starts_with("smallint")
        || type_name.starts_with("mediumint")
        || type_name.starts_with("int")
        || type_name.starts_with("integer")
        || type_name.starts_with("bigint")
        || type_name.starts_with("serial")
}

pub(crate) fn should_use_pk_range_dump(table: &NormalizedTable, row_count: u64, chunk_size: usize) -> bool {
    let threshold = (chunk_size as u64).saturating_mul(2);
    row_count >= threshold && single_numeric_primary_key(table).is_some()
}

pub(crate) fn should_use_pk_range_dump_for_span(
    table: &NormalizedTable,
    row_count: u64,
    chunk_size: usize,
    min_key: i128,
    max_key: i128,
) -> bool {
    if !should_use_pk_range_dump(table, row_count, chunk_size) || min_key > max_key {
        return false;
    }

    let span = max_key.saturating_sub(min_key).saturating_add(1) as u128;
    let row_capacity = (row_count as u128).saturating_mul(MYSQL_PK_RANGE_MAX_SPAN_TO_ROW_RATIO);
    span <= row_capacity
}

pub(crate) fn mysql_range_chunk_size_for_avg_row(fallback_chunk_size: usize, avg_row_bytes: u64) -> usize {
    let fallback_chunk_size = fallback_chunk_size.max(1);
    if avg_row_bytes == 0 {
        return fallback_chunk_size;
    }

    let byte_target_rows =
        MYSQL_DUMP_TARGET_BYTES_PER_CHUNK.saturating_add(avg_row_bytes - 1) / avg_row_bytes;
    byte_target_rows
        .max(1)
        .min(fallback_chunk_size as u64)
        .max(1) as usize
}

/// 순차 MySQL 덤프의 청크 행 수를 산출한다.
///
/// 병렬 경로와 동일하게 바이트 목표(≈64MB) 기반으로 1차 산출하되, `AVG_ROW_LENGTH`가
/// off-page 대형 컬럼을 과소계상하는 경우를 대비해 절대 행수 상한
/// (`MYSQL_DUMP_SEQUENTIAL_MAX_ROWS_PER_CHUNK`)으로 한 번 더 묶는다. avg가 0이면
/// `mysql_range_chunk_size_for_avg_row`가 fallback을 반환하므로, 결과적으로 avg를
/// 신뢰할 수 없을 때는 상한이 지배해 스트리밍 코덱 크래시를 원천 차단한다.
pub(crate) fn sequential_mysql_chunk_size(fallback_chunk_size: usize, avg_row_bytes: u64) -> usize {
    mysql_range_chunk_size_for_avg_row(fallback_chunk_size, avg_row_bytes)
        .min(MYSQL_DUMP_SEQUENTIAL_MAX_ROWS_PER_CHUNK)
        .max(1)
}

pub(crate) fn learned_mysql_range_chunk_size(
    fallback_chunk_size: usize,
    avg_row_bytes: u64,
    profile: Option<&DumpTablePerfProfile>,
) -> usize {
    let byte_target_size = mysql_range_chunk_size_for_avg_row(fallback_chunk_size, avg_row_bytes);
    let Some(profile) = profile else {
        return byte_target_size;
    };
    if avg_row_bytes >= LEARNED_PROFILE_LARGE_ROW_BYTES && profile.chunk_rows >= byte_target_size {
        return profile.chunk_rows.max(1).min(fallback_chunk_size.max(1));
    }
    byte_target_size
}

pub(crate) fn mysql_table_avg_row_length(
    conn: &mut mysql::PooledConn,
    endpoint: &Endpoint,
    table: &str,
) -> u64 {
    let schema_name = endpoint_schema(endpoint);
    let sql = format!(
        "SELECT COALESCE(AVG_ROW_LENGTH, 0) FROM information_schema.tables WHERE TABLE_SCHEMA = {} AND TABLE_NAME = {}",
        sql_literal(&Value::String(schema_name)),
        sql_literal(&Value::String(table.to_string()))
    );
    conn.query_first::<u64, _>(sql).ok().flatten().unwrap_or(0)
}

pub(crate) fn mysql_numeric_min_max(
    conn: &mut mysql::PooledConn,
    table: &str,
    column: &str,
) -> Result<Option<(i128, i128)>, String> {
    let sql = format!(
        "SELECT CAST(MIN({}) AS CHAR), CAST(MAX({}) AS CHAR) FROM {}",
        quote_ident("mysql", column),
        quote_ident("mysql", column),
        quote_ident("mysql", table)
    );
    let result = conn
        .query_first::<(Option<String>, Option<String>), _>(sql)
        .map_err(|err| format!("mysql pk range inspect error: {err}"))?;
    let Some((Some(min), Some(max))) = result else {
        return Ok(None);
    };
    let min = min
        .parse::<i128>()
        .map_err(|err| format!("mysql pk min parse error: {err}"))?;
    let max = max
        .parse::<i128>()
        .map_err(|err| format!("mysql pk max parse error: {err}"))?;
    Ok(Some((min, max)))
}

pub(crate) fn pk_ranges(min_key: i128, max_key: i128, row_count: u64, chunk_size: usize) -> Vec<DumpRange> {
    let chunk_count = ((row_count as usize).saturating_add(chunk_size.saturating_sub(1))
        / chunk_size.max(1))
    .max(1);
    let span = max_key.saturating_sub(min_key).saturating_add(1);
    let width = ((span + chunk_count as i128 - 1) / chunk_count as i128).max(1);
    let mut ranges = Vec::new();
    let mut start = min_key;
    let mut chunk_index = 1_u64;
    while start <= max_key {
        let end = start.saturating_add(width - 1).min(max_key);
        ranges.push(DumpRange {
            chunk_index,
            start,
            end,
        });
        chunk_index += 1;
        if end == max_key {
            break;
        }
        start = end + 1;
    }
    ranges
}

pub(crate) fn current_unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

pub(crate) fn write_dump_manifest(output_path: &Path, manifest: &DumpManifest) -> Result<(), String> {
    let marker_path = output_path.join(DUMP_DIR_MARKER);
    let marker_file =
        File::create(&marker_path).map_err(|err| format!("failed to create dump marker: {err}"))?;
    serde_json::to_writer_pretty(
        marker_file,
        &json!({
            "format": "tunnelforge-dump-dir",
            "created_by": "tunnelforge-core",
            "version": 1
        }),
    )
    .map_err(|err| format!("failed to write dump marker: {err}"))?;
    let path = output_path.join("_tunnelforge_dump.json");
    let file =
        File::create(&path).map_err(|err| format!("failed to create dump manifest: {err}"))?;
    serde_json::to_writer_pretty(file, manifest)
        .map_err(|err| format!("failed to write dump manifest: {err}"))
}

pub(crate) fn read_dump_manifest(input_path: &Path) -> Result<DumpManifest, String> {
    let path = input_path.join("_tunnelforge_dump.json");
    let file = File::open(&path).map_err(|err| format!("failed to open dump manifest: {err}"))?;
    let manifest: DumpManifest = serde_json::from_reader(file)
        .map_err(|err| format!("failed to parse dump manifest: {err}"))?;
    for table in &manifest.tables {
        validate_dump_table_path(&table.path)?;
    }
    Ok(manifest)
}

fn validate_dump_table_path(path: &str) -> Result<(), String> {
    let table_path = Path::new(path);
    if path.trim().is_empty() || table_path.is_absolute() {
        return Err(format!("unsafe dump table path: {path}"));
    }
    for component in table_path.components() {
        match component {
            Component::Normal(_) | Component::CurDir => {}
            _ => return Err(format!("unsafe dump table path: {path}")),
        }
    }
    Ok(())
}

pub(crate) fn dump_manifest_chunk_path(
    input_path: &Path,
    table_path: &str,
    chunk_index: u64,
    data_format: &str,
    compression: &str,
) -> Result<PathBuf, String> {
    validate_dump_table_path(table_path)?;
    let base_path = fs::canonicalize(input_path)
        .map_err(|err| format!("failed to validate dump input_dir: {err}"))?;
    let raw_path =
        input_path
            .join(table_path)
            .join(dump_chunk_name(chunk_index, data_format, compression));
    let chunk_path = fs::canonicalize(&raw_path)
        .map_err(|err| format!("failed to validate dump chunk: {err}"))?;
    if !chunk_path.starts_with(&base_path) {
        return Err(format!(
            "dump chunk path is outside dump directory: {}",
            raw_path.display()
        ));
    }
    if !chunk_path.is_file() {
        return Err(format!(
            "dump chunk path is not a file: {}",
            raw_path.display()
        ));
    }
    Ok(chunk_path)
}

pub(crate) fn classified_import_error(code: &str, message: &str, scope: Option<&str>) -> String {
    match scope.filter(|value| !value.trim().is_empty()) {
        Some(scope) => format!("{code}: {scope}: {message}"),
        None => format!("{code}: {message}"),
    }
}

pub(crate) fn dump_import_ddl_error(operation: &str, table: &str, err: &str) -> String {
    classified_import_error(
        "load_failed",
        &format!("{operation} failed: {err}"),
        Some(table),
    )
}

/// 덤프 밖에 남는 MySQL FK 한 컬럼과 현재 부모 정의를 표현한다. 새 부모 정의가 이
/// 계약을 그대로 재현할 수 있는지 확인해서, 호환되는 target-only 테이블은 보존한다.
#[derive(Debug, Clone, PartialEq, Eq)]
struct SurvivingFkColumn {
    referencing_table: String,
    constraint_name: String,
    referenced_table: String,
    ordinal_position: u64,
    referenced_column: String,
    existing_parent_column_type: String,
    existing_parent_character_set: Option<String>,
    existing_parent_collation: Option<String>,
}

fn mysql_base_column_type(type_name: &str) -> String {
    let lower = type_name.trim().to_ascii_lowercase();
    [" character set ", " charset ", " collate "]
        .iter()
        .filter_map(|marker| lower.find(marker))
        .min()
        .map(|index| lower[..index].trim().to_string())
        .unwrap_or(lower)
}

fn mysql_charset_from_collation(collation: &str) -> Option<String> {
    collation.split_once('_').map(|(charset, _)| charset.to_string())
}

fn imported_mysql_column_fidelity(
    table: &NormalizedTable,
    column: &NormalizedColumn,
) -> MysqlCharacterFidelity {
    let mut fidelity = mysql_character_fidelity(&column.type_name);
    if fidelity.collation.is_none() {
        fidelity.collation = table.table_collation.clone();
    }
    if fidelity.character_set.is_none() {
        fidelity.character_set = fidelity
            .collation
            .as_deref()
            .and_then(mysql_charset_from_collation);
    }
    fidelity
}

fn imported_table_has_referenced_index(table: &NormalizedTable, columns: &[String]) -> bool {
    let primary_columns = table
        .columns
        .iter()
        .filter(|column| column.primary_key)
        .map(|column| column.name.as_str())
        .collect::<Vec<_>>();
    let requested = columns.iter().map(String::as_str).collect::<Vec<_>>();
    primary_columns.starts_with(&requested)
        || table.indexes.iter().any(|index| {
            index
                .columns
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
                .starts_with(&requested)
        })
        || (columns.len() == 1
            && table.columns.iter().any(|column| {
                column.name == columns[0] && (column.primary_key || column.unique)
            }))
}

fn incompatible_surviving_fk_offenders(
    rows: &[SurvivingFkColumn],
    import_schema: &NormalizedSchema,
) -> Vec<String> {
    let import_set = import_schema
        .tables
        .iter()
        .map(|table| table.name.as_str())
        .collect::<BTreeSet<_>>();
    let mut offenders = Vec::new();

    for row in rows {
        if import_set.contains(row.referencing_table.as_str())
            || !import_set.contains(row.referenced_table.as_str())
        {
            continue;
        }
        let Some(table) = import_schema
            .tables
            .iter()
            .find(|table| table.name == row.referenced_table)
        else {
            continue;
        };
        let Some(column) = table
            .columns
            .iter()
            .find(|column| column.name == row.referenced_column)
        else {
            offenders.push(format!(
                "{}.{} -> {} (referenced column {} is absent from dump)",
                row.referencing_table,
                row.constraint_name,
                row.referenced_table,
                row.referenced_column
            ));
            continue;
        };
        let existing_type = mysql_base_column_type(&row.existing_parent_column_type);
        let imported_type = mysql_base_column_type(&column.type_name);
        if existing_type != imported_type {
            offenders.push(format!(
                "{}.{} -> {} (column {} type changes from {} to {})",
                row.referencing_table,
                row.constraint_name,
                row.referenced_table,
                row.referenced_column,
                existing_type,
                imported_type
            ));
            continue;
        }
        let imported_fidelity = imported_mysql_column_fidelity(table, column);
        for (property, existing, imported) in [
            (
                "character set",
                row.existing_parent_character_set.as_deref(),
                imported_fidelity.character_set.as_deref(),
            ),
            (
                "collation",
                row.existing_parent_collation.as_deref(),
                imported_fidelity.collation.as_deref(),
            ),
        ] {
            if let (Some(existing), Some(imported)) = (existing, imported) {
                if !existing.eq_ignore_ascii_case(imported) {
                    offenders.push(format!(
                        "{}.{} -> {} (column {} {} changes from {} to {})",
                        row.referencing_table,
                        row.constraint_name,
                        row.referenced_table,
                        row.referenced_column,
                        property,
                        existing,
                        imported
                    ));
                    break;
                }
            }
        }
    }

    let mut fk_columns = BTreeMap::<(String, String, String), Vec<(u64, String)>>::new();
    for row in rows {
        if import_set.contains(row.referencing_table.as_str())
            || !import_set.contains(row.referenced_table.as_str())
        {
            continue;
        }
        fk_columns
            .entry((
                row.referencing_table.clone(),
                row.constraint_name.clone(),
                row.referenced_table.clone(),
            ))
            .or_default()
            .push((row.ordinal_position, row.referenced_column.clone()));
    }
    for ((referencing_table, constraint_name, referenced_table), mut columns) in fk_columns {
        columns.sort_by_key(|(ordinal, _)| *ordinal);
        let columns = columns
            .into_iter()
            .map(|(_, column)| column)
            .collect::<Vec<_>>();
        let Some(table) = import_schema
            .tables
            .iter()
            .find(|table| table.name == referenced_table)
        else {
            continue;
        };
        if !imported_table_has_referenced_index(table, &columns) {
            offenders.push(format!(
                "{referencing_table}.{constraint_name} -> {referenced_table} (dump does not recreate a referenced index beginning with {})",
                columns.join(", ")
            ));
        }
    }
    offenders.sort();
    offenders.dedup();
    offenders
}

/// 타겟 DB에서 import set 밖의 살아있는 referencing FK를 조회한다. 덤프가 재생성할
/// 부모 정의와 구조적으로 호환되면 MySQL dump 복원처럼 그대로 보존하고 진행한다.
///
/// MySQL 전용. 비-MySQL 어댑터는 그대로 통과시킨다(ERROR 3780은 MySQL 고유 증상이며,
/// PostgreSQL은 `information_schema.KEY_COLUMN_USAGE`에 `REFERENCED_TABLE_NAME`을
/// 노출하지 않아 별도 쿼리가 필요하다 — 후속 과제).
///
/// 타겟을 수정하지 않고 오직 조회만 한다. 구조가 달라지는 FK만 어떤 테이블의 어떤
/// 제약이 충돌하는지 명시한 `preflight_surviving_fk` 에러를 반환한다.
pub(crate) fn preflight_surviving_referencing_fks(
    adapter: &mut LiveAdapter,
    target_schema: &str,
    import_schema: &NormalizedSchema,
) -> Result<(), String> {
    let conn = match adapter {
        LiveAdapter::MySql(conn) => conn,
        _ => return Ok(()),
    };
    let rows: Vec<SurvivingFkColumn> = conn
        .exec_map(
            "SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.REFERENCED_TABLE_NAME, \
                    k.ORDINAL_POSITION, k.REFERENCED_COLUMN_NAME, c.COLUMN_TYPE, \
                    c.CHARACTER_SET_NAME, c.COLLATION_NAME \
             FROM information_schema.KEY_COLUMN_USAGE k \
             JOIN information_schema.COLUMNS c \
               ON c.TABLE_SCHEMA = k.REFERENCED_TABLE_SCHEMA \
              AND c.TABLE_NAME = k.REFERENCED_TABLE_NAME \
              AND c.COLUMN_NAME = k.REFERENCED_COLUMN_NAME \
             WHERE k.TABLE_SCHEMA = ? AND k.REFERENCED_TABLE_NAME IS NOT NULL \
             ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION",
            (target_schema,),
            |(referencing_table, constraint_name, referenced_table, ordinal_position,
              referenced_column, existing_parent_column_type,
              existing_parent_character_set, existing_parent_collation):
             (String, String, String, u64, String, String, Option<String>, Option<String>)| {
                SurvivingFkColumn {
                    referencing_table,
                    constraint_name,
                    referenced_table,
                    ordinal_position,
                    referenced_column,
                    existing_parent_column_type,
                    existing_parent_character_set,
                    existing_parent_collation,
                }
            },
        )
        .map_err(|err| format!("mysql surviving-FK preflight inspect error: {err}"))?;

    let offenders = incompatible_surviving_fk_offenders(&rows, import_schema);
    if offenders.is_empty() {
        Ok(())
    } else {
        Err(classified_import_error(
            "preflight_surviving_fk",
            &format!(
                "target-only foreign keys are incompatible with tables being recreated; \
                 align or detach only these constraints before re-importing: {}",
                offenders.join(", ")
            ),
            None,
        ))
    }
}

pub(crate) fn validate_dump_import_manifest_strictness(
    tables: &[DumpTableManifest],
    strict: bool,
) -> Result<Vec<String>, String> {
    let mut warnings = Vec::new();
    for table in tables {
        if table.chunks > 0 && table.chunk_sha256.len() < table.chunks as usize {
            let message = if table.chunk_sha256.is_empty() {
                format!(
                    "table {} has chunks but no chunk_sha256 metadata",
                    table.name
                )
            } else {
                format!(
                    "table {} has {} chunks but only {} chunk_sha256 entries",
                    table.name,
                    table.chunks,
                    table.chunk_sha256.len()
                )
            };
            if strict {
                return Err(classified_import_error(
                    "export_invalid",
                    &format!("missing chunk_sha256; {message}"),
                    Some(&table.name),
                ));
            }
            warnings.push(format!("legacy dump: {message}"));
        }
    }
    Ok(warnings)
}

pub(crate) fn verify_imported_row_counts(
    tables: &[DumpTableManifest],
    imported_rows_by_table: &BTreeMap<String, u64>,
) -> Result<(), String> {
    for table in tables {
        let imported = imported_rows_by_table
            .get(&table.name)
            .copied()
            .unwrap_or(0);
        if imported != table.rows {
            return Err(classified_import_error(
                "post_load_validation_failed",
                &format!("expected {} rows, imported {}", table.rows, imported),
                Some(&table.name),
            ));
        }
    }
    Ok(())
}

pub(crate) fn validate_foreign_key_column_compatibility(schema: &NormalizedSchema) -> Result<(), String> {
    for table in &schema.tables {
        for fk in &table.foreign_keys {
            for (column_name, referenced_column_name) in
                fk.columns.iter().zip(fk.referenced_columns.iter())
            {
                let Some(column) = find_schema_column(schema, &table.name, column_name) else {
                    continue;
                };
                let Some(referenced_column) =
                    find_schema_column(schema, &fk.referenced_table, referenced_column_name)
                else {
                    continue;
                };

                let column_fidelity = mysql_character_fidelity(&column.type_name);
                let referenced_fidelity = mysql_character_fidelity(&referenced_column.type_name);

                if let (Some(charset), Some(referenced_charset)) = (
                    column_fidelity.character_set.as_deref(),
                    referenced_fidelity.character_set.as_deref(),
                ) {
                    if !charset.eq_ignore_ascii_case(referenced_charset) {
                        return Err(foreign_key_fidelity_error(
                            fk,
                            column_name,
                            referenced_column_name,
                            "character set",
                            charset,
                            referenced_charset,
                        ));
                    }
                }

                if let (Some(collation), Some(referenced_collation)) = (
                    column_fidelity.collation.as_deref(),
                    referenced_fidelity.collation.as_deref(),
                ) {
                    if !collation.eq_ignore_ascii_case(referenced_collation) {
                        return Err(foreign_key_fidelity_error(
                            fk,
                            column_name,
                            referenced_column_name,
                            "collation",
                            collation,
                            referenced_collation,
                        ));
                    }
                }
            }
        }
    }
    Ok(())
}

fn find_schema_column<'a>(
    schema: &'a NormalizedSchema,
    table_name: &str,
    column_name: &str,
) -> Option<&'a NormalizedColumn> {
    schema
        .tables
        .iter()
        .find(|table| table.name == table_name)
        .and_then(|table| {
            table
                .columns
                .iter()
                .find(|column| column.name == column_name)
        })
}

fn foreign_key_fidelity_error(
    fk: &NormalizedForeignKey,
    column_name: &str,
    referenced_column_name: &str,
    property: &str,
    value: &str,
    referenced_value: &str,
) -> String {
    classified_import_error(
        "post_load_validation_failed",
        &format!(
            "foreign key column {column_name} {property} {value} is incompatible with referenced column {referenced_column_name} {property} {referenced_value}"
        ),
        Some(&fk.name),
    )
}

pub(crate) fn dump_import_report_path(input_path: &Path) -> Result<PathBuf, String> {
    if input_path.as_os_str().is_empty() {
        return Err("cannot write import report without input_dir".to_string());
    }
    Ok(input_path.join("_tunnelforge_import_report.json"))
}

pub(crate) fn write_dump_import_report(input_path: &Path, report: &Value) -> Result<(), String> {
    let report_path = dump_import_report_path(input_path)?;
    let bytes = serde_json::to_vec_pretty(report)
        .map_err(|err| format!("cannot serialize import report: {err}"))?;
    fs::write(&report_path, bytes).map_err(|err| {
        format!(
            "cannot write import report {}: {err}",
            report_path.display()
        )
    })
}

pub(crate) fn validate_dump_manifest_chunks(
    input_path: &Path,
    tables: &[DumpTableManifest],
    data_format: &str,
    compression: &str,
) -> Result<(), String> {
    for table in tables {
        for chunk_index in 1..=table.chunks {
            let chunk_name = dump_chunk_name(chunk_index, data_format, compression);
            let chunk_path = dump_manifest_chunk_path(
                input_path,
                &table.path,
                chunk_index,
                data_format,
                compression,
            )?;
            if let Some(expected) = table.chunk_sha256.get(&chunk_name) {
                let actual = sha256_file(&chunk_path)?;
                if !expected.eq_ignore_ascii_case(&actual) {
                    return Err(format!(
                        "dump chunk checksum mismatch: {} expected {} got {}",
                        chunk_path.display(),
                        expected,
                        actual
                    ));
                }
            }
        }
    }
    Ok(())
}

pub(crate) fn dump_chunk_name(index: u64, data_format: &str, compression: &str) -> String {
    let extension = if data_format == "tsv" { "tsv" } else { "jsonl" };
    if compression == "zstd" {
        format!("chunk_{index:06}.{extension}.zst")
    } else {
        format!("chunk_{index:06}.{extension}")
    }
}

pub(crate) fn open_dump_writer(path: &Path, compression: &str) -> Result<Box<dyn Write>, String> {
    let file = File::create(path).map_err(|err| format!("failed to create dump chunk: {err}"))?;
    let writer = BufWriter::new(file);
    match compression {
        "none" => Ok(Box::new(writer)),
        "zstd" => zstd::stream::write::Encoder::new(writer, MYSQL_DUMP_ZSTD_LEVEL)
            .map(|encoder| Box::new(encoder.auto_finish()) as Box<dyn Write>)
            .map_err(|err| format!("failed to create zstd dump encoder: {err}")),
        other => Err(format!("unsupported dump compression: {other}")),
    }
}

pub(crate) fn open_dump_reader(path: &Path, compression: &str) -> Result<Box<dyn BufRead>, String> {
    let file = File::open(path).map_err(|err| format!("failed to open dump chunk: {err}"))?;
    match compression {
        "none" => Ok(Box::new(BufReader::new(file))),
        "zstd" => zstd::stream::read::Decoder::new(file)
            .map(|decoder| Box::new(BufReader::new(decoder)) as Box<dyn BufRead>)
            .map_err(|err| format!("failed to create zstd dump decoder: {err}")),
        other => Err(format!("unsupported dump compression: {other}")),
    }
}

pub(crate) fn write_dump_rows(
    path: &Path,
    table: &NormalizedTable,
    rows: &[Value],
    data_format: &str,
    compression: &str,
) -> Result<String, String> {
    if data_format == "tsv" {
        write_tsv_rows(path, table, rows, compression)
    } else {
        write_jsonl_rows(path, rows, compression)
    }
}

pub(crate) fn write_dump_row<W: Write>(
    writer: &mut W,
    table: &NormalizedTable,
    row: &Value,
    data_format: &str,
) -> Result<(), String> {
    if data_format == "tsv" {
        write_tsv_row(writer, table, row)
    } else {
        serde_json::to_writer(&mut *writer, row)
            .map_err(|err| format!("failed to encode dump row: {err}"))?;
        writer
            .write_all(b"\n")
            .map_err(|err| format!("failed to write dump row: {err}"))
    }
}

pub(crate) fn read_dump_rows(
    path: &Path,
    table: &NormalizedTable,
    data_format: &str,
    compression: &str,
) -> Result<Vec<Value>, String> {
    if data_format == "tsv" {
        read_tsv_rows(path, table, compression)
    } else {
        read_jsonl_rows(path, compression)
    }
}

fn write_jsonl_rows(path: &Path, rows: &[Value], compression: &str) -> Result<String, String> {
    {
        let mut file = open_dump_writer(path, compression)?;
        for row in rows {
            serde_json::to_writer(&mut file, row)
                .map_err(|err| format!("failed to encode dump row: {err}"))?;
            file.write_all(b"\n")
                .map_err(|err| format!("failed to write dump row: {err}"))?;
        }
    }
    sha256_file(path)
}

fn write_tsv_rows(
    path: &Path,
    table: &NormalizedTable,
    rows: &[Value],
    compression: &str,
) -> Result<String, String> {
    {
        let mut file = open_dump_writer(path, compression)?;
        for row in rows {
            write_tsv_row(&mut file, table, row)?;
        }
    }
    sha256_file(path)
}

pub(crate) fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|err| format!("failed to open dump chunk: {err}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|err| format!("failed to read dump chunk: {err}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn write_tsv_row<W: Write>(
    writer: &mut W,
    table: &NormalizedTable,
    row: &Value,
) -> Result<(), String> {
    let object = row.as_object();
    for (index, column) in table.columns.iter().enumerate() {
        if index > 0 {
            writer
                .write_all(b"\t")
                .map_err(|err| format!("failed to write dump row: {err}"))?;
        }
        let value = object
            .and_then(|object| object.get(&column.name))
            .unwrap_or(&Value::Null);
        let field = tsv_field(value);
        writer
            .write_all(field.as_bytes())
            .map_err(|err| format!("failed to write dump row: {err}"))?;
    }
    writer
        .write_all(b"\n")
        .map_err(|err| format!("failed to write dump row: {err}"))
}

fn tsv_field(value: &Value) -> String {
    if value.is_null() {
        return "\\N".to_string();
    }
    let text = match value {
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => value.clone(),
        Value::Array(_) | Value::Object(_) => value.to_string(),
        Value::Null => unreachable!(),
    };
    escape_tsv_text(&text)
}

pub(crate) fn escape_tsv_text(text: &str) -> String {
    text.replace('\\', "\\\\")
        .replace('\t', "\\t")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
}

fn read_tsv_rows(
    path: &Path,
    table: &NormalizedTable,
    compression: &str,
) -> Result<Vec<Value>, String> {
    let reader = open_dump_reader(path, compression)?;
    let mut rows = Vec::new();
    for line in reader.lines() {
        let line = line.map_err(|err| format!("failed to read dump row: {err}"))?;
        if line.is_empty() {
            continue;
        }
        rows.push(tsv_line_to_row(&line, table));
    }
    Ok(rows)
}

pub(crate) fn stream_tsv_rows_in_batches<F: FnMut(&[Value]) -> Result<(), String>>(
    path: &Path,
    table: &NormalizedTable,
    compression: &str,
    max_rows: usize,
    max_bytes: usize,
    mut insert_batch: F,
) -> Result<u64, String> {
    let reader = open_dump_reader(path, compression)?;
    let max_rows = max_rows.max(1);
    let max_bytes = max_bytes.max(1024);
    let mut batch = Vec::new();
    let mut batch_bytes = 0_usize;
    let mut total_rows = 0_u64;

    for line in reader.lines() {
        let line = line.map_err(|err| format!("failed to read dump row: {err}"))?;
        if line.is_empty() {
            continue;
        }
        let row_bytes = line.len() + 1;
        if !batch.is_empty() && (batch.len() >= max_rows || batch_bytes + row_bytes > max_bytes) {
            insert_batch(&batch)?;
            total_rows += batch.len() as u64;
            batch.clear();
            batch_bytes = 0;
        }
        batch.push(tsv_line_to_row(&line, table));
        batch_bytes += row_bytes;
    }

    if !batch.is_empty() {
        insert_batch(&batch)?;
        total_rows += batch.len() as u64;
    }

    Ok(total_rows)
}

fn tsv_line_to_row(line: &str, table: &NormalizedTable) -> Value {
    let columns = column_names(table);
    let fields = split_tsv_line(line);
    let mut object = Map::new();
    for (index, column) in columns.iter().enumerate() {
        let value = fields
            .get(index)
            .map(|field| unescape_tsv_field(field))
            .unwrap_or(Value::Null);
        object.insert(column.clone(), value);
    }
    Value::Object(object)
}

fn split_tsv_line(line: &str) -> Vec<String> {
    line.split('\t').map(ToString::to_string).collect()
}

fn unescape_tsv_field(field: &str) -> Value {
    if field == "\\N" {
        return Value::Null;
    }
    let mut output = String::new();
    let mut chars = field.chars();
    while let Some(ch) = chars.next() {
        if ch != '\\' {
            output.push(ch);
            continue;
        }
        match chars.next() {
            Some('t') => output.push('\t'),
            Some('n') => output.push('\n'),
            Some('r') => output.push('\r'),
            Some('\\') => output.push('\\'),
            Some(other) => {
                output.push('\\');
                output.push(other);
            }
            None => output.push('\\'),
        }
    }
    Value::String(output)
}

fn read_jsonl_rows(path: &Path, compression: &str) -> Result<Vec<Value>, String> {
    let reader = open_dump_reader(path, compression)?;
    let mut rows = Vec::new();
    for line in reader.lines() {
        let line = line.map_err(|err| format!("failed to read dump row: {err}"))?;
        if line.trim().is_empty() {
            continue;
        }
        rows.push(
            serde_json::from_str(&line)
                .map_err(|err| format!("failed to parse dump row: {err}"))?,
        );
    }
    Ok(rows)
}


#[cfg(test)]
mod tests {
    use super::*;
    
    
    use serde_json::{json, Value};
    use std::collections::BTreeMap;
    use std::fs::{self};
    
    use std::path::Path;
    
    
    
    
    
    use crate::adapters::test_support::{empty_table, fk, schema};

    #[test]
    fn dump_manifest_and_jsonl_rows_roundtrip() {
        let dir =
            std::env::temp_dir().join(format!("tunnelforge-dump-test-{}", current_unix_seconds()));
        fs::create_dir_all(&dir).unwrap();

        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 1,
            data_format: "jsonl".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "0001_users".to_string(),
                rows: 2,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
        };
        write_dump_manifest(&dir, &manifest).unwrap();
        assert_eq!(read_dump_manifest(&dir).unwrap(), manifest);

        let table_dir = dir.join("0001_users");
        fs::create_dir_all(&table_dir).unwrap();
        let rows = vec![json!({"id": 1}), json!({"id": 2})];
        let chunk_path = table_dir.join("chunk_000001.jsonl");
        write_jsonl_rows(&chunk_path, &rows, "none").unwrap();
        assert_eq!(read_jsonl_rows(&chunk_path, "none").unwrap(), rows);

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_manifest_rejects_table_paths_outside_dump_dir() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-traversal-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();

        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "zstd".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "../outside".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
        };
        write_dump_manifest(&dir, &manifest).unwrap();

        let err = read_dump_manifest(&dir).unwrap_err();

        assert!(err.contains("unsafe dump table path"));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_manifest_validation_rejects_symlinked_chunk_outside_dump_dir() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-symlink-test-{}",
            current_unix_seconds()
        ));
        let outside = std::env::temp_dir().join(format!(
            "tunnelforge-dump-symlink-outside-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("chunk_000001.tsv"), b"1\toutside\n").unwrap();
        let link_dir = dir.join("0001_users");
        #[cfg(windows)]
        let link_result = std::os::windows::fs::symlink_dir(&outside, &link_dir);
        #[cfg(unix)]
        let link_result = std::os::unix::fs::symlink(&outside, &link_dir);
        if link_result.is_err() {
            fs::remove_dir_all(&dir).ok();
            fs::remove_dir_all(&outside).ok();
            return;
        }
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "0001_users".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
        };

        let err = validate_dump_manifest_chunks(&dir, &manifest.tables, "tsv", "none").unwrap_err();

        assert!(err.contains("outside dump directory"));
        fs::remove_dir_all(&dir).ok();
        fs::remove_dir_all(&outside).ok();
    }

    #[test]
    fn dump_manifest_validation_rejects_missing_chunk_before_import() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-missing-chunk-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(dir.join("0001_users")).unwrap();
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "0001_users".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
        };

        let err = validate_dump_manifest_chunks(&dir, &manifest.tables, "tsv", "none").unwrap_err();

        assert!(err.contains("failed to validate dump chunk"));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_manifest_validation_rejects_checksum_mismatch() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-checksum-test-{}",
            current_unix_seconds()
        ));
        let table_dir = dir.join("0001_users");
        fs::create_dir_all(&table_dir).unwrap();
        fs::write(table_dir.join("chunk_000001.tsv"), b"1\tactual\n").unwrap();
        let mut checksums = BTreeMap::new();
        checksums.insert("chunk_000001.tsv".to_string(), "00".repeat(32));
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "0001_users".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: checksums,
            }],
        };

        let err = validate_dump_manifest_chunks(&dir, &manifest.tables, "tsv", "none").unwrap_err();

        assert!(err.contains("checksum mismatch"));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_manifest_validation_accepts_matching_chunk_checksum() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-checksum-ok-test-{}",
            current_unix_seconds()
        ));
        let table_dir = dir.join("0001_users");
        fs::create_dir_all(&table_dir).unwrap();
        let table = schema().tables[0].clone();
        let chunk_path = table_dir.join("chunk_000001.tsv");
        let checksum = write_dump_rows(
            &chunk_path,
            &table,
            &[json!({"id": 1, "name": "actual"})],
            "tsv",
            "none",
        )
        .unwrap();
        let mut checksums = BTreeMap::new();
        checksums.insert("chunk_000001.tsv".to_string(), checksum);
        let manifest = DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 1,
            chunks: 1,
            chunk_sha256: checksums,
        };

        validate_dump_manifest_chunks(&dir, &[manifest], "tsv", "none").unwrap();

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn strict_manifest_validation_rejects_missing_chunk_checksums() {
        let table = DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 10,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        };

        let err = validate_dump_import_manifest_strictness(&[table], true).unwrap_err();

        assert!(err.contains("export_invalid"));
        assert!(err.contains("users"));
        assert!(err.contains("missing chunk_sha256"));
    }

    #[test]
    fn legacy_manifest_validation_allows_missing_checksums_when_not_strict() {
        let table = DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 10,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        };

        let warnings = validate_dump_import_manifest_strictness(&[table], false).unwrap();

        assert_eq!(
            warnings,
            vec!["legacy dump: table users has chunks but no chunk_sha256 metadata".to_string()]
        );
    }

    #[test]
    fn classified_import_error_formats_code_scope_and_message() {
        let err = classified_import_error(
            "import_plan_invalid",
            "full replacement worker target is unresolved",
            Some("users"),
        );

        assert_eq!(
            err,
            "import_plan_invalid: users: full replacement worker target is unresolved"
        );
    }

    #[test]
    fn dump_import_ddl_error_includes_classification_table_and_operation() {
        let err = dump_import_ddl_error("create_table", "users", "mysql create table error");

        assert_eq!(
            err,
            "load_failed: users: create_table failed: mysql create table error"
        );
    }

    #[test]
    fn dump_import_strict_manifest_rejects_missing_checksums_before_connect() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-strict-import-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let table = schema().tables[0].clone();
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 50_000,
            created_unix_seconds: 1,
            tables: vec![DumpTableManifest {
                name: table.name,
                path: "0001_users".to_string(),
                rows: 10,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
            views: Vec::new(),
        };
        write_dump_manifest(&dir, &manifest).unwrap();

        let events = handle_request(Request {
            command: "dump.import".to_string(),
            request_id: Some("strict-import".to_string()),
            payload: json!({
                "input_dir": dir.to_string_lossy(),
                "target": {
                    "engine": "mysql",
                    "host": "127.0.0.1",
                    "port": 1,
                    "user": "root",
                    "password": "",
                    "database": "app"
                }
            }),
        });

        let message = events
            .iter()
            .find(|event| event.get("event") == Some(&json!("error")))
            .and_then(|event| event.get("message"))
            .and_then(Value::as_str)
            .unwrap();
        assert!(message.contains("export_invalid"));
        assert!(message.contains("missing chunk_sha256"));

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn import_row_count_verification_rejects_missing_rows() {
        let tables = vec![DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 3,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        }];
        let mut imported = BTreeMap::new();
        imported.insert("users".to_string(), 2_u64);

        let err = verify_imported_row_counts(&tables, &imported).unwrap_err();

        assert!(err.contains("post_load_validation_failed"));
        assert!(err.contains("users"));
        assert!(err.contains("expected 3 rows, imported 2"));
    }

    #[test]
    fn import_row_count_verification_accepts_matching_counts() {
        let tables = vec![DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 3,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        }];
        let mut imported = BTreeMap::new();
        imported.insert("users".to_string(), 3_u64);

        verify_imported_row_counts(&tables, &imported).unwrap();
    }

    #[test]
    fn imported_row_verification_ignores_extra_target_rows() {
        // 살아있는 타겟에 외부 write로 여분 행이 생겨도, import가 넣은 수가 덤프와
        // 맞으면 통과해야 한다(타겟 재조회 검증을 제거했으므로). login_attempts처럼
        // import 중에도 계속 쌓이는 테이블에서 정확 일치를 요구하면 오탐이 된다.
        let tables = vec![DumpTableManifest {
            name: "login_attempts".to_string(),
            path: "0001_login_attempts".to_string(),
            rows: 87_603,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        }];
        // import가 넣은 수는 덤프와 정확히 일치 → 통과.
        let imported = BTreeMap::from([("login_attempts".to_string(), 87_603_u64)]);
        verify_imported_row_counts(&tables, &imported).unwrap();
    }

    #[test]
    fn fk_schema_fidelity_rejects_incompatible_text_collations() {
        let schema = NormalizedSchema {
            tables: vec![
                NormalizedTable {
                    name: "audit_category".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "code".to_string(),
                        type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                            .to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                },
                NormalizedTable {
                    name: "df_evaluation_results".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "audit_category_code".to_string(),
                        type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                            .to_string(),
                        default_value: None,
                        nullable: true,
                        primary_key: false,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "df_evaluation_results_ibfk_3".to_string(),
                        columns: vec!["audit_category_code".to_string()],
                        referenced_table: "audit_category".to_string(),
                        referenced_columns: vec!["code".to_string()],
                    }],
                    table_collation: None,
                },
            ],
        };

        let err = validate_foreign_key_column_compatibility(&schema).unwrap_err();

        assert!(err.contains("post_load_validation_failed"));
        assert!(err.contains("df_evaluation_results_ibfk_3"));
        assert!(err.contains("audit_category_code"));
        assert!(err.contains("code"));
    }

    #[test]
    fn fk_schema_fidelity_accepts_matching_text_collations() {
        let schema = NormalizedSchema {
            tables: vec![
                NormalizedTable {
                    name: "audit_category".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "code".to_string(),
                        type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                            .to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                },
                NormalizedTable {
                    name: "df_evaluation_results".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "audit_category_code".to_string(),
                        type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                            .to_string(),
                        default_value: None,
                        nullable: true,
                        primary_key: false,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "df_evaluation_results_ibfk_3".to_string(),
                        columns: vec!["audit_category_code".to_string()],
                        referenced_table: "audit_category".to_string(),
                        referenced_columns: vec!["code".to_string()],
                    }],
                    table_collation: None,
                },
            ],
        };

        validate_foreign_key_column_compatibility(&schema).unwrap();
    }

    #[test]
    fn import_report_path_lives_inside_dump_directory() {
        let dir = Path::new("C:/tmp/dump");
        let path = dump_import_report_path(dir).unwrap();

        assert!(path.ends_with("_tunnelforge_import_report.json"));
        assert!(path.starts_with(dir));
    }

    #[test]
    fn write_dump_import_report_creates_json_file() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-import-report-test-{}",
            current_unix_seconds()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();

        write_dump_import_report(
            &dir,
            &json!({
                "success": true,
                "verification": {"row_counts": "passed"}
            }),
        )
        .unwrap();

        let report_path = dir.join("_tunnelforge_import_report.json");
        let report_text = fs::read_to_string(&report_path).unwrap();
        assert!(report_text.contains("\"row_counts\": \"passed\""));

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn write_dump_manifest_writes_overwrite_marker() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-write-marker-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: Vec::new(),
        };

        write_dump_manifest(&dir, &manifest).unwrap();

        assert!(dir.join(".tunnelforge_dump_dir").is_file());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn learned_mysql_range_chunk_size_uses_previous_faster_large_chunks_for_wide_tables() {
        let profile = DumpTablePerfProfile {
            avg_row_bytes: 9_462,
            chunk_rows: 50_000,
            rows_per_second: 1_350,
            duration_ms: 165_900,
        };

        assert_eq!(
            learned_mysql_range_chunk_size(50_000, 9_462, Some(&profile)),
            50_000
        );
    }

    #[test]
    fn tsv_insert_fallback_streams_rows_in_limited_batches() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-tsv-fallback-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let table = schema().tables[0].clone();
        let path = dir.join("chunk_000001.tsv");
        write_dump_rows(
            &path,
            &table,
            &[
                json!({"id": "1", "name": "a"}),
                json!({"id": "2", "name": "b"}),
                json!({"id": "3", "name": "c"}),
            ],
            "tsv",
            "none",
        )
        .unwrap();
        let mut batches = Vec::new();

        let rows = stream_tsv_rows_in_batches(&path, &table, "none", 2, 1024, |batch| {
            batches.push(batch.len());
            Ok(())
        })
        .unwrap();

        assert_eq!(rows, 3);
        assert_eq!(batches, vec![2, 1]);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn tsv_dump_rows_roundtrip_with_nulls_and_escaped_text() {
        let dir =
            std::env::temp_dir().join(format!("tunnelforge-tsv-test-{}", current_unix_seconds()));
        fs::create_dir_all(&dir).unwrap();
        let table = NormalizedTable {
            name: "notes".to_string(),
            columns: vec![
                NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                },
                NormalizedColumn {
                    name: "body".to_string(),
                    type_name: "text".to_string(),
                    default_value: None,
                    nullable: true,
                    primary_key: false,
                    unique: false,
                },
                NormalizedColumn {
                    name: "empty".to_string(),
                    type_name: "varchar(8)".to_string(),
                    default_value: None,
                    nullable: true,
                    primary_key: false,
                    unique: false,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };
        let rows = vec![json!({"id": "1", "body": "a\tb\nc\\d", "empty": null})];
        let path = dir.join("chunk_000001.tsv");

        write_dump_rows(&path, &table, &rows, "tsv", "none").unwrap();
        assert_eq!(read_dump_rows(&path, &table, "tsv", "none").unwrap(), rows);

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn tsv_dump_rows_preserve_enum_value_case() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-tsv-enum-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let table = NormalizedTable {
            name: "df_evaluations_norm".to_string(),
            columns: vec![NormalizedColumn {
                name: "importance".to_string(),
                type_name: "enum('HIGH','MEDIUM','LOW')".to_string(),
                default_value: None,
                nullable: false,
                primary_key: false,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };
        let rows = vec![json!({"importance": "MEDIUM"})];
        let path = dir.join("chunk_000001.tsv");

        write_dump_rows(&path, &table, &rows, "tsv", "none").unwrap();
        assert_eq!(read_dump_rows(&path, &table, "tsv", "none").unwrap(), rows);

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn zstd_tsv_dump_rows_roundtrip_and_uses_compressed_extension() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-zstd-tsv-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let table = NormalizedTable {
            name: "users".to_string(),
            columns: vec![
                NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "bigint".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: true,
                },
                NormalizedColumn {
                    name: "notes".to_string(),
                    type_name: "text".to_string(),
                    default_value: None,
                    nullable: true,
                    primary_key: false,
                    unique: false,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };
        let rows = vec![
            json!({"id": "1", "notes": "hello\tworld"}),
            json!({"id": "2", "notes": "line\nbreak"}),
        ];
        let chunk_name = dump_chunk_name(1, "tsv", "zstd");
        assert_eq!(chunk_name, "chunk_000001.tsv.zst");
        let path = dir.join(chunk_name);

        write_dump_rows(&path, &table, &rows, "tsv", "zstd").unwrap();

        assert_eq!(read_dump_rows(&path, &table, "tsv", "zstd").unwrap(), rows);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_path_components_are_filesystem_safe() {
        assert_eq!(
            safe_dump_component("orders/detail:2026"),
            "orders_detail_2026"
        );
        assert_eq!(safe_dump_component(""), "table");
    }

    #[test]
    fn dump_import_manifest_tables_follow_fk_dependency_order() {
        let schema = NormalizedSchema {
            tables: vec![
                empty_table("orders", vec![fk("fk_orders_users", "users")]),
                empty_table("users", Vec::new()),
            ],
        };
        let tables = vec![
            DumpTableManifest {
                name: "orders".to_string(),
                path: "0001_orders".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            },
            DumpTableManifest {
                name: "users".to_string(),
                path: "0002_users".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            },
        ];

        let ordered = dependency_ordered_dump_tables(&schema, tables);

        assert_eq!(
            ordered
                .iter()
                .map(|table| table.name.as_str())
                .collect::<Vec<_>>(),
            vec!["users", "orders"]
        );
    }

    #[test]
    fn dump_import_replace_drops_children_before_parents() {
        // dependency order는 parent-first(users -> orders)이므로,
        // replace/recreate가 일괄 DROP할 때 쓰는 rev()는 child-first(orders -> users)여야 한다.
        // 자식을 먼저 drop해야 부모를 재생성할 때 살아있는 자식 FK와 충돌(ERROR 3780)하지 않는다.
        let schema = NormalizedSchema {
            tables: vec![
                empty_table("orders", vec![fk("fk_orders_users", "users")]),
                empty_table("users", Vec::new()),
            ],
        };
        let make_manifest = |name: &str, path: &str| DumpTableManifest {
            name: name.to_string(),
            path: path.to_string(),
            rows: 1,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        };
        let tables = vec![
            make_manifest("orders", "0001_orders"),
            make_manifest("users", "0002_users"),
        ];

        let ordered = dependency_ordered_dump_tables(&schema, tables);
        let drop_order: Vec<&str> = ordered
            .iter()
            .rev()
            .map(|table| table.name.as_str())
            .collect();

        assert_eq!(drop_order, vec!["orders", "users"]);
    }

    #[test]
    fn compatible_surviving_fk_is_preserved_for_mysql_like_import() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "supplement_review_item".to_string(),
                columns: vec![NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "bigint unsigned".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: true,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let rows = vec![SurvivingFkColumn {
            referencing_table: "supplement_review_api_call".to_string(),
            constraint_name: "fk_srac_item".to_string(),
            referenced_table: "supplement_review_item".to_string(),
            ordinal_position: 1,
            referenced_column: "id".to_string(),
            existing_parent_column_type: "bigint unsigned".to_string(),
            existing_parent_character_set: None,
            existing_parent_collation: None,
        }];

        assert!(incompatible_surviving_fk_offenders(&rows, &schema).is_empty());
    }

    #[test]
    fn incompatible_surviving_fk_is_rejected_before_parent_recreate() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "supplement_review_item".to_string(),
                columns: vec![NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "bigint unsigned".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: true,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let rows = vec![SurvivingFkColumn {
            referencing_table: "supplement_review_api_call".to_string(),
            constraint_name: "fk_srac_item".to_string(),
            referenced_table: "supplement_review_item".to_string(),
            ordinal_position: 1,
            referenced_column: "id".to_string(),
            existing_parent_column_type: "int unsigned".to_string(),
            existing_parent_character_set: None,
            existing_parent_collation: None,
        }];

        assert_eq!(
            incompatible_surviving_fk_offenders(&rows, &schema),
            vec!["supplement_review_api_call.fk_srac_item -> supplement_review_item (column id type changes from int unsigned to bigint unsigned)".to_string()]
        );
    }

    #[test]
    fn surviving_fk_requires_dump_to_recreate_referenced_index() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "parent".to_string(),
                columns: vec![NormalizedColumn {
                    name: "external_id".to_string(),
                    type_name: "bigint unsigned".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: false,
                    unique: false,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let rows = vec![SurvivingFkColumn {
            referencing_table: "target_only_child".to_string(),
            constraint_name: "fk_child_parent".to_string(),
            referenced_table: "parent".to_string(),
            ordinal_position: 1,
            referenced_column: "external_id".to_string(),
            existing_parent_column_type: "bigint unsigned".to_string(),
            existing_parent_character_set: None,
            existing_parent_collation: None,
        }];

        assert_eq!(
            incompatible_surviving_fk_offenders(&rows, &schema),
            vec!["target_only_child.fk_child_parent -> parent (dump does not recreate a referenced index beginning with external_id)".to_string()]
        );
    }

    #[test]
    fn dependency_order_puts_referenced_parent_tables_first() {
        let schema = NormalizedSchema {
            tables: vec![
                empty_table("line_items", vec![fk("fk_line_items_orders", "orders")]),
                empty_table("orders", vec![fk("fk_orders_users", "users")]),
                empty_table("audit_log", Vec::new()),
                empty_table("users", Vec::new()),
            ],
        };

        assert_eq!(
            table_dependency_order(&schema),
            vec!["audit_log", "users", "orders", "line_items"]
        );
    }

    #[test]
    fn dependency_order_keeps_all_tables_when_cycle_exists() {
        let schema = NormalizedSchema {
            tables: vec![
                empty_table("a", vec![fk("fk_a_b", "b")]),
                empty_table("b", vec![fk("fk_b_a", "a")]),
                empty_table("root", Vec::new()),
            ],
        };

        let (ordered, cyclic) = table_dependency_order_indices(&schema);

        assert_eq!(
            ordered
                .into_iter()
                .map(|index| schema.tables[index].name.as_str())
                .collect::<Vec<_>>(),
            vec!["root", "a", "b"]
        );
        assert_eq!(cyclic, vec!["a", "b"]);
    }

    #[test]
    fn numeric_single_primary_key_is_parallel_dump_eligible() {
        let table = NormalizedTable {
            name: "big_items".to_string(),
            columns: vec![
                NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "bigint unsigned".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                },
                NormalizedColumn {
                    name: "tenant_id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: false,
                    unique: false,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };

        assert_eq!(single_numeric_primary_key(&table), Some("id"));
    }

    #[test]
    fn composite_primary_key_is_not_parallel_range_eligible() {
        let table = NormalizedTable {
            name: "items".to_string(),
            columns: vec![
                NormalizedColumn {
                    name: "tenant_id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                },
                NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };

        assert_eq!(single_numeric_primary_key(&table), None);
    }

    #[test]
    fn large_numeric_pk_table_is_range_dump_candidate() {
        let table = NormalizedTable {
            name: "events".to_string(),
            columns: vec![NormalizedColumn {
                name: "id".to_string(),
                type_name: "bigint".to_string(),
                default_value: None,
                nullable: false,
                primary_key: true,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };

        assert!(should_use_pk_range_dump(&table, 200_000, 50_000));
    }

    #[test]
    fn sparse_numeric_pk_span_falls_back_to_keyset_dump() {
        let table = NormalizedTable {
            name: "sparse_events".to_string(),
            columns: vec![NormalizedColumn {
                name: "id".to_string(),
                type_name: "bigint".to_string(),
                default_value: None,
                nullable: false,
                primary_key: true,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };

        assert!(!should_use_pk_range_dump_for_span(
            &table,
            200_000,
            50_000,
            1,
            10_000_000_000,
        ));
    }

    #[test]
    fn dense_numeric_pk_span_uses_range_dump() {
        let table = NormalizedTable {
            name: "dense_events".to_string(),
            columns: vec![NormalizedColumn {
                name: "id".to_string(),
                type_name: "bigint".to_string(),
                default_value: None,
                nullable: false,
                primary_key: true,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };

        assert!(should_use_pk_range_dump_for_span(
            &table, 200_000, 50_000, 1, 220_000,
        ));
    }

    #[test]
    fn small_numeric_pk_table_stays_whole_table_candidate() {
        let table = NormalizedTable {
            name: "small_events".to_string(),
            columns: vec![NormalizedColumn {
                name: "id".to_string(),
                type_name: "bigint".to_string(),
                default_value: None,
                nullable: false,
                primary_key: true,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };

        assert!(!should_use_pk_range_dump(&table, 10_000, 50_000));
    }

    #[test]
    fn pk_ranges_split_numeric_span_into_contiguous_chunks() {
        let ranges = pk_ranges(1, 100, 100, 25);

        assert_eq!(ranges.len(), 4);
        assert_eq!(ranges[0].chunk_index, 1);
        assert_eq!((ranges[0].start, ranges[0].end), (1, 25));
        assert_eq!((ranges[3].start, ranges[3].end), (76, 100));
    }

    #[test]
    fn mysql_range_chunk_size_uses_byte_target_for_wide_tables() {
        let chunk_size = mysql_range_chunk_size_for_avg_row(50_000, 9_462);

        assert_eq!(chunk_size, 6_764);
        assert!(chunk_size < 50_000);
    }

    #[test]
    fn mysql_range_chunk_size_keeps_row_fallback_for_narrow_or_unknown_tables() {
        assert_eq!(mysql_range_chunk_size_for_avg_row(50_000, 0), 50_000);
        assert_eq!(mysql_range_chunk_size_for_avg_row(50_000, 128), 50_000);
    }

    #[test]
    fn sequential_chunk_size_uses_byte_target_for_very_wide_rows() {
        // 행이 매우 크면(예: 64KB/행) byte_target = 64MB/64KB ≈ 1000행 < hard cap → byte_target 지배.
        let n = sequential_mysql_chunk_size(50_000, 65_536);
        assert_eq!(n, mysql_range_chunk_size_for_avg_row(50_000, 65_536));
        assert!(n < MYSQL_DUMP_SEQUENTIAL_MAX_ROWS_PER_CHUNK);
        assert!(n >= 1);
    }

    #[test]
    fn schema_columns_accept_data_type_alias() {
        let schema = parse_schema(&json!({
            "tables": [{
                "name": "users",
                "columns": [{"name": "id", "data_type": "int", "primary_key": true}]
            }]
        }))
        .unwrap();

        assert_eq!(schema.tables[0].columns[0].type_name, "int");
        assert!(schema.tables[0].columns[0].primary_key);
    }
}
