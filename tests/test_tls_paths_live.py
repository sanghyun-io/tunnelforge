"""Opt-in live check: safe-promotion and cross-engine payloads really use verified TLS (TF-STATUS-110 hardening).

Skipped unless TF_TLS_TEST_CERT_DIR is set. Environment: scripts/tls_live_env.sh (certs, up-pg, up-mysql, cert <t> good).

    TF_TLS_TEST_CERT_DIR=<certs> TF_TLS_TEST_ENV_SCRIPT=scripts/tls_live_env.sh TF_TLS_TEST_BASH=<git bash> \
    TF_TLS_TEST_CORE=<tunnelforge-core exe> pytest tests/test_tls_paths_live.py -s
"""
import os
import subprocess
from types import SimpleNamespace

import pytest

from src.core import connection_trust as ct
from src.core.cross_engine_migration import DatabaseEngine, make_connection_payload
from src.core.db_core_client import DbCoreServiceClient, DbCoreServiceError, db_core_executable
from src.core.db_core_facade import DbCoreFacade, DbEndpoint
from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter, RustDumpImporter

CERTS = os.environ.get("TF_TLS_TEST_CERT_DIR")
pytestmark = pytest.mark.skipif(not CERTS, reason="TF_TLS_TEST_CERT_DIR not set (opt-in live test)")

MYSQL_PORT, PG_PORT = 23306, 25432


def _swap(target, scenario):
    bash = os.environ.get("TF_TLS_TEST_BASH", "bash")
    script = os.environ.get("TF_TLS_TEST_ENV_SCRIPT", "scripts/tls_live_env.sh")
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "TF_TLS_CERT_DIR": CERTS}
    subprocess.run([bash, script, "cert", target, scenario], check=True, env=env)


def _facade():
    return DbCoreFacade(DbCoreServiceClient(executable=os.environ.get("TF_TLS_TEST_CORE") or db_core_executable()))


@pytest.fixture(autouse=True)
def registry():
    ct.clear_registered_tls()
    policy = ct.TlsPolicy("verify_full", f"{CERTS}/ca.pem", "")
    ct.register_endpoint_tls("127.0.0.1", MYSQL_PORT, policy)
    ct.register_endpoint_tls("127.0.0.1", PG_PORT, policy)
    yield
    ct.clear_registered_tls()
    _swap("mysql", "good")
    _swap("pg", "good")


def _sql(facade, endpoint, *statements):
    connection_id = facade.open_connection(endpoint)
    try:
        for sql in statements:
            result = facade.client.request("query.execute", {"connection_id": connection_id, "sql": sql})
            assert result.get("success"), result
    finally:
        facade.close_connection(connection_id)


def test_cross_engine_migration_uses_verified_tls_and_rejects_bad_certificate():
    _swap("mysql", "good")
    _swap("pg", "good")
    facade = _facade()
    try:
        my = DbEndpoint("mysql", "127.0.0.1", MYSQL_PORT, "root", "tfpass", "tfdb")
        _sql(facade, my, "DROP TABLE IF EXISTS tf_tls_src",
             "CREATE TABLE tf_tls_src (id INT PRIMARY KEY, name VARCHAR(20))",
             "INSERT INTO tf_tls_src VALUES (1,'a'),(2,'b')")
        pg = DbEndpoint("postgresql", "127.0.0.1", PG_PORT, "postgres", "tfpass", "postgres")
        _sql(facade, pg, "DROP TABLE IF EXISTS tf_tls_src")

        source = make_connection_payload(DatabaseEngine.MYSQL, "127.0.0.1", MYSQL_PORT, "root", "tfpass", "tfdb", "tfdb")
        target = make_connection_payload(DatabaseEngine.POSTGRESQL, "127.0.0.1", PG_PORT, "postgres", "tfpass", "postgres", "public")
        assert source["tls"]["mode"] == "verify_full" and target["tls"]["mode"] == "verify_full"

        inspected = facade.client.request("schema.inspect", {"source": source})
        assert inspected.get("success"), inspected
        schema = inspected.get("schema") or inspected.get("normalized_schema")
        migrated = facade.client.request("migration.run", {
            "source_engine": "mysql", "target_engine": "postgresql", "source": source, "target": target,
            "schema": schema, "execution_options": {"mode": "create_only", "chunk_size": 1000},
        })
        assert migrated.get("success"), migrated
        rows = facade.client.request("query.execute", {
            "connection_id": facade.open_connection(pg), "sql": "SELECT count(*) AS n FROM tf_tls_src"})
        assert str(rows["rows"][0]["n"]) == "2"
        print("PASS cross-engine inspect+migrate over verify_full TLS (MySQL -> PostgreSQL), 2 rows copied")

        # an untrusted certificate must be refused on this path as well
        _swap("mysql", "untrusted")
        try:
            refused = facade.client.request("schema.inspect", {"source": source})
            assert not refused.get("success"), refused
            text = str(refused.get("message"))
        except DbCoreServiceError as exc:  # the core reports it as an error event
            text = str(exc)
        assert "Tls" in text or "TLS" in text or ct.extract_error_code(text), text
        print("PASS cross-engine payload with an untrusted server certificate refused")
    finally:
        facade.client.shutdown()


def test_safe_promotion_uses_verified_tls_and_rejects_bad_certificate(tmp_path):
    from src.ui.dialogs import db_import_dialog

    _swap("mysql", "good")
    schema = "tf_tls_promo"
    facade = _facade()
    try:
        my = DbEndpoint("mysql", "127.0.0.1", MYSQL_PORT, "root", "tfpass", schema)
        admin = DbEndpoint("mysql", "127.0.0.1", MYSQL_PORT, "root", "tfpass", "tfdb")
        _sql(facade, admin, f"DROP DATABASE IF EXISTS {schema}", f"CREATE DATABASE {schema}",
             f"CREATE TABLE {schema}.t (id INT PRIMARY KEY, v VARCHAR(10))", f"INSERT INTO {schema}.t VALUES (1,'x')")

        config = RustDumpConfig(host="127.0.0.1", port=MYSQL_PORT, user="root", password="tfpass",
                                engine="mysql", database=schema)
        dump_dir = str(tmp_path / "dump")
        ok, message, _ = RustDumpExporter(config, facade=facade).export_tables(schema, ["t"], dump_dir)
        assert ok, message

        audit_events = []
        ok, message, _ = RustDumpImporter(config, facade=facade).import_dump(
            dump_dir, target_schema=schema, import_mode="safe", raw_output_callback=audit_events.append)
        assert ok, message
        import json
        audit = {}
        for line in audit_events:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            db_import_dialog._capture_import_audit(audit, event)
        assert audit.get("restore_id") and audit.get("report_path"), audit

        dummy = SimpleNamespace(import_audit=audit, restore_config=config)
        plan_payload = db_import_dialog.RustDumpImportDialog._promotion_payload(dummy, "plan")
        assert plan_payload["target"]["tls"]["mode"] == "verify_full"
        plan = facade.promote_dump(plan_payload)
        assert plan.get("success") and plan.get("can_promote") is True, plan
        print("PASS safe promotion plan over verify_full TLS: can_promote=True")

        _swap("mysql", "untrusted")
        refused = None
        try:
            refused = facade.promote_dump(db_import_dialog.RustDumpImportDialog._promotion_payload(dummy, "plan"))
        except DbCoreServiceError as exc:
            refused = {"success": False, "message": str(exc)}
        assert not refused.get("success") or refused.get("can_promote") is not True, refused
        print("PASS safe promotion with an untrusted server certificate refused:", str(refused)[:120])
    finally:
        facade.client.shutdown()


def test_manual_host_form_sends_chosen_tls_and_it_is_enforced():
    """Cross-engine form without a tunnel profile: the TLS choice made in the form reaches the core."""
    import sys
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from src.ui.dialogs.cross_engine_migration_endpoint_form import EndpointForm

    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841 (keeps the app alive)
    ct.clear_registered_tls()  # nothing registered: only the form's own values can carry TLS
    _swap("mysql", "good")
    facade = _facade()
    try:
        form = EndpointForm("src", DatabaseEngine.MYSQL)
        form.input_host.setText("127.0.0.1")
        form.input_port.setValue(MYSQL_PORT)
        form.input_user.setText("root")
        form.input_password.setText("tfpass")
        form.input_database.setText("tfdb")
        form.input_schema.setText("tfdb")
        form.combo_tls.setCurrentIndex(form.combo_tls.findData("verify_full"))
        form.input_tls_ca.setText(f"{CERTS}/ca.pem")
        payload = form.payload()
        assert payload["tls"] == {"mode": "verify_full", "ca_file": f"{CERTS}/ca.pem"}
        inspected = facade.client.request("schema.inspect", {"source": payload})
        assert inspected.get("success"), inspected
        print("PASS manual-host form: verify_full + CA file accepted end to end")

        # same form without the CA file: the private CA is not in the OS store -> refused
        form.input_tls_ca.setText("")
        try:
            refused = facade.client.request("schema.inspect", {"source": form.payload()})
            assert not refused.get("success"), refused
            text = str(refused.get("message"))
        except DbCoreServiceError as exc:
            text = str(exc)
        assert "Tls" in text or "TLS" in text, text
        print("PASS manual-host form: verify_full without the private CA refused")
    finally:
        facade.client.shutdown()


def test_manual_host_form_certificate_name_for_ip_connection():
    """Connect by IP to a certificate issued for a DNS name only: needs the form's certificate name."""
    import sys
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from src.ui.dialogs.cross_engine_migration_endpoint_form import EndpointForm

    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    ct.clear_registered_tls()
    _swap("mysql", "dnsonly")  # SAN = DNS:tf-db.test only, no IP
    facade = _facade()

    def inspect(name):
        form = EndpointForm("src", DatabaseEngine.MYSQL)
        form.input_host.setText("127.0.0.1")
        form.input_port.setValue(MYSQL_PORT)
        form.input_user.setText("root")
        form.input_password.setText("tfpass")
        form.input_database.setText("tfdb")
        form.input_schema.setText("tfdb")
        form.combo_tls.setCurrentIndex(form.combo_tls.findData("verify_full"))
        form.input_tls_ca.setText(f"{CERTS}/ca.pem")
        form.input_tls_name.setText(name)
        payload = form.payload()
        assert payload["tls"].get("server_name", "") == name
        try:
            result = facade.client.request("schema.inspect", {"source": payload})
            return bool(result.get("success")), str(result.get("message"))
        except DbCoreServiceError as exc:
            return False, str(exc)

    try:
        ok, text = inspect("")
        assert not ok and ("Tls" in text or "TLS" in text), text
        print("PASS verify_full by IP without a certificate name refused (name mismatch)")
        ok, text = inspect("other.test")
        assert not ok and ("Tls" in text or "TLS" in text), text
        print("PASS verify_full with a wrong certificate name refused")
        ok, text = inspect("tf-db.test")
        assert ok, text
        print("PASS verify_full by IP with certificate name tf-db.test accepted")
    finally:
        facade.client.shutdown()
