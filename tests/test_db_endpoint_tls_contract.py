"""TF-STATUS-110 shared contract: DbEndpoint TLS fields map to Rust `Endpoint.tls`."""
from src.core.db_core_facade import DbEndpoint


def _endpoint(**kwargs):
    return DbEndpoint("postgresql", "127.0.0.1", 15432, "u", "p", "d", **kwargs)


def test_disabled_tls_keeps_legacy_payload_shape():
    assert "tls" not in _endpoint().to_payload()


def test_verified_tls_payload_includes_only_set_fields():
    assert _endpoint(tls_mode="verify_full", tls_server_name="db.internal").to_payload()["tls"] == {
        "mode": "verify_full",
        "server_name": "db.internal",
    }
    assert _endpoint(tls_mode="verify_ca", tls_ca_file="ca.pem").to_payload()["tls"] == {
        "mode": "verify_ca",
        "ca_file": "ca.pem",
    }
