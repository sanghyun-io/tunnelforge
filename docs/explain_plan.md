# Execution plan view (TF-STATUS-133)

Core command `query.explain` (session `connection_id`, or a one-off endpoint payload) returns the engine's plan for one statement.

| Mode | MySQL | PostgreSQL |
| --- | --- | --- |
| default (nothing executed) | `EXPLAIN FORMAT=JSON` | `EXPLAIN (FORMAT JSON)` |
| `"analyze": true` (really executes) | `EXPLAIN ANALYZE` (tree text; MySQL has no JSON form) | `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` |

- The result is `command: "query.explain"` with `explain: {engine, analyze, format}` and the plan in `rows`. The raw document is kept; `src/core/explain_plan.py` parses it into a tree.
- `analyze` must be requested explicitly and is refused (`error_code: explain_refused`) unless the statement is a plain read-only `SELECT`/`WITH`/`VALUES`: no data change, DDL, `SELECT ... INTO`, locking read (`FOR UPDATE/SHARE`) or known side-effect function (`nextval`, `set_config`, advisory locks, ...). The check is word based, so it can refuse harmless text such as a string literal containing `update`.
- The word filter is only a first line of defence (it cannot see a stored/plpgsql function that writes). The real guard is the server: with `analyze` the statement runs inside `BEGIN TRANSACTION READ ONLY` / `START TRANSACTION READ ONLY` that is **always rolled back**, so a writing function is rejected by the database (`explain_refused`, nothing changed). A session with an open transaction is refused for the same reason (its transaction is left untouched, neither committed nor rolled back).
- Plain `EXPLAIN` of `INSERT/UPDATE/DELETE` is allowed and runs nothing.
- It goes through the normal query path: sessions, `query.cancel`, `timeout_ms`, read-only sessions and TLS behave as for `query.execute`.
- The dialog (`src/ui/dialogs/explain_plan_dialog.py`) shows the tree (cost/rows estimate, actual rows/time with ANALYZE; sequential or full scans, filesort, temporary tables, disk sort/hash highlighted) and a raw tab. It states facts only; it gives no index advice.
- Live check: `tests/test_explain_plan_live.py` (MySQL 8.0/8.4, PostgreSQL 13/18; opt-in `TF_EXPLAIN_LIVE=1`).
