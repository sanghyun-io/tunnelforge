# TunnelForge Current Status

Last reviewed: 2026-07-16

This document is the current repository status index. It separates verified
state from planning documents and lists the next actionable issues.

## Continuity Contract

This file is the canonical handoff document for TunnelForge status work. Any
session that investigates project state, fixes tracked issues, changes recovery
behavior, or changes verification evidence must update this file before ending.

Stable issue IDs use the format `TF-STATUS-###`. Do not renumber IDs. Close an
issue by changing its status and adding evidence; do not delete it unless the
entry was created in error.

Allowed issue statuses:

- `open` - confirmed issue with remaining work.
- `in_progress` - current session is actively changing it.
- `blocked` - cannot continue without external input or environment.
- `fixed_pending_full_verify` - focused fix exists, but broader verification or
  downstream work remains.
- `closed` - verified complete with command evidence.
- `watch` - not actionable now, but should be rechecked when nearby work changes.

## Automatic Update Rules

Update this file automatically when any of these happen:

1. A new issue, risk, doc/code mismatch, disabled feature, or verification gap is
   discovered.
2. A tracked issue is partially fixed, fully fixed, blocked, deprioritized, or
   found invalid.
3. A verification command is run whose result changes or strengthens current
   evidence.
4. A project status document, release/build script, feature flag, or architecture
   boundary changes.
5. A user asks for application status, issue tracking, handoff, roadmap, or next
   work.

Required update fields:

- Update `Last reviewed` if the session materially changes status.
- Add or update an entry in `Issue Tracker`.
- Add command evidence in `Verification Log` when commands are run.
- Add a short entry in `Session Log`.
- Keep `Recommended Execution Order` aligned with open issue priority.

Do not mark an issue `closed` without fresh verification evidence in the same
session. If only focused tests passed, use `fixed_pending_full_verify`.

## Summary

TunnelForge is in a strong build/test state. The active architecture baseline
is Rust Core ownership of DB operations through `tunnelforge-core`, with
Python/PyQt responsible for UI, orchestration, signals, and dialogs.

The latest stable release is now `v2.4.0`. It packages the completed anonymous
error reporting work from TF-STATUS-092: affirmative-consent desktop collection,
strict local sanitization and allowlists, credential-free relay transport,
bounded D1 abuse controls, and GitHub App issue creation through the active
Cloudflare Worker. All three release version sources are aligned at `2.4.0`.
TF-STATUS-093 is `closed` after protected PR #247 and all hosted gates passed,
annotated tag `v2.4.0` was created on the exact merge commit, the separately
approved draft produced and verified all 10 expected assets and SHA-256
digests, and the release was published as stable/latest. The live updater sees
`2.4.0`, the relay remains schema 1 / `active`, and the repository retains only
the two separate Releaser secrets. macOS remains an explicitly unsigned and
unnotarized direct-download artifact under the accepted project policy; this
release makes no real-Mac hardware-validation claim.
Fresh release-candidate verification passed the full Python suite at 2697
passed / 1 skipped / 4 warnings, the Rust Core regression gate and Cargo
test/release build, Worker 316/typecheck/audit-zero/dry-run, the complete
Windows clean build, frozen main-app UI/Rust Core smoke, and WebSetup
self-check. The clean-build review also made the installer command build and
verify WebSetup itself, stopped it from rewriting the tracked Inno source, and
pinned all external actions in the required version and macOS workflows.

The 2026-07-15 product-maturity review converged on one bounded next milestone:
a **Safety and Proof** release before feature expansion or product
repositioning. TF-STATUS-095 through TF-STATUS-101 track confirmed trust,
data-semantics, execution, process-contract, resume-state, dependency, and
analysis-result risks. The governing proposal is
`docs/product_maturity_proposal_2026-07-15.html`. It separates immediate fixes
from contract-first investigation and evidence-gated product hypotheses.
TF-STATUS-095 is `closed`: the persisted SHA-256 trust store, one-time approval
token, approval-time re-probe, changed-key rejection, pinned forwarding and
preflight, raw endpoint binding, and safe first-use PyQt approval are complete.
Every reviewed interactive SSH entry point checks trust before DB credential
access or tunnel creation, while background paths remain noninteractive and
fail closed. TF-STATUS-096 is also `closed`: Import Auto now preserves the
server/session timezone for both MySQL and PostgreSQL, while UTC and KST remain
explicit choices. The next 90-day sequence is safety fixes and executable
contracts, release, then
3-5 observed sessions through one disposable MySQL-over-SSH workflow. Apple
App Store distribution, remote product telemetry, broad support tooling, and
additional database engines remain explicit non-goals.

The historical `2.3.1` release candidate contains the completed release-trust
scope:
GitHub Release asset `digest` verification prevents unverified downloaded
packages from launching, unknown-environment confirmation protects dangerous
operations when tunnel metadata is absent or unclassified, `python-regression`
preserves the full Python suite in CI, and the bilingual Schedule correction
states that scheduled backups and queries remain disabled. TF-STATUS-079,
TF-STATUS-080, and TF-STATUS-082 are closed by this release-candidate
verification. TF-STATUS-083 is closed after live required-check promotion and
successful PR #240 Python/Rust/macOS runs. TF-STATUS-081 remains
closed after the protected merge/tag/build/publication sequence. `v2.3.1` is
published with all 10 expected assets and GitHub SHA-256 digest metadata;
`v2.4.0` supersedes it as stable/latest. TF-STATUS-008 and TF-STATUS-078 remain
open.
TF-STATUS-079 remains closed with strengthened final-review evidence. Fresh
2026-07-13 verification on RC code baseline
`e37f57adfd5053b6a5c8343d9ff7c36f8f4425bd` passed the focused
security/status/version command at 291 passed / 1 skipped in 46.01s and the
full Python suite at 2006 passed / 1 skipped / 4 warnings in 58.07s; the
matching Rust regression gate, Cargo test, release build, version-sync, and
diff checks also passed. The historical 1955-pass release-review snapshot is
preserved in the verification rows below.

TF-STATUS-084 records the final-review update boundary findings: a
verification-to-launch lease, owned cleanup/no-clobber behavior, cancellation
generation isolation, and a bounded streaming path. It remains `closed` with
fresh Fix E secure child creation/name validation and bootstrapper
cancel-before-entry evidence, plus focused update/security/status/version
tests, the full Python suite, Rust Core regression, Cargo test/build, release
build, version-sync, and diff checks.
On non-Windows, automatic installer execution is disabled/reveal-only; this
local work does not make a Mac hardware validation claim. This local
verification does not claim completion of tag/release, GitHub issue closure,
Apple-signed artifact validation, or Mac hardware validation.

TF-STATUS-085 is `closed`: Final Fix F2 aligns the update
cleanup contract across Windows and POSIX, makes Settings/bootstrapper
abandonment idempotent before dispatch, retains verified packages after generic
launch failures, and corrects the Fix Wizard public claim to dry-run/manual SQL
generation. Focused and full Python verification, Rust Core regression and
release builds, frozen WebSetup self-check, version sync, and diff checks are
recorded below against code baseline `87d9021`.

TF-STATUS-086 is `closed`: a final security review found a bootstrapper race
where confirmed cancellation could occur before a completed download path was
published. Code baseline `544c6b0` now synchronizes abandonment and result
publication, discards late results, and suppresses completion dispatch after
confirmed cancellation. The focused/full Python suites and rebuilt frozen
WebSetup self-check passed after the fix.

TF-STATUS-087 is `closed`: non-Windows update UI text now matches reveal-only
behavior. It says "저장 위치 보기", explains that only the containing folder
is shown, and no longer claims that the package opens or the app exits.

TF-STATUS-088 is `closed`: version-gate no longer trusts bump commit messages.
The trusted base calculates the expected version and the PR head may skip only
when all three version files match it. The GitHub App token action that receives
the releaser private key is pinned to reviewed commit
`bcd2ba49218906704ab6c1aa796996da409d3eb1`.

TF-STATUS-089 is `closed`: release tags now require a
manually approved `production-release` deployment, an exact full SHA matching
current `main`, and synchronized version files. Release publication accepts
only a separate approved manual dispatch, validates an immutable tag retained
on `main`, creates a draft for that exact tag, and pins every external action.
The established release policy builds explicitly unsigned macOS artifacts when
Apple credentials are entirely absent, while partial signing/notarization
configuration still fails closed. The
GitHub Environment now has a required reviewer, admin bypass disabled, and
`main`/`v*` deployment policies. `main` protection is strict, applies to admins,
requires conversation resolution and five terminal/platform checks, and an
active ruleset prevents `v*` tag update/deletion/non-fast-forward changes.
Initial PR #240 Actions exposed process-wide i18n leakage in the Windows Python
job and a missing `src.ui` bundle caused by the string-based lazy import in the
macOS package. Code baseline `7d49601` resets language state around every test
and collects `src.ui` submodules for PyInstaller. The full suite, real
PyInstaller build, and frozen UI smoke check pass locally. Replacement PR #240
runs `29229463468` and `29229463485` passed Python regression, Rust Core,
terminal version gate, and both internal/external macOS arm64/x86_64 checks;
the PR is mergeable and `CLEAN`.

TF-STATUS-090 is `watch`: the protected release environment has no Apple
Developer ID signing/notarization secrets, so unsigned macOS artifacts are
accepted for this release under the project's established release behavior.
The long-term distribution policy is direct download through GitHub Releases;
Apple App Store registration is not planned.
TF-STATUS-091 is `watch`: `sanghyun-io` is the only current write/admin
collaborator, and single-maintainer self-approval is accepted for this release.
Adding Apple signing and a second trusted maintainer remain future hardening
opportunities rather than release blockers.

TF-STATUS-092 is `closed`: the legacy issue-reporter GitHub App private key for
App ID `2735888` was embedded by the release path used from `v1.13.4` through
`v2.3.0`. The current `v2.3.1` release does not embed the key or consume
`GH_APP_PRIVATE_KEY` in its release build. Revoking the old key will disable
automatic issue reporting in affected legacy builds without affecting tunnel,
database, migration, or Rust Core operations. Live Developer Settings inventory
proved that the reporter is App ID `2735888`, installed only on
`sanghyun-io/tunnelforge`, with Issues read/write and mandatory Metadata read.
Its exposed key fingerprint is `SHA256:hLY6...nt+k=`. The CI Releaser is a
separate App ID `2927386` with fingerprint `SHA256:6ACP...Unh0=`, so the two
credential lanes are not coupled. A replacement reporter key with fingerprint
`SHA256:6Yki...GYB4=` now overlaps the old key and its local PKCS#8 conversion
matches the GitHub public fingerprint. The replacement Worker secret and
canary are verified: App-authored issue #244 received exactly one bounded
cross-installation recurrence comment, passed the public-content leak check,
and was closed. Emergency `off` and final `active` rollout also passed, and the
desktop client is bound to the exact production Worker route. The exposed key
was deleted at `2026-07-15T02:06:03Z`; the replacement Reporter key remains,
and the separate Releaser key was untouched. Treat previously issued
installation tokens as potentially valid until `2026-07-15T03:06:03Z`.
The containment interval completed at that time. The inventoried-unused
repository secret `GH_APP_PRIVATE_KEY` was removed at
`2026-07-15T03:06:23Z`; the only remaining repository secrets are the separate
`RELEASER_APP_ID` and `RELEASER_APP_PRIVATE_KEY` release credentials.
Fresh-head hosted runs `29385868513` and `29385868516` passed every required
Python, Rust, version, support-tracking, and macOS architecture gate. Protected
PR #245 merged at `2026-07-15T03:22:13Z` as merge commit
`6dbcd51c8c60acef3569697fa79a9e6914a7c0e0`, and the post-merge production
health probe confirmed the relay remains `active`.
The accepted replacement architecture uses a dedicated reporter GitHub App,
installed only on TunnelForge with Metadata read and Issues read/write, behind
a Cloudflare Worker. The desktop client contains no GitHub credential and sends
only an affirmative-consent, versioned allowlist report. Credential containment
does not wait for replacement feature delivery: the exposed key is revoked as
soon as the Releaser coupling gate is resolved.
The accepted design now has a 13-task TDD implementation plan. Its relay abuse
boundary uses atomic D1 global mutation budgets, a pre-create lease, and an
`unknown` quarantine for ambiguous GitHub create timeouts; edge and anonymous
installation limits are explicitly not authentication. Tasks 1-8 now define
the versioned report contract, dependency-free Python validator, shared
adversarial fixtures, local-only environment collector, fail-closed sanitizer,
canonical SHA-256 fingerprint, strict report builder, and atomic consent claim
state machine. The primary-instance consent dialog now defers around active DB
work, releases failed prompt claims, handles application shutdown without
recording an unintended choice, and discloses the exact allowlist boundary.
The desktop transport now requires a current consent dispatch permit, isolates
origin requests from inherited session credentials, retains proxy/CA behavior,
rejects compressed or oversized relay responses, and sanitizes bounded local
Rust diagnostics at every Export/Import display, log, and file boundary.
Configuration transfer cannot carry reporting consent or installation identity.
Settings now provides explicit opt-in/out, a write-free local JSON preview, a
retained nonblocking relay health check, and a privacy-bounded last-attempt
view. The desktop direct GitHub reporter, App authentication, PEM setup path,
PyJWT/python-dotenv runtime dependencies, and packaging imports are retired;
whole-tree guards protect desktop production/build inputs from recurrence.
The Worker edge independently enforces the strict schema, 16 KiB streamed body
bound, exact JSON integer semantics, canonical fingerprint, and fixed
non-echoing errors. Its fail-closed free-text boundary never reads or forwards
the client message; only validated exception class, error code, application
frames, operation, and allowlisted environment fields survive canonical
reconstruction. Task 9 adds exact HTTPS mode routing, privacy-derived shadow
limits, atomic D1 quotas and route leases, cross-state create-generation
uniqueness, stable unknown quarantine, and scheduled cleanup with a named
100-row cleanup bound. Cleanup preserves create-action guard evidence while a
route remains pending, drains backlogs across invocations, and retains ready
and unknown routes.
Task 10 adds PKCS#8-only RS256 GitHub App JWTs, repository-scoped installation
tokens, server-owned bounded issue formatting, and safe create/comment/duplicate
recovery. Every GitHub POST attempt consumes a current route-bound global budget
immediately before mutation, 401 refreshes at most once, ambiguous outcomes are
quarantined, and stale duplicate routes cannot return success. Task 11 adds
hostile-input/log and
global-mutation-cap tests, a synthetic endpoint/mode-only smoke runner,
secret/local-state ignore coverage, and a secure deployment runbook. The
runbook keeps multiline PKCS#8 entry in the Cloudflare Dashboard, limits
interactive Wrangler secret prompts to one-line values, documents operator
canary discipline accurately, and includes bounded smoke process cleanup.
A final cross-task review also closed backup/import privacy-state regressions and
made recurrence counts ignore expired actions before scheduled cleanup. Local
Tasks 1-11 are approved. Tasks 12-13 and live credential containment remain
pending. The reviewed local implementation is published on remote branch
`feat/anonymous-error-reporting-relay`.

Clean Code Round 3 completed on 2026-07-09: the remaining UI/dialog/main-window
refactor work packages WP-3.1 through WP-3.8 were integrated as
behavior-preserving commits. A red-review follow-up restored compatibility for
legacy migration worker constructor kwargs, `CleanupWorker(dry_run=False)`
fail-closed behavior, and Fix Wizard dialog module re-exports including
`BatchOptionDialog`. The SECURE follow-up removed frozen-runtime core helper
`cwd`/implicit `PATH` lookup and hardened schema-derived auto-save filenames
for analysis and rollback files. The integrated main tree passed the Rust Core
regression gate, a whole-tree `MySQLConnector` allowlist scan, Round 3 focused
tests at 491 passed, and the post-strategy-review full Python suite at 1827
passed / 6 warnings.

A role-specialized strategy/security review on 2026-07-10 confirmed that the
next work is release trust rather than another broad refactor. Downloaded update
packages are executed without an application-level hash/signature verification
gate, unset environments allow dangerous SQL without confirmation, current
`main` contains unreleased post-release commits while still declaring the
published version, README advertises scheduled backups while the UI feature
flag is disabled, and branch protection requires only `version-gate`. These
are tracked as TF-STATUS-079 through TF-STATUS-083.

GitHub #170 remains open for issue hygiene only: its reported MySQL ERROR 3780
import path was fixed by PR #171 / commit `a4c7a06`, that fix is contained in
current `main`, and release tags `v2.1.8` through `v2.3.0` contain it. Confirm
the merged fix with the reporter and close the issue unless the failure can be
reproduced on a containing release; it is not remaining Clean Code Round 3
implementation work.

Open GitHub issue #116 remains external. Its current final gate needs both
current-HEAD manual workflow evidence and the real-Mac report before closure.
GitHub #142 is fixed: the legacy Python
Auto-Fix Wizard mutation path is now fail-closed from the user-visible worker
path, legacy Python Auto-Fix Wizard mutations are no longer executable from
that path, and Legacy Auto-Fix Wizard is dry-run/manual SQL only. GitHub issues
#137 through #141 closed the current One-Click
readiness sequence: dry-run preview, limited `deprecated_engine ->
engine_innodb` real execution, charset/collation supplied contract execution,
PyQt-triggered charset contract derivation, and display-only
`int_display_width` skip policy. No repo-side One-Click follow-up issue is
currently open; track each additional automatic-fix class as a separate issue
before implementation.

On 2026-06-27, the remaining repo-side #116 handoff drift found in the next
issue analysis was closed: macOS artifact download defaults now use the PR head
before merge, or current merged main HEAD after PR #117 is merged, matching the
final gate/report SHA policy. #116 itself remains open only for real operator
Mac validation evidence.

The scheduled-backup guide is also reconfirmed as an internal/reactivation
memo while `SCHEDULE_FEATURE_ENABLED = False`; it must not read like current
public UI instructions until the feature flag is intentionally re-enabled and
runtime evidence is refreshed.

The current One-Click Phase A safety gate supersedes the earlier limited
real-execution wording: `ONECLICK_REAL_EXECUTION_ENABLED = False`, both Rust
non-dry-run entry points return `oneclick_apply_disabled` before endpoint or
SQL work, and PyQt exposes dry-run only. Historical June evidence remains a
record of the retired path, not a command to refresh or a current capability.
TF-STATUS-097 Phase B Task 3 is complete at `62dc7f4`: Rust now builds and
validates a canonical, secret-free plan by reconstructing exact ordered actions
from normalized snapshot facts and the fixed profile. FK-connected charset
findings remain manual, malformed engine markers/schema facts/members fail
closed, legacy preview paths are disabled, and `oneclick.apply_fixes` is not
advertised. Independent TERRA rereview approved all seven prior rejection
findings. The issue remains open for the raw apply boundary, generic candidate
executor, Python approval facade, default-No UI, and final evidence tasks; both
production apply predicates remain false.

TF-STATUS-097 Phase B Task 4 is complete through `2115f3a`. The strict raw
apply parser, UUID-only advisory lock, same-session exact replan, per-action
pre/SQL/post checks, partial ordinal reporting, and release-on-definite-exit
candidate are implemented behind the false/false hard gate. Two independent
review rejections closed DDL response-loss ambiguity end to end: Rust now
emits `oneclick_outcome_indeterminate` with a separate uncertain ordinal, and
Python accepts the exact metadata only for mutation requests and never retries.
Task 4 final rereview approved; real MySQL lock/DDL execution is not claimed in
this environment and production apply remains unreachable.

TF-STATUS-098 is `closed` at final implementation commit `420518e`. The DB Core
client now enforces bounded deadlines, strict request IDs and structured wire
errors, generation-bound connection handles, unusable-process reaping, typed
indeterminate mutation outcomes, zero mutation resend, and terminal pipe/PID
settlement. Python consumers retain process ownership through cancellation and
cleanup, never force-terminate Qt workers, accept exactly one command-correlated
terminal frame with exact boolean success, and keep dialogs open until worker
and residual cleanup settle. Fresh strict targeted `638 passed`, Rust `296
passed, 1 ignored`, release build, Python 3.9 compile, diff, and process gates
pass; independent Cleanup, Rust Wire, and Consumers reviews all approved. The
full Python strict gate passed `3115 passed, 2 skipped`, completing local
closure. Hosted CI remains a separate final-release gate rather than a claim in
this local closure.

Current main next-issue re-audit on 2026-06-27 initially confirmed only #116
was open and found no Rust Core baseline violation in legacy connector names:
`MySQLConnector`/`PostgresConnector` route through
`DbCoreFacade`/`RustDbConnection`, hidden schedule SQL execution uses the Rust
connector shim when enabled, and SQL editor query execution also routes through
the Rust connector shim. A later focused audit found a different repo-side
baseline gap: the legacy Auto-Fix Wizard mutation policy is still owned by
Python, now tracked as GitHub #142 / TF-STATUS-040.

That Legacy Auto-Fix Wizard mutation path was fixed later on 2026-06-27:
`FixWizardDialog.ExecutionPage` now starts `FixWizardWorker` with
`dry_run=True`, the worker rejects `dry_run=False`, and the UI text presents
the page as SQL/Dry-run confirmation instead of DB execution.

GitHub #143 is fixed as the deeper follow-up: the legacy Auto-Fix core
mutation APIs now fail-close when `dry_run=False` is requested, and the
legacy Auto-Fix core mutation APIs are no longer executable in Python mutation
mode.
`BatchFixExecutor.execute_batch` and
`FKSafeCharsetChanger.execute_safe_charset_change` reject Python-owned DB
mutation mode before session state or execution hooks are touched, and
`BatchFixExecutor._execute_single` is also fail-closed if called directly,
while dry-run/SQL generation remains available. Direct
`cursor.execute`/`commit`/`rollback` mutation calls were removed from
`src/core/migration_fix_wizard.py`.

GitHub #144 is fixed as the next Rust Core baseline follow-up: the legacy
MigrationAnalyzer cleanup mutations now fail-close when `dry_run=False` is
requested, and the migration analyzer dialog no longer offers legacy
Python-owned actual cleanup execution. `MigrationAnalyzer.execute_cleanup`
rejects non-dry-run mutation mode before cursor/session/commit/rollback work,
while Dry-Run and SQL preview remain available.

GitHub #145 is fixed as the worker-level follow-up: legacy CleanupWorker
actual cleanup mode now fails closed at construction time. `CleanupWorker(...,
dry_run=False)` is rejected before a thread can emit misleading `[실행]`
progress or call the analyzer path, while Dry-run cleanup worker construction
remains available.

GitHub #146 is fixed as the connector-surface follow-up: the unused legacy
MySQLConnector execute_many mutation helper now fails closed before cursor or
commit work. `MySQLConnector.execute_many` no longer exposes a dormant Python
batch mutation helper, while existing read/query helper behavior is unchanged.

Post-#146 next issue analysis on 2026-06-27 reconfirmed #116 was the only open
GitHub issue. The normal repository-side #116 gate passed, and the then-current
final-gate blockers were external validation evidence rather than a new
repo-side implementation issue. The older manual-workflow portion of that
finding is superseded by later current-head workflow refreshes on #116; the
current blocker is missing real-Mac report evidence under `build/`.

GitHub #147 is fixed as the release-readiness follow-up: post-release version
drift after `v2.1.6` is resolved by bumping the next unreleased source version
to `2.1.7` across `src/version.py`, `pyproject.toml`, and
`installer/TunnelForge.iss`.

GitHub #148 is fixed as the release-publication follow-up: tag `v2.1.7` was
created from current `main` commit `fa22306`, Build and Release workflow run
`28255274238` completed successfully, and GitHub release `v2.1.7` was
published with `TunnelForge-Setup-2.1.7.exe`, `TunnelForge-WebSetup.exe`,
`TunnelForge-macOS-2.1.7-arm64.dmg`,
`TunnelForge-macOS-2.1.7-arm64.zip`,
`TunnelForge-macOS-2.1.7-x86_64.dmg`,
`TunnelForge-macOS-2.1.7-x86_64.zip`, and checksum assets.

Post-#148 next issue analysis on 2026-06-27 reconfirmed #116 was the only open
GitHub issue. The normal repository-side #116 gate passed, and the then-current
final-gate blockers were external validation evidence rather than repo-side
implementation work. The older manual-workflow portion of that finding is
superseded by later current-head workflow refreshes on #116; the current
blocker is missing real-Mac report evidence under `build/`.

GitHub #149 is fixed as the next release-readiness follow-up: post-v2.1.7
version drift after release-tracking commits was resolved by bumping the next
unreleased source version to `2.1.8` across `src/version.py`,
`pyproject.toml`, and `installer/TunnelForge.iss`.

GitHub #150 is fixed as a Rust Core baseline hardening follow-up: the unused
`RustDbCursor.executemany` Python-side batch helper now fails closed before
any query/facade call. Explicit single-query Rust Core execution paths remain
unchanged, and batch DB operations must be modeled as explicit Rust Core
commands.

GitHub #151 is fixed as a current-status handoff cleanup: stale current-tense
`1830 passed, 5 warnings` full-suite wording from TF-STATUS-049 is now
superseded by later full-suite evidence from TF-STATUS-050 / TF-STATUS-051.

Post-#151 main merge and next issue analysis on 2026-06-27 reconfirmed that
main was aligned with origin/main before that status update, the status update
was pushed to origin/main, #116 was still the only open GitHub issue, and the
normal repository-side #116 gate passed. The then-current final-gate blockers
were external validation evidence rather than repo-side implementation work.
The older manual-workflow portion of that finding is superseded by later
current-head workflow refreshes on #116; the current blocker is missing
real-Mac report evidence under `build/`.

GitHub #152 is fixed as the post-#151 full-suite evidence refresh: after adding
post-#151 current-status coverage, `pytest -q` reported `1839 passed, 5
warnings`; that count is now superseded by TF-STATUS-057 full-suite evidence.

GitHub #153 is fixed as a Rust Core DML affected-row reporting follow-up:
Rust Core `query.execute` now returns `rows_affected` for non-row-returning
statements, Python `DbCoreFacade` preserves that metadata, and
`RustDbCursor.rowcount` uses it for DML. Scheduled SQL and SQL editor DML
reporting can now show real affected-row counts instead of the previous
empty-row fallback count.

GitHub #154 is fixed as the call-local affected-row metadata follow-up:
`DbCoreFacade.execute_query_result` and `execute_on_connection_result` now
return rows plus `rows_affected` together, and `RustDbCursor.rowcount` consumes
that per-call result instead of shared facade state. This prevents concurrent
cursor calls on the shared Rust Core facade from mixing DML rowcount metadata.

GitHub #155 is fixed as the SQL statement parser mismatch follow-up:
`src/core/sql_statement_parser.py` now owns the shared robust parser for SQL
file execution, SQL Editor execute-all/current-query, and hidden scheduled SQL.
`find_sql_statement_at_position` uses parser ranges so SQL Editor current-query
execution returns a whole statement when the cursor is inside comments,
PostgreSQL dollar quote bodies, quoted identifiers, or MySQL DELIMITER scripts.

GitHub #156 is fixed as a SQL dollar quote helper guard follow-up:
`read_dollar_quote` now returns an empty marker for empty SQL text and
out-of-range start offsets instead of raising `IndexError` or inspecting a
negative Python index. The compatibility wrapper
`SQLExecutionWorker._read_dollar_quote` now inherits the same fail-closed
behavior.

GitHub #157 is fixed as a One-Click readiness handoff cleanup:
`docs/oneclick_readiness.md` no longer labels the completed One-Click guidance
as `Recommended next repo-side change`. The section is now standing policy
for future One-Click automatic-fix expansion and explicitly states that no
repo-side One-Click follow-up issue is currently open.

GitHub #158 is fixed as a SQL dollar quote helper None input follow-up:
`read_dollar_quote(None, 0)` and
`SQLExecutionWorker._read_dollar_quote(None, 0)` now return an empty marker
instead of raising `TypeError`, matching the parser's existing fail-closed
empty-input behavior.

Post-#156 main merge and next issue analysis on 2026-06-27 reconfirmed that
`main` was already aligned with `origin/main`, #116 was still the only open
GitHub issue, and the normal repository-side #116 gate passed. The then-current
final-gate blockers were external validation evidence rather than repo-side
implementation work. The older manual-workflow portion of that finding is
superseded by later current-head workflow refreshes on #116; the current
blocker is missing real-Mac report evidence under `build/`.

The current full Python suite count was refreshed again on 2026-06-27 after
the latest status update regression coverage was added.

GitHub #160 is fixed: partial Export FK parent auto-inclusion now resolves
transitive parent tables through Rust Core-owned schema inspection
(`schema.inspect`) instead of constructing a Python `MySQLConnector` in
`RustDumpExporter.export_tables`.

GitHub #161 is fixed: PostgreSQL Export/Import now preserves the PostgreSQL
engine from `PostgresConnector` through `RustDumpConfig` into Rust Core
`dump.run` and `dump.import` endpoints instead of falling back to MySQL.

GitHub #162 is fixed as the PostgreSQL Import timezone follow-up: the Import
dialog no longer runs MySQL `mysql.time_zone_name` auto-detection or sends
MySQL `SET SESSION time_zone` correction SQL for PostgreSQL dump imports.
PostgreSQL default auto mode now leaves timezone SQL unset, while forced KST
and UTC options use PostgreSQL `SET TIME ZONE` syntax.

GitHub #163 is fixed as the Rust Core boundary follow-up: `dump.import`
timezone validation now accepts the safe PostgreSQL `SET TIME ZONE` form in
addition to the existing MySQL `SET SESSION time_zone` form, while still
rejecting multi-statement SQL, comments, global timezone mutation, and unsafe
timezone literals.

GitHub #164 is fixed as the PostgreSQL dump wrapper API follow-up: the
module-level `export_schema`, `export_tables`, and `import_dump` convenience
wrappers now accept an optional `engine` parameter, default to MySQL for
backward compatibility, and preserve `engine="postgresql"` into
`RustDumpConfig` for Rust Core endpoints.

GitHub #165 is fixed as the hidden scheduled backup follow-up:
`BackupScheduler._execute_backup` now preserves PostgreSQL tunnel engine
metadata into `RustDumpConfig`, matching the engine-aware scheduled SQL path.
Because `SCHEDULE_FEATURE_ENABLED = False`, this was a reactivation/internal
path issue rather than a current public UI regression.

GitHub #166 is fixed as the next hidden scheduled backup follow-up:
`BackupScheduler._execute_backup` now accepts the real
`TunnelEngine.get_connection_info()` `(host, port)` tuple shape as well as
dict-shaped test doubles, and resolves credentials through
`config_manager.get_tunnel_credentials(...)` or tunnel config fallbacks before
constructing `RustDumpConfig`.

Post-#166 next issue re-audit on 2026-06-27 reconfirmed #116 was the only open
GitHub issue and the normal repository-side #116 gate passed. The then-current
final-gate blockers were external validation evidence rather than repo-side
implementation work. The older manual-workflow portion of that finding is
superseded by later current-head workflow refreshes on #116; the current
blocker is missing real-Mac report evidence under `build/`. Rust Core baseline
and stale handoff scans found no new repo-side implementation issue.

GitHub #167 is fixed as the #116 current-head workflow evidence handoff
follow-up: `docs/current_status.md` no longer treats an exact manual macOS
workflow run ID or SHA as durable current-head evidence. Exact current-head
manual workflow evidence is tracked on GitHub #116 comments after status-only
commits, and `scripts\check-macos-support-gate.py --final` is the authoritative
check that the latest successful manual `macOS App Validation`
`workflow_dispatch` run matches current `main`. GitHub #116 remains external
because the final gate still fails only for missing real operator Mac validation
report evidence under `build/`.

GitHub #116 manual macOS workflow evidence was refreshed during this session:
a manual `macOS App Validation` workflow_dispatch run passed for the
then-current main HEAD, including both `arm64` and `x86_64` jobs. That evidence
is historical in this document because status-only commits advance `main`;
rerun the manual workflow after such commits and record the exact current-head
run on #116.

GitHub #168 is fixed as the current focused final-gate row cleanup: the
current focused final-gate row now fails only for missing real-Mac report,
matching the latest `scripts\check-macos-support-gate.py --final` output after
current-head manual workflow evidence was refreshed on #116.

GitHub #169 is fixed as the current-status Summary cleanup: superseded
missing-manual-workflow wording from older re-audit paragraphs is no longer
presented as current Summary state. The Summary now keeps the current #116
blocker focused on missing real operator Mac validation report evidence.

Post-#169 next issue re-audit on 2026-06-27 reconfirmed GitHub #116 is still
the only open issue. Rust Core boundary and stale handoff scans confirmed that
no new repo-side implementation issue was found: legacy-shaped connector calls
still route through Rust Core shims, the only external command hit was live
evidence container seeding, and current open work remains external real-Mac
validation report evidence. Current-head manual workflow evidence remains
tracked on #116 comments and by `scripts\check-macos-support-gate.py --final`,
not as a durable exact run ID in this Summary.

GitHub #116 final validation tooling was rechecked on 2026-06-27: the macOS
manual validation/report scripts still parse cleanly, macOS focused tests still
pass at 53 passed, the normal #116 repository-side gate passes, and the final
gate accepts the latest current-head manual workflow proof while failing only
for the missing real-Mac report under `build/`. No additional repo-side tooling
issue was found.

Post-#142 next issue analysis on 2026-06-27 found #116 was still the only open
GitHub issue and the normal repository-side macOS support gate passed. The
then-current final-gate blockers were external validation evidence rather than
a new repo-side implementation issue. The older manual-workflow portion of that
finding is superseded by later current-head workflow refreshes on #116; the
current blocker is missing real-Mac report evidence under `build/`.

Rust Core Export/Import context-menu wording was realigned on 2026-06-27 so
the visible tunnel actions and handlers match the Rust Core implementation
instead of legacy shell-branded labels.

One-Click fallback dry-run tooltip wording was also cleaned up on 2026-06-27:
if real execution is disabled in a future build, the dialog now explains that
real execution is disabled in this build instead of pointing at the already
closed GitHub #138 gate.

One-Click module scope wording now matches the current implementation: Rust DB
Core owns the workflow, dry-run is the default, and real execution is limited
to backup-confirmed validated scopes.

Windows installer examples in `BUILD.md` now avoid the stale `1.0.0` sample
version and use `{version}` / `{#MyAppVersion}` placeholders aligned with the
release version sync path.

The next #116 repo-side analysis found and closed one final-gate mismatch:
after PR #117 has merged, the manual workflow_dispatch artifact run now follows
the same head policy as the final report and artifact download path: PR head
before merge, current merged main HEAD after merge.

A post-merge next-issue external re-audit on 2026-06-27 reconfirmed that
`main` is aligned with `origin/main`, #116 remained external, the full #116
repository-side gate passed, and SQL editor query execution also routes through
the Rust connector shim. The follow-up baseline scan created GitHub #142 after
confirming the separate legacy Python Auto-Fix Wizard mutation path.

## Current Baseline Verification

Commands run locally. The 2026-07-13 release-candidate verification refreshes
the current Python, Rust Core, release-build, version-sync, and diff evidence
on the verified RC code baseline; status-only history remains historical and
does not call the prior status-only baseline current. This latest status update
preserves earlier broad evidence rows, including the historical Full-suite count refreshed on 2026-06-27 baseline.

| Check | Result |
| --- | --- |
| `git status --short --branch` | verified RC code baseline `7d49601` on `feat/trust-release-sprint`; status-only history remains historical and does not alter the verified code baseline |
| update/security/status/version focused pytest | PASS, 319 passed, 1 skipped in 48.27s, exit 0 |
| `pytest -q` | PASS, 2028 passed, 1 skipped, 4 warnings, 59.79s, exit 0 |
| Round 3 focused pytest suite | PASS, 491 passed, 2 warnings |
| `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS, Rust regression gate pass, exit 0 |
| whole-tree `MySQLConnector` allowlist scan | PASS, 22 product imports and no missing allowlist entries |
| `cargo test --manifest-path migration_core\Cargo.toml` | PASS, 216 lib, 2 JSONL CLI, 9 live, 2 stress tests passed; 1 stress test ignored, exit 0 |
| `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS, 0.30s, exit 0 |
| `.venv\Scripts\python.exe -m PyInstaller --noconfirm bootstrapper\bootstrapper.spec` | PASS, frozen `TunnelForge-WebSetup.exe` built, exit 0 |
| `dist\TunnelForge-WebSetup.exe --self-check` | PASS, `TUNNELFORGE_WEBSETUP_SELF_CHECK_OK`, exit 0 |
| `pytest tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q` | PASS, 1 passed in 0.09s, exit 0 |
| `python -m compileall -q main.py src tests scripts` | PASS |
| `git diff --check` | PASS, exit 0; final docs/status diff check recorded in this session |
| `tunnelforge-core service.hello` | PASS, reports `dump.run`, `dump.import`, migration commands, and `oneclick.*` commands |
| `cargo test --manifest-path migration_core\Cargo.toml --test live_roundtrip -- --nocapture` | PASS, 6 live container MySQL/PostgreSQL smoke tests |
| `pytest tests\test_live_ui_migration_capture.py tests\test_live_ui_migration_evidence.py -q` | PASS, capture and validator tests |
| `RUST_CORE_REQUIRE_PERF_EVIDENCE=1; RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS |
| `python scripts\check-macos-support-gate.py --skip-github` | PASS |
| `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS, 53 passed |
| `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL, missing current-HEAD manual workflow evidence and real-Mac report |
| GitHub required status checks | `version-gate` only |

Historical release-review snapshots (preserved for audit):

    | `pytest -q` | PASS, 1827 passed, 6 warnings |
    | `pytest -q` | PASS, 1955 passed, 1 skipped, 4 warnings, 60.38s, exit 0 |

Version references are aligned at `2.4.0` across:

- `src/version.py`
- `pyproject.toml`
- `installer/TunnelForge.iss`

## Focused Verification On 2026-06-27

Commands run locally:

| Check | Result |
| --- | --- |
| `pytest -q` | PASS, 1876 passed, 5 warnings |
| `python scripts\check-macos-support-gate.py --skip-github` | PASS |
| `python scripts\check-macos-support-gate.py` | PASS |
| `pytest tests\test_build_docs.py tests\test_current_status_docs.py::test_current_status_records_build_doc_installer_version_cleanup -q` | RED then PASS |
| `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_module_docstring_matches_limited_rust_core_scope tests\test_current_status_docs.py::test_current_status_records_oneclick_module_scope_docstring_cleanup -q` | RED then PASS |
| `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_disabled_real_execution_tooltip_does_not_reference_closed_138 tests\test_current_status_docs.py::test_current_status_records_oneclick_fallback_dry_run_tooltip_cleanup -q` | RED then PASS |
| `pytest tests\test_main_window_export_import_labels.py -q` | PASS |
| `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_uses_local_head_for_manual_workflow_after_pr_merge -q` | RED then PASS |
| `pytest tests\test_rust_core_packaging.py::test_macos_validation_artifact_download_script_uses_local_head_after_pr_merge -q` | RED then PASS |
| `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS, 53 passed |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_merge_next_issue_external_reaudit -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_python_auto_fix_wizard_issue -q` | RED then PASS |
| `pytest tests\test_fix_wizard_dialog.py -q` | RED then PASS, 2 passed |
| `pytest tests\test_migration_fix_wizard.py -q` | RED then PASS, 88 passed |
| `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py -q` | PASS, 20 passed |
| `pytest tests\test_migration_analyzer.py::TestExecuteCleanup::test_actual_cleanup_rejects_legacy_python_mutation_mode tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled -q` | RED then PASS |
| `pytest tests\test_migration_analyzer.py::TestExecuteCleanup tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled -q` | PASS, 3 passed, 2 warnings |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_migration_analyzer_cleanup_issue -q` | RED then PASS |
| `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_migration_analyzer.py::TestExecuteCleanup tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled tests\test_current_status_docs.py::test_current_status_tracks_legacy_migration_analyzer_cleanup_issue tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count -q` | PASS, 6 passed, 2 warnings |
| `pytest tests\test_migration_worker.py -q` | RED then PASS, 2 passed, 2 warnings |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_cleanup_worker_issue -q` | RED then PASS |
| `pytest tests\test_db_connector.py::TestMySQLConnector::test_execute_many_rejects_legacy_python_mutation_helper -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_execute_many_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_146_next_issue_analysis -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_release_version_drift_issue -q` | RED then PASS |
| `pytest tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q` | PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_v217_release_publication_issue -q` | RED then PASS |
| `gh run view 28255274238 --json status,conclusion,url` | PASS, Build and Release workflow completed successfully |
| `gh release view v2.1.7 --json tagName,name,url,assets,publishedAt,targetCommitish,isDraft,isPrerelease` | PASS, release `v2.1.7` published with Windows and macOS assets |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_148_next_issue_analysis -q` | RED then PASS |
| `git status --short --branch` | `## main...origin/main`, no local changes before #116 re-analysis |
| `gh issue list --state open --limit 20` | PASS, only #116 open |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_v217_version_drift_issue -q` | RED then PASS |
| `python scripts\bump_version.py --bump-type patch` | PASS, bumped `2.1.7` to `2.1.8` |
| `pytest tests\test_db_core_service.py::test_rust_db_cursor_executemany_rejects_python_batch_helper -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_rust_db_cursor_executemany_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_does_not_describe_stale_full_pytest_count_as_current -q` | RED then PASS |
| `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL, missing real-Mac report only |
| `bash -n scripts/macos-download-validation-artifacts.sh scripts/macos-manual-validation-report.sh` | PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_151_next_issue_analysis -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py -q` | PASS, 53 passed |
| `python -m compileall -q src\core\i18n.py src\ui\dialogs\fix_wizard_dialog.py src\ui\workers\fix_wizard_worker.py tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py` | PASS |
| `git diff --check` | PASS |
| `gh issue create --title "Unify SQL statement parsing across SQL Editor and execution paths" ...` | PASS, created #155 |
| direct parser comparison between `SQLEditorDialog._split_queries` and `SQLExecutionWorker._parse_sql_statements` | PASS, reproduced SQL Editor over-splitting comments, PostgreSQL dollar quote bodies, and MySQL `DELIMITER` scripts |
| `pytest tests\test_sql_editor_dialog.py::test_split_queries_preserves_comments_dollar_quotes_and_delimiters tests\test_sql_editor_dialog.py::test_get_query_at_cursor_uses_statement_parser_ranges -q` | RED then PASS |
| `pytest tests\test_scheduler.py::TestBackupScheduler::test_parse_sql_queries_preserves_comments_dollar_quotes_and_delimiters -q` | RED then PASS |
| `pytest tests\test_sql_editor_dialog.py tests\test_scheduler.py tests\test_sql_execution_worker.py -q` | PASS, 71 passed, 2 warnings |
| `pytest tests\test_sql_execution_worker.py::test_dollar_quote_reader_fails_closed_for_out_of_range_starts -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_156_next_issue_analysis -q` | RED then PASS |
| `gh issue list --state open --limit 30 --json number,title,labels,url,updatedAt` | PASS, only #116 open |
| `pytest tests\test_oneclick_readiness_docs.py::test_oneclick_readiness_does_not_present_closed_issues_as_current_tracking -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_oneclick_next_action_wording_issue -q` | RED then PASS |
| `pytest tests\test_sql_execution_worker.py::test_dollar_quote_reader_fails_closed_for_none_sql_text -q` | RED then PASS |
| `pytest tests\test_sql_execution_worker.py tests\test_sql_editor_dialog.py tests\test_scheduler.py -q` | PASS, 73 passed, 2 warnings |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_dollar_quote_none_input_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_baseline_provenance_uses_latest_status_update -q` | RED then PASS |
| `pytest tests\test_rust_dump_exporter.py::TestRustDumpExporter::test_export_tables_resolves_fk_parents_through_rust_schema_inspect -q` | RED then PASS |
| `pytest tests\test_rust_dump_exporter.py -q` | PASS, 37 passed, 2 warnings |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_partial_export_fk_parent_rust_inspect_issue -q` | RED then PASS |
| `pytest tests\test_rust_dump_exporter.py::TestRustDumpConfig::test_config_preserves_postgresql_engine tests\test_rust_dump_exporter.py::TestRustDumpExporter::test_export_full_schema_preserves_postgresql_engine_in_rust_payload tests\test_rust_dump_exporter.py::TestRustDumpImporter::test_import_dump_preserves_postgresql_engine_in_rust_payload -q` | RED then PASS |
| `pytest tests\test_db_dialogs.py::test_preselected_export_tunnel_uses_postgres_connector_for_postgresql tests\test_db_dialogs.py::test_export_dialog_uses_direct_connector_host_for_rust_dump tests\test_db_dialogs.py::test_import_dialog_uses_direct_connector_host_for_rust_dump -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_rust_dump_engine_issue -q` | RED then PASS |
| `pytest tests\test_db_dialogs.py -q -k "postgresql_import_auto_timezone or postgresql_import_forced_kst"` | RED then PASS |
| `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_db_dialogs.py -q -k "direct_hardcoded or postgresql_import_auto_timezone or postgresql_import_forced_kst"` | PASS, 3 passed |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_import_timezone_issue -q` | RED then PASS |
| `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_mysql_and_postgresql_timezone_forms --lib` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_import_timezone_core_validation_issue -q` | RED then PASS |
| `pytest tests\test_rust_dump_exporter.py -q -k "wrapper_preserves_postgresql_engine"` | RED then PASS, 3 passed |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_dump_wrapper_engine_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_scheduled_backup_postgresql_engine_issue -q` | RED then PASS |
| `pytest tests\test_scheduler.py::TestBackupScheduler::test_backup_task_preserves_postgresql_engine_for_rust_dump -q` | RED then PASS |
| `pytest tests\test_scheduler.py::TestBackupScheduler::test_backup_task_accepts_tuple_connection_info_for_rust_dump -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_scheduled_backup_tuple_connection_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_166_next_issue_reaudit -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_manual_macos_workflow_evidence -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_non_self_stale_macos_workflow_evidence_policy -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_focused_final_gate_reason_matches_current_workflow_evidence -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_summary_does_not_keep_superseded_missing_manual_workflow_wording -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_169_next_issue_reaudit -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_macos_final_validation_tooling_recheck -q` | RED then PASS |

## Verification Log

| Date | Scope | Command | Result | Notes |
| --- | --- | --- | --- | --- |
| 2026-07-16 | TF-STATUS-097 Phase B Task 4 gated apply candidate | Rust One-Click RED/GREEN; live harness; full Cargo; strict DB Core service/process contract; `RUSTFLAGS=-D warnings cargo check --all-targets`; release build; Python compile; `git diff --check`; independent TERRA review with two fix loops | One-Click `61 passed`; full Rust `301 + 10 + 11 + 9 + 2 passed, 1 manual stress ignored`; strict Python process `238 passed`; warning check, release build, compile, and diff passed; final `Task 4 Review: APPROVE` | Public apply remains false/false and unadvertised. SQL response loss is explicit typed indeterminate with completed and uncertain ordinals separated; Python accepts that metadata only for mutations and does not retry. Live tests passed as harnesses but explicitly skipped DB work because `TF_MYSQL_*` was unset. |
| 2026-07-16 | TF-STATUS-097 Phase B Task 3 canonical Rust plan | `cargo test --manifest-path migration_core\Cargo.toml oneclick_plan --lib`; full Cargo; `RUSTFLAGS=-D warnings cargo check --all-targets`; Cargo release build; focused status/proposal docs; `git diff --check`; independent TERRA rereview | focused plan `21 passed`; independent One-Click `48 passed`; full Rust `285 + 10 + 11 + 9 + 2 passed, 1 manual stress ignored`; warning check, release build, docs `79 passed`, and diff check passed; `Task 3 Review: APPROVE` | Exact action reconstruction rejects fully rehashed substitutions; FK-connected charset actions are omitted while findings remain visible; markers, member/schema scope, nested nulls, and quoted semicolons are strict; apply is unadvertised and both production predicates remain false. TF-STATUS-097 stays open for Tasks 4-7. |
| 2026-07-16 | TF-STATUS-098 final process and consumer closure | Strict targeted DB Core/consumer gate; full Python strict warning gate; `cargo test --manifest-path migration_core\Cargo.toml`; Cargo release build; Python 3.9 compile; `git diff --check`; independent Cleanup/Rust Wire/Consumers reviews | strict targeted `638 passed`; full Python strict `3115 passed, 2 skipped`; Rust `296 passed, 1 ignored`; release build and static gates passed; `Cleanup: APPROVE`; `Rust Wire: APPROVE`; `Consumers: APPROVE` | Final commit `420518e` closes bounded request, generation, protocol/ID, typed outcome, zero mutation resend, pipe/process ownership, nonblocking cancellation, terminal-frame cardinality/correlation, and fail-closed dialog dismissal. Hosted CI remains required at the final release gate and is not claimed here. |
| 2026-07-16 | TF-STATUS-098 Task 2 generation barrier and pipe settlement | Focused finalizer RED/GREEN; two failing real-child nodes; strict ResourceWarning integration; full process contract; adjacent DB Core/exporter/CI bundle; Cargo release; compileall; `git diff --check`; worktree Python-process gates between commands | RED `3 failed, 2 passed`; GREEN `5 passed`; named nodes `2 passed`; strict integration `16 passed`; contract `129 passed`; adjacent `255 passed`; Cargo release, compileall, and diff check exited `0`; every process gate found zero worktree-owned Python processes | The finalizer snapshots `client._process`, never independently settles that current generation, settles only unresolved orphan transports before shutdown, and verifies all tracked PIDs/transports after production shutdown. Terminal `BrokenPipeError`/`ConnectionResetError` from `wait_closed()` is accepted only after stdin close was requested; unrelated exceptions and strict `ResourceWarning` remain unsuppressed. TF-STATUS-098 is `fixed_pending_full_verify` pending full-project and hosted verification. |
| 2026-07-15 | One-Click Phase A fail-closed gate / TF-STATUS-097 | Existing mutation characterization; Rust entry-point RED/GREEN; Python worker/UI/i18n RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; full Cargo; focused One-Click/DB Core/i18n and evidence/capture regressions; direct capture CLI/import-order checks; full Python; Python 3.9 compile; `git diff --check`; independent LUNA review loops | PASS: internal mutation characterization `1 passed`; Rust One-Click `23 passed`; full Rust `231 passed, 1 ignored`; focused Phase A `156 passed`; full Python `2840 passed, 2 skipped`; final Spec and Quality verdicts `APPROVE` | Both `oneclick.run` and `oneclick.apply_fixes` reject non-dry-run before phase/preflight/action/endpoint/adapter/SQL; Python rejects before connector/facade; all mutation-capture CLI/callables reject before runtime imports, seed, DB work, or artifact output; dry-run evidence accepts only exact `oneclick_real_execution_enabled=false`; backup remains disabled after completion. Historical real-execution readiness/capture instructions are superseded during Phase A. TF-STATUS-097 remains open for the TF-STATUS-098-dependent exact-plan contract. |
| 2026-07-15 | Neutral Import timezone / TF-STATUS-096 | RED/GREEN dialog, copy, and payload tests; `.venv\Scripts\python.exe -m pytest tests\test_db_import_dialog.py tests\test_rust_dump_exporter.py tests\test_i18n.py -q`; expanded import/i18n regression; full Python; `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_mysql_and_postgresql_timezone_forms --lib`; production probe/duplicate scan; `git diff --check`; independent TERRA review and PostgreSQL legacy-compatibility follow-up | PASS: focused `120 passed`; expanded `171 passed`; full `2825 passed, 2 skipped`; Rust `1 passed, 215 filtered out`; TF-STATUS-096 final review: APPROVE with no Critical or Important findings | Auto is selected by default and emits `timezone_sql=None` for MySQL and PostgreSQL; `mysql.time_zone_name` probing and the duplicate None radio are gone; explicit UTC/KST SQL remains engine-specific; legacy `none` resolves to no SQL for both engines; the exporter already omitted a falsey timezone field, so no Rust protocol or settings migration was required. |
| 2026-07-15 | SSH first-use approval UI / TF-STATUS-095 Task 2 | Dialog and integration RED/GREEN; expanded MainWindow, cross-engine, SQL Editor, and SQL Execution ordering regressions; focused Task 2 and wider SSH/UI suites; full `.venv\Scripts\python.exe -m pytest`; production `py_compile`; repository call-path scan; `git diff --check`; independent LUNA final review | PASS: final focused `218 passed`; wider `424 passed, 1 skipped`; full `2820 passed, 2 skipped, 2 warnings`; Task 2 final review: APPROVE with no blocking findings | Unknown hosts show host:port, algorithm, and SHA-256 fingerprint with Cancel/Escape as the safe default; changed keys have no approval action; approval races fail closed; all reviewed interactive starts approve before credential access, worker/dialog creation, or tunnel creation; direct mode bypasses SSH trust; workers/scheduler/monitor/reconnect remain noninteractive; result and QThread lifecycle signals are separated; no `PytestCollectionWarning` remains. TF-STATUS-095 is closed. |
| 2026-07-15 | SSH host trust Core slice / TF-STATUS-095 Task 1 | RED/GREEN trust-store and engine tests; `.venv\Scripts\python.exe -m pytest tests\test_ssh_host_trust.py tests\test_platform_paths.py tests\test_tunnel_engine.py tests\test_connection_test_worker.py -q`; related call-path regression; `git diff --check`; three independent TERRA security reviews with two fix loops | PASS: final focused `71 passed, 1 skipped, 1 warning`; related regression `163 passed`; Task 1 security re-review: APPROVE with no Critical or Important findings | Core is `fixed_pending_full_verify`: opaque one-time approval tokens are consumed before approval re-probe, the supplied/current key identities must match, trust writes fsync the file and POSIX parent directory, forwarding pins the fresh key and disables hidden SSH config reinterpretation, and target preflight uses `RejectPolicy`. UI approval paths remain, so TF-STATUS-095 stays open; the existing `TestType` collection warning is carried into Task 2. |
| 2026-07-15 | Product maturity proposal / TF-STATUS-094-101 | Eight role-specialized reviews and red-team consensus; source/status evidence scan; `.venv\Scripts\python.exe -m pytest tests\test_product_maturity_proposal.py tests\test_current_status_docs.py -q`; Edge headless screenshots at 1440x1100 and 390x844; Chrome DevTools Protocol viewport/overflow/image inspection; `git diff --check` | PASS: focused docs regression 75 passed; desktop and mobile documents reported no horizontal overflow and zero broken images; 13 report sections loaded | Produced `docs/product_maturity_proposal_2026-07-15.html`, corrected stale latest-release wording, closed TF-STATUS-094, and opened TF-STATUS-095 through TF-STATUS-101. Strategy is one Safety and Proof release followed by 3-5 observed disposable MySQL-over-SSH sessions; no implementation or full product test-suite completion is claimed by this documentation session. |
| 2026-07-15 | `v2.4.0` protected publication closure / TF-STATUS-093 | PR #247 hosted checks and merge; protected `create-release-tag.yml` run `29391247402`; approved `release.yml` run `29391317995`; annotated-tag object/peeled-commit inspection; draft asset/digest and checksum-sidecar inspection; stable/latest API and live `UpdateChecker` checks; post-release relay health and repository-secret inventory | runs `29390539762`, `29390540655`, and `29390539802` passed the required Python, Rust, version, support-tracking, and internal/external macOS arm64/x86_64 gates; PR #247 merged at `2026-07-15T05:21:06Z` as `bfee81613c7f77d96136346fa305858bf62670d7`; tag object `34a111cc90373a485c6dd168d4755f43cfccc768` peels to that exact commit; release run built all platforms and published at `2026-07-15T05:33:59Z`; all 10 expected assets have GitHub SHA-256 digests and all four macOS sidecars match their DMG/ZIP asset digests; `UpdateChecker` returned latest `2.4.0` with no error; relay health returned HTTP 200, schema 1, mode `active`; exactly `RELEASER_APP_ID` and `RELEASER_APP_PRIVATE_KEY` remain | `v2.4.0` is stable/latest at `https://github.com/sanghyun-io/tunnelforge/releases/tag/v2.4.0`. Release notes explicitly identify macOS artifacts as unsigned and unnotarized. TF-STATUS-093 is closed; TF-STATUS-090 and TF-STATUS-091 remain watch items. |
| 2026-07-15 | `2.4.0` release-candidate preparation / TF-STATUS-093 | RED/GREEN status and version sync; full Python; Rust regression/Cargo test+release; Worker test/typecheck/audit/dry-run; `build-installer.ps1 -Clean`; frozen UI/Rust Core and WebSetup self-checks; workflow pin scan; `git diff --check` | RED proved the source remained `2.3.1`; deterministic minor bump aligned all three sources at `2.4.0`; release-candidate full Python 2697 passed / 1 skipped / 4 warnings; Rust and Worker gates passed; all external actions in `version-gate.yml` are full-SHA pinned; `build-installer.ps1 -Clean` completed end to end and produced the main app, WebSetup, and `TunnelForge-Setup-2.4.0.exe`; Installer source unchanged; frozen checks passed | The initial local installer command exposed stale-output handling, missing bootstrapper creation after clean, tracked Inno-source rewriting, and Windows PowerShell 5.1 UTF-8 parsing problems. TDD added bootstrapper build/self-check, read-only version validation, and a UTF-8 BOM contract. A concurrent clean-build deleted one independent-review test's transient evidence ZIP; two focused reruns and a later standalone full suite passed. Protected version PR, exact-main tag, draft asset/digest inspection, and publication remain. |
| 2026-07-15 | TF-STATUS-092 protected merge closure | `gh pr checks 245 --watch`; `gh pr ready 245`; `gh pr merge 245 --merge`; post-merge PR/main ancestry, repository-secret inventory, and production health verification | fresh-head runs `29385868513` and `29385868516` passed all required gates; PR #245 merged at `2026-07-15T03:22:13Z` as `6dbcd51c8c60acef3569697fa79a9e6914a7c0e0`; `origin/main` has the expected two-parent merge ancestry; `GH_APP_PRIVATE_KEY` remains absent while exactly the two Releaser secrets remain; relay health returned schema 1 and mode `active` | TF-STATUS-092 is closed. The exposed Reporter key, its possible derived-token lifetime, the unused repository secret, client credential retirement, replacement canary/rollout, frozen package smoke, fresh-head hosted gates, and protected merge are all complete. |
| 2026-07-15 | TF-STATUS-092 exposed-key deletion, repository-secret containment, and protected PR gate | GitHub Developer Settings exact-fingerprint deletion and post-action key inventory; one-hour containment wait; `gh secret delete GH_APP_PRIVATE_KEY`; repository-secret name inventory; `dist\TunnelForge\TunnelForge.exe --ui-smoke-check`; `git push`; draft PR #245; `gh pr checks 245 --watch` | exposed Reporter fingerprint `SHA256:hLY6...nt+k=` deleted at `2026-07-15T02:06:03Z`; containment completed at `2026-07-15T03:06:03Z`; unused `GH_APP_PRIVATE_KEY` deleted at `2026-07-15T03:06:23Z`; only `RELEASER_APP_ID` and `RELEASER_APP_PRIVATE_KEY` remain; replacement `SHA256:6Yki...GYB4=` remains; frozen UI smoke returned `success=true` with bundled Rust Core service hello; commit `9367faa` pushed; PR #245 opened; hosted runs `29382607405` and `29382607434` passed Python, Rust, version, macOS support tracking, and both internal/external macOS arm64/x86_64 gates | The replacement Reporter key and separate Releaser lane were not changed. TF-STATUS-092 remains `fixed_pending_full_verify` until the final status commit, fresh-head CI, and protected PR completion are recorded. |
| 2026-07-15 | TF-STATUS-092 Task 13 canary, emergency off, active, and client binding | authenticated synthetic canary POSTs; GitHub issue/D1 inspection; `gh issue close 244`; Wrangler deploy/health/smoke for off and active; Cloudflare GraphQL `workersInvocationsAdaptive`; full Python/Rust/Worker/package gates | canary created and updated `https://github.com/sanghyun-io/tunnelforge/issues/244` with exact App attribution, fixed labels, one comment, one hidden fingerprint marker, and no forbidden credential/path pattern; emergency-off version `615088ce-96d6-406f-9d5a-d511675c70e6` passed; active version `9dbed64a-0d60-43bf-b946-24ab96e312f5` passed; full Python 2697 passed / 1 skipped; Rust gate, Cargo test/release build, Worker 316, typecheck, audit 0, dry-run, and PyInstaller passed | The first authenticated canary exposed a Workers-runtime incompatibility where `fetch(..., redirect:"error")` threw before GitHub; TDD changed GitHub requests to `redirect:"manual"`, preserving no-follow behavior, and explicit hostile-302 tests prove token and mutation requests stop after one fetch. Same-installation retry correctly returned `duplicate`; a new anonymous installation ID with the same server fingerprint produced the one intended `updated` recurrence. D1 has one complete create and one complete comment. The last-24-hour analytics window reported 61 successful invocations and zero errors; raw grouped `cpuTimeP50/P99` values ranged 213-15189, and the dataset exposed no cold/warm dimension, so no unsupported cold/warm or percentile claim is made. The exact client route is `https://tunnelforge-issue-relay.ppkimsanh.workers.dev/v1/reports`. Old-key deletion, the one-hour derived-token interval, and unused repository-secret deletion are now complete; final fresh-head hosted PR gates remain. |
| 2026-07-15 | TF-STATUS-092 Task 13 live D1, off, and shadow rollout | OAuth `wrangler whoami`; `wrangler d1 create/list/migrations apply/execute --remote`; Worker test/typecheck/audit/dry-run/deploy; Python/curl health probes; synthetic off/shadow smoke; D1 row-count query | D1 `tunnelforge-issue-relay` created in APAC and migration `0001_init.sql` applied; Worker 314 passed; typecheck passed; 0 vulnerabilities; off version `e837d9df-1043-4298-8864-5eac2246a7ac` and shadow version `e432cb75-97c8-48a1-8e99-6e3bbb088b87` deployed; exact health and both smoke modes passed; all three application tables remained at 0 rows | The live endpoint is `https://tunnelforge-issue-relay.ppkimsanh.workers.dev`. An immediate post-deploy PowerShell probe briefly returned Cloudflare 1042, then PowerShell, Python requests, curl, and Node converged on the exact 200 health response without a code/config change. Shadow uses no GitHub/D1 secrets or persistence. Secret upload, canary, emergency off, active promotion, client binding, and final hosted verification remain pending. |
| 2026-07-15 | TF-STATUS-092 Task 12 GitHub App inventory and overlapping-key creation | GitHub Developer Settings App/general/permissions/installations reads; local OpenSSL PKCS#1-to-PKCS#8 conversion, private-key validation, and derived public-fingerprint comparison | Reporter App ID `2735888` is installed only on `sanghyun-io/tunnelforge` with Issues read/write and Metadata read; Releaser is separate App ID `2927386`; replacement reporter fingerprint `SHA256:6Yki...GYB4=` exactly matches the locally derived fingerprint | At this overlap checkpoint, the exposed reporter key `SHA256:hLY6...nt+k=` remained present. Releaser fingerprint is `SHA256:6ACP...Unh0=` and was unaffected. Secret values and PEM contents were not written to chat, Git, status docs, or command output. The later rows record completed old-key and unused `GH_APP_PRIVATE_KEY` deletion after canary and containment evidence. |
| 2026-07-15 | TF-STATUS-092 reviewed branch publication | focused sanitizer pytest; whole-tree secret-shaped literal scan; clean squash from `origin/main`; `git push -u origin feat/anonymous-error-reporting-relay` | sanitizer 270 passed; GitHub Push Protection passed; remote branch `feat/anonymous-error-reporting-relay` created at `a9f3d08` | The first push was correctly blocked because intermediate commits contained secret-shaped synthetic Slack fixture literals. Raw literals were replaced with runtime-composed test values, focused tests passed, and the unpublished multi-commit history was preserved locally under `feat/anonymous-error-reporting-relay-history`. A clean single publish commit removed the obsolete intermediate literals without bypassing Push Protection. |
| 2026-07-15 | TF-STATUS-092 final local Tasks 1-11 integration and privacy review | `.venv\Scripts\python.exe -m pytest -q --tb=short`; `npm test -- --run --testTimeout=5000 --hookTimeout=5000`; `npm run typecheck`; `npm audit --omit=dev`; `npx wrangler deploy --dry-run`; `py -3.9 -m compileall -q ...`; independent SOL/Terra whole-branch reviews; `git diff --check` | full Python 2695 passed / 1 skipped / 4 existing warnings; final Worker 314 passed; typecheck passed; 0 vulnerabilities; dry-run/compile/diff passed; local Tasks 1-11 approved | Final review fixed three cross-task regressions: backup restore/recovery cannot re-enable revoked consent and fails closed when current privacy state is unreadable; config import strips source reporting state while preserving destination terminal state, prompt budget, claim, generation, and identity; recurrence comments count only unexpired completed actions even before cleanup. Tasks 12-13 remain owner/live pending. |
| 2026-07-15 | TF-STATUS-092 Task 11 relay security tests, synthetic smoke, and secure deployment runbook | `npm test -- --run`; `npm run typecheck`; `npm audit --omit=dev`; `npx wrangler deploy --dry-run`; local D1 migration/smoke; `.venv\Scripts\python.exe -m pytest tests\test_error_reporting_packaging.py -q`; repository secret scan; independent SOL/Terra reviews; `git diff --check` | final 311 passed; packaging 47 passed; security 5 passed; typecheck passed; 0 vulnerabilities; dry-run/local smoke passed; Task 11 quality approved; diff check passed | Hostile payloads, GitHub failures, Markdown/control input, and global mutation caps are covered without forbidden-value leakage. Smoke accepts endpoint/mode only, uses synthetic fixtures, rejects unsafe URLs, keeps remote active health-only, and has bounded process cleanup. The runbook documents GitHub App, local PKCS#8 conversion/deletion, Dashboard multiline PEM entry, one-line Wrangler secrets, D1, modes, canary, rollback, and credential deletion without secret arguments or chat pasting. |
| 2026-07-15 | TF-STATUS-092 Task 10 GitHub App authentication and safe issue upsert | focused auth/format/issue tests; `npm test -- --run`; `npm run typecheck`; `npm audit --omit=dev`; `npx wrangler deploy --dry-run`; independent SOL/Terra review loops; `git diff --check` | focused 43 passed; final 306 passed; typecheck passed; 0 vulnerabilities; dry-run passed; Task 10 quality approved; diff check passed | PKCS#8-only GitHub App JWT uses bounded RS256 claims and API version 2026-03-10. Installation tokens are repository/Issues-write scoped and cached safely. Fixed bounded issue content excludes client Markdown. Route/action leases, current-window mutation budgets, 401 retry budgets, duplicate route revalidation, same-installation closed/404 recovery, and unknown quarantine are covered. |
| 2026-07-14 | TF-STATUS-092 Task 9 D1 idempotency, quotas, rollout modes, and cleanup safety | `npm test -- --run`; `npm run typecheck`; `npm audit --omit=dev`; `npx wrangler deploy --dry-run`; local D1 migration and scheduled smoke; independent SOL/Terra review loops; `git diff --check` | final 259 passed; typecheck passed; 0 vulnerabilities; dry-run passed; local D1 smoke passed; Task 9 quality approved; diff check passed | Exact HTTPS endpoints reject before body or bindings; shadow stays edge-only; canary/active use atomic D1 budgets and route leases. Create uniqueness covers every action state. The named 100-row cleanup bound drains repeated batches without deleting a complete/unknown create guard while its route remains pending; ready and unknown routes are retained. |
| 2026-07-14 | TF-STATUS-092 Task 8 Cloudflare Worker validation and fail-closed free-text boundary | npm test -- --run; npm run typecheck; npm audit --omit=dev; npx wrangler deploy --dry-run; independent SOL/Terra review loops; git diff --check | final 198 passed; typecheck passed; 0 vulnerabilities; dry-run passed; Task 8 quality approved; diff check passed | The Worker uses a streamed 16 KiB cap, bounded duplicate-rejecting JSON parser, strict detached reconstruction, exact integer tokens, server-side SHA-256 fingerprint verification, fixed response bodies/counters, and no raw logging. Repeated adversarial review replaced the free-text regex grammar with a fail-closed boundary: the edge never reads or forwards sanitized_message, while preserving validated structured error and environment evidence. |
| 2026-07-14 | TF-STATUS-092 Task 7 desktop credential and direct-reporter retirement | `.venv\Scripts\python.exe -m pytest -q --tb=short`; focused retirement/CI/settings/package suites; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python -m PyInstaller tunnel-manager.spec --noconfirm`; `py -3.9 -m compileall -q ...`; independent SOL/Terra reviews; `git diff --check` | Retirement guard 31 passed; full 2672 passed / 1 skipped / 4 existing warnings; Rust release and PyInstaller builds passed; Task 7 quality approved; compile/diff checks passed | Removed desktop GitHub App auth, direct issue reporter, summary builder, PEM/client-secret examples, PyJWT/python-dotenv, and jwt/dotenv hidden imports. `requests`, About/download/update links, incident history, consent non-migration coverage, and separate `RELEASER_APP_PRIVATE_KEY` remain. Tracked desktop/build inputs are AST/text scanned; PEM/key and relay local-secret files remain ignored. |
| 2026-07-14 | TF-STATUS-092 Task 6 Settings UX, local preview, relay health, and last-attempt review | `.venv\Scripts\python.exe -m pytest -q --tb=short`; focused Settings/worker/update/i18n/consent/transport suites; offscreen full-dialog and live QThread probes; `py -3.9 -m py_compile ...`; independent SOL/Terra reviews; `git diff --check` | Final focused review 273 passed / 2 external warnings; full 2748 passed / 1 skipped / 4 existing warnings; Task 6 quality approved; compile/diff checks passed | The compact bilingual Settings group replaces client GitHub-App configuration. Explicit checkbox clicks alone mutate consent; preview performs no config write or network call; an unconfigured relay disables opt-in/health only; retained health workers handle startup, duplicate click, deletion, and cleanup safely; last-attempt display accepts only fixed status, UTC time, and canonical issue URL. |
| 2026-07-14 | TF-STATUS-092 Task 5 relay transport, Export/Import integration, and adversarial security review | `.venv\Scripts\python.exe -m pytest -q --tb=short`; focused transport/consent/sanitizer/config/Export/Import suites; `py -3.9 -m py_compile ...`; independent SOL/Terra review loops; `git diff --check` | Final focused sanitizer 270 passed; full 2729 passed / 1 skipped / 4 existing warnings; Task 5 quality approved; compile/diff checks passed | Submission fails closed without a current consent permit; origin requests exclude inherited auth, cookies, headers, and parameters while honoring environment proxy/CA settings. Strict wire, TLS, retry, response-size/encoding, worker lifecycle, configuration-import privacy, diagnostic redaction/control escaping, and bounded retention/collision behavior are covered. Final targeted SOL and Terra reviews approved the closure fixes. |
| 2026-07-14 | TF-STATUS-092 Task 4 consent dialog, deferred presentation, and lifecycle review | `.venv\Scripts\python.exe -m pytest tests\test_error_reporting_consent_dialog.py tests\test_main_window_error_reporting_consent.py tests\test_error_report_consent.py tests\test_migration_worker.py tests\test_oneclick_rust_core_gate.py tests\test_i18n.py -q`; focused shutdown/race selectors; `py -3.9 -m compileall -q ...`; independent Terra task reviews; `git diff --check` | Initial 87 passed; lifecycle fix 170 passed; race-fix RED 18 failed / 2 passed; final 188 passed; focused shutdown/race 9 passed; Task 4 quality approved; compile/diff checks passed | The primary instance schedules a modal consent prompt only while visible and idle, retries after modal or detached DB work, releases claims on dialog failures and shutdown, rejects stale outcomes, and never sends network traffic from the dialog. Bilingual disclosure covers every schema-v1 field and forbidden-data category. |
| 2026-07-14 | TF-STATUS-092 Task 3 atomic consent policy and concurrency review | `.venv\Scripts\python.exe -m pytest tests\test_config_manager.py tests\test_error_report_consent.py -q`; `.venv\Scripts\python.exe -m pytest tests\test_error_report_builder.py -q`; `py -3.9 -m compileall -q ...`; independent SOL/Terra task reviews; `git diff --check` | Initial RED import failure; review RED 28 failed / 35 passed; final 93 passed; builder 31 passed; Task 3 quality approved; compile/diff checks passed | Consent reads/transitions use coherent ConfigManager transactions; prompt displays are preclaimed at most twice; UUID claim tokens reject stale outcomes; every consent write preserves/repairs installation UUIDv4. Primary-process prompting depends on the established `SingleInstanceGuard`, and Task 4 owns the claim-token UI integration. |
| 2026-07-14 | TF-STATUS-092 Task 2 privacy-allowlisted report builder and security review | `.venv\Scripts\python.exe -m pytest tests\test_error_report_sanitizer.py tests\test_error_report_builder.py -q`; `.venv\Scripts\python.exe -m pytest tests\test_error_report_schema.py -q`; `py -3.9 -m compileall -q ...`; independent SOL/Terra task reviews; `git diff --check` | Initial RED import failures; security RED waves reached 17 failed / 147 passed and 7 failed / 164 passed; final 171 passed; schema 76 passed; Task 2 quality approved; compile/diff checks passed | All reported credential, Unicode/control splitting, escaped quote, host-role, path/CWD, SQL/DB object, and hostile-exception bypasses are covered. Collector stays local-only, fingerprint excludes message/installation ID, frames are application-only, and sanitizer intentionally removes Unicode Marks while preserving Korean/CJK base text. |
| 2026-07-14 | TF-STATUS-092 Task 1 report-contract implementation and review fixes | `.venv\Scripts\python.exe -m pytest tests\test_error_report_schema.py -q`; `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q`; independent SOL/Terra task reviews; `git diff --check` | RED: import failure, then 6 failed / 66 passed; GREEN: 72 passed; final 76 passed; status 70 passed; Task 1 quality approved; diff check passed | Draft 2020-12 and Python agree on mathematically integral JSON numbers and canonicalize them to `int`; every valid/invalid fixture executes against `schema.json`, invalid cases assert exact paths, redaction cases prove each forbidden value occurs in its input, and independent schema tests cover message 2,000/2,001 and frame 20/21 boundaries. `jsonschema` is dev-only. |
| 2026-07-14 | TF-STATUS-092 implementation planning and relay threat-model review | `docs/superpowers/plans/2026-07-14-anonymous-error-reporting.md`; independent relay architecture review; focused status TDD; `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q`; `git diff --check` | PLAN READY; 13 TDD tasks; status suite 70 passed; runtime implementation not started | The plan assigns `gpt-5.6-sol/terra/luna` by role, adds D1 global mutation budgets, pre-create leases, `unknown` GitHub timeout quarantine, strict 16 KiB validation, no raw report retention/logging, and measurable rollout gates. |
| 2026-07-14 | TF-STATUS-092 anonymous error-reporting and rolling-rotation design | Chat design review; `docs/superpowers/specs/2026-07-14-anonymous-error-reporting-design.md` placeholder/heading scan; `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q`; `git diff --check` | DESIGN ACCEPTED; implementation not started; status suite 69 passed; placeholder/diff scans passed | Fixes the client/credential boundary around a dedicated reporter App and Cloudflare Worker, strict client/server allowlists, D1 HMAC idempotency without raw payload retention, local-only collection, best-effort HTTPS transport, and off/shadow/canary/active modes. Consent is affirmative; the prompt appears initially and, after defer, once more after 30 days, at most twice total. No key, secret, Worker, GitHub App, or live service was changed. |
| 2026-07-13 | TF-STATUS-092 legacy GitHub App private-key rotation scope | Current and `v2.3.0` source/workflow scan; release-tag matrix scan; `v2.1.5` Actions log inspection; GitHub Actions secret-name inventory across 46 accessible `sanghyun-io` repositories; `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q`; `git diff --check` | HIGH exposure confirmed; rotation not yet performed; status suite 68 passed; diff check passed | App ID `2735888` release automation embedded the issue-reporter key in `v1.13.4` through `v2.3.0`: 43 affected tags and 42 published releases, with `v2.1.4` being the only affected tag without a published release. The `v2.1.5` build log directly confirms private-key embedding; the full release range is a high-confidence inference from the identical embedding workflow and successful release records, not individual reverse engineering of all 42 binaries. `v2.3.1` has no embedded-key support and its release workflows do not read `GH_APP_PRIVATE_KEY`. Exact GitHub App/Releaser secret names were found only in `sanghyun-io/tunnelforge`; external secret stores, local PEM copies, installation scope, and differently named copies remain unresolved. |
| 2026-07-13 | `v2.3.1` protected release publication / TF-STATUS-081 closure | PR #240 checks and merge; tag workflow `29233663954`; Build and Release workflow `29233708190`; annotated tag API read; `gh release view v2.3.1`; release asset/digest assertion | PASS | PR #240 merged at `b80e15c6148ba19a357a84b4e9e6cee8ae0b4727`. The approved tag workflow created immutable annotated `v2.3.1` at that commit. Release preflight, Windows build, unsigned macOS arm64/x86_64 build/package/smoke, artifact normalization, and draft creation passed. The published latest stable release has 10 release assets, all GitHub SHA-256 digests present, at `https://github.com/sanghyun-io/tunnelforge/releases/tag/v2.3.1`. |
| 2026-07-13 | Accepted unsigned macOS release policy | credential matrix and workflow focused tests; standalone full Python suite; Cargo baseline; security re-review; `git diff --check` | PASS | RED produced the expected workflow contract failure and missing credential-classifier import. GREEN: credential/workflow focused 23 passed; final full Python 2038 passed / 1 skipped / 4 warnings in 60.14s; Cargo 216 + 2 JSONL + 9 live + 2 stress passed / 1 ignored. All Apple values absent selects unsigned; any partial configuration fails closed; complete required credentials select signed. Security re-review: SECURE. |
| 2026-07-13 | TF-STATUS-083/089 live closure | PR #240 checks; runs `29229463468` and `29229463485`; live main protection, `production-release` Environment, and ruleset API reads | PASS | Python regression, Rust Core regression, terminal `version-gate`, macOS tracking, and both macOS arm64/x86_64 validation surfaces passed. PR #240 is `CLEAN`/mergeable. Main protection is strict, admin-enforced, conversation-gated, and requires five stable checks; release Environment approval/admin-bypass/ref restrictions and immutable `v*` update/delete rules are active. |
| 2026-07-13 | PR #240 hosted regression failure remediation | PR runs `29228401540`, `29228401548`, and `29228414876` failure logs; i18n leak reproduction; focused app/cross-engine/i18n and packaging tests; standalone full Python suite; PyInstaller build; frozen `--ui-smoke-check`; `git diff --check` | LOCAL PASS, replacement hosted run pending | Hosted logs showed English Qt state leaking from UI smoke into later tests and `ModuleNotFoundError: No module named 'src.ui'` in both macOS architectures. RED reproduced the translated dialog and missing spec collection. GREEN: focused 79 + 14 passed, full Python 2028 passed, 1 skipped, 4 warnings in 59.79s, PyInstaller build passed, and frozen smoke returned `success=true` with bundled Rust Core service hello. |
| 2026-07-13 | TF-STATUS-089 release approval boundary implementation and live controls | release/CI focused suite; GitHub App auth focused suite; standalone full Python suite; Rust regression/Cargo test/release build; GitHub Environment, branch protection, and tag ruleset API reads; `git diff --check` | PASS with external blockers recorded | Workflow: 64 passed in 47.39s. GitHub auth/settings: 114 passed in 7.60s. Final full Python: 2028 passed, 1 skipped, 4 warnings in 61.83s. Rust regression, Cargo 216 + 2 JSONL + 9 live + 2 stress / 1 ignored, and release build passed. `production-release`, strict main protection, and immutable `v*` update/delete rules are live. Apple secrets, an independent maintainer, PR Actions, and real-Mac evidence remain pending. |
| 2026-07-13 | TF-STATUS-088 final status-document verification | `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q`; `git diff --check` | PASS, 65 passed, exit 0; diff check pass | Confirms the CI trust-boundary issue lifecycle and code baseline `9088aab`. |
| 2026-07-13 | TF-STATUS-088 version-gate trust-boundary closure | CI workflow focused suite; update/security/status/version focused suite; standalone full Python suite; `git diff --check` | PASS | CI workflow: 9 passed. Focused: 319 passed, 1 skipped in 48.27s. Full Python: 2031 passed, 1 skipped, 4 warnings in 60.28s. Commit-message bypass is removed, three real version files are checked against the trusted expected value, and the App token action is commit-pinned. |
| 2026-07-13 | TF-STATUS-086 cancelled-error scheduling follow-up | cancelled-DownloadError RED/GREEN regression; bootstrapper suite; update/security/status/version focused suite; standalone full Python suite; rebuilt frozen WebSetup self-check; `git diff --check` | PASS | Bootstrapper: 74 passed. Focused: 318 passed, 1 skipped in 50.65s. Full Python: 2030 passed, 1 skipped, 4 warnings in 62.49s. Frozen self-check emitted `TUNNELFORGE_WEBSETUP_SELF_CHECK_OK`. Error UI scheduling rejected by a destroyed root retires only after confirmed cancellation; non-cancellation errors remain visible. |
| 2026-07-13 | TF-STATUS-086 destroyed-root scheduling follow-up | destroyed-root RED/GREEN regression; bootstrapper suite; update/security/status/version focused suite; standalone full Python suite; rebuilt frozen WebSetup self-check; `git diff --check` | PASS | Bootstrapper: 73 passed. Focused: 317 passed, 1 skipped in 50.81s. Full Python: 2029 passed, 1 skipped, 4 warnings in 62.51s. Frozen self-check emitted `TUNNELFORGE_WEBSETUP_SELF_CHECK_OK`. A destroyed Tk root after confirmed cancellation is treated as normal retirement without recursive UI error scheduling. |
| 2026-07-13 | TF-STATUS-086 lock-inversion and TF-STATUS-087 Linux follow-up | focused bootstrapper/settings/platform suite; standalone full Python suite; rebuilt frozen WebSetup self-check; `git diff --check` | PASS | Focused: 107 passed in 3.22s. Full Python: 2028 passed, 1 skipped, 4 warnings in 64.28s. Frozen self-check emitted `TUNNELFORGE_WEBSETUP_SELF_CHECK_OK`. Tk scheduling occurs outside the state lock, queued callbacks retire when abandoned, and all non-Windows platforms use reveal-only UI wording. |
| 2026-07-13 | TF-STATUS-086/087 final status-document verification | `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q`; `git diff --check` | PASS, 64 passed, exit 0; diff check pass | Confirms both final review follow-up lifecycles and current code baseline `77b0d31`. |
| 2026-07-13 | TF-STATUS-086 callback-retirement follow-up and TF-STATUS-087 wording alignment | queued-completion RED/GREEN regression; Windows lease test isolation; update/security/status/version focused suite; standalone full Python suite; rebuilt frozen WebSetup self-check; focused settings/bootstrapper suite; `git diff --check` | PASS | Bootstrapper: 72 passed. Settings/bootstrapper: 100 passed. Focused: 315 passed, 1 skipped in 48.24s. Full Python: 2027 passed, 1 skipped, 4 warnings in 58.74s. Frozen self-check emitted `TUNNELFORGE_WEBSETUP_SELF_CHECK_OK`. The already queued completion callback now retires after confirmed cancellation; non-Windows UI says reveal-only/no app exit. |
| 2026-07-13 | TF-STATUS-086 final status-document verification | `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q`; `git diff --check` | PASS, 63 passed, exit 0; diff check pass | Confirms the new issue lifecycle, `544c6b0` code baseline, verification evidence, execution order, and session record. |
| 2026-07-13 | TF-STATUS-086 confirmed-cancel result-publication race | RED focused race test; bootstrapper focused suite; update/security/status/version focused suite; standalone full Python suite; rebuilt frozen WebSetup self-check; `git diff --check` | PASS | RED reproduced zero discard calls after confirmed cancellation. GREEN bootstrapper: 71 passed. Focused: 313 passed, 1 skipped in 47.39s. Full Python: 2025 passed, 1 skipped, 4 warnings in 57.98s. Frozen self-check emitted `TUNNELFORGE_WEBSETUP_SELF_CHECK_OK`. The earlier parallel full-suite failure was a verification-process collision on a shared macOS-test log; the standalone rerun passed. |
| 2026-07-13 | TF-STATUS-085 final status-document verification | `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q`; `git diff --check` | PASS, 62 passed, exit 0; diff check pass | Confirms the closed tracker state, `87d9021` code baseline, verification log, recommended order, and session record after the status-only close commit. |
| 2026-07-13 | TF-STATUS-085 final broad verification | verified code baseline `87d9021`; focused and full Python suites; Rust Core regression gate; Cargo test/release build; PyInstaller frozen WebSetup build and self-check; version-sync; `git diff --check` | PASS | Focused: 311 passed, 1 skipped in 55.41s. Full Python: 2023 passed, 1 skipped, 4 warnings in 58.64s. Cargo: 216 lib, 2 JSONL CLI, 9 live, 2 stress passed / 1 ignored. Release build: 0.30s. Frozen self-check emitted `TUNNELFORGE_WEBSETUP_SELF_CHECK_OK`. Version sync: 1 passed in 0.09s. TF-STATUS-085 is closed locally without claims about live Actions, branch protection, tag/release, GitHub closure, or Mac hardware. |
| 2026-07-13 | TF-STATUS-085 Final Fix F2 focused verification | `python -m py_compile src/ui/dialogs/settings.py bootstrapper/bootstrapper.py tests/test_update_downloader.py tests/test_bootstrapper_integrity.py tests/test_settings_update_launch.py`; `$env:QT_QPA_PLATFORM='offscreen'; pytest tests/test_update_downloader.py tests/test_bootstrapper_integrity.py tests/test_settings_update_launch.py tests/test_i18n.py tests/test_current_status_docs.py -q`; `git diff --check` | PASS | 253 passed, 1 skipped, 2 warnings in 2.79s. Covers Windows identity-conditional cleanup, POSIX residue retention assertions, deterministic macOS reveal-only behavior, pre-dispatch abandonment cleanup, generic launch-failure retention, docs, i18n, and status. |
| 2026-07-13 | TF-STATUS-079/084 final security and release baseline refresh | verified RC code baseline `e37f57adfd5053b6a5c8343d9ff7c36f8f4425bd`; focused security/status/version command; full Python suite; Rust Core regression gate; Cargo test; release build; version-sync; `git diff --check` | PASS | Focused: 291 passed, 1 skipped in 46.01s. Full Python: 2006 passed, 1 skipped, 4 warnings in 58.07s. Cargo: 216 lib, 2 JSONL CLI, 9 live, 2 stress passed / 1 ignored. Release build: 2.82s. Version sync: 1 passed in 0.08s. Fix E secure child creation/name validation and bootstrapper cancel-before-entry evidence support TF-STATUS-084 closed; TF-STATUS-079 remains closed and other statuses are unchanged. |
| 2026-07-13 | Final current-status documentation verification | .venv\\Scripts\\python.exe -m pytest tests\\test_current_status_docs.py -q; git diff --check | PASS, 61 passed in 0.36s; diff check pass | Fresh current-status tests and the final diff check passed after preserving historical 1827, 1870, and 1955 snapshots. |
| 2026-07-13 | TF-STATUS-084 final status-suite record | `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q` | PASS, 60 passed in 0.21s | Canonical final status-suite result. The earlier `60 passed in 0.33s` row records a separate post-close run and remains preserved in chronological order. |
| 2026-07-10 | TF-STATUS-084 post-close status consistency | RED then GREEN: `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py -q` | PASS, 60 passed in 0.33s | RED was 1 failed, 59 passed in 0.39s because `test_current_status_records_231_release_candidate_verification_evidence` still expected the superseded current `1870` count. Updated it to assert the fresh `1955` final-review evidence; no full pytest rerun was performed, preserving the required exactly-one full-suite run. |
| 2026-07-10 | TF-STATUS-084 final-review boundary verification | RED: `.venv\Scripts\python.exe -m pytest tests\test_current_status_docs.py::test_current_status_tracks_final_review_update_boundary_pending_verification -q`; GREEN: focused update/security/status/version pytest, exactly one `pytest -q`, Rust Core gate, Cargo test/build, version-sync, and final diff check | PASS, all local gates exit 0 | RED was 1 failed in 0.24s before TF-STATUS-084 existed. GREEN focused suite: 183 passed, 1 skipped in 1.63s; full suite: 1955 passed, 1 skipped, 4 warnings in 60.38s; Rust gate: 1.4s; Cargo test: 216 lib, 2 JSONL CLI, 9 live, 2 stress passed / 1 ignored in 14.1s; release build: 4.25s; version-sync: 1 passed in 0.09s. TF-STATUS-084 is closed locally without claims about Actions, branch protection, tags/releases, GitHub closure, or Mac hardware. |
| 2026-07-10 | Task 6 review follow-up: historical-versus-RC status evidence | RED then GREEN: `pytest tests\test_current_status_docs.py tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q` | PASS, 60 passed in 0.25s | RED had 2 failures in 0.44s: the Round 3 `1827 passed / 6 warnings` baseline snapshot was no longer asserted/present, and Session Log had two delimiter rows. Restored the historical assertion, split `1870 passed / 4 warnings` plus Rust evidence into a dedicated RC test, preserved both records, and removed the duplicate delimiter. |
| 2026-07-10 | `2.3.1` release-candidate status and version finalization | RED: `pytest tests\test_current_status_docs.py tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q`; GREEN: same focused command after `.venv\Scripts\python.exe scripts\bump_version.py --bump-type patch`; `$env:PYTHONUTF8='1'; $env:QT_QPA_PLATFORM='offscreen'; pytest -q`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; version-sync pytest; `git diff --check` | PASS, all exit 0 | RED: 1 failed, 57 passed in 0.43s because source was `2.3.0`; bump emitted `new_version=2.3.1`; GREEN: 58 passed in 0.26s. Full Python: 1870 passed, 4 warnings in 60.08s. Rust gate: 1.4s; Cargo test: 216 lib, 2 JSONL CLI, 9 live, 2 stress passed / 1 ignored in 4.1s; release build: 36.61s; version-sync: 1 passed in 0.08s; diff check: 0.5s. The handoff records GitHub Release asset `digest` verification, unknown-environment confirmation, `python-regression`, the bilingual Schedule correction, and the `2.3.1` release candidate. |
| 2026-07-10 | Role-specialized strategy, release, and security review | six role-specific read-only repository reviews plus cross-critique; `python scripts\check-macos-support-gate.py --final`; `git rev-list --left-right --count v2.3.0...HEAD`; `gh api repos/sanghyun-io/tunnelforge/branches/main/protection/required_status_checks`; focused source tracing for updater execution and ProductionGuard; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `git diff --check` | PASS with expected macOS final-gate failure | Confirmed TF-STATUS-079 through TF-STATUS-083 and refreshed TF-STATUS-008. Current-status tests passed at 56 passed; full Python suite passed at 1827 passed / 6 warnings. The final macOS gate fails only for missing current-HEAD manual workflow evidence and the real-Mac report. |
| 2026-07-10 | Round 3 completion and open-issue reconciliation | `git status --short --branch`; `git rev-list --left-right --count origin/main...main`; `gh issue list --state open --limit 30 --json number,title,updatedAt,url`; `gh issue view 170 --json ...`; `git branch --contains a4c7a06`; `git merge-base --is-ancestor a4c7a06 HEAD`; `gh pr view 171 --json ...`; `git tag --contains a4c7a06`; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `git diff --check` | PASS | Round 3 remains complete and pushed at `09ab060`. GitHub #170 is still open, but PR #171 fixed its ERROR 3780 path, the fix is in current `main`, and release tags from `v2.1.8` through `v2.3.0` contain it. Remaining #170 work is issue confirmation/closure, not implementation. Current-status tests passed at 55 passed; full Python suite passed at 1826 passed / 6 warnings. |
| 2026-07-09 | Clean Code Round 3 UI/dialog/main-window integration | `python -m py_compile` on all Round 3 production Python files; `pytest` focused Round 3 suite; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; custom whole-tree `MySQLConnector` allowlist scan; `git diff --check HEAD~8..HEAD`; `pytest -q` | PASS | Integrated WP-3.1 through WP-3.8 as behavior-preserving commits. Focused Round 3 tests passed at 491 passed / 2 warnings, Rust Core regression gate passed, allowlist scan found 22 product imports with no missing entries, and full Python suite passed at 1819 passed / 4 warnings. |
| 2026-07-09 | Clean Code Round 3 red-review follow-up | RED/GREEN: migration worker constructor compatibility and cleanup dry-run rejection tests; Fix Wizard dialog re-export regression test; `python -m py_compile src/ui/workers/migration_worker.py src/ui/dialogs/fix_wizard_dialog.py tests/test_migration_worker.py tests/test_fix_wizard_dialog.py`; `pytest tests/test_migration_worker.py tests/test_fix_wizard_dialog.py tests/test_migration_fix_wizard.py tests/test_fix_wizard_sql_helpers.py -q`; `pytest -q` | PASS | Red-review found behavior-preserving compatibility regressions in `MigrationAnalyzerWorker` legacy `check_*` kwargs, `CleanupWorker(dry_run=False)` fail-closed semantics, and `fix_wizard_dialog` module re-exports. All three were restored; focused tests passed at 118 passed and full Python suite passed at 1821 passed / 4 warnings. |
| 2026-07-09 | Clean Code Round 3 SECURE/APPROVE follow-up | RED/GREEN: `pytest tests\test_cross_engine_migration_protocol.py::test_db_core_frozen_candidate_dirs_exclude_cwd tests\test_cross_engine_migration_protocol.py::test_db_core_executable_does_not_use_path_lookup_without_dev_flag tests\test_migration_result_store.py::test_migration_result_store_auto_save_sanitizes_schema_path_components tests\test_fix_wizard_dialog.py::test_execution_page_auto_saved_rollback_stays_inside_rollback_dir -q`; focused: `pytest tests\test_cross_engine_migration_protocol.py tests\test_migration_result_store.py tests\test_fix_wizard_dialog.py tests\test_rust_core_packaging.py tests\test_path_safety.py -q`; focused review/security suite: `pytest tests\test_migration_worker.py tests\test_migration_fix_wizard.py tests\test_fix_wizard_sql_helpers.py tests\test_rust_dump_exporter.py tests\test_db_import_dialog.py tests\test_cross_engine_migration_worker.py tests\test_sql_editor_editability.py tests\test_sql_execution_worker.py -q`; `python -m py_compile` on touched Python files; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; custom product-import `MySQLConnector` allowlist scan; `git diff --check` | PASS | SECURE review found two medium issues: frozen/helper lookup could fall through to untrusted locations and schema-derived auto-save names could escape intended directories. Both are fixed with focused regression coverage. Post-fix APPROVE also restored the missing `BatchOptionDialog` legacy re-export. macOS packaging bash tests now use the discovered Git Bash path and pass at 51 passed. Full Python suite passed at 1824 passed / 4 warnings; allowlist scan found 22 product imports with no missing entries. |
| 2026-06-27 | macOS final validation tooling recheck | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_macos_final_validation_tooling_recheck -q`; `bash -n scripts/macos-manual-validation-report.sh scripts/macos-download-validation-artifacts.sh scripts/validate-macos-release.sh scripts/build-macos.sh scripts/package-macos.sh`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | #116 final validation tooling remains ready from the repository side: shell syntax is valid, macOS focused tests pass at 53, the normal gate passes, and the final gate accepts current-head manual workflow evidence while failing only for missing real-Mac report evidence under `build/`. |
| 2026-06-27 | Post-#169 next issue re-audit | `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 30 --json number,title,state,url,labels,updatedAt`; `gh issue view 116 --comments --json number,title,state,body,labels,comments,updatedAt,url`; `rg -n "TODO\|FIXME\|XXX\|HACK\|NotImplemented\|raise NotImplementedError\|pass\s*$" src tests scripts docs README.md README.ko.md SCHEDULE.md`; `rg -n "not yet supported\|pending\|future\|disabled\|hidden\|preview\|manual\|not implemented\|unsupported\|준비\|미지원\|비활성\|숨김\|수동\|TODO" docs README.md README.ko.md SCHEDULE.md src tests`; `rg -n "pymysql\|psycopg\|mysql\.connector\|mysqldump\|pg_dump\|mysqlpump\|mysqlimport\|\bpsql\b\|mysqlsh\|dump tool\|external dump\|shell export\|shell import" src scripts tests docs README.md README.ko.md BUILD.md SCHEDULE.md`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_169_next_issue_reaudit -q`; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | EXPECTED FAIL for `--final` only | #116 remains the only open GitHub issue and still requires external real-Mac report evidence. The re-audit found no new repo-side issue; Rust Core-shaped connector calls route through `RustDbConnection`/`RustDbCursor`, and the lone `psql` hit is Docker live evidence seeding rather than an active export/import dump path. |
| 2026-06-27 | Superseded missing manual workflow Summary cleanup | `gh issue create` created #169; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_summary_does_not_keep_superseded_missing_manual_workflow_wording -q`; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | EXPECTED FAIL for `--final` only | GitHub #169 is fixed: Summary no longer presents older missing current-head manual workflow evidence as current state; the current #116 blocker remains missing real-Mac report evidence |
| 2026-06-27 | Focused final-gate failure reason refresh | `gh issue create` created #168; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_focused_final_gate_reason_matches_current_workflow_evidence -q`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | GitHub #168 is fixed: the current focused verification row now matches final-gate output after current-head workflow evidence refresh, so the only current final-gate failure reason is missing real-Mac manual validation report under `build/` |
| 2026-06-27 | Non-self-stale macOS workflow evidence policy | `gh issue create` created #167; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_non_self_stale_macos_workflow_evidence_policy -q` | PASS | GitHub #167 is fixed: current-status summary now treats exact current-head manual workflow run IDs/SHAs as non-durable after status-only commits and points to GitHub #116 comments plus `scripts\check-macos-support-gate.py --final` as authoritative current-head evidence |
| 2026-06-27 | Manual macOS workflow evidence refresh | `gh workflow run "macOS App Validation" --ref main`; `gh run watch 28264164795 --interval 30 --exit-status`; `gh run view 28264164795 --json status,conclusion,headSha,event,workflowName,url,createdAt,updatedAt,jobs`; `python scripts\check-macos-support-gate.py --final`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_manual_macos_workflow_evidence -q` | EXPECTED FAIL for `--final` only | Manual `macOS App Validation` workflow_dispatch run `28264164795` passed for then-current main HEAD `6ad09590bf14d678a568fd64ac74765fd1eff0c9`, including arm64 and x86_64. Final gate accepted that workflow evidence for that HEAD and failed only because no real-Mac manual validation report was present under `build/`; rerun after status-only commits if main advances. |
| 2026-06-27 | Post-#166 next issue re-audit | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_166_next_issue_reaudit -q`; `git status --short --branch`; `gh issue list --state open --limit 30`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; Rust Core baseline and stale handoff scans | EXPECTED FAIL for `--final` only | `main` was aligned with `origin/main`; #116 is the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for current merged main HEAD, so no new repo-side implementation issue was created. |
| 2026-06-27 | Scheduled backup tuple connection info | RED/GREEN: `pytest tests\test_scheduler.py::TestBackupScheduler::test_backup_task_accepts_tuple_connection_info_for_rust_dump -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_scheduled_backup_tuple_connection_issue -q`; `gh issue create` created #166 | PASS | GitHub #166 is fixed: scheduled Rust dump backups now accept real `TunnelEngine.get_connection_info()` tuple output and resolve DB credentials outside the connection-info tuple before creating `RustDumpConfig` |
| 2026-06-27 | Scheduled backup PostgreSQL engine | `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 30`; `rg -n "_execute_backup\|RustDumpConfig\|db_engine\|get_connection_info\|tunnel_configs\|export_full_schema\|export_tables" src\core\scheduler.py tests\test_scheduler.py`; `gh issue create` created #165; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_scheduled_backup_postgresql_engine_issue -q`; RED/GREEN: `pytest tests\test_scheduler.py::TestBackupScheduler::test_backup_task_preserves_postgresql_engine_for_rust_dump -q` | PASS | GitHub #165 is fixed: scheduled Rust dump backups now normalize tunnel `db_engine` metadata and pass it into `RustDumpConfig`, preserving PostgreSQL while keeping the MySQL default fallback |
| 2026-06-27 | PostgreSQL dump wrapper engine | RED/GREEN: `pytest tests\test_rust_dump_exporter.py -q -k "wrapper_preserves_postgresql_engine"`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_dump_wrapper_engine_issue -q`; `gh issue create` created #164; `pytest -q` | PASS | GitHub #164 is fixed: `export_schema`, `export_tables`, and `import_dump` convenience wrappers preserve PostgreSQL engine into `RustDumpConfig`; full-suite count is superseded by TF-STATUS-067 |
| 2026-06-27 | PostgreSQL Import timezone Core validation | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_mysql_and_postgresql_timezone_forms --lib`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_import_timezone_core_validation_issue -q`; `gh issue create` created #163; `pytest -q`; `cargo test --manifest-path migration_core\Cargo.toml` | PASS | GitHub #163 is fixed: Rust Core `dump.import` accepts PostgreSQL `SET TIME ZONE` timezone SQL as well as MySQL `SET SESSION time_zone`, while preserving single-statement and safe-literal validation; full-suite count is superseded by TF-STATUS-066 |
| 2026-06-27 | PostgreSQL Import timezone SQL | RED/GREEN: `pytest tests\test_db_dialogs.py -q -k "postgresql_import_auto_timezone or postgresql_import_forced_kst"`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_import_timezone_issue -q`; i18n regression: `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_db_dialogs.py -q -k "direct_hardcoded or postgresql_import_auto_timezone or postgresql_import_forced_kst"`; `gh issue create` created #162; `pytest -q` | PASS | GitHub #162 is fixed: PostgreSQL dump import default auto timezone mode skips MySQL timezone table detection and sends no MySQL timezone correction SQL; forced KST/UTC use PostgreSQL `SET TIME ZONE`; full-suite count is superseded by TF-STATUS-065 |
| 2026-06-27 | PostgreSQL Rust dump endpoint engine | RED/GREEN: `pytest tests\test_rust_dump_exporter.py::TestRustDumpConfig::test_config_preserves_postgresql_engine tests\test_rust_dump_exporter.py::TestRustDumpExporter::test_export_full_schema_preserves_postgresql_engine_in_rust_payload tests\test_rust_dump_exporter.py::TestRustDumpImporter::test_import_dump_preserves_postgresql_engine_in_rust_payload -q`; RED/GREEN: `pytest tests\test_db_dialogs.py::test_preselected_export_tunnel_uses_postgres_connector_for_postgresql tests\test_db_dialogs.py::test_export_dialog_uses_direct_connector_host_for_rust_dump tests\test_db_dialogs.py::test_import_dialog_uses_direct_connector_host_for_rust_dump -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_rust_dump_engine_issue -q`; `gh issue create` created #161; `pytest -q` | PASS | GitHub #161 is fixed: `RustDumpConfig` preserves `engine`, PostgreSQL Export/Import dialogs pass `PostgresConnector.engine`, preselected PostgreSQL tunnels construct `PostgresConnector`, and Rust Core `dump.run`/`dump.import` payloads use `postgresql` endpoints; full-suite count is superseded by TF-STATUS-064 |
| 2026-06-27 | Partial export FK parent resolution | RED/GREEN: `pytest tests\test_rust_dump_exporter.py::TestRustDumpExporter::test_export_tables_resolves_fk_parents_through_rust_schema_inspect -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_partial_export_fk_parent_rust_inspect_issue -q`; `gh issue create` created #160; `pytest tests\test_rust_dump_exporter.py -q`; `pytest -q` | PASS | GitHub #160 is fixed: `RustDumpExporter.export_tables(... include_fk_parents=True)` now uses Rust Core-owned `schema.inspect` to include transitive FK parent tables before `dump.run`, without instantiating Python `MySQLConnector`; full-suite count is superseded by TF-STATUS-063 |
| 2026-06-27 | Current-status baseline provenance refresh | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_baseline_provenance_uses_latest_status_update -q`; `gh issue create` created #159; `pytest tests\test_current_status_docs.py -q`; `pytest -q` | PASS | GitHub #159 is fixed: top current-status baseline provenance now refers to the latest status update instead of stale post-#156 wording; full-suite count is superseded by TF-STATUS-062 |
| 2026-06-27 | SQL dollar quote helper None input guard | RED/GREEN: `pytest tests\test_sql_execution_worker.py::test_dollar_quote_reader_fails_closed_for_none_sql_text -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_dollar_quote_none_input_issue -q`; `gh issue create` created #158; `pytest tests\test_sql_execution_worker.py tests\test_sql_editor_dialog.py tests\test_scheduler.py -q`; `pytest -q` | PASS | GitHub #158 is fixed: `read_dollar_quote(None, 0)` and `SQLExecutionWorker._read_dollar_quote(None, 0)` now fail closed with `""` instead of raising `TypeError`; full-suite count is superseded by TF-STATUS-061 |
| 2026-06-27 | One-Click readiness next-action wording cleanup | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py::test_oneclick_readiness_does_not_present_closed_issues_as_current_tracking -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_oneclick_next_action_wording_issue -q`; `gh issue create` created #157; `pytest -q` | PASS | GitHub #157 is fixed: `docs/oneclick_readiness.md` now frames additional One-Click automatic-fix work as standing policy/watch guidance instead of a current `Recommended next repo-side change`; full-suite count is superseded by TF-STATUS-060 |
| 2026-06-27 | Post-#156 main merge and next issue analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_156_next_issue_analysis -q`; `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 30 --json number,title,labels,url,updatedAt`; `gh issue view 116 --json number,title,state,labels,body,comments,url,updatedAt`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; `pytest -q` | EXPECTED FAIL for `--final` only | `main` was already aligned with `origin/main`; #116 is still the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for current merged main HEAD, so no new repo-side implementation issue was created. Full-suite count is superseded by TF-STATUS-060 |
| 2026-06-27 | SQL dollar quote helper guard | RED/GREEN: `pytest tests\test_sql_execution_worker.py::test_dollar_quote_reader_fails_closed_for_out_of_range_starts -q`; `gh issue create` created #156; `pytest tests\test_sql_execution_worker.py tests\test_sql_editor_dialog.py tests\test_scheduler.py -q`; `python -m compileall -q src\core\sql_statement_parser.py tests\test_sql_execution_worker.py`; `git diff --check` | PASS | GitHub #156 is fixed: `read_dollar_quote` and `SQLExecutionWorker._read_dollar_quote` return `""` for empty SQL text or out-of-range start offsets |
| 2026-06-27 | SQL statement parser mismatch fix | RED/GREEN: `pytest tests\test_sql_editor_dialog.py::test_split_queries_preserves_comments_dollar_quotes_and_delimiters tests\test_sql_editor_dialog.py::test_get_query_at_cursor_uses_statement_parser_ranges -q`; RED/GREEN: `pytest tests\test_scheduler.py::TestBackupScheduler::test_parse_sql_queries_preserves_comments_dollar_quotes_and_delimiters -q`; `pytest tests\test_sql_editor_dialog.py tests\test_scheduler.py tests\test_sql_execution_worker.py -q`; `pytest -q` | PASS | GitHub #155 is fixed: SQL file execution, SQL Editor execute-all/current-query, and scheduled SQL now share `src/core/sql_statement_parser.py`; SQL Editor current-query lookup uses parser ranges via `find_sql_statement_at_position` |
| 2026-06-27 | SQL statement parser mismatch analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_sql_statement_parser_mismatch_issue -q`; `git fetch --all --prune`; `git status --short --branch`; `gh issue list --state open --limit 30`; `gh issue view 116 --json ...`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; direct parser comparison; `gh issue create` created #155 | EXPECTED FAIL for `--final` only | `main` is aligned with `origin/main`; #116 remains external real-Mac evidence work; GitHub #155 now tracks the confirmed repo-side mismatch where SQL Editor/Scheduler quote-only splitting diverges from the robust SQL file execution parser |
| 2026-06-27 | Call-local Rust cursor affected-row metadata | RED/GREEN: `pytest tests\test_db_core_service.py::test_rust_db_cursor_rowcount_uses_call_local_rows_affected -q`; `gh issue create` created #154; focused DB core/current-status pytest; `pytest -q` | PASS | GitHub #154 is fixed: `RustDbCursor.rowcount` uses call-local `execute_on_connection_result` metadata instead of shared facade state; the then-current 1839-test suite evidence is superseded by TF-STATUS-056 |
| 2026-06-27 | Rust Core DML affected row counts | RED/GREEN: `pytest tests\test_db_core_service.py::test_rust_db_cursor_rowcount_uses_core_rows_affected_for_dml -q`; RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml query_result_includes_non_row_rows_affected --lib`; `gh issue create` created #153; focused Python/Rust query result tests; `cargo test --manifest-path migration_core\Cargo.toml`; `pytest -q` | PASS | GitHub #153 is fixed: Rust Core query execution carries `rows_affected` metadata and `RustDbCursor.rowcount` preserves it for scheduled SQL and SQL editor DML reporting; the then-current 1839-test suite evidence is superseded by TF-STATUS-056 |
| 2026-06-27 | Post-#151 full-suite evidence refresh | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_151_full_pytest_refresh_issue tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count tests\test_current_status_docs.py::test_current_status_does_not_describe_stale_full_pytest_count_as_current -q`; `gh issue create` created #152; `pytest -q` | PASS | GitHub #152 is fixed: the suite evidence was refreshed to 1839 tests, stale 1832/1834/1835/1837-count wording cannot return as current evidence, and the count is now superseded by TF-STATUS-056 |
| 2026-06-27 | Post-#151 main merge and next issue analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_151_next_issue_analysis -q`; `git fetch --all --prune`; `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 20`; `gh issue view 116 --json number,title,state,labels,milestone,updatedAt,url,body`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | `main` was aligned with `origin/main` before this status update, and this status update was pushed to `origin/main`; #116 is still the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for current merged main HEAD, so no new repo-side implementation issue was created |
| 2026-06-27 | Stale current pytest count wording cleanup | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_does_not_describe_stale_full_pytest_count_as_current -q`; `gh issue create` created #151; stale full-suite wording scan in `docs\current_status.md` and `tests\test_current_status_docs.py` | PASS | GitHub #151 is fixed: older TF-STATUS-049 wording no longer describes the prior full-suite count as current evidence; current full-suite evidence is superseded above |
| 2026-06-27 | RustDbCursor executemany batch helper fail-closed | RED/GREEN: `pytest tests\test_db_core_service.py::test_rust_db_cursor_executemany_rejects_python_batch_helper -q`; `gh issue create` created #150; `pytest tests\test_current_status_docs.py::test_current_status_tracks_rust_db_cursor_executemany_issue -q`; `rg -n "executemany\(|execute_many\(" src tests migration_core\src migration_core\tests`; `pytest -q` | PASS | GitHub #150 is fixed: `RustDbCursor.executemany` now rejects the unused Python-side batch helper before any query/facade call; single-query Rust Core paths remain unchanged; full-suite evidence is superseded above |
| 2026-06-27 | Post-v2.1.7 version drift fix | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_v217_version_drift_issue -q`; `gh issue create` created #149; `git rev-list --count v2.1.7..HEAD`; `gh release list --limit 5`; `python scripts\bump_version.py --bump-type patch`; `pytest tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q`; `pytest -q` | PASS | GitHub #149 is fixed: current source/package/installer version is `2.1.8`, ahead of already published release `v2.1.7` after main accumulated release-tracking commits; its full-suite count is superseded by the current evidence above |
| 2026-06-27 | Post-#148 next issue analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_148_next_issue_analysis -q`; `git status --short --branch`; `gh issue list --state open --limit 20`; `gh issue view 116 --json number,title,state,labels,body,comments,url,updatedAt`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | Current `main` is aligned with `origin/main`; #116 is the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for the current merged main HEAD, so no new repo-side implementation issue was created |
| 2026-06-27 | v2.1.7 release publication | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_v217_release_publication_issue -q`; `git status --short --branch`; `gh issue list --state open --limit 20`; `git tag -a v2.1.7 -m "Release v2.1.7"`; `git push origin v2.1.7`; `gh run view 28255274238 --json status,conclusion,url`; `gh release view v2.1.7 --json tagName,name,url,assets,publishedAt,targetCommitish,isDraft,isPrerelease` | PASS | GitHub #148 is fixed: release `v2.1.7` was published from current `main` with `TunnelForge-Setup-2.1.7.exe`, `TunnelForge-WebSetup.exe`, `TunnelForge-macOS-2.1.7-arm64.dmg`, `TunnelForge-macOS-2.1.7-arm64.zip`, `TunnelForge-macOS-2.1.7-x86_64.dmg`, `TunnelForge-macOS-2.1.7-x86_64.zip`, and checksum assets |
| 2026-06-27 | Post-release version drift fix | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_release_version_drift_issue -q`; `gh issue create` created #147; `python scripts\bump_version.py --bump-type patch`; `pytest tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q`; `git log --oneline v2.1.6..HEAD`; `gh release list --limit 10` | PASS | GitHub #147 is fixed: current source/package/installer version is `2.1.7` after `v2.1.6` was already released and main accumulated post-release commits |
| 2026-06-27 | Post-#146 next issue analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_146_next_issue_analysis -q`; `git status --short --branch`; `gh issue list --state open --limit 30`; `gh issue view 116 --json number,title,state,labels,body,comments,url`; direct DB mutation/helper scan; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | Current `main` is aligned with `origin/main`; #116 is still the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for the current merged main HEAD, so no new repo-side implementation issue was created |
| 2026-06-27 | Legacy MySQLConnector execute_many mutation helper fail-closed | RED/GREEN: `pytest tests\test_db_connector.py::TestMySQLConnector::test_execute_many_rejects_legacy_python_mutation_helper -q`; `gh issue create` created #146; `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_execute_many_issue -q`; final: `pytest tests\test_db_connector.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\core\db_connector.py tests\test_db_connector.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #146 is fixed: `MySQLConnector.execute_many` now rejects the unused Python batch mutation helper before cursor/commit work, while read/query helper behavior is unchanged |
| 2026-06-27 | Legacy CleanupWorker actual cleanup mode fail-closed | RED/GREEN: `pytest tests\test_migration_worker.py -q`; `gh issue create` created #145; `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_cleanup_worker_issue -q`; final: `pytest tests\test_migration_worker.py tests\test_migration_analyzer.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\ui\workers\migration_worker.py tests\test_migration_worker.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #145 is fixed: `CleanupWorker(..., dry_run=False)` rejects legacy actual cleanup mode before a thread can start, while dry-run cleanup worker construction remains available |
| 2026-06-27 | Legacy MigrationAnalyzer cleanup mutations fail-closed | RED/GREEN: `pytest tests\test_migration_analyzer.py::TestExecuteCleanup::test_actual_cleanup_rejects_legacy_python_mutation_mode tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled -q`; `pytest tests\test_migration_analyzer.py::TestExecuteCleanup tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled -q`; `gh issue create` created #144; `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_migration_analyzer_cleanup_issue -q`; i18n regression: `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation ... -q`; final: `pytest tests\test_migration_analyzer.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\core\i18n.py src\core\migration_analyzer.py src\ui\dialogs\migration_dialogs.py tests\test_migration_analyzer.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #144 is fixed: `MigrationAnalyzer.execute_cleanup(..., dry_run=False)` rejects Python-owned cleanup mutation mode, the migration analyzer dialog keeps actual cleanup execution disabled until Rust Core owns it, and Dry-Run and SQL preview remain available |
| 2026-06-27 | Legacy Auto-Fix core mutation APIs fail-closed | RED/GREEN: `pytest tests\test_migration_fix_wizard.py::TestSessionGuardFaultInjection::test_batch_executor_rejects_legacy_python_mutation_mode tests\test_migration_fix_wizard.py::TestSessionGuardFaultInjection::test_fk_safe_charset_changer_rejects_legacy_python_mutation_mode -q`; RED/GREEN: `pytest tests\test_migration_fix_wizard.py::TestSessionGuardFaultInjection::test_private_single_execution_hook_is_fail_closed -q`; `pytest tests\test_migration_fix_wizard.py -q`; `gh issue create` created #143; `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_auto_fix_core_mutation_api_issue -q`; final: `pytest tests\test_migration_fix_wizard.py tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\core\migration_fix_wizard.py tests\test_migration_fix_wizard.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #143 is fixed: `BatchFixExecutor.execute_batch`, `FKSafeCharsetChanger.execute_safe_charset_change`, and `BatchFixExecutor._execute_single` now reject Python-owned DB mutation/session execution; dry-run/SQL preview remains available; current full Python suite is superseded above by the 1827-test run |
| 2026-06-27 | Post-#142 next issue analysis | `gh issue list --state open --limit 30 --json number,title,state,labels,updatedAt,url,assignees`; `gh issue view 116 --comments --json number,title,state,body,labels,comments,updatedAt,url`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; `rg -n "#116|TF-STATUS-008|real-Mac|real Mac|Mac validation|macOS Support M6|manual validation|final" docs\current_status.md docs\macos_support.md scripts tests README.md README.ko.md` | EXPECTED FAIL for `--final` only | #116 is still the only open GitHub issue. Normal repo-side gate passes. Final gate currently reports `no macOS manual validation report found under build/` and `no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD`, so the blocker remains external real-Mac evidence/current-head manual validation, not a repo-side implementation issue |
| 2026-06-27 | Legacy Auto-Fix Wizard dry-run only | RED/GREEN: `pytest tests\test_fix_wizard_dialog.py::test_legacy_fix_wizard_execution_page_runs_dry_run_only -q`; RED/GREEN: `pytest tests\test_fix_wizard_dialog.py::test_fix_wizard_worker_rejects_legacy_python_mutation_mode -q`; `pytest tests\test_fix_wizard_dialog.py -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py -q`; `pytest tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\core\i18n.py src\ui\dialogs\fix_wizard_dialog.py src\ui\workers\fix_wizard_worker.py tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #142 is fixed: `ExecutionPage` now starts `FixWizardWorker` with `dry_run=True`, `FixWizardWorker` rejects `dry_run=False`, and the legacy Auto-Fix UI presents Dry-run/SQL/manual execution rather than direct DB mutation; English runtime translations cover the new UI copy; current full Python suite is superseded above by the 1827-test run |
| 2026-06-27 | Legacy Python Auto-Fix Wizard mutation issue split | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_python_auto_fix_wizard_issue -q`; `gh issue create` created #142; `pytest -q`; `rg -n "MigrationFixWizard|FixWizard|fix_wizard|btn_auto_fix|auto_fix|MigrationAnalyzerDialog|migration_dialogs|oneclick|One-Click" src tests docs README.md README.ko.md`; inspection of `src/ui/dialogs/migration_dialogs.py`, `src/ui/dialogs/fix_wizard_dialog.py`, `src/ui/workers/fix_wizard_worker.py`, and `src/core/migration_fix_wizard.py` | PASS | GitHub #142 tracked the repo-side Rust Core baseline gap where the legacy Auto-Fix Wizard could execute DB mutations through Python-owned fix logic; this count is superseded above by the 1827-test run |
| 2026-06-27 | Post-merge next-issue external re-audit | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_merge_next_issue_external_reaudit -q`; `git status --short --branch`; `gh issue list --state open --limit 30 --json number,title,state,labels,updatedAt,url,assignees`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py`; `pytest -q`; direct DB/feature-flag/stale-doc scans | PASS | At that pass #116 was the only open issue, the #116 repo-side gates passed, SQL editor query execution also routed through the Rust connector shim, and no new GitHub issue was created because no confirmed repo-side issue was found yet |
| 2026-06-27 | macOS manual workflow head policy | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_uses_local_head_for_manual_workflow_after_pr_merge -q`; RED/GREEN: `pytest tests\test_macos_support_docs.py -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python scripts\check-macos-support-gate.py`; `gh issue view 116 --json body` | PASS | `scripts/check-macos-support-gate.py --final` now resolves the successful manual `workflow_dispatch` macOS artifact run against the same head policy as report SHA/artifact download: PR head before merge, current merged main HEAD after PR #117 has merged; GitHub #116 body now says the same, and current macOS focused suite is 53 passed |
| 2026-06-27 | BUILD installer version examples | RED/GREEN: `pytest tests\test_build_docs.py tests\test_current_status_docs.py::test_current_status_records_build_doc_installer_version_cleanup -q`; final: `pytest -q`; `pytest tests\test_build_docs.py tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_build_docs.py tests\test_current_status_docs.py`; `git diff --check`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py` | PASS | `BUILD.md` no longer shows stale 1.0.0 installer filename/AppVersion examples; installer examples use `{version}` and `AppVersion={#MyAppVersion}`; the current full Python suite count is superseded above by the 1827-test run |
| 2026-06-27 | One-Click module scope docstring | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_module_docstring_matches_limited_rust_core_scope tests\test_current_status_docs.py::test_current_status_records_oneclick_module_scope_docstring_cleanup -q`; final: `pytest -q`; `pytest tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py`; `git diff --check`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py` | PASS | Historical evidence: module-level wording stopped claiming full automatic execution and described that release's Rust DB Core dry-run default and limited real execution; TF-STATUS-097 Phase A now supersedes it |
| 2026-06-27 | One-Click fallback dry-run tooltip | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_disabled_real_execution_tooltip_does_not_reference_closed_138 tests\test_current_status_docs.py::test_current_status_records_oneclick_fallback_dry_run_tooltip_cleanup -q`; final: `pytest -q`; `pytest tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py`; `git diff --check`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py` | PASS | The disabled-real-execution fallback tooltip now says real execution is `disabled in this build` and no longer points at the already closed GitHub #138 gate; the current full Python suite count is superseded above by the 1827-test run |
| 2026-06-27 | Rust Core Export/Import menu wording | RED/GREEN: `pytest tests\test_main_window_export_import_labels.py -q`; `pytest tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count tests\test_current_status_docs.py::test_current_status_records_rust_core_export_import_menu_wording -q`; final: `pytest -q`; `pytest tests\test_main_window_export_import_labels.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\ui\main_window.py tests\test_main_window_export_import_labels.py tests\test_current_status_docs.py`; `git diff --check`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py` | PASS | Tunnel context menu actions now display `Rust DB Core Export` / `Rust DB Core Import`, handlers use `_context_rust_core_export` / `_context_rust_core_import`; the current full Python suite count is superseded above by the 1827-test run |
| 2026-06-27 | Current baseline duplicate service.hello cleanup | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_current_baseline_has_no_duplicate_check_rows -q`; `pytest tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | PASS | `Current Baseline Verification` now keeps one `tunnelforge-core service.hello` row that covers dump/import, migration, and One-Click capability evidence |
| 2026-06-27 | Focused verification duplicate row cleanup | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_focused_verification_has_no_duplicate_check_rows -q`; `pytest tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | PASS | `Focused Verification On 2026-06-27` no longer repeats the same `python scripts\check-macos-support-gate.py --skip-github` check row |
| 2026-06-27 | Current baseline count refresh after re-audit coverage | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_macos_focused_test_count tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count -q`; `pytest -q`; `pytest tests\test_current_status_docs.py -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | PASS | Top current baseline reflects the refreshed current-status coverage and macOS focused suite evidence; the current `pytest -q` row is superseded above by the 1827-test run, and macOS focused tests are now superseded by the 53-test run |
| 2026-06-27 | Current main next-issue re-audit | `git status --short --branch`; `git log --oneline --decorate -5`; `gh issue list --state open --limit 20`; `gh issue view 116 --comments`; `rg -n "pymysql|psycopg|mysql\.connector|mysqldump|pg_dump|mysqlpump|mysqlimport|\bpsql\b" src scripts`; `rg -n "execute\(|cursor\(|commit\(|rollback\(" src\core src\ui src\exporters`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS | Main is aligned with origin/main, #116 is the only open GitHub issue, #116 repo-side gates pass, macOS focused tests now pass at 53 tests, and the Rust Core baseline scan found no new repo-side violation; legacy-shaped DB connector paths route through Rust Core shims |
| 2026-06-27 | Current baseline verification heading | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_current_baseline_section_is_not_stale_dated -q`; `pytest tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | PASS | Top status no longer labels the mixed current baseline as `Verified On 2026-06-26`; the section now distinguishes the refreshed 2026-06-27 full-suite count from preserved 2026-06-26 broader baseline evidence |
| 2026-06-27 | Current full Python suite count refresh | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count -q`; `pytest -q`; `pytest tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py tests\test_schedule_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py tests\test_schedule_docs.py`; `git diff --check` | PASS | Updated top current-status full Python suite count from stale `1826 passed` to current `1827 passed, 5 warnings` after the post-release version drift regression test was added |
| 2026-06-27 | One-Click limited production scope wording | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py::test_oneclick_readiness_distinguishes_limited_real_execution_from_broad_production_support -q`; `pytest tests\test_oneclick_readiness_docs.py -q`; `pytest tests\test_oneclick_readiness_docs.py tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_oneclick_readiness_docs.py tests\test_current_status_docs.py`; `git diff --check` | PASS | Historical evidence: readiness docs distinguished that release's backup-confirmed `engine_innodb` real-execution path from unsupported broad production automatic remediation and production charset/collation execution; TF-STATUS-097 Phase A now supersedes it |
| 2026-06-27 | Schedule guide hidden-feature wording | RED/GREEN: `pytest tests\test_schedule_docs.py -q`; `pytest tests\test_schedule_docs.py tests\test_current_status_docs.py -q`; `rg -n -F -e '메인 툴바에서 **"스케줄"** 버튼을 클릭' -e '스케줄 시간을 기다리지 않고 바로 백업하려면:' -e '스케줄 관리 창의 **"백업 로그"** 탭에서' -e '스케줄이 작동하려면 TunnelForge가 실행 중이어야 합니다' SCHEDULE.md`; `python -m compileall -q tests\test_schedule_docs.py tests\test_current_status_docs.py`; `git diff --check` | PASS | `SCHEDULE.md` now reads as an internal/reactivation memo while `SCHEDULE_FEATURE_ENABLED = False`, and no longer gives public-toolbar/log/immediate-run instructions as current user steps |
| 2026-06-27 | macOS artifact default source after PR #117 merge | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_validation_artifact_download_script_uses_local_head_after_pr_merge -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `bash -n scripts/macos-download-validation-artifacts.sh scripts/macos-manual-validation-report.sh`; `pytest tests\test_current_status_docs.py -q`; `python scripts\check-macos-support-gate.py --skip-github`; `python -m compileall -q tests\test_rust_core_packaging.py tests\test_macos_support_docs.py tests\test_current_status_docs.py scripts\check-macos-support-gate.py`; `git diff --check` | PASS | `macos-download-validation-artifacts.sh` now finds the latest successful manual `macOS App Validation` run for PR head before merge, or current merged main HEAD after PR #117 is merged, so downloaded artifact provenance matches the final report/gate SHA policy |
| 2026-06-26 | Direct DB Export/Import Rust Core endpoint host | RED/GREEN: `pytest tests\test_db_dialogs.py::test_export_dialog_uses_direct_connector_host_for_rust_dump -q`; RED/GREEN: `pytest tests\test_db_dialogs.py::test_import_dialog_uses_direct_connector_host_for_rust_dump -q`; `pytest tests\test_db_dialogs.py::test_export_dialog_uses_direct_connector_host_for_rust_dump tests\test_db_dialogs.py::test_import_dialog_uses_direct_connector_host_for_rust_dump -q` | PASS | `RustDumpExportDialog` and `RustDumpImportDialog` now preserve direct connector `host` when creating `RustDumpConfig`; tunnel connections still use their connector host, normally `127.0.0.1` |
| 2026-06-26 | Export table selection audit | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_export_table_selection_audit -q`; `git status --short --branch`; `gh issue list --state open --limit 30 --json number,title,state,labels,updatedAt,url`; `rg -n "class RustDumpExportDialog|export_tables|dump.run|selected_tables|table selection" src tests docs README.md README.ko.md migration_core\src migration_core\tests`; code inspection of `RustDumpExportDialog`, `RustDumpExporter.export_tables`, `RustDumpWorker`, and Rust `dump.run` table filtering | PASS | Export individual table selection is currently implemented: PyQt exposes `선택 테이블 Export`, checkbox table list, select-all/deselect-all, and FK parent auto-include; Python forwards selected tables through `RustDumpExporter.export_tables`; Rust Core `dump.run` filters schema tables from the `tables` payload |
| 2026-06-26 | GitHub #116 final evidence attachment wording | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_can_post_github_comment -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py tests\test_current_status_docs.py -q`; `python scripts\check-macos-support-gate.py`; `python -m compileall -q tests\test_rust_core_packaging.py tests\test_macos_support_docs.py tests\test_current_status_docs.py scripts\check-macos-support-gate.py`; `git diff --check` | PASS | Finalizer stdout, generated GitHub comment, and macOS support docs now tell operators to attach final real-Mac evidence to #116 first; PR #117 is only a traceability mirror |
| 2026-06-26 | GitHub #116 Actions lookup command accuracy | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_rejects_hard_coded_current_issue_head -q`; RED/GREEN: `python scripts\check-macos-support-gate.py`; `gh run list --workflow "macOS App Validation" --branch main --limit 5`; `gh run list --workflow "macOS App Validation" --event pull_request --limit 3`; `gh run list --workflow "Version Gate" --event pull_request --limit 3`; `gh issue edit 116 --body-file <temp>` | PASS | #116 no longer tells operators to use `--branch main` for PR workflow run lookup; event-filtered commands return relevant PR/manual runs |
| 2026-06-26 | Current full Python suite count refresh | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; `pytest -q` | PASS | Updated top current-status full Python suite count from stale `1729 passed` to current `1786 passed, 5 warnings` |
| 2026-06-26 | Current macOS focused test count refresh | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `pytest tests\test_current_status_docs.py tests\test_macos_support_docs.py tests\test_oneclick_readiness_docs.py -q` | PASS | Updated top current-status macOS focused test count from stale `47 passed` to current `51 passed` |
| 2026-06-26 | GitHub #116 non-volatile Actions run wording | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_rejects_hard_coded_current_issue_head -q`; RED/GREEN: `python scripts\check-macos-support-gate.py`; `gh issue view 116 --json body`; `gh issue edit 116 --body-file <temp>` | PASS | Gate now rejects #116 Current Evidence lines that label fixed GitHub Actions run URLs as `Latest`; issue body now uses reference-run wording and lets the gate resolve current matching runs |
| 2026-06-26 | GitHub #116 non-volatile current head policy | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_rejects_hard_coded_current_issue_head -q`; RED/GREEN: `python scripts\check-macos-support-gate.py`; `gh issue view 116 --json body`; `gh issue edit 116 --body-file <temp>` | PASS | Gate now rejects #116 body wording that hard-codes a current gate head SHA; #116 Current Evidence now tells operators to use latest pushed `main` / `origin/main` instead |
| 2026-06-26 | GitHub #116 current head refresh after docs commits | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; `gh issue view 116 --json body,updatedAt,url`; `gh issue edit 116 --body-file <temp>`; `gh issue view 116 --json body --jq .body` | PASS | #116 Current Evidence is refreshed to the latest pushed `main` / gate head and clarifies final reports match PR head before merge or current merged main after merge |
| 2026-06-26 | One-Click evidence README completion wording | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q`; `rg -n "future|template|completed|oneclick-real-execution-evidence|oneclick-charset-evidence|oneclick-charset-derivation-evidence|#138|#139|#140" reports\oneclick_readiness docs\oneclick_readiness.md tests` | PASS | Evidence README no longer describes completed #138/#139 local evidence as future work; templates are documented as refresh shapes, not missing evidence |
| 2026-06-26 | One-Click closed-issue wording drift | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q`; `rg -n "TODO|FIXME|XXX|HACK|NotImplemented|raise NotImplementedError|pass\s*$" src tests scripts docs README.md README.ko.md BUILD.md SCHEDULE.md`; `rg -n "not yet supported|pending|future|disabled|hidden|preview|manual|not implemented|unsupported|준비|미지원|비활성|숨김|수동|remaining|still needs|still requires|open issue|blocked" docs README.md README.ko.md BUILD.md SCHEDULE.md src tests scripts`; `rg -n "#1(1[0-9]|2[0-9]|3[0-9]|4[0-9])|TF-STATUS-[0-9]+|Next action:" docs README.md README.ko.md BUILD.md SCHEDULE.md reports scripts tests src` | PASS | Fresh repo-side scan found stale current-tense One-Click tracking wording for closed #138/#139; readiness doc now states #137-#141 are completed and no One-Click follow-up issue is open |
| 2026-06-26 | macOS artifact head SHA provenance | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_validation_artifact_download_script_writes_env_file -q`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_manual_validation_report_check_complete_rejects_missing_artifact_metadata -q`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_checks_report_artifact_head_sha -q`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate -q`; `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_script_accepts_local_final_report tests\test_rust_core_packaging.py::test_macos_support_gate_script_checks_report_artifact_workflow_run tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate -q` | PASS | Final macOS evidence and generated GitHub evidence comment now record and gate-check the artifact workflow head SHA separately from the report Git SHA |
| 2026-06-26 | GitHub #116 handoff body refresh | `gh issue view 116 --json body`; `gh issue edit 116 --body-file <temp>`; `gh issue view 116 --json body --jq .body` | PASS | Historical refresh: #116 Current Evidence then pointed operators at gate head `6da13f7` and no longer said PR #117 still needed to be marked ready |
| 2026-06-26 | macOS final report SHA after PR #117 merge | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_uses_local_head_for_final_report_after_pr_merge -q`; RED/GREEN: `pytest tests\test_macos_support_docs.py -q`; `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_accepts_merged_pr_with_unknown_merge_state tests\test_rust_core_packaging.py::test_macos_support_gate_script_accepts_local_final_report tests\test_rust_core_packaging.py::test_macos_support_gate_script_rejects_report_from_different_git_sha -q`; `python scripts\check-macos-support-gate.py` | PASS | Final gate now expects the current merged main HEAD for report Git SHA after PR #117 is merged, instead of the stale PR head |
| 2026-06-26 | macOS support gate after PR #117 merge | `python scripts\check-macos-support-gate.py` failed before fix because merged PR #117 reports `mergeStateStatus=UNKNOWN`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_accepts_merged_pr_with_unknown_merge_state -q`; `python scripts\check-macos-support-gate.py`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python -m compileall -q scripts\check-macos-support-gate.py tests\test_rust_core_packaging.py`; `git diff --check` | PASS | Full GitHub #116 gate now accepts merged PR #117 while still checking issue state and status checks |
| 2026-06-26 | Current status stale handoff scan | `rg -n "TODO|FIXME|XXX|HACK|NotImplemented|raise NotImplementedError|pass\s*$" src tests scripts docs README.md README.ko.md SCHEDULE.md`; `rg -n "not yet supported|pending|future|disabled|hidden|preview|manual|not implemented|unsupported|준비|미지원|비활성|숨김|수동" docs README.md README.ko.md SCHEDULE.md src tests`; `rg -n "GitHub issue #[0-9]+ now tracks|next actionable|remaining unchecked|should remain open|still requires|still needs|TODO" docs README.md README.ko.md reports scripts tests`; `pytest tests\test_current_status_docs.py -q` RED/GREEN | PASS | Found no new repo-side issue beyond #116; corrected stale top-handoff wording that still presented closed #140 as current work |
| 2026-06-26 | Current status summary consistency and next issue analysis | `pytest tests\test_current_status_docs.py -q` RED/GREEN; `pytest tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py -q`; `gh issue list --state open --limit 30 --json number,title,labels,updatedAt,url`; `gh issue view 116 --json number,title,state,body,labels,comments,url,updatedAt`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `git diff --check` | PASS | Summary now matches GitHub state: #116 is the only open issue and #137-#141 are closed One-Click readiness work; no additional repo-side #116 gap found |
| 2026-06-26 | One-Click PyQt charset derivation evidence | `pytest tests\test_oneclick_charset_derivation_evidence.py -q` RED/GREEN; `pytest tests\test_oneclick_charset_derivation_capture.py -q` RED/GREEN; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py tests\test_oneclick_charset_derivation_capture.py tests\test_oneclick_charset_derivation_evidence.py -q`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python scripts\capture-oneclick-charset-derivation-evidence.py --seed-local-container --mysql-container tf-live-mysql --mysql-host 127.0.0.1 --mysql-port 3406 --mysql-user root --mysql-password test --schema tf_oneclick_derive_charset --output reports\oneclick_readiness\oneclick-charset-derivation-evidence.json`; `python scripts\validate-oneclick-charset-derivation-evidence.py reports\oneclick_readiness\oneclick-charset-derivation-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_DERIVATION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS | #140 local evidence proves PyQt-triggered Rust Core derivation feeds `oneclick.run dry_run=false` and converts 2 FK-connected local tables |
| 2026-06-26 | One-Click follow-up issue split | `rg -n "invalid_date|zerofill_usage|float_precision|int_display_width|enum_empty_value|manual|skip|oneclick_issues_from_inspection|oneclick_recommendations|oneclick_auto_fix_option" migration_core\src\lib.rs docs\oneclick_readiness.md tests docs\current_status.md`; `gh issue create` created #141 | PASS | `int_display_width` skip semantics are now tracked separately from closed #140 |
| 2026-06-26 | One-Click `int_display_width` skip policy | `pytest tests\test_oneclick_readiness_docs.py -q` RED/GREEN; `pytest tests\test_oneclick_readiness_docs.py tests\test_oneclick_rust_core_gate.py -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick_live_inspection_does_not_synthesize_int_display_width_skip --lib`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` | PASS | #141 policy is now explicit: externally supplied `skip` is display-only and Rust Core live One-Click does not synthesize or execute this class |
| 2026-06-26 | Main merge/status and then-current next issue analysis | `git fetch origin --prune`; `git status --short --branch`; `gh issue list --state open --limit 20 --json number,title,labels,updatedAt,assignees,url`; `gh issue view 140 --comments --json number,title,state,body,comments,labels,url,updatedAt`; `gh issue view 116 --json number,title,state,body,labels,url,updatedAt`; `rg -n "TF-STATUS-022|#140|derive_charset|oneclick.derive_charset|charset_contracts|OneClickMigrationWorker|derive_oneclick_charset_contracts" ...` | PASS | Historical row: at that point `main` was aligned with `origin/main`, #140 was the next actionable in-repo issue, and #116 remained external real-Mac evidence; #140 is now closed |
| 2026-06-26 | Current main full Python suite | `pytest -q` | PASS | 1786 passed, 5 warnings |
| 2026-06-26 | Current main Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 166 lib tests, JSONL CLI test, 6 live-roundtrip tests, 2 non-ignored stress tests, doctests |
| 2026-06-26 | Current main Rust release build | `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS | Produced release Rust core binary |
| 2026-06-26 | Current main Python syntax | `python -m compileall -q main.py src tests scripts` | PASS | No compile errors |
| 2026-06-26 | Current main live UI evidence validator | `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json` | PASS | 2 directions and 12,000,000 rows checked |
| 2026-06-26 | Current main Rust performance evidence validator | `python scripts\validate-rust-core-performance-evidence.py` | PASS | 4 files and 11,000,000 rows proven |
| 2026-06-26 | Current main optional evidence regression gate | `RUST_CORE_REQUIRE_PERF_EVIDENCE=1; RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS | Requires both archived Rust performance evidence and live UI migration evidence |
| 2026-06-26 | Current main macOS support gate | `python scripts\check-macos-support-gate.py --skip-github` | PASS | Repository-side macOS support tracking checks pass without final real-Mac evidence |
| 2026-06-26 | Current main macOS focused tests | `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS | 51 passed |
| 2026-06-26 | Current main diff hygiene | `git diff --check` | PASS | No whitespace errors |
| 2026-06-26 | One-Click production-readiness audit | `tunnelforge-core service.hello`; `rg -n "oneclick\.|ONE_CLICK_MIGRATION_FEATURE_ENABLED" migration_core\src\lib.rs src tests docs README.md README.ko.md`; `gh issue view 124` | PASS | Rust Core advertises `oneclick.*` commands and Python worker uses Rust Core, but the PyQt entry point is still hidden; created GitHub #137 |
| 2026-06-26 | One-Click dry-run safety gate | `pytest tests\test_oneclick_rust_core_gate.py tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `pytest tests\test_oneclick_rust_core_gate.py tests\test_db_core_service.py -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py`; `git diff --check` | PASS | Worker rejects real execution until #138; dialog locks Dry-run checked/disabled |
| 2026-06-26 | One-Click dry-run evidence | `pytest tests\test_oneclick_dry_run_evidence.py -q`; `python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS | Local MySQL Rust Core `oneclick.run` dry-run evidence captured and wired to optional regression gate |
| 2026-06-26 | One-Click dry-run preview gate | `pytest tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_exposes_oneclick_as_dry_run_preview_only -q`; `pytest tests\test_oneclick_dry_run_evidence.py::test_oneclick_dry_run_evidence_accepts_complete_report tests\test_oneclick_dry_run_evidence.py::test_oneclick_dry_run_evidence_requires_preview_ui_enabled -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json` | PASS | PyQt entry point is visible as dry-run preview; evidence requires preview UI enabled and real execution disabled |
| 2026-06-26 | One-Click issue split | `gh issue create` created #138; `gh issue view 137`; `gh issue view 138`; `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_rejects_real_execution_until_readiness_gate_opens tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_locks_dry_run_until_readiness_gate_opens -q`; `rg -n "TF-STATUS-019|TF-STATUS-020|#138|ONECLICK_REAL_EXECUTION_ENABLED" docs src tests migration_core` | PASS | #137 dry-run preview gate is separated from #138 real-execution/automatic-fix coverage; real-execution lock copy points to #138 |
| 2026-06-26 | One-Click real-execution evidence contract | `pytest tests\test_oneclick_real_execution_evidence.py -q` RED, then GREEN; `pytest tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py -q`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.template.json` expected reject; `python -m compileall -q scripts tests`; `git diff --check` | PASS | #138 real-execution validator and optional gate hook added; template is rejected until real git SHA/evidence is captured |
| 2026-06-26 | One-Click engine_innodb apply path | `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_actions_accepts_only_engine_innodb_steps --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_plan_executes_engine_innodb_sql --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_fixes_real_engine_innodb_requires_endpoint --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml`; `pytest tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py -q`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.template.json` expected reject; `git diff --check` | PASS | Rust Core `oneclick.apply_fixes` now executes only planned `deprecated_engine -> engine_innodb` actions through the Rust adapter path and fails closed when a real apply request lacks an endpoint |
| 2026-06-26 | One-Click real-execution evidence capture | `pytest tests\test_db_core_service.py::test_facade_uses_oneclick_apply_fixes_protocol -q` RED/GREEN; `pytest tests\test_oneclick_real_execution_capture.py -q` RED/GREEN; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python scripts\capture-oneclick-real-execution-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `pytest tests\test_oneclick_real_execution_capture.py tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py tests\test_db_core_service.py -q`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python -m compileall -q src\core\db_core_service.py scripts\capture-oneclick-real-execution-evidence.py tests\test_oneclick_real_execution_capture.py tests\test_db_core_service.py`; `git diff --check` | PASS | Local MySQL evidence captured: Rust Core `oneclick.apply_fixes` converted `tf_oneclick_real_execution.tf_oneclick_legacy_engine_table` from `MyISAM` to `InnoDB` while app real execution stayed disabled |
| 2026-06-26 | One-Click deprecated engine live discovery | `cargo test --manifest-path migration_core\Cargo.toml oneclick_issues_classify_deprecated_engine_marker_as_auto_fixable --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml mysql_deprecated_engine_sql_targets_table_engines --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo test --manifest-path migration_core\Cargo.toml`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` | PASS | MySQL inspection can now emit deprecated-engine markers for MyISAM tables and One-Click converts those markers into typed `deprecated_engine` auto-fix candidates |
| 2026-06-26 | One-Click run orchestration for engine_innodb | `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_engine_innodb_when_env_is_configured --test live_roundtrip -- --nocapture` RED/GREEN | PASS | UI-facing Rust Core `oneclick.run dry_run=false` now sequences the validated `engine_innodb` apply path and converts a live MyISAM table to InnoDB |
| 2026-06-26 | One-Click limited real-execution PyQt gate | `pytest tests\test_oneclick_rust_core_gate.py -q` RED then GREEN; `pytest tests\test_oneclick_rust_core_gate.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_real_execution_capture.py tests\test_db_core_service.py -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json` | PASS | PyQt keeps Dry-run default, allows limited backup-confirmed real execution, and rejects non-dry-run without backup confirmation |
| 2026-06-26 | One-Click #138 closure gate | `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_engine_innodb_when_env_is_configured --test live_roundtrip -- --nocapture`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py src\ui\dialogs\migration_dialogs.py src\core\i18n.py scripts\validate-oneclick-dry-run-evidence.py scripts\validate-oneclick-real-execution-evidence.py tests\test_oneclick_rust_core_gate.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_real_execution_evidence.py`; `git diff --check` | PASS | #138 acceptance is satisfied for the exact `deprecated_engine -> engine_innodb` automatic scope |
| 2026-06-26 | Post-#138 open issue scan and #116 re-audit | `gh issue list --repo sanghyun-io/tunnelforge --state open --limit 20 --json number,title,labels,url`; `gh issue view 116 --repo sanghyun-io/tunnelforge --json number,title,state,body,comments,url,labels`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS | #116 is the only remaining open issue; no repository-side macOS gap found, final real operator Mac evidence is still external |
| 2026-06-26 | One-Click follow-up issue split | `rg -n "charset_issue|invalid_date|zerofill_usage|float_precision|enum_empty_value|deprecated_engine|engine_innodb|manual|oneclick_recommend|oneclick_apply" migration_core\src\lib.rs tests docs\oneclick_readiness.md`; `gh issue create` created #139 | PASS | Charset/collation One-Click automatic fix coverage is now tracked separately from closed #138 |
| 2026-06-26 | One-Click charset/collation evidence contract | `pytest tests\test_oneclick_charset_evidence.py -q` RED then GREEN; `python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.template.json` expected reject; `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` expected reject without completed evidence | PASS | #139 now has a machine-checkable evidence contract/template and optional regression gate hook, but no charset real execution is enabled |
| 2026-06-26 | Full Python suite | `pytest -q` | PASS | 1707 passed, 3 warnings |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | Unit, CLI, and gated live tests pass or skip according to env |
| 2026-06-26 | Rust release build | `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS | Produced `migration_core\target\release\tunnelforge-core.exe` |
| 2026-06-26 | Python syntax | `python -m compileall -q main.py src tests` | PASS | No compile errors |
| 2026-06-26 | Diff hygiene | `git diff --check` | PASS | No whitespace errors |
| 2026-06-26 | Core smoke | `tunnelforge-core service.hello` | PASS | Advertises dump/import and migration commands |
| 2026-06-26 | Live MySQL/PostgreSQL smoke | `cargo test --manifest-path migration_core\Cargo.toml --test live_roundtrip -- --nocapture` | PASS | 6 live container tests passed against MySQL 8.4 on port 3406 and PostgreSQL 16 on port 55432 |
| 2026-06-26 | Live UI evidence capture tests | `pytest tests\test_live_ui_migration_capture.py tests\test_live_ui_migration_evidence.py -q` | PASS | Capture helper report shape and final validator behavior covered |
| 2026-06-26 | Live UI capture smoke | `python scripts\capture-live-ui-migration-evidence.py --rows 1000 --chunk-size 250 --seed-local-containers --output reports\live_ui_migration\live-ui-migration-evidence-smoke.json --stress-source-type synthetic_adapter --stress-peak-rss-mb 512 --stress-rss-limit-mb 2048 --stress-notes "smoke only; not #136 closure evidence"` | PASS | Smoke produced bidirectional 1,000-row worker evidence; smoke artifact removed and not used for #136 closure |
| 2026-06-26 | Live UI evidence negative check | `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence-smoke.json` | FAIL expected | Validator rejected the smoke report because rows were below 1,000,000 |
| 2026-06-26 | Live UI 1M partial capture | `python scripts\capture-live-ui-migration-evidence.py --rows 1000000 --chunk-size 10000 --seed-local-containers --output reports\live_ui_migration\live-ui-migration-evidence-1m-local.json --stress-source-type synthetic_adapter --stress-peak-rss-mb 0 --stress-rss-limit-mb 0 --stress-notes "placeholder; RSS not measured in this run, do not use as final #136 evidence"` | PASS | Both 1M directions migrated+verified through `CrossEngineMigrationWorker`; max heartbeat gap 125ms; renamed to `live-ui-migration-evidence-1m-local-partial.json` |
| 2026-06-26 | Live UI partial evidence negative check | `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence-1m-local-partial.json` | FAIL expected | Validator rejects the partial because 10M RSS fields are intentionally 0 |
| 2026-06-26 | Rust Core 10M stress RSS | `TF_STRESS_ROWS=10000000 TF_STRESS_CHUNK_SIZE=200000 TF_STRESS_RSS_REPORT=<abs>\stress-10m-rss.json TF_STRESS_RSS_LIMIT_MB=2048 cargo test --manifest-path migration_core\Cargo.toml --test stress_rss synthetic_10m_stress_resume_verify_reports_rss_bound -- --ignored --nocapture` | PASS | 10M synthetic adapter resume+verify succeeded; peak RSS 921MB / 2048MB |
| 2026-06-26 | Final live UI evidence validator | `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json` | PASS | 2 directions and 12,000,000 rows checked |
| 2026-06-26 | Import wrapper/dialog focused tests | `python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 62 passed after payload/UI wording fixes |
| 2026-06-26 | Rust timezone validation TDD | `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_session_time_zone_only --lib` | FAIL then PASS | Initial RED failed because `validated_timezone_sql` did not exist; GREEN passed after helper implementation |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 139 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Import wrapper/dialog focused tests | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 62 passed after Rust timezone change |
| 2026-06-26 | Strict manifest classification TDD | `cargo test --manifest-path migration_core\Cargo.toml strict_manifest_validation_rejects_missing_chunk_checksums --lib` | FAIL then PASS | Initial RED failed because strictness/classification helpers did not exist; GREEN covered strict reject, legacy warning, classified formatting |
| 2026-06-26 | Strict import wiring | `cargo test --manifest-path migration_core\Cargo.toml dump_import_strict_manifest_rejects_missing_checksums_before_connect --lib` | PASS | Confirms strict import fails before dummy DB connection |
| 2026-06-26 | Classified error wrapper | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py::TestRustDumpImporter::test_import_dump_preserves_classified_core_error -q` | PASS | Confirms `export_invalid` and scope survive Python wrapper |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 143 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Import wrapper/dialog focused tests | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 63 passed after classified error regression |
| 2026-06-26 | Rust format and diff hygiene | `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` | PASS | No formatting or whitespace issues |
| 2026-06-26 | Import row-count verification TDD | `cargo test --manifest-path migration_core\Cargo.toml import_row_count_verification --lib` | FAIL then PASS | Initial RED failed because `verify_imported_row_counts` and report path helper did not exist; GREEN covered matching and mismatched table row counts |
| 2026-06-26 | Import report path | `cargo test --manifest-path migration_core\Cargo.toml import_report_path_lives_inside_dump_directory --lib` | PASS | Confirms report path resolves under dump directory |
| 2026-06-26 | Import report artifact | `cargo test --manifest-path migration_core\Cargo.toml write_dump_import_report_creates_json_file --lib` | PASS | Confirms `_tunnelforge_import_report.json` is written with verification JSON |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 147 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Import wrapper/dialog focused tests | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 63 passed after import verification/report change |
| 2026-06-26 | Export manifest metadata TDD | `cargo test --manifest-path migration_core\Cargo.toml dump_manifest_strictness_fields_default_for_legacy_json --lib` | FAIL then PASS | Initial RED failed because `snapshot_policy`, `strict_export`, and `manifest_warnings` did not exist |
| 2026-06-26 | Export consistency policy | `cargo test --manifest-path migration_core\Cargo.toml dump_manifest_consistency_metadata --lib` | PASS | Parallel exports are marked non-strict; single-thread exports are marked connection-consistent |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 150 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Import wrapper/dialog focused tests | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 63 passed after export metadata change |
| 2026-06-26 | Merge post-load DDL policy TDD | `cargo test --manifest-path migration_core\Cargo.toml post_load_ddl_policy --lib` | FAIL then PASS | Initial RED failed because merge/recreate DDL policy helpers did not exist |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 152 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Rust release build | `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS | Release Rust core binary builds |
| 2026-06-26 | Python syntax | `.venv\Scripts\python -m compileall -q main.py src tests` | PASS | No compile errors |
| 2026-06-26 | Full Python suite | `.venv\Scripts\python -m pytest -q` | FAIL then PASS | Initial failure exposed missing English translation for new import UI wording; final run passed 1710 tests with 3 warnings |
| 2026-06-26 | Final remediation report | `Get-Item reports\export_import_flow_review_20260601.html` | PASS | Report exists, length 6104 bytes |

## Existing Status And Planning Documents

Use these documents as inputs, not as proof of completion:

- `AGENTS.md` - repository operating guidelines and active Rust Core baseline.
- `CLAUDE.md` - broader architecture notes and release workflow notes.
- `docs/cross_engine_migration_plan.md` - cross-engine migration plan plus Rust
  Core transition audit notes.
- `docs/macos_support.md` - macOS support scope and final real-Mac validation
  gates.
- `docs/superpowers/specs/2026-05-19-rust-core-export-progress-performance-design.md`
- `docs/superpowers/plans/2026-05-19-rust-core-export-progress-performance.md`
- `docs/superpowers/specs/2026-05-20-db-conversion-wizard-design.md`
- `docs/superpowers/plans/2026-05-20-db-conversion-guided-wizard.md`
- `docs/superpowers/specs/2026-06-01-export-import-recovery-design.md`
- `docs/superpowers/plans/2026-06-01-export-import-recovery.md`

The three `docs/superpowers/plans/*.md` files are implementation plans with
unchecked task lists. They should not be interpreted as completed work.

## Confirmed Strengths

- Python and Rust test suites are large and currently pass.
- Rust Core JSONL service advertises the expected DB capabilities.
- The packaged app path is guarded by tests for Rust Core inclusion.
- Cross-engine migration UI has focused tests around guided wizard state,
  target approval, cleanup planning, and verify-step flow.
- Python DB connector shims route through `DbCoreFacade` and `RustDbConnection`
  rather than importing direct Python MySQL/PostgreSQL drivers.
- `git ls-files` does not show tracked build/cache artifacts such as
  `__pycache__`, `dist`, `build`, `output`, or `migration_core/target`.

## High Priority Issues

### TF-STATUS-001: Initial Import Intent And Strictness Gates

Status: `closed`
Severity: High
Area: Export/Import Recovery

Evidence:

- 2026-06-26 update: Python now forwards `timezone_sql` and
  `strict_manifest=True` to the Rust import payload, with focused pytest
  coverage.
- 2026-06-26 update: Rust now validates `timezone_sql` as a single
  `SET SESSION time_zone` statement with a literal value and applies it on the
  import adapter session immediately after connection.
- 2026-06-26 update: Rust now rejects strict imports with missing
  `chunk_sha256` metadata before DB connection/target mutation; non-strict
  legacy imports emit warning events.
- 2026-06-26 update: classified Rust import errors are preserved through the
  Python import wrapper message.

Historical impact at closure:

- Initial import intent and strictness gates are now enforced at the Rust Core
  boundary instead of only being represented in Python payloads.
- Remaining release-readiness watch items are tracked separately.

Next action:

1. Keep the regression tests and report aligned if import intent handling
   changes.

### TF-STATUS-002: Import Success Is Gated By Row Verification

Status: `closed`
Severity: High
Area: Rust Core dump.import

Evidence:

- 2026-06-26 update: `dump_import()` now tracks imported rows per table and
  calls `verify_imported_row_counts()` before returning success.
- 2026-06-26 update: mismatched imported row counts fail with
  `post_load_validation_failed` and table scope.
- 2026-06-26 update: successful imports write
  `_tunnelforge_import_report.json` beside the dump and include the report path
  plus verification summary in the result payload.

Impact:

- Import success now has an explicit row-count verification gate and persisted
  report artifact.
- Export consistency metadata is tracked separately and is now closed.

Next action:

1. Keep row verification and report artifact coverage when import modes change.

### TF-STATUS-003: Import UI Overpromises Object Restoration

Status: `closed`
Severity: High
Area: Import UI

Evidence:

- 2026-06-26 update: the import dialog no longer says `모든 객체`.
- The dialog now describes table structure/data recreation and states that
  View restoration is best effort while procedures/triggers/events require
  separate confirmation.

Impact:

- The overpromising object restoration wording is fixed and verified by the
  focused UI regression plus the full Python suite.
- Unsupported object restoration remains a documented residual limit in the
  final remediation report.

Next action:

1. Keep the regression test that rejects `모든 객체`.

### TF-STATUS-004: Export Consistency Is Explicit In The Manifest

Status: `closed`
Severity: High
Area: Rust Core dump.run manifest

Evidence:

- 2026-06-26 update: `DumpManifest` now includes `snapshot_policy`,
  `strict_export`, and `manifest_warnings`.
- 2026-06-26 update: legacy manifests default to
  `snapshot_policy = "unknown"`, `strict_export = false`, and no warnings.
- 2026-06-26 update: new single-thread exports are marked
  `connection_consistent` and strict; parallel exports are marked
  `non_consistent_parallel`, non-strict, with a warning.

Impact:

- Dump artifacts now communicate the export consistency policy instead of
  implying a shared snapshot that was not proven.

Next action:

1. Keep export consistency metadata coverage when export scheduling changes.

## Medium Priority Issues

### TF-STATUS-005: Disabled UI Features Are Labeled In Docs

Status: `closed`
Severity: Medium
Area: Docs/UI feature flags

Evidence:

- 2026-06-26 update: `SCHEDULE.md` now states that scheduled backup is
  disabled in the main UI and is retained as internal/reactivation
  documentation.
- `src/ui/main_window.py` sets `SCHEDULE_FEATURE_ENABLED = False`.
- `src/ui/main_window.py` sets `SQL_FILE_EXECUTION_FEATURE_ENABLED = False`.
- No separate public SQL file execution guide is tracked; the main context menu
  entry remains hidden by the feature flag.

Impact:

- Public schedule documentation no longer implies the feature is currently
  available in the main UI.

Next action:

1. If either feature is re-enabled, update the docs and add fresh UI/runtime
   verification evidence in the same session.

### TF-STATUS-006: Large Files Increase Change Risk

Status: `watch`
Severity: Medium
Area: Maintainability

Largest implementation files after Clean Code Round 3:

- `migration_core/src/ddl.rs` - about 2,710 lines.
- `src/ui/dialogs/sql_editor_dialog.py` - about 2,534 lines.
- `migration_core/src/migrate.rs` - about 2,491 lines.
- `migration_core/src/oneclick.rs` - about 2,125 lines.
- `migration_core/src/dump.rs` - about 2,091 lines.
- `src/ui/dialogs/db_import_dialog.py` - about 1,611 lines.
- `src/ui/dialogs/cross_engine_migration_dialog.py` - about 1,429 lines.
- `src/ui/dialogs/db_export_dialog.py` - about 1,368 lines.

Impact:

- Round 1 through Round 3 removed the worst legacy god-file hotspots, including
  the previous `db_dialogs.py` and main-window concentration. Remaining large
  files still have enough surface area to make broad behavior changes risky.

Next action:

1. Keep future fixes narrowly scoped and test-first.
2. Treat further structural splitting as watch work, not an active blocker,
   unless a nearby feature touches one of the remaining large files.

## Lower Priority / Tracking

### TF-STATUS-007: Referenced Export/Import HTML Report Exists

Status: `closed`
Severity: Low
Area: Documentation/reporting

Evidence:

- `reports/export_import_flow_review_20260601.html` is referenced by the
  recovery design and plan.
- 2026-06-26 update: `reports/export_import_flow_review_20260601.html` exists
  and has been converted into a remediation report with verification evidence
  and residual limits.

Next action:

1. Keep the report aligned when recovery scope changes.

### TF-STATUS-008: macOS Support Still Requires Real-Mac Final Validation

Status: `open`
Severity: Low
Area: macOS release readiness

Evidence:

- `docs/macos_support.md` explicitly states final real-Mac validation is
  separate from repository verification.
- 2026-06-26 update: PR #117 is merged, `python
  scripts\check-macos-support-gate.py --skip-github` passes, and focused macOS
  support tests pass locally, but GitHub issue #116 remains open because the
  final real operator Mac interactive evidence bundle is not attached.
- 2026-06-26 update: after #99/#136 closure, `python
  scripts\check-macos-support-gate.py --skip-github`, `pytest
  tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`, and
  `python -m compileall -q scripts tests` still pass on main. #116 remains open
  only for the real operator Mac report/log/system-evidence/evidence-zip
  attachment.
- 2026-06-26 current-main re-audit before #137 creation: full Python suite,
  full Rust core tests, Rust release build, compileall, final live UI evidence
  validator, Rust performance evidence validator, optional evidence regression
  gate with both required evidence flags, macOS support gate, focused macOS
  tests, and diff hygiene all pass. GitHub issue #116 still has only the final
  real operator Mac validation checkbox unchecked.
- 2026-06-27 post-#142 next issue analysis: #116 is still the only open GitHub
  issue. `python scripts\check-macos-support-gate.py` passes, but
  `python scripts\check-macos-support-gate.py --final` fails as expected
  because no macOS manual validation report was found under `build/` and no
  successful manual `macOS App Validation` `workflow_dispatch` run exists for
  the current merged main HEAD.
- 2026-07-10 fresh final-gate run at `edd0c75` confirms both conditions remain:
  no real-Mac report under `build/` and no successful manual workflow run for
  the current merged main HEAD.

Next action:

1. Do not call macOS support production-ready until the final manual validation
   evidence bundle exists.
2. Before closing #116, run the manual macOS validation flow on a real operator
   Mac from current `main`, including the signed/notarized manual workflow
   artifact run for the same merged main HEAD.

### TF-STATUS-009: Merge Import Reapplied Post-Load DDL

Status: `closed`
Severity: High
Area: Rust Core dump.import

Evidence:

- 2026-06-26 discovery: `dump_import()` applied post-load DDL unconditionally,
  including `merge` imports.
- 2026-06-26 update: `should_apply_post_load_ddl()` limits post-load DDL to
  `replace` and `recreate`; `merge` emits an explicit existing-schema skip
  phase.

Impact:

- Merge import no longer treats an existing target schema as if it had just
  been recreated.

Next action:

1. Keep the policy tests for merge/recreate behavior.

### TF-STATUS-010: Shadow Full Replacement Architecture Retired

Status: `closed`
Severity: High
Area: Rust Core dump.import

Evidence:

- The original recovery design required full replacement to load into a shadow
  schema/database, verify, then switch after verification.
- 2026-06-26 decision: current TunnelForge support is direct
  `replace`/`recreate`/`merge` import against the selected target database, not
  atomic shadow-schema replacement.
- The recovery design, recovery plan, and final remediation report now state
  this explicitly so future sessions do not implement a partial shadow helper
  without a new product decision.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/133

Impact:

- Full replacement remains non-atomic direct replacement. Strict manifest
  validation, row verification, post-load validation, classified errors, and
  import reports are the supported safety boundary.

Next action:

1. Do not reintroduce shadow replacement unless a new product decision includes
   DB-specific switch, rollback, cleanup, and worker endpoint semantics.
2. Keep UI wording aligned with direct replacement behavior.

### TF-STATUS-011: MySQL FK Charset/Collation Fidelity

Status: `closed`
Severity: High
Area: Rust Core dump.run manifest / dump.import plan

Evidence:

- The recovery design calls out MySQL charset/collation/table-option fidelity
  and treats `ERROR 3780` as a schema fidelity/import-plan validation problem.
- MySQL column inspection now captures `CHARACTER_SET_NAME` and
  `COLLATION_NAME` and preserves them in the native column type literal stored
  in the dump manifest schema.
- `dump.import` and migration post-load DDL now validate FK column
  charset/collation compatibility before applying FK DDL.
- Focused tests cover incompatible FK text collations, matching collations,
  metadata capture in MySQL inspect SQL, and MySQL-to-PostgreSQL type mapping
  with MySQL character options stripped.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/134

Impact:

- The import pipeline now classifies the `ERROR 3780` class of FK
  charset/collation mismatch as `post_load_validation_failed` before sending
  incompatible FK DDL to the target database.

Next action:

1. Keep FK fidelity regression coverage aligned with future schema metadata
   changes.
2. Track broader table-option fidelity separately if table engine/table
   collation preservation becomes a release requirement beyond FK validation.

### TF-STATUS-012: Import Cumulative Telemetry

Status: `closed`
Severity: Medium
Area: Import UI / Rust Core dump.import events

Evidence:

- GitHub issue #128 identified that Import speed and ETA could be mistaken for
  end-to-end throughput because the UI showed recent chunk speed without a
  separate cumulative baseline.
- Rust Core `dump.import` row progress events now include table-local rows and
  manifest-wide cumulative rows through `table_rows_done`,
  `table_rows_total`, `overall_rows_done`, and `overall_rows_total`.
- The Python bridge forwards the cumulative fields and calculates visible
  progress from the manifest-wide denominator when available.
- The Import dialog now displays cumulative processed rows, average speed since
  Import start, current chunk speed, and row-based ETA only while data load is
  still in progress.
- Post-load DDL emits an explicit phase event so the UI can stop implying a
  row-based ETA after data reaches 100%.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/128

Impact:

- Import progress is now anchored to the dump manifest row total instead of the
  current table/chunk alone, while recent chunk throughput remains separately
  labeled as current speed.

Next action:

1. Re-check wording with real long-running imports if additional post-load
   phases are split out later.

### TF-STATUS-013: MySQL JSON Fallback Insert Encoding

Status: `closed`
Severity: High
Area: Rust Core dump.import / MySQL INSERT fallback

Evidence:

- GitHub issue #118 reported MySQL `ERROR 3140 (22032): Invalid JSON text:
  "Invalid encoding in string."` while importing
  `ai_phase1_cache.result_json`.
- MySQL JSON fallback INSERT literals now use the `_utf8mb4` introducer so
  JSON text is interpreted with the character set required by MySQL JSON
  parsing, regardless of the connection/session default character set.
- Import session tuning now removes `NO_BACKSLASH_ESCAPES` while data is being
  loaded so JSON escape sequences generated by TunnelForge are interpreted
  consistently during fallback INSERT.
- Focused tests cover utf8mb4 JSON literal generation, preservation of JSON
  escape backslashes, and the adjusted MySQL import session tuning SQL.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/118

Impact:

- MySQL JSON columns containing non-ASCII text and escaped quotes are less
  likely to fail during safe INSERT fallback when `LOAD DATA LOCAL` is
  unavailable.

Next action:

1. Prefer live reproduction evidence if another `ERROR 3140` report appears,
   because malformed source JSON should still fail as data-invalid.

### TF-STATUS-014: Large SQL Editor Rendering

Status: `closed`
Severity: Medium
Area: SQL editor UI

Evidence:

- GitHub issue #86 reported severe slowdown when opening a large SQL file of
  roughly 645KB.
- `SQLEditorTab` now detects SQL text at or above 512KB and enables a
  large-document mode before calling `setPlainText`.
- Large-document mode detaches the syntax/validation highlighter, stops the
  validation debounce timer, and skips whole-document validation requests.
- The tab shows an inline notice that syntax highlighting and real-time
  validation are disabled for the large SQL document.
- Returning to small content re-enables the normal validator highlighter.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/86

Impact:

- Large SQL files avoid the expensive whole-document regex highlighter and SQL
  validator passes that were the dominant app-side rendering cost.

Next action:

1. Revisit true virtualized SQL rendering only if large plain-text insertion
   remains a measured bottleneck after this guard.

### TF-STATUS-015: SQL Editor Schema Tree

Status: `closed`
Severity: Medium
Area: SQL editor UI

Evidence:

- GitHub issue #92 requested a SQL editor side panel for schema/table browsing
  so users do not need to type table names manually.
- The SQL editor now has a left-side `스키마 / 테이블` tree panel next to the
  editor/results splitter.
- The tree shows DB/schema roots from the SQL editor selector and populates
  tables and columns under the currently loaded schema metadata.
- Clicking a table item inserts the table identifier into the current editor,
  quoted with backticks for MySQL and double quotes for PostgreSQL.
- Focused tests cover tree population from loaded metadata and table-click
  insertion into the editor.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/92

Impact:

- SQL editor users can discover available schemas/tables/columns in-place and
  insert table names without memorizing or manually typing them.

Next action:

1. Consider column insertion or drag/drop later if users ask for richer query
   composition.

### TF-STATUS-016: MySQL Post-Load DDL Table-Full Guidance

Status: `closed`
Severity: Medium
Area: Rust Core dump.import diagnostics

Evidence:

- GitHub issue #126 reported MySQL `ERROR 1114 (HY000): The table
  '#sql-1cbc_17b' is full` during replace import post-load DDL.
- Earlier handling classified the failure as `post_load_validation_failed` and
  included the exact failing post-load DDL statement.
- The current update adds a specific guidance suffix for MySQL table-full
  errors, telling the operator that target MySQL storage or temporary table
  space is full and to increase disk space, `tmpdir` capacity, or
  `innodb_temp_data_file_path` before retrying.
- Focused regression coverage verifies the `ERROR 1114` guidance while keeping
  the existing SQL-context classification behavior.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/126

Impact:

- TunnelForge now distinguishes this class as an actionable target
  environment/resource condition instead of leaving users with a raw MySQL
  temporary table name.

Next action:

1. If another `ERROR 1114` report appears after this guidance, collect target
   MySQL storage, `tmpdir`, and InnoDB temporary tablespace evidence rather than
   changing import semantics first.

### TF-STATUS-017: Rust Core Performance Evidence Is Durable

Status: `closed`
Severity: High
Area: Rust Core migration performance evidence

Evidence:

- GitHub issue #99 requires MySQL/PostgreSQL 1M row migration+verify and 10M row
  streaming/resume/verify evidence before the Rust DB Core Service epic can be
  closed.
- `RUST_CORE_REQUIRE_PERF_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File
  scripts\rust-core-regression-gate.ps1` passes on this machine because the
  expected performance JSONL files exist under `migration_core\target`.
- `migration_core\target` is ignored by git, so those JSONL files are local
  machine state rather than durable repo, CI, release, or handoff evidence.
- 2026-06-26 update: the four required JSONL files are archived under
  `reports\rust_core_performance`, with `README.md` documenting refresh and
  validation.

- `scripts\validate-rust-core-performance-evidence.py` validates that all four
  files exist, contain successful Rust Core `result` events, prove the required
  1M/10M row counts, and do not report verification mismatches.
- `scripts\rust-core-regression-gate.ps1` now uses the archived evidence
  validator when `RUST_CORE_REQUIRE_PERF_EVIDENCE=1`, so a clean checkout audits
  committed evidence instead of ignored `target` artifacts.
- Parent GitHub epic: https://github.com/sanghyun-io/tunnelforge/issues/99
- Follow-up GitHub issue:
  https://github.com/sanghyun-io/tunnelforge/issues/135

Impact:

- #99 can now point at repo-preserved 1M/10M performance evidence and a
  repeatable validator instead of relying on one developer's ignored local
  `target` directory.

Next action:

1. Refresh the archived evidence if Rust Core migration/verify streaming
   semantics change.

### TF-STATUS-018: Rust Core Live UI Performance Evidence Complete

Status: `closed`
Severity: High
Area: Rust Core live migration / PyQt responsiveness evidence

Evidence:

- GitHub issue #99 requires MySQL -> PostgreSQL and PostgreSQL -> MySQL 1M row
  migration+verify to complete without UI freeze.
- TF-STATUS-017 preserves Rust Core 1M/10M JSONL evidence, but those archived
  files alone do not prove bidirectional live database coverage or PyQt
  responsiveness during a live 1M migration run.
- Cross-engine worker/dialog tests cover progress, checkpoint, resume, and
  worker signal plumbing, but they do not run a live 1M row UI workflow.
- 2026-06-26 update: `scripts\validate-live-ui-migration-evidence.py` and
  `reports\live_ui_migration\live-ui-migration-evidence.template.json` now
  define the machine-checkable final evidence shape for #136.
- 2026-06-26 update: local Docker-backed MySQL 8.4 and PostgreSQL 16 endpoints
  passed the existing `live_roundtrip` Rust integration tests, covering inspect,
  readiness, guide, preflight, MySQL -> PostgreSQL migrate+verify, and
  PostgreSQL -> MySQL migrate+verify on small fixtures.
- 2026-06-26 update: `scripts\capture-live-ui-migration-evidence.py` now seeds
  local `tf-live-*` containers with deterministic `tf_live_*` tables, runs both
  directions through `CrossEngineMigrationWorker`, samples Qt event-loop
  heartbeat gaps while the migrate worker is active, and writes the
  validator-compatible report.
- 2026-06-26 update: a 1,000-row smoke run of the capture helper succeeded for
  both directions and was intentionally rejected by the final validator because
  it was below the required 1,000,000 rows.
- 2026-06-26 update: `live-ui-migration-evidence-1m-local-partial.json`
  preserves a local Docker 1M bidirectional PyQt worker run. MySQL ->
  PostgreSQL and PostgreSQL -> MySQL each migrated and verified 1,000,000 rows,
  emitted 201 worker progress events, and recorded a 125ms max Qt heartbeat gap
  against a 1000ms threshold. The file remains partial because the 10M RSS
  fields are intentionally 0 and therefore fail the final validator.
- 2026-06-26 update: `migration_core\tests\stress_rss.rs` adds an ignored
  10M synthetic adapter RSS harness. The committed harness measured 10M
  resume+verify success, 0 mismatches, and 921MB peak RSS against a 2048MB
  limit, writing `reports\live_ui_migration\stress-10m-rss.json`.
- 2026-06-26 update: `reports\live_ui_migration\live-ui-migration-evidence.json`
  combines the live bidirectional 1M PyQt worker evidence with the 10M RSS
  measurement. `python scripts\validate-live-ui-migration-evidence.py
  reports\live_ui_migration\live-ui-migration-evidence.json` passes with 2
  directions and 12,000,000 rows checked.
- 2026-06-26 update: `scripts\rust-core-regression-gate.ps1` can now require
  the live UI evidence validator when `RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1`.
- GitHub issue #136 tracked this final #99 closure evidence and is now closed.
- Parent GitHub epic: https://github.com/sanghyun-io/tunnelforge/issues/99
- Follow-up GitHub issue:
  https://github.com/sanghyun-io/tunnelforge/issues/136

Impact:

- #99/#136 now have durable validator-passing evidence for live bidirectional
  1M PyQt worker responsiveness and 10M stress RSS bounds.
- Keep the final validator in the release evidence path if migration worker,
  Rust Core migration streaming, or stress adapter semantics change.

Next action:

1. Refresh `reports\live_ui_migration\live-ui-migration-evidence.json` only if
   migration worker, Rust Core streaming, heartbeat sampling, or stress/RSS
   semantics change.

### TF-STATUS-019: One-Click Migration UI Dry-Run Preview Gate

Status: `closed`
Severity: Medium
Area: One-Click migration UI / Rust Core integration

Evidence:

- GitHub issue #124 is closed because One-Click migration orchestration moved
  into Rust Core, but its acceptance criteria intentionally kept the hidden UI
  gate disabled until the workflow is production-ready.
- `tunnelforge-core service.hello` advertises `oneclick.run`,
  `oneclick.preflight`, `oneclick.analyze`, `oneclick.recommend`,
  `oneclick.apply_fixes`, `oneclick.validate`, and `oneclick.report`.
- `src\ui\dialogs\oneclick_migration_dialog.py` uses
  `DbCoreFacade.run_oneclick(...)` and fails closed unless the connector has
  the Rust Core facade shape.
- `src\ui\dialogs\migration_dialogs.py` sets
  `ONE_CLICK_MIGRATION_FEATURE_ENABLED = True`, exposing the entry point as
  "One-Click Dry-run Preview" only.
- 2026-06-26 update: created GitHub issue #137 to track the production-readiness
  decision and evidence required before changing the feature flag.
- 2026-06-26 update: `OneClickMigrationWorker` now rejects non-dry-run payloads
  while `ONECLICK_REAL_EXECUTION_ENABLED = False`, and the dialog locks the
  Dry-run checkbox checked/disabled until the real-execution gate is complete.
- 2026-06-26 update: `docs\oneclick_readiness.md` defines the current
  dry-run-only preview support scope; `reports\oneclick_readiness` now contains
  validator-backed local MySQL Rust Core `oneclick.run` dry-run evidence.
- `scripts\validate-oneclick-dry-run-evidence.py` verifies that the evidence
  includes all `oneclick.*` service capabilities, preview UI enabled,
  real-execution disabled, `dry_run=true`, every expected phase, a 100%
  progress event, zero validation remnants, and the explicit dry-run execution
  log.
- `scripts\rust-core-regression-gate.ps1` can require the evidence when
  `RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE=1`.
- 2026-06-26 follow-up analysis in `docs\oneclick_readiness.md` concluded that
  the then-current backend supported hidden or dry-run preview scope only, not
  full enablement.
- 2026-06-26 update: the migration analyzer now exposes the entry point as
  `One-Click Dry-run Preview`, with tooltip copy that says no real changes are
  performed and automatic SQL fixes are not enabled.

GitHub issue:

- https://github.com/sanghyun-io/tunnelforge/issues/137

Impact:

- Users can run the Rust Core One-Click flow as a dry-run preview from the
  migration analyzer.
- Real execution and automatic SQL fix claims remain out of scope for this
  closed dry-run preview gate.

Closure evidence:

1. Commit `40cc5ca` exposed `One-Click Dry-run Preview`, refreshed
   machine-checkable dry-run evidence, and kept real execution disabled.
2. Follow-up real-execution work was split to GitHub #138.

### TF-STATUS-020: One-Click Real Execution / Automatic Fix Coverage

Status: `closed`
Severity: High
Area: One-Click migration UI / Rust Core automatic fixes

Evidence (historical closure snapshot, superseded by the TF-STATUS-097 Phase A
fail-closed gate):

- GitHub #138 tracked the remaining scope after #137: define, implement, and
  prove the automatic fix classes before real One-Click execution is enabled.
- At closure, Rust Core recommendation behavior marked `deprecated_engine` payload
  issues with `table_name` as automatic candidates using `engine_innodb`
  recommendation metadata. `oneclick.apply_fixes` can execute only those
  planned `engine_innodb` actions through Rust Core `MigrationAdapter`; other
  issue classes remain manual/skipped or blocked as disallowed.
- `scripts\validate-oneclick-real-execution-evidence.py` defines the
  machine-checkable #138 evidence contract for a controlled local
  `deprecated_engine -> engine_innodb` non-dry-run proof. It requires a safe
  `tf_oneclick_` schema, app real execution still disabled, all `oneclick.*`
  service capabilities, no disallowed fix attempts, and before/after table
  engine evidence proving only the allowed fix was applied.
- `reports\oneclick_readiness\oneclick-real-execution-evidence.template.json`
  documents the required evidence shape.
- `reports\oneclick_readiness\oneclick-real-execution-evidence.json` now
  contains validator-backed local MySQL evidence proving Rust Core
  `oneclick.apply_fixes` converted
  `tf_oneclick_real_execution.tf_oneclick_legacy_engine_table` from `MyISAM`
  to `InnoDB` with `ONECLICK_REAL_EXECUTION_ENABLED = False`.
- MySQL live inspection now emits deprecated-engine markers for MyISAM base
  tables, and One-Click converts those markers into typed
  `deprecated_engine` issues that can be recommended as `engine_innodb`.
- At closure, the UI-facing Rust command `oneclick.run dry_run=false` sequenced
  the validated `engine_innodb` apply path.
- At closure, `src\ui\dialogs\oneclick_migration_dialog.py` kept
  `ONECLICK_REAL_EXECUTION_ENABLED = True`, left Dry-run checked by default,
  and failed closed when a non-dry-run payload lacked backup confirmation.
- At closure, `src\ui\dialogs\migration_dialogs.py` exposed `One-Click Migration` with
  user-facing copy that says Dry-run is the default and the only automatic
  non-dry-run scope is verified MyISAM/deprecated engine tables becoming
  `InnoDB` after backup confirmation.

GitHub issue:

- https://github.com/sanghyun-io/tunnelforge/issues/138

Historical impact at closure:

- Users could run dry-run inspection by default.
- Users could opt into non-dry-run only after backup confirmation, and Rust Core
  applied only the validated `deprecated_engine -> engine_innodb` strategy.
- Automatic remediation for every other issue class remains out of scope and
  must be tracked separately.

Closure evidence:

1. Rust Core contract tests, local MySQL before/after evidence, and the
   machine-checkable real-execution validator all pass for
   `deprecated_engine -> engine_innodb`.
2. PyQt tests prove the dialog keeps Dry-run default, allows limited
   non-dry-run with backup confirmation, and rejects non-dry-run without backup
   confirmation.
3. Docs and user-facing copy document the exact automatic/manual split.

Next action:

1. Create separate issues for any additional automatic fix class before
   enabling it.
2. Keep production database usage out of One-Click real execution until there
   is explicit production-readiness evidence.

### TF-STATUS-021: One-Click Charset/Collation Automatic Fix Coverage

Status: `closed`
Severity: High
Area: One-Click migration UI / Rust Core automatic fixes

Evidence (historical closure snapshot, superseded by the TF-STATUS-097 Phase A
fail-closed gate):

- GitHub #139 tracks charset/collation automatic fix coverage as a separate
  follow-up after #138.
- `docs\oneclick_readiness.md` now limits `charset_issue` automation to
  supplied complete `charset_collation_fk_safe` contracts with explicit target,
  FK order, rollback SQL, and local-safe evidence.
- Rust Core One-Click apply logic allowlists `deprecated_engine -> engine_innodb`
  plus the supplied complete `charset_issue -> charset_collation_fk_safe`
  contract shape; missing or incomplete charset data remains manual/fail-closed.
- `scripts\validate-oneclick-charset-evidence.py` and
  `reports\oneclick_readiness\oneclick-charset-evidence.template.json` now
  define the required #139 evidence shape. The validator requires local MySQL
  source, safe `tf_oneclick_` schema/table identifiers, explicit
  `utf8mb4`/collation target proof, FK-valid after-state, rollback metadata,
  and zero disallowed fix attempts.
- `scripts\rust-core-regression-gate.ps1` can require completed charset
  evidence with `RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE=1`; it fails
  until `reports\oneclick_readiness\oneclick-charset-evidence.json` exists.
- `docs\oneclick_readiness.md` now defines the #139 policy boundary: only
  table-level `charset_issue -> charset_collation_fk_safe` with explicit
  target charset/collation, FK closure/order evidence, rollback metadata, and
  local `tf_oneclick_` evidence can become automatic in a future change.
- 2026-06-26 historical next-issue analysis selected #139 as the next
  in-repo issue after that `main` merge. GitHub #116 remained external real-Mac
  evidence work, while #139 had concrete Rust Core, PyQt, and local MySQL
  evidence tasks that could proceed in this repository. #139 is now closed.
- Existing Python Fix Wizard charset code already generates FK DROP, table
  conversion, FK ADD, and recovery SQL, but #139 must not route One-Click real
  execution through Python DB drivers. The reusable idea is the contract shape;
  execution ownership must stay in `tunnelforge-core`.
- `scripts\capture-oneclick-charset-evidence.py` and
  `tests\test_oneclick_charset_capture.py` now implement the #139 local MySQL
  capture/report layer through Rust DB Core APIs. The helper seeds only safe
  `tf_oneclick_` scopes, captures before/after charset state, captures FK
  evidence, executes `oneclick.apply_fixes dry_run=false`, and writes a
  validator-compatible report.
- `migration_core\src\lib.rs` has a `charset_collation_fk_safe` contract helper
  covered by Rust tests. It validates safe `tf_oneclick_` evidence identifiers,
  explicit charset/collation target, FK order table coverage, rollback SQL, and
  generated table-level conversion SQL.
- Rust Core now gates `charset_issue -> charset_collation_fk_safe`
  recommendations on complete request `charset_contracts[]` evidence and keeps
  charset issues manual when that evidence is missing or incomplete.
  `oneclick.apply_fixes dry_run=true` can preview charset `planned_fixes` from
  the same contract.
- Rust Core command-level `oneclick.apply_fixes dry_run=false` can now execute
  complete `charset_collation_fk_safe` contract SQL through the adapter path and
  returns rollback metadata, target charset/collation, FK order, SQL list, and
  success/error state in `applied_fixes`.
- `reports\oneclick_readiness\oneclick-charset-evidence.json` now provides
  validator-backed local MySQL evidence for the command-level charset path.
  `RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE=1` passes.
- UI-facing Rust Core `oneclick.run dry_run=false` now merges supplied
  `issues[]` and `charset_contracts[]`, shifts charset contract indexes behind
  inspection-derived issues, and executes the same allowlisted complete
  `charset_collation_fk_safe` contract shape. A live MySQL regression proves
  FK-connected local `tf_oneclick_` tables convert from `utf8mb3` /
  `utf8mb3_general_ci` to `utf8mb4` / `utf8mb4_0900_ai_ci`.
- PyQt rendering/count/copy coverage now proves charset automatic, manual, and
  skip payloads are counted and logged accurately.
- GitHub #140 / TF-STATUS-022 is closed after local PyQt-triggered derivation
  evidence proved `OneClickMigrationWorker._core_payload()` feeds derived
  `issues[]` / `charset_contracts[]` into `oneclick.run dry_run=false`.

GitHub issue:

- https://github.com/sanghyun-io/tunnelforge/issues/139

Impact:

- Users could run One-Click dry-run and the validated engine fix. Charset
  execution was available only for complete local-safe Rust Core contracts,
  including contracts derived by Rust Core for the PyQt worker path in local
  `tf_oneclick_` evidence scopes.
- Production charset/collation execution remains out of scope without separate
  production-readiness evidence.

Next action:

1. Keep #139 evidence refreshed if the supplied charset contract, validator, or
   One-Click event payload changes.
2. Keep #140 derivation evidence refreshed if PyQt payload construction,
   Rust Core derivation, or One-Click event payloads change.

### TF-STATUS-024: Direct DB Export/Import Uses Connector Host

Status: closed
Severity: High
Area: Export/Import UI

Evidence:

- 2026-06-26 audit found `RustDumpExportDialog.do_export()` and
  `RustDumpImportDialog.do_import()` created `RustDumpConfig` with
  `host="127.0.0.1"` even when the active connector represented a direct
  remote DB connection.
- Tunnel flows normally expose `connector.host == "127.0.0.1"` because they
  connect through a local forwarded port, but direct flows must preserve the
  connector host.
- RED/GREEN tests now cover both dialogs with a direct connector at
  `db.example.com:3307`.

Resolution:

- Export and Import dialogs now set `RustDumpConfig.host` from
  `connector.host`, falling back to `127.0.0.1` only when the connector lacks a
  host attribute.
- Focused tests verify host, port, user, and password are forwarded to the Rust
  DB Core worker config.

Next action:

1. Keep direct-connection endpoint coverage aligned if Export/Import worker
   construction moves or if PostgreSQL dump support is added later.

### TF-STATUS-025: macOS Artifact Lookup Uses Current Main After PR Merge

Status: closed
Severity: High
Area: macOS release validation

Evidence:

- 2026-06-27 next-issue analysis found `scripts/macos-download-validation-artifacts.sh`
  still defaulted to the latest successful manual `macOS App Validation` run
  for `PR #117 head`.
- That conflicted with the already-fixed final report gate policy:
  `scripts/check-macos-support-gate.py` expects report Git SHA to match PR head
  before merge, or current merged main HEAD after PR #117 is merged.
- RED/GREEN coverage now simulates merged PR #117 with a fake `gh` binary and
  fails unless the artifact lookup query uses local `git rev-parse HEAD`
  instead of the stale PR head.

Resolution:

- `scripts/macos-download-validation-artifacts.sh` now resolves the default
  artifact run target as PR head before merge, or current merged main HEAD after
  PR #117 is merged.
- `scripts/macos-manual-validation-report.sh` and `docs/macos_support.md`
  describe the same default, keeping operator instructions aligned with the
  final gate.

Next action:

1. Keep the artifact lookup default aligned with
   `check-macos-support-gate.py::expected_final_report_sha` if the final
   validation branch/merge policy changes.
2. #116 still requires real operator Mac validation evidence before it can be
   closed.

### TF-STATUS-026: Schedule Guide Stays Internal While Feature Is Hidden

Status: closed
Severity: Medium
Area: Docs/UI feature flags

Evidence:

- 2026-06-27 stale-doc scan found that `SCHEDULE.md` opened with the correct
  hidden-feature warning, but later still told readers to click the toolbar
  schedule button, use the backup log tab, and run schedules immediately as if
  the feature were public.
- `src/ui/main_window.py` still sets `SCHEDULE_FEATURE_ENABLED = False`, so
  those instructions were not reachable in normal builds.
- RED/GREEN coverage in `tests/test_schedule_docs.py` now rejects public-UI
  wording while the guide is marked disabled/internal.

Resolution:

- `SCHEDULE.md` is now titled and worded as an internal implementation /
  reactivation memo.
- Current-user steps were converted into reactivation verification items for
  entry point, create/save, immediate run, logs, and app lifecycle behavior.

Next action:

1. If `SCHEDULE_FEATURE_ENABLED` is intentionally enabled, rewrite
   `SCHEDULE.md` back into a public user guide and add fresh UI/runtime
   verification evidence in the same session.

### TF-STATUS-027: One-Click Production Scope Wording Matches Limited Gate

Status: closed
Severity: Medium
Area: One-Click migration docs

Evidence (historical closure snapshot, superseded by the TF-STATUS-097 Phase A
fail-closed gate):

- 2026-06-27 repo-side scan found `docs/oneclick_readiness.md` still said
  `Production database usage` was not supported, even though that release's UI
  gate allowed backup-confirmed non-dry-run execution for the validated
  `deprecated_engine -> engine_innodb` path.
- The same document already stated `ONECLICK_REAL_EXECUTION_ENABLED = True`,
  Dry-run default, backup confirmation requirement, and limited
  `engine_innodb` execution, so the old production-usage bullet was stale
  scope wording rather than the active implementation policy.
- RED/GREEN coverage now rejects the broad stale bullet and requires the docs
  to distinguish backup-confirmed `engine_innodb` from unsupported broad
  production automatic remediation and production charset/collation execution.

Resolution:

- At closure, `docs/oneclick_readiness.md` stated that broad production
  automatic remediation was unsupported, while that release's only non-dry-run
  production-facing path was backup-confirmed
  `deprecated_engine -> engine_innodb`.
- Production charset/collation execution remains explicitly unsupported.

Next action:

1. Keep this wording aligned if the One-Click real-execution allowlist expands
   beyond `engine_innodb`.

### TF-STATUS-028: Current Full Python Suite Count Refreshed

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- 2026-06-27 `pytest -q` completed with `1827 passed, 5 warnings`.
- `docs/current_status.md` still reported the previous `1786 passed, 5
  warnings` count from before the added documentation regression tests.
- RED/GREEN coverage now rejects the stale `1786 passed` line and requires the
  current `1827 passed, 5 warnings` evidence.

Resolution:

- The top `pytest -q` verification row now reports `1827 passed, 5 warnings`.
- The verification log records the exact full-suite refresh command.

Next action:

1. Refresh the count again whenever new tests are added and a full `pytest -q`
   run is completed.

### TF-STATUS-029: Current Baseline Verification Heading Is Not Stale-Dated

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- 2026-06-27 status audit found the top verification table still used
  `## Verified On 2026-06-26` even after its `pytest -q` row had been refreshed
  with 2026-06-27 evidence.
- That heading made the mixed baseline ambiguous: the full Python suite count
  was current, while broader Rust/macOS rows were preserved from the 2026-06-26
  sweep.
- RED/GREEN coverage now rejects the stale-dated heading and requires explicit
  wording that the full-suite count was refreshed on 2026-06-27.

Resolution:

- The section is now `## Current Baseline Verification`.
- The paragraph under the heading states which evidence was refreshed on
  2026-06-27 and which broader baseline rows are preserved until rerun.

Next action:

1. If a full broad baseline sweep is rerun, replace the preservation note with
   that sweep's concrete date and command evidence.

### TF-STATUS-030: Current Main Next-Issue Re-Audit

Status: closed
Severity: Low
Area: Status documentation / Rust Core boundary audit

Evidence:

- 2026-06-27 main alignment check found `main` aligned with `origin/main`;
  latest pushed commits already include the recent schedule, One-Click, and
  status documentation fixes.
- GitHub issue scan found #116 as the only open issue.
- `python scripts\check-macos-support-gate.py --skip-github` passed.
- `python scripts\check-macos-support-gate.py` passed against GitHub state,
  confirming #110-#115 closure, #116/M6 tracking, merged PR #117 state, and
  green repository-side checks.
- `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`
  passed with 53 tests.
- Rust Core boundary scan checked DB driver/tool names, subprocess callers,
  SQL cursor/commit/rollback usage, and disabled feature flags. The scan found
  no new repo-side Rust Core baseline violation: legacy-shaped
  `MySQLConnector` and `PostgresConnector` now open `DbCoreFacade`
  connections and expose `RustDbConnection`/`RustDbCursor` shims, while hidden
  scheduler SQL execution uses `create_rust_db_connector`.

Resolution:

- No new GitHub issue was created from this pass because the only remaining
  actionable item is already tracked as #116 / TF-STATUS-008 and requires
  external real-Mac operator evidence.
- The re-audit is recorded here so later sessions do not repeat the same
  connector-name false positive without new evidence.

Next action:

1. Keep #116 open until a real Mac operator attaches the completed evidence
   bundle and final handoff comment.
2. If a future scan finds an actual non-Rust DB operation owner path, create a
   separate GitHub issue before implementation.

### TF-STATUS-031: Current Baseline Counts After Re-Audit Coverage

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- Adding the TF-STATUS-030 current-status regression test changed the full
  Python suite size.
- The top `Current Baseline Verification` macOS focused row still preserved
  `51 passed` even though the current 2026-06-27 focused run is `52 passed`.
  Later TF-STATUS-038 coverage superseded this again to `53 passed`.
- RED/GREEN coverage now rejects `PASS, 51 passed` inside the current baseline
  section and rejects the previous 1793-test full-suite count as the current
  pytest row.

Resolution:

- The top `pytest -q` verification row now reports the current full-suite count.
- The top macOS focused verification row now reports the current focused count.
- The focused verification table records the refreshed full-suite command.

Next action:

1. Refresh these counts whenever tests are added and the matching verification
   commands are rerun.

### TF-STATUS-032: Focused Verification Table Has No Duplicate Check Rows

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- The `Focused Verification On 2026-06-27` table listed
  `python scripts\check-macos-support-gate.py --skip-github` twice.
- RED/GREEN coverage now extracts focused verification command rows and rejects
  duplicate command entries.

Resolution:

- Removed the duplicate focused verification row while preserving the full
  #116 gate and skip-GitHub gate evidence.

Next action:

1. Keep focused verification tables deduplicated when adding future evidence
   rows.

### TF-STATUS-033: Current Baseline Table Has No Duplicate Check Rows

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- The `Current Baseline Verification` table listed `tunnelforge-core
  service.hello` twice: once for dump/import/migration capability evidence and
  once for One-Click capability evidence.
- RED/GREEN coverage now extracts current baseline check rows and rejects
  duplicate command entries.

Resolution:

- Merged the duplicate `service.hello` rows into one row covering dump/import,
  migration, and One-Click command advertisement.

Next action:

1. Keep current baseline command rows unique; add detail to the result cell
   rather than duplicating a command row.

### TF-STATUS-034: Rust Core Export/Import Menu Wording

Status: closed
Severity: Low
Area: Export/Import UI

Evidence:

- The tunnel context menu still used legacy shell-branded action labels and
  handler names even though Export/Import now routes through Rust DB Core.
- RED/GREEN coverage now scans `src/ui/main_window.py` and rejects the legacy
  labels/handlers while requiring Rust DB Core action labels and handler names.

Resolution:

- The tree export/import shortcuts now dispatch to `_context_rust_core_export`
  and `_context_rust_core_import`.
- The tunnel context menu now shows `Rust DB Core Export` and
  `Rust DB Core Import`.
- The focused regression is recorded in
  `tests/test_main_window_export_import_labels.py`.

Next action:

1. Keep user-facing Export/Import wording aligned with Rust Core ownership when
   adding new context-menu or toolbar actions.

### TF-STATUS-035: One-Click Fallback Dry-Run Tooltip

Status: closed
Severity: Low
Area: One-Click migration UI

Evidence (historical closure snapshot, superseded by the TF-STATUS-097 Phase A
fail-closed gate):

- At closure, `ONECLICK_REAL_EXECUTION_ENABLED` was true, but the disabled fallback
  tooltip still described real execution as blocked until GitHub #138 completed.
- GitHub #138 was already closed, and that release supported limited
  backup-confirmed real execution with dry-run as the default.
- RED/GREEN coverage now forces the disabled fallback tooltip to avoid closed
  issue wording and to state that real execution is disabled in this build.

Resolution:

- The disabled fallback tooltip now says One-Click real execution is
  `disabled in this build` and that dry-run remains available for Rust Core
  recommendation previews.
- The regression is recorded in `tests/test_oneclick_rust_core_gate.py`.

Next action:

1. Keep fallback/feature-flag copy aligned with the current One-Click support
   matrix when flags change.

### TF-STATUS-036: One-Click Module Scope Docstring

Status: closed
Severity: Low
Area: One-Click migration UI

Evidence (historical closure snapshot, superseded by the TF-STATUS-097 Phase A
fail-closed gate):

- The module docstring in `src/ui/dialogs/oneclick_migration_dialog.py` still
  said the whole migration process is automatically executed.
- Behavior at closure was narrower: Rust DB Core owned the workflow, dry-run
  was the default, execution paused for plan confirmation, and non-dry-run
  changes required backup confirmation with validated limited scope.
- At closure, RED/GREEN coverage rejected the overbroad automatic-execution
  phrase and required Rust DB Core dry-run default and limited real-execution
  wording.

Resolution:

- Reworded the module docstring to describe Rust DB Core ownership, dry-run
  default, and backup-confirmed limited real execution.
- The regression is recorded in `tests/test_oneclick_rust_core_gate.py`.

Next action:

1. Keep One-Click source-level comments aligned with the supported execution
   matrix when the workflow expands.

### TF-STATUS-037: BUILD Installer Version Examples

Status: closed
Severity: Low
Area: Build documentation

Evidence:

- `BUILD.md` still showed stale 1.0.0 installer filename/AppVersion examples
  even though release version sources are aligned at `2.1.7` and the installer
  uses `MyAppVersion`.
- RED/GREEN coverage now rejects the stale installer example version and
  requires `{version}` / `{#MyAppVersion}` placeholders.

Resolution:

- Replaced stale Windows installer output/test examples with
  `TunnelForge-Setup-{version}.exe`.
- Updated the Inno Setup snippet to use `AppVersion={#MyAppVersion}` and note
  that it is synced from `src/version.py`.
- The regression is recorded in `tests/test_build_docs.py`.

Next action:

1. Keep build documentation examples version-neutral unless they intentionally
   document the current release version.

### TF-STATUS-038: macOS Manual Workflow Head Policy

Status: closed
Severity: High
Area: macOS release validation

Evidence:

- `scripts/macos-download-validation-artifacts.sh` already used PR head before
  merge, or current merged main HEAD after PR #117 has merged.
- `scripts/check-macos-support-gate.py` still resolved the successful manual
  `workflow_dispatch` `macOS App Validation` run from PR #117 head only.
- RED/GREEN coverage now proves merged-PR finalization uses local HEAD when
  matching the manual workflow artifact run.
- GitHub #116 now describes the manual workflow run policy as PR head before
  merge, or current merged main HEAD after PR #117 has merged.

Resolution:

- `check_manual_macos_validation_workflow()` now uses the same head policy as
  final report Git SHA and artifact download.
- `docs/macos_support.md` documents that the manual workflow_dispatch artifact
  run follows the same head policy.
- The regression is recorded in `tests/test_rust_core_packaging.py`.

Next action:

1. Keep final report Git SHA, artifact download head SHA, and manual workflow
   artifact run head SHA aligned whenever the #116 final gate changes.

### TF-STATUS-039: Post-Merge Next-Issue External Re-Audit

Status: closed
Severity: Low
Area: Status documentation / Rust Core boundary audit

Evidence:

- `git status --short --branch` shows `main` aligned with `origin/main`.
- `gh issue list --state open --limit 30` then returned only GitHub #116.
- `python scripts\check-macos-support-gate.py --skip-github` and
  `python scripts\check-macos-support-gate.py` both pass.
- Focused scans checked stale handoff wording, feature flags, direct DB driver
  paths, and SQL execution surfaces. SQL editor query execution also routes
  through the Rust connector shim via `create_sql_editor_connector()` and
  `create_rust_db_connector()`.

Resolution:

- No repo-side follow-up issue was confirmed during that pass. Therefore, no
  new GitHub issue was created from that pass; #116 remained blocked on
  external real-Mac operator validation evidence.
- This entry records the latest post-merge re-audit so future sessions do not
  re-open the SQL editor or legacy connector names as false positives without
  new evidence.

Next action:

1. Keep #116 open until the real-Mac evidence bundle is attached and the final
   device validation checkbox is checked.
2. A later scan did find confirmed repo-side evidence and created GitHub #142 /
   TF-STATUS-040.

### TF-STATUS-040: Legacy Python Auto-Fix Wizard Mutations

Status: closed
Severity: High
Area: Rust Core baseline / Migration Auto-Fix Wizard

Evidence:

- GitHub #142 tracked this issue separately from external macOS #116.
- `src/ui/dialogs/migration_dialogs.py` exposes `btn_auto_fix` and opens
  `FixWizardDialog` for auto-fixable migration issues.
- Before the fix, `src/ui/dialogs/fix_wizard_dialog.py` wired the final
  execution button to `FixWizardWorker(..., dry_run=False)`.
- Before the fix, `src/ui/workers/fix_wizard_worker.py` could call
  `FKSafeCharsetChanger.execute_safe_charset_change(..., dry_run=False)` and
  `BatchFixExecutor.execute_batch(..., dry_run=False)`.
- `src/core/migration_fix_wizard.py` directly generates and executes DDL/DML
  through `connector.connection.cursor().execute(...)`, `commit()`, and
  `rollback()`; this code remains available only behind the now fail-closed
  legacy worker path for SQL generation/dry-run behavior.
- This differs from the current baseline where `tunnelforge-core` should own
  DB mutation operations and Python/PyQt should orchestrate UI/signals/dialogs.
- The fix adds `tests/test_fix_wizard_dialog.py` coverage proving the legacy
  UI starts `FixWizardWorker` with `dry_run=True` and the worker rejects
  `dry_run=False`.

Resolution:

- No user-visible legacy Auto-Fix Wizard path can execute DB mutations through
  Python-owned fix logic.
- `ExecutionPage` is now labeled as SQL/Dry-run confirmation, calls
  `FixWizardWorker(..., dry_run=True)`, and explains that real DB changes must
  use a Rust Core-owned path.
- `FixWizardWorker` raises `RuntimeError` if constructed with `dry_run=False`,
  keeping the legacy mutation path fail-closed even if a caller bypasses the
  wizard UI.
- This remains separate from One-Click `oneclick.*`; its earlier limited
  real-execution path was Rust Core-owned and evidence-backed but is superseded
  by the TF-STATUS-097 Phase A fail-closed gate.

Next action:

1. Keep the legacy wizard dry-run/manual SQL only unless a future issue adds a
   Rust Core-owned command for this exact automatic-fix workflow.
2. Keep the fail-closed worker coverage when refactoring the wizard.
3. Track any future real execution path as a separate issue with Rust command
   tests before enabling it in PyQt.

### TF-STATUS-041: Legacy Auto-Fix Core Mutation APIs

Status: closed
Severity: High
Area: Rust Core baseline / Migration Auto-Fix Wizard

Evidence:

- GitHub #143 tracked the deeper follow-up after #142: the UI/worker path was
  dry-run/manual SQL only, but the underlying legacy Python core APIs still
  accepted `dry_run=False`.
- `BatchFixExecutor.execute_batch(..., dry_run=False)` could enter session
  state changes, SQL mode changes, FK check toggles, rollback capture, and
  `_execute_single(...)`.
- `FKSafeCharsetChanger.execute_safe_charset_change(..., dry_run=False)` could
  generate and execute FK DROP, ALTER, FK ADD, commit, rollback, and recovery
  SQL from Python-owned logic.
- RED/GREEN coverage in `tests/test_migration_fix_wizard.py` now proves both
  core APIs reject `dry_run=False` with a Rust Core ownership error, that
  `BatchFixExecutor` rejects mutation mode before session state or execution
  hooks are touched, and that `BatchFixExecutor._execute_single` is also
  fail-closed if called directly.
- Direct `cursor.execute`/`commit`/`rollback` mutation calls no longer appear in
  `src/core/migration_fix_wizard.py`.

Resolution:

- `BatchFixExecutor.execute_batch` raises `RuntimeError` immediately when
  `dry_run=False`.
- `FKSafeCharsetChanger.execute_safe_charset_change` raises `RuntimeError`
  immediately when `dry_run=False`.
- `BatchFixExecutor._execute_single` raises `RuntimeError` immediately if a
  direct caller tries to use the old private SQL execution hook.
- Dead legacy direct `cursor.execute`/`commit`/`rollback` bodies were removed
  from `src/core/migration_fix_wizard.py`.
- Dry-run/SQL generation remains available for preview and manual execution
  guidance.
- The older Python mutation-specific session/fallback tests were rewritten to
  assert fail-closed behavior rather than preserving an execution path that
  violates the Rust Core baseline.

Next action:

1. Keep these core APIs dry-run/SQL-generation only unless a future issue adds
   a Rust Core-owned command for the exact automatic-fix workflow.
2. If real automatic fix execution is needed later, implement it in
   `tunnelforge-core` first and add Rust command tests before exposing it in
   PyQt.

### TF-STATUS-042: Legacy MigrationAnalyzer Cleanup Mutations

Status: closed
Severity: High
Area: Rust Core baseline / Migration Analyzer cleanup

Evidence:

- GitHub #144 tracked this issue separately after #143: the legacy Auto-Fix
  core APIs were fail-closed, but `MigrationAnalyzer.execute_cleanup(...,
  dry_run=False)` still used Python-owned cursor execution.
- Before the fix, `src/core/migration_analyzer.py` opened
  `connector.connection.cursor()`, executed generated cleanup SQL, and called
  `commit()` / `rollback()` in non-dry-run cleanup mode.
- `src/ui/workers/migration_worker.py::CleanupWorker` can call
  `MigrationAnalyzer.execute_cleanup`, and
  `src/ui/dialogs/migration_dialogs.py` enabled the actual cleanup execution
  button when orphan rows existed.
- RED/GREEN coverage now proves `MigrationAnalyzer.execute_cleanup(...,
  dry_run=False)` raises a Rust Core ownership error before any cursor,
  commit, rollback, or connector query work is touched.
- UI coverage now proves the migration analyzer dialog keeps legacy actual
  cleanup execution disabled even when orphan rows exist.

Resolution:

- `MigrationAnalyzer.execute_cleanup` raises `RuntimeError` immediately when
  `dry_run=False`.
- The old direct cleanup `cursor.execute` / `commit` / `rollback` body was
  removed from `src/core/migration_analyzer.py`.
- The migration analyzer dialog describes cleanup as Dry-Run/SQL preview only,
  keeps `btn_execute` disabled, and guards direct `execute_cleanup(False)`
  calls with an explanatory warning.
- Dry-Run and SQL preview remain available.

Next action:

1. Keep legacy migration analyzer cleanup dry-run/SQL-preview only unless a
   future issue adds a Rust Core-owned cleanup command.
2. If real orphan cleanup execution is needed later, implement it in
   `tunnelforge-core` first and add Rust command tests before enabling the PyQt
   execution button.

### TF-STATUS-043: Legacy CleanupWorker Actual Cleanup Mode

Status: closed
Severity: Medium
Area: Rust Core baseline / Migration Analyzer cleanup worker

Evidence:

- GitHub #145 tracked the worker-level follow-up after #144: core cleanup
  mutation mode and the dialog were fail-closed, but `CleanupWorker` still
  accepted `dry_run=False`.
- Before the fix, a direct caller could construct `CleanupWorker(...,
  dry_run=False)`, start the thread, and receive `[실행]` progress text before
  the analyzer-level RuntimeError ended the worker.
- This did not re-enable DB mutation after #144, but it left the worker
  contract weaker and more misleading than the Rust Core fail-closed baseline.
- RED/GREEN coverage in `tests/test_migration_worker.py` now proves
  `CleanupWorker(..., dry_run=False)` rejects with a Rust Core ownership error
  at construction time.

Resolution:

- `CleanupWorker.__init__` raises `RuntimeError` immediately when
  `dry_run=False`.
- Dry-run cleanup worker construction remains available.

Next action:

1. Keep cleanup worker actual execution disabled unless a future issue adds a
   Rust Core-owned cleanup command and rewires this worker explicitly.

### TF-STATUS-044: Legacy MySQLConnector Execute Many Mutation Helper

Status: closed
Severity: Medium
Area: Rust Core baseline / DB connector helper API

Evidence:

- GitHub #146 tracked this unused connector-surface follow-up after #145.
- `src/core/db_connector.py::MySQLConnector.execute_many(...)` accepted
  arbitrary batch SQL/data, opened a cursor, called `executemany`, and
  committed from Python.
- `rg` found no repo callers of `execute_many`, so this was dormant API surface
  rather than an active feature workflow.
- RED/GREEN coverage in `tests/test_db_connector.py` now proves
  `MySQLConnector.execute_many` rejects with a Rust Core ownership error before
  cursor or commit work is touched.

Resolution:

- `MySQLConnector.execute_many` raises `RuntimeError` immediately.
- The dead direct `executemany` / `commit` body was removed from
  `src/core/db_connector.py`.
- Existing read/query helper behavior is unchanged.

Next action:

1. If batch mutation support is needed later, implement the specific workflow
   as a Rust Core command instead of reviving a generic Python `execute_many`
   helper.

## Issue Tracker

| ID | Severity | Status | Area | Short Title | Next Action |
| --- | --- | --- | --- | --- | --- |
| TF-STATUS-001 | High | closed | Export/Import Recovery | Initial import intent and strictness gates | Keep regression coverage aligned with import intent changes |
| TF-STATUS-002 | High | closed | Rust Core import | Import success gated by row verification | Keep row verification/report coverage aligned with import mode changes |
| TF-STATUS-003 | High | closed | Import UI | Object restoration wording | Keep focused regression |
| TF-STATUS-004 | High | closed | Rust Core export | Export consistency explicit | Keep metadata coverage aligned with export scheduling changes |
| TF-STATUS-005 | Medium | closed | Docs/UI flags | Disabled UI features labeled | Reverify docs if feature flags change |
| TF-STATUS-006 | Medium | watch | Maintainability | Remaining large files after Clean Code Round 3 | Keep future fixes narrow; split further only when nearby work justifies it |
| TF-STATUS-007 | Low | closed | Reporting | Referenced HTML report exists | Keep report aligned with future recovery changes |
| TF-STATUS-008 | Low | open | macOS | Current-HEAD workflow and final real-Mac validation pending | Run the manual workflow on the frozen release candidate, collect the real-Mac evidence bundle, and require the final gate to pass before production-ready claims |
| TF-STATUS-009 | High | closed | Rust Core import | Merge import post-load DDL policy | Keep merge/recreate policy tests |
| TF-STATUS-010 | High | closed | Rust Core import | Shadow replacement retired; direct replacement documented | Keep UI/docs aligned |
| TF-STATUS-011 | High | closed | Rust Core schema fidelity | MySQL FK charset/collation fidelity | Keep FK fidelity regression coverage |
| TF-STATUS-012 | Medium | closed | Import UI telemetry | Cumulative Import rows/s and ETA | Re-check wording with real long-running imports |
| TF-STATUS-013 | High | closed | Rust Core import | MySQL JSON fallback encoding | Watch for malformed-source JSON reports |
| TF-STATUS-014 | Medium | closed | SQL editor UI | Large SQL rendering guard | Revisit virtual rendering if measured bottleneck remains |
| TF-STATUS-015 | Medium | closed | SQL editor UI | Schema/table tree panel | Consider richer query composition later |
| TF-STATUS-016 | Medium | closed | Rust Core dump.import diagnostics | MySQL ERROR 1114 table-full guidance | Collect target storage/tmpdir evidence if it recurs |
| TF-STATUS-017 | High | closed | Rust Core migration performance evidence | 1M/10M evidence archived and validated | Refresh if migration/verify streaming semantics change |
| TF-STATUS-018 | High | closed | Rust Core live migration / UI evidence | Bidirectional 1M live UI evidence captured | Refresh final validator evidence if migration/RSS semantics change |
| TF-STATUS-019 | Medium | closed | One-Click migration UI | Dry-run preview One-Click entry point | Keep preview evidence aligned if event payloads change |
| TF-STATUS-020 | High | closed | One-Click migration UI / Rust Core automatic fixes | Real execution and automatic fix coverage | Track any additional automatic fix class as a separate issue |
| TF-STATUS-021 | High | closed | One-Click migration UI / Rust Core automatic fixes | Charset/collation automatic fix coverage | Keep validator/live evidence aligned if the charset contract changes |
| TF-STATUS-022 | High | closed | One-Click migration UI / Rust Core automatic fixes | Derive charset contracts for PyQt execution | Keep derivation evidence aligned if PyQt payload construction or Rust Core derivation changes |
| TF-STATUS-023 | Medium | closed | One-Click migration UI / Rust Core automatic fixes | Align `int_display_width` skip semantics | Keep display-only skip policy aligned if Rust Core begins emitting this class |
| TF-STATUS-024 | High | closed | Export/Import UI | Direct DB Rust Core endpoint host | Keep direct connector host coverage when worker construction changes |
| TF-STATUS-025 | High | closed | macOS release validation | Artifact lookup uses current main after PR merge | Keep artifact lookup default aligned with final report SHA policy |
| TF-STATUS-026 | Medium | closed | Docs/UI feature flags | Schedule guide hidden-feature wording | Rewrite as public guide only when schedule feature is re-enabled with evidence |
| TF-STATUS-027 | Medium | closed | One-Click migration docs | Limited production scope wording | Keep docs aligned if the real-execution allowlist expands |
| TF-STATUS-028 | Low | closed | Status documentation | Full Python suite count refresh | Refresh count when new tests are added and full pytest is rerun |
| TF-STATUS-029 | Low | closed | Status documentation | Baseline verification heading | Replace preservation note after a full broad baseline sweep is rerun |
| TF-STATUS-030 | Low | closed | Status documentation / Rust Core boundary audit | Current main next-issue re-audit | Keep #116 as the only open issue unless new repo-side evidence appears |
| TF-STATUS-031 | Low | closed | Status documentation | Baseline count refresh after re-audit coverage | Refresh counts when new tests are added and rerun |
| TF-STATUS-032 | Low | closed | Status documentation | Focused verification duplicate rows | Keep focused verification command rows unique |
| TF-STATUS-033 | Low | closed | Status documentation | Current baseline duplicate rows | Keep current baseline command rows unique |
| TF-STATUS-034 | Low | closed | Export/Import UI | Rust Core Export/Import menu wording | Keep Export/Import labels aligned with Rust Core ownership |
| TF-STATUS-035 | Low | closed | One-Click migration UI | One-Click fallback dry-run tooltip | Keep feature-flag fallback copy aligned with current support matrix |
| TF-STATUS-036 | Low | closed | One-Click migration UI | One-Click module scope docstring | Keep source comments aligned with the One-Click support matrix |
| TF-STATUS-037 | Low | closed | Build documentation | BUILD installer version examples | Keep build examples version-neutral or synced |
| TF-STATUS-038 | High | closed | macOS release validation | macOS manual workflow head policy | Keep final report/artifact/manual workflow SHA policies aligned |
| TF-STATUS-039 | Low | closed | Status documentation / Rust Core boundary audit | Post-merge next-issue external re-audit | Keep #116 external unless confirmed repo-side evidence appears |
| TF-STATUS-040 | High | closed | Rust Core baseline / Migration Auto-Fix Wizard | Legacy Python Auto-Fix Wizard mutations | Keep the legacy wizard dry-run/manual SQL only unless a future Rust Core-owned command is added |
| TF-STATUS-041 | High | closed | Rust Core baseline / Migration Auto-Fix Wizard | Legacy Auto-Fix core mutation APIs | Keep core legacy Auto-Fix APIs fail-closed for `dry_run=False` unless Rust Core owns the workflow |
| TF-STATUS-042 | High | closed | Rust Core baseline / Migration Analyzer cleanup | Legacy MigrationAnalyzer cleanup mutations | Keep cleanup actual execution disabled unless Rust Core owns the workflow |
| TF-STATUS-043 | Medium | closed | Rust Core baseline / Migration Analyzer cleanup worker | Legacy CleanupWorker actual cleanup mode | Keep cleanup worker actual execution disabled unless Rust Core owns the workflow |
| TF-STATUS-044 | Medium | closed | Rust Core baseline / DB connector helper API | Legacy MySQLConnector execute_many mutation helper | Keep generic Python batch mutation helper disabled unless Rust Core owns the workflow |
| TF-STATUS-045 | Low | closed | Status documentation / macOS release validation | Post-#146 next issue analysis | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-046 | Medium | closed | Release versioning | Post-release version drift | Keep source/package/installer versions ahead of the latest released tag before release tagging |
| TF-STATUS-047 | Medium | closed | Release publication | v2.1.7 release publication | Keep release tags/assets aligned when version bumps land directly on main |
| TF-STATUS-048 | Low | closed | Status documentation / macOS release validation | Post-#148 next issue analysis | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-049 | Medium | closed | Release versioning | Post-v2.1.7 version drift | Keep source/package/installer versions ahead of the latest released tag before release tagging |
| TF-STATUS-050 | Medium | closed | Rust Core baseline / DB connector shim | RustDbCursor executemany batch helper | Keep generic Python batch helpers disabled unless Rust Core owns the batch operation |
| TF-STATUS-051 | Low | closed | Status documentation | Stale current pytest count wording | Keep current-tense full-suite wording aligned with the latest full `pytest -q` evidence |
| TF-STATUS-052 | Low | closed | Status documentation / macOS release validation | Post-#151 main merge and next issue analysis | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-053 | Low | closed | Status documentation | Post-#151 full-suite evidence refresh | Keep current full-suite count aligned when current-status tests are added |
| TF-STATUS-054 | Medium | closed | Rust Core query execution / SQL reporting | Rust Core DML affected row counts | Preserve Rust Core affected-row metadata in Python cursor shims |
| TF-STATUS-055 | Medium | closed | Rust Core Python shim / SQL reporting | Call-local affected-row metadata | Do not store per-query rowcount metadata on shared facade state |
| TF-STATUS-056 | High | closed | SQL execution / SQL Editor / Scheduler | SQL statement parser mismatch | Share one robust parser for SQL file execution, SQL Editor execute-all/current-query, and scheduled SQL |
| TF-STATUS-057 | Low | closed | SQL parser helper | SQL dollar quote helper guard | Keep dollar quote marker detection fail-closed for invalid start offsets |
| TF-STATUS-058 | Low | closed | Status documentation / macOS release validation | Post-#156 main merge and next issue analysis | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-059 | Low | closed | One-Click readiness docs | One-Click readiness next-action wording | Keep completed One-Click readiness guidance framed as standing policy, not current next repo-side work |
| TF-STATUS-060 | Low | closed | SQL parser helper | SQL dollar quote helper None input | Keep dollar quote marker detection fail-closed for invalid and missing SQL text |
| TF-STATUS-061 | Low | closed | Status documentation | Current-status baseline provenance refresh | Keep current baseline provenance tied to the latest status update |
| TF-STATUS-062 | Medium | closed | Rust Core baseline / Export | Partial export FK parent resolution | Keep partial Export FK parent auto-inclusion owned by Rust Core schema inspection, not Python DB connectors |
| TF-STATUS-063 | High | closed | Rust Core Export/Import | PostgreSQL Rust dump endpoint engine | Keep PostgreSQL Export/Import endpoint engine preserved through `RustDumpConfig` into Rust Core dump commands |
| TF-STATUS-064 | High | closed | Rust Core Export/Import | PostgreSQL Import timezone SQL | Keep PostgreSQL dump import from using MySQL timezone detection or MySQL timezone correction SQL |
| TF-STATUS-065 | High | closed | Rust Core dump.import | PostgreSQL Import timezone Core validation | Keep Rust Core timezone validation aligned with MySQL and PostgreSQL import timezone SQL forms |
| TF-STATUS-066 | Medium | closed | Rust Core Export/Import helper API | PostgreSQL dump wrapper engine | Keep module-level dump helper wrappers engine-aware while preserving MySQL default compatibility |
| TF-STATUS-067 | Medium | closed | Hidden Scheduler / Rust Core dump backup | Scheduled PostgreSQL backup engine | Keep scheduled backup `RustDumpConfig` engine derivation aligned with scheduled SQL connector derivation |
| TF-STATUS-068 | Medium | closed | Hidden Scheduler / Rust Core dump backup | Scheduled backup tuple connection info | Keep scheduled backup connection normalization aligned with real `TunnelEngine.get_connection_info()` tuple output |
| TF-STATUS-069 | Low | closed | Status documentation / macOS release validation | Post-#166 next issue re-audit | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-070 | Low | closed | macOS release validation | Manual macOS workflow evidence refresh | Keep workflow_dispatch evidence refreshed before final real-Mac report finalization |
| TF-STATUS-071 | Low | closed | Status documentation / macOS release validation | Non-self-stale macOS workflow evidence policy | Keep exact current-head workflow run IDs/SHAs on #116 comments and final gate output, not as durable current-status summary evidence |
| TF-STATUS-072 | Low | closed | Status documentation / macOS release validation | Focused final-gate failure reason refresh | Keep current focused final-gate rows aligned with latest accepted current-head manual workflow evidence |
| TF-STATUS-073 | Low | closed | Status documentation / macOS release validation | Superseded missing manual workflow Summary cleanup | Keep Summary current-state paragraphs from presenting superseded missing manual workflow evidence as current |
| TF-STATUS-074 | Low | closed | Status documentation / repo-side re-audit | Post-#169 next issue re-audit | Keep #116 as the only open issue unless new repo-side evidence appears |
| TF-STATUS-075 | Low | closed | Status documentation / macOS release validation | macOS final validation tooling recheck | Keep #116 final validation tooling evidence fresh while external real-Mac report evidence remains pending |
| TF-STATUS-076 | Medium | closed | Security / Rust Core helper resolution | Frozen-runtime core helper lookup trusted boundary | Keep packaged runtime helper resolution limited to app-owned locations; allow PATH lookup only with explicit development opt-in |
| TF-STATUS-077 | Medium | closed | Security / auto-save paths | Schema-derived analysis and rollback filenames | Keep schema-derived filenames sanitized and resolved under their intended base directories |
| TF-STATUS-078 | Low | open | GitHub issue hygiene / Rust Core import | GitHub #170 remains open after merged ERROR 3780 fix | Confirm the PR #171 fix with the reporter and close #170 unless it reproduces on a containing release |
| TF-STATUS-079 | High | closed | Security / update integrity | Downloaded update package integrity verification | Keep GitHub Release asset `digest` verification fail-closed before every downloaded-package launch |
| TF-STATUS-080 | Medium | closed | Security / ProductionGuard | Unknown-environment dangerous-operation confirmation | Keep unknown-environment confirmation default-No for missing, unrecognized, and direct Import contexts |
| TF-STATUS-081 | High | closed | Release readiness / versioning | `2.3.1` release candidate version alignment | Keep `v2.3.1` immutable and retain the approved tag/build/publication evidence and asset digests |
| TF-STATUS-082 | Medium | closed | Product documentation / feature flags | Bilingual Schedule correction for disabled features | Keep both language surfaces explicit that Schedule remains disabled until intentional reactivation and verification |
| TF-STATUS-083 | Medium | closed | CI / branch protection | Full Python regression workflow | Keep the terminal version gate plus Rust Core, macOS tracking, and both macOS architecture checks required and strict on current main |
| TF-STATUS-084 | High | closed | Update final review / launch boundary | verification-to-launch lease, owned cleanup/no-clobber, cancellation generation, streaming bound | Retain the reviewed bounds, Fix E secure child creation/name validation, and bootstrapper cancel-before-entry evidence; local closure does not claim external Actions, branch protection, tag/release, GitHub closure, or Mac hardware evidence |
| TF-STATUS-085 | High | closed | Update cross-platform cleanup / documentation | POSIX residue policy, pre-dispatch abandonment, Fix Wizard capability wording | Retain the verified POSIX residue policy, pre-dispatch abandonment cleanup, and manual-SQL Fix Wizard wording |
| TF-STATUS-086 | High | closed | Bootstrapper cancellation / result publication | Confirmed cancellation before completed-path publication | Keep abandonment and result publication synchronized; discard late completed results and never schedule completion after confirmed cancellation |
| TF-STATUS-087 | Medium | closed | Non-Windows update UX / platform policy | Reveal-only action text claimed package open and app exit | Keep non-Windows action wording aligned with folder reveal-only behavior and no app exit |
| TF-STATUS-088 | High | closed | CI version gate / credentialed action trust | Commit-message bump bypass and mutable App token action tag | Require all three version files to match the trusted expected version and keep the credentialed action pinned by full commit SHA |
| TF-STATUS-089 | High | closed | Release approval / tag and publication trust | Automatic tag, automatic stable publication, mutable credentialed actions, optional macOS signing | Retain separate approved manual tag/draft workflows, exact tag/version/ancestry checks, pinned actions, protected Environment, and immutable release-tag updates/deletes |
| TF-STATUS-090 | High | watch | macOS release signing / notarization | Protected Environment intentionally has no paid Apple Developer credentials | Publish explicitly unsigned macOS artifacts for this release; revisit signing/notarization when an Apple Developer account is available |
| TF-STATUS-091 | Medium | watch | Release governance / independent approval | Only one write/admin collaborator exists | Keep single-maintainer approval enabled; revisit independent approval after adding a second trusted maintainer |
| TF-STATUS-092 | High | closed | Security / GitHub App credentials | Legacy issue-reporter App private key was embedded in public releases | Keep the exposed key and unused legacy repository secret absent; retain the replacement Reporter key, separate Releaser credentials, D1 global mutation caps, affirmative consent, strict allowlists, fail-closed edge free text, emergency off, and protected hosted gates |
| TF-STATUS-093 | High | closed | Release readiness / `2.4.0` publication | Anonymous error reporting is merged but not present in the latest stable `v2.3.1` release | Keep `v2.4.0` stable/latest with its exact-main annotated tag, 10 verified assets, explicit unsigned macOS notice, updater visibility, active relay health, and separate Releaser credential lane |
| TF-STATUS-094 | Low | closed | Product strategy / status documentation | Product-maturity team review and stale `v2.3.1` latest-release wording | Keep the HTML proposal, tracker, verification log, recommended order, and session log aligned with `v2.4.0` and the Safety and Proof decision |
| TF-STATUS-095 | Critical | closed | Security / SSH server identity | SSH preflight accepts unknown host keys and normal forwarding does not pin trusted server identity | Retain visible SHA-256 TOFU approval, one-time approval re-probe, persisted trust, pinned preflight/forwarding, changed-key rejection, and background fail-closed behavior |
| TF-STATUS-096 | High | closed | Import / timezone semantics | Import `Auto` can inject KST `+09:00` instead of preserving server/session defaults | Retain no-session-change Auto semantics and explicit engine-specific UTC/KST choices with MySQL/PostgreSQL regression coverage |
| TF-STATUS-097 | High | open | One-Click / execution approval | Canonical planning and the gated candidate executor are approved, but the Python approval facade/evidence, default-No UI, and final gates remain | Keep both production apply predicates false while completing and independently reviewing Phase B Tasks 5-7 |
| TF-STATUS-098 | High | closed | Rust Core client / process contract | Shared request lock can block indefinitely on an unbounded core response and mismatched IDs are discarded | Retain bounded deadlines, strict ID/protocol validation, negotiated generation barriers, typed indeterminate mutation outcomes, zero mutation resend, owned process/pipe settlement, and fail-closed consumer lifecycle regressions |
| TF-STATUS-099 | High | open | Cross-engine migration / resume contract | Resume identity omits endpoint details, state writes are non-atomic, and stale state rejection is under-specified | First add focused identity, plan-fingerprint, atomic-write, stale-state, and single-terminal-state reproducers; redesign only to satisfy those contracts |
| TF-STATUS-100 | Medium | open | Release engineering / Python dependencies | Python release inputs use compatible ranges without a hash-locked resolution | Generate and verify a release-only hash lock; reject altered artifacts while retaining compatible development ranges |
| TF-STATUS-101 | Medium | open | Migration analysis / error semantics | Analysis query failures collapse to empty findings and can appear as a clean result | Separate failed analysis from successful zero findings and expose a typed, user-visible failure state |

## Recommended Execution Order

1. Keep TF-STATUS-095 closed by retaining SSH unknown-host SHA-256 approval,
   persisted trust, changed-key rejection, pinned forwarding/preflight, and
   trust-before-credentials ordering on every interactive SSH path.
2. Keep TF-STATUS-096 closed by retaining Import Auto server/session-default
   preservation and emitting no implicit timezone-changing SQL.
3. Keep One-Click non-dry-run disabled for TF-STATUS-097 while completing its
   Python approval facade/evidence, default-No UI, and final gates; canonical
   Rust planning and the gated candidate executor are approved through
   `2115f3a`.
4. Keep TF-STATUS-098 closed by retaining deadline, mismatch, generation,
   process-reap, pipe-settlement, typed-indeterminate, zero-retry, and
   fail-closed consumer lifecycle coverage.
5. Re-enable One-Click non-dry-run for TF-STATUS-097 only if a later review
   independently proves both exact-plan and strong-fence predicates; this
   release keeps both predicates false even after Tasks 4-7 land.
6. Write TF-STATUS-099 reproducers for complete endpoint identity, plan
   fingerprint, atomic resume storage, stale-state rejection, and exactly one
   terminal cancellation/result state before changing resume architecture.
7. Complete TF-STATUS-100 with a reviewed release-only hash lock and CI
   verification of the locked artifacts.
8. Fix TF-STATUS-101 so analysis failure is never rendered as a successful
   zero-finding result.
9. After 1-8 pass focused and full Python/Rust/Worker gates, publish one Safety
   and Proof release through the existing free GitHub direct-download channel.
10. Package and observe 3-5 independent sessions through one disposable
   MySQL-over-SSH workflow before changing positioning, adding telemetry, or
   expanding onboarding and support tooling.
11. Keep TF-STATUS-085 closed by retaining the POSIX residue policy,
    pre-dispatch abandonment cleanup, and manual-SQL Fix Wizard wording.
12. Keep TF-STATUS-086 closed by retaining synchronized cancellation/result
    publication and late-result discard behavior.
13. Keep TF-STATUS-087 closed by preserving reveal-only/no-exit non-Windows
    update wording.
14. Keep TF-STATUS-088 closed by verifying all version files against trusted
    expected state and pinning credentialed actions by full commit SHA.
15. Keep TF-STATUS-089 closed by retaining the approved manual tag/draft split,
    protected Environment, strict branch checks, and immutable release-tag
    updates/deletes.
16. Keep TF-STATUS-092 closed: preserve the repository-only Reporter and
    separate Releaser lanes defined in
    `docs/superpowers/plans/2026-07-14-anonymous-error-reporting.md` and
    `docs/superpowers/specs/2026-07-14-anonymous-error-reporting-design.md`;
    never distribute the replacement private key in TunnelForge clients.
17. Keep TF-STATUS-093 closed by retaining the protected PR #247, exact-main
    annotated `v2.4.0` tag, approved draft build, asset digests, updater
    visibility, and post-release relay health evidence.
18. Keep TF-STATUS-090 on watch: unsigned macOS distribution is accepted; add
    signing/notarization only when paid Apple credentials become available.
19. Keep TF-STATUS-091 on watch under the accepted single-maintainer approval
    policy; revisit independent approval after adding another trusted maintainer.
20. Keep TF-STATUS-084 closed by retaining the verification-to-launch lease,
    owned cleanup/no-clobber, cancellation generation, and streaming bound.
21. Keep TF-STATUS-079 closed by retaining GitHub Release asset `digest`
    verification before every downloaded-package launch.
22. Keep TF-STATUS-080 closed by retaining unknown-environment confirmation for
    dangerous operations without classified tunnel metadata.
23. Keep TF-STATUS-083 closed by retaining strict required checks and the
    terminal gate that aggregates Python, Rust Core, macOS tracking, and both
    macOS architectures.
24. Keep TF-STATUS-082 closed by preserving the bilingual Schedule correction
    while the feature flag remains disabled.
25. Keep TF-STATUS-081 closed by retaining the protected `v2.3.1` tag, approved
    release evidence, and expected asset digest metadata.
25. Complete TF-STATUS-008 / GitHub #116 when external real-Mac report evidence
    becomes available. #116 remains external. Do not hard-code exact current-head
    workflow run IDs or SHAs; use #116 comments and the final gate for proof.
26. Resolve TF-STATUS-078: close #170 after confirming the merged fix from PR
    #171 / commit `a4c7a06`; reopen implementation only if it reproduces on a
    release containing the fix.

## Session Log

| Date | Session Summary | Files Touched | Verification |
| --- | --- | --- | --- |
| 2026-07-16 | Completed TF-STATUS-097 Phase B Task 4 after two independent-review fix loops. The gated candidate uses a strict hash-only approval parser, same-session UUID lock/replan/pre-SQL-post sequence, definite-exit release, and honest partial ordinals. DDL response loss is typed indeterminate across Rust and Python, including strict mutation-only metadata and zero retry. | Rust One-Click executor/protocol/live harness and dependency lock; Python DB Core outcome mapping/tests; Phase B plan/report/progress; canonical status | One-Click `61 passed`; full Rust `301 + 10 + 11 + 9 + 2 passed, 1 manual stress ignored`; strict DB Core process `238 passed`; warnings, release, compile, and diff gates passed; final independent verdict `Task 4 Review: APPROVE`. |
| 2026-07-16 | Completed TF-STATUS-097 Phase B Task 3 after fixing all seven independent-review findings. Canonical validation now reconstructs exact actions from snapshot/profile facts, FK-connected charset findings stay manual, deprecated-engine markers and schema/member boundaries fail closed, quoted identifier semicolons remain valid, and apply is unadvertised with both production gates false. | Rust One-Click planner/protocol, Phase B plan, Task report/progress, canonical status | Focused plan `21 passed`; independent One-Click `48 passed`; full Rust `285 + 10 + 11 + 9 + 2 passed, 1 manual stress ignored`; warning check, release build, docs `79 passed`, and diff check passed; independent TERRA verdict `Task 3 Review: APPROVE`. |
| 2026-07-16 | Closed TF-STATUS-098 after completing six DB Core process-contract tasks and repeated independent consumer lifecycle review loops. The final consumer boundary blocks nested-modal dismissal races, stale workflow continuations, malformed or duplicate terminal frames, wrong-command results, non-boolean success, synchronous GUI cleanup, and force-terminated Qt workers. | DB Core client/facade/wire, migration and dump consumers, focused process/UI tests, One-Click dependency checkpoint, canonical status | Strict targeted `638 passed`; full Python strict `3115 passed, 2 skipped`; Rust `296 passed, 1 ignored`; release build, Python 3.9 compile, diff and process gates passed; final independent verdicts Cleanup/Rust Wire/Consumers `APPROVE`. |
| 2026-07-16 | Closed the two strict TF-STATUS-098 Task 2 integration failures without weakening production assertions. Test finalization now excludes the snapshotted current process from independent owner-loop settlement, leaves production shutdown as its sole owner, settles only unresolved orphan transports, and rejects any tracked process lacking terminal PID and transport proof. Requested stdin close accepts only terminal broken/reset pipe completion. | DB Core client WIP, process contract/integration tests, detached-reap Task 2 plan, local Task 4 report, canonical status | Focused RED `3 failed, 2 passed`; focused GREEN `5 passed`; named real-child nodes `2 passed`; strict integration `16 passed`; contract `129 passed`; adjacent `255 passed`; Cargo release, compileall, and diff check passed; zero worktree Python processes after every command. |
| 2026-07-15 | Recorded the TF-STATUS-097 Phase A fail-closed gate without closing the issue. Rust blocks both public non-dry-run commands before DB work, Python/UI retain dry-run only, backup cannot reactivate after completion, all retired mutation captures fail before runtime imports or DB setup, and historical real-execution refresh paths are archived until TF-STATUS-098 and exact-plan approval are complete. | Rust One-Click/protocol/live harness, Python One-Click UI/worker/translations, readiness/capture docs, validators, and tests, canonical status regression | Rust One-Click `23 passed`; full Rust `231 passed, 1 ignored`; focused Phase A `156 passed`; full Python `2840 passed, 2 skipped`; direct capture/import-order, compile, and diff checks passed; final independent Spec/Quality verdicts `APPROVE`. |
| 2026-07-15 | Closed TF-STATUS-096 after making Import Auto timezone-neutral for MySQL and PostgreSQL. Auto no longer probes MySQL timezone tables or emits session SQL, the duplicate None option is removed, explicit UTC/KST behavior remains, and legacy `none` compatibility is covered for both engines. | Import dialog, legacy translations, dialog/exporter/i18n tests, canonical status regressions | Focused `120 passed`; expanded `171 passed`; full Python `2825 passed, 2 skipped`; Rust timezone validator passed; production scan and diff check passed; final independent verdict `APPROVE`. |
| 2026-07-15 | Closed TF-STATUS-095 after completing the SSH first-use UI and two review-fix loops. Unknown hosts now receive explicit default-Cancel SHA-256 approval, changed keys cannot be approved, every reviewed interactive path gates before credentials or tunnel creation, background paths remain noninteractive, and MainWindow result/lifecycle signals are correct. | Shared SSH approval dialog/translations, MainWindow, TunnelConfig, cross-engine endpoint, SQL Editor/Execution dialogs, connection worker, focused UI/Core/status tests | Final focused `218 passed`; wider `424 passed, 1 skipped`; full Python `2820 passed, 2 skipped, 2 warnings`; production compile and diff checks passed; independent Task 2 final verdict `APPROVE`. |
| 2026-07-15 | Completed the TF-STATUS-095 SSH trust Core slice after two independent security-review fix loops. The Core now persists only public host identity, binds approval to a one-time token and a fresh same-endpoint probe, rejects changed keys, pins forwarding/preflight, and disables implicit `~/.ssh/config` endpoint reinterpretation. The issue remains open because the first-use PyQt approval paths are Task 2. | SSH host trust store/path, tunnel engine, focused Core tests, Safety and Proof plans, canonical status regression | RED/GREEN evidence for forged/stale approvals, directory durability, and SSH config endpoint drift; final focused `71 passed, 1 skipped, 1 warning`; related `163 passed`; final independent Task 1 verdict `APPROVE`; diff check passed. |
| 2026-07-15 | Convened product, UX, architecture, quality/security/release, market, and operations/analytics roles, followed by a meeting chair and adversarial red-team review. The final proposal separates confirmed defects, contract-first investigations, and evidence-gated hypotheses. It prioritizes a Safety and Proof release before product repositioning, then 3-5 observed disposable MySQL-over-SSH sessions. | `docs/product_maturity_proposal_2026-07-15.html`, `docs/current_status.md`, `tests/test_product_maturity_proposal.py`, `tests/test_current_status_docs.py` | Focused documentation regression passed 75 tests. Edge desktop/mobile screenshots rendered successfully; CDP checks at 1440x1100 and 390x844 found no horizontal overflow and no broken images. Final diff check passed. |
| 2026-07-15 | Published `v2.4.0` as the stable/latest release and closed TF-STATUS-093 after the complete protected PR, exact-main tag, approved draft-build, asset inspection, updater, and production-health sequence. Release notes now explicitly identify the free macOS downloads as unsigned and unnotarized. | PR #247, tag `v2.4.0`, release workflow/metadata, canonical status and status regression contract | Runs `29390539762`, `29390540655`, and `29390539802` passed; merge commit `bfee81613c7f77d96136346fa305858bf62670d7`; tag run `29391247402` and release run `29391317995` passed; 10/10 assets had valid GitHub digests and four macOS sidecars matched; public latest and `UpdateChecker` returned `2.4.0`; relay health was HTTP 200/schema 1/`active`; only the two Releaser repository secrets remain. |
| 2026-07-15 | Prepared the `2.4.0` release candidate for anonymous error reporting and opened TF-STATUS-093. Hardened the local clean-build path and full-SHA-pinned the version/macOS workflow actions before release. Historical `v2.3.1` publication evidence remains immutable. | version sources, installer/bootstrapper build script and contract, CI workflows/tests, canonical status | Focused RED/GREEN; standalone full Python 2697/1 skipped/4 warnings; Rust and Worker gates passed; corrected `build-installer.ps1 -Clean` completed without source mutation; frozen main/WebSetup checks passed; diff check passed. Protected hosted gates, tag, draft inspection, and publication remain. |
| 2026-07-15 | Closed TF-STATUS-092 after protected PR #245 merged. The replacement anonymous error-reporting path, credential containment, client retirement, hosted gates, and protected integration are complete. | PR #245, `origin/main`, GitHub repository-secret inventory, production relay health, canonical status | Fresh-head runs `29385868513` and `29385868516` passed; PR #245 merged at `2026-07-15T03:22:13Z` as two-parent merge commit `6dbcd51c8c60acef3569697fa79a9e6914a7c0e0`; the legacy secret remains absent, only the separate Releaser secrets remain, and post-merge relay health is schema 1 / `active`. |
| 2026-07-15 | Removed the unused `GH_APP_PRIVATE_KEY` repository secret after the full derived-token containment interval. The replacement Reporter credential and independent Releaser credentials remain in their intended lanes. | GitHub repository secret inventory, frozen package smoke, canonical status | Containment completed at `2026-07-15T03:06:03Z`; secret deletion completed at `2026-07-15T03:06:23Z`; the remaining repository secret names are exactly `RELEASER_APP_ID` and `RELEASER_APP_PRIVATE_KEY`. `dist\TunnelForge\TunnelForge.exe --ui-smoke-check` returned `success=true`, found the bundled Rust Core, and completed its service hello. Fresh-head hosted checks and protected PR #245 remain. |
| 2026-07-15 | Deleted the exposed Reporter key after replacement canary approval and opened protected draft PR #245. The exact old fingerprint disappeared, the replacement Reporter fingerprint remained, and the independent Releaser lane was untouched. | GitHub App private-key inventory, remote feature branch, PR #245, canonical status | Deletion recorded at `2026-07-15T02:06:03Z`; commit `9367faa` pushed; runs `29382607405` and `29382607434` passed all Python, Rust, version, support-tracking, and macOS arm64/x86_64 gates. At this checkpoint, the one-hour token containment ended at `2026-07-15T03:06:03Z` and repository secret removal remained; the newer row records its completion. |
| 2026-07-15 | Completed the live Task 13 canary and active rollout. The replacement GitHub App key authenticated through Cloudflare, one designated issue and one cross-installation recurrence were created by the App and inspected without leaks, the canary issue was closed, emergency off passed, active was restored, and the exact public route was bound into the desktop client. A Workers `redirect:"error"` runtime failure was isolated and fixed with no-follow `manual` handling. | relay GitHub auth/issue tests, Wrangler active D1 config, production endpoint config/transport tests, Settings unconfigured fixture, sanitizer collision regression, canonical status | Canary #244 had one create/comment and correct App/labels; versions `615088ce-...` off and `9dbed64a-...` active passed; analytics showed 61 success/0 errors; focused client 371 passed; full Python 2697/1 skipped, Rust gate/Cargo test+release, Worker 316/typecheck/audit0/dry-run, PyInstaller, and Terra client review passed. Hostile 302 no-follow tests and focused Python 462 passed after review. Old-key and repository-secret deletion plus protected hosted gates remain. |
| 2026-07-15 | Advanced TF-STATUS-092 Tasks 12-13 through live GitHub/Cloudflare inventory and shadow rollout. The reporter App is repository-only and independent from Releaser; a new fingerprint-verified key overlaps the exposed key. A production D1 database and Worker were deployed through off and shadow while GitHub secrets, canary, active mode, and client binding remain pending. | GitHub App key/installation state, Cloudflare D1/Worker state, relay Wrangler D1 ID/mode, canonical status | App IDs `2735888` and `2927386` plus distinct public fingerprints proved no Releaser coupling. PKCS#8 validation and public-fingerprint comparison passed. D1 migration succeeded; Worker 314, typecheck, audit, dry-run, off/shadow smoke, exact health, and zero-row D1 checks passed. |
| 2026-07-15 | Published the reviewed local Tasks 1-11 branch without bypassing GitHub Push Protection. Secret-shaped synthetic Slack test literals were converted to runtime composition, then the unpublished development history was retained locally while a clean squash commit was pushed as `feat/anonymous-error-reporting-relay`. | sanitizer test fixtures, local branch refs, remote feature branch, canonical status | Focused sanitizer passed 270; raw secret-shaped literal scan returned no matches; initial protected push blocked as designed; clean commit `a9f3d08` pushed successfully and set the upstream branch. |
| 2026-07-15 | Completed the final local Tasks 1-11 review for TF-STATUS-092. Backup creation excludes reporting privacy state; manual restore preserves readable destination-local state; unreadable-current recovery strips old reporting authority. Config import strips source state but preserves destination terminal consent, prompt budget, claim, generation, and identity. Worker recurrence comments now ignore expired completed actions before cleanup. | ConfigManager backup/restore/import, consent/privacy tests, relay store/index and recurrence tests, final review evidence, canonical status | Focused Python passed 158 and focused Worker passed 75. Fresh controller verification passed full Python 2695 / 1 skipped / 4 existing warnings and Worker 314; typecheck, audit with 0 vulnerabilities, Wrangler dry-run, Python 3.9 compile, and diff checks passed. SOL/Terra whole-branch re-review approved local Tasks 1-11 with no remaining findings. |
| 2026-07-15 | Completed and independently approved TF-STATUS-092 Task 11. The relay now has hostile-input/log-containment and global-cap security coverage, a synthetic endpoint/mode-only smoke runner with URL guardrails and bounded process cleanup, secret/local-state ignores, and an exact credential-safe deployment runbook. Canary documentation distinguishes Worker token enforcement from operator fixture discipline, and multiline PKCS#8 entry is Dashboard-only. | relay README/example/smoke/security test, root ignore, error-reporting guide, packaging/runbook tests, SDD review evidence, canonical status | Initial relay/security/packaging verification passed 311/5/39. Review fixes added subprocess timeout and thread termination assertions, unsafe URL and remote-active health-only execution tests, corrected canary semantics, and Dashboard-only PEM entry. Final relay passed 311, packaging 47, security 5; typecheck, audit with 0 vulnerabilities, local D1/synthetic smoke, Wrangler dry-run, secret scan, diff check, and SOL/Terra approvals passed. |
| 2026-07-15 | Completed and independently approved TF-STATUS-092 Task 10. The relay now signs PKCS#8-only GitHub App JWTs, obtains repository-scoped Issues-write installation tokens, constructs fixed bounded issue content, and safely creates, comments, deduplicates, or recovers routes without accepting client Markdown. Every POST attempt has a current route-bound global budget; 401 retries once, stale duplicate routes fail closed or recover, and ambiguous results enter unknown quarantine. | GitHub auth/issue/format modules, relay/store/quota integration, focused Worker tests, SDD review evidence, canonical status | Initial implementation passed 300 tests. Review fixes added current-window retry budgets, exact ready-route duplicate resolution, same-installation create-to-closed/404 recovery, and pending/unknown race coverage. Final suite passed 306; typecheck, audit with 0 vulnerabilities, Wrangler dry-run, diff check, and SOL/Terra approvals passed. |
| 2026-07-14 | Completed and independently approved TF-STATUS-092 Task 9. The relay now has exact HTTPS mode routing, privacy-derived edge-only shadow limits, atomic D1 per-installation and global write budgets, route leases, cross-state create-generation uniqueness, stable unknown quarantine, and bounded scheduled cleanup. Cleanup cannot remove a complete/unknown create guard while its route remains pending, and no alternate unbounded cleanup API remains. | D1 migration, relay store/quota/mode handlers, Wrangler cron/config, focused Worker tests, SDD review evidence, canonical status | Three review-fix waves closed route-generation duplication, unbounded cleanup, and mismatched route/action batch ordering. Final relay suite passed 259; typecheck, audit with 0 vulnerabilities, Wrangler dry-run, local D1 migration/scheduled smoke, diff check, and SOL/Terra approvals passed. |
| 2026-07-14 | Completed and independently approved TF-STATUS-092 Task 8. The Cloudflare Worker package now enforces the strict report contract, streamed 16 KiB body limit, bounded duplicate-rejecting JSON parsing, exact integer semantics, detached canonical reconstruction, server-side fingerprint verification, and fixed non-echoing errors/counters. After repeated adversarial reviews, the edge free-text boundary was simplified to never read or forward the client message; validated structured error and environment evidence remains. | Worker package/lock/config, schema/parser, fingerprint, observability, fail-closed sanitizer, Workers-runtime tests, SDD review evidence, canonical status | Initial implementation passed 189 tests. Five security review waves closed credential, SQL/network/object, Unicode, parser, and numeric-token findings before replacing unprovable natural-language classification with the design's fail-closed omission rule. Final relay suite passed 198; typecheck, audit with 0 vulnerabilities, Wrangler dry-run, diff check, and SOL/Terra approvals passed. |
| 2026-07-14 | Completed and independently approved TF-STATUS-092 Task 7. The desktop no longer contains GitHub App authentication, direct issue creation, PEM/client-secret setup, PyJWT/python-dotenv runtime dependencies, or jwt/dotenv packaging imports. Client docs now describe only consent, the strict allowlist, local preview/health, and the credential-free relay. | retired core modules/tests, package/spec dependencies, environment/secrets examples, bilingual README wording, error-reporting guide, retirement guards, SDD review evidence, canonical status | RED began with four retirement failures. Focused implementation reached 755 passed; guard review added tracked-input, ignore, AST import, structural TOML/PEP 508, and canonical-name regressions. Final guard passed 31; full Python passed 2672 / 1 skipped / 4 existing warnings; Rust release, PyInstaller, Python 3.9 compile, diff check, and SOL/Terra approval passed. |
| 2026-07-14 | Completed and independently approved TF-STATUS-092 Task 6. Settings now offers explicit anonymous-reporting consent, a local-only write-free JSON preview, a retained nonblocking relay health check, and a last-attempt view restricted to fixed status, UTC timestamp, and canonical issue URL. Legacy GitHub App configuration language and controls are gone while the About repository link remains. | Settings dialog, error-report worker status persistence, i18n keys, focused tests, offscreen screenshot/probes, SDD review evidence, canonical status | Initial RED covered nine missing interfaces plus missing status persistence. Review fixes closed preview mutation, health startup retention, duplicate-click/deletion lifecycle, and full Settings construction. Final focused review reached 273 passed / 2 external warnings; full Python passed 2748 / 1 skipped / 4 existing warnings; Python 3.9 compile, diff check, and SOL/Terra approval passed. |
| 2026-07-14 | Completed and independently approved TF-STATUS-092 Task 5. Anonymous reports now require a linearized consent permit, use a credential-free HTTPS relay transport, preserve environment proxy/CA behavior without inherited origin state, and cannot alter Export/Import outcomes. Rust diagnostics remain local, useful, redacted, control-escaped, and bounded; configuration import/export cannot transfer reporting consent or identity. | relay transport/worker, consent authorization, sanitizer, ConfigManager privacy boundary, Export/Import dialogs and focused tests, SDD review evidence, canonical status | Multiple adversarial RED/GREEN waves closed consent, session-state, credential, escaped-key, DSN, response compression/size, diagnostic collision/retention, and config-import bounds. Final sanitizer suite passed 270; full Python passed 2729 / 1 skipped / 4 existing warnings; Python 3.9 compile and diff checks passed. Final targeted SOL/Terra reviews approved. |
| 2026-07-14 | Completed and independently approved TF-STATUS-092 Task 4. The primary instance now presents the consent prompt only when visible and idle, retries after modal or detached DB operations, restores claims after dialog failures or application shutdown, and exposes exact bilingual collected/excluded disclosures without network access. | consent dialog/lifecycle, primary startup wiring, detached-worker activity helpers, i18n and focused tests, SDD review evidence, canonical status | Initial focused suite passed 87. Review fixes reached 170, then shutdown and detached-worker race tests began RED at 18 failed / 2 passed. Final Task 4 suite passed 188 and exact shutdown/race selectors passed 9; Python 3.9 compile and diff checks passed. Independent Terra review approved with no findings. |
| 2026-07-14 | Completed and independently approved TF-STATUS-092 Task 3. Consent state now uses coherent atomic settings transactions, claims each automatic display before showing it, caps exposure at two even when an outcome is lost, binds outcomes to UUID claim tokens, ignores stale dialogs, and preserves or repairs the anonymous installation UUID in the same mutation. | ConfigManager transaction API, consent policy/tests, SDD review evidence, canonical status | Initial import RED and two concurrency review-fix loops. Final config/consent suite passed 93, builder interaction passed 31, Python 3.9 compile and diff checks passed. Corrected SOL/Terra review found no code findings; Task 4 explicitly owns primary-process claim-token UI integration. |
| 2026-07-14 | Completed and independently approved TF-STATUS-092 Task 2 after three privacy review-fix loops. The desktop now builds a schema-validated report from local allowlisted metadata, sanitizes recognized credentials, identities, paths, SQL, and DB objects fail-closed, extracts only application traceback frames, and fingerprints only stable non-message fields. | report sanitizer/environment/builder and focused tests; SDD review evidence; canonical status | Initial RED was two missing-module collection errors. Adversarial RED waves exposed credential, Unicode/control, escaped quote, host-role, path, SQL, DB object, and exception-introspection bypasses. Final Task 2 suite passed 171, schema passed 76, Python 3.9 compile and diff checks passed; final SOL/Terra review had no findings and approved Task 2. |
| 2026-07-14 | Completed and independently approved Task 1 for TF-STATUS-092. Python follows Draft 2020-12 mathematical-integer semantics for schema version, UTC offset, and frame line while returning canonical integers. Shared valid and invalid payloads execute through an independent JSON Schema validator; invalid fixtures carry exact expected paths; redaction fixtures verify their wrapper version and that every forbidden value is present in the source input. | report schema validator/tests, shared fixtures, dev dependency, Task 1 report, canonical status | Initial RED was the expected import failure. Review-fix RED was 6 failed / 66 passed; GREEN reached 72 and final boundary coverage reached 76 passed. Status suite 70 passed; SOL/Terra reviews ended with no Critical/Important findings and Task 1 approved. |
| 2026-07-14 | Converted the accepted TF-STATUS-092 architecture into a reviewed 13-task TDD execution plan and moved the issue to `in_progress`. Public-endpoint abuse is bounded by atomic D1 repository-write budgets and create leases; ambiguous GitHub timeouts are quarantined instead of retried. Implementation has not yet changed runtime code, credentials, or deployed services. | implementation plan, canonical status, status documentation contract test | Focused status test began RED; the complete status suite passed 70 tests and `git diff --check` passed. Independent relay review supplied the abuse, race, logging, and free-plan CPU gates incorporated into the plan. |
| 2026-07-14 | Accepted the TF-STATUS-092 replacement design: a dedicated repository-scoped reporter GitHub App, Cloudflare Worker relay, client/server allowlists, affirmative consent prompt shown at most twice with a 30-day defer, and an independent exposed-key containment lane. No key or deployed service changed in this design session. | anonymous error-reporting design, canonical status, status documentation contract test | Design self-review found no placeholders; storage was fixed to D1 HMAC idempotency, Cloudflare network processing was distinguished from application retention, and DB version collection was constrained to already-known metadata. Focused status test began RED; final status suite passed 69 tests and placeholder/diff scans passed. |
| 2026-07-13 | Opened TF-STATUS-092 after mapping the GitHub App private-key rotation blast radius. App ID `2735888` powered the legacy issue reporter and its key was embedded in the public release path through `v2.3.0`; current `v2.3.1` is outside that path. The repository's `RELEASER_APP_PRIVATE_KEY` remains a separate CI credential unless Developer Settings App ID and key-fingerprint evidence proves otherwise. No key was generated, replaced, deleted, or revoked in this session. | release history, release workflows, GitHub App authentication history, Actions logs/secrets inventory, `docs/current_status.md`, status documentation test | Confirmed 43 affected tags and 42 published releases; direct embedding evidence in the `v2.1.5` build log; no current embedded support or release secret read; exact secret-name inventory across 46 accessible account repositories. Status suite 68 passed and `git diff --check` passed. External consumers and installation scope still require fingerprint-based inventory before rotation. |
| 2026-07-13 | Published TunnelForge `v2.3.1` through the protected release path and closed TF-STATUS-081. PR #240 merged at `b80e15c6148ba19a357a84b4e9e6cee8ae0b4727`; the approved annotated tag, multi-platform release build, draft asset verification, direct-distribution security notice, and stable/latest publication all completed. | live PR/tag/Actions/Release state; final status docs/tests | Tag run `29233663954` and release run `29233708190` passed. Windows installer/WebSetup and unsigned macOS arm64/x86_64 DMG/ZIP assets passed hosted build/package/smoke checks. The published release contains all 10 expected assets with GitHub SHA-256 digests: `https://github.com/sanghyun-io/tunnelforge/releases/tag/v2.3.1`. |
| 2026-07-13 | Restored the established release behavior for maintainers without paid Apple credentials: build and smoke-test unsigned macOS arm64/x86_64 artifacts only when all Apple values are absent, while failing on every partial signing/notarization configuration. Recorded GitHub Releases direct distribution and no planned Apple App Store registration as durable project policy. The owner accepted unsigned macOS distribution and single-maintainer approval for `2.3.1`; TF-STATUS-090/091 moved from blockers to watch items. | release workflow, Apple credential classifier, workflow/credential/status/macOS documentation tests, `AGENTS.md`, support/status docs | TDD RED/GREEN; focused credential/workflow 23 passed; final full Python 2038 passed / 1 skipped / 4 warnings; Cargo baseline passed; security re-review SECURE; diff check passed. |
| 2026-07-13 | Closed TF-STATUS-083 and TF-STATUS-089 after replacement PR #240 hosted verification and live GitHub control re-reads. The PR is clean/mergeable; release remains blocked only by separately tracked Apple signing, independent-approval, and real-Mac evidence. | status docs/tests and live GitHub configuration evidence | Runs `29229463468`/`29229463485`: Python, Rust, terminal gate, and all macOS arm64/x86_64 checks passed; main protection, protected Environment, and immutable tag rules confirmed active. |
| 2026-07-13 | Remediated PR #240 hosted regressions at code baseline `7d49601`: isolated process-wide language state between tests and explicitly bundled lazy-loaded `src.ui` modules. | `tests/conftest.py`, `tunnel-manager.spec`, packaging regression test, status docs/tests | RED hosted Python/macOS runs and local reproductions; GREEN focused 79 + 14, full Python 2028 / 1 skipped / 4 warnings, PyInstaller build and frozen UI smoke success; replacement hosted run pending. |
| 2026-07-13 | Hardened the pre-release boundary at code baseline `c52f60e`: removed distributable GitHub App private-key embedding, made tag and draft release separate approved manual operations, pinned every action, and added exact tag/ancestor/version plus macOS signature/notarization checks. Applied the protected Environment, strict main checks, and immutable release-tag update/delete rules. | release/tag workflows, GitHub App auth and UI copy, setup docs, focused tests, status docs/tests, live GitHub configuration | Workflow 64 passed; GitHub auth/settings 114 passed; final standalone full Python 2028 passed / 1 skipped / 4 warnings; Rust regression/Cargo test/release build and diff check passed. Security re-review: SECURE / APPROVE. TF-STATUS-090/091 record missing Apple secrets and independent maintainer. |
| 2026-07-13 | Closed TF-STATUS-088 after the final CI security audit: removed commit-message-only bump completion, made trusted expected-version comparison cover all three version files, and pinned the releaser App token action to a full commit SHA. | `.github/workflows/version-gate.yml`, `tests/test_ci_workflows.py`, status docs/tests | CI workflow 9 passed; focused 319 passed / 1 skipped; standalone full Python 2031 passed / 1 skipped / 4 warnings; diff check passed. |
| 2026-07-13 | Closed the cancelled-DownloadError destroyed-root edge by applying the synchronized abandonment retirement rule to error UI scheduling as well as completion scheduling. | `bootstrapper/bootstrapper.py`, `tests/test_bootstrapper_integrity.py`, status docs/tests | Bootstrapper 74 passed; focused 318 passed / 1 skipped; standalone full Python 2030 passed / 1 skipped / 4 warnings; rebuilt frozen self-check and diff check passed. |
| 2026-07-13 | Closed the destroyed-root scheduling edge after confirmed cancellation: completion scheduling now silently retires only when Tk rejects the call and the synchronized state is already abandoned; non-cancellation Tk errors still propagate. | `bootstrapper/bootstrapper.py`, `tests/test_bootstrapper_integrity.py`, status docs/tests | Bootstrapper 73 passed; focused 317 passed / 1 skipped; standalone full Python 2029 passed / 1 skipped / 4 warnings; rebuilt frozen self-check and diff check passed. |
| 2026-07-13 | Removed the final lock-inversion risk by moving Tk scheduling outside the bootstrapper state lock while retaining abandoned-callback retirement, and extended reveal-only action strategy from macOS to every non-Windows platform. | `bootstrapper/bootstrapper.py`, `src/core/platform_integration.py`, focused bootstrapper/platform tests, status docs/tests | Focused 107 passed; standalone full Python 2028 passed / 1 skipped / 4 warnings; rebuilt frozen self-check and diff check passed. |
| 2026-07-13 | Addressed the final cross-review follow-ups: made path publication and completion scheduling one synchronized transition, retired already queued completion callbacks after confirmed cancellation, isolated delayed Windows lease destructors before monkeypatching, and aligned non-Windows update wording with reveal-only/no-exit behavior. Closed TF-STATUS-087. | `bootstrapper/bootstrapper.py`, `src/ui/dialogs/settings_update_helpers.py`, bootstrapper/settings/update tests, status docs/tests | Bootstrapper 72 passed; settings/bootstrapper 100 passed; focused 315 passed / 1 skipped; standalone full Python 2027 passed / 1 skipped / 4 warnings; rebuilt frozen self-check and diff check passed. |
| 2026-07-13 | Closed TF-STATUS-086 after a final security review reproduced confirmed cancellation racing completed-path publication. Added a deterministic threaded regression and synchronized bootstrapper abandonment/result publication so late results are discarded without completion dispatch. | `bootstrapper/bootstrapper.py`, `tests/test_bootstrapper_integrity.py`, `docs/current_status.md`, `tests/test_current_status_docs.py` | RED race test failed with zero discard calls; GREEN bootstrapper 71 passed; focused 313 passed / 1 skipped; standalone full Python 2025 passed / 1 skipped / 4 warnings; rebuilt frozen WebSetup self-check and diff check passed; final status suite 63 passed. |
| 2026-07-13 | Closed TF-STATUS-085 after fresh broad verification of cross-platform update cleanup, pre-dispatch abandonment, generic launch-failure retention, documentation accuracy, and the frozen WebSetup import boundary on code baseline `87d9021`. | `docs/current_status.md`, `tests/test_current_status_docs.py` | Focused Python 311 passed / 1 skipped; full Python 2023 passed / 1 skipped / 4 warnings; Rust gate and Cargo test/release build passed; frozen WebSetup build/self-check passed; version sync and diff check passed; final status suite 62 passed. |
| 2026-07-13 | Final Fix F2 aligned update cleanup/docs contracts: POSIX retains identity-unsafe cleanup residue by policy, Windows alone deletes identity-matched children, non-Windows auto-launch remains disabled, and Settings/bootstrapper discard completed undispatched packages idempotently. README Fix Wizard wording now states dry-run/manual SQL generation only. | `src/ui/dialogs/settings.py`, `bootstrapper/bootstrapper.py`, focused update/bootstrapper/settings tests, README/design/plan/status docs | `py_compile`; focused update/bootstrapper/settings/i18n/current-status suite: 253 passed / 1 skipped / 2 warnings in 2.79s; `git diff --check` passed. |
| 2026-07-13 | Refreshed the final release/security status against RC code baseline `e37f57adfd5053b6a5c8343d9ff7c36f8f4425bd`; TF-STATUS-084 remains closed with Fix E secure child creation/name validation and bootstrapper cancel-before-entry evidence. TF-STATUS-079 remains closed; TF-STATUS-080/082 remain closed, TF-STATUS-081/083 remain `fixed_pending_full_verify`, and TF-STATUS-008/078 remain open. Historical status-only rows, including the prior `b35dde6` baseline, remain historical. | `docs/current_status.md`, `tests/test_current_status_docs.py` | Focused security/status/version: 291 passed / 1 skipped in 46.01s; full Python: 2006 passed / 1 skipped / 4 warnings in 58.07s; Rust regression gate pass; Cargo 216 lib, 2 JSONL CLI, 9 live, 2 stress passed / 1 ignored; release build 2.82s; version sync 1 passed in 0.08s; diff check pass; fresh current-status tests and diff check appended above. |
| 2026-07-13 | Corrected TF-STATUS-084 baseline lineage wording: `b35dde6` is the verified RC code baseline, while status-only documentation lineage starts at `7810ea3` and does not alter that code baseline. The external boundary explicitly does not claim completion of live Actions, branch protection promotion, tag/release, GitHub issue closure, or Mac hardware validation. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED: focused TF-STATUS-084 wording regression failed because the baseline/lineage sentence was absent; GREEN focused current-status tests and diff check recorded in this session. Canonical final status suite: 60 passed in 0.21s; separate historical post-close run: 60 passed in 0.33s. |
| 2026-07-10 | Closed TF-STATUS-084 after fresh local final-review verification of the verification-to-launch lease, owned cleanup/no-clobber, cancellation generation, and streaming bound. Non-Windows automatic installer execution is disabled/reveal-only; no Mac hardware validation claim is made. TF-STATUS-079 remains closed with strengthened evidence; TF-080/082 remain closed, TF-081/083 remain `fixed_pending_full_verify`, and TF-008/078 remain open. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED: TF-STATUS-084 documentation regression failed as expected (1 failed in 0.24s), then a stale 1870-count assertion failed post-close (1 failed / 59 passed in 0.39s); GREEN status suite 60 passed in 0.21s (separate prior post-close run: 60 passed in 0.33s), focused suite 183 passed / 1 skipped in 1.63s, exactly one full pytest 1955 passed / 1 skipped / 4 warnings in 60.38s; Rust gate, Cargo test/build, version-sync, and diff check exit 0 |
| 2026-07-10 | Addressed Task 6 review feedback by restoring the Round 3 historical `1827 passed / 6 warnings` assertion, separating current `2.3.1` RC and Rust evidence into a dedicated regression, preserving both records, and removing the duplicate Session Log delimiter. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED: 2 failed / 58 passed in 0.44s; GREEN: 60 passed in 0.25s; final diff check recorded with the fix commit |
| 2026-07-10 | Finalized the `2.3.1` release candidate status handoff: GitHub Release asset `digest` verification, unknown-environment confirmation, `python-regression`, and the bilingual Schedule correction are reflected in the tracker. TF-STATUS-079/080/082 are closed with fresh focused and full evidence; TF-STATUS-081/083 remain `fixed_pending_full_verify`; TF-STATUS-008/078 remain open. | `src/version.py`, `pyproject.toml`, `installer/TunnelForge.iss`, `docs/current_status.md`, `tests/test_current_status_docs.py` | RED: 1 failed / 57 passed in 0.43s; GREEN: 58 passed in 0.26s; full pytest: 1870 passed / 4 warnings in 60.08s; Rust gate exit 0 in 1.4s; Cargo test exit 0 in 4.1s; release build exit 0 in 36.61s; version sync 1 passed in 0.08s; diff check exit 0 in 0.5s |
| 2026-07-10 | Convened architecture, product, UX, quality, security, and critical-program-review agents for two rounds of repository-grounded strategy review. Consensus prioritizes update integrity, dangerous-SQL defaults, release truth, public capability accuracy, required regression gates, and real-Mac evidence before new features or broad refactors. | `docs/current_status.md`, `tests/test_current_status_docs.py` | six independent reviews plus cross-critique; direct source and GitHub verification; current-status pytest 56 passed; full pytest 1827 passed / 6 warnings; expected macOS final-gate failure for two missing evidence conditions |
| 2026-07-10 | Reconciled Round 3 completion against current Git and GitHub state. Round 3 remains complete and synchronized; #170 is open only because the already merged/released ERROR 3780 fix was not linked for automatic closure. | `docs/current_status.md`, `tests/test_current_status_docs.py` | `git` ancestry/sync checks; GitHub open-issue, #170, and PR #171 inspection; release-tag containment check; current-status pytest 55 passed; full pytest 1826 passed / 6 warnings |
| 2026-07-09 | Integrated Clean Code Round 3 WP-3.1 through WP-3.8 into `main`, covering SQL editor, DB dialogs, migration dialogs, Fix Wizard pages, cross-engine/diff dialogs, settings/schedule/tunnel dialogs, main window controllers, and UI workers. | Round 3 UI/core helper files plus `docs/current_status.md` | Round 3 focused pytest 491 passed; full `pytest -q` 1819 passed / 4 warnings; Rust Core regression gate passed; whole-tree `MySQLConnector` allowlist scan passed; `git diff --check` passed |
| 2026-07-09 | Addressed Clean Code Round 3 red-review findings by restoring migration worker legacy constructor compatibility, cleanup worker `dry_run=False` RuntimeError behavior, and Fix Wizard dialog re-export compatibility. | `src/ui/workers/migration_worker.py`, `src/ui/dialogs/fix_wizard_dialog.py`, `tests/test_migration_worker.py`, `tests/test_fix_wizard_dialog.py`, `docs/current_status.md` | RED/GREEN compatibility tests; focused migration/Fix Wizard suite 118 passed; full `pytest -q` 1821 passed / 4 warnings |
| 2026-07-09 | Addressed SECURE/APPROVE follow-up findings by restoring `BatchOptionDialog` legacy re-export, limiting core helper lookup trust boundaries, hardening schema-derived auto-save paths, and stabilizing Windows Git Bash packaging tests. | `src/core/cross_engine_migration.py`, `src/core/path_safety.py`, `src/ui/dialogs/migration_result_store.py`, `src/ui/dialogs/fix_wizard_execution_page.py`, `src/ui/dialogs/fix_wizard_dialog.py`, `scripts/check-macos-support-gate.py`, `scripts/macos-download-validation-artifacts.sh`, tests, `docs/current_status.md` | Security regression tests passed; focused review/security suites passed; macOS packaging pytest 51 passed; full `pytest -q` 1824 passed / 4 warnings; Rust Core regression gate passed; allowlist scan passed; `git diff --check` passed |
| 2026-06-27 | Recorded TF-STATUS-075 after rechecking #116 final validation tooling: shell syntax is valid, focused macOS support tests pass, the normal #116 gate passes, and the final gate fails only for missing external real-Mac report evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: final validation tooling current-status pytest; final: macOS focused pytest, #116 gates, shell syntax |
| 2026-06-27 | Recorded TF-STATUS-074 after a post-#169 next-issue re-audit found no new repo-side issue: #116 is still the only open GitHub issue, Rust Core boundary scans still route through shims, stale-handoff scans found no new current task, and the remaining blocker is external real-Mac report evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#169 current-status pytest; final: current-status pytest, full pytest, #116 gates, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-073 / GitHub #169 by removing superseded missing-manual-workflow current-state wording from the Summary; older verification log rows remain historical, while the Summary now keeps the #116 current blocker to real-Mac report evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #169 | RED/GREEN: superseded Summary wording current-status pytest; final: `pytest -q`, #116 gates, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-072 / GitHub #168 by refreshing the current focused final-gate row so it no longer lists missing current-head manual workflow evidence after that evidence was refreshed on #116; the current final-gate blocker is real-Mac report evidence only. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #168 | RED/GREEN: focused final-gate reason current-status pytest |
| 2026-06-27 | Fixed TF-STATUS-071 / GitHub #167 by changing current-status macOS workflow evidence handoff to avoid self-stale exact current-head run IDs/SHAs in durable status summary text; #116 comments and the final gate remain authoritative for the latest current-head workflow proof. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #167 | RED/GREEN: non-self-stale macOS workflow policy current-status pytest |
| 2026-06-27 | Triggered and verified manual `macOS App Validation` workflow_dispatch run `28264164795` for GitHub #116; both arm64 and x86_64 jobs passed for the then-current main HEAD, leaving only real-Mac manual validation report evidence before final closure. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116, GitHub Actions run 28264164795 | RED/GREEN: manual workflow current-status pytest; final gate expected-failing for missing real-Mac report only |
| 2026-06-27 | Re-audited the next issue after #166 and confirmed `main` was aligned with `origin/main`; #116 is the only open GitHub issue, the normal repo-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#166 current-status pytest; final gate expected-failing for external evidence only |
| 2026-06-27 | Fixed TF-STATUS-068 / GitHub #166 by normalizing real tuple-shaped scheduled backup connection info and resolving credentials before building `RustDumpConfig`. | `src/core/scheduler.py`, `tests/test_scheduler.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #166 | RED/GREEN: tuple connection backup pytest and current-status pytest; final: full `pytest -q` at 1869 passed |
| 2026-06-27 | Fixed TF-STATUS-067 / GitHub #165 by passing the normalized tunnel `db_engine` into scheduled backup `RustDumpConfig`, matching the existing scheduled SQL Rust Core connector path. | `src/core/scheduler.py`, `tests/test_scheduler.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #165 | RED/GREEN: scheduled backup engine pytest and current-status pytest; final: full `pytest -q` at 1867 passed |
| 2026-06-27 | Fixed TF-STATUS-066 / GitHub #164 by adding optional `engine` parameters to the module-level `export_schema`, `export_tables`, and `import_dump` convenience wrappers so PostgreSQL helper callers preserve Rust Core endpoint engines. | `src/exporters/rust_dump_exporter.py`, `tests/test_rust_dump_exporter.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #164 | RED/GREEN: wrapper engine pytest and current-status pytest; final: full `pytest -q` at 1865 passed |
| 2026-06-27 | Fixed TF-STATUS-065 / GitHub #163 by allowing Rust Core `dump.import` timezone validation to accept PostgreSQL `SET TIME ZONE` while preserving the existing MySQL `SET SESSION time_zone` allowlist and injection rejection. | `migration_core/src/lib.rs`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #163 | RED/GREEN: Rust timezone validator pytest and current-status pytest; final: Rust core tests, full `pytest -q` at 1861 passed |
| 2026-06-27 | Fixed TF-STATUS-064 / GitHub #162 by skipping MySQL timezone auto-detection for PostgreSQL dump import and using PostgreSQL `SET TIME ZONE` syntax for forced timezone options. | `src/ui/dialogs/db_dialogs.py`, `src/core/i18n.py`, `tests/test_db_dialogs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #162 | RED/GREEN: PostgreSQL import timezone pytest and current-status pytest; i18n regression; final: full `pytest -q` at 1860 passed |
| 2026-06-27 | Fixed TF-STATUS-063 / GitHub #161 by preserving PostgreSQL engine through RustDumpConfig, Export/Import dialog worker config, preselected PostgreSQL tunnel connectors, and Rust Core dump endpoints. | `src/exporters/rust_dump_exporter.py`, `src/ui/dialogs/db_dialogs.py`, `src/core/db_connector.py`, `src/core/postgres_connector.py`, `tests/test_rust_dump_exporter.py`, `tests/test_db_dialogs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #161 | RED/GREEN: PostgreSQL dump endpoint engine pytest, dialog worker config pytest, current-status pytest; full-suite count superseded by TF-STATUS-064 |
| 2026-06-27 | Fixed TF-STATUS-062 / GitHub #160 by routing partial Export FK parent resolution through Rust Core `schema.inspect` instead of Python `MySQLConnector`. | `src/exporters/rust_dump_exporter.py`, `tests/test_rust_dump_exporter.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #160 | RED/GREEN: partial export FK parent Rust inspect pytest and current-status pytest; exporter suite; full-suite count superseded by TF-STATUS-063 |
| 2026-06-27 | Fixed TF-STATUS-061 / GitHub #159 by refreshing the current-status baseline provenance wording after TF-STATUS-060. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #159 | RED/GREEN: current-status baseline provenance pytest; full-suite count superseded by TF-STATUS-062 |
| 2026-06-27 | Fixed TF-STATUS-060 / GitHub #158 by making the SQL dollar quote helper fail closed for `None` SQL text. | `src/core/sql_statement_parser.py`, `tests/test_sql_execution_worker.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #158 | RED/GREEN: dollar quote None-input pytest and current-status pytest; parser suite; final: full `pytest -q` at 1849 passed |
| 2026-06-27 | Fixed TF-STATUS-059 / GitHub #157 by changing One-Click readiness follow-up wording from a current next repo-side change to standing policy/watch guidance. | `docs/oneclick_readiness.md`, `tests/test_oneclick_readiness_docs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #157 | RED/GREEN: One-Click readiness docs pytest and current-status pytest; full-suite count superseded by TF-STATUS-060 |
| 2026-06-27 | Re-analyzed the next issue after #156 and confirmed `main` was already aligned with `origin/main`. #116 is the only open GitHub issue; the normal repository-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#156 current-status pytest; final: #116 gate pass, expected-failing final gate; full-suite count superseded by TF-STATUS-060 |
| 2026-06-27 | Fixed TF-STATUS-057 / GitHub #156 by making the SQL dollar quote helper fail closed for empty SQL text and out-of-range starts. | `src/core/sql_statement_parser.py`, `tests/test_sql_execution_worker.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #156 | RED/GREEN: dollar quote helper bounds pytest; parser suite, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-056 / GitHub #155 by extracting the robust SQL statement parser to `src/core/sql_statement_parser.py` and routing SQL file execution, SQL Editor split/current-query, and scheduled SQL through it. | `src/core/sql_statement_parser.py`, `src/ui/workers/test_worker.py`, `src/ui/dialogs/sql_editor_dialog.py`, `src/core/scheduler.py`, `tests/test_sql_editor_dialog.py`, `tests/test_scheduler.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #155 | RED/GREEN: SQL Editor/Scheduler parser tests; final: SQL Editor/Scheduler/worker pytest and full `pytest -q` |
| 2026-06-27 | Created TF-STATUS-056 / GitHub #155 after confirming that SQL Editor and hidden scheduler statement splitters can over-split comments, PostgreSQL dollar quote bodies, and MySQL `DELIMITER` scripts while SQL file execution already handles those cases. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #155 | RED/GREEN: SQL parser mismatch current-status pytest; #116 gate pass, expected-failing final gate |
| 2026-06-27 | Created and fixed TF-STATUS-055 / GitHub #154 after finding that the #153 Python cursor shim used shared facade state for affected-row metadata. | `src/core/db_core_service.py`, `tests/test_db_core_service.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #154 | RED/GREEN: call-local rowcount metadata pytest |
| 2026-06-27 | Created and fixed TF-STATUS-054 / GitHub #153 after finding that Rust Core DML execution returned empty rows without affected-row metadata, causing Python cursor shims to report `rowcount=0` for successful DML. | `migration_core/src/lib.rs`, `src/core/db_core_service.py`, `tests/test_db_core_service.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #153 | RED/GREEN: Rust/Python affected-row tests; focused scheduler/SQL worker tests |
| 2026-06-27 | Created and fixed TF-STATUS-053 / GitHub #152 after the post-#151 status coverage increased the full Python suite; the count is now superseded by TF-STATUS-054 at `1837 passed, 5 warnings`. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #152 | RED/GREEN: full-suite count current-status pytest; final: `pytest -q` |
| 2026-06-27 | Re-analyzed the next issue after #151 and confirmed `main` was aligned with `origin/main` before this status update, then pushed this status update to `origin/main`. #116 is the only open GitHub issue; the normal repository-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#151 current-status pytest; final: #116 gate pass, expected-failing final gate |
| 2026-06-27 | Created and fixed TF-STATUS-051 / GitHub #151 after finding stale current-tense `1830 passed` wording left behind after the #150 full-suite run. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #151 | RED/GREEN: stale current-count current-status pytest |
| 2026-06-27 | Created and fixed TF-STATUS-050 / GitHub #150 after finding the unused `RustDbCursor.executemany` Python-side batch helper. | `src/core/db_core_service.py`, `tests/test_db_core_service.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #150 | RED/GREEN: RustDbCursor executemany pytest and current-status pytest; full pytest count superseded by TF-STATUS-053 |
| 2026-06-27 | Created and fixed TF-STATUS-049 / GitHub #149 after finding that `main` had post-`v2.1.7` commits while source/package/installer references still declared `2.1.7`. | `src/version.py`, `pyproject.toml`, `installer/TunnelForge.iss`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #149 | RED/GREEN: post-v2.1.7 version drift current-status pytest; version sync pytest; full pytest count superseded by TF-STATUS-050/051 |
| 2026-06-27 | Re-analyzed the next issue after #148 closure. #116 is the only open GitHub issue; the normal repository-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#148 current-status pytest; final: #116 gate pass, expected-failing final gate |
| 2026-06-27 | Created and fixed TF-STATUS-047 / GitHub #148 after direct `main` version bumping left release publication behind; pushed tag `v2.1.7`, verified Build and Release workflow run `28255274238`, and confirmed the GitHub release assets. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #148, release `v2.1.7` | RED/GREEN: v2.1.7 release-publication current-status pytest; final: release workflow success and `gh release view v2.1.7` |
| 2026-06-27 | Created and fixed TF-STATUS-046 / GitHub #147 after finding that `main` still declared `2.1.6` even though release/tag `v2.1.6` already exists and post-release commits have accumulated. | `src/version.py`, `pyproject.toml`, `installer/TunnelForge.iss`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #147 | RED/GREEN: post-release version drift current-status pytest; version sync pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
| 2026-06-27 | Re-analyzed the next issue after #146. #116 is still the only open GitHub issue; the normal repo-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#146 current-status pytest; final: #116 gate pass, expected-failing final gate, current-status pytest |
| 2026-06-27 | Created and fixed TF-STATUS-044 / GitHub #146 after finding the unused `MySQLConnector.execute_many` public helper still exposed a Python-owned batch mutation/commit API. | `src/core/db_connector.py`, `tests/test_db_connector.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #146 | RED/GREEN: connector helper pytest and current-status pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
| 2026-06-27 | Created and fixed TF-STATUS-043 / GitHub #145 after finding that `CleanupWorker(..., dry_run=False)` still accepted legacy actual cleanup mode after #144 fail-closed the analyzer and dialog paths. | `src/ui/workers/migration_worker.py`, `tests/test_migration_worker.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #145 | RED/GREEN: cleanup worker pytest and current-status pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
| 2026-06-27 | Created and fixed TF-STATUS-042 / GitHub #144 after finding that `MigrationAnalyzer.execute_cleanup(..., dry_run=False)` still provided a Python-owned cleanup mutation path and the dialog could expose actual cleanup execution. | `src/core/migration_analyzer.py`, `src/ui/dialogs/migration_dialogs.py`, `src/core/i18n.py`, `tests/test_migration_analyzer.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #144 | RED/GREEN: cleanup mutation pytest, migration analyzer dialog pytest, i18n pytest, and current-status pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
| 2026-06-26 | Created canonical status inventory after full repo survey. | `docs/current_status.md` | `pytest -q`; `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `compileall`; `git diff --check`; `service.hello` |
| 2026-06-26 | Added Python import payload forwarding for `timezone_sql` and `strict_manifest`; removed import UI `모든 객체` overpromise; added focused regression tests. | `src/exporters/rust_dump_exporter.py`, `src/ui/dialogs/db_dialogs.py`, `tests/test_rust_dump_exporter.py`, `tests/test_db_dialogs.py`, `docs/current_status.md` | `python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `git diff --check` |
| 2026-06-26 | Added Rust validation/application for dump import `timezone_sql`; arbitrary SQL and multi-statement payloads are rejected before DB connection. | `migration_core/src/lib.rs`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_session_time_zone_only --lib`; `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` |
| 2026-06-26 | Added strict manifest classification before dump import target mutation and preserved classified core errors through Python import messages. | `migration_core/src/lib.rs`, `tests/test_rust_dump_exporter.py`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Added dump import row-count success gate and `_tunnelforge_import_report.json` success artifact. | `migration_core/src/lib.rs`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `cargo test --manifest-path migration_core\Cargo.toml write_dump_import_report_creates_json_file --lib` |
| 2026-06-26 | Added dump manifest consistency metadata for strict and non-strict export paths. | `migration_core/src/lib.rs`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Added merge import post-load DDL skip policy, fixed English translation for import UI wording, and created the final remediation report. | `migration_core/src/lib.rs`, `src/core/i18n.py`, `reports/export_import_flow_review_20260601.html`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `.venv\Scripts\python -m pytest -q`; `compileall`; `git diff --check` |
| 2026-06-26 | Marked scheduled backup documentation as disabled/internal while the main UI feature flag remains off. | `SCHEDULE.md`, `docs/current_status.md` | `rg -n "SCHEDULE_FEATURE_ENABLED|SQL_FILE_EXECUTION_FEATURE_ENABLED|스케줄" src docs SCHEDULE.md` |
| 2026-06-26 | Re-audited recovery design residuals after user challenge; added explicit open tracking for shadow replacement and MySQL schema fidelity gaps. | `docs/current_status.md` | `rg -n "shadow|ERROR 3780|charset|collation" docs/superpowers/specs/2026-06-01-export-import-recovery-design.md docs/superpowers/plans/2026-06-01-export-import-recovery.md reports/export_import_flow_review_20260601.html migration_core/src/lib.rs` |
| 2026-06-26 | Created GitHub issues for remaining recovery gaps. | `docs/current_status.md` | `gh issue create` created #133 and #134 |
| 2026-06-26 | Added MySQL FK charset/collation fidelity capture and post-load validation for GitHub #134. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py -q -k "classified_core_error"`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Explicitly retired shadow full replacement as a current guarantee and documented direct replacement as the supported import architecture for GitHub #133. | `docs/superpowers/specs/2026-06-01-export-import-recovery-design.md`, `docs/superpowers/plans/2026-06-01-export-import-recovery.md`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | `rg -n "shadow|direct replacement|atomic" docs/superpowers/specs/2026-06-01-export-import-recovery-design.md docs/superpowers/plans/2026-06-01-export-import-recovery.md docs/current_status.md reports/export_import_flow_review_20260601.html`; `git diff --check` |
| 2026-06-26 | Closed resolved import issues #120 and #123; added classified table/operation context for direct replace DDL failures supporting #119. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | `cargo test --manifest-path migration_core\Cargo.toml`; focused RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml dump_import_ddl_error_includes_classification_table_and_operation --lib` |
| 2026-06-26 | Fixed post-load DDL ordering so all secondary/unique indexes are applied before any foreign keys, addressing GitHub #127. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml post_data_ddl_applies_all_indexes_before_any_foreign_keys --lib`; `cargo test --manifest-path migration_core\Cargo.toml` |
| 2026-06-26 | Added final target row-count verification for direct replace/recreate imports, addressing GitHub #131 while preserving merge semantics. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml import_target_row_verification --lib`; `cargo test --manifest-path migration_core\Cargo.toml` |
| 2026-06-26 | Classified post-load DDL execution failures with the failing SQL statement for diagnosis of errors such as GitHub #126. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml post_load_ddl_errors_include_classification_and_sql_context --lib`; `cargo test --manifest-path migration_core\Cargo.toml` |
| 2026-06-26 | Added cumulative Import telemetry for GitHub #128: Rust row events now carry table-local and manifest-wide row counts, Python forwards them, and the UI separates average speed, current speed, ETA, and post-load phase text. | `migration_core/src/lib.rs`, `src/exporters/rust_dump_exporter.py`, `src/ui/dialogs/db_dialogs.py`, `tests/test_db_dialogs.py`, `tests/test_rust_dump_exporter.py`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `pytest tests/test_db_dialogs.py::test_format_import_row_labels_reports_cumulative_average_current_and_eta tests/test_db_dialogs.py::test_format_import_row_labels_stops_row_eta_during_post_load_phase tests/test_rust_dump_exporter.py::TestRustDumpImporter::test_import_row_progress_forwards_cumulative_totals_to_detail_callback`; `cargo test --manifest-path migration_core\Cargo.toml dump_import_row_progress_event_reports_table_and_overall_rows`; final: `pytest tests/test_db_dialogs.py tests/test_rust_dump_exporter.py`; `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `pytest -q`; `python -m compileall -q main.py src tests`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Hardened MySQL JSON fallback INSERT handling for GitHub #118 by using `_utf8mb4` JSON literals and removing `NO_BACKSLASH_ESCAPES` during import session tuning. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml mysql_json_literal_uses_utf8mb4_introducer_for_unicode_json_text --lib`; `cargo test --manifest-path migration_core\Cargo.toml mysql_dump_import_uses_fast_session_tuning_statements --lib`; final: `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `pytest -q`; `python -m compileall -q main.py src tests`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Added a large-document guard for GitHub #86 so SQL files at or above 512KB open with syntax highlighting and real-time validation disabled, then restore normal editor features for smaller content. | `src/ui/dialogs/sql_editor_dialog.py`, `src/core/i18n.py`, `tests/test_sql_editor_dialog.py`, `docs/current_status.md` | RED/GREEN: `pytest tests/test_sql_editor_dialog.py::test_large_sql_file_disables_expensive_editor_features tests/test_sql_editor_dialog.py::test_small_content_reenables_editor_features_after_large_file`; final: `pytest tests/test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests/test_sql_editor_dialog.py`; `pytest -q`; `python -m compileall -q main.py src tests`; `git diff --check` |
| 2026-06-26 | Added the SQL editor schema/table tree panel for GitHub #92 with schema roots, loaded table/column children, and table-click insertion into the current editor. | `src/ui/dialogs/sql_editor_dialog.py`, `tests/test_sql_editor_dialog.py`, `docs/current_status.md` | RED/GREEN: `pytest tests/test_sql_editor_dialog.py::test_metadata_loaded_populates_schema_tree tests/test_sql_editor_dialog.py::test_schema_tree_table_click_inserts_quoted_table_name`; final: `pytest tests/test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests/test_sql_editor_dialog.py`; `pytest -q`; `python -m compileall -q main.py src tests`; `git diff --check` |
| 2026-06-26 | Analyzed GitHub #126 and added MySQL `ERROR 1114` storage/tmpdir guidance to post-load DDL import failures. | `migration_core/src/lib.rs`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml post_load_ddl_mysql_table_full_error_includes_storage_guidance --lib`; focused regression: `cargo test --manifest-path migration_core\Cargo.toml post_load_ddl --lib`; final: `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Re-audited GitHub #116 after #126 closure: PR #117 is merged and local codebase gates pass, but #116 remains open only for final real operator Mac evidence. | `docs/current_status.md` | `gh pr view 117 --repo sanghyun-io/tunnelforge`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` |
| 2026-06-26 | Analyzed GitHub #99 and created #135 for the remaining Rust Core 1M/10M performance evidence durability gap. | `docs/current_status.md` | `RUST_CORE_REQUIRE_PERF_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `git status --ignored --short migration_core\target\perf_*.jsonl`; `gh issue create` |
| 2026-06-26 | Archived Rust Core 1M/10M performance evidence under `reports\rust_core_performance`, added a validator, and wired the optional performance regression gate to the archived evidence for GitHub #135/#99. | `reports/rust_core_performance`, `scripts/validate-rust-core-performance-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_rust_core_performance_evidence.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_rust_core_performance_evidence.py -q`; final: `python scripts\validate-rust-core-performance-evidence.py`; `RUST_CORE_REQUIRE_PERF_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Audited GitHub #99 closure criteria after #135 and created #136 for the remaining live bidirectional 1M UI responsiveness evidence. | `migration_core/src/lib.rs`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; focused Python Rust Core/UI plumbing tests; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `RUST_CORE_REQUIRE_PERF_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `rg` direct DB driver scan |
| 2026-06-26 | Added a machine-checkable #136 live UI migration evidence validator and JSON template so future real 1M bidirectional runs can be accepted or rejected consistently. | `scripts/validate-live-ui-migration-evidence.py`, `tests/test_live_ui_migration_evidence.py`, `reports/live_ui_migration`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_live_ui_migration_evidence.py -q`; final: `python -m compileall -q scripts tests`; `git diff --check` |
| 2026-06-26 | Analyzed GitHub #136 after merging prior work to main; confirmed local MySQL/PostgreSQL live endpoint wiring passes small Rust Core roundtrip tests, but #136 still requires durable 1M bidirectional PyQt heartbeat evidence. | `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml --test live_roundtrip -- --nocapture` |
| 2026-06-26 | Added the #136 live UI evidence capture helper with deterministic local-container seeding, CrossEngineMigrationWorker execution, Qt heartbeat sampling, and validator-compatible report generation; verified the path with a 1,000-row smoke that must not be used as final evidence. | `scripts/capture-live-ui-migration-evidence.py`, `tests/test_live_ui_migration_capture.py`, `reports/live_ui_migration/README.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_live_ui_migration_capture.py -q`; smoke: `python scripts\capture-live-ui-migration-evidence.py --rows 1000 ...`; expected reject: `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence-smoke.json` |
| 2026-06-26 | Captured and preserved partial #136 evidence for the live 1M bidirectional PyQt worker path; both directions passed migrate+verify with heartbeat max gap 125ms, leaving only real 10M RSS evidence before final validator closure. | `reports/live_ui_migration/live-ui-migration-evidence-1m-local-partial.json`, `reports/live_ui_migration/README.md`, `docs/current_status.md` | `python scripts\capture-live-ui-migration-evidence.py --rows 1000000 ...`; expected reject: `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence-1m-local-partial.json` |
| 2026-06-26 | Added and ran the Rust Core 10M synthetic stress RSS harness, generated the final #136 evidence file, and closed TF-STATUS-018 after the final validator passed. | `migration_core/tests/stress_rss.rs`, `reports/live_ui_migration/stress-10m-rss.json`, `reports/live_ui_migration/live-ui-migration-evidence.json`, `reports/live_ui_migration/README.md`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml --test stress_rss synthetic_stress_run_reports_resume_verify_and_rss_bound -- --nocapture`; ignored 10M: `cargo test --manifest-path migration_core\Cargo.toml --test stress_rss synthetic_10m_stress_resume_verify_reports_rss_bound -- --ignored --nocapture`; final: `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json` |
| 2026-06-26 | Re-audited the last open issue #116 after #99/#136 closure; local macOS support gates still pass, but the issue remains open for external real operator Mac evidence. | `docs/current_status.md` | `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python -m compileall -q scripts tests` |
| 2026-06-26 | Cleaned up stale wording after closing #99/#136 so the evidence READMEs and status heading describe completed evidence instead of pending closure gates. | `docs/current_status.md`, `reports/live_ui_migration/README.md`, `reports/rust_core_performance/README.md` | `rg -n "remaining #99|GitHub issue #136 now tracks|Live UI Performance Evidence Pending|should remain open until the live|#99 remains open|#136 still remains open" docs reports scripts tests README.md README.ko.md`; `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json`; `python scripts\validate-rust-core-performance-evidence.py`; `git diff --check` |
| 2026-06-26 | Wired final live UI evidence into the optional Rust Core regression gate so clean checkouts can require both archived Rust performance evidence and live UI evidence. | `scripts/rust-core-regression-gate.ps1`, `tests/test_live_ui_migration_evidence.py`, `reports/live_ui_migration/README.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_live_ui_migration_evidence.py::test_regression_gate_can_require_live_ui_evidence -q`; `RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Reconfirmed current `main` was aligned with `origin/main`, ran a broader current-main verification sweep, and re-analyzed GitHub #116 as the then-only remaining open issue before the later One-Click tracker was created. | `docs/current_status.md` | `pytest -q`; `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python -m compileall -q main.py src tests scripts`; `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json`; `python scripts\validate-rust-core-performance-evidence.py`; `RUST_CORE_REQUIRE_PERF_EVIDENCE=1; RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `git diff --check` |
| 2026-06-26 | Audited stale plan/TODO candidates after #116 was confirmed external; found the One-Click Rust Core command surface exists while the PyQt entry point remains hidden, created GitHub #137, and added TF-STATUS-019 so the production-readiness gate is tracked separately from closed #124. | `docs/current_status.md` | `rg -n "oneclick\.|ONE_CLICK_MIGRATION_FEATURE_ENABLED" migration_core\src\lib.rs src tests docs README.md README.ko.md`; `tunnelforge-core service.hello`; `gh issue view 124`; `gh issue create` created #137 |
| 2026-06-26 | Hardened the hidden One-Click path for #137 so real execution is blocked until the readiness gate opens and the hidden dialog cannot uncheck Dry-run. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_rejects_real_execution_until_readiness_gate_opens -q`; RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_locks_dry_run_until_readiness_gate_opens -q`; final: `pytest tests\test_oneclick_rust_core_gate.py tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `pytest tests\test_oneclick_rust_core_gate.py tests\test_db_core_service.py -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py`; `git diff --check` |
| 2026-06-26 | Added One-Click dry-run evidence capture/validation tooling, archived local MySQL Rust Core `oneclick.run` dry-run evidence, documented the current hidden dry-run-only scope, and wired the optional regression gate to that evidence. | `scripts/validate-oneclick-dry-run-evidence.py`, `scripts/capture-oneclick-dry-run-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_oneclick_dry_run_evidence.py`, `reports/oneclick_readiness`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_dry_run_evidence.py -q`; capture: `python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json`; final: `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Analyzed the next #137 decision after merging One-Click evidence: the then-current Rust Core behavior supported hidden or dry-run-only preview scope, but not full enablement because automatic fix coverage was not implemented. | `docs/oneclick_readiness.md`, `docs/current_status.md` | `rg -n "ONE_CLICK_MIGRATION_FEATURE_ENABLED|ONECLICK_REAL_EXECUTION_ENABLED|oneclick|OneClick" src migration_core tests docs README.md README.ko.md`; `gh issue view 137`; Rust Core `oneclick_*` function inspection |
| 2026-06-26 | Exposed #137 as a dry-run-only preview: the migration analyzer shows `One-Click Dry-run Preview`, real execution remains blocked, and refreshed evidence now requires preview UI enabled plus real execution disabled. | `src/ui/dialogs/migration_dialogs.py`, `src/core/i18n.py`, `scripts/validate-oneclick-dry-run-evidence.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_oneclick_dry_run_evidence.py`, `reports/oneclick_readiness`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_exposes_oneclick_as_dry_run_preview_only -q`; RED/GREEN: `pytest tests\test_oneclick_dry_run_evidence.py::test_oneclick_dry_run_evidence_accepts_complete_report tests\test_oneclick_dry_run_evidence.py::test_oneclick_dry_run_evidence_requires_preview_ui_enabled -q`; i18n: `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; capture: `python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json`; validator: `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json` |
| 2026-06-26 | Split the remaining One-Click real-execution work into GitHub #138, marked TF-STATUS-019 as the closed dry-run preview gate, opened TF-STATUS-020 for automatic fix coverage, and updated the real-execution lock copy to point at #138. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `docs/current_status.md`, `docs/oneclick_readiness.md` | `gh issue create` created #138; `gh issue view 137`; `gh issue view 138`; RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_rejects_real_execution_until_readiness_gate_opens tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_locks_dry_run_until_readiness_gate_opens -q`; `rg -n "TF-STATUS-019|TF-STATUS-020|#138|ONECLICK_REAL_EXECUTION_ENABLED" docs src tests migration_core` |
| 2026-06-26 | Started GitHub #138 automatic-fix coverage by adding typed Rust Core recommendation metadata: `deprecated_engine` with `table_name` becomes an `engine_innodb` automatic candidate while real execution remains disabled. | `migration_core/src/lib.rs`, `tests/test_oneclick_rust_core_gate.py`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_recommend_classifies_deprecated_engine_as_auto_fixable --lib` |
| 2026-06-26 | Added the #138 real-execution evidence validator and optional regression-gate hook without enabling real execution. The validator requires controlled local MySQL evidence for `deprecated_engine -> engine_innodb`, safe `tf_oneclick_` schema scope, app real execution still disabled, no disallowed fix attempts, and before/after `InnoDB` proof. | `scripts/validate-oneclick-real-execution-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_oneclick_real_execution_evidence.py`, `reports/oneclick_readiness/oneclick-real-execution-evidence.template.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_real_execution_evidence.py -q`; final: `pytest tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py -q`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; template expected reject: `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.template.json`; `python -m compileall -q scripts tests`; `git diff --check` |
| 2026-06-26 | Added the Rust Core `oneclick.apply_fixes` execution path for the first allowed automatic fix only: `deprecated_engine -> engine_innodb`. The command now plans allowed actions, skips manual/skip steps, blocks disallowed strategies, requires a MySQL endpoint for real execution, and executes through Rust `MigrationAdapter::execute_sql`; PyQt real execution remains disabled and local before/after evidence is still pending. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_actions_accepts_only_engine_innodb_steps --lib`; RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_plan_executes_engine_innodb_sql --lib`; RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_fixes_real_engine_innodb_requires_endpoint --lib`; final: `cargo test --manifest-path migration_core\Cargo.toml`; `pytest tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py -q`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; template expected reject: `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.template.json`; `git diff --check` |
| 2026-06-26 | Added and ran the controlled local real-execution evidence capture for #138. The archived evidence proves Rust Core `oneclick.apply_fixes` changed the test table from `MyISAM` to `InnoDB`; the PyQt real-execution flag remains disabled because `oneclick.run` still needs UI-facing automatic-fix orchestration. | `src/core/db_core_service.py`, `scripts/capture-oneclick-real-execution-evidence.py`, `tests/test_db_core_service.py`, `tests/test_oneclick_real_execution_capture.py`, `reports/oneclick_readiness/oneclick-real-execution-evidence.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_db_core_service.py::test_facade_uses_oneclick_apply_fixes_protocol -q`; RED/GREEN: `pytest tests\test_oneclick_real_execution_capture.py -q`; capture: `cargo build --manifest-path migration_core\Cargo.toml --release`; `python scripts\capture-oneclick-real-execution-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-real-execution-evidence.json`; final: `pytest tests\test_oneclick_real_execution_capture.py tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py tests\test_db_core_service.py -q`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python -m compileall -q src\core\db_core_service.py scripts\capture-oneclick-real-execution-evidence.py tests\test_oneclick_real_execution_capture.py tests\test_db_core_service.py`; `git diff --check` |
| 2026-06-26 | Added live MySQL discovery for deprecated engine One-Click candidates. Rust Core now marks MyISAM base tables during inspection and converts those markers into typed `deprecated_engine` issues for `engine_innodb` recommendations. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_issues_classify_deprecated_engine_marker_as_auto_fixable --lib`; RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml mysql_deprecated_engine_sql_targets_table_engines --lib`; final: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo test --manifest-path migration_core\Cargo.toml`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Connected `oneclick.run dry_run=false` to the validated `engine_innodb` apply path. The live MySQL regression creates a MyISAM table, runs the UI-facing Rust command, and verifies the table becomes InnoDB. | `migration_core/src/lib.rs`, `migration_core/tests/live_roundtrip.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_engine_innodb_when_env_is_configured --test live_roundtrip -- --nocapture` |
| 2026-06-26 | Opened the PyQt One-Click real-execution gate only for the validated `deprecated_engine -> engine_innodb` scope, kept Dry-run as the default, required backup confirmation for non-dry-run payloads, updated evidence validators/docs/status, and prepared GitHub #138 for closure. | `src/ui/dialogs/oneclick_migration_dialog.py`, `src/ui/dialogs/migration_dialogs.py`, `src/core/i18n.py`, `scripts/validate-oneclick-dry-run-evidence.py`, `scripts/validate-oneclick-real-execution-evidence.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_oneclick_dry_run_evidence.py`, `tests/test_oneclick_real_execution_evidence.py`, `docs/oneclick_readiness.md`, `docs/current_status.md`, `reports/oneclick_readiness/README.md` | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py -q`; final: `pytest tests\test_oneclick_rust_core_gate.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_real_execution_capture.py tests\test_db_core_service.py -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; live: `cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_engine_innodb_when_env_is_configured --test live_roundtrip -- --nocapture`; evidence gate: `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python -m compileall -q ...`; `git diff --check` |
| 2026-06-26 | Closed GitHub #138, scanned remaining open issues, and re-audited #116 as the only remaining open issue. The macOS support gate and focused tests still pass; #116 remains blocked only on real operator Mac evidence. | `docs/current_status.md` | `gh issue list --repo sanghyun-io/tunnelforge --state open --limit 20 --json number,title,labels,url`; `gh issue view 116 --repo sanghyun-io/tunnelforge --json number,title,state,body,comments,url,labels`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` |
| 2026-06-26 | Created GitHub #139 and TF-STATUS-021 for the next actionable One-Click automatic-fix class: charset/collation coverage. | `docs/current_status.md`, `docs/oneclick_readiness.md` | `rg -n "charset_issue|invalid_date|zerofill_usage|float_precision|enum_empty_value|deprecated_engine|engine_innodb|manual|oneclick_recommend|oneclick_apply" migration_core\src\lib.rs tests docs\oneclick_readiness.md`; `gh issue create` created #139 |
| 2026-06-26 | Added the #139 charset/collation evidence validator, JSON template, and optional regression-gate hook without enabling charset real execution. | `scripts/validate-oneclick-charset-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_oneclick_charset_evidence.py`, `reports/oneclick_readiness/oneclick-charset-evidence.template.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_charset_evidence.py -q`; expected reject: `python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.template.json`; expected reject until evidence capture: `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Documented the #139 charset/collation automation policy boundary before enabling any Rust Core recommendation or execution path. | `docs/oneclick_readiness.md`, `docs/current_status.md` | Policy-only change; no charset real execution enabled |
| 2026-06-26 | Historical row: reconfirmed the latest changes were already on `main`/`origin/main` and analyzed the next open issue at that time. #139 was then the next in-repo issue; #116 remained external real-Mac evidence. The next safe #139 step was evidence capture/report scaffolding before any Rust Core charset allowlist expansion. #139 is now closed. | `docs/current_status.md` | `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 30 --json number,title,labels,updatedAt,createdAt,url,assignees`; `gh issue view 139 --comments --json ...`; `gh issue view 116 --json ...`; `rg -n "charset_issue|charset|collation|oneclick_auto_fix_option|oneclick_apply_actions|engine_innodb|deprecated_engine" migration_core\src\lib.rs tests docs\oneclick_readiness.md src` |
| 2026-06-26 | Added the #139 charset/collation capture/report scaffold without enabling live charset execution. The report builder produces validator-backed evidence shape from captured inputs, unsafe `tf_oneclick_` scope checks run before capture, and the live capture entry point fails closed until Rust Core implements the allowlisted path. | `scripts/capture-oneclick-charset-evidence.py`, `tests/test_oneclick_charset_capture.py`, `docs/oneclick_readiness.md`, `reports/oneclick_readiness/README.md`, `docs/current_status.md` | RED: `pytest tests\test_oneclick_charset_capture.py -q` failed because `scripts\capture-oneclick-charset-evidence.py` did not exist; RED: `pytest tests\test_oneclick_charset_capture.py::test_oneclick_charset_capture_cli_fails_closed_without_traceback -q` failed because the CLI raised `CaptureNotImplementedError`; GREEN: `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; expected fail-closed: `python scripts\capture-oneclick-charset-evidence.py --schema tf_oneclick_charset`; expected template reject: `python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.template.json`; `python -m compileall -q scripts\capture-oneclick-charset-evidence.py tests\test_oneclick_charset_capture.py`; `git diff --check` |
| 2026-06-26 | Added an internal Rust Core #139 contract helper for future `charset_issue -> charset_collation_fk_safe` options without wiring it into recommendation or execution paths. The helper validates safe evidence identifiers, explicit target charset/collation, FK-order coverage, rollback SQL, and generated table-level conversion SQL. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_charset_contract --lib` failed because `oneclick_charset_fk_safe_option_from_payload` did not exist; GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Connected the #139 Rust Core charset contract to recommendation and dry-run preview only. Complete `charset_contracts[]` data can produce a `charset_collation_fk_safe` recommendation and `oneclick.apply_fixes dry_run=true` `planned_fixes`; missing contract data remains manual. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_recommend_gates_charset_auto_fix_on_complete_contract --lib` failed with `auto_fixable` 0; RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_fixes_dry_run_previews_charset_plan_without_execution_allowlist --lib` failed with disallowed charset dry-run; GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Added command-level Rust Core charset execution planning for complete `charset_collation_fk_safe` contracts. The adapter path executes generated charset SQL in FK order, preserves rollback SQL/target/fk_order metadata in applied fixes, and reports SQL failure with rollback metadata. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_plan_executes_charset_sql_in_fk_order_with_rollback_metadata --lib` failed because no charset SQL executed; GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `git diff --check` |
| 2026-06-26 | Implemented and captured #139 local MySQL charset/collation evidence through Rust DB Core. The completed report proves `oneclick.apply_fixes dry_run=false` changed the local FK-connected `tf_oneclick_charset` tables from `utf8mb3`/`utf8mb3_general_ci` to `utf8mb4`/`utf8mb4_0900_ai_ci`, preserved FK evidence, and includes rollback metadata. | `scripts/capture-oneclick-charset-evidence.py`, `tests/test_oneclick_charset_capture.py`, `reports/oneclick_readiness/oneclick-charset-evidence.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `pytest tests\test_oneclick_charset_capture.py::test_oneclick_charset_capture_orchestrates_validator_backed_live_report -q` failed because `capture_oneclick_charset` did not accept a facade; GREEN: `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python scripts\capture-oneclick-charset-evidence.py --seed-local-container --mysql-container tf-live-mysql --mysql-host 127.0.0.1 --mysql-port 3406 --mysql-user root --mysql-password test --schema tf_oneclick_charset --output reports\oneclick_readiness\oneclick-charset-evidence.json`; `python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Connected UI-facing Rust Core `oneclick.run dry_run=false` to supplied complete #139 charset contracts. The command now merges payload issues with inspection-derived issues, shifts charset contract indexes safely, and executes the same allowlisted `charset_collation_fk_safe` apply path. | `migration_core/src/lib.rs`, `migration_core/tests/live_roundtrip.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_charset_contract_when_env_is_configured --test live_roundtrip -- --nocapture` |
| 2026-06-26 | Added PyQt coverage for #139 charset execution-plan rendering/count copy, split automatic PyQt charset contract derivation into GitHub #140 / TF-STATUS-022, and closed TF-STATUS-021 after final gates passed. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_renders_charset_plan_counts_and_copy -q`; `pytest tests\test_oneclick_rust_core_gate.py -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; live: `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_charset_contract_when_env_is_configured --test live_roundtrip -- --nocapture`; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check`; `gh issue create` created #140 |
| 2026-06-26 | Started #140 by adding a Rust Core `oneclick.derive_charset_contracts` command and pure facts-based derivation helper. The helper derives complete local-safe `charset_contracts[]` only from safe table facts, FK closure/order, explicit target charset/collation, and rollback SQL; unsafe or incomplete facts produce no contract. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_derives_charset_contract --lib` failed because derivation structs/helper did not exist; RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_derive_charset_contracts_command_returns_contracts_from_safe_facts --lib` failed because no result command existed; GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo test --manifest-path migration_core\Cargo.toml service_hello_advertises_core_protocol --lib` |
| 2026-06-26 | Extended #140 derivation from static facts to live Rust-owned MySQL facts and connected PyQt payload construction to `oneclick.derive_charset_contracts`. The Rust command now synthesizes safe charset issues/contracts from live `information_schema` facts, and `OneClickMigrationWorker._core_payload()` includes derived issues/contracts only when the derivation gate returns both. | `migration_core/src/lib.rs`, `migration_core/tests/live_roundtrip.rs`, `src/core/db_core_service.py`, `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_db_core_service.py`, `tests/test_oneclick_rust_core_gate.py`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_derive_charset_contracts_live_facts_when_env_is_configured --test live_roundtrip -- --nocapture`; RED/GREEN: `pytest tests\test_db_core_service.py::test_facade_uses_oneclick_derive_charset_contracts_protocol -q`; RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_includes_derived_charset_contracts_when_gate_passes tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_omits_charset_contracts_when_derivation_gate_fails -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `python -m compileall -q src\core\db_core_service.py src\ui\dialogs\oneclick_migration_dialog.py tests\test_db_core_service.py tests\test_oneclick_rust_core_gate.py` |
| 2026-06-26 | Rechecked `main`/`origin/main` after the merge request and analyzed the next open issue. The #140 commits are already on `main`; #140 should continue with derivation-specific validator-backed local evidence, and #116 remains separate because it needs real operator Mac validation. | `docs/current_status.md` | `git fetch origin --prune`; `git status --short --branch`; `gh issue list --state open --limit 20 --json number,title,labels,updatedAt,assignees,url`; `gh issue view 140 --comments --json ...`; `gh issue view 116 --json ...`; `rg -n "TF-STATUS-022|#140|derive_charset|oneclick.derive_charset|charset_contracts|OneClickMigrationWorker|derive_oneclick_charset_contracts" ...` |
| 2026-06-26 | Added validator-backed #140 local evidence for PyQt-triggered charset derivation, closed TF-STATUS-022, and closed GitHub #140. The archived report proves `OneClickMigrationWorker._core_payload()` calls Rust Core derivation, includes derived `issues[]` / `charset_contracts[]`, and `oneclick.run dry_run=false` converts the FK-connected local tables. | `scripts/validate-oneclick-charset-derivation-evidence.py`, `scripts/capture-oneclick-charset-derivation-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_oneclick_charset_derivation_evidence.py`, `tests/test_oneclick_charset_derivation_capture.py`, `reports/oneclick_readiness/oneclick-charset-derivation-evidence.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_charset_derivation_evidence.py -q`; RED/GREEN: `pytest tests\test_oneclick_charset_derivation_capture.py -q`; final: `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py tests\test_oneclick_charset_derivation_capture.py tests\test_oneclick_charset_derivation_evidence.py -q`; `cargo build --manifest-path migration_core\Cargo.toml --release`; capture: `python scripts\capture-oneclick-charset-derivation-evidence.py --seed-local-container ...`; validator: `python scripts\validate-oneclick-charset-derivation-evidence.py reports\oneclick_readiness\oneclick-charset-derivation-evidence.json`; gate: `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_DERIVATION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `gh issue comment 140`; `gh issue close 140` |
| 2026-06-26 | Created GitHub #141 and TF-STATUS-023 for the next One-Click repo-side follow-up: resolving the contradictory `int_display_width` skip/manual policy before any implementation. | `docs/current_status.md` | `rg -n "invalid_date|zerofill_usage|float_precision|int_display_width|enum_empty_value|manual|skip|oneclick_issues_from_inspection|oneclick_recommendations|oneclick_auto_fix_option" ...`; `gh issue create` created #141 |
| 2026-06-26 | Resolved #141 / TF-STATUS-023 by documenting `int_display_width` as display-only skip: PyQt may render externally supplied skip payloads, but Rust Core live One-Click does not synthesize this class and `skip` never executes SQL. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `tests/test_oneclick_readiness_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q`; final: `pytest tests\test_oneclick_readiness_docs.py tests\test_oneclick_rust_core_gate.py -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick_live_inspection_does_not_synthesize_int_display_width_skip --lib`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Refreshed the current status Summary after #139-#141 closure so new sessions do not treat a closed One-Click issue as the next repo-side task; re-analyzed #116 as the only open issue. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; final: `pytest tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py -q`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `git diff --check`; `gh issue list --state open --limit 30 --json number,title,labels,updatedAt,url` |
| 2026-06-26 | Scanned current code/docs for untracked TODO, disabled-feature, and stale next-issue wording; found no new repo-side issue beyond external #116 and corrected one stale top Verification Log note about now-closed #140. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; final: `pytest tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py -q`; `git diff --check` |
| 2026-06-26 | Fixed the #116 macOS support gate for the current merged-PR state. The gate now treats PR #117 `state=MERGED` as satisfying merge-state readiness even when GitHub reports `mergeStateStatus=UNKNOWN`, while keeping status-check validation active. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_accepts_merged_pr_with_unknown_merge_state -q`; final: `python scripts\check-macos-support-gate.py`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python -m compileall -q scripts\check-macos-support-gate.py tests\test_rust_core_packaging.py`; `git diff --check` |
| 2026-06-26 | Updated the #116 final gate SHA comparison for merged PR reality: before merge, final reports still match PR #117 head; after merge, they match current merged main HEAD so operators can finalize from the current repository state. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/macos_support.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_uses_local_head_for_final_report_after_pr_merge -q`; RED/GREEN: `pytest tests\test_macos_support_docs.py -q`; final: `python scripts\check-macos-support-gate.py`; focused pytest and compileall |
| 2026-06-26 | Refreshed GitHub #116 body after merged-PR gate fixes so the open issue itself points to current `main` and the updated final gate instead of stale `0717f45`/PR-ready wording. | `docs/current_status.md`, GitHub #116 body | `gh issue view/edit 116`; `pytest tests\test_current_status_docs.py tests\test_macos_support_docs.py -q`; `git diff --check` |
| 2026-06-26 | Added artifact head SHA provenance to the #116 final Mac evidence path. The download helper writes `MACOS_VALIDATION_ARTIFACT_HEAD_SHA`, the report and generated GitHub evidence comment record `Artifact head SHA`, check-complete requires it, and the final gate compares it to the successful manual macOS workflow run. | `scripts/macos-download-validation-artifacts.sh`, `scripts/macos-manual-validation-report.sh`, `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/macos_support.md`, `docs/current_status.md` | RED/GREEN focused pytest; final focused pytest, full macOS support gate, compileall, `git diff --check` |
| 2026-06-26 | Re-scanned repo-side follow-up candidates after #116 remained external and fixed stale One-Click readiness wording that still described closed #138/#139 as current tracking. | `docs/oneclick_readiness.md`, `tests/test_oneclick_readiness_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q`; broad `rg` stale/TODO/disabled scan |
| 2026-06-26 | Tightened One-Click evidence README wording so completed #138/#139 artifacts are not framed as future evidence; templates now read as refresh shapes. | `reports/oneclick_readiness/README.md`, `tests/test_oneclick_readiness_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q` |
| 2026-06-26 | Refreshed #116 body after documentation-only main commits moved current HEAD; issue body now matches the latest final-gate handoff. | `docs/current_status.md`, GitHub #116 body | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; `gh issue view/edit 116` |
| 2026-06-26 | Hardened the #116 handoff against future doc-only commit drift by replacing the fixed current-head SHA in the issue body with latest-pushed-main wording and adding a gate check to reject hard-coded current head SHAs. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/current_status.md`, GitHub #116 body | RED/GREEN: focused pytest and `python scripts\check-macos-support-gate.py` |
| 2026-06-26 | Hardened the #116 handoff against stale `Latest ... actions/runs/<id>` wording; fixed run URLs are now reference evidence, and the gate rejects future reintroduction. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/current_status.md`, GitHub #116 body | RED/GREEN: focused pytest and `python scripts\check-macos-support-gate.py` |
| 2026-06-26 | Refreshed stale top-level macOS focused test count after additional #116 gate coverage increased the focused suite to 51 tests. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; final focused pytest |
| 2026-06-26 | Refreshed stale top-level full pytest count after accumulated test additions increased the suite to 1786 tests. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; final `pytest -q` |
| 2026-06-26 | Replaced misleading #116 `gh run list --workflow ... --branch main` operator guidance with event-filtered commands that were verified to return relevant workflow runs; gate now rejects the bad pattern. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/current_status.md`, GitHub #116 body | RED/GREEN: focused pytest and `python scripts\check-macos-support-gate.py` |
| 2026-06-26 | Tightened #116 final evidence attachment wording so the finalizer and docs direct operators to attach the real-Mac bundle to #116 before closing it, while PR #117 remains only a mirrored traceability target. | `scripts/macos-manual-validation-report.sh`, `docs/macos_support.md`, `tests/test_rust_core_packaging.py`, `docs/current_status.md` | RED/GREEN: focused finalizer pytest; final: focused macOS/docs pytest, full #116 gate, compileall, `git diff --check` |
| 2026-06-26 | Audited the original Export table-selection question and recorded the current contract: the app can export individually selected tables today through `RustDumpExportDialog` -> `RustDumpExporter.export_tables` -> Rust Core `dump.run` `tables` filtering. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_export_table_selection_audit -q`; source/doc/GitHub issue scan |
| 2026-06-26 | Fixed TF-STATUS-024 after finding that direct DB Export/Import dialogs hard-coded the Rust DB Core endpoint host to `127.0.0.1`; both dialogs now preserve `connector.host` while tunnel flows still use their local connector host. | `src/ui/dialogs/db_dialogs.py`, `tests/test_db_dialogs.py`, `docs/current_status.md` | RED/GREEN: focused Export and Import direct-host pytest |
| 2026-06-27 | Analyzed the next remaining issue after main alignment. #116 still needs external real-Mac evidence, but the repo-side handoff had one drift: artifact download defaults still targeted PR #117 head after merge. Fixed TF-STATUS-025 so artifact lookup now follows PR head before merge and current merged main HEAD after PR #117 is merged. | `scripts/macos-download-validation-artifacts.sh`, `scripts/macos-manual-validation-report.sh`, `docs/macos_support.md`, `tests/test_rust_core_packaging.py`, `tests/test_macos_support_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_validation_artifact_download_script_uses_local_head_after_pr_merge -q`; final: macOS/docs focused pytest, shell syntax, current-status tests, #116 gate skip-github, compileall, `git diff --check` |
| 2026-06-27 | Re-scanned disabled-feature docs after #116 remained external and fixed TF-STATUS-026: `SCHEDULE.md` no longer mixes a hidden-feature warning with current public UI instructions. | `SCHEDULE.md`, `tests/test_schedule_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_schedule_docs.py -q`; final: schedule/current-status docs pytest, stale-phrase scan, compileall, `git diff --check` |
| 2026-06-27 | Re-scanned One-Click readiness wording and fixed TF-STATUS-027: docs distinguished that release's backup-confirmed `engine_innodb` real-execution path from unsupported broad production automatic remediation and production charset/collation execution. TF-STATUS-097 Phase A now supersedes that capability. | `docs/oneclick_readiness.md`, `tests/test_oneclick_readiness_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py::test_oneclick_readiness_distinguishes_limited_real_execution_from_broad_production_support -q`; final: One-Click/current-status docs pytest, compileall, `git diff --check` |
| 2026-06-27 | Refreshed TF-STATUS-028 after rerunning the full Python suite. The current suite is now superseded by `1827 passed, 5 warnings`, replacing the stale `1786 passed` handoff count. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count -q`; final: `pytest -q`, docs pytest, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-029 after noticing the top verification table still said `Verified On 2026-06-26` while containing a 2026-06-27 full pytest count. The section now describes a current baseline with preserved broader rows. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_current_baseline_section_is_not_stale_dated -q`; final: current-status pytest, compileall, `git diff --check` |
| 2026-06-27 | Re-audited current main and the next remaining issue. #116 is the only open GitHub issue, #116 repo-side gates pass, macOS focused tests now pass at 53 tests, and the Rust Core boundary scan found no new repo-side baseline violation; legacy-shaped DB connector names currently route through Rust Core shims. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_current_main_next_issue_reaudit -q`; final: #116 gates, macOS/docs focused pytest, current-status pytest, compileall, `git diff --check` |
| 2026-06-27 | Refreshed the top baseline counts after adding current-status re-audit coverage. The current full Python suite is now superseded by the 1827-test run, and the current macOS focused suite is now superseded by the 53-test run. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: stale-count current-status pytest; final: `pytest -q`, current-status pytest, macOS/docs focused pytest, compileall, `git diff --check` |
| 2026-06-27 | Removed a duplicate `--skip-github` row from the focused verification table and added a current-status regression so future focused verification command rows stay unique. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: duplicate-row current-status pytest; final: current-status pytest, compileall, `git diff --check` |
| 2026-06-27 | Merged duplicate `tunnelforge-core service.hello` rows in the current baseline table and added a regression so current baseline command rows stay unique. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: baseline duplicate-row current-status pytest; final: current-status pytest, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-034 after finding legacy-branded Export/Import context-menu wording on the Rust Core path; handlers and labels now use Rust DB Core naming, with a focused source-level regression and refreshed full-suite count. | `src/ui/main_window.py`, `tests/test_main_window_export_import_labels.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: Export/Import label pytest and current-status pytest; final: `pytest -q`, focused docs/UI pytest, compileall, `git diff --check`, #116 gate checks |
| 2026-06-27 | Fixed TF-STATUS-035 after finding One-Click disabled-real-execution fallback copy still pointed at closed #138; the fallback now describes real execution as disabled in this build and keeps dry-run preview wording current. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: One-Click tooltip/current-status pytest; final: `pytest -q`, focused One-Click/current-status pytest, compileall, `git diff --check`, #116 gates |
| 2026-06-27 | Fixed TF-STATUS-036 after finding the One-Click module docstring still overpromised full automatic migration; at that time it was changed to describe Rust DB Core dry-run default and limited backup-confirmed real execution. TF-STATUS-097 Phase A now supersedes that capability. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: One-Click docstring/current-status pytest; final: `pytest -q`, focused One-Click/current-status pytest, compileall, `git diff --check`, #116 gates |
| 2026-06-27 | Fixed TF-STATUS-037 after finding stale Windows installer version examples in `BUILD.md`; output/test paths now use `{version}` and the Inno snippet uses `{#MyAppVersion}`. | `BUILD.md`, `tests/test_build_docs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: build-doc/current-status pytest; final: `pytest -q`, focused build-doc/current-status pytest, compileall, `git diff --check`, #116 gates |
| 2026-06-27 | Fixed TF-STATUS-038 after finding that #116 final gate manual workflow lookup still targeted PR #117 head after merge while artifact download/report SHA policy had moved to current merged main HEAD. | `scripts/check-macos-support-gate.py`, `docs/macos_support.md`, GitHub #116 body, `tests/test_rust_core_packaging.py`, `tests/test_macos_support_docs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: manual workflow head-policy pytest, macOS support docs pytest, current-status pytest; #116 body updated and full gate rechecked |
| 2026-06-27 | Recorded TF-STATUS-039 after a post-merge next-issue re-audit found no new repo-side issue: #116 is still the only open GitHub issue, full #116 gates pass, and SQL editor execution also routes through Rust Core connector shims. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: post-merge current-status pytest; final: current-status pytest, #116 gates, compileall, `git diff --check` |
| 2026-06-27 | Created GitHub #142 and TF-STATUS-040 after finding a separate repo-side Rust Core baseline gap: the legacy Auto-Fix Wizard can still execute DB mutations through Python-owned fix logic. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #142 | RED/GREEN: legacy Auto-Fix current-status pytest; final: current-status pytest, issue scan, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-040 / GitHub #142 by making the legacy Auto-Fix Wizard dry-run/manual SQL only and fail-closing `FixWizardWorker` when `dry_run=False` is requested. | `src/ui/dialogs/fix_wizard_dialog.py`, `src/ui/workers/fix_wizard_worker.py`, `src/core/i18n.py`, `tests/test_fix_wizard_dialog.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #142 | RED/GREEN: legacy Auto-Fix dialog/worker pytest and current-status pytest; final: full pytest, #116 gates, compileall, `git diff --check` |
| 2026-06-27 | Analyzed the next open issue after #142 closure. #116 is still the only open GitHub issue; normal repo-side gate passes, while `--final` fails because the real-Mac report and current-main manual workflow_dispatch evidence are not present. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#142 current-status pytest; final: #116 gate, expected-failing final gate, current-status pytest, full pytest, compileall, `git diff --check` |
| 2026-06-27 | Created and fixed TF-STATUS-041 / GitHub #143 after finding that the underlying legacy Auto-Fix core APIs still accepted `dry_run=False` after #142 closed the user-visible worker path. | `src/core/migration_fix_wizard.py`, `tests/test_migration_fix_wizard.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #143 | RED/GREEN: legacy core mutation API pytest and current-status pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
