# Saving SQL results to files

The SQL editor result grid offers two different actions (right-click a result table):

| Action | What is saved | Where it runs |
| --- | --- | --- |
| Save displayed results | Only the rows already in the grid (bounded by the editor limit, default 100,000 rows / 256 MiB) | Python, from memory |
| Save full result to file | Every row: the query is **re-run** on its own autocommit connection | Rust core streams straight to the file; no row limit, constant memory, cancellable |

The full-result export **re-runs the query in a server-enforced read-only transaction** on its
own connection and always rolls it back (never commits). It therefore cannot change data: a
query that writes (`DELETE ... RETURNING`, `SELECT nextval(...)`, a stored function that
inserts) is refused with `error_code: "export_requires_read_only"`, no file is created and
nothing is modified. Uncommitted changes of a manual transaction are not visible, and the
result can differ from the grid if data changed since the first run. The editor also disables
the menu entry for queries that do not look like plain reads (a UI hint only; the core is the
guarantee). A session with an open transaction is refused with
`export_session_in_transaction` (MySQL `START TRANSACTION` would otherwise commit it).

## Core protocol

`query.execute` accepts an `output` object (session queries only; runs inside `START TRANSACTION READ ONLY` /
`BEGIN TRANSACTION READ ONLY`, rolled back afterwards):

| Key | Default | Meaning |
| --- | --- | --- |
| `path` | required | Final file |
| `format` | `csv` | `csv` or `jsonl` |
| `bom` | false | UTF-8 BOM for Excel (CSV) |
| `formula_guard` | true | Prefix `'` to cells starting with `=`, `+`, `-`, `@`, TAB, CR unless the text is a plain number (CSV) |
| `binary` | `hex` | `hex` or `base64` for MySQL BLOB/BINARY/BIT/GEOMETRY and PostgreSQL bytea |
| `keep_partial` | false | Keep `<path>.partial` after a cancelled/failed run |
| `overwrite` | false | Replace an existing file (otherwise `output file already exists`) |

While running, `progress` events carry `rows_written` / `bytes_written`. The final `result`
has `output_path`, `rows_written`, `bytes_written`. Data is written to `<path>.partial` and
renamed onto `<path>` only after a complete, synced, error-free run. On cancel, timeout or
error the result has `output_path: null`, the partial file is deleted (or reported as
`partial_path` with `keep_partial`), and no final file exists.

## Value fidelity

- CSV is RFC 4180 (CRLF, quoted fields). NULL is an empty unquoted field; the empty string is
  `""`. JSON Lines uses `null` and `""`.
- File exports keep the server's own text for numbers, decimals, dates/times, JSON and arrays
  (no float rounding, `12345678901234567890.1234567890` stays exact). In JSON Lines these are
  therefore JSON strings; only booleans and NULL use JSON primitives.
- Text is UTF-8. Binary columns are hex/base64 text, never lossy UTF-8.
- JSON Lines was chosen over a JSON array: it streams without buffering, an interrupted file is
  still parseable line by line, and every row is one self-contained object in column order.
- The formula guard is on by default because a query result is untrusted data opened in a
  spreadsheet; numbers such as `-5` are exempt so they are not corrupted.
