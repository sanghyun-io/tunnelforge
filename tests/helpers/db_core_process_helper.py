"""Real child-process modes for DB Core lifecycle integration tests."""
import argparse
import json
import os
import signal
import sys
import time


MAX_JSONL_FRAME_BYTES = 1_048_576
MAX_ASSEMBLED_EVENT_BYTES = 64 * 1024 * 1024
MAX_ASSEMBLED_EVENT_CHUNKS = 4_096
MAX_ASSEMBLED_EVENT_NODES = 65_536
MAX_ASSEMBLED_EVENT_DEPTH = 128
PROCESS_CAPABILITIES = [
    "mutation.outcome_indeterminate",
    "process.generation",
    "request.deadline",
    "request.strict_id",
]


def _append_state(path, value):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(value + "\n")
        handle.flush()


def _write_event(event):
    encoded = (
        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_JSONL_FRAME_BYTES:
        raise RuntimeError("helper attempted an oversized normal frame")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _hello(request_id):
    return {
        "event": "result",
        "request_id": request_id,
        "command": "service.hello",
        "success": True,
        "service": "tunnelforge-core",
        "protocol_version": 1,
        "process_version": 1,
        "process_capabilities": PROCESS_CAPABILITIES,
        "max_jsonl_frame_bytes": MAX_JSONL_FRAME_BYTES,
        "max_assembled_event_bytes": MAX_ASSEMBLED_EVENT_BYTES,
        "max_assembled_event_chunks": MAX_ASSEMBLED_EVENT_CHUNKS,
        "max_assembled_event_nodes": MAX_ASSEMBLED_EVENT_NODES,
        "max_assembled_event_depth": MAX_ASSEMBLED_EVENT_DEPTH,
        "capabilities": ["service.shutdown"],
    }


def _read_request():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def _stall():
    while True:
        time.sleep(0.05)


def _install_signal_probe(state_path, ignore_term):
    if not hasattr(signal, "SIGTERM"):
        return

    def handle_term(signum, _frame):
        _append_state(state_path, "SIGNAL {} {}".format(os.getpid(), signum))
        if not ignore_term:
            raise SystemExit(128 + int(signum))

    signal.signal(signal.SIGTERM, handle_term)


def _emit_near_limit(request, state_path):
    event = {
        "event": "result",
        "request_id": request["request_id"],
        "command": request["command"],
        "success": True,
        "value": "x" * (MAX_JSONL_FRAME_BYTES + 257),
    }
    _write_chunked_event(
        event,
        state_path,
        text_chars=MAX_JSONL_FRAME_BYTES - 1_024,
    )


def _chunk_frames(event, text_chars=120_000):
    request_id = event["request_id"]
    command = event["command"]
    logical_event = event["event"]
    frames = []
    next_node_id = [0]

    def add_frame(node_id, parent_id, slot_index, sequence, final, value_kind, payload):
        frame = {
            "event": "payload_chunk",
            "request_id": request_id,
            "command": command,
            "logical_event": logical_event,
            "node_id": node_id,
            "parent_node_id": parent_id,
            "slot_index": slot_index,
            "sequence": sequence,
            "final": final,
            "value_kind": value_kind,
        }
        frame.update(payload)
        encoded = (
            json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_JSONL_FRAME_BYTES:
            raise RuntimeError("chunk frame exceeds wire cap")
        frames.append(encoded)

    def visit(value, parent_id=None, slot_index=None):
        node_id = next_node_id[0]
        next_node_id[0] += 1
        if isinstance(value, dict):
            items = []
            for index, pair in enumerate(value.items()):
                key, child = pair
                key_id = visit(key, node_id, index)
                value_id = visit(child, node_id, index)
                items.append({"key_node_id": key_id, "value_node_id": value_id})
            add_frame(
                node_id,
                parent_id,
                slot_index,
                0,
                True,
                "object",
                {"items": items},
            )
        elif isinstance(value, str):
            chunks = [
                value[index:index + text_chars]
                for index in range(0, len(value), text_chars)
            ] or [""]
            for sequence, chunk in enumerate(chunks):
                add_frame(
                    node_id,
                    parent_id,
                    slot_index,
                    sequence,
                    sequence == len(chunks) - 1,
                    "utf8_string",
                    {"text": chunk},
                )
        else:
            add_frame(
                node_id,
                parent_id,
                slot_index,
                0,
                True,
                "atomic",
                {"items": [value]},
            )
        return node_id

    if visit(event) != 0:
        raise RuntimeError("root node id changed")
    return frames


def _write_chunked_event(event, state_path, text_chars=120_000):
    for frame in _chunk_frames(event, text_chars=text_chars):
        _append_state(
            state_path,
            "CHUNK_FRAME {} {}".format(os.getpid(), len(frame)),
        )
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()


def _emit_oversized_scalar(request, state_path):
    event = {
        "event": "result",
        "request_id": request["request_id"],
        "command": request["command"],
        "success": True,
        "value": "\U0001f642" * 300_000,
    }
    _write_chunked_event(event, state_path)


def _emit_malicious_frame(request):
    prefix = (
        '{{"event":"result","request_id":{},"command":{},'
        '"success":true,"value":"'
    ).format(
        json.dumps(request["request_id"]),
        json.dumps(request["command"]),
    ).encode("utf-8")
    frame = prefix + (b"z" * MAX_JSONL_FRAME_BYTES) + b'"}\n'
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


def _first_failure(marker_path):
    try:
        descriptor = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(descriptor)
    return True


def _emit_post_side_effect_encode_failure(request, state_path):
    _write_event({
        "event": "phase",
        "request_id": request["request_id"],
        "command": request["command"],
        "phase": "mutation",
        "message": "side effect started",
    })
    try:
        _write_event({
            "event": "result",
            "request_id": request["request_id"],
            "command": request["command"],
            "success": True,
            "value": object(),
        })
    except TypeError:
        _append_state(
            state_path,
            "ENCODE_FAILURE {} {}".format(os.getpid(), request["request_id"]),
        )
        os._exit(71)
    _append_state(
        state_path,
        "FRAME_AFTER_ENCODE_FAILURE {} {}".format(
            os.getpid(),
            request["request_id"],
        ),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "stall",
            "no-read",
            "near-limit",
            "oversized-scalar",
            "malicious-frame",
            "post-side-effect-encode-failure-once",
        ),
    )
    parser.add_argument("--state-file", default="")
    parser.add_argument("--marker-file", default="")
    args = parser.parse_args()

    _install_signal_probe(args.state_file, args.mode == "no-read")
    _append_state(args.state_file, "START {} {}".format(os.getpid(), args.mode))
    hello_request = _read_request()
    if hello_request is None or hello_request.get("command") != "service.hello":
        return 2
    _write_event(_hello(hello_request["request_id"]))

    if args.mode == "no-read":
        _append_state(args.state_file, "NO_READ_READY {}".format(os.getpid()))
        _stall()

    request = _read_request()
    if request is None:
        return 0
    _append_state(
        args.state_file,
        "REQUEST {} {} {}".format(
            os.getpid(),
            request.get("request_id", ""),
            request.get("command", ""),
        ),
    )

    if args.mode == "stall":
        _stall()
    if args.mode == "near-limit":
        _emit_near_limit(request, args.state_file)
        _stall()
    if args.mode == "oversized-scalar":
        _emit_oversized_scalar(request, args.state_file)
        _stall()
    if args.mode == "malicious-frame":
        _emit_malicious_frame(request)
        _stall()
    if args.mode == "post-side-effect-encode-failure-once":
        if _first_failure(args.marker_file):
            _emit_post_side_effect_encode_failure(request, args.state_file)
        _write_event({
            "event": "result",
            "request_id": request["request_id"],
            "command": request["command"],
            "success": True,
            "value": "fresh-generation",
        })
        _stall()
    return 3


if __name__ == "__main__":
    sys.exit(main())
