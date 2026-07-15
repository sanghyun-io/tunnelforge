# TF-STATUS-098 Bounded DB Core Process Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every Python-to-Rust DB Core request one portable deadline, one generation-safe asyncio subprocess owner, typed definite/failed/indeterminate outcomes, strict wire correlation, bounded cancellation/reaping, and no hidden replay so TF-STATUS-098 can close before One-Click Phase B.

**Architecture:** One dedicated owner thread runs one asyncio event loop per client; callers use `run_coroutine_threadsafe`, while only that loop owns subprocess/I/O/state/cancel/reap. It uses `create_subprocess_exec`, `wait_for`, one absolute monotonic deadline, Proactor on Windows and Selector on POSIX. Real children are gated on Windows, Ubuntu Python 3.9, macOS arm64, and macOS x86_64.

**Tech Stack:** Python 3.9+, `asyncio`, `threading`, `concurrent.futures`, dataclasses/enums, pytest, Rust/Cargo, serde/serde_json, JSONL protocol v1.

## Global Constraints

- Rust `tunnelforge-core` remains the only DB operation owner. Do not add Python DB drivers, external dump tools, request replay, connection reopen, or DB mutation fallback.
- Create the absolute request deadline before `run_coroutine_threadsafe`; loop scheduling, serialization lock wait, spawn, hello, write, `drain`, read, validation, cancellation, terminate/kill, task drain, and reap consume that same deadline.
- Reserve `min(2.0 seconds, 20% of timeout_seconds)` inside that deadline: derive `cleanup_start_at = deadline_at - cleanup_reserve`, stop normal lock/spawn/hello/write/read work there, and spend only the remaining interval through `deadline_at` on cancel/reap. This is one caller-visible request deadline, not a reset or second timeout. Use `DEFAULT_REQUEST_TIMEOUT_SECONDS = 3600.0` and `DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0`; reject non-finite/non-positive values before scheduling.
- Every blocking await is wrapped by `asyncio.wait_for(awaitable, max(0.0, cutoff_at - time.monotonic()))`, where normal work uses `cleanup_start_at` and cleanup uses `deadline_at`; progress never moves either cutoff.
- Only the owner loop mutates `_ProcessGeneration`, subprocess handles/streams, active request, task sets, and lifecycle state. Caller threads may only submit/cancel futures and join the owner after loop shutdown completes.
- The serialized active request coroutine is the sole stdout reader for its generation; there is no speculative/background stdout reader and no second consumer that can steal or discard an event.
- Keep the owner thread non-daemon only because every GUI/startup/CLI-owned path has explicit bounded shutdown and hosted closure gates prove exit; `atexit` is fallback, not lifecycle ownership.
- Required process capabilities equal exactly `request.deadline`, `request.strict_id`, `process.generation`, and `mutation.outcome_indeterminate`; reject each missing member and any extra/non-string member before ACTIVE.
- `MAX_JSONL_FRAME_BYTES = 1_048_576` counts UTF-8 bytes including newline. Pass `limit=MAX_JSONL_FRAME_BYTES` to `asyncio.create_subprocess_exec`; reject oversized outbound frames before write and oversized inbound frames as protocol failure with poison/reap.
- The same frame constant exists in Rust/Python and hello advertises it. Rust recursively chunks collections and strings; string chunks split only at valid UTF-8 code-point boundaries, and Python reconstructs every nested value byte-for-byte/code-point-for-code-point without truncation.
- Serialization/encoding/emit failure before a side effect may produce a bounded structured `FAILED` terminal. After mutation side effect starts, any frame encode/emit failure is `OUTCOME_INDETERMINATE`, poisons/reaps the generation, emits no later stdout event, and retries zero times. Any protocol contamination always poisons/reaps, including read-only requests.
- Generation states are exactly `CREATING`, `ACTIVE`, `POISONED`, `REAPING`, and `CLOSED`; transitions are `CREATING -> ACTIVE|POISONED`, `ACTIVE -> POISONED|REAPING`, `POISONED -> REAPING`, and `REAPING -> CLOSED`.
- Every request carries its generation and request ID. Recheck generation identity plus `ACTIVE` immediately before `stdin.write()` and again immediately before `await stdin.drain()`.
- Cancellation is linearized on the owner loop. Cleanup uses the earlier of the active request deadline and cancel/shutdown deadline.
- A mutation becomes `OUTCOME_INDETERMINATE` when `stdin.write()` is entered and remains so until an exact validated terminal event. Mutation timeout/process loss/cancel is never retried.
- Read-only retry count is also zero. Any future read retry policy is separate, explicit, read-only, and cannot share a generic retry loop with mutations.
- If native spawn cancellation, terminate/kill, final `wait()`, task drain, loop stop, or owner join leaves a possible child/task/thread residual, return structured residual diagnostics, record the risk, and keep TF-STATUS-098 open.
- Preserve current dict consumers with `request_result()` -> `request_payload()` -> legacy `request()`; do not make `DbCoreRequestResult` dict-like.
- Keep One-Click non-dry-run disabled. Phase B cannot start until implementation/status commits land, every required gate passes, the dependency checkpoint is updated to the landed four-outcome API, and no residual process/thread risk remains.
- Other workers may be changing One-Click/Rust protocol files. Preserve their changes and stage only reviewed TF-098 hunks; never revert or indiscriminately stage shared files.

---

## Current Findings and Decisions

- `src/core/db_core_client.py:84-219` uses a shared `threading.Lock`, synchronous `Popen`, synchronous write/flush/readline, no hello negotiation/generation, ID mismatch discard, message-only errors, and unbounded shutdown without wait/kill/reap.
- `src/ui/workers/rust_dump_worker.py:33-51` reaches into `client._process` to terminate an owned dump child, bypassing lifecycle serialization.
- `src/exporters/rust_dump_exporter.py:99-118` constructs a temporary `DbCoreFacade` in `RustDumpChecker.check_installation()` and never shuts it down.
- `src/core/db_core_facade.py:45-53` exposes raw connection ID strings. `src/core/db_core_dbapi_shim.py:214-306` stores them in connections/cursors, so a restarted generation can reuse the same string and an old cursor can address the wrong generation.
- `src/core/cross_engine_migration.py:141-173` coerces helper fields and remains unchanged; add a strict DB service parser in `db_core_client.py`.
- `migration_core/src/protocol.rs` has separate free/stateful/alias/streaming/line paths; outer normalization must cover all of them. `service.hello` lacks process contract metadata and most error events lack stable codes.
- `migration_core/src/protocol.rs:171-180` routes `migration.cleanup`, but `service_hello():248-282` does not advertise it; router/capability parity is currently unenforced.
- `main.py:230-297` connects window/single-instance shutdown only and calls `sys.exit(app.exec())`; shared DB Core shutdown depends on `atexit`, so normal GUI and startup-failure ownership is not explicit.
- A valid exact `result` with `success:false` is a `DEFINITE` business result for either request kind. It is not transport `FAILED`; facade methods may return it or raise a business error whose outcome remains `DEFINITE`.
- Python generation is a monotonically increasing client-local integer. A fresh process never reopens or replays old Rust connections.

## File and Ownership Map

- Modify `src/core/db_core_client.py`: public types, owner thread/loop, async subprocess state machine, strict parser, deadline helpers, typed sync wrappers, cancel/shutdown/reap, residual diagnostics.
- Modify `src/core/db_core_facade.py`: explicit request kinds, `DbCoreConnectionHandle`, stale-generation rejection, definite business failure handling.
- Modify `src/core/db_core_dbapi_shim.py`: generation-bound connection/cursor use and complete structured local errors.
- Modify `src/core/db_core_service.py`: re-export new public types.
- Modify `src/core/postgres_connector.py`: consume `DbCoreConnectionHandle` from `open_connection()`.
- Modify `src/exporters/rust_dump_exporter.py`: checker `finally` lifecycle, explicit request kinds, dict compatibility.
- Modify `src/ui/workers/rust_dump_worker.py`: public bounded `cancel_active_request()` instead of `_process` access.
- Modify `main.py` in the same Task 2 commit that introduces the non-daemon owner: bounded shared-facade shutdown on `aboutToQuit` and every startup/exception/finally path.
- Modify `scripts/capture-oneclick-real-execution-evidence.py`: explicit mutation wrapper; no retry.
- Modify `migration_core/src/protocol.rs`: hello contract and one outer structured-error wrapper across stateful/free/alias/streaming/line paths.
- Modify `migration_core/src/main.rs`: keep parsed input on stateful `CoreService`; normalize only malformed/read failures.
- Modify `tests/test_db_core_service.py`: type/wrapper/facade/error-constructor compatibility.
- Create `tests/test_db_core_process_contract.py`: deterministic fake async process/stream barriers and state/deadline/cancel/recovery tests.
- Create `tests/test_db_core_process_integration.py`: real Rust/fault-helper child tests and platform loop assertions.
- Modify `tests/test_db_dialogs.py`: owned-only dump cancel via public API.
- Modify `tests/test_rust_dump_exporter.py`: checker success/error/timeout lifecycle and structured errors.
- Modify `tests/test_db_connector.py`: generation-bound connector compatibility.
- Modify `tests/test_main_window_error_reporting_consent.py`: normal GUI exit, startup failure, and residual shutdown propagation.
- Modify `tests/conftest.py`: tracked client/shared-facade teardown with bounded stop/join and no surviving owner fixture.
- Modify `tests/test_ci_workflows.py`: four-platform native jobs and exact terminal `version-gate` needs/result assertions.
- Create `tests/helpers/db_core_process_helper.py`: real child stall/write-block/signal modes.
- Modify `migration_core/tests/jsonl_cli.rs`: exact binary hello/error/two-request tests.
- Modify `.github/workflows/version-gate.yml`: native integration on Windows, macOS arm64/x86_64, and Ubuntu Python 3.9; aggregate all in terminal `version-gate`.
- Modify `docs/superpowers/plans/2026-07-15-oneclick-plan-approval.md:91-114` only in Task 6, after the API lands, to replace the preliminary two-outcome checkpoint with the exact four-outcome contract.
- Modify `docs/current_status.md` only after fresh focused/full evidence; do not close TF-STATUS-098 early.

## Exact Public and Internal Contracts

```python
class DbCoreRequestKind(str, Enum):
    READ_ONLY = "read_only"
    MUTATION = "mutation"

class DbCoreOutcome(str, Enum):
    DEFINITE = "definite"
    NOT_STARTED = "not_started"
    FAILED = "failed"
    OUTCOME_INDETERMINATE = "outcome_indeterminate"

class DbCoreGenerationState(str, Enum):
    CREATING = "creating"; ACTIVE = "active"; POISONED = "poisoned"
    REAPING = "reaping"; CLOSED = "closed"

MAX_JSONL_FRAME_BYTES = 1_048_576
DB_CORE_STDIN_HIGH_WATER_BYTES = 65_536
REQUIRED_PROCESS_CAPABILITIES = frozenset({
    "request.deadline", "request.strict_id", "process.generation",
    "mutation.outcome_indeterminate",
})

@dataclass(frozen=True)
class DbCoreRequestResult:
    request_kind: DbCoreRequestKind
    outcome: DbCoreOutcome
    request_id: str
    process_generation: int
    message: str
    rust_code: Optional[str]
    payload: Mapping[str, Any]

class DbCoreCallbackError(RuntimeError):
    request_result: Optional[DbCoreRequestResult]
    request_kind: DbCoreRequestKind; outcome: DbCoreOutcome; cause: BaseException

@dataclass(frozen=True)
class DbCoreConnectionHandle:
    connection_id: str
    process_generation: int

class DbCoreServiceError(RuntimeError):
    # DbCoreServiceError(message, *, code, request_kind, outcome, request_id,
    #                    process_generation, rust_code, payload)
    code: str; message: str; request_kind: DbCoreRequestKind
    outcome: DbCoreOutcome; request_id: str; process_generation: int
    rust_code: Optional[str]; payload: Mapping[str, Any]

DbCoreServiceClient(executable=None, *, process_argv=None, process_factory=None,
                    monotonic=time.monotonic, loop_factory=None,
                    phase_observer=None)  # deterministic test seam
request_result(command, payload=None, *, request_kind, request_id=None,
               on_event=None, timeout_seconds=None,
               required_generation=None) -> DbCoreRequestResult
request_payload(command, payload=None, *, request_kind=None, request_id=None,
                on_event=None, timeout_seconds=None,
                required_generation=None) -> Dict[str, Any]
request(command, payload=None, request_id=None, on_event=None, *,
        request_kind=None, timeout_seconds=None,
        required_generation=None) -> Dict[str, Any]
cancel_active_request(*, timeout_seconds=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> bool
shutdown(*, timeout_seconds=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None
shutdown_shared_db_core_facade(*, timeout_seconds=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None
```

`request_result` requires a kind. Compatibility wrappers infer read-only only for `service.hello`, `connection.test`, `schema.list`, `schema.inspect`, `schema.diff`, `migration.plan`, `migration.verify`, and `oneclick.derive_charset_contracts`; every unknown, generic SQL, connection open/close, dump, import, migration run/resume/cleanup, One-Click run/apply, cancel, and shutdown command is mutation/stateful.

Internal owner signatures are `_AsyncDbCoreOwner.request(_GenerationRequest)`, `cancel_request(cancel_token, deadline_at)`, `cancel_active(deadline_at)`, `shutdown(deadline_at)`, `_spawn_generation(deadline_at)`, `_poison_and_reap(generation, deadline_at, cause)`, and `_await_before(awaitable, deadline_at)`. `_GenerationRequest` stores cancel token, assigned/required generation, ID/kind/deadline, `write_started`, and validated-event queue. The sync wrapper drains that queue and invokes `on_event` on the caller thread; callbacks never run on the owner loop.

Allowed wire events add internal `payload_chunk`. Each carries ID/command/logical event, `node_id`, `parent_node_id`, `slot_index`, sequence/final, `value_kind` (`list`, `object`, `utf8_string`, or atomic), and bounded items/text. Large object keys are string child nodes, never copied into a raw path. Strings split between UTF-8 code points; containers recurse. `_PayloadAssembler` exposes only the exact logical event. Missing/duplicate/out-of-order/conflicting-node/invalid-UTF-8 chunks poison/reap.

Inventory all variable-size producers: non-streaming/streaming query results, schema/list/diff, inspect/preflight/readiness/guide/plan/verify/resume, dump/import/migrate/One-Click progress/analysis/report/issues/warnings/validation, and terminal payloads. Chunk nested collections and arbitrarily large strings. Only genuinely unserializable values or encoder/writer failure use the fallible-emitter failure contract; never truncate or partially emit a logical event.

| Terminal/failure | Read-only | Mutation | Generation |
| --- | --- | --- | --- |
| invalid timeout, owner unavailable, queued-lock/spawn/hello/capability failure before write | `NOT_STARTED` | `NOT_STARTED` | creating/current generation poisoned/reaped when present |
| request-specific cancel while queued before lock/write | `NOT_STARTED` | `NOT_STARTED` | active request/generation untouched |
| outbound JSONL exceeds 1,048,576 bytes before write | `NOT_STARTED` | `NOT_STARTED` | current generation untouched |
| exact `result`, including `success:false` | `DEFINITE` payload | `DEFINITE` payload | remains active |
| exact structured Rust `error` | `FAILED` | `FAILED` | remains active |
| callback exception before terminal validation | `FAILED` | `OUTCOME_INDETERMINATE` after write | poisoned/reaped |
| callback exception after terminal validation | definite result carried by `DbCoreCallbackError` | definite result carried by `DbCoreCallbackError` | remains active |
| write/drain failure, stalled read, malformed/wrong-ID event, child loss, cancel after write | `FAILED` | `OUTCOME_INDETERMINATE` | poisoned/reaped |
| inbound JSONL exceeds 1,048,576 bytes | `FAILED` | `OUTCOME_INDETERMINATE` after write | poisoned/reaped |
| clean bounded encode error before side effect | `FAILED` | `FAILED` | may remain active after validated terminal |
| encode/emit failure after mutation side effect | n/a | `OUTCOME_INDETERMINATE` | poisoned/reaped; no retry or later frame |
| stale `DbCoreConnectionHandle` before wire | `NOT_STARTED` | `NOT_STARTED` | current generation untouched |

Required Python codes: `db_core_capability_missing`, `db_core_protocol_mismatch`, `db_core_request_id_mismatch`, `db_core_timeout`, `db_core_process_died`, `db_core_start_failed`, `db_core_write_failed`, `db_core_stale_connection`, `db_core_business_failure`, `db_core_callback_failed`, `db_core_cleanup_failed`, and `db_core_residual_process`. Matching Rust `code` is copied only to `rust_code`; it is never parsed from `message`.

---

### Task 1: RED/GREEN Complete Rust Hello and Error Envelope

**Files:**
- Modify: `migration_core/src/protocol.rs`, `migration_core/src/main.rs`
- Modify: `migration_core/tests/jsonl_cli.rs`

**Interfaces:** Produces protocol/process version 1, preserves the existing command `capabilities`, adds exact `process_capabilities`, and emits structured `{event,request_id,code,message}` errors through every entry path.

- [ ] **Step 1: Add RED unit coverage for every outer path**

Add `service_hello_advertises_exact_process_contract`, `service_hello_advertises_exact_max_jsonl_frame_bytes`, command parity/cleanup tests, every outer-path normalization test, and missing/empty/non-string code normalization tests. Assert exact four capabilities, `max_jsonl_frame_bytes=1048576`, and matching ID plus nonempty string code/message.

- [ ] **Step 2: Add RED binary tests**

Add `helper_binary_negotiates_process_contract`, structured unknown/invalid-JSON tests, `stdin_read_failure_uses_protocol_error_envelope`, `helper_binary_preserves_two_request_ids`, and `parsed_lines_dispatch_through_stateful_core_service`. Assert parsed lines still call `CoreService::handle_request_streaming`; only malformed JSON/stdin-read failures use the protocol-error constructor, and no-stdin CLI fallback remains free.

- [ ] **Step 3: Run RED**

Run: `cargo test --manifest-path migration_core\Cargo.toml --lib protocol::tests::service_hello_advertises_exact_process_contract -- --exact`

Run: `cargo test --manifest-path migration_core\Cargo.toml --test jsonl_cli helper_binary_negotiates_process_contract -- --exact`

Run: `cargo test --manifest-path migration_core\Cargo.toml --bin tunnelforge-core tests::parsed_lines_dispatch_through_stateful_core_service -- --exact`

Expected: FAIL because process contract metadata and complete normalization are absent.

- [ ] **Step 4: Implement one outer envelope**

Define protocol/process version 1, exact capabilities, Rust `MAX_JSONL_FRAME_BYTES=1_048_576`, matching hello field, and public command constant including `migration.cleanup`. Normalize every outer path and malformed code. Keep parsed `main.rs` requests on `CoreService`; malformed/read failures alone use `protocol_error_event`.

- [ ] **Step 5: Run GREEN and commit**

Run: `cargo test --manifest-path migration_core\Cargo.toml --lib protocol::tests`

Run: `cargo test --manifest-path migration_core\Cargo.toml --test jsonl_cli`

Run: `cargo test --manifest-path migration_core\Cargo.toml --bin tunnelforge-core`

Expected: PASS for every stateful/free/alias/streaming/line/binary path.

```powershell
git add -p migration_core/src/protocol.rs migration_core/src/main.rs migration_core/tests/jsonl_cli.rs
git commit -m "Add DB Core process protocol envelope"
```

### Task 2: RED/GREEN Atomic Typed Owner and Explicit Application/Test Lifecycle

**Files:**
- Modify: `src/core/db_core_client.py`, `src/core/db_core_service.py`, `src/core/db_core_facade.py`, `main.py`
- Modify: `tests/conftest.py`, `tests/test_db_core_service.py`, `tests/test_main_window_error_reporting_consent.py`
- Create: `tests/test_db_core_process_contract.py`

**Interfaces:** Atomically produces typed wrappers, the first non-daemon owner, bounded stop/join, shared-facade shutdown, GUI/startup/finally ownership, and leak-free test fixtures; no commit may contain the owner without all lifecycle paths.

- [ ] **Step 1: Add RED type/wrapper/business-result tests**

Add typed result/wrapper/re-export tests plus `test_rust_error_rejects_missing_code`, `test_rust_error_rejects_empty_code`, and `test_rust_error_rejects_non_string_code`. Each malformed envelope is `db_core_protocol_mismatch`, never parsed from message.

- [ ] **Step 2: Add RED owner bootstrap tests**

Add Proactor/selector/deadline/thread-ownership tests and `test_shutdown_stops_loop_and_joins_non_daemon_owner_boundedly`, `test_client_fixture_teardown_joins_every_owner`, `test_shared_shutdown_is_bounded_and_idempotent`, `test_main_normal_exit_runs_about_to_quit_and_finally_shutdown`, `test_main_secondary_instance_return_runs_finally`, `test_main_startup_failure_before_qapplication_runs_finally`, `test_main_window_startup_failure_runs_finally`, `test_main_event_loop_exception_runs_finally`, and `test_main_surfaces_residual_owner_join_failure`.

- [ ] **Step 3: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_service.py tests\test_db_core_process_contract.py tests\test_main_window_error_reporting_consent.py -k "outcome or wrapper or code or owner or deadline or shutdown or startup or event_loop or fixture" -q`

Expected: FAIL because types and asyncio owner do not exist.

- [ ] **Step 4: Implement typed sync-to-async boundary**

Implement types/parser/wrappers and one named non-daemon Proactor/selector owner. `shutdown(timeout_seconds)` awaits tasks, stops loop, and joins within the bound; join failure is residual. Make shared shutdown locked/idempotent, clear only after success. In `main()`, enter `try/finally` before QApplication/config/window startup, connect the same bounded function to `aboutToQuit`, and `return app.exec()`; primary, secondary, startup, window, and event-loop exceptions all reach finally, while `atexit` is fallback. Add tracked client/shared fixtures, migrate owner-starting tests, and assert no owner survives teardown.

- [ ] **Step 5: Run GREEN and commit**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_service.py tests\test_db_core_process_contract.py tests\test_main_window_error_reporting_consent.py -k "outcome or wrapper or code or owner or deadline or shutdown or startup or event_loop or fixture" -q`

Expected: PASS; every test-created owner and every main exit path reaches bounded stop/join.

```powershell
git add main.py src/core/db_core_client.py src/core/db_core_service.py src/core/db_core_facade.py tests/conftest.py tests/test_db_core_service.py tests/test_db_core_process_contract.py tests/test_main_window_error_reporting_consent.py
git commit -m "Add bounded DB Core owner lifecycle"
```

### Task 3: RED/GREEN State Machine, Deadline, and Generation Correlation

**Files:**
- Modify: `src/core/db_core_client.py`, `migration_core/src/protocol.rs`, `migration_core/src/main.rs`
- Modify: `migration_core/src/dump.rs`, `migration_core/src/import.rs`, `migration_core/src/migrate.rs`, `migration_core/src/oneclick.rs`
- Modify: `tests/test_db_core_process_contract.py`, `migration_core/tests/jsonl_cli.rs`; add producer/side-effect unit tests in each modified Rust module's existing `#[cfg(test)]` module.

**Interfaces:** Atomically adds spawn/generation/reap plus Rust byte-aware frame encoding and Python logical-event assembly; producer and consumer wire changes land in one commit.

- [ ] **Step 1: Build deterministic async fakes**

Define `FakeClock`, async process/stdin/stdout factories, and barriers for spawn, hello, before-write, drain-entered/pending, read, wait, and stderr exit. Record UTF-8 frame byte lengths, subprocess `limit`, transitions, writes, terminate/kill/wait, tasks, and loop thread ID.

- [ ] **Step 2: Add RED state/deadline tests**

Add state/capability tests plus `test_oversized_intermediate_utf8_scalar_reassembles_exactly`, `test_nested_large_key_and_multibyte_value_reassemble_exactly`, near-limit query/stream/schema/plan compatibility, `test_mutation_terminal_scalar_encode_failure_after_side_effect_is_indeterminate`, `test_emit_failure_produces_no_followup_frames`, `test_next_request_after_emit_failure_uses_fresh_generation`, malformed node/chunk, and malicious raw over-limit poison/reap. Rust tests inventory producers, frame sizes, UTF-8 boundaries, and fallible emitter break propagation.

- [ ] **Step 3: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py -k "spawn or hello or generation or queued or capability or write or drain or stalled or mismatch or malformed or death or callback or frame or jsonl or code" -q`

Run: `cargo test --manifest-path migration_core\Cargo.toml --lib protocol::tests -- frame`

Run: `cargo test --manifest-path migration_core\Cargo.toml --test jsonl_cli -- frame`

Expected: FAIL because state transitions and async subprocess handling are absent.

- [ ] **Step 4: Implement minimal stateful request coroutine**

Route output through byte-aware `encode_protocol_frames`, recursively chunking lists/objects and UTF-8 strings at code-point boundaries. Change `CoreService::handle_request_streaming`, free/alias/dump/import/migrate/One-Click streaming producers, and `main.rs` emitter to `Result<(), ProtocolEmitError>`; every call propagates `?`, a failed emitter is fused, and the request loop breaks so no later event is written. Mark mutation side-effect start before DB/file mutation. Python reassembles exact values; post-side-effect encode/emit loss is indeterminate and poison/reap, while clean pre-side-effect/read-only structured failure is typed `FAILED`. Protocol contamination always poison/reaps.

- [ ] **Step 5: Run GREEN and commit**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py -k "spawn or hello or generation or queued or capability or write or drain or stalled or mismatch or malformed or death or callback or frame or jsonl or code" -q`

Run: `cargo test --manifest-path migration_core\Cargo.toml --lib protocol::tests -- frame`

Run: `cargo test --manifest-path migration_core\Cargo.toml --test jsonl_cli -- frame`

Expected: PASS with no automatic resend and monotonically increasing generations.

```powershell
git add src/core/db_core_client.py tests/test_db_core_process_contract.py migration_core/src/protocol.rs migration_core/src/main.rs migration_core/src/dump.rs migration_core/src/import.rs migration_core/src/migrate.rs migration_core/src/oneclick.rs migration_core/tests/jsonl_cli.rs
git commit -m "Add bounded DB Core frame assembly"
```

### Task 4: RED/GREEN Cancellation, Cleanup, and Real Child Integration

**Files:**
- Modify: `src/core/db_core_client.py`, `tests/test_db_core_process_contract.py`
- Modify: `.github/workflows/version-gate.yml`, `tests/test_ci_workflows.py`
- Create: `tests/test_db_core_process_integration.py`
- Create: `tests/helpers/db_core_process_helper.py`

**Interfaces:** Produces loop-linearized `cancel_active_request`, bounded poison/reap, stderr/task drain, owner stop/join, and explicit residual risk.

- [ ] **Step 1: Add RED cancellation phase matrix**

Add `test_cancel_during_start`, `test_cancel_during_hello`, `test_cancel_while_request_waits_to_write`, `test_cancel_during_stdin_drain`, `test_cancel_during_stdout_read`, and `test_cancel_during_shutdown`. For each, assert cancellation runs on the owner loop, uses `min(active_deadline, cancel_deadline)`, transitions through POISONED/REAPING/CLOSED when a generation exists, and never writes/resends after cancellation linearizes.

- [ ] **Step 2: Add RED cleanup escalation/residual tests**

Cover close-stdin failure, terminate exception, terminate wait timeout then kill, kill exception, final wait timeout, stderr drain cancellation, owner task refusal, loop-stop failure, and owner join timeout. Reap success leaves no child/task/thread; final native refusal raises `db_core_residual_process` with stage/PID/task diagnostics and is an explicit status blocker.

- [ ] **Step 3: Add RED real-helper integration tests**

Use release Rust and real child modes for stall/no-read/signals, near-limit chunks, oversized UTF-8 scalar chunks, malicious raw frame, and post-side-effect terminal encode failure. Assert exact scalar/public reconstruction; encode failure yields no subsequent frame, indeterminate mutation, poison/reap, retry zero, and fresh generation on next request. Keep pending-drain, terminal PID, signal, and zero-residual assertions on every runner.

- [ ] **Step 4: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py -k "cancel or cleanup or residual or shutdown" -q`

Expected: FAIL because loop-linearized cleanup is absent.

- [ ] **Step 5: Implement cancellation and cleanup**

`cancel_active_request` submits `_AsyncDbCoreOwner.cancel_active`; cancellation is linearized on the loop, including while the tracked `create_subprocess_exec` task is pending. The loop marks the matching generation `POISONED`, cancels and awaits the request/spawn task, reaps a child returned during cancellation, closes stdin, calls terminate then kill as needed, awaits `process.wait()` within the earlier request/cancel deadline and bounded cleanup cap, drains or cancels/awaits stderr, and reaches `CLOSED`. If the native spawn await never yields enough process identity to prove reap, surface residual spawn diagnostics. `shutdown` cancels queued/active work, reaps, awaits every tracked task, then stops the loop; caller joins the owner only after the shutdown coroutine confirms task exit. Preserve residual diagnostics rather than claiming cleanup success.

- [ ] **Step 6: Run GREEN and real integration**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py -k "cancel or cleanup or residual or shutdown" -q`

Run: `cargo build --manifest-path migration_core\Cargo.toml --release`

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_integration.py -q`

Run: `.venv\Scripts\python.exe -m pytest tests\test_ci_workflows.py -k "python39_db_core_native or required_version_gate" -q`

Update `.github/workflows/version-gate.yml`: Windows and both macOS matrix runners explicitly run the full native integration after release build. Add Ubuntu job `python39-db-core-native` using Python 3.9, release Rust build, compile/import/focused owner gates, and the full native integration. Add that exact job ID to terminal `version-gate.needs`, expose `${{ needs.python39-db-core-native.result }}`, and require `success`. Add `test_python39_db_core_native_builds_release_and_runs_full_integration`, `test_required_version_gate_needs_python39_db_core_native`, and `test_required_version_gate_checks_python39_db_core_native_result` to `tests/test_ci_workflows.py`.

Ubuntu Python 3.9 commands: `python -m pip install -e ".[dev]"`; `cargo build --manifest-path migration_core/Cargo.toml --release`; `python -m compileall -q main.py src/core/db_core_client.py src/core/db_core_facade.py src/core/db_core_dbapi_shim.py src/core/db_core_service.py tests/test_db_core_process_contract.py`; `python -c "import main; import src.core.db_core_client; import src.core.db_core_service"`; `python -m pytest tests/test_db_core_process_contract.py -k "owner or deadline or callback or queued or frame" -q`; `python -m pytest tests/test_db_core_process_integration.py -q`.

- [ ] **Step 7: Commit**

```powershell
git add src/core/db_core_client.py tests/test_db_core_process_contract.py tests/test_db_core_process_integration.py tests/helpers/db_core_process_helper.py tests/test_ci_workflows.py .github/workflows/version-gate.yml
git commit -m "Bound DB Core cancellation and reaping"
```

### Task 5: RED/GREEN Generation-Bound Consumers and Compatibility

**Files:**
- Modify: `src/core/db_core_facade.py`, `src/core/db_core_dbapi_shim.py`, `src/core/db_core_service.py`, `src/core/postgres_connector.py`
- Modify: `src/exporters/rust_dump_exporter.py`, `src/ui/workers/rust_dump_worker.py`, `scripts/capture-oneclick-real-execution-evidence.py`
- Modify: `tests/test_db_core_service.py`, `tests/test_db_connector.py`, `tests/test_db_dialogs.py`, `tests/test_rust_dump_exporter.py`

**Interfaces:** Produces `DbCoreConnectionHandle(connection_id, process_generation)` and preserves all existing dict/list/tuple consumer shapes.

- [ ] **Step 1: Add RED generation-binding tests**

Add `test_open_connection_returns_generation_handle`, `test_stale_handle_rejected_before_wire`, `test_old_cursor_rejected_when_new_generation_reuses_same_connection_id`, `test_new_cursor_with_reused_id_succeeds`, and connector/PostgreSQL compatibility tests. Raw strings are rejected; stale errors are `NOT_STARTED/db_core_stale_connection` with zero wire writes.

- [ ] **Step 2: Add RED checker/cancel/error migration tests**

For `RustDumpChecker.check_installation`, assert temporary facade shutdown through `_shutdown_owned_facade` on all exits. Test owned-only dump cancel, AST-required error metadata, and positional fixture migration; lifecycle tests remain green from atomic Task 2.

- [ ] **Step 3: Add RED explicit request-kind/compatibility tests**

Spy on `request_result`/`request_payload`: hello/test/schema/verify/derive are read-only; connection open/close, all query, dump/import, migration run, One-Click run/apply, direct evidence mutation, cancel, and shutdown are mutation/stateful. Preserve `.get()`, indexing, callbacks, rowcount/columns, exporter/importer, worker, and dialog return shapes. Assert no consumer retries or reopens after transport failure.

- [ ] **Step 4: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_service.py tests\test_db_connector.py tests\test_db_dialogs.py tests\test_rust_dump_exporter.py -q`

Expected: FAIL on raw IDs, checker leak, private process access, positional errors, and implicit kinds.

- [ ] **Step 5: Implement minimal consumer migration**

Make `open_connection` return `DbCoreConnectionHandle`; `close_connection` and all `execute_on_connection*` methods pass its generation through `required_generation`, while `RustDbConnection`/cursor retain the handle. Apply checker finally, public cancel, structured errors, and `DEFINITE` business handling without changing Task 2 lifecycle ownership.

- [ ] **Step 6: Run focused compatibility GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_service.py tests\test_db_connector.py tests\test_db_dialogs.py tests\test_rust_dump_exporter.py tests\test_main_window_error_reporting_consent.py tests\test_migration_worker.py tests\test_cross_engine_migration_worker.py tests\test_cross_engine_migration_protocol.py tests\test_cross_engine_migration_dialog.py tests\test_oneclick_rust_core_gate.py -q`

Expected: PASS with stale handles rejected before wire and all public shapes preserved.

- [ ] **Step 7: Commit**

```powershell
git add src/core/db_core_facade.py src/core/db_core_dbapi_shim.py src/core/db_core_service.py src/core/postgres_connector.py src/exporters/rust_dump_exporter.py src/ui/workers/rust_dump_worker.py scripts/capture-oneclick-real-execution-evidence.py tests/test_db_core_service.py tests/test_db_connector.py tests/test_db_dialogs.py tests/test_rust_dump_exporter.py
git commit -m "Bind DB Core consumers to process generations"
```

### Task 6: Full Gates, Review, One-Click Checkpoint, and Status

**Files:**
- Verify all Task 1-5 files
- Modify after landed API/evidence: `docs/superpowers/plans/2026-07-15-oneclick-plan-approval.md:91-114`, `docs/current_status.md`

- [ ] **Step 1: Run targeted process and consumer gates**

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py tests\test_db_core_process_integration.py tests\test_db_core_service.py tests\test_db_connector.py tests\test_db_dialogs.py tests\test_rust_dump_exporter.py tests\test_main_window_error_reporting_consent.py tests\test_ci_workflows.py tests\test_migration_worker.py tests\test_cross_engine_migration_worker.py tests\test_cross_engine_migration_protocol.py tests\test_cross_engine_migration_dialog.py tests\test_oneclick_rust_core_gate.py -q
cargo test --manifest-path migration_core\Cargo.toml --lib protocol::tests
cargo test --manifest-path migration_core\Cargo.toml --test jsonl_cli
cargo test --manifest-path migration_core\Cargo.toml --bin tunnelforge-core
```

Expected: lifecycle, exact capabilities, nested collection/string reconstruction, fallible-emitter stop, post-side-effect indeterminate recovery, malicious oversize reap, pending drain, consumers, CI, and real reap pass.

- [ ] **Step 2: Run full gates**

```powershell
.venv\Scripts\python.exe -m pytest -q
cargo test --manifest-path migration_core\Cargo.toml
cargo build --manifest-path migration_core\Cargo.toml --release
git diff --check
```

Expected: full suites/build pass. Do not replace targeted lifecycle assertions with broad source-search claims.

- [ ] **Step 3: Independent review gate**

Invoke review for atomic lifecycle/wire commits, inventory, UTF-8-safe nested string chunks, exact reconstruction, fallible `Result` emitter/fused break propagation, mutation side-effect marker and indeterminate classification, no follow-up frames/retry, read-only clean failure versus contamination poison, malicious oversize, deadlines/state/native residuals, four-platform gate, and consumers. Resolve Critical/High and rerun gates.

- [ ] **Step 4: Commit reviewed implementation fixes before docs**

Stage only TF-098 hunks. Confirm no One-Click Phase B implementation is staged, then commit final fixes with `Fix: complete bounded DB Core process contract`.

- [ ] **Step 5: Explicitly update the One-Click dependency checkpoint**

Edit checkpoint lines 91-114 to name the four outcomes, owner state machine, structured Rust envelope, shared frame cap/chunk assembly/no-truncation contract, generation handles, tests, commit SHA, and residual prohibition. Preserve One-Click request kinds and keep Phase B blocked until checkpoint/status closure commit.

- [ ] **Step 6: Update canonical status only from fresh evidence**

Align tracker/log/order/session. Close only after terminal `version-gate` proves Windows Proactor, macOS arm64, macOS x86_64, and Ubuntu Python 3.9 release-build/full-native PASS, every PID terminal, all GUI/startup/fixture owners joined, and no process/task/thread residual. Any missing/failed needs result or reap/join residual keeps TF098 open and One-Click blocked.

- [ ] **Step 7: Commit documentation gate**

```powershell
git add docs/superpowers/plans/2026-07-15-oneclick-plan-approval.md docs/current_status.md
git commit -m "docs: close bounded DB Core process issue"
```

---

## Self-Review

- [ ] One absolute monotonic deadline begins before coroutine submission and includes queueing, spawn, all I/O, validation, cancel, cleanup, reap, and task exit.
- [ ] The first non-daemon owner commit also contains bounded stop/join, fixture teardown, shared shutdown, `aboutToQuit`, and every startup/exception/finally path; `atexit` is fallback only.
- [ ] Windows Proactor and POSIX selector owner loops use `asyncio.create_subprocess_exec`; only the loop mutates lifecycle state or process I/O.
- [ ] CREATING/ACTIVE/POISONED/REAPING/CLOSED transitions and generation tags are exact; generation/state is rechecked before write and drain.
- [ ] Cancel during start, hello, queued write, drain, read, and shutdown uses the earlier deadline; queued cancellation is request-specific and never interrupts the active request.
- [ ] Cleanup closes stdin, terminate/kill/waits, drains/cancels stderr, awaits tasks, stops loop, then joins owner; residual native failure keeps TF-098 open.
- [ ] Hello requires each of the exact four process capabilities, router capabilities include `migration.cleanup`, and missing/empty/non-string Rust error code is rejected.
- [ ] `MAX_JSONL_FRAME_BYTES=1_048_576` is passed as subprocess limit; >64KiB valid, oversized protocol failure/reap, and real pending-drain cancel are proven.
- [ ] Every variable-size producer chunks nested collections and arbitrarily large strings at UTF-8 boundaries; Python reconstructs exact query/stream/schema/inspect/plan/public semantics without truncation.
- [ ] Rust streaming emitters return/propagate `Result`, stop after first encode/emit failure, and post-side-effect mutation failure is indeterminate, poison/reap, retry zero, with fresh next generation.
- [ ] Rust encoder/Python assembler land atomically; oversized intermediate scalar, no-followup-frame, malformed chunk, and malicious raw frame tests cover compatibility and poison/reap.
- [ ] Every free/stateful/alias/streaming/handle-line Rust error path passes one outer structured envelope; parsed `main.rs` stays stateful.
- [ ] Exact result `success:false` is `DEFINITE` business payload; structured service error is `FAILED`; mutation transport uncertainty is `OUTCOME_INDETERMINATE`.
- [ ] Connection handles bind ID plus generation; stale and old-cursor/reused-ID cases fail before wire without reconnect/retry.
- [ ] `required_generation` is enforced before write/drain; callbacks receive intermediate plus terminal events in order, drain before return, and callback failures preserve the defined pre/post-terminal outcome.
- [ ] Dict consumers, callback order, checker finally lifecycle, owned-only dump cancel, and required error metadata have focused tests.
- [ ] Real children on Windows, both macOS architectures, and Ubuntu Python 3.9 cover timeout/cancel/reap, terminal PID, owner/task exit, and zero residual; terminal `version-gate` requires every result.
- [ ] Targeted tests, full gates, independent review, landed four-outcome One-Click checkpoint, status closure, and no residual all precede Phase B.
