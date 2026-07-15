import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from src.core.error_report_schema import (
    ARCHITECTURES,
    DB_ENGINES,
    OPERATION_KINDS,
    OPERATION_PHASES,
    OS_FAMILIES,
    PACKAGE_KINDS,
    REPORT_SCHEMA_VERSION,
    ReportValidationError,
    validate_report_payload,
)


CONTRACT_DIR = (
    Path(__file__).parents[1] / "contracts" / "error-reporting" / "v1"
)


def _load_contract(name):
    with (CONTRACT_DIR / name).open(encoding="utf-8") as contract_file:
        return json.load(contract_file)


def _set_path(payload, path, value):
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


def _get_path(payload, path):
    target = payload
    for part in path:
        target = target[part]
    return target


def _schema_error_path(error):
    path = ""
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path or "root"


@pytest.fixture
def valid_payload():
    return _load_contract("valid-full.json")


@pytest.fixture(scope="module")
def schema_validator():
    schema = _load_contract("schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_contract_constants_are_versioned_and_explicit():
    assert REPORT_SCHEMA_VERSION == 1
    assert PACKAGE_KINDS == frozenset({"source", "frozen"})
    assert OS_FAMILIES == frozenset({"windows", "macos", "linux"})
    assert ARCHITECTURES == frozenset({"x86_64", "arm64"})
    assert OPERATION_KINDS == frozenset({"export", "import"})
    assert DB_ENGINES == frozenset({"mysql", "postgresql"})
    assert OPERATION_PHASES == frozenset({"dump.run", "dump.import"})


@pytest.mark.parametrize("fixture_name", ["valid-minimal.json", "valid-full.json"])
def test_shared_valid_fixtures_are_accepted(fixture_name):
    payload = _load_contract(fixture_name)

    assert validate_report_payload(payload) == payload


@pytest.mark.parametrize("fixture_name", ["valid-minimal.json", "valid-full.json"])
def test_json_schema_accepts_every_shared_valid_fixture(schema_validator, fixture_name):
    payload = _load_contract(fixture_name)

    errors = list(schema_validator.iter_errors(payload))

    assert not errors, [error.message for error in errors]


def test_validator_returns_a_new_canonical_dictionary(valid_payload):
    original = copy.deepcopy(valid_payload)

    validated = validate_report_payload(valid_payload)

    assert validated == original
    assert validated is not valid_payload
    assert all(validated[group] is not valid_payload[group] for group in validated)
    assert validated["error"]["app_frames"] is not valid_payload["error"]["app_frames"]
    assert valid_payload == original


def test_schema_rejects_unknown_top_level_field(valid_payload):
    valid_payload["context"] = {"anything": "forbidden"}

    with pytest.raises(ReportValidationError, match=r"root: unknown field: context"):
        validate_report_payload(valid_payload)


def test_schema_rejects_non_string_dictionary_key_as_unknown_field(valid_payload):
    valid_payload[1] = "forbidden"
    valid_payload["context"] = "also forbidden"

    with pytest.raises(ReportValidationError, match=r"root: unknown field: 1"):
        validate_report_payload(valid_payload)


def test_schema_rejects_unknown_nested_field(valid_payload):
    valid_payload["operation"]["schema_name"] = "production"

    with pytest.raises(ReportValidationError, match=r"operation: unknown field"):
        validate_report_payload(valid_payload)


def test_schema_rejects_unknown_frame_field(valid_payload):
    valid_payload["error"]["app_frames"][0]["absolute_path"] = "C:/synthetic.py"

    with pytest.raises(ReportValidationError, match=r"error.app_frames\[0\]: unknown field"):
        validate_report_payload(valid_payload)


def test_schema_requires_all_top_level_groups(valid_payload):
    del valid_payload["runtime"]

    with pytest.raises(ReportValidationError, match=r"root: required field: runtime"):
        validate_report_payload(valid_payload)


def test_schema_rejects_non_object_payload():
    with pytest.raises(ReportValidationError, match=r"root: expected object"):
        validate_report_payload([])


@pytest.mark.parametrize("bad_version", [0, 2, "1", True, 1.5])
def test_schema_rejects_unknown_or_mistyped_version(valid_payload, bad_version):
    valid_payload["report"]["report_schema_version"] = bad_version

    with pytest.raises(ReportValidationError, match=r"report.report_schema_version"):
        validate_report_payload(valid_payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("report", "report_schema_version"), 1.0),
        (("system", "utc_offset_minutes"), -840.0),
        (("system", "utc_offset_minutes"), 840.0),
        (("error", "app_frames", 0, "line"), 1.0),
        (("error", "app_frames", 0, "line"), 10_000_000.0),
    ],
)
def test_integer_contract_accepts_and_canonicalizes_integral_json_numbers(
    schema_validator, valid_payload, path, value
):
    _set_path(valid_payload, path, value)

    assert not list(schema_validator.iter_errors(valid_payload))
    validated = validate_report_payload(valid_payload)

    assert _get_path(validated, path) == int(value)
    assert type(_get_path(validated, path)) is int


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("report", "report_schema_version"), 1.5),
        (("report", "report_schema_version"), True),
        (("system", "utc_offset_minutes"), -841.0),
        (("system", "utc_offset_minutes"), 841.0),
        (("system", "utc_offset_minutes"), 0.5),
        (("system", "utc_offset_minutes"), True),
        (("error", "app_frames", 0, "line"), 0.0),
        (("error", "app_frames", 0, "line"), 10_000_001.0),
        (("error", "app_frames", 0, "line"), 1.5),
        (("error", "app_frames", 0, "line"), True),
    ],
)
def test_integer_contract_rejects_non_integral_boolean_and_out_of_range_numbers(
    schema_validator, valid_payload, path, value
):
    _set_path(valid_payload, path, value)

    assert list(schema_validator.iter_errors(valid_payload))
    with pytest.raises(ReportValidationError):
        validate_report_payload(valid_payload)


@pytest.mark.parametrize(
    "installation_id",
    [
        "not-a-uuid",
        "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "00000000-0000-4000-8000-000000000000",
        "550E8400-E29B-41D4-A716-446655440000",
    ],
)
def test_schema_requires_canonical_non_nil_uuid_v4(valid_payload, installation_id):
    valid_payload["report"]["anonymous_installation_id"] = installation_id

    with pytest.raises(ReportValidationError, match=r"anonymous_installation_id"):
        validate_report_payload(valid_payload)


@pytest.mark.parametrize(
    "fingerprint",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, 7],
)
def test_schema_requires_lowercase_sha256_fingerprint(valid_payload, fingerprint):
    valid_payload["report"]["error_fingerprint"] = fingerprint

    with pytest.raises(ReportValidationError, match=r"error_fingerprint"):
        validate_report_payload(valid_payload)


@pytest.mark.parametrize(
    ("group", "field", "bad_value"),
    [
        ("app", "package_kind", "portable"),
        ("system", "os_family", "freebsd"),
        ("system", "architecture", "x86"),
        ("operation", "kind", "migrate"),
        ("operation", "db_engine", "sqlite"),
        ("operation", "phase", "connect"),
    ],
)
def test_schema_rejects_values_outside_enums(valid_payload, group, field, bad_value):
    valid_payload[group][field] = bad_value

    with pytest.raises(ReportValidationError, match=rf"{group}.{field}: invalid value"):
        validate_report_payload(valid_payload)


@pytest.mark.parametrize("offset", [-840, 840])
def test_schema_accepts_utc_offset_boundaries(valid_payload, offset):
    valid_payload["system"]["utc_offset_minutes"] = offset

    assert validate_report_payload(valid_payload)["system"]["utc_offset_minutes"] == offset


@pytest.mark.parametrize("offset", [-841, 841, 1.5, True])
def test_schema_rejects_invalid_utc_offset(valid_payload, offset):
    valid_payload["system"]["utc_offset_minutes"] = offset

    with pytest.raises(ReportValidationError, match=r"system.utc_offset_minutes"):
        validate_report_payload(valid_payload)


def test_schema_accepts_message_at_maximum_length(valid_payload):
    valid_payload["error"]["sanitized_message"] = "x" * 2000

    assert len(validate_report_payload(valid_payload)["error"]["sanitized_message"]) == 2000


def test_json_schema_accepts_message_at_maximum_length(schema_validator, valid_payload):
    valid_payload["error"]["sanitized_message"] = "x" * 2000

    errors = list(schema_validator.iter_errors(valid_payload))

    assert not errors, [error.message for error in errors]


def test_schema_rejects_message_over_maximum_length(valid_payload):
    valid_payload["error"]["sanitized_message"] = "x" * 2001

    with pytest.raises(ReportValidationError, match=r"error.sanitized_message: too long"):
        validate_report_payload(valid_payload)


def test_json_schema_rejects_message_over_maximum_length(schema_validator, valid_payload):
    valid_payload["error"]["sanitized_message"] = "x" * 2001

    errors = list(schema_validator.iter_errors(valid_payload))
    paths = {_schema_error_path(error) for error in errors}

    assert errors, "JSON Schema unexpectedly accepted a 2001-character message"
    assert "error.sanitized_message" in paths, (
        "JSON Schema rejected the wrong path for a 2001-character message: "
        f"{sorted(paths)}"
    )


def test_schema_accepts_twenty_application_frames(valid_payload):
    frame = valid_payload["error"]["app_frames"][0]
    valid_payload["error"]["app_frames"] = [copy.deepcopy(frame) for _ in range(20)]

    assert len(validate_report_payload(valid_payload)["error"]["app_frames"]) == 20


def test_json_schema_accepts_twenty_application_frames(schema_validator, valid_payload):
    frame = valid_payload["error"]["app_frames"][0]
    valid_payload["error"]["app_frames"] = [copy.deepcopy(frame) for _ in range(20)]

    errors = list(schema_validator.iter_errors(valid_payload))

    assert not errors, [error.message for error in errors]


def test_schema_rejects_more_than_twenty_application_frames(valid_payload):
    frame = valid_payload["error"]["app_frames"][0]
    valid_payload["error"]["app_frames"] = [copy.deepcopy(frame) for _ in range(21)]

    with pytest.raises(ReportValidationError, match=r"error.app_frames: too many items"):
        validate_report_payload(valid_payload)


def test_json_schema_rejects_more_than_twenty_application_frames(
    schema_validator, valid_payload
):
    frame = valid_payload["error"]["app_frames"][0]
    valid_payload["error"]["app_frames"] = [copy.deepcopy(frame) for _ in range(21)]

    errors = list(schema_validator.iter_errors(valid_payload))
    paths = {_schema_error_path(error) for error in errors}

    assert errors, "JSON Schema unexpectedly accepted 21 application frames"
    assert "error.app_frames" in paths, (
        "JSON Schema rejected the wrong path for 21 application frames: "
        f"{sorted(paths)}"
    )


@pytest.mark.parametrize(
    ("group", "field", "bad_value"),
    [
        ("app", "version", "2.3.1-beta"),
        ("app", "ui_language", "../../ko"),
        ("system", "os_version", ""),
        ("system", "locale", "ko KR"),
        ("runtime", "python_version", "3.12rc1"),
        ("runtime", "qt_version", "6"),
        ("runtime", "rust_core_version", "v2.3.1"),
        ("operation", "db_server_version", "8.0.36"),
        ("error", "exception_class", "builtins.ValueError()"),
        ("error", "error_code", "code with spaces"),
    ],
)
def test_schema_rejects_invalid_bounded_strings(valid_payload, group, field, bad_value):
    valid_payload[group][field] = bad_value

    with pytest.raises(ReportValidationError, match=rf"{group}.{field}"):
        validate_report_payload(valid_payload)


def test_schema_allows_only_documented_optional_fields_to_be_absent(valid_payload):
    del valid_payload["operation"]["db_server_version"]
    del valid_payload["error"]["error_code"]

    validated = validate_report_payload(valid_payload)

    assert "db_server_version" not in validated["operation"]
    assert "error_code" not in validated["error"]


def test_json_schema_declares_strict_objects_and_matches_exported_enums():
    schema = _load_contract("schema.json")

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/error-reporting/v1/schema.json")
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "report",
        "app",
        "system",
        "runtime",
        "operation",
        "error",
    ]
    assert all(
        group_schema["additionalProperties"] is False
        for group_schema in schema["properties"].values()
    )
    frame_schema = schema["properties"]["error"]["properties"]["app_frames"]["items"]
    assert frame_schema["additionalProperties"] is False
    installation_schema = schema["properties"]["report"]["properties"]["anonymous_installation_id"]
    assert installation_schema["not"] == {
        "const": "00000000-0000-4000-8000-000000000000"
    }
    version_schema = schema["properties"]["report"]["properties"]["report_schema_version"]
    assert version_schema == {"type": "integer", "const": REPORT_SCHEMA_VERSION}
    assert set(schema["properties"]["app"]["properties"]["package_kind"]["enum"]) == PACKAGE_KINDS
    assert set(schema["properties"]["system"]["properties"]["os_family"]["enum"]) == OS_FAMILIES
    assert set(schema["properties"]["system"]["properties"]["architecture"]["enum"]) == ARCHITECTURES
    assert set(schema["properties"]["operation"]["properties"]["kind"]["enum"]) == OPERATION_KINDS
    assert set(schema["properties"]["operation"]["properties"]["db_engine"]["enum"]) == DB_ENGINES
    assert set(schema["properties"]["operation"]["properties"]["phase"]["enum"]) == OPERATION_PHASES


def test_all_invalid_contract_cases_are_rejected_by_expected_class():
    invalid_contract = _load_contract("invalid-cases.json")
    cases = invalid_contract["cases"]

    assert len(cases) >= 15
    assert len({case["name"] for case in cases}) == len(cases)
    for case in cases:
        assert set(case) == {
            "name",
            "expected_rejection",
            "expected_path",
            "payload",
        }
        with pytest.raises(ReportValidationError) as exc_info:
            validate_report_payload(case["payload"])
        assert exc_info.value.rejection_class == case["expected_rejection"], case["name"]
        assert exc_info.value.path == case["expected_path"], case["name"]


def test_json_schema_rejects_every_invalid_fixture_at_expected_path(schema_validator):
    invalid_contract = _load_contract("invalid-cases.json")

    assert invalid_contract["schema_version"] == REPORT_SCHEMA_VERSION
    for case in invalid_contract["cases"]:
        errors = list(schema_validator.iter_errors(case["payload"]))
        paths = {_schema_error_path(error) for error in errors}
        assert errors, f"{case['name']}: JSON Schema unexpectedly accepted payload"
        assert case["expected_path"] in paths, (
            f"{case['name']}: expected error at {case['expected_path']}, "
            f"got {sorted(paths)}"
        )


def test_redaction_contract_has_all_required_synthetic_categories():
    redaction_contract = _load_contract("redaction-cases.json")
    cases = redaction_contract["cases"]
    required_categories = {
        "credentials",
        "authorization_header",
        "url",
        "dsn",
        "ipv4",
        "ipv6",
        "email",
        "windows_path",
        "posix_path",
        "unc_path",
        "sql",
        "quoted_identifier",
        "markdown",
        "unicode_separator",
        "control_character",
        "high_entropy_token",
    }

    assert redaction_contract["schema_version"] == REPORT_SCHEMA_VERSION
    assert redaction_contract["synthetic_values_only"] is True
    assert {case["category"] for case in cases} == required_categories
    assert len({case["name"] for case in cases}) == len(cases)
    assert all(set(case) == {"name", "category", "input", "forbidden"} for case in cases)
    for case in cases:
        assert case["input"], case["name"]
        assert case["forbidden"], case["name"]
        assert all(
            forbidden and forbidden in case["input"]
            for forbidden in case["forbidden"]
        ), case["name"]
