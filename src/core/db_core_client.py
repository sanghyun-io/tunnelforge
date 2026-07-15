"""Sequential JSONL client for the long-lived Rust TunnelForge DB core process."""
import asyncio
import concurrent.futures
import inspect
import json
import math
import queue
import re
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Set, Tuple

from src.core.cross_engine_migration import (
    HelperProtocolError,
    db_core_executable,
)
from src.core.logger import get_logger
from src.core.platform_integration import no_window_creation_flags

logger = get_logger("db_core_service")


class DbCoreRequestKind(str, Enum):
    READ_ONLY = "read_only"
    MUTATION = "mutation"


class DbCoreOutcome(str, Enum):
    DEFINITE = "definite"
    NOT_STARTED = "not_started"
    FAILED = "failed"
    OUTCOME_INDETERMINATE = "outcome_indeterminate"


class DbCoreGenerationState(str, Enum):
    CREATING = "creating"
    ACTIVE = "active"
    POISONED = "poisoned"
    REAPING = "reaping"
    CLOSED = "closed"


MAX_JSONL_FRAME_BYTES = 1_048_576
DB_CORE_STDIN_HIGH_WATER_BYTES = 65_536
MAX_ASSEMBLED_EVENT_BYTES = 64 * 1024 * 1024
MAX_ASSEMBLED_EVENT_CHUNKS = 4_096
MAX_ASSEMBLED_EVENT_NODES = 65_536
MAX_ASSEMBLED_EVENT_DEPTH = 128
DEFAULT_REQUEST_TIMEOUT_SECONDS = 3600.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
REQUIRED_PROCESS_CAPABILITIES = frozenset({
    "request.deadline",
    "request.strict_id",
    "process.generation",
    "mutation.outcome_indeterminate",
})
_bootstrap_residual_lock = threading.Lock()
_bootstrap_residual_clients: List[Any] = []


def has_bootstrap_residual_db_core_clients() -> bool:
    with _bootstrap_residual_lock:
        return bool(_bootstrap_residual_clients)


def retry_bootstrap_residual_db_core_clients(
    *,
    timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    """Retry owner-thread cleanup retained from a failed constructor bootstrap."""
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    deadline_at = time.monotonic() + timeout
    with _bootstrap_residual_lock:
        clients = list(_bootstrap_residual_clients)
    for client in clients:
        client._stop_and_join_failed_bootstrap(deadline_at)
        if client.owner_thread.is_alive():
            raise DbCoreServiceError(
                "DB Core bootstrap owner remained alive after retry",
                code="db_core_residual_process",
                outcome=DbCoreOutcome.NOT_STARTED,
            )
        with _bootstrap_residual_lock:
            if client in _bootstrap_residual_clients:
                _bootstrap_residual_clients.remove(client)


@dataclass(frozen=True)
class DbCoreRequestResult:
    request_kind: DbCoreRequestKind
    outcome: DbCoreOutcome
    request_id: str
    process_generation: int
    message: str
    rust_code: Optional[str]
    payload: Mapping[str, Any]


@dataclass
class _CallbackDelivery:
    payload: Dict[str, Any]
    is_terminal: bool
    ack: Optional[threading.Event] = None
    callback_error: Optional[BaseException] = None


@dataclass
class _PayloadNode:
    node_id: int
    parent_node_id: Optional[int]
    slot_index: Optional[int]
    value_kind: str
    next_sequence: int = 0
    final: bool = False
    items: List[Any] = field(default_factory=list)
    text: List[str] = field(default_factory=list)


class _PayloadAssembler:
    """Reassembles internal payload_chunk frames into one public event."""

    _VALUE_KINDS = frozenset({"list", "object", "utf8_string", "atomic"})

    def __init__(
        self,
        request_id: str,
        *,
        max_aggregate_bytes: int = MAX_ASSEMBLED_EVENT_BYTES,
        max_chunks: int = MAX_ASSEMBLED_EVENT_CHUNKS,
        max_nodes: int = MAX_ASSEMBLED_EVENT_NODES,
        max_depth: int = MAX_ASSEMBLED_EVENT_DEPTH,
    ):
        self._request_id = request_id
        self._max_aggregate_bytes = max_aggregate_bytes
        self._max_chunks = max_chunks
        self._max_nodes = max_nodes
        self._max_depth = max_depth
        self._aggregate_bytes = 0
        self._chunk_count = 0
        self._nodes: Dict[int, _PayloadNode] = {}
        self._root_node_id: Optional[int] = None
        self._command: Optional[str] = None
        self._logical_event: Optional[str] = None
        self._resolved_node_ids: Set[int] = set()

    @staticmethod
    def _integer(value: Any, field_name: str, *, nullable: bool = False) -> Optional[int]:
        if nullable and value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HelperProtocolError(
                f"payload_chunk {field_name} must be a non-negative integer"
            )
        return value

    @staticmethod
    def _required_keys(payload: Mapping[str, Any], required: Set[str]) -> None:
        missing = sorted(required.difference(payload))
        if missing:
            raise HelperProtocolError(
                "payload_chunk is missing required fields: " + ", ".join(missing)
            )

    def consume(
        self,
        payload: Dict[str, Any],
        *,
        frame_bytes: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if payload.get("event") != "payload_chunk":
            if self._nodes:
                raise HelperProtocolError(
                    "logical event changed before payload_chunk assembly completed"
                )
            return payload

        if frame_bytes is None:
            frame_bytes = len(
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                .encode("utf-8")
            )
        if isinstance(frame_bytes, bool) or not isinstance(frame_bytes, int) or frame_bytes < 0:
            raise HelperProtocolError("payload_chunk frame byte count is invalid")
        if self._chunk_count >= self._max_chunks:
            raise HelperProtocolError("payload_chunk exceeds aggregate chunk count limit")
        if frame_bytes > self._max_aggregate_bytes - self._aggregate_bytes:
            raise HelperProtocolError("payload_chunk exceeds aggregate byte limit")
        self._chunk_count += 1
        self._aggregate_bytes += frame_bytes

        self._required_keys(payload, {
            "request_id",
            "command",
            "logical_event",
            "node_id",
            "parent_node_id",
            "slot_index",
            "sequence",
            "final",
            "value_kind",
        })
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or request_id != self._request_id:
            raise HelperProtocolError("payload_chunk request_id does not match")
        command = payload.get("command")
        if command is not None and not isinstance(command, str):
            raise HelperProtocolError("payload_chunk command must be a string or null")
        logical_event = payload.get("logical_event")
        if (
            not isinstance(logical_event, str)
            or not logical_event
            or logical_event == "payload_chunk"
        ):
            raise HelperProtocolError("payload_chunk logical_event is invalid")
        if self._logical_event is None:
            self._logical_event = logical_event
            self._command = command
        elif logical_event != self._logical_event or command != self._command:
            raise HelperProtocolError("payload_chunk logical metadata conflicts")

        node_id = self._integer(payload.get("node_id"), "node_id")
        parent_node_id = self._integer(
            payload.get("parent_node_id"),
            "parent_node_id",
            nullable=True,
        )
        slot_index = self._integer(
            payload.get("slot_index"),
            "slot_index",
            nullable=True,
        )
        sequence = self._integer(payload.get("sequence"), "sequence")
        final = payload.get("final")
        if not isinstance(final, bool):
            raise HelperProtocolError("payload_chunk final must be boolean")
        value_kind = payload.get("value_kind")
        if value_kind not in self._VALUE_KINDS:
            raise HelperProtocolError("payload_chunk value_kind is invalid")
        if parent_node_id is None:
            if slot_index is not None:
                raise HelperProtocolError("payload_chunk root slot_index must be null")
            if self._root_node_id is None:
                self._root_node_id = node_id
            elif self._root_node_id != node_id:
                raise HelperProtocolError("payload_chunk contains multiple roots")
        elif slot_index is None or parent_node_id == node_id:
            raise HelperProtocolError("payload_chunk child relationship is invalid")

        node = self._nodes.get(node_id)
        if node is None:
            if sequence != 0:
                raise HelperProtocolError("payload_chunk node sequence must start at zero")
            if len(self._nodes) >= self._max_nodes:
                raise HelperProtocolError("payload_chunk exceeds aggregate node count limit")
            node = _PayloadNode(
                node_id=node_id,
                parent_node_id=parent_node_id,
                slot_index=slot_index,
                value_kind=value_kind,
            )
            self._nodes[node_id] = node
        elif (
            node.parent_node_id != parent_node_id
            or node.slot_index != slot_index
            or node.value_kind != value_kind
        ):
            raise HelperProtocolError("payload_chunk node metadata conflicts")
        if node.final or sequence != node.next_sequence:
            raise HelperProtocolError("payload_chunk sequence is duplicate or out of order")

        if value_kind == "utf8_string":
            text = payload.get("text")
            if not isinstance(text, str) or "items" in payload:
                raise HelperProtocolError("payload_chunk utf8_string text is invalid")
            node.text.append(text)
        else:
            items = payload.get("items")
            if not isinstance(items, list) or "text" in payload:
                raise HelperProtocolError("payload_chunk items are invalid")
            if value_kind == "atomic":
                if sequence != 0 or not final or len(items) != 1:
                    raise HelperProtocolError("payload_chunk atomic node is invalid")
            elif value_kind == "list":
                for child_id in items:
                    self._integer(child_id, "list child node_id")
            else:
                for item in items:
                    if not isinstance(item, dict) or set(item) != {
                        "key_node_id",
                        "value_node_id",
                    }:
                        raise HelperProtocolError("payload_chunk object item is invalid")
                    self._integer(item["key_node_id"], "object key_node_id")
                    self._integer(item["value_node_id"], "object value_node_id")
            node.items.extend(items)
        node.next_sequence += 1
        node.final = final

        if node_id != self._root_node_id or not final:
            return None
        value = self._resolve(node_id, set(), 1)
        if not isinstance(value, dict):
            raise HelperProtocolError("payload_chunk root must reconstruct an object")
        if value.get("event") != self._logical_event:
            raise HelperProtocolError("payload_chunk logical event does not match root")
        if value.get("request_id") != self._request_id:
            raise HelperProtocolError("payload_chunk reconstructed request_id does not match")
        if self._command is not None and value.get("command") != self._command:
            raise HelperProtocolError("payload_chunk reconstructed command does not match")
        if len(self._resolved_node_ids) != len(self._nodes):
            raise HelperProtocolError("payload_chunk contains unreachable nodes")
        self._nodes.clear()
        self._root_node_id = None
        self._command = None
        self._logical_event = None
        self._aggregate_bytes = 0
        self._chunk_count = 0
        return value

    def _resolve(self, node_id: int, visiting: Set[int], depth: int) -> Any:
        if depth > self._max_depth:
            raise HelperProtocolError("payload_chunk exceeds reconstruction depth limit")
        if not visiting:
            self._resolved_node_ids = set()
        if node_id in visiting:
            raise HelperProtocolError("payload_chunk contains a node cycle")
        node = self._nodes.get(node_id)
        if node is None or not node.final:
            raise HelperProtocolError("payload_chunk references a missing or incomplete node")
        visiting.add(node_id)
        if node.value_kind == "utf8_string":
            value: Any = "".join(node.text)
        elif node.value_kind == "atomic":
            value = node.items[0]
        elif node.value_kind == "list":
            values = []
            for index, child_id in enumerate(node.items):
                child = self._nodes.get(child_id)
                if (
                    child is None
                    or child.parent_node_id != node_id
                    or child.slot_index != index
                ):
                    raise HelperProtocolError("payload_chunk list child relationship conflicts")
                values.append(self._resolve(child_id, visiting, depth + 1))
            value = values
        else:
            values_dict: Dict[str, Any] = {}
            for index, item in enumerate(node.items):
                key_id = item["key_node_id"]
                value_id = item["value_node_id"]
                key_node = self._nodes.get(key_id)
                value_node = self._nodes.get(value_id)
                if any(
                    child is None
                    or child.parent_node_id != node_id
                    or child.slot_index != index
                    for child in (key_node, value_node)
                ):
                    raise HelperProtocolError("payload_chunk object child relationship conflicts")
                key = self._resolve(key_id, visiting, depth + 1)
                if not isinstance(key, str) or key in values_dict:
                    raise HelperProtocolError("payload_chunk object key is invalid or duplicate")
                values_dict[key] = self._resolve(value_id, visiting, depth + 1)
            value = values_dict
        visiting.remove(node_id)
        self._resolved_node_ids.add(node_id)
        return value


class DbCoreServiceError(RuntimeError):
    """Raised when the Rust DB core service cannot complete a request."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "db_core_business_failure",
        request_kind: DbCoreRequestKind = DbCoreRequestKind.MUTATION,
        outcome: DbCoreOutcome = DbCoreOutcome.FAILED,
        request_id: str = "",
        process_generation: int = 0,
        rust_code: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_kind = request_kind
        self.outcome = outcome
        self.request_id = request_id
        self.process_generation = process_generation
        self.rust_code = rust_code
        self.payload = payload or {}
        self.cleanup_error: Optional["DbCoreServiceError"] = None


class DbCoreCallbackError(RuntimeError):
    """Raised when a caller callback fails while consuming DB Core events."""

    def __init__(
        self,
        cause: BaseException,
        *,
        request_kind: DbCoreRequestKind,
        outcome: DbCoreOutcome,
        request_result: Optional[DbCoreRequestResult] = None,
    ):
        super().__init__(str(cause))
        self.request_result = request_result
        self.request_kind = request_kind
        self.outcome = outcome
        self.cause = cause


def _format_error_event(payload: Dict[str, Any]) -> str:
    message = str(payload.get("message") or payload.get("error") or "DB core service error")
    details: List[str] = []
    for key, label in (
        ("code", "code"),
        ("detail", "detail"),
        ("hint", "hint"),
        ("context", "context"),
        ("table", "table"),
        ("column", "column"),
        ("constraint", "constraint"),
    ):
        value = payload.get(key)
        if value not in (None, ""):
            details.append(f"{label}={value}")
    if not details:
        return message
    return f"{message} ({'; '.join(details)})"


SUPPORTED_DB_ENGINES = {"mysql", "postgresql"}


def parse_db_version_tuple(version: Any) -> Tuple[int, int, int]:
    """Return a connector-compatible (major, minor, patch) tuple."""
    if isinstance(version, tuple):
        parts = list(version)
    elif isinstance(version, list):
        parts = version
    else:
        text = str(version or "")
        match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
        if not match:
            return (0, 0, 0)
        parts = [match.group(1), match.group(2) or 0, match.group(3) or 0]

    parsed = []
    for index in range(3):
        try:
            parsed.append(int(parts[index]))
        except (IndexError, TypeError, ValueError):
            parsed.append(0)
    return tuple(parsed)


def normalize_db_engine(engine: Optional[str], port: Optional[int] = None) -> str:
    """Return the Rust core engine id used by DB-facing product paths."""
    value = str(engine or "").strip().lower()
    if value in ("postgres", "postgresql", "pg"):
        return "postgresql"
    if value in ("mysql", "mariadb"):
        return "mysql"
    if int(port or 0) == 5432:
        return "postgresql"
    return "mysql"


def default_database_for_engine(engine: str, database: Optional[str] = None) -> str:
    if database:
        return database
    return "postgres" if normalize_db_engine(engine) == "postgresql" else ""


class DbCoreServiceClient:
    """Sequential JSONL client for the long-lived Rust DB core process."""

    def __init__(
        self,
        executable: Optional[str] = None,
        popen_factory: Optional[Callable[..., Any]] = None,
        *,
        process_argv: Optional[List[str]] = None,
        process_factory: Optional[Callable[..., Any]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        loop_factory: Optional[Callable[[], asyncio.AbstractEventLoop]] = None,
        phase_observer: Optional[Callable[..., None]] = None,
        bootstrap_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ):
        if popen_factory is not None and process_factory is not None:
            raise ValueError("popen_factory and process_factory are mutually exclusive")
        self.executable = executable or db_core_executable()
        self._process_argv = list(process_argv or [self.executable])
        self._process_factory = process_factory or popen_factory
        self._monotonic = monotonic
        self._loop_factory = loop_factory or self._default_loop_factory
        self._phase_observer = phase_observer
        self._process: Optional[Any] = None
        self._process_generation = 0
        self._generation_state = DbCoreGenerationState.CLOSED
        self._stderr_tail: Deque[str] = deque(maxlen=200)
        self._stderr_lock = threading.Lock()
        self._stderr_task: Optional[asyncio.Task] = None
        self._admission_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._shutdown_complete = False
        self._active_request_task: Optional[asyncio.Task] = None
        self._active_request_deadline_at: Optional[float] = None
        self._request_tasks: Set[asyncio.Task] = set()
        self._spawn_task: Optional[asyncio.Task] = None
        self._spawn_is_native = False
        self._spawn_residual: Optional[Dict[str, Any]] = None
        self._request_lock: Optional[asyncio.Lock] = None
        self._owner_ready = threading.Event()
        self._owner_stop_requested = threading.Event()
        self._owner_loop: Optional[asyncio.AbstractEventLoop] = None
        self._owner_error: Optional[BaseException] = None
        self._shutdown_owner_drained = False
        self._loop_stop_submitted = False
        self._owner_thread = threading.Thread(
            target=self._run_owner_loop,
            name=f"TunnelForgeDbCoreOwner-{id(self):x}",
            daemon=False,
        )
        self._owner_thread.start()
        bootstrap_timeout = self._validated_timeout(
            bootstrap_timeout_seconds,
            DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )
        bootstrap_deadline = time.monotonic() + bootstrap_timeout
        ready_timeout = min(bootstrap_timeout / 2.0, bootstrap_timeout)
        if not self._owner_ready.wait(timeout=ready_timeout):
            bootstrap_cleaned = self._stop_and_join_failed_bootstrap(bootstrap_deadline)
            raise DbCoreServiceError(
                "DB Core owner loop did not start within the bounded timeout",
                code=(
                    "db_core_start_failed"
                    if bootstrap_cleaned
                    else "db_core_residual_process"
                ),
                outcome=DbCoreOutcome.NOT_STARTED,
            )
        if self._owner_loop is None or self._owner_error is not None:
            self._stop_and_join_failed_bootstrap(bootstrap_deadline)
            raise DbCoreServiceError(
                f"DB Core owner loop failed to start: {self._owner_error}",
                code="db_core_start_failed",
                outcome=DbCoreOutcome.NOT_STARTED,
            )

    @staticmethod
    def _default_loop_factory() -> asyncio.AbstractEventLoop:
        if sys.platform == "win32":
            return asyncio.ProactorEventLoop()
        return asyncio.SelectorEventLoop()

    @property
    def owner_thread(self) -> threading.Thread:
        return self._owner_thread

    @property
    def owner_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._owner_loop
        if loop is None:
            raise RuntimeError("DB Core owner loop is unavailable")
        return loop

    @property
    def shutdown_complete(self) -> bool:
        return self._shutdown_complete

    @property
    def process_generation(self) -> int:
        return self._process_generation

    @property
    def generation_state(self) -> DbCoreGenerationState:
        return self._generation_state

    def _transition_generation(self, state: DbCoreGenerationState) -> None:
        if state is self._generation_state:
            return
        allowed = {
            DbCoreGenerationState.CREATING: {
                DbCoreGenerationState.ACTIVE,
                DbCoreGenerationState.POISONED,
            },
            DbCoreGenerationState.ACTIVE: {
                DbCoreGenerationState.POISONED,
                DbCoreGenerationState.REAPING,
            },
            DbCoreGenerationState.POISONED: {DbCoreGenerationState.REAPING},
            DbCoreGenerationState.REAPING: {DbCoreGenerationState.CLOSED},
            DbCoreGenerationState.CLOSED: {DbCoreGenerationState.CREATING},
        }
        if state not in allowed[self._generation_state]:
            raise RuntimeError(
                "invalid DB Core generation transition: "
                f"{self._generation_state.value} -> {state.value}"
            )
        self._generation_state = state
        if self._phase_observer is not None:
            self._phase_observer(state, self._process_generation)

    def _stop_and_join_failed_bootstrap(self, deadline_at: float) -> bool:
        self._owner_stop_requested.set()
        loop = self._owner_loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        self._owner_thread.join(timeout=max(0.0, deadline_at - time.monotonic()))
        if self._owner_thread.is_alive():
            with _bootstrap_residual_lock:
                if self not in _bootstrap_residual_clients:
                    _bootstrap_residual_clients.append(self)
            return False
        return True

    def _run_owner_loop(self) -> None:
        loop: Optional[asyncio.AbstractEventLoop] = None
        try:
            loop = self._loop_factory()
            self._owner_loop = loop
            asyncio.set_event_loop(loop)
            self._request_lock = asyncio.Lock()
            self._owner_ready.set()
            if not self._owner_stop_requested.is_set():
                loop.run_forever()
        except BaseException as exc:
            self._owner_error = exc
            self._owner_ready.set()
        finally:
            if loop is not None:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            asyncio.set_event_loop(None)

    @staticmethod
    def _validated_timeout(timeout_seconds: Optional[float], default: float) -> float:
        timeout = default if timeout_seconds is None else float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        return timeout

    def _owner_unavailable_error(
        self,
        request_kind: DbCoreRequestKind,
        request_id: str,
    ) -> DbCoreServiceError:
        shutdown_started = self._shutdown_started
        return DbCoreServiceError(
            (
                "DB Core shutdown has started; new requests are not admitted"
                if shutdown_started
                else "DB Core owner is unavailable"
            ),
            code=("db_core_cleanup_failed" if shutdown_started else "db_core_start_failed"),
            request_kind=request_kind,
            outcome=DbCoreOutcome.NOT_STARTED,
            request_id=request_id,
            process_generation=self._process_generation,
        )

    def _submit_owner(self, coroutine, request_kind: DbCoreRequestKind, request_id: str):
        if self._shutdown_complete or not self._owner_thread.is_alive():
            coroutine.close()
            raise self._owner_unavailable_error(request_kind, request_id)
        loop = self._owner_loop
        if loop is None or loop.is_closed():
            coroutine.close()
            raise self._owner_unavailable_error(request_kind, request_id)
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError as exc:
            coroutine.close()
            raise self._owner_unavailable_error(request_kind, request_id) from exc

    def _submit_admitted(
        self,
        coroutine,
        request_kind: DbCoreRequestKind,
        request_id: str,
        deadline_at: float,
    ):
        remaining = self._remaining(deadline_at)
        if remaining <= 0.0 or not self._admission_lock.acquire(timeout=remaining):
            coroutine.close()
            raise DbCoreServiceError(
                "DB Core request admission exceeded its absolute deadline",
                code="db_core_timeout",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            )
        try:
            if self._shutdown_started:
                coroutine.close()
                raise self._owner_unavailable_error(request_kind, request_id)
            return self._submit_owner(coroutine, request_kind, request_id)
        finally:
            self._admission_lock.release()

    def _remaining(self, deadline_at: float) -> float:
        return max(0.0, deadline_at - self._monotonic())

    @staticmethod
    def _cleanup_start(deadline_at: float, timeout_seconds: float) -> float:
        cleanup_reserve = min(2.0, timeout_seconds * 0.2)
        return deadline_at - cleanup_reserve

    async def _await_before(self, awaitable, cutoff_at: float):
        return await asyncio.wait_for(awaitable, timeout=self._remaining(cutoff_at))

    async def _settle_cancelled_task_before(
        self,
        task: asyncio.Task,
        cutoff_at: float,
    ) -> None:
        if not task.done():
            await asyncio.sleep(0)
        if task.done():
            await asyncio.shield(task)
            return
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=self._remaining(cutoff_at),
        )

    async def _settle_process_wait_before(self, awaitable, cutoff_at: float):
        wait_task = asyncio.ensure_future(awaitable)
        if not wait_task.done():
            await asyncio.sleep(0)
        if wait_task.done():
            return wait_task.result()
        remaining = self._remaining(cutoff_at)
        if remaining <= 0.0:
            wait_task.cancel()
            await asyncio.sleep(0)
            raise asyncio.TimeoutError
        return await asyncio.wait_for(wait_task, timeout=remaining)

    def _cleanup_deadline_on_owner(self, *deadlines: float) -> float:
        return min(
            *deadlines,
            self._monotonic() + DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )

    @staticmethod
    def _task_diagnostic(task: asyncio.Task) -> str:
        try:
            name = task.get_name()
        except AttributeError:
            name = "task"
        try:
            coroutine = task.get_coro()
            coroutine_name = getattr(
                coroutine,
                "__qualname__",
                getattr(coroutine, "__name__", type(coroutine).__name__),
            )
        except BaseException:
            coroutine_name = "unknown"
        return f"{name}:{coroutine_name}"

    def _residual_process_error(
        self,
        stage: str,
        message: str,
        *,
        process: Optional[Any] = None,
        pending_tasks: Optional[List[str]] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> DbCoreServiceError:
        target = self._process if process is None else process
        payload: Dict[str, Any] = {
            "stage": stage,
            "pid": getattr(target, "pid", None),
            "process_generation": self._process_generation,
            "generation_state": self._generation_state.value,
            "pending_tasks": list(pending_tasks or []),
        }
        if extra:
            payload.update(extra)
        return DbCoreServiceError(
            message,
            code="db_core_residual_process",
            outcome=DbCoreOutcome.FAILED,
            process_generation=self._process_generation,
            payload=payload,
        )

    def start(self) -> None:
        request_id = f"py-{uuid.uuid4().hex}"
        timeout = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
        deadline_at = self._monotonic() + timeout
        work_cutoff = self._cleanup_start(deadline_at, timeout)
        future = self._submit_admitted(
            self._start_on_owner(
                DbCoreRequestKind.MUTATION,
                request_id,
                work_cutoff,
                deadline_at,
            ),
            DbCoreRequestKind.MUTATION,
            request_id,
            deadline_at,
        )
        try:
            future.result(timeout=self._remaining(deadline_at))
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise DbCoreServiceError(
                "DB Core process start exceeded its deadline",
                code="db_core_timeout",
                request_kind=DbCoreRequestKind.MUTATION,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc
        except concurrent.futures.CancelledError as exc:
            raise DbCoreServiceError(
                "DB Core process start was cancelled by client shutdown",
                code="db_core_cleanup_failed",
                request_kind=DbCoreRequestKind.MUTATION,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc

    async def _start_on_owner(
        self,
        request_kind: DbCoreRequestKind,
        request_id: str,
        work_cutoff: float,
        deadline_at: float,
    ) -> None:
        lock = self._request_lock
        assert lock is not None
        current = asyncio.current_task()
        assert current is not None
        self._request_tasks.add(current)
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    lock.acquire(),
                    timeout=self._remaining(work_cutoff),
                )
                acquired = True
            except asyncio.TimeoutError as exc:
                raise DbCoreServiceError(
                    "DB Core process start waited past its absolute deadline",
                    code="db_core_timeout",
                    request_kind=request_kind,
                    outcome=DbCoreOutcome.NOT_STARTED,
                    request_id=request_id,
                    process_generation=self._process_generation,
                ) from exc
            if self._remaining(work_cutoff) <= 0.0:
                raise DbCoreServiceError(
                    "DB Core process start waited past its absolute deadline",
                    code="db_core_timeout",
                    request_kind=request_kind,
                    outcome=DbCoreOutcome.NOT_STARTED,
                    request_id=request_id,
                    process_generation=self._process_generation,
                )
            self._active_request_task = current
            self._active_request_deadline_at = deadline_at
            await self._start_process_on_owner(
                request_kind,
                request_id,
                work_cutoff,
                deadline_at,
            )
        finally:
            if self._active_request_task is current:
                self._active_request_task = None
                self._active_request_deadline_at = None
            if acquired:
                lock.release()
            self._request_tasks.discard(current)

    async def _start_process_on_owner(
        self,
        request_kind: DbCoreRequestKind,
        request_id: str,
        work_cutoff: float,
        deadline_at: float,
    ) -> None:
        """Start the core process on the dedicated owner thread."""
        if (
            self._process is not None
            and self._generation_state is DbCoreGenerationState.ACTIVE
            and self._process_is_running_on_owner(self._process)
        ):
            return
        if self._process is not None:
            await self._poison_and_reap_on_owner(deadline_at)
        self._process_generation += 1
        self._transition_generation(DbCoreGenerationState.CREATING)
        try:
            if self._process_factory is None:
                pending_process = asyncio.create_subprocess_exec(
                    *self._process_argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_JSONL_FRAME_BYTES,
                    creationflags=no_window_creation_flags(),
                )
            else:
                pending_process = self._process_factory(
                    self._process_argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_JSONL_FRAME_BYTES,
                    creationflags=no_window_creation_flags(),
                )
            if inspect.isawaitable(pending_process):
                spawn_task = asyncio.ensure_future(pending_process)
                self._spawn_task = spawn_task
                self._spawn_is_native = self._process_factory is None
                process = await self._await_before(
                    asyncio.shield(spawn_task),
                    work_cutoff,
                )
                if self._spawn_task is spawn_task:
                    self._spawn_task = None
                    self._spawn_is_native = False
            else:
                process = pending_process
        except FileNotFoundError as exc:
            await self._poison_and_reap_on_owner(deadline_at)
            raise DbCoreServiceError(
                "Rust DB Core 실행 파일을 찾을 수 없습니다: "
                f"{self.executable}\n"
                "소스 실행이면 `cargo build --manifest-path migration_core\\Cargo.toml --release`를 먼저 실행하고, "
                "설치본이면 배포 패키지에 tunnelforge-core 실행 파일이 포함되어 있는지 확인하세요.",
                code="db_core_start_failed",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc
        except asyncio.TimeoutError as exc:
            await self._poison_and_reap_on_owner(deadline_at)
            raise DbCoreServiceError(
                "DB Core process creation exceeded the request deadline",
                code="db_core_timeout",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc
        except DbCoreServiceError as error:
            await self._poison_and_reap_preserving_on_owner(error, deadline_at)
            raise
        except Exception as exc:
            await self._poison_and_reap_on_owner(deadline_at)
            raise DbCoreServiceError(
                f"DB Core process failed to start: {type(exc).__name__}: {exc}",
                code="db_core_start_failed",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc
        if process is None:
            await self._poison_and_reap_on_owner(deadline_at)
            raise DbCoreServiceError(
                "DB Core process factory returned no process",
                code="db_core_start_failed",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            )
        self._process = process
        with self._stderr_lock:
            self._stderr_tail.clear()
        self._stderr_task = asyncio.create_task(self._drain_stderr_on_owner(process))
        try:
            await self._negotiate_process_on_owner(
                process,
                request_kind,
                request_id,
                work_cutoff,
            )
        except DbCoreServiceError as error:
            await self._poison_and_reap_preserving_on_owner(error, deadline_at)
            raise
        except Exception as exc:
            await self._poison_and_reap_on_owner(deadline_at)
            raise DbCoreServiceError(
                f"DB Core hello negotiation failed: {type(exc).__name__}: {exc}",
                code="db_core_capability_missing",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc
        self._transition_generation(DbCoreGenerationState.ACTIVE)

    def _process_is_running_on_owner(self, process: Any) -> bool:
        if hasattr(process, "returncode"):
            return process.returncode is None
        poll = getattr(process, "poll", None)
        return bool(callable(poll) and poll() is None)

    @staticmethod
    def _encode_request_frame(body: Mapping[str, Any]) -> bytes:
        try:
            encoded = (
                json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise HelperProtocolError(f"request JSON encoding failed: {exc}") from exc
        if len(encoded) > MAX_JSONL_FRAME_BYTES:
            raise HelperProtocolError(
                "request JSONL frame exceeds the negotiated 1 MiB wire cap"
            )
        return encoded

    async def _write_request_frame_on_owner(
        self,
        process: Any,
        body: Mapping[str, Any],
        cutoff_at: float,
        *,
        allowed_state: DbCoreGenerationState,
        write_started: Optional[threading.Event] = None,
    ) -> None:
        if process is not self._process or self._generation_state is not allowed_state:
            raise HelperProtocolError("DB Core generation changed before transport write")
        stdin = process.stdin
        if stdin is None:
            raise HelperProtocolError("DB Core stdin is unavailable")
        encoded = self._encode_request_frame(body)
        if write_started is not None:
            write_started.set()
        try:
            try:
                stdin.write(encoded)
            except TypeError:
                stdin.write(encoded.decode("utf-8"))
            if process is not self._process or self._generation_state is not allowed_state:
                raise HelperProtocolError("DB Core generation changed before transport drain")
            drain = getattr(stdin, "drain", None)
            if callable(drain):
                pending_drain = drain()
                if inspect.isawaitable(pending_drain):
                    await self._await_before(pending_drain, cutoff_at)
            else:
                flush = getattr(stdin, "flush", None)
                if callable(flush):
                    flush()
        except asyncio.TimeoutError:
            raise
        except HelperProtocolError:
            raise
        except Exception as exc:
            raise OSError(f"DB Core request write failed: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _strict_event_payload(line: str, request_id: str) -> Dict[str, Any]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HelperProtocolError(f"Invalid helper JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HelperProtocolError("Helper event must be a JSON object")
        event_type = payload.get("event")
        if not isinstance(event_type, str) or not event_type:
            raise HelperProtocolError("Helper event is missing a string event")
        response_id = payload.get("request_id")
        if not isinstance(response_id, str) or response_id != request_id:
            raise DbCoreServiceError(
                "DB Core response request_id did not match the active request",
                code="db_core_request_id_mismatch",
                request_id=request_id,
                payload=payload,
            )
        return payload

    async def _read_logical_event_on_owner(
        self,
        process: Any,
        request_id: str,
        assembler: _PayloadAssembler,
        cutoff_at: float,
    ) -> Dict[str, Any]:
        stdout = process.stdout
        if stdout is None:
            raise HelperProtocolError("DB Core stdout is unavailable")
        while True:
            line = await self._read_stream_line_on_owner(
                stdout,
                deadline_at=cutoff_at,
                enforce_frame_cap=True,
            )
            if line == "":
                raise EOFError(self._stderr_tail_text() or "DB Core process closed stdout")
            payload = self._strict_event_payload(line, request_id)
            logical = assembler.consume(
                payload,
                frame_bytes=len(line.encode("utf-8", errors="strict")),
            )
            if logical is not None:
                return logical

    async def _negotiate_process_on_owner(
        self,
        process: Any,
        request_kind: DbCoreRequestKind,
        request_id: str,
        work_cutoff: float,
    ) -> None:
        hello_id = f"py-hello-{self._process_generation}-{uuid.uuid4().hex}"
        body = {"command": "service.hello", "request_id": hello_id, "payload": {}}
        try:
            await self._write_request_frame_on_owner(
                process,
                body,
                work_cutoff,
                allowed_state=DbCoreGenerationState.CREATING,
            )
            payload = await self._read_logical_event_on_owner(
                process,
                hello_id,
                _PayloadAssembler(hello_id),
                work_cutoff,
            )
        except DbCoreServiceError as exc:
            exc.request_kind = request_kind
            exc.outcome = DbCoreOutcome.NOT_STARTED
            exc.request_id = request_id
            exc.process_generation = self._process_generation
            raise
        except asyncio.TimeoutError as exc:
            raise DbCoreServiceError(
                "DB Core hello negotiation exceeded the absolute request deadline",
                code="db_core_timeout",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc
        except (EOFError, HelperProtocolError, OSError) as exc:
            raise DbCoreServiceError(
                f"DB Core hello negotiation failed: {exc}",
                code="db_core_capability_missing",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc

        capabilities = payload.get("process_capabilities")
        exact_capabilities = (
            isinstance(capabilities, list)
            and all(isinstance(value, str) for value in capabilities)
            and len(capabilities) == len(REQUIRED_PROCESS_CAPABILITIES)
            and frozenset(capabilities) == REQUIRED_PROCESS_CAPABILITIES
        )
        exact_integer = lambda value, expected: (
            isinstance(value, int) and not isinstance(value, bool) and value == expected
        )
        if not (
            payload.get("event") == "result"
            and payload.get("command") == "service.hello"
            and payload.get("success") is True
            and payload.get("service") == "tunnelforge-core"
            and exact_integer(payload.get("protocol_version"), 1)
            and exact_integer(payload.get("process_version"), 1)
            and exact_integer(
                payload.get("max_jsonl_frame_bytes"),
                MAX_JSONL_FRAME_BYTES,
            )
            and exact_integer(
                payload.get("max_assembled_event_bytes"),
                MAX_ASSEMBLED_EVENT_BYTES,
            )
            and exact_integer(
                payload.get("max_assembled_event_chunks"),
                MAX_ASSEMBLED_EVENT_CHUNKS,
            )
            and exact_integer(
                payload.get("max_assembled_event_nodes"),
                MAX_ASSEMBLED_EVENT_NODES,
            )
            and exact_integer(
                payload.get("max_assembled_event_depth"),
                MAX_ASSEMBLED_EVENT_DEPTH,
            )
            and exact_capabilities
        ):
            raise DbCoreServiceError(
                "DB Core hello did not advertise the exact required process contract",
                code="db_core_capability_missing",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
                payload=payload,
            )

    async def _read_stream_line_on_owner(
        self,
        stream: Any,
        *,
        deadline_at: Optional[float] = None,
        enforce_frame_cap: bool = False,
    ) -> str:
        try:
            pending_line = stream.readline()
            if inspect.isawaitable(pending_line):
                if deadline_at is None:
                    line = await pending_line
                else:
                    line = await self._await_before(pending_line, deadline_at)
            else:
                line = pending_line
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise HelperProtocolError(
                "DB Core stream exceeded its configured JSONL frame limit"
            ) from exc
        if isinstance(line, bytes):
            encoded = line
            try:
                text = line.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise HelperProtocolError("DB Core frame is not valid UTF-8") from exc
        else:
            text = str(line)
            try:
                encoded = text.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise HelperProtocolError("DB Core frame is not valid UTF-8") from exc
        if enforce_frame_cap and len(encoded) > MAX_JSONL_FRAME_BYTES:
            raise HelperProtocolError("DB Core JSONL frame exceeds the 1 MiB wire cap")
        return text

    async def _drain_stderr_on_owner(self, process: Any) -> None:
        stream = process.stderr
        if stream is None:
            return
        try:
            while True:
                line = await self._read_stream_line_on_owner(stream)
                if line == "":
                    return
                text = line.rstrip()
                if text:
                    with self._stderr_lock:
                        self._stderr_tail.append(text[-4000:])
        except (asyncio.CancelledError, HelperProtocolError, ValueError, OSError):
            return

    def _stderr_tail_text(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)

    def _transport_outcome(self, request_kind: DbCoreRequestKind) -> DbCoreOutcome:
        if request_kind is DbCoreRequestKind.MUTATION:
            return DbCoreOutcome.OUTCOME_INDETERMINATE
        return DbCoreOutcome.FAILED

    async def _send_on_owner(
        self,
        command: str,
        payload: Optional[Dict[str, Any]],
        request_id: str,
        request_kind: DbCoreRequestKind,
        event_queue: "queue.Queue[_CallbackDelivery]",
        required_generation: Optional[int],
        work_cutoff: float,
        deadline_at: float,
        requires_callback_ack: bool,
        write_started: threading.Event,
    ) -> DbCoreRequestResult:
        body = {
            "command": command,
            "request_id": request_id,
            "payload": payload or {},
        }
        try:
            self._encode_request_frame(body)
        except HelperProtocolError as exc:
            raise DbCoreServiceError(
                f"DB Core request cannot be encoded within the wire cap: {exc}",
                code="db_core_write_failed",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc
        if required_generation is not None and not (
            required_generation == self._process_generation
            and self._generation_state is DbCoreGenerationState.ACTIVE
            and self._process is not None
            and self._process_is_running_on_owner(self._process)
        ):
            raise DbCoreServiceError(
                "DB Core connection belongs to a stale process generation",
                code="db_core_stale_connection",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            )
        await self._start_process_on_owner(
            request_kind,
            request_id,
            work_cutoff,
            deadline_at,
        )
        if self._remaining(work_cutoff) <= 0.0:
            raise DbCoreServiceError(
                "DB Core request expired before transport write",
                code="db_core_timeout",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            )
        if required_generation is not None and required_generation != self._process_generation:
            raise DbCoreServiceError(
                "DB Core connection belongs to a stale process generation",
                code="db_core_stale_connection",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            )
        process = self._process
        assert process is not None
        try:
            await self._write_request_frame_on_owner(
                process,
                body,
                work_cutoff,
                allowed_state=DbCoreGenerationState.ACTIVE,
                write_started=write_started,
            )
        except asyncio.TimeoutError as exc:
            error = DbCoreServiceError(
                "DB Core request write exceeded its absolute deadline",
                code="db_core_timeout",
                request_kind=request_kind,
                outcome=self._transport_outcome(request_kind),
                request_id=request_id,
                process_generation=self._process_generation,
            )
            error.__cause__ = exc
            await self._poison_and_reap_preserving_on_owner(error, deadline_at)
            raise error
        except (HelperProtocolError, OSError) as exc:
            error = DbCoreServiceError(
                f"DB Core request write failed: {type(exc).__name__}: {exc}",
                code="db_core_write_failed",
                request_kind=request_kind,
                outcome=self._transport_outcome(request_kind),
                request_id=request_id,
                process_generation=self._process_generation,
            )
            error.__cause__ = exc
            await self._poison_and_reap_preserving_on_owner(error, deadline_at)
            raise error
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = DbCoreServiceError(
                f"DB Core request write failed: {type(exc).__name__}: {exc}",
                code="db_core_write_failed",
                request_kind=request_kind,
                outcome=self._transport_outcome(request_kind),
                request_id=request_id,
                process_generation=self._process_generation,
            )
            error.__cause__ = exc
            await self._poison_and_reap_preserving_on_owner(error, deadline_at)
            raise error

        assembler = _PayloadAssembler(request_id)
        while True:
            remaining = self._remaining(work_cutoff)
            if remaining <= 0.0:
                error = DbCoreServiceError(
                    "DB Core request exceeded its absolute deadline",
                    code="db_core_timeout",
                    request_kind=request_kind,
                    outcome=self._transport_outcome(request_kind),
                    request_id=request_id,
                    process_generation=self._process_generation,
                )
                await self._poison_and_reap_preserving_on_owner(error, deadline_at)
                raise error
            try:
                payload_event = await self._read_logical_event_on_owner(
                    process,
                    request_id,
                    assembler,
                    work_cutoff,
                )
            except asyncio.TimeoutError as exc:
                error = DbCoreServiceError(
                    "DB Core request exceeded its absolute deadline",
                    code="db_core_timeout",
                    request_kind=request_kind,
                    outcome=self._transport_outcome(request_kind),
                    request_id=request_id,
                    process_generation=self._process_generation,
                )
                error.__cause__ = exc
                await self._poison_and_reap_preserving_on_owner(error, deadline_at)
                raise error
            except EOFError as exc:
                error = DbCoreServiceError(
                    str(exc),
                    code="db_core_process_died",
                    request_kind=request_kind,
                    outcome=self._transport_outcome(request_kind),
                    request_id=request_id,
                    process_generation=self._process_generation,
                )
                error.__cause__ = exc
                await self._poison_and_reap_preserving_on_owner(error, deadline_at)
                raise error
            except DbCoreServiceError as exc:
                exc.request_kind = request_kind
                exc.outcome = self._transport_outcome(request_kind)
                exc.request_id = request_id
                exc.process_generation = self._process_generation
                await self._poison_and_reap_preserving_on_owner(exc, deadline_at)
                raise
            except HelperProtocolError as exc:
                error = DbCoreServiceError(
                    f"DB Core emitted a malformed protocol event: {exc}",
                    code="db_core_protocol_mismatch",
                    request_kind=request_kind,
                    outcome=self._transport_outcome(request_kind),
                    request_id=request_id,
                    process_generation=self._process_generation,
                )
                error.__cause__ = exc
                await self._poison_and_reap_preserving_on_owner(error, deadline_at)
                raise error
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                error = DbCoreServiceError(
                    f"DB Core request transport failed: {type(exc).__name__}: {exc}",
                    code="db_core_transport_failed",
                    request_kind=request_kind,
                    outcome=self._transport_outcome(request_kind),
                    request_id=request_id,
                    process_generation=self._process_generation,
                )
                error.__cause__ = exc
                await self._poison_and_reap_preserving_on_owner(error, deadline_at)
                raise error
            event_type = payload_event["event"]
            is_terminal = event_type in ("result", "error")
            if is_terminal and payload_event.get("command") != command:
                error = DbCoreServiceError(
                    "DB Core terminal command did not match the active request",
                    code="db_core_protocol_mismatch",
                    request_kind=request_kind,
                    outcome=self._transport_outcome(request_kind),
                    request_id=request_id,
                    process_generation=self._process_generation,
                    payload=payload_event,
                )
                await self._poison_and_reap_preserving_on_owner(error, deadline_at)
                raise error
            if requires_callback_ack:
                delivery = _CallbackDelivery(
                    payload=payload_event,
                    is_terminal=is_terminal,
                    ack=(threading.Event() if not is_terminal else None),
                )
                event_queue.put(delivery)
                if delivery.ack is not None:
                    while not delivery.ack.is_set():
                        remaining = self._remaining(work_cutoff)
                        if remaining <= 0.0:
                            error = DbCoreServiceError(
                                "DB Core progress callback exceeded the request deadline",
                                code="db_core_callback_failed",
                                request_kind=request_kind,
                                outcome=self._transport_outcome(request_kind),
                                request_id=request_id,
                                process_generation=self._process_generation,
                            )
                            await self._poison_and_reap_preserving_on_owner(
                                error,
                                deadline_at,
                            )
                            raise error
                        await asyncio.sleep(min(0.01, remaining))
                    if delivery.callback_error is not None:
                        error = DbCoreServiceError(
                            f"DB Core progress callback failed: {delivery.callback_error}",
                            code="db_core_callback_failed",
                            request_kind=request_kind,
                            outcome=self._transport_outcome(request_kind),
                            request_id=request_id,
                            process_generation=self._process_generation,
                        )
                        await self._poison_and_reap_preserving_on_owner(
                            error,
                            deadline_at,
                        )
                        raise error
            if event_type == "result":
                return DbCoreRequestResult(
                    request_kind=request_kind,
                    outcome=DbCoreOutcome.DEFINITE,
                    request_id=request_id,
                    process_generation=self._process_generation,
                    message=str(payload_event.get("message") or ""),
                    rust_code=None,
                    payload=payload_event,
                )
            if event_type == "error":
                rust_code = payload_event.get("code")
                if not isinstance(rust_code, str) or not rust_code.strip():
                    error = DbCoreServiceError(
                        "DB Core error event is missing a non-empty string code",
                        code="db_core_protocol_mismatch",
                        request_kind=request_kind,
                        outcome=self._transport_outcome(request_kind),
                        request_id=request_id,
                        process_generation=self._process_generation,
                        payload=payload_event,
                    )
                    await self._poison_and_reap_preserving_on_owner(error, deadline_at)
                    raise error
                raise DbCoreServiceError(
                    _format_error_event(payload_event),
                    code="db_core_business_failure",
                    request_kind=request_kind,
                    outcome=DbCoreOutcome.FAILED,
                    request_id=request_id,
                    process_generation=self._process_generation,
                    rust_code=rust_code,
                    payload=payload_event,
                )

    async def _request_on_owner(
        self,
        command: str,
        payload: Optional[Dict[str, Any]],
        request_id: str,
        request_kind: DbCoreRequestKind,
        event_queue: "queue.Queue[_CallbackDelivery]",
        required_generation: Optional[int],
        work_cutoff: float,
        deadline_at: float,
        requires_callback_ack: bool,
        write_started: threading.Event,
    ) -> DbCoreRequestResult:
        lock = self._request_lock
        assert lock is not None
        current = asyncio.current_task()
        assert current is not None
        self._request_tasks.add(current)
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    lock.acquire(),
                    timeout=self._remaining(work_cutoff),
                )
                acquired = True
            except asyncio.TimeoutError as exc:
                raise DbCoreServiceError(
                    "DB Core request waited past its absolute deadline",
                    code="db_core_timeout",
                    request_kind=request_kind,
                    outcome=DbCoreOutcome.NOT_STARTED,
                    request_id=request_id,
                    process_generation=self._process_generation,
                ) from exc
            if self._remaining(work_cutoff) <= 0.0:
                raise DbCoreServiceError(
                    "DB Core request waited past its absolute deadline",
                    code="db_core_timeout",
                    request_kind=request_kind,
                    outcome=DbCoreOutcome.NOT_STARTED,
                    request_id=request_id,
                    process_generation=self._process_generation,
                )
            self._active_request_task = current
            self._active_request_deadline_at = deadline_at
            return await self._send_on_owner(
                command,
                payload,
                request_id,
                request_kind,
                event_queue,
                required_generation,
                work_cutoff,
                deadline_at,
                requires_callback_ack,
                write_started,
            )
        finally:
            if self._active_request_task is current:
                self._active_request_task = None
                self._active_request_deadline_at = None
            if acquired:
                lock.release()
            self._request_tasks.discard(current)

    @staticmethod
    def _infer_request_kind(command: str) -> DbCoreRequestKind:
        read_only_commands = {
            "service.hello",
            "connection.test",
            "schema.list",
            "schema.inspect",
            "schema.diff",
            "migration.plan",
            "migration.verify",
            "oneclick.derive_charset_contracts",
        }
        if command in read_only_commands:
            return DbCoreRequestKind.READ_ONLY
        return DbCoreRequestKind.MUTATION

    def _invoke_callback(
        self,
        payload: Dict[str, Any],
        on_event: Callable[[Dict[str, Any]], None],
        request_kind: DbCoreRequestKind,
        request_result: Optional[DbCoreRequestResult],
        request_error: Optional[DbCoreServiceError],
    ) -> None:
        try:
            on_event(payload)
        except BaseException as exc:
            if request_result is not None:
                outcome = request_result.outcome
            elif request_error is not None:
                outcome = request_error.outcome
            else:
                outcome = self._transport_outcome(request_kind)
            raise DbCoreCallbackError(
                exc,
                request_kind=request_kind,
                outcome=outcome,
                request_result=request_result,
            ) from exc

    def request_result(
        self,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        request_kind: DbCoreRequestKind,
        request_id: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout_seconds: Optional[float] = None,
        required_generation: Optional[int] = None,
    ) -> DbCoreRequestResult:
        if not isinstance(request_kind, DbCoreRequestKind):
            request_kind = DbCoreRequestKind(request_kind)
        timeout = self._validated_timeout(timeout_seconds, DEFAULT_REQUEST_TIMEOUT_SECONDS)
        deadline_at = self._monotonic() + timeout
        work_cutoff = self._cleanup_start(deadline_at, timeout)
        request_id = request_id or f"py-{uuid.uuid4().hex}"
        events: "queue.Queue[_CallbackDelivery]" = queue.Queue(
            maxsize=(1 if on_event is not None else 0)
        )
        write_started = threading.Event()
        future = self._submit_admitted(
            self._request_on_owner(
                command,
                payload,
                request_id,
                request_kind,
                events,
                required_generation,
                work_cutoff,
                deadline_at,
                on_event is not None,
                write_started,
            ),
            request_kind,
            request_id,
            deadline_at,
        )
        result: Optional[DbCoreRequestResult] = None
        request_error: Optional[DbCoreServiceError] = None
        deferred_events: List[_CallbackDelivery] = []
        timed_out = False
        if on_event is not None:
            while not future.done():
                remaining = self._remaining(deadline_at)
                if remaining <= 0.0:
                    timed_out = True
                    break
                try:
                    delivery = events.get(
                        timeout=min(0.05, remaining),
                    )
                except queue.Empty:
                    continue
                if delivery.is_terminal:
                    deferred_events.append(delivery)
                else:
                    try:
                        self._invoke_callback(
                            delivery.payload,
                            on_event,
                            request_kind,
                            None,
                            None,
                        )
                    except DbCoreCallbackError as callback_error:
                        delivery.callback_error = callback_error.cause
                        if delivery.ack is not None:
                            delivery.ack.set()
                        try:
                            future.result(timeout=self._remaining(deadline_at))
                        except BaseException:
                            pass
                        raise
                    else:
                        if delivery.ack is not None:
                            delivery.ack.set()

        try:
            if timed_out:
                raise concurrent.futures.TimeoutError()
            result = future.result(timeout=self._remaining(deadline_at))
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            request_error = DbCoreServiceError(
                "DB Core request exceeded its absolute deadline",
                code="db_core_timeout",
                request_kind=request_kind,
                outcome=(
                    self._transport_outcome(request_kind)
                    if write_started.is_set()
                    else DbCoreOutcome.NOT_STARTED
                ),
                request_id=request_id,
                process_generation=self._process_generation,
            )
            request_error.__cause__ = exc
        except concurrent.futures.CancelledError as exc:
            request_error = DbCoreServiceError(
                "DB Core request was cancelled by client shutdown",
                code="db_core_cleanup_failed",
                request_kind=request_kind,
                outcome=(
                    self._transport_outcome(request_kind)
                    if write_started.is_set()
                    else DbCoreOutcome.NOT_STARTED
                ),
                request_id=request_id,
                process_generation=self._process_generation,
            )
            request_error.__cause__ = exc
        except DbCoreServiceError as exc:
            request_error = exc

        if on_event is not None:
            while True:
                try:
                    delivery = events.get_nowait()
                except queue.Empty:
                    break
                deferred_events.append(delivery)
            for delivery in deferred_events:
                self._invoke_callback(
                    delivery.payload,
                    on_event,
                    request_kind,
                    result,
                    request_error,
                )
        if request_error is not None:
            raise request_error
        assert result is not None
        return result

    def request_payload(
        self,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        request_kind: Optional[DbCoreRequestKind] = None,
        request_id: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout_seconds: Optional[float] = None,
        required_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        result = self.request_result(
            command,
            payload,
            request_kind=request_kind or self._infer_request_kind(command),
            request_id=request_id,
            on_event=on_event,
            timeout_seconds=timeout_seconds,
            required_generation=required_generation,
        )
        return dict(result.payload)

    def request(
        self,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        *,
        request_kind: Optional[DbCoreRequestKind] = None,
        timeout_seconds: Optional[float] = None,
        required_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.request_payload(
            command,
            payload,
            request_kind=request_kind,
            request_id=request_id,
            on_event=on_event,
            timeout_seconds=timeout_seconds,
            required_generation=required_generation,
        )

    @staticmethod
    def _attach_cleanup_error(
        primary_error: DbCoreServiceError,
        cleanup_error: DbCoreServiceError,
    ) -> None:
        primary_error.cleanup_error = cleanup_error
        payload = dict(primary_error.payload)
        payload["cleanup_error"] = {
            "code": cleanup_error.code,
            "message": cleanup_error.message,
            "outcome": cleanup_error.outcome.value,
            "process_generation": cleanup_error.process_generation,
            "payload": dict(cleanup_error.payload),
        }
        primary_error.payload = payload

    async def _poison_and_reap_preserving_on_owner(
        self,
        primary_error: DbCoreServiceError,
        deadline_at: float,
    ) -> None:
        try:
            await self._poison_and_reap_on_owner(deadline_at)
        except BaseException as cleanup_exception:
            if isinstance(cleanup_exception, DbCoreServiceError):
                cleanup_error = cleanup_exception
            else:
                cleanup_error = DbCoreServiceError(
                    f"DB Core cleanup failed: {type(cleanup_exception).__name__}: "
                    f"{cleanup_exception}",
                    code="db_core_cleanup_failed",
                    outcome=DbCoreOutcome.FAILED,
                    process_generation=self._process_generation,
                )
                cleanup_error.__cause__ = cleanup_exception
            self._attach_cleanup_error(primary_error, cleanup_error)

    async def _poison_and_reap_on_owner(self, deadline_at: float) -> None:
        if self._generation_state in (
            DbCoreGenerationState.CREATING,
            DbCoreGenerationState.ACTIVE,
        ):
            self._transition_generation(DbCoreGenerationState.POISONED)
        await self._terminate_process_on_owner(deadline_at)

    async def _settle_spawn_on_owner(self, deadline_at: float) -> None:
        spawn_task = self._spawn_task
        if spawn_task is None:
            return
        native_spawn = self._spawn_is_native
        if not spawn_task.done():
            spawn_task.cancel()
            # Let cancellation settle before enforcing a possibly exhausted deadline.
            await asyncio.sleep(0)
        try:
            process = await self._await_before(
                asyncio.shield(spawn_task),
                deadline_at,
            )
        except asyncio.CancelledError as exc:
            self._spawn_task = None
            self._spawn_is_native = False
            if not native_spawn:
                return
            error = self._residual_process_error(
                "spawn_identity",
                "Native DB Core spawn cancellation returned no process identity",
                pending_tasks=["native_spawn:cancelled_without_identity"],
            )
            self._spawn_residual = dict(error.payload)
            raise error from exc
        except asyncio.TimeoutError as exc:
            error = self._residual_process_error(
                "spawn_wait",
                "DB Core spawn task resisted bounded cancellation",
                pending_tasks=[self._task_diagnostic(spawn_task)],
            )
            self._spawn_residual = dict(error.payload)
            raise error from exc
        except Exception:
            self._spawn_task = None
            self._spawn_is_native = False
            return
        self._spawn_task = None
        self._spawn_is_native = False
        if process is not None and self._process is None:
            self._process = process

    def _raise_spawn_residual_on_owner(self) -> None:
        if self._spawn_residual is None:
            return
        payload = dict(self._spawn_residual)
        raise DbCoreServiceError(
            "DB Core native spawn residual remains unresolved",
            code="db_core_residual_process",
            outcome=DbCoreOutcome.FAILED,
            process_generation=self._process_generation,
            payload=payload,
        )

    async def _terminate_process_on_owner(self, deadline_at: float) -> None:
        self._raise_spawn_residual_on_owner()
        if (
            self._process is None
            and self._spawn_task is None
            and self._generation_state is DbCoreGenerationState.CLOSED
        ):
            return
        if self._generation_state is DbCoreGenerationState.CREATING:
            self._transition_generation(DbCoreGenerationState.POISONED)
        if self._generation_state in (
            DbCoreGenerationState.ACTIVE,
            DbCoreGenerationState.POISONED,
        ):
            self._transition_generation(DbCoreGenerationState.REAPING)
        await self._settle_spawn_on_owner(deadline_at)
        process = self._process
        if process is None:
            self._transition_generation(DbCoreGenerationState.CLOSED)
            return
        stderr_task = self._stderr_task
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            await asyncio.sleep(0)

        stdin = getattr(process, "stdin", None)
        if stdin is not None:
            close = getattr(stdin, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            wait_closed = getattr(stdin, "wait_closed", None)
            if callable(wait_closed):
                try:
                    pending_close = wait_closed()
                    if inspect.isawaitable(pending_close):
                        close_cutoff = min(
                            deadline_at,
                            self._monotonic()
                            + min(0.1, self._remaining(deadline_at) / 4.0),
                        )
                        await self._await_before(pending_close, close_cutoff)
                except (asyncio.TimeoutError, Exception):
                    pass

        needs_kill = False
        if self._process_is_running_on_owner(process):
            try:
                process.terminate()
            except Exception:
                needs_kill = True

        wait = getattr(process, "wait", None)
        if not callable(wait):
            if self._process_factory is None or self._process_is_running_on_owner(process):
                raise self._residual_process_error(
                    "wait_unavailable",
                    "DB Core process has no wait handle to prove reap",
                    process=process,
                )
        else:
            if not needs_kill:
                try:
                    pending_wait = wait()
                    if inspect.isawaitable(pending_wait):
                        graceful_wait_cutoff = self._monotonic() + (
                            self._remaining(deadline_at) / 2.0
                        )
                        await self._settle_process_wait_before(
                            pending_wait,
                            graceful_wait_cutoff,
                        )
                except asyncio.TimeoutError:
                    needs_kill = True
            if needs_kill:
                kill = getattr(process, "kill", None)
                if not callable(kill):
                    raise self._residual_process_error(
                        "kill",
                        "DB Core process cannot be killed after terminate refusal",
                        process=process,
                    )
                try:
                    kill()
                except Exception as exc:
                    raise self._residual_process_error(
                        "kill",
                        f"DB Core process kill failed: {type(exc).__name__}: {exc}",
                        process=process,
                    ) from exc
                try:
                    pending_wait = wait()
                    if inspect.isawaitable(pending_wait):
                        await self._settle_process_wait_before(
                            pending_wait,
                            deadline_at,
                        )
                except asyncio.TimeoutError as exc:
                    raise self._residual_process_error(
                        "final_wait",
                        "DB Core process remained alive after bounded kill",
                        process=process,
                    ) from exc
        if stderr_task is not None and not stderr_task.done():
            try:
                await self._await_before(asyncio.shield(stderr_task), deadline_at)
            except asyncio.CancelledError:
                if not stderr_task.done():
                    raise
            except asyncio.TimeoutError as exc:
                raise self._residual_process_error(
                    "stderr_drain",
                    "DB Core stderr task resisted bounded cancellation",
                    process=process,
                    pending_tasks=[self._task_diagnostic(stderr_task)],
                ) from exc
        self._stderr_task = None
        self._process = None
        self._transition_generation(DbCoreGenerationState.CLOSED)

    async def _cancel_active_on_owner(self, deadline_at: float) -> bool:
        task = self._active_request_task
        if task is None or task.done():
            return False
        active_deadline = self._active_request_deadline_at or deadline_at
        cleanup_deadline = self._cleanup_deadline_on_owner(
            active_deadline,
            deadline_at,
        )
        if self._generation_state in (
            DbCoreGenerationState.CREATING,
            DbCoreGenerationState.ACTIVE,
        ):
            self._transition_generation(DbCoreGenerationState.POISONED)
        task.cancel()
        settlement_error: Optional[BaseException] = None
        try:
            await self._settle_cancelled_task_before(task, cleanup_deadline)
        except asyncio.CancelledError as exc:
            if not task.done():
                settlement_error = exc
        except DbCoreServiceError:
            pass
        except asyncio.TimeoutError as exc:
            settlement_error = self._residual_process_error(
                "request_cancel",
                "DB Core active request did not cancel before the deadline",
                pending_tasks=[self._task_diagnostic(task)],
            )
            settlement_error.__cause__ = exc
        except BaseException as exc:
            settlement_error = exc
        try:
            await self._terminate_process_on_owner(cleanup_deadline)
        except BaseException as cleanup_exception:
            if isinstance(settlement_error, DbCoreServiceError):
                if isinstance(cleanup_exception, DbCoreServiceError):
                    cleanup_error = cleanup_exception
                else:
                    cleanup_error = DbCoreServiceError(
                        f"DB Core cleanup failed: {type(cleanup_exception).__name__}: "
                        f"{cleanup_exception}",
                        code="db_core_cleanup_failed",
                        outcome=DbCoreOutcome.FAILED,
                        process_generation=self._process_generation,
                    )
                    cleanup_error.__cause__ = cleanup_exception
                self._attach_cleanup_error(settlement_error, cleanup_error)
                raise settlement_error
            if settlement_error is not None:
                raise cleanup_exception from settlement_error
            raise
        if settlement_error is not None:
            raise settlement_error
        return True

    def cancel_active_request(
        self,
        *,
        timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> bool:
        timeout = self._validated_timeout(timeout_seconds, DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)
        deadline_at = self._monotonic() + timeout
        owner_deadline_at = self._cleanup_start(deadline_at, timeout)
        remaining = self._remaining(deadline_at)
        if remaining <= 0.0 or not self._admission_lock.acquire(timeout=remaining):
            raise DbCoreServiceError(
                "DB Core active request cancellation admission exceeded its deadline",
                code="db_core_residual_process",
                outcome=DbCoreOutcome.FAILED,
                process_generation=self._process_generation,
            )
        try:
            if self._shutdown_started:
                return False
            future = self._submit_owner(
                self._cancel_active_on_owner(owner_deadline_at),
                DbCoreRequestKind.MUTATION,
                "cancel-active",
            )
        finally:
            self._admission_lock.release()
        try:
            return bool(future.result(timeout=self._remaining(deadline_at)))
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise DbCoreServiceError(
                "DB Core active request cancellation exceeded its deadline",
                code="db_core_residual_process",
                outcome=DbCoreOutcome.FAILED,
                process_generation=self._process_generation,
            ) from exc
        except concurrent.futures.CancelledError as exc:
            raise DbCoreServiceError(
                "DB Core active request cancellation was interrupted by shutdown",
                code="db_core_residual_process",
                outcome=DbCoreOutcome.FAILED,
                process_generation=self._process_generation,
            ) from exc

    async def _shutdown_on_owner(self, deadline_at: float) -> None:
        current = asyncio.current_task()
        assert current is not None
        queued_requests = [
            task
            for task in self._request_tasks
            if task is not self._active_request_task and not task.done()
        ]
        for task in queued_requests:
            task.cancel()
        cancelled_active = await self._cancel_active_on_owner(deadline_at)
        if not cancelled_active:
            await self._terminate_process_on_owner(deadline_at)
        if queued_requests:
            _, queued_pending = await asyncio.wait(
                queued_requests,
                timeout=self._remaining(deadline_at),
            )
            if queued_pending:
                raise self._residual_process_error(
                    "task_drain",
                    "DB Core queued requests did not cancel before shutdown deadline",
                    pending_tasks=[
                        self._task_diagnostic(task)
                        for task in sorted(queued_pending, key=id)
                    ],
                )
        pending = [
            task
            for task in asyncio.all_tasks(self.owner_loop)
            if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            _, still_pending = await asyncio.wait(
                pending,
                timeout=self._remaining(deadline_at),
            )
            if still_pending:
                raise self._residual_process_error(
                    "task_drain",
                    "DB Core owner tasks did not drain before shutdown deadline",
                    pending_tasks=[
                        self._task_diagnostic(task)
                        for task in sorted(still_pending, key=id)
                    ],
                )

    def shutdown(
        self,
        *,
        timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        timeout = self._validated_timeout(timeout_seconds, DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)
        deadline_at = self._monotonic() + timeout
        owner_deadline_at = self._cleanup_start(deadline_at, timeout)
        remaining = self._remaining(deadline_at)
        if remaining <= 0.0 or not self._admission_lock.acquire(timeout=remaining):
            raise DbCoreServiceError(
                "DB Core shutdown admission exceeded its deadline",
                code="db_core_residual_process",
                outcome=DbCoreOutcome.FAILED,
                process_generation=self._process_generation,
            )
        try:
            self._shutdown_started = True
        finally:
            self._admission_lock.release()

        remaining = self._remaining(deadline_at)
        if remaining <= 0.0 or not self._shutdown_lock.acquire(timeout=remaining):
            raise DbCoreServiceError(
                "DB Core shutdown lock acquisition exceeded its deadline",
                code="db_core_residual_process",
                outcome=DbCoreOutcome.FAILED,
                process_generation=self._process_generation,
            )
        try:
            if self._shutdown_complete:
                return
            if not self._owner_thread.is_alive() and self._process is None:
                self._shutdown_complete = True
                return
            if not self._shutdown_owner_drained:
                try:
                    future = self._submit_owner(
                        self._shutdown_on_owner(owner_deadline_at),
                        DbCoreRequestKind.MUTATION,
                        "shutdown",
                    )
                    future.result(timeout=self._remaining(deadline_at))
                    self._shutdown_owner_drained = True
                except concurrent.futures.TimeoutError as exc:
                    future.cancel()
                    raise self._residual_process_error(
                        "shutdown_owner",
                        "DB Core owner shutdown exceeded its deadline",
                        pending_tasks=["shutdown"],
                    ) from exc
                except concurrent.futures.CancelledError as exc:
                    raise self._residual_process_error(
                        "shutdown_owner",
                        "DB Core owner shutdown task was cancelled",
                        pending_tasks=["shutdown"],
                    ) from exc

            loop = self._owner_loop
            if (
                not self._loop_stop_submitted
                and loop is not None
                and not loop.is_closed()
            ):
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except BaseException as exc:
                    raise self._residual_process_error(
                        "loop_stop",
                        f"DB Core owner loop stop failed: {type(exc).__name__}: {exc}",
                        extra={
                            "thread_name": self._owner_thread.name,
                            "thread_ident": self._owner_thread.ident,
                        },
                    ) from exc
                self._loop_stop_submitted = True
            self._owner_thread.join(timeout=self._remaining(deadline_at))
            if self._owner_thread.is_alive():
                raise self._residual_process_error(
                    "owner_join",
                    "DB Core owner thread remained alive after bounded join",
                    extra={
                        "thread_name": self._owner_thread.name,
                        "thread_ident": self._owner_thread.ident,
                    },
                )
            self._shutdown_complete = True
        finally:
            self._shutdown_lock.release()

    def __enter__(self) -> "DbCoreServiceClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.shutdown()
        return False
