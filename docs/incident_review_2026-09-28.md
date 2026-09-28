# 2.5.0 / 2.5.1 Import incident follow-up

The user supplied an incident timeline and two failure logs. This document maps
the reported limitations to the current worktree; it does not claim that the
user's live Staging database has been repaired. Reported historical baseline
counts are not independently verified counts of the current PROD schema.

## Confirmed reproduction and data verification

- The first failure was on 2.5.0 and rejected a fractional timestamp default.
  2.5.1 already contains that fix; it remains covered by this candidate.
- The second failure was on 2.5.1: a target-only child's FK referenced a parent
  secondary UNIQUE key, which was absent during CREATE. An anonymized real-DB
  fixture reproduced ERROR 6125 and now preserves the parent key and child data.
- A private copy of the supplied dump was restored to an isolated local MySQL
  database: 226 tables, 8,937,334 rows, 8 views, zero failed views (541.33 seconds).
  The user's source dump and live databases were not modified.
- Independent target queries matched all 226 table row counts, all 2,342 raw
  column defaults, and found all 520 declared secondary indexes and 304 FKs.
  SQL-quoted versus raw default representations were compared semantically.
  This proves restoration of the information present in the old dump, not
  recovery of attributes the old dump never recorded.
- That real restore exposed 54 legacy ENUM-zero values. Narrow compatibility now
  preserves those exact values while rejecting unrelated truncation/overflow.
- The subsequent safe-restore rehearsal loaded the same 8,937,334 rows and passed
  all 226 table content checks, then refused cutover because one FK had 11 orphan
  child rows. A separate audit of all 304 declared FKs confirmed exactly one
  violating constraint and 11 orphan references in the isolated exact restore.
  The existing destination retained all 226 tables and 8 views throughout.
  The earlier row-count/declared-object proof did not establish referential
  integrity; creating FKs with checks disabled does not validate existing rows.
- For complete large cutover testing, a separate owned test copy excludes those
  11 rows and is freshly exported. This is a derived rehearsal fixture, not a
  repair of the supplied dump or live PROD/Staging, and not a recommendation to
  delete those records. Production reconciliation needs authoritative parent/child
  data and an explicit repair decision.

## Item-by-item assessment

| Report item | Current correction | What remains |
| --- | --- | --- |
| A1 fractional temporal default | Already fixed in 2.5.1; same/cross-engine fractional precision and live future-insert tests strengthened | Users must actually install the containing release |
| A2 DROP all selected tables first / no complete rollback | New default safe mode restores and verifies a candidate before an explicitly reviewed, engine-specific guarded cutover; original tables are retained as backup | Final failure/race/large-dump verification is in progress before release. Advanced legacy replace remains destructive; unsupported cutover graphs are refused |
| A3 finalization skipped on error | MySQL UNIQUE keys are now created with tables; unsafe subset retry is disabled; failed jobs distinguish data-loaded from final verification. Durable failure/checkpoint report work is included in the incident follow-up | Later ordinary indexes/FKs/views can remain unfinished after failure; reports do not make the whole operation reversible |
| A4 target-only FK / missing secondary UNIQUE | Real reproduction fixed with inline UNIQUE creation. Known incompatible surviving FKs are refused before DROP | Updating the app cannot recreate four constraints already manually removed from Staging |
| A5 misleading failures / missing target | Failed versus unattempted statuses are distinct. Local logs snapshot actual endpoint, DB/schema, SSH remote endpoint, environment, executed mode, phase, confirmed DROP evidence and report path | Missing deletion evidence is reported as unknown, never inferred from an unattempted table. Local logs are distinct from anonymized reports |
| B ON UPDATE | New schema metadata and live update-after-restore regression preserve it | Old dumps cannot recover missing clauses |
| B FK actions | ON DELETE/ON UPDATE are recorded and restored; real CASCADE/SET NULL tests | Missing actions in old dumps and manually dropped target-only constraints need authoritative definitions |
| B CHECK / UUID expression / invisible index / allocator / comments | Same-engine MySQL preservation passes real 8.0/8.4 schema and future-insert fixtures, including allocator gaps | Old dumps lack these attributes; cross-engine limitations remain explicit and final release gates are pending |
| B View security | DEFINER stripping / invoker policy is intentional and unchanged | Restored views need the caller's underlying-table permissions; not a transparent privilege backup |
| C retry always merge | Removed: UI subset retry is refused, complete original scope must be rerun knowingly | Blind append retry can duplicate data; it remains prohibited |
| C server DDL validation after DROP | Server-side owned-table DDL probing is part of the follow-up, before any original DROP, with cleanup and name-collision checks | A successful DDL probe cannot predict every later data/load or connection failure |
| D absent real coverage | Public live schema, dump-format, mode, privilege, timezone, FK and failure fixtures; required disposable-DB CI gate added | CI does not replace testing every provider/version/custom object |

## Existing Staging reconciliation

The historical dump cannot supply missing CHECK/actions/ON UPDATE/comments or
authoritative definitions of manually deleted target-only FKs. Compare with
current PROD definitions and the pre-deletion target definitions, verify column
types/indexes and orphan rows, and review explicit repair SQL before applying it.
The historical baseline SQL is useful evidence but must not be assumed current.
No repair statements were executed against the user's Staging or PROD servers.

The user requires safe restoration before release. A missing destination uses the
source name; an existing destination requires comparison and a retain/candidate/
explicit-overwrite choice. This scope is implemented and undergoing final
verification, including real Python-to-Core promotion on both engines. See [policy](export_import_policy.md) and
[verification/release work record](export_import_verification_2026-09-28.md).
