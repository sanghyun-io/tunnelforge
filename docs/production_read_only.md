# Production read-only sessions and change review

Policy (TF-STATUS-128): a profile with `environment == production` always opens its
interactive SQL editor sessions read-only, including existing profiles after the update.
Writing needs an explicit, per-window unlock; Export/Import/safe promotion/migration keep the
existing ProductionGuard confirmations and are not affected.

## Behaviour

- `DbEndpoint.read_only` (payload key `read_only`, sent only when true) makes `connection.open`
  switch the session to read-only: MySQL `SET SESSION TRANSACTION READ ONLY`, PostgreSQL
  `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`.
- Unlock: the banner button opens `SchemaConfirmDialog` (type the database/schema name). The
  window's session is then reopened writable and the banner turns red. Closing the window or
  changing DB/schema locks it again; locking back needs no uncommitted changes.
- Result grids of a locked window are not editable.
- Banner: target (host / database), environment, read-only or unlocked, uncommitted changes.
- Manual transactions: before a commit the executed write statements and the affected row
  total are listed; a MySQL DDL (implicit commit) during a transaction warns which earlier
  changes will be committed automatically.

## Scope and design notes

- Windows covered: the SQL editor (all its connectors, workers and the grid) and the SQL file
  execution dialog. Both use the same banner, per-window unlock and re-lock rules. Tunnel health
  checks (`SELECT 1`) are not affected.
- Contract deviation: `read_only` is read from the request's endpoint dict (`{"connection":
  {..., "read_only": true}}`), not from a new Rust `Endpoint` field, so the many `Endpoint`
  literals owned by other areas stay untouched. One-off endpoint queries (`query.execute` with a
  `connection` instead of a `connection_id`) honour it exactly like sessions.
- No reconnect exists to lose the setting: a core session holds one connection and the core never
  re-creates it (a killed session answers with errors); the Python shim does not reconnect
  (`ping(reconnect=False)`); the editor's own reconnect goes through `_create_db_connector`, which
  passes the current policy. A new `connection.open` re-applies `SET SESSION ... READ ONLY`. The
  cancel/KILL connection runs no user SQL. Covered by a live test that kills a read-only session.

## What stops what (verified on MySQL 8.0/8.4, PostgreSQL 13/18)

Errors carry `error_code: "read_only_session"` whichever layer refused.

| | Server refuses (read-only mode) | Not stopped by the server, refused by the core |
| --- | --- | --- |
| MySQL | INSERT/UPDATE/DELETE/REPLACE, TRUNCATE, CREATE/ALTER/DROP TABLE, CREATE INDEX, RENAME, CREATE DATABASE/VIEW, CREATE TEMPORARY TABLE, OPTIMIZE, CALL/functions that write, CREATE USER, GRANT | `SET SESSION TRANSACTION READ WRITE`, `START TRANSACTION READ WRITE`, `SET tx_read_only=0`, multi-statement bypass (`SELECT 1; SET ...`), `CALL` (a procedure can switch the mode off itself - verified), `LOCK TABLES`, `SET GLOBAL/PERSIST`, `SELECT ... INTO OUTFILE/DUMPFILE` |
| PostgreSQL | INSERT/UPDATE/DELETE, TRUNCATE, CREATE/ALTER/DROP, COMMENT, CREATE ROLE/DATABASE/TEMP TABLE, nextval/setval, DO/functions that write | `SET TRANSACTION READ WRITE`, `SET SESSION CHARACTERISTICS ... READ WRITE`, `SET/RESET default_transaction_read_only`, `set_config(...)`, `BEGIN READ WRITE`, `RESET ALL`, `DISCARD ALL`, multi-statement bypass, `CALL`, `lo_create`/other large-object writers (a data write the server allows), `ALTER SYSTEM`, `VACUUM`/`REINDEX`/`CLUSTER`, `COPY ... PROGRAM` |

Still allowed in a read-only session: SELECT, EXPLAIN, SHOW, ANALYZE, NOTIFY, advisory locks.

The core check is defence in depth against accidental or plain bypasses. It does not stop
deliberately obfuscated dynamic SQL; separation of duties is a database account without write
privileges. Opt-in live test: `migration_core/tests/live_read_only_session.rs`
(`TF_QUERY_LIVE_MYSQL_*` / `TF_QUERY_LIVE_PG_*`).
