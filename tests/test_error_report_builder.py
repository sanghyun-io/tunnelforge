import ast
import json
import re
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from src.core.error_report_builder import build_error_report
from src.core.error_report_environment import collect_environment
from src.core.error_report_schema import ReportValidationError, validate_report_payload


class MemoryConfigManager:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.set_calls = []

    def get_app_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_app_setting(self, key, value):
        self.set_calls.append((key, value))
        self.settings[key] = value


class CodedError(RuntimeError):
    def __init__(self, message, code=" dump_import_failed "):
        super().__init__(message)
        self.code = code


CodedError.__module__ = "src.core.synthetic_worker"


@pytest.fixture
def config_manager():
    return MemoryConfigManager()


@pytest.fixture
def fixed_environment():
    return {
        "app": {
            "version": "2.3.1",
            "package_kind": "source",
            "ui_language": "en",
        },
        "system": {
            "os_family": "linux",
            "os_version": "6.8.0",
            "architecture": "x86_64",
            "locale": "en_US",
            "utc_offset_minutes": 0,
        },
        "runtime": {
            "python_version": "3.12.4",
            "qt_version": "6.7.2",
            "rust_core_version": "2.3.1",
        },
    }


def _raise_from_module(module_name="src.core.synthetic_worker", depth=1):
    lines = []
    for index in range(depth):
        next_call = f"frame_{index + 1}()" if index + 1 < depth else (
            "raise CodedError('synthetic failure', ' dump_import_failed ')"
        )
        lines.extend([f"def frame_{index}():", f"    {next_call}"])
    namespace = {"__name__": module_name, "CodedError": CodedError}
    code = compile("\n".join(lines), r"C:\Users\Secret\private_worker.py", "exec")
    exec(code, namespace)
    try:
        namespace["frame_0"]()
    except CodedError as exc:
        return exc
    raise AssertionError("synthetic exception was not raised")


def _raise_mixed_traceback():
    inner_namespace = {
        "__name__": "src.core.synthetic_inner",
        "CodedError": CodedError,
    }
    outer_namespace = {"__name__": "src.core.synthetic_outer"}
    external_namespace = {"__name__": "external.synthetic_bridge"}
    exec(
        compile(
            "def app_inner():\n    raise CodedError('failure')",
            "/home/alice/private_inner.py",
            "exec",
        ),
        inner_namespace,
    )
    exec(
        compile(
            "def app_outer(external, inner):\n    external(inner)",
            "/home/alice/private_outer.py",
            "exec",
        ),
        outer_namespace,
    )
    exec(
        compile(
            "def external_middle(inner):\n    inner()",
            "/opt/vendor/private_bridge.py",
            "exec",
        ),
        external_namespace,
    )
    try:
        outer_namespace["app_outer"](
            external_namespace["external_middle"],
            inner_namespace["app_inner"],
        )
    except CodedError as exc:
        return exc
    raise AssertionError("synthetic exception was not raised")


def _build(monkeypatch, config_manager, fixed_environment, **overrides):
    monkeypatch.setattr(
        "src.core.error_report_builder.collect_environment",
        lambda: fixed_environment,
    )
    arguments = {
        "operation_kind": "export",
        "db_engine": "mysql",
        "phase": "dump.run",
        "error_message": "failure",
    }
    arguments.update(overrides)
    return build_error_report(config_manager, **arguments)


def test_builder_redacts_context_free_high_entropy_error_message(
    monkeypatch, config_manager, fixed_environment
):
    token = "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq"

    payload = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        error_message=f"request failed {token}",
    )

    assert token not in payload["error"]["sanitized_message"]
    assert "REDACTED" in payload["error"]["sanitized_message"]


def test_collector_returns_only_the_contract_allowlist(monkeypatch):
    import src.core.error_report_environment as environment

    monkeypatch.setattr(environment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(environment.platform, "release", lambda: "6.8.0-test")
    monkeypatch.setattr(environment.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(environment.locale, "getlocale", lambda: ("ko_KR", "UTF-8"))
    monkeypatch.setattr(environment, "QT_VERSION_STR", "6.7.2")
    monkeypatch.setattr(environment, "current_language", lambda: "ko")

    result = collect_environment()

    assert set(result) == {"app", "system", "runtime"}
    assert set(result["app"]) == {"version", "package_kind", "ui_language"}
    assert set(result["system"]) == {
        "os_family",
        "os_version",
        "architecture",
        "locale",
        "utc_offset_minutes",
    }
    assert set(result["runtime"]) == {
        "python_version",
        "qt_version",
        "rust_core_version",
    }
    assert result["system"]["os_family"] == "linux"
    assert result["system"]["architecture"] == "x86_64"
    assert result["system"]["locale"] == "ko_KR"
    assert result["app"]["ui_language"] == "ko"
    assert all(
        re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", result["runtime"][field])
        for field in result["runtime"]
    )


def test_collector_uses_no_host_hardware_network_subprocess_or_database_probe(
    monkeypatch,
):
    import src.core.db_core_facade as db_core_facade
    import src.core.error_report_environment as environment

    forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("forbidden probe")
    )
    monkeypatch.setattr(environment.platform, "node", forbidden)
    monkeypatch.setattr(environment.platform, "processor", forbidden)
    monkeypatch.setattr(socket, "gethostname", forbidden)
    monkeypatch.setattr(socket, "gethostbyname", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(db_core_facade, "get_shared_db_core_facade", forbidden)

    serialized = json.dumps(collect_environment(), sort_keys=True)

    for forbidden_value in (
        "SyntheticUser",
        "synthetic-host",
        "192.0.2.44",
        "serial-number",
    ):
        assert forbidden_value not in serialized


def test_task2_modules_import_only_the_explicit_local_allowlist():
    root = Path(__file__).parents[1]
    allowed_imports = {
        "src/core/error_report_sanitizer.py": {"re", "unicodedata"},
        "src/core/error_report_environment.py": {
            "datetime",
            "locale",
            "platform",
            "PyQt6.QtCore",
            "re",
            "src.core.i18n",
            "src.version",
            "sys",
            "unicodedata",
        },
        "src/core/error_report_builder.py": {
            "hashlib",
            "json",
            "re",
            "src.core.error_report_environment",
            "src.core.error_report_sanitizer",
            "src.core.error_report_schema",
            "threading",
            "unicodedata",
            "uuid",
        },
    }

    for relative_path, allowed in allowed_imports.items():
        tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        assert imports == allowed, relative_path


def test_collector_never_forwards_a_custom_release_label(monkeypatch):
    import src.core.error_report_environment as environment

    monkeypatch.setattr(environment.platform, "release", lambda: "customer-alice-laptop")

    assert collect_environment()["system"]["os_version"] == "0.0"


def test_builder_generates_and_reuses_one_uuid_v4(
    monkeypatch, config_manager, fixed_environment
):
    first = _build(monkeypatch, config_manager, fixed_environment)
    second = _build(monkeypatch, config_manager, fixed_environment)

    installation_id = first["report"]["anonymous_installation_id"]
    assert uuid.UUID(installation_id).version == 4
    assert second["report"]["anonymous_installation_id"] == installation_id
    assert config_manager.set_calls == [
        ("error_reporting_installation_id", installation_id)
    ]


def test_builder_generates_one_uuid_when_first_reports_are_concurrent(
    monkeypatch, fixed_environment
):
    class SlowFirstReadConfig(MemoryConfigManager):
        def get_app_setting(self, key, default=None):
            value = super().get_app_setting(key, default)
            if value is None:
                time.sleep(0.05)
            return value

    config = SlowFirstReadConfig()
    monkeypatch.setattr(
        "src.core.error_report_builder.collect_environment",
        lambda: fixed_environment,
    )
    start = threading.Barrier(3)
    reports = []

    def build():
        start.wait()
        reports.append(
            build_error_report(
                config,
                operation_kind="export",
                db_engine="mysql",
                phase="dump.run",
                error_message="failure",
            )
        )

    threads = [threading.Thread(target=build) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    installation_ids = {
        report["report"]["anonymous_installation_id"] for report in reports
    }
    assert len(installation_ids) == 1
    assert len(config.set_calls) == 1


@pytest.mark.parametrize(
    "stored_id",
    [
        "not-a-canonical-uuid-v4",
        "00000000-0000-4000-8000-000000000000",
        "550E8400-E29B-41D4-A716-446655440000",
    ],
)
def test_builder_replaces_an_invalid_stored_installation_id(
    monkeypatch, fixed_environment, stored_id
):
    config = MemoryConfigManager(
        {"error_reporting_installation_id": stored_id}
    )

    report = _build(monkeypatch, config, fixed_environment)

    installation_id = report["report"]["anonymous_installation_id"]
    assert uuid.UUID(installation_id).version == 4
    assert config.settings["error_reporting_installation_id"] == installation_id


@pytest.mark.parametrize(
    ("raw_version", "expected"),
    [
        ("8.0.36", "8.0"),
        ("16.3 (synthetic build)", "16.3"),
        ((10, 11, 6), "10.11"),
    ],
)
def test_builder_reduces_known_database_version_to_major_minor(
    monkeypatch, config_manager, fixed_environment, raw_version, expected
):
    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        db_server_version=raw_version,
    )

    assert report["operation"]["db_server_version"] == expected


def test_builder_omits_database_version_when_it_has_no_numeric_pair(
    monkeypatch, config_manager, fixed_environment
):
    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        db_server_version="unknown private build",
    )

    assert "db_server_version" not in report["operation"]


@pytest.mark.parametrize("raw_version", [(-1, 4), (8.5, 1), (True, 2)])
def test_builder_omits_noncanonical_tuple_database_versions(
    monkeypatch, config_manager, fixed_environment, raw_version
):
    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        db_server_version=raw_version,
    )

    assert "db_server_version" not in report["operation"]


def test_builder_includes_only_normalized_application_traceback_frames(
    monkeypatch, config_manager, fixed_environment
):
    exception = _raise_from_module(depth=25)

    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        error_message=str(exception),
        exception=exception,
    )

    frames = report["error"]["app_frames"]
    assert len(frames) == 20
    assert all(set(frame) == {"module", "function", "line"} for frame in frames)
    assert all(frame["module"].startswith("src.") for frame in frames)
    assert all(frame["line"] > 0 for frame in frames)
    assert "Secret" not in json.dumps(frames)
    assert report["error"]["error_code"] == "DUMP_IMPORT_FAILED"


def test_builder_discards_non_application_traceback_frames(
    monkeypatch, config_manager, fixed_environment
):
    exception = _raise_from_module("external.plugin")

    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        exception=exception,
    )

    assert report["error"]["app_frames"] == []


def test_builder_rejects_exception_metadata_from_non_application_modules(
    monkeypatch, config_manager, fixed_environment
):
    external_type = type(
        "Failure",
        (RuntimeError,),
        {"__module__": "tenant_alice_example"},
    )
    exception = external_type("failure")
    exception.code = "CUSTOMER_ALICE_123"

    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        exception=exception,
    )

    assert report["error"]["exception_class"] == "Exception"
    assert "error_code" not in report["error"]


def test_builder_never_invokes_hostile_exception_attribute_hooks(
    monkeypatch, config_manager, fixed_environment
):
    accessed = []

    class HostileAttributeAccess(BaseException):
        pass

    class HostileError(RuntimeError):
        def __getattribute__(self, name):
            accessed.append(name)
            raise HostileAttributeAccess("hostile attribute access")

    HostileError.__module__ = "src.core.synthetic_worker"
    exception = HostileError("failure")
    try:
        raise exception
    except HostileError as caught:
        exception = caught

    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        exception=exception,
    )

    assert accessed == []
    assert report["error"]["exception_class"] == (
        "src.core.synthetic_worker.HostileError"
    )
    assert "error_code" not in report["error"]


def test_builder_fails_closed_when_exception_class_metadata_is_hostile(
    monkeypatch, config_manager, fixed_environment
):
    class HostileClassMetadata(BaseException):
        pass

    class HostileMeta(type):
        def __getattribute__(cls, name):
            if name in {"__name__", "__module__"}:
                raise HostileClassMetadata("hostile class metadata")
            return super().__getattribute__(name)

    class HostileClassError(RuntimeError, metaclass=HostileMeta):
        pass

    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        exception=HostileClassError("failure"),
    )

    assert report["error"]["exception_class"] == "Exception"
    assert "error_code" not in report["error"]
    assert report["error"]["app_frames"] == []


def test_fingerprint_is_sha256_and_excludes_message_and_installation_id(
    monkeypatch, fixed_environment
):
    first_config = MemoryConfigManager()
    second_config = MemoryConfigManager()
    first_exception = _raise_from_module()
    second_exception = _raise_from_module()

    first = _build(
        monkeypatch,
        first_config,
        fixed_environment,
        error_message="first message",
        exception=first_exception,
    )
    second = _build(
        monkeypatch,
        second_config,
        fixed_environment,
        error_message="completely different message",
        exception=second_exception,
    )

    first_fingerprint = first["report"]["error_fingerprint"]
    assert re.fullmatch(r"[0-9a-f]{64}", first_fingerprint)
    assert first_fingerprint == second["report"]["error_fingerprint"]
    assert (
        first["report"]["anonymous_installation_id"]
        != second["report"]["anonymous_installation_id"]
    )


def test_fingerprint_changes_with_a_specified_canonical_field(
    monkeypatch, config_manager, fixed_environment
):
    exception = _raise_from_module()
    export_report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        exception=exception,
    )
    import_report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        operation_kind="import",
        db_engine="postgresql",
        phase="dump.import",
        exception=exception,
    )

    assert (
        export_report["report"]["error_fingerprint"]
        != import_report["report"]["error_fingerprint"]
    )


def test_fingerprint_has_a_fixed_canonical_json_vector(
    monkeypatch, config_manager, fixed_environment
):
    report = _build(monkeypatch, config_manager, fixed_environment)

    assert report["report"]["error_fingerprint"] == (
        "89f9596300ecce6534542780e6960c23e52742ca268e1ec2f0914f78cf78f8bc"
    )


def test_fingerprint_has_a_fixed_mixed_traceback_vector(
    monkeypatch, config_manager, fixed_environment
):
    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        exception=_raise_mixed_traceback(),
    )

    assert report["error"]["app_frames"] == [
        {"module": "src.core.synthetic_outer", "function": "app_outer", "line": 2},
        {"module": "src.core.synthetic_inner", "function": "app_inner", "line": 2},
    ]
    assert report["report"]["error_fingerprint"] == (
        "e21dd0c10e819627a76cccd0664db74b11d395e81728449c9d6e44c146043a88"
    )


def test_fingerprint_excludes_phase_but_includes_engine(
    monkeypatch, config_manager, fixed_environment
):
    baseline = _build(monkeypatch, config_manager, fixed_environment)
    phase_changed = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        phase="dump.import",
    )
    engine_changed = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        db_engine="postgresql",
    )

    baseline_fingerprint = baseline["report"]["error_fingerprint"]
    assert phase_changed["report"]["error_fingerprint"] == baseline_fingerprint
    assert engine_changed["report"]["error_fingerprint"] != baseline_fingerprint


def test_fingerprint_includes_exception_class_code_and_frame_signature(
    monkeypatch, config_manager, fixed_environment
):
    first = _raise_from_module(depth=1)
    changed_code = _raise_from_module(depth=1)
    changed_code.code = "OTHER_CODE"
    changed_frames = _raise_from_module(depth=2)
    other_type = type(
        "OtherError",
        (RuntimeError,),
        {"__module__": "src.core.synthetic_worker"},
    )
    changed_class = other_type("failure")

    reports = [
        _build(
            monkeypatch,
            config_manager,
            fixed_environment,
            exception=exception,
        )
        for exception in (first, changed_code, changed_frames, changed_class)
    ]
    fingerprints = [report["report"]["error_fingerprint"] for report in reports]

    assert len(set(fingerprints)) == 4


def test_builder_sanitizes_the_message_and_returns_schema_valid_payload(
    monkeypatch, config_manager, fixed_environment
):
    report = _build(
        monkeypatch,
        config_manager,
        fixed_environment,
        error_message=(
            "password=SyntheticPassword123! at 192.0.2.44 "
            "for C:\\Users\\SyntheticUser\\dump.sql"
        ),
    )

    serialized = json.dumps(report)
    assert "SyntheticPassword123!" not in serialized
    assert "192.0.2.44" not in serialized
    assert "SyntheticUser" not in serialized
    assert validate_report_payload(report) == report


def test_builder_delegates_final_payload_to_runtime_validator(
    monkeypatch, config_manager, fixed_environment
):
    calls = []

    def validate(payload):
        calls.append(payload)
        return validate_report_payload(payload)

    monkeypatch.setattr(
        "src.core.error_report_builder.validate_report_payload", validate
    )

    report = _build(monkeypatch, config_manager, fixed_environment)

    assert calls == [report]


def test_builder_rejects_values_outside_the_contract(
    monkeypatch, config_manager, fixed_environment
):
    with pytest.raises(ReportValidationError):
        _build(
            monkeypatch,
            config_manager,
            fixed_environment,
            operation_kind="migrate",
        )


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
