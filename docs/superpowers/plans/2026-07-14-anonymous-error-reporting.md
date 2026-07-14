# Anonymous Error Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace client-side GitHub credentials with a consent-based, privacy-allowlisted TunnelForge error reporter backed by a repository-scoped GitHub App and a Cloudflare Worker relay, while revoking the credential exposed in legacy releases.

**Architecture:** Python/PyQt builds a versioned report from explicit local fields, sanitizes it, and sends it to one HTTPS relay without affecting the source Export/Import result. A TypeScript Cloudflare Worker independently validates and sanitizes the report, applies edge and D1 quotas, recomputes routing fingerprints, and uses a dedicated GitHub App to create or update public issues. Credential containment is a separate lane and does not wait for feature delivery.

**Tech Stack:** Python 3.9+, PyQt6, requests, pytest, TypeScript 7, Cloudflare Workers/Wrangler 4, Vitest 4 with Cloudflare Workers pool, D1, Web Crypto, GitHub REST API.

## Global Constraints

- `tunnelforge-core` remains the DB-operation owner. Error reporting may read already-known metadata but must not open a DB connection, execute SQL, or add a Python driver path.
- No GitHub credential, shared API secret, canary token, JWT, installation token, PEM, or user token may enter Python source, packaged artifacts, client config, logs, fixtures, or command arguments.
- The client sends only schema v1 fields. Unknown/arbitrary context is rejected rather than filtered after collection.
- Every report object rejects unknown properties. The public endpoint treats installation IDs and fingerprints as attacker-controlled hints, never as authentication.
- Existing `github_auto_report=true` does not count as consent and must not migrate to enabled.
- Consent prompt exposure is initial plus one retry after 30 full days, at most twice total. Showing the dialog performs no network request.
- Reporting is best-effort and never changes Export/Import success, failure, cancellation, shutdown, or cleanup behavior.
- TLS verification stays enabled. The client opens no inbound listener and persists no report queue.
- The Worker never logs or stores raw payload, source IP, anonymous ID, HMAC, error message, JWT, token, PEM, or request body.
- Repository mutations are protected by D1 atomic global budgets and a pre-create lease. Edge/IP/installation rate limits are burst controls only, not the final security boundary.
- GitHub create timeouts enter an `unknown` quarantine state and are never blindly retried; this prevents duplicate public issues when the upstream result is ambiguous.
- The new GitHub App is installed only on `sanghyun-io/tunnelforge` with Metadata read and Issues read/write.
- Worker mode defaults to `off`; a client build with a production relay URL cannot ship until `off`, `shadow`, `canary`, and `active` evidence passes.
- Use TDD for every task. Record a focused RED before production changes and fresh GREEN before each commit.
- Keep each task independently reviewable. A task is not complete until specification review and code-quality/security review both approve it.

---

### Task 1: Versioned Report Contract and Adversarial Fixtures

**Model:** Implementer `gpt-5.6-sol` high; reviewers `gpt-5.6-sol` high and `gpt-5.6-terra` high.

**Files:**
- Create: `contracts/error-reporting/v1/schema.json`
- Create: `contracts/error-reporting/v1/valid-minimal.json`
- Create: `contracts/error-reporting/v1/valid-full.json`
- Create: `contracts/error-reporting/v1/invalid-cases.json`
- Create: `contracts/error-reporting/v1/redaction-cases.json`
- Create: `src/core/error_report_schema.py`
- Create: `tests/test_error_report_schema.py`

**Interfaces:**
- Produces `REPORT_SCHEMA_VERSION = 1`, field enums and bounds, `ReportValidationError`, and `validate_report_payload(payload: object) -> dict`.
- JSON contract fixtures are language-neutral inputs consumed unchanged by Python and Worker tests.

- [ ] **Step 1: Write failing schema tests**

Require exact top-level groups `report`, `app`, `system`, `runtime`, `operation`, and `error`; reject unknown keys at every level. Test UUIDv4 installation ID, 64-character lowercase SHA-256 fingerprint, enum values, UTC offset `[-840, 840]`, message length `<= 2000`, and application frame count `<= 20`.

```python
def test_schema_rejects_unknown_nested_field(valid_payload):
    valid_payload["operation"]["schema_name"] = "production"
    with pytest.raises(ReportValidationError, match="unknown field"):
        validate_report_payload(valid_payload)
```

- [ ] **Step 2: Run RED**

Run `pytest tests/test_error_report_schema.py -q`.

Expected: import failure because `src.core.error_report_schema` does not exist.

- [ ] **Step 3: Implement a dependency-free strict validator**

Use explicit dictionaries and type checks rather than adding `jsonschema` to the desktop runtime. Return a newly constructed canonical dictionary; never return or mutate arbitrary input.

```python
TOP_LEVEL_FIELDS = frozenset({"report", "app", "system", "runtime", "operation", "error"})


def _require_exact_keys(value: dict, allowed: frozenset[str], path: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ReportValidationError(f"{path}: unknown field: {sorted(unknown)[0]}")
```

- [ ] **Step 4: Add complete shared fixtures**

`invalid-cases.json` must name each case and expected rejection class. `redaction-cases.json` must include credentials, Authorization headers, URLs/DSNs, IPv4/IPv6, emails, Windows/POSIX/UNC paths, SQL, quoted identifiers, Markdown controls, Unicode separators, control characters, and high-entropy bearer-like strings. Fixtures contain synthetic values only.

- [ ] **Step 5: Run GREEN and commit**

Run `pytest tests/test_error_report_schema.py -q` and `git diff --check`.

Commit: `feat: define anonymous error report contract`.

---

### Task 2: Client Environment Collector, Sanitizer, and SHA-256 Fingerprint

**Model:** Implementer `gpt-5.6-sol` high; reviewers `gpt-5.6-sol` high and `gpt-5.6-terra` high.

**Files:**
- Create: `src/core/error_report_sanitizer.py`
- Create: `src/core/error_report_environment.py`
- Create: `src/core/error_report_builder.py`
- Create: `tests/test_error_report_sanitizer.py`
- Create: `tests/test_error_report_builder.py`

**Interfaces:**
- Produces `sanitize_error_text(text: object, max_length: int = 2000) -> str`.
- Produces `collect_environment() -> dict` using local standard/Qt APIs only.
- Produces `build_error_report(config_manager, *, operation_kind, db_engine, phase, error_message, exception=None, db_server_version=None) -> dict`.

- [ ] **Step 1: Write failing sanitizer and collector tests**

Test every shared redaction fixture, post-redaction length bounds, no hardware/user/host values, no network/subprocess/DB call, numeric dotted runtime versions, major/minor DB normalization, relative TunnelForge frames only, and deterministic SHA-256 fingerprinting.

```python
def test_builder_never_accepts_arbitrary_context(config_manager):
    with pytest.raises(TypeError):
        build_error_report(
            config_manager,
            operation_kind="export",
            db_engine="mysql",
            phase="dump.run",
            error_message="failure",
            context={"schema": "secret"},
        )
```

- [ ] **Step 2: Run RED**

Run `pytest tests/test_error_report_sanitizer.py tests/test_error_report_builder.py -q`.

- [ ] **Step 3: Implement allowlist-first collection**

Use `platform`, `sys`, `locale`, `datetime.now().astimezone().utcoffset()`, `src.version.__version__`, and PyQt version constants. Generate a UUIDv4 once through the consent/settings store. Do not call Rust Core or a connector; accept already-known safe versions as optional inputs.

- [ ] **Step 4: Implement fail-closed text sanitization**

Normalize Unicode and control characters, then replace forbidden patterns with fixed placeholders. Strip Markdown controls from scalar values. Extract at most 20 traceback frames whose normalized module begins with `src.` and store only module, function, and positive line number.

- [ ] **Step 5: Compute and validate the report**

SHA-256 input is canonical JSON over operation kind, engine, exception class, normalized error code, and application frame signature. Call `validate_report_payload()` before returning.

- [ ] **Step 6: Run GREEN and commit**

Run focused tests plus `pytest tests/test_error_report_schema.py -q`.

Commit: `feat: build privacy-allowlisted error reports`.

---

### Task 3: Atomic Consent Policy and Settings Persistence

**Model:** Implementer `gpt-5.6-terra` high; reviewers `gpt-5.6-sol` high and `gpt-5.6-terra` high.

**Files:**
- Modify: `src/core/config_manager.py`
- Create: `src/core/error_report_consent.py`
- Modify: `tests/test_config_manager.py`
- Create: `tests/test_error_report_consent.py`

**Interfaces:**
- Adds `ConfigManager.set_app_settings(updates: Mapping[str, object]) -> None` as one `_mutate_config` transaction.
- Produces `ConsentState`, `ConsentPolicy`, `PromptOutcome`, and `CONSENT_VERSION = 1`.
- Produces `ConsentPolicy.should_prompt(now: datetime) -> bool`, `record_outcome(...)`, `set_enabled(bool)`, and `is_enabled() -> bool`.

- [ ] **Step 1: Write RED state-machine tests**

Cover initial prompt, 29 days, exactly 30 days, first defer, second/final defer, suppression, activation, user disable, corrupt timestamps, future clock skew, old consent version, and legacy `github_auto_report=true` remaining disabled.

- [ ] **Step 2: Run RED**

Run `pytest tests/test_config_manager.py tests/test_error_report_consent.py -q`.

- [ ] **Step 3: Add atomic settings update**

```python
def set_app_settings(self, updates):
    safe_updates = dict(updates)

    def mutator(config):
        settings = config.setdefault("settings", {})
        settings.update(safe_updates)
        return True, None

    self._mutate_config(mutator)
```

- [ ] **Step 4: Implement the explicit state transitions**

Persist `error_reporting_state`, `error_reporting_consent_version`, `error_reporting_prompt_count`, `error_reporting_deferred_until`, and `error_reporting_installation_id` atomically. A second close always enters `prompt_exhausted`. A disabled user never returns to an automatic prompt.

- [ ] **Step 5: Run GREEN and commit**

Commit: `feat: add anonymous reporting consent policy`.

---

### Task 4: Consent Dialog and Deferred Startup Presentation

**Model:** Implementer `gpt-5.6-terra` high; reviewers `gpt-5.6-sol` high and `gpt-5.6-terra` high.

**Files:**
- Create: `src/ui/dialogs/error_reporting_consent_dialog.py`
- Modify: `src/ui/dialogs/__init__.py`
- Modify: `src/ui/main_window.py`
- Modify: `src/core/i18n/keys.py`
- Modify: `src/core/i18n/legacy_translate.py`
- Create: `tests/test_error_reporting_consent_dialog.py`
- Create: `tests/test_main_window_error_reporting_consent.py`
- Modify: `tests/test_i18n.py`

**Interfaces:**
- `ErrorReportingConsentDialog.get_outcome() -> tuple[PromptOutcome, bool]` returns activation/later and unchecked/checked suppression.
- `TunnelManagerUI._maybe_show_error_reporting_consent()` evaluates policy and records one outcome.

- [ ] **Step 1: Write RED UI/state tests**

Verify exact Korean/English disclosure, public GitHub issue warning, collected/excluded expanders, Settings path, unchecked suppression checkbox, primary enable action, Later, close, Escape, and zero transport calls while displaying the dialog.

- [ ] **Step 2: Run RED offscreen**

```powershell
$env:QT_QPA_PLATFORM='offscreen'
pytest tests/test_error_reporting_consent_dialog.py tests/test_main_window_error_reporting_consent.py tests/test_i18n.py -q
```

- [ ] **Step 3: Implement the modal**

Use standard Qt widgets, existing button styles, word-wrapped copy, accessible names, stable minimum/maximum dimensions, and no nested card styling. The suppression checkbox defaults false. Reject/Escape is Later.

- [ ] **Step 4: Schedule once after the first visible main window**

Set `_error_reporting_prompt_scheduled` and `_error_reporting_prompt_shown` flags. In `showEvent`, schedule `QTimer.singleShot(500, ...)` only once. `--minimized` naturally defers until the first visible `showEvent`. Re-check policy immediately before opening.

- [ ] **Step 5: Run GREEN and commit**

Commit: `feat: add anonymous reporting consent prompt`.

---

### Task 5: Relay Transport and Background Reporting Worker

**Model:** Implementer `gpt-5.6-sol` high; reviewers `gpt-5.6-sol` high and `gpt-5.6-terra` high.

**Files:**
- Create: `src/core/error_report_transport.py`
- Create: `src/ui/workers/error_reporting_worker.py`
- Modify: `src/ui/workers/__init__.py`
- Delete: `src/ui/workers/github_worker.py`
- Modify: `src/ui/dialogs/db_export_dialog.py`
- Modify: `src/ui/dialogs/db_import_dialog.py`
- Create: `tests/test_error_report_transport.py`
- Create: `tests/test_error_reporting_worker.py`
- Modify: `tests/test_db_export_dialog.py`
- Modify: `tests/test_db_import_dialog.py`

**Interfaces:**
- Produces `RelayResult(success, message, issue_url, status_code)`.
- Produces `ErrorReportTransport.submit(payload) -> RelayResult` and `health() -> RelayResult`.
- `ErrorReportingWorker.report_finished` emits `(success: bool, message: str, issue_url: str)`; inherited `QThread.finished` remains the lifecycle signal.
- `ErrorReportingMixin._start_error_report_worker(...)` accepts only allowlisted operation arguments and never accepts an arbitrary error message.
- `GET /health` succeeds only for HTTP 200 with exactly `{"service":"issue-relay","schema":1,"mode":"off|shadow|canary|active"}`.
- `POST /v1/reports` succeeds only for one exact response: HTTP 202 `accepted` with a canonical UUIDv4 `receipt`; HTTP 201 `created` with a canonical TunnelForge issue URL; or HTTP 200 `updated`/`duplicate` with that URL. Success responses contain no display message. Duplicate JSON members are invalid even when their values match.

- [ ] **Step 1: Write RED transport and integration tests**

Test HTTPS-only URL validation including C0/whitespace rejection, Requests `(3.05, 8.0)` connect/read timeout, explicit `verify=True` overriding an injected `Session.verify=False` while preserving `REQUESTS_CA_BUNDLE`, environment proxy behavior, fixed-length/chunked/gzip/connection-close response framing, bounded response parsing, exactly one retry only for `RequestException` or HTTP 502/503/504, no 429 or other 4xx retry, no payload logging, pre-permit revoke/disable-reenable races, post-permit nonblocking disable before Python enters `Session.post`, worker retention through inherited lifecycle completion, stale Export/Import result isolation, C++-deleted receivers, and no schema/table/failed-table context from Export/Import.

- [ ] **Step 2: Run RED**

Run focused transport, worker, export, and import tests offscreen.

- [ ] **Step 3: Implement best-effort transport**

Use `requests.Session.post(..., json=payload, timeout=(3.05, 8.0), verify=True)` and the equivalent explicit verification argument for health checks. This overrides an injected Session's insecure default while Requests still resolves `REQUESTS_CA_BUNDLE` through normal environment merging. This is a bounded connect timeout plus a bounded socket-read inactivity timeout, not an absolute request wall-clock deadline. Do not traverse or mutate private Requests/urllib3 socket internals. Bound decoded response bytes and require the exact endpoint-specific wire objects above; convert them to fixed local `RelayResult.message` strings and never display or log remote strings. Retry exactly once after `RequestException` or HTTP 502/503/504, and do not retry 429, any other 4xx, or any other HTTP status.

- [ ] **Step 4: Replace the worker and dialog calls**

Remove arbitrary context and error-message construction. Export passes `operation_kind="export"`, connector engine, and `phase="dump.run"`; Import passes `operation_kind="import"`, connector engine, and `phase="dump.import"`. The worker derives the fixed safe builder message from that allowlisted operation/phase pair. Increment an operation-log generation whenever Export/Import begins or resets, capture it when starting the report worker, and write the report completion log only if that generation remains current. Always perform worker lifecycle cleanup, including for stale operation results. Export/Import retain the full escaped local diagnostic for their own UI while relay input contains no object name or raw Rust output. Before local credential matching, safely recognize Unicode-escaped sensitive key spellings and escaped quotes. Redact bounded known provider-token prefixes such as `ghp_`, `github_pat_`, `glpat-`, `sk-`, and `xox[baprs]-`, but preserve ordinary identifiers such as `customer_orders_archive_partition_2024`. Escape C0, format controls, and Unicode line/paragraph separators at every Rust-derived display and saved-log boundary so malformed local diagnostics cannot forge entries.

Consent token validation and every consent mutation share one process-local linearization lease. `authorize_submission()` is the linearization and dispatch-commit point. Revocation, including disable then re-enable, that linearizes before the permit prevents commit. After a true permit, the report is no longer pending and later revocation does not recall the committed dispatch, even if Python has not entered `Session.post`. Never hold the lease across blocking `Session.post`; this keeps the Settings disable path nonblocking.

- [ ] **Step 5: Run GREEN and commit**

Commit: `feat: route anonymous reports through relay`.

---

### Task 6: Settings UX, Local Preview, Health Test, and Last Status

**Model:** Implementer `gpt-5.6-terra` high; reviewers `gpt-5.6-sol` high and `gpt-5.6-terra` high.

**Files:**
- Modify: `src/ui/dialogs/settings.py`
- Modify: `src/core/i18n/keys.py`
- Modify: `src/core/i18n/legacy_translate.py`
- Create: `tests/test_settings_error_reporting.py`
- Modify: `tests/test_settings_update_actions.py`
- Modify: `tests/test_i18n.py`

**Interfaces:**
- Replaces the GitHub App configuration group with `Anonymous Error Reporting` controls.
- Preview uses current local allowlisted environment plus a synthetic error and never sends it.
- Health test uses a retained background worker and does not enable consent.

- [ ] **Step 1: Write RED Settings tests**

Require enable/disable, disclosure, local preview, connection test, last attempt, clickable issue URL, disabled state when relay URL is not configured, and exact `Settings > General > Anonymous Error Reporting` copy.

- [ ] **Step 2: Implement the unframed compact controls**

Use one group box consistent with current Settings. Use a checkbox for enablement, text buttons for preview/test commands, and a read-only modal for JSON preview. Do not expose App ID, installation ID, PEM, environment variable setup, or token language.

- [ ] **Step 3: Persist explicit user choices only**

Settings enable calls the consent policy and records the current consent version. Settings disable enters `disabled_by_user`. Merely opening or saving unrelated Settings does not change consent.

- [ ] **Step 4: Run GREEN and commit**

Commit: `feat: add anonymous reporting settings controls`.

---

### Task 7: Remove Client GitHub Credentials and Retired Direct Reporter

**Model:** Implementer `gpt-5.6-luna` high; reviewers `gpt-5.6-sol` high and `gpt-5.6-terra` high.

**Files:**
- Delete: `src/core/github_app_auth.py`
- Delete: `src/core/github_issue_reporter.py`
- Delete: `src/core/error_summary_builder.py`
- Delete: `tests/test_github_app_auth.py`
- Delete: `tests/test_github_issue_reporter.py`
- Delete: `tests/test_error_summary_builder.py`
- Modify: `src/core/__init__.py`
- Modify: `pyproject.toml`
- Modify: `tunnel-manager.spec`
- Replace: `GITHUB_APP_SETUP.md` with `docs/error_reporting.md`
- Modify: `.gitignore`
- Create: `tests/test_error_reporting_packaging.py`

**Interfaces:**
- Removes desktop `PyJWT` and `python-dotenv` dependencies and the `jwt`/`dotenv` PyInstaller hidden imports.
- Preserves `requests` because update and relay clients use it.

- [ ] **Step 1: Write a RED whole-tree retirement test**

Assert no production client file references `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_INSTALLATION_ID`, `GH_APP_PRIVATE_KEY`, PEM loading, `github_app_auth`, `github_issue_reporter`, or retired embed scripts. Assert `PyJWT` and `python-dotenv` are absent from runtime dependencies.

- [ ] **Step 2: Remove direct-auth code and docs**

Delete the modules/tests, update exports, remove dependencies/hidden imports, and document only consent, allowlist, relay behavior, and troubleshooting. Keep secret example exclusions for relay-local `.dev.vars*` and never introduce a real secret file.

- [ ] **Step 3: Run focused, packaging, and full Python tests**

Run the new retirement scan, auth/reporting/settings tests, `pytest -q`, and PyInstaller analysis/build smoke after Rust Core release build exists.

- [ ] **Step 4: Commit**

Commit: `refactor(security): remove client GitHub credentials`.

---

### Task 8: Cloudflare Worker Project, Strict Validator, and Sanitizer

**Model:** Implementer `gpt-5.6-sol` max; reviewers `gpt-5.6-sol` max and `gpt-5.6-terra` high.

**Files:**
- Create: `services/issue-relay/package.json`
- Create: `services/issue-relay/package-lock.json`
- Create: `services/issue-relay/tsconfig.json`
- Create: `services/issue-relay/wrangler.jsonc`
- Create: `services/issue-relay/vitest.config.ts`
- Create: `services/issue-relay/src/types.ts`
- Create: `services/issue-relay/src/schema.ts`
- Create: `services/issue-relay/src/sanitize.ts`
- Create: `services/issue-relay/src/fingerprint.ts`
- Create: `services/issue-relay/src/observability.ts`
- Create: `services/issue-relay/test/schema.test.ts`
- Create: `services/issue-relay/test/sanitize.test.ts`

**Interfaces:**
- Worker dev dependencies pin compatible, currently resolved Wrangler, Workers Vitest pool, Vitest, TypeScript, and Workers types versions in the lockfile. The first install must prove the exact set with tests, typecheck, audit, and dry-run bundling before it becomes contractual.
- Produces `parseReport(request)`, `sanitizeReport(report)`, and `computeFingerprint(report)`.

- [ ] **Step 1: Scaffold with tests and lockfile**

Use npm only inside `services/issue-relay`. Configure Workers runtime tests and load the repository contract fixtures by relative path.

- [ ] **Step 2: Write and run RED Worker contract tests**

Run `npm test -- --run` from `services/issue-relay`.

Expected: failures because validator/sanitizer modules are absent.

- [ ] **Step 3: Implement independent validation and sanitization**

Do not reuse client Markdown or trust the client fingerprint. Enforce method, content type, an application maximum body of 16 KiB, parse depth/shape, enums, bounds, and `additionalProperties: false` semantics for every object before any persistence or GitHub call. Recompute the fingerprint server-side and return 422 on mismatch. Errors expose only `{error: {code, retryable}}`.

- [ ] **Step 4: Run GREEN, typecheck, and commit**

Run `npm test -- --run`, `npm run typecheck`, `npm audit --omit=dev`, and `npx wrangler deploy --dry-run`.

Commit: `feat(relay): validate anonymous reports at edge`.

---

### Task 9: D1 Idempotency, Quotas, and Operating Modes

**Model:** Implementer `gpt-5.6-sol` max; reviewers `gpt-5.6-sol` max and `gpt-5.6-terra` high.

**Files:**
- Create: `services/issue-relay/migrations/0001_init.sql`
- Create: `services/issue-relay/src/store.ts`
- Create: `services/issue-relay/src/quotas.ts`
- Create: `services/issue-relay/src/modes.ts`
- Create: `services/issue-relay/src/index.ts`
- Create: `services/issue-relay/test/store.test.ts`
- Create: `services/issue-relay/test/modes.test.ts`
- Modify: `services/issue-relay/wrangler.jsonc`

**Interfaces:**
- D1 stores only server fingerprint, HMAC-derived installation key, issue route/lease state, action status, counters, and expiry.
- `off` returns before reading the body or touching bindings; `shadow` validates without D1/GitHub; `canary` requires constant-time `Authorization: Bearer <CANARY_ADMIN_TOKEN>` verification; `active` accepts public schema-valid reports.

- [ ] **Step 1: Write RED D1/mode tests**

Cover lease races, failed and `unknown` transitions, 24-hour installation/fingerprint idempotency, edge burst controls, per-install hourly quota, atomic global creation/comment budgets, expiration cleanup, and every mode's binding access. Rotate installation IDs and simulated edge locations to prove the D1 global cap still holds.

- [ ] **Step 2: Create minimal D1 tables**

Create `issue_routes(fingerprint PRIMARY KEY, issue_number, state, lease_token, lease_until, ...)`, `report_actions(installation_hmac, fingerprint, window, kind, state, expires_at, PRIMARY KEY (...))`, and `write_budgets(bucket, kind, used, hard_limit, PRIMARY KEY (...))`. Do not include a body/message/IP/anonymous-ID column.

- [ ] **Step 3: Implement HMAC and bounded quotas**

Use Web Crypto HMAC-SHA256 with `INSTALLATION_ID_HMAC_KEY`. Start edge controls at IP `3/10s` and `10/min` plus installation-HMAC `3/min`, without treating them as authentication. Atomically cap GitHub creates at `5/hour` and `20/day`, and comments at `20/hour` and `100/day`; exceeding any budget forbids the GitHub call.

- [ ] **Step 4: Implement health and report routing**

`GET /health` returns exactly `{"service":"issue-relay","schema":1,"mode":"off|shadow|canary|active"}`. Stable 4xx/429/5xx responses never echo payload. `shadow` returns HTTP 202 with exactly `{"status":"accepted","receipt":"<random canonical UUIDv4>"}` and stores nothing. A concurrent pending lease uses the same 202 `accepted` receipt contract. Invocation logs/traces are disabled in Wrangler configuration, and code never passes bodies, headers, exception objects, IPs, UUIDs, or HMACs to `console.*`.

- [ ] **Step 5: Run GREEN and commit**

Commit: `feat(relay): add quotas and rollout modes`.

---

### Task 10: GitHub App JWT, Installation Token, and Safe Issue Upsert

**Model:** Implementer `gpt-5.6-sol` max; reviewers `gpt-5.6-sol` max and `gpt-5.6-terra` high.

**Files:**
- Create: `services/issue-relay/src/github-auth.ts`
- Create: `services/issue-relay/src/github-issues.ts`
- Create: `services/issue-relay/src/issue-format.ts`
- Create: `services/issue-relay/test/github-auth.test.ts`
- Create: `services/issue-relay/test/github-issues.test.ts`
- Create: `services/issue-relay/test/issue-format.test.ts`
- Modify: `services/issue-relay/src/index.ts`

**Interfaces:**
- `getInstallationToken(env, forceRefresh=False)` caches module-local tokens until five minutes before expiry.
- `upsertIssue(report, fingerprint)` returns issue number/URL/action and never accepts title/body/labels from the client.

- [ ] **Step 1: Write RED auth and formatting tests**

Test RS256 JWT claims (`iat=now-60`, `exp<=now+540`, `iss=App ID`), PKCS#8 key import, explicit repository/Issues permission token request, 401-only one-refresh behavior, no refresh on 403, no token/PEM logging, escaped bounded Markdown, hidden fingerprint marker, create, duplicate comment, closed/missing route recovery, and GitHub timeout/error behavior.

- [ ] **Step 2: Implement Web Crypto signing and token cache**

Import only PKCS#8 PEM through `crypto.subtle.importKey`. Use GitHub API version `2026-03-10`. Installation-token request specifies the TunnelForge repository and `issues: write` permission.

- [ ] **Step 3: Implement server-owned issue construction**

Title and body use validated enums/scalars and fixed templates. Labels are fixed `bug`, operation-specific label, and `auto-reported`. Duplicate comments include only bounded recurrence count/environment summaries, never raw client Markdown.

- [ ] **Step 4: Bind D1 reservation to GitHub action**

Atomically acquire a fingerprint lease before create, return 202 for concurrent leases, and update the route after success. A definite pre-send failure may release the lease; a timeout or ambiguous upstream result transitions to `unknown` and blocks automatic create retry until reconciliation. Never create when duplicate lookup or route state is ambiguous.

Map upsert outcomes exactly: `created` -> HTTP 201 `{status:"created",issue_url}`; `commented` -> HTTP 200 `{status:"updated",issue_url}`; and a recurrence requiring no mutation -> HTTP 200 `{status:"duplicate",issue_url}`. Pending lease responses remain HTTP 202 `accepted` with a canonical UUIDv4 receipt. No successful response contains a remote display message.

- [ ] **Step 5: Run GREEN, dry-run bundle, and commit**

Commit: `feat(relay): create issues with dedicated GitHub App`.

---

### Task 11: Relay Security Tests, Deployment Docs, and Secret-Safe Operations

**Model:** Implementer `gpt-5.6-sol` high; reviewers `gpt-5.6-sol` max and `gpt-5.6-terra` high.

**Files:**
- Create: `services/issue-relay/README.md`
- Create: `services/issue-relay/.dev.vars.example`
- Create: `services/issue-relay/scripts/smoke.mjs`
- Create: `services/issue-relay/test/security.test.ts`
- Modify: `.gitignore`
- Modify: `docs/error_reporting.md`
- Modify: `tests/test_error_reporting_packaging.py`

**Interfaces:**
- Documents exact GitHub App creation, PKCS#1-to-PKCS#8 conversion, Wrangler login, D1 creation/migration, secret upload, mode change, rollback, canary, and credential deletion steps.
- Smoke script accepts endpoint/mode from environment, uses synthetic fixtures, and never accepts a private key.

- [ ] **Step 1: Add RED secret/log/abuse tests**

Capture Worker logs under malformed payloads and mocked GitHub failures; assert forbidden values are absent. Fuzz unknown fields, deep JSON, long arrays/strings, control characters, and Markdown injection. Prove global quotas cap repository mutations.

- [ ] **Step 2: Write operator instructions with no secret command arguments**

Use interactive `wrangler secret put` or Cloudflare dashboard entry. Explain PKCS#8 conversion in a private local directory and immediate cleanup. Never ask the owner to paste a key into chat.

- [ ] **Step 3: Run full relay verification**

Run tests, typecheck, audit, dry-run bundle, local D1 migrations, local off/shadow/canary/active smoke, and repository secret scan.

- [ ] **Step 4: Commit**

Commit: `docs(relay): add secure deployment runbook`.

---

### Task 12: Manual Credential Containment and Cloud Account Gate

**Model:** Coordinator `gpt-5.6-sol` max; owner performs secret-bound UI actions.

**Files:**
- Modify after evidence: `docs/current_status.md`
- Modify after evidence: `tests/test_current_status_docs.py`

**Interfaces:**
- Produces recorded App IDs, installation scope, non-secret SHA-256 public-key fingerprints, Releaser coupling verdict, old-key deletion evidence, and unused secret deletion evidence.

- [ ] **Step 1: Give the owner a Developer Settings inventory checklist**

Request only App name/ID, installed repositories, and displayed public-key fingerprints. Do not request PEM content.

- [ ] **Step 2: Resolve the Releaser coupling gate**

If independent, delete the exposed App ID `2735888` key. If coupled, generate an overlapping key, update `RELEASER_APP_PRIVATE_KEY` interactively, run a controlled version-label PR through `version-bump`, then delete the exposed key.

- [ ] **Step 3: Wait out derived token lifetime and remove unused secret**

Treat installation tokens as potentially valid for one hour. Delete repository secret `GH_APP_PRIVATE_KEY` only after external consumer inventory is clear.

- [ ] **Step 4: Authenticate Cloudflare without sharing credentials**

Owner completes `npx wrangler login` in the local browser and confirms account name/ID only. Coordinator verifies `wrangler whoami` and proceeds without receiving login credentials.

- [ ] **Step 5: Record evidence without closing TF-STATUS-092 prematurely**

Use `fixed_pending_full_verify` after exposed-key deletion if relay/client work remains. Close only after final active/canary/client verification.

---

### Task 13: Shadow, Canary, Active, Client Endpoint, and Final Verification

**Model:** Coordinator `gpt-5.6-sol` max; reviewers `gpt-5.6-sol` max and `gpt-5.6-terra` high.

**Files:**
- Modify: `services/issue-relay/wrangler.jsonc`
- Create: `src/core/error_reporting_config.py`
- Modify: `tests/test_error_report_transport.py`
- Modify: `docs/current_status.md`
- Modify: `tests/test_current_status_docs.py`

**Interfaces:**
- Production client URL is an exact HTTPS Worker/custom-domain URL in `error_reporting_config.py`; no secret is present.
- Final evidence includes Worker version, mode transitions, canary issue URL, duplicate action, off rollback, active health, hosted CI, and package smoke.

- [ ] **Step 1: Deploy `off` and create/migrate D1**

Verify health and prove report submission performs no GitHub subrequest.

- [ ] **Step 2: Upload secrets and deploy `shadow`**

Owner uploads secrets interactively. Run valid, invalid, oversized, unknown-field, redaction, and rate-limit probes. Confirm no D1 raw payload and no GitHub issue.

- [ ] **Step 3: Deploy `canary` and exercise one synthetic route**

Create exactly one designated synthetic issue and one duplicate update. Inspect public content for leaks, verify App attribution/labels, then close the canary issue.

- [ ] **Step 4: Exercise emergency `off`, then deploy `active`**

Prove off blocks GitHub, restore active, and verify health. In real shadow/canary traffic, record available cold/warm Worker CPU observations and require zero CPU-limit errors (`exceededCpu`/1102). Do not claim a percentile or maximum the account telemetry cannot evidence. If the free-plan CPU budget is exceeded or cannot support the validated flow reliably, do not ship the client; optimize or change hosting.

- [ ] **Step 5: Bind the public endpoint and rerun client tests**

Set the exact HTTPS URL, ensure no other host is accepted, and run transport/Settings/consent/export/import tests.

- [ ] **Step 6: Run final local verification once**

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest -q
powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1
cargo test --manifest-path migration_core\Cargo.toml
cargo build --manifest-path migration_core\Cargo.toml --release
Push-Location services\issue-relay
npm test -- --run
npm run typecheck
npm audit --omit=dev
npx wrangler deploy --dry-run
Pop-Location
pyinstaller tunnel-manager.spec
git diff --check
```

Expected: all suites pass, the frozen UI smoke succeeds, no secret scan hit, and no live mode/issue claim exceeds recorded evidence.

- [ ] **Step 7: Request final security/code review**

Run independent client privacy, Worker auth/abuse, UI consent, and release/package reviews. Resolve every P0/P1 and rerun affected gates.

- [ ] **Step 8: Update TF-STATUS-092 and publish through protected PR**

Use `fixed_pending_full_verify` until hosted Python, Rust, macOS arm64/x86_64, and terminal version gate pass. Close only after the protected merge and live service evidence remain consistent.

Commit: `feat: enable anonymous error reporting relay`.
