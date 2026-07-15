# DB Core Detached Reap Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make DB Core cancellation bounded for callers without allowing caller timeout to cancel owner-loop process reaping, and prove real asyncio subprocess transports are settled.

**Architecture:** Cancellation creates or reuses one strongly held owner-loop reap task. A thread-safe handoff may time out, but it never cancels that task. One shielded `process.wait()` task spans terminate, liveness recheck, optional kill, and final settlement. Integration finalization settles real asyncio subprocess transports on their owning loop before owner shutdown; stopped-owner fallback may prove PID termination but must report transport settlement as unproven.

**Tech Stack:** Python 3.9+, asyncio, concurrent.futures, PyQt application core, pytest, Win32/POSIX process APIs.

## Global Constraints

- Caller return time is bounded by the requested `timeout_seconds`; scheduling tolerance belongs only in tests.
- Caller timeout or abandonment must never cancel the owner reap task.
- Owner cleanup uses `min(active_deadline, cancel_deadline, cleanup_cap)` and keeps typed stage/PID/task diagnostics.
- A live generation transitions through `POISONED -> REAPING -> CLOSED` only after process wait, stderr, and task settlement are proved.
- Use exactly one owner-loop `process.wait()` task per cleanup attempt and shield it from graceful timeout cancellation.
- Recheck process liveness immediately before kill; never kill a child already proved terminal.
- OS PID termination without asyncio transport settlement is not cleanup success.
- Preserve mutation uncertainty and never retry or resend after cancellation linearizes.

---

### Task 1: Detached Owner Reap And Transport Settlement

**Files:**
- Modify: `src/core/db_core_client.py`
- Modify: `tests/test_db_core_process_contract.py`
- Modify: `tests/test_db_core_process_integration.py`
- Append: `.superpowers/sdd/tf098-task-4-report.md`

**Interfaces:**
- Consumes: `_cancel_active_on_owner(deadline_at)`, `_terminate_process_on_owner(deadline_at)`, `cancel_active_request(timeout_seconds=...)`, `DbCoreServiceError` residual payloads.
- Produces: one strongly held owner reap task/ticket that caller timeout cannot cancel; one shielded process wait task reused through terminate and kill; owner-loop transport settlement in real-child finalization.

- [x] **Step 1: Add RED caller-abandonment regression**

Add `test_cancel_caller_timeout_does_not_cancel_owner_reap`. Block process wait past the caller deadline, assert `cancel_active_request` returns a typed `db_core_residual_process` by the caller bound with `reap_continues=true`, assert the owner reap task is not cancelled, release the wait, and assert the generation reaches `CLOSED` with no child/task/thread residual.

- [x] **Step 2: Run the caller regression RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py::test_cancel_caller_timeout_does_not_cancel_owner_reap -q`

Expected: FAIL because caller timeout currently cancels the thread-safe future and owner coroutine.

- [x] **Step 3: Implement detached owner reap**

Keep the owner reap task in a strong field until its done callback retrieves its result. The thread-safe handoff awaits it through `asyncio.shield`. When caller time expires, do not call `future.cancel()`; raise a typed residual containing `stage=cancel_handoff`, generation state, pending task diagnostics, and whether reap continues. Shutdown must reuse/await the same reap task before stopping the loop.

- [x] **Step 4: Add RED single-wait and liveness regressions**

Add `test_process_reap_reuses_one_shielded_wait_task` and `test_process_exit_at_grace_boundary_skips_kill`. Use a native-shaped async fake with one `wait()` coroutine that survives graceful timeout. Assert `wait()` is called once, kill occurs only after a fresh live check, and a child exiting at the boundary is not killed.

- [x] **Step 5: Run process regressions RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py -k "single_wait or grace_boundary" -q`

Expected: FAIL because cleanup currently cancels/recreates wait tasks and sets kill without a final liveness check.

- [x] **Step 6: Implement one shielded wait task**

Create `process_wait_task` once on the owner loop. Give it one settlement turn, await `asyncio.shield(process_wait_task)` for graceful settlement, recheck liveness after timeout or terminate failure, kill only while still live, and await the same shielded task through the final owner deadline. A timeout leaves the task tracked and produces typed residual diagnostics; it does not manufacture cleanup success.

- [x] **Step 7: Add RED real asyncio transport finalizer regression**

Add `test_finalizer_settles_tracked_asyncio_process_on_owner_loop`. Start a real helper through the DB Core owner loop, clear `client._process` to exercise independent tracking, and assert finalization schedules kill/wait on the owner loop, settles the process transport, then shuts down the owner with no PID/task/thread residual. The existing stopped-owner fallback must prove PID termination and report transport settlement as unavailable rather than silently passing.

- [x] **Step 8: Run integration regression RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_integration.py -k "settles_tracked_asyncio_process or stopped_owner" -q`

Expected: FAIL because the helper currently waits only for `subprocess.Popen` and uses PID proof for asyncio processes.

- [x] **Step 9: Implement owner-loop transport finalization**

Before owner shutdown, submit one bounded coroutine to the live owner loop for every tracked asyncio process: recheck liveness, kill only if live, and await its actual `wait()` transport. Direct `Popen` children retain kill/wait handling. If the owner is already stopped, perform bounded OS-level PID cleanup but append a typed `transport_unsettled_owner_stopped` failure; do not treat PID termination as full settlement.

- [x] **Step 10: Run focused GREEN and broad verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py -q
.venv\Scripts\python.exe -m pytest tests\test_db_core_process_integration.py -q
.venv\Scripts\python.exe -m pytest tests\test_db_core_service.py tests\test_rust_dump_exporter.py tests\test_db_core_process_contract.py tests\test_db_core_process_integration.py tests\test_ci_workflows.py -q
cargo build --manifest-path migration_core\Cargo.toml --release
.venv\Scripts\python.exe -m compileall -q src/core/db_core_client.py tests/test_db_core_process_contract.py tests/test_db_core_process_integration.py
git diff --check
```

Expected: all commands exit `0`, pytest processes return normally, no tracked child/task/thread remains, and the report records exact RED/GREEN outputs.

- [x] **Step 11: Commit**

```powershell
git add src/core/db_core_client.py tests/test_db_core_process_contract.py tests/test_db_core_process_integration.py docs/superpowers/plans/2026-07-16-db-core-detached-reap-fix.md
git commit -m "Fix DB Core detached reap ownership"
```
