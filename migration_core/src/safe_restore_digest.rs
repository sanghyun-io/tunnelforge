//! Streaming, order-independent content verification for a restore candidate.
//! Memory is bounded by one row plus the database driver's receive buffer.
use crate::*;
use mysql::prelude::Queryable;
use postgres::fallible_iterator::FallibleIterator;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Read};
use std::path::Path;

#[derive(Debug, Default, PartialEq, Eq)]
struct RowDigest {
    count: u64,
    sum: [u8; 32],
    xor: [u8; 32],
}

impl RowDigest {
    fn add(&mut self, table: &NormalizedTable, cells: Vec<Option<String>>) -> Result<(), String> {
        if cells.len() != table.columns.len() {
            return Err("safe_restore_content_invalid: row column count differs".into());
        }
        let mut hash = Sha256::new();
        hash.update(b"TunnelForge typed row digest v1\0");
        for (column, cell) in table.columns.iter().zip(cells) {
            match cell {
                None => hash.update([0]),
                Some(text) => {
                    let bytes = canonical_cell(&column.type_name, &text)?;
                    hash.update([1]);
                    hash.update((bytes.len() as u64).to_be_bytes());
                    hash.update(bytes);
                }
            }
        }
        let hash = hash.finalize();
        let mut carry = 0u16;
        for index in (0..32).rev() {
            let total = self.sum[index] as u16 + hash[index] as u16 + carry;
            self.sum[index] = total as u8;
            carry = total >> 8;
            self.xor[index] ^= hash[index];
        }
        self.count = self
            .count
            .checked_add(1)
            .ok_or("safe_restore_content_invalid: row count overflow")?;
        Ok(())
    }

    fn report(&self) -> Value {
        json!({"algorithm": "sha256-multiset-sum-xor-v1", "rows": self.count,
            "sum": hex::encode(self.sum), "xor": hex::encode(self.xor)})
    }
}

// Canonical decimal coefficient and scale, without floating-point conversion.
// This preserves distinctions beyond IEEE-754 precision and avoids exponent
// expansion (a compact 1e1000000 stays compact).
fn canonical_number(text: &str) -> Result<String, String> {
    let text = text.trim();
    if ["nan", "infinity", "+infinity", "-infinity"]
        .iter()
        .any(|value| text.eq_ignore_ascii_case(value))
    {
        return Ok(text.trim_start_matches('+').to_ascii_lowercase());
    }
    let (mantissa, exponent) = match text.split_once(['e', 'E']) {
        Some((mantissa, exponent)) => (
            mantissa,
            exponent
                .parse::<i64>()
                .map_err(|_| "safe_restore_content_invalid: invalid numeric exponent")?,
        ),
        None => (text, 0),
    };
    let negative = mantissa.starts_with('-');
    let unsigned = mantissa.strip_prefix(['-', '+']).unwrap_or(mantissa);
    let (integer, fraction) = unsigned.split_once('.').unwrap_or((unsigned, ""));
    if integer.is_empty() && fraction.is_empty()
        || !integer
            .bytes()
            .chain(fraction.bytes())
            .all(|byte| byte.is_ascii_digit())
    {
        return Err("safe_restore_content_invalid: invalid numeric value".into());
    }
    let combined = format!("{integer}{fraction}");
    let digits = combined.trim_start_matches('0');
    if digits.is_empty() {
        return Ok("0".into());
    }
    let coefficient = digits.trim_end_matches('0');
    let power = exponent
        .checked_sub(fraction.len() as i64)
        .and_then(|value| value.checked_add((digits.len() - coefficient.len()) as i64))
        .ok_or("safe_restore_content_invalid: numeric scale overflow")?;
    Ok(format!(
        "{}{coefficient}e{power}",
        if negative { "-" } else { "" }
    ))
}

fn canonical_cell(type_name: &str, text: &str) -> Result<Vec<u8>, String> {
    let lowered = type_name.trim().to_ascii_lowercase();
    let base = lowered.split(['(', ' ']).next().unwrap_or("");
    if lowered.starts_with("timestamptz")
        || (lowered.starts_with("timestamp") && lowered.contains("with time zone"))
    {
        if let Some(instant) = canonical_timestamp(text) {
            return Ok(instant.into_bytes());
        }
    }
    if is_binary_type(type_name) {
        return hex::decode(text)
            .map_err(|_| "safe_restore_content_invalid: invalid binary hex".into());
    }
    if matches!(base, "bool" | "boolean") {
        return match text.to_ascii_lowercase().as_str() {
            "true" | "t" | "1" => Ok(b"true".to_vec()),
            "false" | "f" | "0" => Ok(b"false".to_vec()),
            _ => Err("safe_restore_content_invalid: invalid boolean".into()),
        };
    }
    if matches!(
        base,
        "tinyint"
            | "smallint"
            | "mediumint"
            | "int"
            | "integer"
            | "bigint"
            | "int2"
            | "int4"
            | "int8"
            | "decimal"
            | "dec"
            | "numeric"
            | "real"
            | "float"
            | "double"
            | "float4"
            | "float8"
    ) {
        return canonical_number(text).map(String::into_bytes);
    }
    // Preserve text/JSON whitespace, case, NULs, trailing spaces and empty cells.
    // A conservative mismatch is preferable to lossy normalization.
    Ok(text.as_bytes().to_vec())
}

// PostgreSQL emits ISO timestamps with numeric offsets. Compare their exact
// instant and fractional seconds so a verification session's timezone does not
// turn a correctly restored TIMESTAMPTZ into a false mismatch. Unrecognized
// representations retain exact text semantics (no lossy or guessed timezone).
fn canonical_timestamp(text: &str) -> Option<String> {
    let (date, time) = text.split_once([' ', 'T'])?;
    let date = date
        .split('-')
        .map(str::parse::<i64>)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    if date.len() != 3 {
        return None;
    }
    let (year, month, day) = (date[0], date[1], date[2]);
    if !(1..=999999).contains(&year) || !(1..=12).contains(&month) {
        return None;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let max_day = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month as usize - 1];
    if !(1..=max_day).contains(&day) {
        return None;
    }
    let (clock, offset) = if let Some(clock) = time.strip_suffix('Z') {
        (clock, 0)
    } else {
        let split = time.rfind(['+', '-'])?;
        let (clock, zone) = time.split_at(split);
        let values = zone[1..]
            .split(':')
            .map(str::parse::<i64>)
            .collect::<Result<Vec<_>, _>>()
            .ok()?;
        if values.is_empty()
            || values.len() > 3
            || values.iter().any(|value| *value < 0)
            || values[0] > 24
            || values.iter().skip(1).any(|value| *value > 59)
        {
            return None;
        }
        let seconds = values[0] * 3600
            + values.get(1).copied().unwrap_or(0) * 60
            + values.get(2).copied().unwrap_or(0);
        (
            clock,
            if zone.starts_with('-') {
                -seconds
            } else {
                seconds
            },
        )
    };
    let clock = clock.split(':').collect::<Vec<_>>();
    if clock.len() != 3 {
        return None;
    }
    let hour: i64 = clock[0].parse().ok()?;
    let minute: i64 = clock[1].parse().ok()?;
    let (second, fraction) = clock[2].split_once('.').unwrap_or((clock[2], ""));
    let second: i64 = second.parse().ok()?;
    if !(0..=23).contains(&hour)
        || !(0..=59).contains(&minute)
        || !(0..=59).contains(&second)
        || !fraction.bytes().all(|value| value.is_ascii_digit())
    {
        return None;
    }
    let adjusted_year = year - i64::from(month <= 2);
    let era = adjusted_year.div_euclid(400);
    let year_of_era = adjusted_year - era * 400;
    let adjusted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * adjusted_month + 2) / 5 + day - 1;
    let days = era * 146097 + year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year
        - 719468;
    Some(format!(
        "@unix:{}.{}",
        days * 86400 + hour * 3600 + minute * 60 + second - offset,
        fraction.trim_end_matches('0')
    ))
}

fn json_cell(value: &Value, type_name: &str) -> Result<Option<String>, String> {
    match value {
        Value::Null => Ok(None),
        Value::String(value) => Ok(Some(value.clone())),
        Value::Number(number) if number.is_f64() => {
            // serde_json's default floating-point parser cannot retain an
            // arbitrary decimal literal exactly. Native dumps quote projected
            // values, so fail closed on these external/legacy numeric literals.
            Err("safe_restore_content_invalid: non-integer JSON numbers require quoted exact text for content verification".into())
        }
        Value::Number(number) => Ok(Some(number.to_string())),
        Value::Bool(value) => {
            let lowered = type_name.to_ascii_lowercase();
            let base = lowered.split(['(', ' ']).next().unwrap_or("");
            if matches!(base, "tinyint" | "smallint" | "mediumint" | "int" | "integer" | "bigint" | "int2" | "int4" | "int8" | "decimal" | "dec" | "numeric" | "real" | "float" | "double" | "float4" | "float8") {
                Ok(Some(if *value { "1" } else { "0" }.into()))
            } else { Ok(Some(value.to_string())) }
        }
        _ => Err("safe_restore_content_invalid: structured JSON cells require quoted exact text for content verification".into()),
    }
}

fn unescape_tsv(field: &str) -> Option<String> {
    if field == "\\N" {
        return None;
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
    Some(output)
}

struct CheckedReader<'a> {
    file: std::fs::File,
    checksum: &'a mut Sha256,
}

impl Read for CheckedReader<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        let count = self.file.read(buffer)?;
        self.checksum.update(&buffer[..count]);
        Ok(count)
    }
}

fn checked_reader<'a>(
    path: &Path,
    compression: &str,
    checksum: &'a mut Sha256,
) -> Result<Box<dyn BufRead + 'a>, String> {
    let source = CheckedReader {
        file: std::fs::File::open(path)
            .map_err(|err| format!("safe_restore_content_invalid: {err}"))?,
        checksum,
    };
    match compression {
        "none" => Ok(Box::new(BufReader::new(source))),
        "zstd" => Ok(Box::new(BufReader::new(
            zstd::stream::read::Decoder::new(source)
                .map_err(|err| format!("safe_restore_content_invalid: {err}"))?,
        ))),
        _ => Err("safe_restore_content_invalid: unsupported compression".into()),
    }
}

fn dump_digest(
    table: &NormalizedTable,
    manifest: &DumpTableManifest,
    input_path: &Path,
    format: &str,
    compression: &str,
) -> Result<RowDigest, String> {
    let mut digest = RowDigest::default();
    for index in 1..=manifest.chunks {
        let chunk = dump_chunk_name(index, format, compression);
        let path = input_path.join(&manifest.path).join(&chunk);
        let mut checksum = Sha256::new();
        let mut reader = checked_reader(&path, compression, &mut checksum)?;
        let mut line = String::new();
        loop {
            line.clear();
            if reader
                .read_line(&mut line)
                .map_err(|err| format!("safe_restore_content_invalid: {err}"))?
                == 0
            {
                break;
            }
            if line.ends_with('\n') {
                line.pop();
                if line.ends_with('\r') {
                    line.pop();
                }
            }
            let cells = match format {
                "tsv" => line.split('\t').map(unescape_tsv).collect(),
                "jsonl" => {
                    if line.trim().is_empty() {
                        continue;
                    }
                    let row: Value = serde_json::from_str(&line)
                        .map_err(|err| format!("safe_restore_content_invalid: {err}"))?;
                    let object = row
                        .as_object()
                        .ok_or("safe_restore_content_invalid: JSONL row is not an object")?;
                    table
                        .columns
                        .iter()
                        .map(|column| {
                            json_cell(
                                object.get(&column.name).ok_or_else(|| {
                                    format!(
                                        "safe_restore_content_invalid: missing column {}",
                                        column.name
                                    )
                                })?,
                                &column.type_name,
                            )
                        })
                        .collect::<Result<Vec<_>, String>>()?
                }
                _ => return Err("safe_restore_content_invalid: unsupported dump format".into()),
            };
            digest.add(table, cells)?;
        }
        drop(reader);
        if let Some(expected) = manifest.chunk_sha256.get(&chunk) {
            if !hex::encode(checksum.finalize()).eq_ignore_ascii_case(expected) {
                return Err(format!(
                    "safe_restore_content_mismatch: dump checksum changed for {} chunk {index}",
                    table.name
                ));
            }
        }
    }
    if digest.count != manifest.rows {
        return Err(format!(
            "safe_restore_content_mismatch: dump row count for {} expected {}, found {}",
            table.name, manifest.rows, digest.count
        ));
    }
    Ok(digest)
}

fn mysql_rows_digest(
    conn: &mut mysql::PooledConn,
    table: &NormalizedTable,
    sql: &str,
) -> Result<RowDigest, String> {
    let mut digest = RowDigest::default();
    let rows = conn
        .query_iter(sql)
        .map_err(|err| format!("safe_restore_content_read_failed: {err}"))?;
    for row in rows {
        let row = row.map_err(|err| format!("safe_restore_content_read_failed: {err}"))?;
        let cells = row
            .unwrap()
            .into_iter()
            .map(|value| match value {
                mysql::Value::NULL => Ok(None),
                mysql::Value::Bytes(bytes) => String::from_utf8(bytes).map(Some).map_err(|_| {
                    "safe_restore_content_invalid: invalid UTF-8 projection".to_string()
                }),
                _ => Err("safe_restore_content_invalid: unexpected non-text SQL projection".into()),
            })
            .collect::<Result<Vec<_>, String>>()?;
        digest.add(table, cells)?;
    }
    Ok(digest)
}

/// Hash a text/hex projection on the caller's existing session. In particular,
/// this preserves the caller's WRITE locks during promotion re-verification.
pub(crate) fn mysql_table_content_digest(
    conn: &mut mysql::PooledConn,
    table: &NormalizedTable,
    sql: &str,
) -> Result<Value, String> {
    Ok(mysql_rows_digest(conn, table, sql)?.report())
}

pub(crate) fn verify_table_content(
    endpoint: &Endpoint,
    table: &NormalizedTable,
    manifest: &DumpTableManifest,
    input_path: &Path,
    format: &str,
    compression: &str,
    timezone_sql: Option<&str>,
) -> Result<Value, String> {
    let expected = dump_digest(table, manifest, input_path, format, compression)?;
    let mut adapter = LiveAdapter::connect(endpoint)?;
    if let Some(sql) = timezone_sql {
        adapter.execute_sql(sql)?;
    }
    let sql = format!(
        "SELECT {} FROM {}",
        projected_text_columns_sql(&endpoint.engine, table),
        quote_ident(&endpoint.engine, &table.name)
    );
    let mut actual = RowDigest::default();
    match &mut adapter {
        LiveAdapter::MySql(conn) => {
            actual = mysql_rows_digest(conn, table, &sql)?;
        }
        LiveAdapter::PostgreSql(client) => {
            client
                .batch_execute("SET DateStyle = 'ISO, YMD'")
                .map_err(|err| {
                    format!("safe_restore_content_read_failed: DateStyle setup: {err}")
                })?;
            let mut rows = client
                .query_raw(
                    &sql,
                    std::iter::empty::<&(dyn postgres::types::ToSql + Sync)>(),
                )
                .map_err(|err| format!("safe_restore_content_read_failed: {err}"))?;
            while let Some(row) = rows
                .next()
                .map_err(|err| format!("safe_restore_content_read_failed: {err}"))?
            {
                let cells = (0..table.columns.len())
                    .map(|index| {
                        row.try_get::<_, Option<String>>(index)
                            .map_err(|err| format!("safe_restore_content_read_failed: {err}"))
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                actual.add(table, cells)?;
            }
        }
    }
    if actual != expected {
        return Err(format!("safe_restore_content_mismatch: table {} differs from the dump (expected {}, actual {})", table.name, expected.report(), actual.report()));
    }
    let mut report = actual.report();
    report["verified"] = json!(true);
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table() -> NormalizedTable {
        serde_json::from_value(json!({"name":"digest_fixture", "columns":[
            {"name":"text","type":"text"}, {"name":"number","type":"numeric(30,10)"},
            {"name":"binary","type":"bytea"}, {"name":"flag","type":"boolean"}]}))
        .unwrap()
    }

    #[test]
    fn digest_preserves_exact_text_binary_null_and_duplicate_multiplicity() {
        let table = table();
        let cells = vec![
            Some("A\0B\t\n ".into()),
            Some("9007199254740993.000".into()),
            Some("00ff".into()),
            Some("t".into()),
        ];
        let mut first = RowDigest::default();
        first.add(&table, cells.clone()).unwrap();
        let mut equivalent = RowDigest::default();
        equivalent
            .add(
                &table,
                vec![
                    cells[0].clone(),
                    Some("9.007199254740993e15".into()),
                    Some("00FF".into()),
                    Some("true".into()),
                ],
            )
            .unwrap();
        assert_eq!(first, equivalent);
        for changed in [
            None,
            Some("".into()),
            Some("AB\t\n ".into()),
            Some("A\0B\t\n".into()),
        ] {
            let mut modified = cells.clone();
            modified[0] = changed;
            let mut digest = RowDigest::default();
            digest.add(&table, modified).unwrap();
            assert_ne!(first, digest);
        }
        equivalent.add(&table, cells).unwrap();
        assert_ne!(first, equivalent);
    }

    #[test]
    fn digest_is_order_independent_without_decimal_precision_loss() {
        let table = table();
        let a = vec![
            Some("a".into()),
            Some("12345678901234567890.1234567890".into()),
            None,
            None,
        ];
        let b = vec![
            Some("b".into()),
            Some("12345678901234567890.1234567891".into()),
            None,
            None,
        ];
        let mut first = RowDigest::default();
        first.add(&table, a.clone()).unwrap();
        first.add(&table, b.clone()).unwrap();
        let mut second = RowDigest::default();
        second.add(&table, b).unwrap();
        second.add(&table, a).unwrap();
        assert_eq!(first, second);
        assert_ne!(
            canonical_number("12345678901234567890.1234567890").unwrap(),
            canonical_number("12345678901234567890.1234567891").unwrap()
        );
        assert_eq!(unescape_tsv("\\N"), None);
        assert_eq!(unescape_tsv("\\\\N"), Some("\\N".into()));
        assert_eq!(unescape_tsv("\0"), Some("\0".into()));
        assert!(json_cell(&json!(1.5), "numeric").is_err());
        assert_eq!(
            json_cell(&json!(9007199254740993u64), "numeric").unwrap(),
            Some("9007199254740993".into())
        );
        assert_eq!(
            json_cell(&json!(true), "tinyint(1)").unwrap(),
            Some("1".into())
        );
        assert_eq!(
            canonical_cell("timestamptz", "2026-09-28 01:02:03.123456+00").unwrap(),
            canonical_cell(
                "timestamp(6) with time zone",
                "2026-09-28 10:02:03.123456+09"
            )
            .unwrap()
        );
        assert_ne!(
            canonical_cell("timestamp", "2026-09-28 01:02:03").unwrap(),
            canonical_cell("timestamp", "2026-09-28 10:02:03").unwrap()
        );
        assert_eq!(
            canonical_timestamp("1970-01-01 00:00:00+00"),
            Some("@unix:0.".into())
        );
        assert_eq!(
            canonical_timestamp("2000-02-29 01:00:00+01"),
            canonical_timestamp("2000-02-29 00:00:00+00")
        );
    }

    #[test]
    #[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
    fn live_digest_detects_same_count_tampering_in_all_formats() {
        for (engine, variable, port, user) in [
            ("mysql", "TF_MYSQL_HOST", 3306, "root"),
            ("postgresql", "TF_POSTGRES_HOST", 5432, "postgres"),
        ] {
            let endpoint = Endpoint {
                engine: engine.into(),
                host: std::env::var(variable).unwrap(),
                port,
                user: user.into(),
                password: "tf_local_test".into(),
                database: "tf_test".into(),
                schema: None,
            };
            let name = format!("tf_content_digest_{}", std::process::id());
            let mut writer = LiveAdapter::connect(&endpoint).unwrap();
            let (binary_type, bytes, empty_bytes, message) = if engine == "mysql" {
                ("BLOB", "X'00ff'", "X''", "CONCAT('A',CHAR(0),'B')")
            } else {
                (
                    "BYTEA",
                    "decode('00ff','hex')",
                    "decode('','hex')",
                    "'A\tB'",
                )
            };
            let moment_type = if engine == "mysql" {
                "TIMESTAMP(6)"
            } else {
                "TIMESTAMPTZ(6)"
            };
            let moment = if engine == "mysql" {
                "'2026-09-28 01:02:03.123456'"
            } else {
                "'2026-09-28 01:02:03.123456+00'"
            };
            writer
                .execute_sql(if engine == "mysql" {
                    "SET SESSION time_zone='+00:00'"
                } else {
                    "SET TIME ZONE 'UTC'"
                })
                .unwrap();
            writer.execute_sql(&format!("CREATE TABLE {name} (id INT PRIMARY KEY, message TEXT, amount DECIMAL(30,10), body {binary_type}, flag BOOLEAN, moment {moment_type})")).unwrap();
            writer.execute_sql(&format!("INSERT INTO {name} VALUES (1,{message},12345678901234567890.1234567890,{bytes},true,{moment}),(2,'',NULL,{empty_bytes},false,NULL)")).unwrap();
            for format in ["jsonl", "tsv"] {
                for compression in ["none", "zstd"] {
                    let directory = std::env::temp_dir()
                        .join(format!("{name}_{engine}_{format}_{compression}"));
                    let events = handle_request(Request {
                        command: "dump.run".into(),
                        request_id: None,
                        payload: json!({"endpoint": endpoint, "tables": [name], "output_dir": directory,
                            "mysql_snapshot_mode":"single_connection", "chunk_size":1,
                            "data_format":format,"compression":compression}),
                    });
                    assert!(
                        !events.iter().any(|event| event["event"] == "error"),
                        "{events:?}"
                    );
                    let manifest = read_dump_manifest(&directory).unwrap();
                    let table = &manifest.schema.tables[0];
                    let chunks = &manifest.tables[0];
                    let report = verify_table_content(
                        &endpoint,
                        table,
                        chunks,
                        &directory,
                        format,
                        compression,
                        None,
                    )
                    .unwrap();
                    assert_eq!(report["rows"], 2);
                    if let LiveAdapter::MySql(conn) = &mut writer {
                        conn.query_drop(format!(
                            "LOCK TABLES {} WRITE",
                            quote_ident("mysql", &name)
                        ))
                        .unwrap();
                        let sql = format!(
                            "SELECT {} FROM {}",
                            projected_text_columns_sql("mysql", table),
                            quote_ident("mysql", &name)
                        );
                        let locked_report = mysql_table_content_digest(conn, table, &sql);
                        conn.query_drop("UNLOCK TABLES").unwrap();
                        let mut expected = report.clone();
                        expected.as_object_mut().unwrap().remove("verified");
                        assert_eq!(locked_report.unwrap(), expected);
                    }
                    if engine == "postgresql" {
                        verify_table_content(
                            &endpoint,
                            table,
                            chunks,
                            &directory,
                            format,
                            compression,
                            Some("SET TIME ZONE 'Asia/Seoul'"),
                        )
                        .unwrap();
                    }
                    writer
                        .execute_sql(&format!(
                            "UPDATE {name} SET message='same-count tampered' WHERE id=1"
                        ))
                        .unwrap();
                    let error = verify_table_content(
                        &endpoint,
                        table,
                        chunks,
                        &directory,
                        format,
                        compression,
                        None,
                    )
                    .unwrap_err();
                    assert!(error.contains("safe_restore_content_mismatch"), "{error}");
                    writer
                        .execute_sql(&format!("UPDATE {name} SET message={message} WHERE id=1"))
                        .unwrap();
                    if format == "jsonl" && compression == "none" {
                        use std::io::Write;
                        let chunk = directory.join(&chunks.path).join(dump_chunk_name(
                            1,
                            format,
                            compression,
                        ));
                        std::fs::OpenOptions::new()
                            .append(true)
                            .open(chunk)
                            .unwrap()
                            .write_all(b"\n")
                            .unwrap();
                        let error = verify_table_content(
                            &endpoint,
                            table,
                            chunks,
                            &directory,
                            format,
                            compression,
                            None,
                        )
                        .unwrap_err();
                        assert!(error.contains("checksum changed"), "{error}");
                    }
                    std::fs::remove_dir_all(directory).unwrap();
                }
            }
            writer.execute_sql(&format!("DROP TABLE {name}")).unwrap();
        }
    }

    #[test]
    #[ignore = "requires disposable TF_POSTGRES_HOST tf_test database"]
    fn postgres_datestyle_cannot_mask_a_swapped_month_and_day() {
        let endpoint = Endpoint {
            engine: "postgresql".into(),
            host: std::env::var("TF_POSTGRES_HOST").unwrap(),
            port: 5432,
            user: "postgres".into(),
            password: "tf_local_test".into(),
            database: "tf_test".into(),
            schema: None,
        };
        let name = format!("tf_datestyle_{}", std::process::id());
        let dmy = format!("{name}_dmy");
        let mdy = format!("{name}_mdy");
        let mut admin = LiveAdapter::connect(&endpoint).unwrap();
        admin.execute_sql(&format!("CREATE TABLE {name} (id INT PRIMARY KEY, value DATE DEFAULT DATE '2026-04-03'); CREATE VIEW {name}_view AS SELECT DATE '2026-04-03' AS value; INSERT INTO {name} VALUES (1,DATE '2026-04-03'); CREATE ROLE {dmy} LOGIN PASSWORD 'tf_local_test'; CREATE ROLE {mdy} LOGIN PASSWORD 'tf_local_test'; ALTER ROLE {dmy} SET DateStyle='SQL, DMY'; ALTER ROLE {mdy} SET DateStyle='SQL, MDY'; GRANT SELECT ON {name},{name}_view TO {dmy},{mdy}")).unwrap();
        let source = Endpoint {
            user: dmy.clone(),
            ..endpoint.clone()
        };
        let target = Endpoint {
            user: mdy.clone(),
            ..endpoint
        };
        let inspected = inspect_live(&source).unwrap();
        let default = inspected
            .schema
            .tables
            .iter()
            .find(|table| table.name == name)
            .unwrap()
            .columns[1]
            .default_value
            .as_ref()
            .unwrap();
        assert!(
            default.contains("2026-04-03"),
            "non-ISO inspected default: {default}"
        );
        let views = collect_views(&source).unwrap();
        assert!(views
            .iter()
            .find(|view| view.name == format!("{name}_view"))
            .unwrap()
            .definition
            .contains("2026-04-03"));
        let directory = std::env::temp_dir().join(&name);
        let events = handle_request(Request {
            command: "dump.run".into(),
            request_id: None,
            payload: json!({"endpoint": source, "tables": [name], "output_dir": directory,
                "data_format":"jsonl", "compression":"none"}),
        });
        assert!(
            !events.iter().any(|event| event["event"] == "error"),
            "{events:?}"
        );
        let manifest = read_dump_manifest(&directory).unwrap();
        let table = &manifest.schema.tables[0];
        let chunks = &manifest.tables[0];
        // SQL/DMY April 3 and SQL/MDY March 4 both stringify as 03/04/2026.
        admin
            .execute_sql(&format!("UPDATE {name} SET value=DATE '2026-03-04'"))
            .unwrap();
        let mismatch =
            verify_table_content(&target, table, chunks, &directory, "jsonl", "none", None);
        assert!(
            mismatch.is_err(),
            "DateStyle masked changed data: {mismatch:?}"
        );
        admin
            .execute_sql(&format!("UPDATE {name} SET value=DATE '2026-04-03'"))
            .unwrap();
        verify_table_content(&target, table, chunks, &directory, "jsonl", "none", None).unwrap();
        let text = std::fs::read_to_string(
            directory
                .join(&chunks.path)
                .join(dump_chunk_name(1, "jsonl", "none")),
        )
        .unwrap();
        assert!(text.contains("2026-04-03"), "export is not ISO: {text}");
        admin
            .execute_sql(&format!(
                "DROP VIEW {name}_view; DROP TABLE {name}; DROP ROLE {dmy}; DROP ROLE {mdy}"
            ))
            .unwrap();
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    #[ignore = "requires disposable TF_POSTGRES_HOST tf_test database"]
    fn candidate_view_expression_change_invalidates_verification() {
        candidate_metadata_change(
            "CREATE OR REPLACE VIEW item_view AS SELECT id+100 AS id FROM items",
            "view",
        );
    }

    #[test]
    #[ignore = "requires disposable TF_POSTGRES_HOST tf_test database"]
    fn candidate_extra_check_invalidates_verification() {
        candidate_metadata_change(
            "ALTER TABLE items ADD CONSTRAINT review_positive CHECK(id>0)",
            "check",
        );
    }

    #[test]
    #[ignore = "requires disposable TF_POSTGRES_HOST tf_test database"]
    fn candidate_extra_unique_invalidates_verification() {
        candidate_metadata_change("CREATE UNIQUE INDEX review_unique ON items(id)", "unique");
    }

    fn candidate_metadata_change(mutation: &str, suffix: &str) {
        let endpoint = Endpoint {
            engine: "postgresql".into(),
            host: std::env::var("TF_POSTGRES_HOST").unwrap(),
            port: 5432,
            user: "postgres".into(),
            password: "tf_local_test".into(),
            database: "tf_test".into(),
            schema: None,
        };
        let name = format!("tf_review_view_{}_{suffix}", std::process::id());
        let mut admin = LiveAdapter::connect(&endpoint).unwrap();
        admin.execute_sql(&format!("CREATE SCHEMA {name}; CREATE TABLE {name}.items(id INT PRIMARY KEY); INSERT INTO {name}.items VALUES(1); CREATE VIEW {name}.item_view AS SELECT id FROM {name}.items")).unwrap();
        let original = Endpoint {
            schema: Some(name.clone()),
            ..endpoint
        };
        let directory = std::env::temp_dir().join(&name);
        let exported = handle_request(Request {
            command: "dump.run".into(),
            request_id: None,
            payload: json!({"endpoint":original,"output_dir":directory,"data_format":"jsonl","compression":"none"}),
        });
        assert!(
            !exported.iter().any(|event| event["event"] == "error"),
            "{exported:?}"
        );
        let imported = handle_request(Request {
            command: "dump.import".into(),
            request_id: None,
            payload: json!({"endpoint":original,"input_dir":directory,"mode":"safe"}),
        });
        let result = imported
            .iter()
            .find(|event| event["event"] == "result")
            .unwrap();
        assert_eq!(result["success"], true, "{imported:?}");
        let report_path = dump_import_report_path(&directory).unwrap();
        let plan = super::super::safe_restore::load_verified_plan(&report_path, &original).unwrap();
        let mut candidate = LiveAdapter::connect(&plan.candidate).unwrap();
        candidate.execute_sql(mutation).unwrap();
        let verified = super::super::safe_restore::reverify_candidate(&plan);
        assert!(
            verified.is_err(),
            "changed candidate metadata retained verification ({mutation}): {verified:?}"
        );
        if let LiveAdapter::PostgreSql(mut client) = LiveAdapter::connect(&original).unwrap() {
            assert_eq!(
                client
                    .query_one("SELECT id FROM item_view", &[])
                    .unwrap()
                    .get::<_, i32>(0),
                1,
                "original view/data changed during candidate preparation or verification"
            );
        }
        drop(candidate);
        admin
            .execute_sql(&format!(
                "DROP SCHEMA {} CASCADE; DROP SCHEMA {name} CASCADE",
                quote_ident("postgresql", &endpoint_schema(&plan.candidate))
            ))
            .unwrap();
        std::fs::remove_dir_all(directory).unwrap();
    }
}
