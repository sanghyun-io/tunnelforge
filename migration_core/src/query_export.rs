//! Streams a query result to a CSV / JSON Lines file (`query.execute` with an `output` object).
//!
//! Rows are written as they are fetched, never accumulated. The data goes to `<path>.partial`
//! and is renamed onto `<path>` only after a complete, synced write, so a cancelled, timed-out
//! or failed export can never be mistaken for a finished file.

use serde_json::Value;
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::PathBuf;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum FileFormat {
    Csv,
    Jsonl,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum BinaryEncoding {
    Hex,
    Base64,
}

#[derive(Clone, Debug)]
pub(crate) struct OutputSpec {
    pub(crate) path: PathBuf,
    pub(crate) format: FileFormat,
    /// UTF-8 BOM so Excel detects the encoding (CSV only).
    pub(crate) bom: bool,
    /// Prefix cells that a spreadsheet would evaluate as formulas with `'` (CSV only).
    pub(crate) formula_guard: bool,
    pub(crate) binary: BinaryEncoding,
    /// Keep `<path>.partial` after a failed/cancelled export instead of deleting it.
    pub(crate) keep_partial: bool,
    pub(crate) overwrite: bool,
}

impl OutputSpec {
    /// `Ok(None)` when the request has no `output`.
    pub(crate) fn from_payload(payload: &Value) -> Result<Option<Self>, String> {
        let Some(output) = payload.get("output").filter(|v| !v.is_null()) else {
            return Ok(None);
        };
        let text = |key: &str| output.get(key).and_then(Value::as_str);
        let flag = |key: &str, default: bool| output.get(key).and_then(Value::as_bool).unwrap_or(default);
        let path = text("path").filter(|p| !p.is_empty()).ok_or("output.path is required")?;
        let format = match text("format").unwrap_or("csv") {
            "csv" => FileFormat::Csv,
            "jsonl" => FileFormat::Jsonl,
            other => return Err(format!("unsupported output.format: {other}")),
        };
        let binary = match text("binary").unwrap_or("hex") {
            "hex" => BinaryEncoding::Hex,
            "base64" => BinaryEncoding::Base64,
            other => return Err(format!("unsupported output.binary: {other}")),
        };
        Ok(Some(Self {
            path: PathBuf::from(path),
            format,
            bom: flag("bom", false),
            formula_guard: flag("formula_guard", true),
            binary,
            keep_partial: flag("keep_partial", false),
            overwrite: flag("overwrite", false),
        }))
    }
}

pub(crate) struct OutputWriter {
    spec: OutputSpec,
    partial: PathBuf,
    out: BufWriter<File>,
    columns: Vec<String>,
    pub(crate) rows: u64,
    pub(crate) bytes: u64,
}

fn partial_path(path: &std::path::Path) -> PathBuf {
    let mut name = path.file_name().map(|n| n.to_os_string()).unwrap_or_default();
    name.push(".partial");
    path.with_file_name(name)
}

impl OutputWriter {
    pub(crate) fn create(spec: &OutputSpec, columns: &[String]) -> Result<Self, String> {
        if !spec.overwrite && spec.path.exists() {
            return Err(format!("output file already exists: {}", spec.path.display()));
        }
        let partial = partial_path(&spec.path);
        let file = File::create(&partial)
            .map_err(|err| format!("cannot create output file {}: {err}", partial.display()))?;
        let mut writer = Self {
            spec: spec.clone(),
            partial,
            out: BufWriter::with_capacity(1 << 16, file),
            columns: columns.to_vec(),
            rows: 0,
            bytes: 0,
        };
        if spec.format == FileFormat::Csv {
            if spec.bom {
                writer.put(b"\xEF\xBB\xBF")?;
            }
            let header: Vec<String> = columns
                .iter()
                .map(|name| csv_field(&Value::String(name.clone()), spec.formula_guard))
                .collect();
            writer.put(format!("{}\r\n", header.join(",")).as_bytes())?;
        }
        Ok(writer)
    }

    fn put(&mut self, bytes: &[u8]) -> Result<(), String> {
        self.out.write_all(bytes).map_err(|err| format!("output write failed: {err}"))?;
        self.bytes += bytes.len() as u64;
        Ok(())
    }

    pub(crate) fn write_row(&mut self, row: &Value) -> Result<(), String> {
        let null = Value::Null;
        let line = match self.spec.format {
            FileFormat::Csv => {
                let cells: Vec<String> = self
                    .columns
                    .iter()
                    .map(|name| csv_field(row.get(name).unwrap_or(&null), self.spec.formula_guard))
                    .collect();
                format!("{}\r\n", cells.join(","))
            }
            FileFormat::Jsonl => {
                // Manual object so keys keep the query's column order (serde_json sorts them).
                let mut line = String::from("{");
                for (index, name) in self.columns.iter().enumerate() {
                    if index > 0 {
                        line.push(',');
                    }
                    line.push_str(&Value::String(name.clone()).to_string());
                    line.push(':');
                    line.push_str(&row.get(name).unwrap_or(&null).to_string());
                }
                line.push_str("}\n");
                line
            }
        };
        self.put(line.as_bytes())?;
        self.rows += 1;
        Ok(())
    }

    /// Flush, sync and move the file into place.
    pub(crate) fn finish(mut self) -> Result<PathBuf, String> {
        self.out.flush().map_err(|err| format!("output flush failed: {err}"))?;
        self.out
            .get_ref()
            .sync_all()
            .map_err(|err| format!("output sync failed: {err}"))?;
        let Self { spec, partial, out, .. } = self;
        drop(out);
        fs::rename(&partial, &spec.path)
            .map_err(|err| format!("cannot move {} into place: {err}", partial.display()))?;
        Ok(spec.path)
    }

    /// Give up: delete the partial file, or keep it (path returned) when requested.
    pub(crate) fn abort(self) -> Option<PathBuf> {
        let Self { spec, partial, out, .. } = self;
        drop(out);
        if spec.keep_partial {
            Some(partial)
        } else {
            let _ = fs::remove_file(&partial);
            None
        }
    }
}

/// True for plain numeric text such as `-12`, `3.5`, `1e-3`; those are never formulas.
fn is_plain_number(text: &str) -> bool {
    text.bytes().any(|b| b.is_ascii_digit())
        && text.bytes().all(|b| b.is_ascii_digit() || matches!(b, b'+' | b'-' | b'.' | b'e' | b'E'))
        && text.parse::<f64>().is_ok()
}

/// RFC 4180 field. NULL is an empty unquoted field; an empty string is `""`.
pub(crate) fn csv_field(value: &Value, formula_guard: bool) -> String {
    let text = match value {
        Value::Null => return String::new(),
        Value::String(text) => text.clone(),
        Value::Bool(flag) => flag.to_string(),
        Value::Number(number) => number.to_string(),
        other => other.to_string(),
    };
    let guarded = formula_guard
        && matches!(text.as_bytes().first(), Some(b'=' | b'+' | b'-' | b'@' | b'\t' | b'\r'))
        && !is_plain_number(&text);
    let text = if guarded { format!("'{text}") } else { text };
    if text.is_empty() || text.contains([',', '"', '\r', '\n']) {
        format!("\"{}\"", text.replace('"', "\"\""))
    } else {
        text
    }
}

pub(crate) fn encode_binary(bytes: &[u8], encoding: BinaryEncoding) -> String {
    match encoding {
        BinaryEncoding::Hex => hex::encode(bytes),
        BinaryEncoding::Base64 => {
            const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
            let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
            for chunk in bytes.chunks(3) {
                let n = (chunk[0] as u32) << 16
                    | (*chunk.get(1).unwrap_or(&0) as u32) << 8
                    | *chunk.get(2).unwrap_or(&0) as u32;
                out.push(TABLE[(n >> 18) as usize & 63] as char);
                out.push(TABLE[(n >> 12) as usize & 63] as char);
                out.push(if chunk.len() > 1 { TABLE[(n >> 6) as usize & 63] as char } else { '=' });
                out.push(if chunk.len() > 2 { TABLE[n as usize & 63] as char } else { '=' });
            }
            out
        }
    }
}

/// PostgreSQL `bytea` text output (`\x...` hex) in the requested encoding.
pub(crate) fn pg_bytea_text(text: &str, encoding: BinaryEncoding) -> String {
    match text.strip_prefix("\\x").and_then(|digits| hex::decode(digits).ok()) {
        Some(bytes) => encode_binary(&bytes, encoding),
        None => text.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn csv_distinguishes_null_from_empty_and_quotes_per_rfc4180() {
        assert_eq!(csv_field(&Value::Null, true), "");
        assert_eq!(csv_field(&json!(""), true), "\"\"");
        assert_eq!(csv_field(&json!("a,b"), true), "\"a,b\"");
        assert_eq!(csv_field(&json!("say \"hi\"\nnow"), true), "\"say \"\"hi\"\"\nnow\"");
        assert_eq!(csv_field(&json!("한글 ✓"), true), "한글 ✓");
        assert_eq!(csv_field(&json!("12345678901234567890.123456789"), true), "12345678901234567890.123456789");
    }

    #[test]
    fn csv_formula_guard_escapes_formulas_but_not_numbers() {
        for formula in ["=1+1", "+SUM(A1)", "-2+3", "@cmd", "\tx"] {
            assert!(csv_field(&json!(formula), true).starts_with('\''), "{formula}");
            assert_eq!(csv_field(&json!(formula), false).trim_start_matches('"'), formula);
        }
        for number in ["-5", "+3", "-0.25", "1e-3"] {
            assert_eq!(csv_field(&json!(number), true), number);
        }
        assert_eq!(csv_field(&json!("-abc"), true), "'-abc");
    }

    /// Same vectors as tests/test_result_export.py: the UI-side writer and the core must agree.
    #[test]
    fn csv_field_matches_shared_vectors() {
        let vectors: Vec<Value> =
            serde_json::from_str(include_str!("../../tests/fixtures/csv_field_vectors.json")).unwrap();
        for vector in vectors {
            let guard = vector["guard"].as_bool().unwrap();
            let expected = vector["expected"].as_str().unwrap();
            assert_eq!(csv_field(&vector["value"], guard), expected, "{vector}");
        }
    }

    #[test]
    fn binary_encodings() {
        assert_eq!(encode_binary(&[0, 255, 16], BinaryEncoding::Hex), "00ff10");
        assert_eq!(encode_binary(b"Man", BinaryEncoding::Base64), "TWFu");
        assert_eq!(encode_binary(b"Ma", BinaryEncoding::Base64), "TWE=");
        assert_eq!(encode_binary(b"M", BinaryEncoding::Base64), "TQ==");
        assert_eq!(pg_bytea_text("\\xdeadbeef", BinaryEncoding::Hex), "deadbeef");
        assert_eq!(pg_bytea_text("\\xdeadbeef", BinaryEncoding::Base64), "3q2+7w==");
    }

    #[test]
    fn writer_uses_partial_file_until_finished() {
        let dir = std::env::temp_dir().join(format!("tf_export_unit_{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let spec = OutputSpec {
            path: dir.join("out.csv"),
            format: FileFormat::Csv,
            bom: true,
            formula_guard: true,
            binary: BinaryEncoding::Hex,
            keep_partial: false,
            overwrite: false,
        };
        let columns = vec!["id".to_string(), "name".to_string()];
        let mut writer = OutputWriter::create(&spec, &columns).unwrap();
        writer.write_row(&json!({"id": "1", "name": null})).unwrap();
        writer.write_row(&json!({"id": "2", "name": ""})).unwrap();
        assert!(!spec.path.exists() && partial_path(&spec.path).exists());
        let path = writer.finish().unwrap();
        let data = fs::read(&path).unwrap();
        assert_eq!(data, b"\xEF\xBB\xBFid,name\r\n1,\r\n2,\"\"\r\n");
        assert!(!partial_path(&path).exists());
        assert!(OutputWriter::create(&spec, &columns).is_err(), "no silent overwrite");

        let jsonl = OutputSpec { path: dir.join("out.jsonl"), format: FileFormat::Jsonl, ..spec.clone() };
        let mut writer = OutputWriter::create(&jsonl, &columns).unwrap();
        writer.write_row(&json!({"name": "x", "id": "1"})).unwrap();
        assert_eq!(writer.abort(), None);
        assert!(!jsonl.path.exists() && !partial_path(&jsonl.path).exists());

        let keep = OutputSpec { keep_partial: true, ..jsonl };
        let writer = OutputWriter::create(&keep, &columns).unwrap();
        assert_eq!(writer.abort(), Some(partial_path(&keep.path)));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn jsonl_keeps_column_order_and_null() {
        let dir = std::env::temp_dir().join(format!("tf_export_unit_j_{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let spec = OutputSpec {
            path: dir.join("o.jsonl"),
            format: FileFormat::Jsonl,
            bom: false,
            formula_guard: true,
            binary: BinaryEncoding::Hex,
            keep_partial: false,
            overwrite: true,
        };
        let columns = vec!["z".to_string(), "a".to_string()];
        let mut writer = OutputWriter::create(&spec, &columns).unwrap();
        writer.write_row(&json!({"a": "", "z": null})).unwrap();
        let path = writer.finish().unwrap();
        assert_eq!(fs::read_to_string(path).unwrap(), "{\"z\":null,\"a\":\"\"}\n");
        fs::remove_dir_all(&dir).unwrap();
    }
}
