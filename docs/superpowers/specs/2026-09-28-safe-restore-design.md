# Safe restore and reviewed namespace replacement

Status: implementation and verification in progress; required before the proposed
2.6.0 release. The user approved safe restoration before deployment and destination
name preservation with an explicit choice when a destination already exists.

## Contract

Rust Core owns restoration, inspection, verification and cutover. Python presents
the plan, collects explicit overwrite consent, and reports the actual outcome.
`dump.import` defaults to `safe` in Core and Python entry points.

If the requested namespace does not exist, create it exclusively using the source
name or the user's explicit destination. PostgreSQL operates inside the selected
database; the namespace is a schema. Never guess a missing legacy source schema.
If it already exists, create an owned candidate namespace and restore there.
Preparation never drops the existing tables. Failed candidates remain inspectable.

Before offering replacement, verify actual row counts, streamed content digests,
schema, constraints, references and view isolation. Bind the report to the dump,
candidate, endpoint and restore identifier. Old dumps cannot recover metadata
that their exporter omitted. Same-engine safe restore is the initial scope.

## User choices

Present the original and candidate endpoints, changed objects, preserved
target-only objects, backup namespace and blockers. The user can retain the
original, use the verified candidate, select another destination before restoring,
or confirm replacement of the original name. Replacement defaults to No and uses
the existing production guard. Copying candidate connection details must exclude
credentials. Never silently change a saved connection profile.

## Engine-specific replacement

MySQL: use a bounded supported object graph, native definitions for target-only
tables, precreated replacement views, write locks and one atomic multi-object
RENAME. Refresh target-only rows under those locks and compare SHA-256 content
digests on the same locked connection. Keep original tables in an owned backup
database. Reject unsupported triggers, privileges, object shapes or dependencies
before replacement. This is not a general atomic database swap API.

PostgreSQL: lock and recheck dependencies, then move tables and rebind supported
views and incoming FKs within one transaction. Preserve original tables in an
owned backup schema. Reject unsupported grants, owners and dependencies. Rollback
must restore the original graph if any step fails before commit.

Persist an attempt journal before cutover. An interrupted or uncertain result
must block automatic replay and new-plan bypass until its outcome is reconciled.
Never claim the original is unchanged merely because the client received an
error. A successful cutover report identifies the active and backup namespaces.

## Verification and release gates

1. Public disposable-DB fixtures for preparation success and DDL/data/view/FK
   failure, missing/existing namespace, altered dumps and same-count corruption.
2. Promotion fixtures for target-only FKs/views, concurrent writes, dependency
   changes, stale plans, connection loss, rollback and uncertain-outcome replay.
3. Actual Python-to-Core safe import/plan/confirm flows on both engines.
4. Private-copy large restore and guarded promotion, with aggregate-only evidence.
5. Windows/Linux tests, native release builds and packaged smoke checks; protected
   CI including macOS artifacts before the versioned GitHub release.

Legacy `replace`/`recreate`/`merge` remain explicitly destructive advanced modes.
They have preflight and durable reporting but do not gain whole-import rollback.
The existing Staging incident requires separate authoritative schema reconciliation;
installing this release cannot recreate information missing from an old dump.
