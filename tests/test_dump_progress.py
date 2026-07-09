"""
dump_progress 모듈(src.exporters.dump_progress) 신규 경로 테스트
"""


def test_import_path_from_dump_progress_module():
    """새 경로에서 emit_core_event/TableProgressTracker/DumpEventCallbacks를 직접 import할 수 있다."""
    from src.exporters.dump_progress import DumpEventCallbacks, TableProgressTracker, emit_core_event

    assert emit_core_event is not None
    assert TableProgressTracker is not None
    assert DumpEventCallbacks is not None


def test_rust_dump_exporter_reexports_same_symbols():
    """rust_dump_exporter의 re-export가 dump_progress 모듈의 심볼과 동일 객체를 가리킨다."""
    from src.exporters.dump_progress import TableProgressTracker as CoreTracker
    from src.exporters.dump_progress import emit_core_event as core_emit_core_event
    from src.exporters.rust_dump_exporter import TableProgressTracker as ReexportedTracker
    from src.exporters.rust_dump_exporter import emit_core_event as reexported_emit_core_event

    assert CoreTracker is ReexportedTracker
    assert core_emit_core_event is reexported_emit_core_event


def test_dump_event_callbacks_defaults_to_none():
    """DumpEventCallbacks의 모든 필드는 기본값 None을 가진다."""
    from src.exporters.dump_progress import DumpEventCallbacks

    callbacks = DumpEventCallbacks()

    assert callbacks.progress is None
    assert callbacks.table_progress is None
    assert callbacks.detail is None
    assert callbacks.table_status is None
    assert callbacks.raw_output is None
    assert callbacks.metadata is None
    assert callbacks.table_chunk_progress is None


def test_emit_core_event_dispatches_dump_plan():
    """dump_plan 이벤트가 detail_callback으로 전달된다 (dispatch table 경유)."""
    from src.exporters.dump_progress import emit_core_event

    details = []
    emit_core_event(
        {"event": "dump_plan", "tables_total": 1, "rows_total": 10, "tables": []},
        detail_callback=details.append,
    )

    assert details == [{
        "event": "dump_plan",
        "tables_total": 1,
        "rows_total": 10,
        "tables": [],
    }]


def test_emit_core_event_ignores_unknown_event_type():
    """알 수 없는 event type은 어떤 콜백도 호출하지 않는다."""
    from src.exporters.dump_progress import emit_core_event

    calls = []
    emit_core_event(
        {"event": "unknown_event"},
        progress_callback=calls.append,
        detail_callback=calls.append,
    )

    assert calls == []


def test_emit_core_event_raw_output_forwarded_regardless_of_event_type():
    """raw_output_callback은 event type과 무관하게 항상 호출된다."""
    from src.exporters.dump_progress import emit_core_event

    raw_events = []
    emit_core_event(
        {"event": "unknown_event", "foo": "bar"},
        raw_output_callback=raw_events.append,
    )

    assert len(raw_events) == 1
    assert "unknown_event" in raw_events[0]
