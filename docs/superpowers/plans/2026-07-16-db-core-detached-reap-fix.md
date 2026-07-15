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

---

### Task 2: Generation Barrier And Pipe EOF Proof

**Files:**
- Modify: `src/core/db_core_client.py`
- Modify: `tests/test_db_core_process_contract.py`
- Modify: `tests/test_db_core_process_integration.py`
- Append: `.superpowers/sdd/tf098-task-4-report.md`

**Interfaces:**
- Consumes: the Task 1 strong reap task, request serialization lock, active/cancel absolute deadlines, one shielded process wait task.
- Produces: a published generation barrier that precedes active-task cancellation; exact owner cleanup deadline without handoff subtraction; process-plus-pipe settlement proof before `CLOSED`.

- [x] **Step 1: Add RED cancel-plus-queued-request race**

Add `test_cancel_publishes_generation_barrier_before_queued_request_runs`. Hold generation 1 in a cancellable stdout read, queue a second request, linearize cancellation, and release the first task's request lock before reap completes. Assert the queued request performs zero wire writes and neither starts nor clears generation 2 until the generation-1 reap barrier completes. After the barrier closes generation 1, the queued request may create generation 2 and must succeed without sharing generation-1 process/wait/stderr state.

- [x] **Step 2: Run the barrier regression RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py::test_cancel_publishes_generation_barrier_before_queued_request_runs -q`

Expected: FAIL because the active task releases the request lock before the detached reap is published to queued requests.

- [x] **Step 3: Publish and enforce the generation barrier**

On the owner loop, create and store the generation reap task before cancelling the active request. Treat that task as the generation barrier. Every request, including requests already queued on the request lock, must check the barrier immediately after acquiring the lock and before process creation, generation transition, stdin access, or wire write. Await it through `asyncio.shield` within the request deadline. If the request deadline expires first, fail `NOT_STARTED` with `db_core_reap_in_progress`, the blocked generation, barrier task diagnostics, and zero wire writes. Shutdown joins the same barrier before loop stop.

- [x] **Step 4: Add RED exact cancel-deadline regression**

Add `test_cancel_owner_uses_exact_minimum_deadline_without_handoff_subtraction`. Provide active deadline `A`, cancel deadline `C`, and cleanup cap `K`; assert the owner receives exactly `min(A, C, K)` and not `C - min(2.0, 0.2 * timeout)`. Include a child that exits after the former 80% cutoff but before `C` and assert no kill or residual occurs.

- [x] **Step 5: Run the exact-deadline regression RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py::test_cancel_owner_uses_exact_minimum_deadline_without_handoff_subtraction -q`

Expected: FAIL because caller handoff reserve is currently subtracted from the owner cleanup deadline.

- [x] **Step 6: Remove owner deadline subtraction**

Pass the exact caller cancel deadline to the owner reap. Inside the owner, compute only `min(active_deadline, cancel_deadline, monotonic() + DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)`. The caller waits until its same absolute deadline; if result delivery loses the boundary race, it raises typed `cancel_handoff` without cancelling the reap task. The owner deadline must never be shortened to reserve caller handoff time.

- [x] **Step 7: Add RED terminal-returncode pending-pipe regression**

Add `test_terminal_returncode_does_not_close_generation_before_pipe_eof`. Use a native-shaped process with `returncode` already set and `wait()` already complete while stdout EOF and stderr completion are held pending. Assert generation remains `REAPING`, process/stdout/stderr references remain tracked, and cleanup raises a typed `stdout_drain` or `stderr_drain` residual at the exact stage rather than transitioning to `CLOSED`.

- [x] **Step 8: Add RED real Proactor settlement regression**

Add a Windows-capable real-child test that lets the process return before pipe callbacks settle, then runs owner cleanup with strict `ResourceWarning` and unraisable-exception capture. Assert owner-loop cleanup drains stdout to EOF, awaits the stderr task to EOF, closes/waits stdin, settles the existing process wait task, produces no unclosed transport warning, and leaves zero task/thread/PID residual. Keep stopped-owner fallback as explicit `transport_unsettled_owner_stopped`; do not use it as proof of full transport settlement.

- [x] **Step 9: Run pipe regressions RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py::test_terminal_returncode_does_not_close_generation_before_pipe_eof -q
.venv\Scripts\python.exe -m pytest tests\test_db_core_process_integration.py -k "proactor and pipe" -W error::ResourceWarning -q
```

Expected: FAIL because `Process.wait()`/`returncode` currently proves only process termination and terminal processes are skipped by integration finalization.

- [x] **Step 10: Prove pipe settlement before CLOSED**

After the one shielded process wait task completes, owner cleanup must discard/drain remaining stdout to EOF within the cleanup deadline, await the existing stderr reader task through EOF, and complete stdin `wait_closed` when available. Do not clear process, pipe, stderr, wait-task, or generation references until all applicable proofs succeed. Emit typed `stdin_close`, `stdout_drain`, `stderr_drain`, or `transport_unsettled_owner_stopped` residuals with PID/generation/task diagnostics. Integration finalization must not skip an asyncio process merely because `returncode` is set; it must settle its wait and pipe readers on the live owner loop.

- [x] **Step 11: Run focused and broad GREEN sequentially**

Run each command only after the previous command exits and confirm zero worktree-owned Python processes between commands:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_db_core_process_contract.py -q
.venv\Scripts\python.exe -m pytest tests\test_db_core_process_integration.py -W error::ResourceWarning -q
.venv\Scripts\python.exe -m pytest tests\test_db_core_service.py tests\test_rust_dump_exporter.py tests\test_db_core_process_contract.py tests\test_db_core_process_integration.py tests\test_ci_workflows.py -q
cargo build --manifest-path migration_core\Cargo.toml --release
.venv\Scripts\python.exe -m compileall -q src/core/db_core_client.py tests/test_db_core_process_contract.py tests/test_db_core_process_integration.py
git diff --check
```

Expected: every command exits `0`; no warning, unraisable exception, child, task, or owner thread remains; the report records exact RED/GREEN evidence.

- [x] **Step 12: Commit**

```powershell
git add src/core/db_core_client.py tests/test_db_core_process_contract.py tests/test_db_core_process_integration.py docs/superpowers/plans/2026-07-16-db-core-detached-reap-fix.md
git commit -m "Fence DB Core reaping and pipe settlement"
```
