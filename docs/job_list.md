# Job list (TF-STATUS-132)

The main window button "Job List" shows every long-running Export, Import, safe promotion and
cross-engine migration run, newest first, with sort, filter (status, kind, text), report opening,
failure reason and record deletion. Storage is `job_history.json` in the app-support directory
(atomic write, newest 500 records, running jobs are never trimmed or deleted).

| Field | Content |
| --- | --- |
| profile id / name, target | connection profile and the database/schema names (migration: `source (engine) -> target (engine)`) |
| kind | `export_full`, `export_tables`, `import`, `promote`, `migration_preflight`, `migration_run`, `migration_resume` |
| mode | the mode actually used (snapshot mode, compression, threads, import mode, "incomplete export") |
| status | `completed`, `partial` (some tables loaded and some failed, or an explicit table-data-only export), `failed`, `cancelled`, `running`, `interrupted` (the app ended while the job ran) |
| error summary, report/log path, small counts | error text with URI credentials, `password=...`, `-p...` masked, max 400 characters |

Never stored: passwords, connection strings, host or user names, data, SQL. An unknown safe-promotion
outcome (`cutover_unknown`) is recorded as failed with an explicit "outcome unknown" message.

Re-open policy: Export re-opens its dialog pre-filled with schema, scope, selected tables, compression
and threads; the run itself passes the Core pre-checks again. Import, safe promotion and migration runs
only open their original dialog without settings (no partial retry from the list). Scheduled backups
are untouched.
