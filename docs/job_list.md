# Job list (TF-STATUS-132)

The main window button "Job List" shows every long-running Export, Import, safe promotion and
cross-engine migration run, newest first, with sort, filter (status, kind, text), report opening,
failure reason and record deletion. Storage is `job_history.json` in the app-support directory
(atomic write, newest 500 records, running jobs are never trimmed or deleted).

| Field | Content |
| --- | --- |
| profile id / name, target | connection profile and the database/schema names (migration: `source (engine) -> target (engine)`) |
| kind | `export_full`, `export_tables`, `import`, `promote`, `migration_preflight`, `migration_run`, `migration_resume` |
| mode | the mode that ran (compression, threads, "incomplete export"); a finished Export shows the method sentence from the Core result (e.g. `같은 시점으로 내보냄 · 동시 작업 8개`), and when the incomplete choice re-ran it, the mode of that last run. Import: `Import · 덤프 테이블 교체`, set from the result: ` · 복사본 검증` as soon as a copy was made, also on the running record (only these records, and older "안전 복원"/"덮어쓰기" ones, open backup management; without a saved profile or saved credentials it asks for the DB account), ` · 새 DB 생성` (PostgreSQL: ` · 새 스키마 생성`) when the run created the database, ` · 대상 {name}` for another name |
| status | `completed`, `partial` (some tables loaded and some failed, or an explicit incomplete export that left out the listed objects), `failed`, `cancelled` (including an Export the user chose not to run without the objects the dump cannot keep), `running`, `interrupted` (the app ended while the job ran) |
| error summary, report/log path, small counts | error text with URI credentials, `password=...`, `-p...` masked, max 400 characters; an Export the Core refused keeps the dialog's sentences (`담을 수 없는 객체가 있어 Export를 시작하지 않았습니다. 테이블 ...`), not the Core's text |

Never stored: passwords, connection strings, host or user names, data, SQL. An unknown safe-promotion
outcome (`cutover_unknown`) is recorded as failed with an explicit "outcome unknown" message.

Re-open policy: Export re-opens its dialog pre-filled with schema, the checked tables (every table for a
full export; for a table export only those the user checked, not the FK parents the dialog added),
compression and threads; a recorded snapshot mode or FK-parent choice is never reused (the dialog's
current Advanced settings apply and the core picks the method again), and the run itself passes the
Core pre-checks again. Import, safe promotion and migration runs
only open their original dialog without settings (no partial retry from the list). Scheduled backups
are untouched.
