# SQL workspace recovery (commercial readiness P1-4) - design

Date: 2026-10-01. Status: design only; implementation starts after the SQL editor
work in flight (query control / production read-only policy) has merged, because
every hook below lives in `sql_editor_dialog.py`.

Goal: after a restart or a crash, the user finds the SQL they were writing again:
unsaved text, open tabs (order, titles, file paths), the connection target of the
editor, and cursor positions. Passwords, result data and credentials are never
stored.

## 1. Current structure (surveyed on main = v2.8.0)

- `SQLEditorDialog` (`src/ui/dialogs/sql_editor_dialog.py`, ~2600 lines) is a
  **modal dialog per tunnel profile** (`main_window.py`: `SQLEditorDialog(self,
  tunnel, config_mgr, engine).exec()`). The profile is `tunnel_config['id']`
  (uuid). Nothing in the dialog is persisted today except through the paths below.
- Tabs: `self.editor_tabs` (`QTabWidget`, movable, closable). Each tab is a
  `SQLEditorTab` (`sql_editor_code_editor.py`) with `file_path`, `is_modified`,
  `_tab_index` (title "Query N" when no file), and a `ValidatingCodeEditor`
  (`QPlainTextEdit`). Tabs are created with `_add_new_tab(file_path)`; files are
  read/written as UTF-8 (`load_file` / `save_file`). The first tab is created in
  `_build_editor_panel`.
- Connection target: **one per dialog, not per tab** - `db_combo` (database for
  MySQL, schema for PostgreSQL), `_connected_target`, `_on_schema_changed`. The
  combo is filled asynchronously by `refresh_databases()`. Environment
  (`production` etc.) comes from the profile (`ProductionGuard.get_environment`).
- Existing persistence: `SQLHistory` -> `sql_history.json` (every executed
  statement, permanent, plaintext, same directory as `config.json`); favorites live
  there too. Config and settings go through `ConfigManager` (`config.json`, atomic
  tmp+`os.replace`, `_CONFIG_LOCK`, `get_app_setting` / `set_app_setting`).
  Locations come from `src/core/platform_paths.py` (`app_support_dir()`).
- Closing: `closeEvent` asks "unsaved changes will be lost" when tabs are modified
  or transactions are pending, then drops everything. There is no autosave, no
  crash handling, no `aboutToQuit` hook for the editor (main.py connects
  `prepare_for_shutdown` for the main window only).

Consequence: today a crash, a forced quit or an accidental Discard loses all
unsaved SQL.

## 2. Scope and privacy

Stored per workspace: SQL text of tabs that need it (see 4), file paths, tab
order/titles, active tab, cursor and scroll position, the profile id and the
selected database/schema name, a format version and timestamps.

Never stored: passwords, keys, tokens, connection parameters (host/port/user),
query results, grid edits, pending/uncommitted statements, transaction or
autocommit state, "write unlocked" state of a read-only production session,
history, schema metadata.

The SQL text itself can contain secrets typed by the user (for example
`CREATE USER ... IDENTIFIED BY '...'`). `sql_history.json` already keeps executed
SQL permanently in plaintext, so plaintext drafts do not widen the exposure class,
but they also cover statements that were never executed. Mitigations: file mode
0600 where the OS supports it, a user-visible off switch that also deletes stored
workspaces, no SQL in logs or error reports, and (decision D3) optional encryption
with the existing `ConfigManager` encryptor.

## 3. Storage

- Location: `app_support_dir()/workspaces/<profile_id>.json` (new
  `platform_paths.workspaces_dir()`), one file per profile. One file per profile
  limits the blast radius of corruption, makes "profile deleted" a directory
  listing question, and avoids cross-profile locking. `profile_id` is validated
  (`[A-Za-z0-9_-]{1,96}`) before it is used in a path.
- Write protocol (new `src/core/workspace_store.py`, no Qt): serialize to
  `<name>.tmp.<pid>.<tid>`, flush + `fsync`, `os.replace` over the target. Same
  pattern as `ConfigManager._write_config_atomic_unlocked`. A module lock
  serializes writers; last write wins (only one dialog per profile is open; the app
  already has a single-instance guard).
- Limits: at most 50 tabs and 2 MiB of text per tab, 16 MiB per file. A tab over
  the limit keeps its metadata only and the UI says its draft was not saved
  (never silently truncate SQL).
- Format `version: 1`:

```json
{
  "version": 1,
  "profile_id": "uuid",
  "saved_at": "2026-10-01T09:30:00Z",
  "session": {"state": "open", "app_version": "2.8.1"},
  "target": {"database": "app", "schema": ""},
  "active_tab": 2,
  "tabs": [
    {"id": "t1", "title_index": 3, "file_path": null,
     "text": "SELECT 1;", "dirty": true,
     "cursor": {"position": 9, "anchor": 9, "first_visible_line": 0},
     "file_state": null,
     "target": null},
    {"id": "t2", "title_index": 1, "file_path": "C:/work/report.sql",
     "text": null, "dirty": false,
     "cursor": {"position": 120, "anchor": 120, "first_visible_line": 14},
     "file_state": {"mtime_ns": 1790000000000000000, "size": 2048, "sha256": "..."},
     "target": null}
  ]
}
```

  `session.state` is `open` while an editor is alive and `closed` after a clean
  close; `open` at startup means the previous session ended abnormally. `target`
  at the workspace level is the dialog's database/schema; the per-tab `target`
  field is reserved (null) in case the editor gains per-tab targets (D1).
  Unknown fields are ignored on read and dropped on write; a `version` greater
  than the reader's is treated as unreadable-but-preserved (see 6).

## 4. What is saved per tab

- Untitled tab: `text` always (when non-empty), `dirty` true.
- File-backed, unmodified: path, cursor, `file_state` only - no text, so the file
  on disk stays the single source of truth.
- File-backed, modified: path, draft `text`, cursor, and the `file_state`
  (mtime/size/sha256) of the file as last loaded or saved. On restore the file is
  compared with `file_state`; if it changed on disk the user chooses "restore my
  draft" (tab stays modified) or "reload from disk" (draft kept in a new untitled
  tab, never discarded silently). A missing file restores the draft as modified
  with a "file not found" title marker.
- Empty untitled tabs are not stored; at least one blank tab always exists.

## 5. When to save and restore

Save (UI thread collects a snapshot, which is cheap; the write runs on a single
background worker with coalescing so typing never blocks on `fsync`):

- debounced 2 s after the last text change, and at most every 30 s while dirty;
- immediately on tab add/close/reorder/switch, target change, file open/save;
- on `closeEvent` (final write with `session.state = "closed"`) and on
  `QApplication.aboutToQuit`; the OS-kill case is covered by the periodic write.

Close behaviour (decision D2): with recovery enabled, closing the editor keeps the
drafts and the confirmation dialog changes from "will be lost" to "will be restored
next time" for text, while pending transactions and unsaved cell edits keep the
current loss warning (they are never recoverable). An explicit "Discard" removes
the stored workspace for that profile.

Restore (in the dialog after `init_ui`, before the first user action):

1. Read and validate the file (section 6). On success build tabs in saved order
   with `_add_new_tab`, replacing the initial blank tab; set text without marking
   a clean tab modified (drafts are modified by definition); restore cursor and
   scroll; select the saved active tab.
2. Target: remember `target.database/schema`; after `refresh_databases()` has
   filled `db_combo`, select it if present. If it no longer exists, keep the
   default and show a message; the SQL is still restored.
3. Show a one-line status message ("Recovered N tabs from the previous session"),
   with an "after crash" wording when `session.state` was `open`.
4. Restored text is **never executed automatically**, and restore never connects
   by itself beyond what opening the editor already does.

Production and read-only policy: nothing about write permission is stored.
A restored workspace opens exactly like a new editor on that profile: the
read-only default and its explicit unlock (production profiles) apply unchanged,
and a restored tab never carries an unlocked state.

Deleted profiles: the editor can only be opened for an existing profile, so a
workspace whose profile id no longer exists is an **orphan**. Orphans are not
restored automatically; the main window lists them under "Recovered SQL" (D4) where
each tab can be opened in an editor for a chosen profile (SQL only, no target),
exported to a `.sql` file, or deleted. Orphans older than 90 days are offered for
deletion, never deleted silently.

## 6. Failure handling

- Write failure (disk full, permission): log without SQL, keep the in-memory
  snapshot, retry on the next trigger, show one status-bar warning per session.
  The editor stays fully usable.
- Unreadable file (invalid JSON, wrong types, truncated): rename to
  `<name>.corrupt-<YYYYmmddHHMMSS>` (keep the newest 3), start with a blank
  workspace, show a message naming the backup file. Never delete the evidence.
- Per-field validation: wrong-typed tab entries are skipped individually; a bad
  cursor is clamped to the text; unsupported keys ignored; path strings must be
  absolute and are only used to read the file the user already had open.
- `version` newer than supported: do not restore and do not overwrite; keep the
  file untouched, tell the user the workspace was written by a newer TunnelForge,
  and write new state to a sibling file until the user decides.
- Leftover `*.tmp.*` files from a crash during write are ignored and removed on
  the next start (the target is either the old or the new complete file).
- Recovery must never block the editor from opening: every restore step is wrapped
  so a failure degrades to a blank editor plus a message.

## 7. Settings

- `workspace_recovery_enabled` (default true) and `workspace_recovery_autosave_seconds`
  (default 30, bounds 5-300) in `config.json` settings via `ConfigManager`
  (`get_app_setting` / `set_app_setting`), exposed in the settings dialog with the
  data-scope text from section 2.
- Turning it off stops writing immediately and offers to delete the stored
  workspaces ("Delete now" removes the directory contents).

## 8. Test plan

Unit (no Qt, `tests/test_workspace_store.py`): round trip of the v1 schema;
atomic write (target never partial; failure injection between tmp write and
replace leaves the old file); validation (wrong types, oversize text, bad cursor,
huge tab count, path traversal in `profile_id`); corrupt file is renamed and a
blank state returned; newer `version` is preserved untouched; tmp cleanup;
size limits never truncate silently; no password/result keys can be serialized
(the snapshot builder only accepts the whitelisted fields).

Qt (`tests/test_sql_editor_workspace.py`, offscreen, `QApplication` bound to a
variable, timeouts on every run): capture/restore of tabs, order, active tab,
titles, file-backed unmodified tab (no text stored), modified file-backed tab with
unchanged / changed / missing file, cursor and scroll; autosave debounce with a
fake clock; final save on close with and without Discard; target selection after
a fake `refresh_databases`; missing database keeps SQL; restore never executes SQL
(worker factories asserted not called); read-only/unlocked state absent from the
file and from the restored tabs; disabled setting writes nothing and delete-now
empties the directory; orphan listing; write failure surfaces one warning and
keeps editing; crash simulation = `session.state == "open"` on startup.

Process-level: a script starts the editor against a temporary config directory,
types, kills the process hard, restarts and asserts recovery (Windows and Linux CI
only run it offscreen). Real-DB checks are not needed because no database call is
involved, except one smoke test that opens a restored workspace against the
existing disposable MySQL/PostgreSQL fixtures to confirm the target selection.

Guards: a source test that the snapshot builder cannot reach `config_mgr` credential
APIs, and that log calls in the new modules never include text fields.

## 9. Expected changes

New: `src/core/workspace_store.py` (schema, validation, atomic IO, corruption
handling, orphan listing); `src/ui/dialogs/sql_editor_workspace.py` (a mixin that
owns capture/restore, the debounce timer and the background writer, so the large
dialog only gains a few hook calls); tests above;
`docs/export_import_policy.md` is untouched.

Modified (small hooks): `src/core/platform_paths.py` (`workspaces_dir`);
`src/ui/dialogs/sql_editor_dialog.py` (mixin base, calls in `__init__`,
`_add_new_tab`/`_close_editor_tab`/`_on_schema_changed`/`_on_editor_tab_changed`/
`closeEvent`); `src/ui/dialogs/sql_editor_code_editor.py` (cursor/scroll
accessors on `SQLEditorTab`); `src/ui/main_window.py` ("Recovered SQL" entry,
`aboutToQuit` flush); settings dialog; `src/core/i18n/legacy_translate.py`;
`docs/commercial_readiness_2026-09-28.md` status line.

## 10. Decisions needed

- D1 Per-tab targets: tabs share one connection target today. Keep a
  dialog-level target (proposed) or wait for per-tab targets from the editor work?
  The format reserves `tabs[].target` either way.
- D2 Close semantics: keep drafts on normal close and change the warning text
  (proposed), or keep the loss warning and only recover after crashes.
- D3 Plaintext vs encrypted drafts: plaintext with 0600 (consistent with
  `sql_history.json`, proposed) or encrypt with the `ConfigManager` encryptor
  (protects against casual disclosure, adds key-loss failure modes).
- D4 Orphan workspaces: a "Recovered SQL" list in the main window (proposed) or
  silently delete when the profile is deleted.
- D5 Retention: should unsaved drafts expire (proposed: no expiry for open
  profiles, 90-day prompt for orphans)?
- D6 Sequencing: implementation after the SQL editor changes merge, in two PRs:
  store + tests first (no UI), then the dialog integration and settings.
