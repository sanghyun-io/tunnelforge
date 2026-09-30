"""TF-STATUS-110 hardening: every connection payload sent to the Rust core carries the TLS policy."""
import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core import connection_trust as ct
from src.core.cross_engine_migration import ConnectionEndpointInput, DatabaseEngine, make_connection_payload

POLICY = ct.TlsPolicy("verify_full", "ca.pem", "db.internal")
EXPECTED = {"mode": "verify_full", "ca_file": "ca.pem", "server_name": "db.internal"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_registry():
    ct.clear_registered_tls()
    yield
    ct.clear_registered_tls()


def test_apply_registered_tls_is_noop_without_policy_and_keeps_explicit_tls():
    payload = {"host": "h", "port": 1}
    assert ct.apply_registered_tls(payload) is payload
    ct.register_endpoint_tls("h", 1, POLICY)
    assert ct.apply_registered_tls(payload)["tls"] == EXPECTED
    assert "tls" not in payload  # input is not mutated
    explicit = {"host": "h", "port": 1, "tls": {"mode": "verify_ca"}}
    assert ct.apply_registered_tls(explicit)["tls"] == {"mode": "verify_ca"}


def test_cross_engine_endpoint_payload_carries_tls():
    ct.register_endpoint_tls("127.0.0.1", 13306, POLICY)
    endpoint = ConnectionEndpointInput(DatabaseEngine.MYSQL, "127.0.0.1", 13306, "u", "p", "db")
    assert endpoint.to_payload()["tls"] == EXPECTED
    assert make_connection_payload(DatabaseEngine.MYSQL, "127.0.0.1", 13306, "u", "p", "db")["tls"] == EXPECTED
    assert "tls" not in make_connection_payload(DatabaseEngine.MYSQL, "127.0.0.1", 13307, "u", "p", "db")


def test_promotion_payload_carries_tls():
    from src.ui.dialogs.db_import_dialog import RustDumpImportDialog

    ct.register_endpoint_tls("127.0.0.1", 13306, POLICY)
    dummy = SimpleNamespace(
        import_audit={
            "original_target": {"engine": "mysql", "host": "127.0.0.1", "port": 13306, "database": "d"},
            "restore_id": "r1", "report_path": "report.json",
        },
        restore_config=SimpleNamespace(user="u", password="p"),
    )
    for action in ("plan", "confirm"):
        payload = RustDumpImportDialog._promotion_payload(dummy, action)
        assert payload["target"]["tls"] == EXPECTED
        assert payload["target"]["user"] == "u"


def test_oneclick_and_dump_payloads_use_db_endpoint_registry():
    from src.core.db_core_facade import DbEndpoint

    ct.register_endpoint_tls("127.0.0.1", 13306, POLICY)
    assert DbEndpoint("mysql", "127.0.0.1", 13306, "u", "p", "d").to_payload()["tls"] == EXPECTED


# ---- source-wide guard --------------------------------------------------------------------

def _functions_with_parents(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _calls_apply(func):
    return any(
        isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == "apply_registered_tls" or getattr(n.func, "attr", None) == "apply_registered_tls")
        for n in ast.walk(func)
    )


def _credential_dict_sites(tree):
    """Dict literals with host+user+password keys, and `.update(password=...)` style credential attachments."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if {"host", "user", "password"} <= keys:
                yield node
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "update":
            if any(kw.arg == "password" for kw in node.keywords):
                yield node


ALLOWED = {
    # DbEndpoint resolves the registry in __post_init__ (same policy source as apply_registered_tls)
    ("src/core/db_core_facade.py", "to_payload"),
    # mysql_config_editor login path (local client tool), never sent to the Rust core
    ("src/core/mysql_login_path.py", "register"),
}


def test_every_core_bound_connection_dict_goes_through_the_tls_helper():
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in _functions_with_parents(tree):
            sites = [s for s in _credential_dict_sites(func)]
            if sites and not _calls_apply(func) and (rel, func.name) not in ALLOWED:
                offenders.append(f"{rel}:{func.name} (line {sites[0].lineno})")
    assert offenders == [], (
        "connection payloads built by hand must wrap the dict with "
        "src.core.connection_trust.apply_registered_tls (or use DbEndpoint): " + ", ".join(offenders)
    )


def test_guard_actually_detects_a_plain_payload():
    tree = ast.parse('def f():\n    return {"host": h, "user": u, "password": p}\n')
    func = next(_functions_with_parents(tree))
    assert list(_credential_dict_sites(func)) and not _calls_apply(func)
