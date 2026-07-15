import gzip
import io
import json
import logging
from pathlib import Path

import pytest
import requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse


REPORT_URL = "https://relay.example.test/v1/reports"
HEALTH_URL = "https://relay.example.test/health"
RECEIPT = "11111111-1111-4111-8111-111111111111"
ISSUE_URL = "https://github.com/sanghyun-io/tunnelforge/issues/12"
PRODUCTION_RELAY_ORIGIN = "https://tunnelforge-issue-relay.ppkimsanh.workers.dev"
PRODUCTION_REPORT_URL = f"{PRODUCTION_RELAY_ORIGIN}/v1/reports"


def test_transport_binds_only_the_exact_production_relay_endpoint():
    from src.core.error_reporting_config import (
        ERROR_REPORT_RELAY_ORIGIN,
        ERROR_REPORT_RELAY_URL,
    )
    from src.core.error_report_transport import (
        ERROR_REPORT_RELAY_URL as transport_relay_url,
    )

    assert ERROR_REPORT_RELAY_ORIGIN == PRODUCTION_RELAY_ORIGIN
    assert ERROR_REPORT_RELAY_URL == PRODUCTION_REPORT_URL
    assert transport_relay_url == PRODUCTION_REPORT_URL


def authorized_transport(*args, **kwargs):
    from src.core.error_report_transport import ErrorReportTransport

    kwargs.setdefault("submission_authorizer", lambda: True)
    return ErrorReportTransport(*args, **kwargs)


class FakeResponse:
    def __init__(self, status_code=202, body=None, chunks=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        if chunks is None:
            chunks = [json.dumps(body or {}).encode("utf-8")]
        self._chunks = chunks
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size):
        self.iterated = True
        assert 0 < chunk_size <= 4096
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.prepared_requests = []
        self.trust_env = True

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def send(self, request, **kwargs):
        self.prepared_requests.append(request)
        if request.method == "POST":
            kwargs["json"] = json.loads(request.body)
        return self._request(request.method, request.url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)


def accepted_response(**overrides):
    body = {"status": "accepted", "receipt": RECEIPT}
    body.update(overrides)
    return FakeResponse(status_code=202, body=body)


class RecordingHttpsAdapter(HTTPAdapter):
    def __init__(self, response_mode="fixed"):
        super().__init__()
        self.response_mode = response_mode
        self.requests = []
        self.responses = []
        self.proxy_headers_seen = {}

    def send(self, request, **kwargs):
        self.requests.append((request, kwargs))
        proxy_url = kwargs.get("proxies", {}).get("https")
        self.proxy_headers_seen = (
            self.proxy_headers(proxy_url) if proxy_url is not None else {}
        )
        if request.url == HEALTH_URL:
            status_code = 200
            response_body = {
                "service": "issue-relay",
                "schema": 1,
                "mode": "active",
            }
        else:
            status_code = 202
            response_body = {"status": "accepted", "receipt": RECEIPT}
        body = json.dumps(response_body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.response_mode in {"gzip", "gzip_bomb"}:
            if self.response_mode == "gzip_bomb":
                body = b"x" * (16 * 1024 * 64)
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(body))
        elif self.response_mode == "chunked":
            headers["Transfer-Encoding"] = "chunked"
        elif self.response_mode == "close":
            headers["Connection"] = "close"
        else:
            headers["Content-Length"] = str(len(body))

        response = requests.Response()
        response.status_code = status_code
        response.url = request.url
        response.request = request
        response.headers.update(headers)
        response.raw = HTTPResponse(
            body=io.BytesIO(body),
            headers=headers,
            preload_content=False,
            decode_content=False,
        )
        original_close = response.close

        def record_close():
            response.closed = True
            original_close()

        response.closed = False
        response.close = record_close
        self.responses.append(response)
        return response

    def close(self):
        pass


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://relay.example.test/v1/reports",
        "ftp://relay.example.test/v1/reports",
        "https:///v1/reports",
        "https://user:pass@relay.example.test/v1/reports",
        "https://relay.example.test/v1/reports#fragment",
        "https://relay.example.test/v1/reports\x00",
        "https://relay.example.test/v1/reports\x1f",
        "https://relay.example.test/v1/reports\t",
        "https://relay.example.test/v1/reports\n",
        "https://relay.example.test/v1 /reports",
        "https://relay.example.test/v1\u00a0/reports",
    ],
)
def test_transport_rejects_unconfigured_non_https_or_tainted_report_urls(url):
    from src.core.error_report_transport import ErrorReportTransport

    with pytest.raises(ValueError, match="HTTPS relay URL"):
        authorized_transport(url)


def test_submit_uses_exact_url_requests_timeout_tls_and_environment_proxy_defaults():
    from src.core.error_report_transport import ErrorReportTransport

    payload = {"report": {"report_schema_version": 1}}
    session = FakeSession([accepted_response()])

    result = authorized_transport(REPORT_URL, session=session).submit(payload)

    assert result.success is True
    assert session.trust_env is True
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", REPORT_URL)
    assert kwargs["json"] == payload
    assert kwargs["timeout"] == (3.05, 8.0)
    assert kwargs["stream"] is True
    assert kwargs["allow_redirects"] is False
    assert kwargs["verify"] is True
    assert isinstance(kwargs["proxies"], dict)
    assert session.prepared_requests[0].headers["Accept-Encoding"] == "identity"


def test_default_session_preserves_requests_environment_proxy_support():
    from src.core.error_report_transport import ErrorReportTransport

    transport = authorized_transport(REPORT_URL)

    assert isinstance(transport.session, requests.Session)
    assert transport.session.trust_env is True


def test_submit_without_authorizer_fails_closed_before_network():
    from src.core.error_report_transport import ErrorReportTransport

    session = FakeSession([accepted_response()])

    result = ErrorReportTransport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is False
    assert result.message == "Relay request cancelled."
    assert result.issue_url == ""
    assert result.status_code is None
    assert session.calls == []


def test_health_does_not_require_submission_authorizer():
    from src.core.error_report_transport import ErrorReportTransport

    session = FakeSession(
        [
            FakeResponse(
                status_code=200,
                body={"service": "issue-relay", "schema": 1, "mode": "active"},
            )
        ]
    )

    result = ErrorReportTransport(REPORT_URL, session=session).health()

    assert result.success is True
    assert [call[0] for call in session.calls] == ["GET"]


def test_explicit_tls_verification_overrides_injected_insecure_session_and_honors_ca_bundle(
    tmp_path, monkeypatch
):
    from src.core.error_report_transport import ErrorReportTransport

    ca_bundle = Path(tmp_path, "relay-ca.pem")
    ca_bundle.write_text("test CA bundle", encoding="ascii")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_bundle))
    session = requests.Session()
    session.verify = False
    adapter = RecordingHttpsAdapter()
    session.mount("https://", adapter)

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is True
    assert adapter.requests[0][1]["verify"] == str(ca_bundle)


def test_relay_url_cannot_be_downgraded_or_reassigned_after_construction():
    from src.core.error_report_transport import ErrorReportTransport

    transport = authorized_transport(REPORT_URL, session=FakeSession([]))
    downgraded_url = REPORT_URL.replace("https://", "http://", 1)

    with pytest.raises(AttributeError):
        setattr(transport, "relay_url", downgraded_url)
    with pytest.raises(AttributeError):
        transport._ErrorReportTransport__relay_url = (
            "https://other.example.test/v1/reports"
        )


@pytest.mark.parametrize("method", ["submit", "health"])
def test_transport_defensively_revalidates_immutable_url_before_dispatch(
    monkeypatch, method
):
    import src.core.error_report_transport as transport_module

    session = FakeSession([])
    transport = authorized_transport(REPORT_URL, session=session)
    monkeypatch.setattr(transport_module, "is_valid_relay_url", lambda _url: False)

    result = (
        transport.submit({"safe": True})
        if method == "submit"
        else transport.health()
    )

    assert result.success is False
    assert result.issue_url == ""
    assert session.calls == []


@pytest.mark.parametrize("mode", ["fixed", "chunked", "close"])
def test_real_requests_stack_accepts_bounded_response_framing_modes(mode):
    from src.core.error_report_transport import ErrorReportTransport

    session = requests.Session()
    adapter = RecordingHttpsAdapter(mode)
    session.mount("https://", adapter)

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is True
    assert result.message == "Report accepted."
    assert result.issue_url == ""
    assert [request.url for request, _kwargs in adapter.requests] == [REPORT_URL]


@pytest.mark.parametrize("method", ["submit", "health"])
def test_requests_advertise_identity_response_encoding(method):
    from src.core.error_report_transport import ErrorReportTransport

    session = requests.Session()
    adapter = RecordingHttpsAdapter()
    session.mount("https://", adapter)
    transport = authorized_transport(REPORT_URL, session=session)

    result = transport.submit({"safe": True}) if method == "submit" else transport.health()

    assert result.success is True
    assert adapter.requests[0][0].headers["Accept-Encoding"] == "identity"


def test_identity_content_encoding_is_treated_as_plain_bounded_json():
    response = accepted_response()
    response.headers["Content-Encoding"] = " identity "
    session = FakeSession([response])

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is True
    assert response.iterated is True
    assert response.closed is True


@pytest.mark.parametrize(
    "content_encoding", ["gzip", "br", "deflate", "identity, gzip"]
)
def test_non_identity_content_encoding_is_rejected_before_body_iteration(
    content_encoding,
):
    response = FakeResponse(
        status_code=202,
        chunks=[gzip.compress(b"x" * (16 * 1024 * 64))],
        headers={"Content-Encoding": content_encoding},
    )
    session = FakeSession([response])

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is False
    assert result.message == "Relay returned an invalid response."
    assert response.iterated is False
    assert response.closed is True


@pytest.mark.parametrize("mode", ["gzip", "gzip_bomb"])
def test_real_requests_stack_rejects_gzip_responses_and_closes(mode):
    session = requests.Session()
    adapter = RecordingHttpsAdapter(mode)
    session.mount("https://", adapter)

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is False
    assert adapter.requests[0][0].headers["Accept-Encoding"] == "identity"
    assert adapter.responses[0].closed is True


def test_real_requests_stack_uses_environment_proxy(monkeypatch):
    from src.core.error_report_transport import ErrorReportTransport

    proxy_url = "http://proxy.example.test:8080"
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("https_proxy", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    session = requests.Session()
    adapter = RecordingHttpsAdapter()
    session.mount("https://", adapter)

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is True
    request, kwargs = adapter.requests[0]
    assert request.url == REPORT_URL
    assert proxy_url in kwargs["proxies"].values()


@pytest.mark.parametrize("method", ["submit", "health"])
@pytest.mark.parametrize("auth_source", ["netrc", "session", "session_header"])
def test_relay_suppresses_origin_auth_but_preserves_environment_proxy_and_ca(
    tmp_path, monkeypatch, method, auth_source
):
    from src.core.error_report_transport import ErrorReportTransport

    netrc_path = Path(tmp_path, ".netrc")
    netrc_path.write_text(
        "machine relay.example.test login netrc-user password netrc-secret\n",
        encoding="ascii",
    )
    netrc_path.chmod(0o600)
    ca_bundle = Path(tmp_path, "relay-ca.pem")
    ca_bundle.write_text("test CA bundle", encoding="ascii")
    proxy_url = "http://proxy-user:proxy-pass@proxy.example.test:8080"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("NETRC", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("https_proxy", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_bundle))

    session = requests.Session()
    if auth_source == "session":
        session.auth = HTTPBasicAuth("session-user", "session-secret")
    elif auth_source == "session_header":
        session.headers["Authorization"] = "Bearer session-header-secret"
    adapter = RecordingHttpsAdapter()
    session.mount("https://", adapter)
    transport = authorized_transport(REPORT_URL, session=session)

    result = (
        transport.submit({"safe": True})
        if method == "submit"
        else transport.health()
    )

    assert result.success is True
    request, kwargs = adapter.requests[0]
    assert "Authorization" not in request.headers
    assert proxy_url in kwargs["proxies"].values()
    assert kwargs["verify"] == str(ca_bundle)


@pytest.mark.parametrize("method", ["submit", "health"])
def test_origin_request_does_not_inherit_session_state_but_keeps_environment(
    tmp_path, monkeypatch, method
):
    from src.core.error_report_transport import ErrorReportTransport

    ca_bundle = Path(tmp_path, "relay-ca.pem")
    ca_bundle.write_text("test CA bundle", encoding="ascii")
    proxy_url = "http://proxy.example.test:8080"
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("https_proxy", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_bundle))

    session = requests.Session()
    session.headers.update(
        {
            "X-Injected-Secret": "header-secret",
            "Cookie": "manual-cookie=manual-secret",
        }
    )
    session.cookies.set("session-cookie", "cookie-secret")
    session.params = {"injected_query": "query-secret"}
    adapter = RecordingHttpsAdapter()
    session.mount("https://", adapter)
    transport = ErrorReportTransport(
        REPORT_URL,
        session=session,
        submission_authorizer=lambda: True,
    )

    result = (
        transport.submit({"safe": True})
        if method == "submit"
        else transport.health()
    )

    assert result.success is True
    request, kwargs = adapter.requests[0]
    assert request.url == (REPORT_URL if method == "submit" else HEALTH_URL)
    assert "X-Injected-Secret" not in request.headers
    assert "Cookie" not in request.headers
    assert "header-secret" not in str(request.headers)
    assert "cookie-secret" not in str(request.headers)
    assert "query-secret" not in request.url
    assert proxy_url in kwargs["proxies"].values()
    assert kwargs["verify"] == str(ca_bundle)


def test_no_proxy_never_sends_origin_or_proxy_authorization(monkeypatch):
    from src.core.error_report_transport import ErrorReportTransport

    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(variable, raising=False)
    session = requests.Session()
    session.headers["Proxy-Authorization"] = "Basic inherited-proxy-secret"
    adapter = RecordingHttpsAdapter()
    session.mount("https://", adapter)

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is True
    request, _kwargs = adapter.requests[0]
    assert "Authorization" not in request.headers
    assert "Proxy-Authorization" not in request.headers
    assert adapter.proxy_headers_seen == {}


@pytest.mark.parametrize("auth_source", ["netrc", "session"])
def test_matching_origin_auth_is_suppressed_without_disabling_trust_env(
    tmp_path, monkeypatch, auth_source
):
    from src.core.error_report_transport import ErrorReportTransport

    netrc_path = Path(tmp_path, ".netrc")
    netrc_path.write_text(
        "machine relay.example.test login netrc-user password netrc-secret\n",
        encoding="ascii",
    )
    netrc_path.chmod(0o600)
    ca_bundle = Path(tmp_path, "relay-ca.pem")
    ca_bundle.write_text("test CA bundle", encoding="ascii")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("NETRC", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_bundle))
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(variable, raising=False)

    session = requests.Session()
    if auth_source == "session":
        session.auth = HTTPBasicAuth("session-user", "session-secret")
    adapter = RecordingHttpsAdapter()
    session.mount("https://", adapter)

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is True
    request, kwargs = adapter.requests[0]
    assert "Authorization" not in request.headers
    assert "Proxy-Authorization" not in request.headers
    assert kwargs["verify"] == str(ca_bundle)
    assert session.trust_env is True


def test_proxy_url_credentials_are_sent_only_to_proxy(monkeypatch):
    from src.core.error_report_transport import ErrorReportTransport

    proxy_url = "http://proxy-user:proxy-secret@proxy.example.test:8080"
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("https_proxy", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    session = requests.Session()
    adapter = RecordingHttpsAdapter()
    session.mount("https://", adapter)

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is True
    request, kwargs = adapter.requests[0]
    assert proxy_url in kwargs["proxies"].values()
    assert "Authorization" not in request.headers
    assert "Proxy-Authorization" not in request.headers
    assert adapter.proxy_headers_seen["Proxy-Authorization"].startswith("Basic ")


def test_submission_authorization_commits_once_before_network_retry():
    from src.core.error_report_transport import ErrorReportTransport

    authorizations = []

    def authorize():
        authorizations.append(True)
        return True

    session = FakeSession([
        requests.ConnectionError("offline"),
        accepted_response(),
    ])

    result = authorized_transport(
        REPORT_URL,
        session=session,
        backoff=lambda _milliseconds: None,
        submission_authorizer=authorize,
    ).submit({"safe": True})

    assert result.success is True
    assert authorizations == [True]
    assert len(session.calls) == 2


@pytest.mark.parametrize("mode", ["off", "shadow", "canary", "active"])
def test_health_requires_exact_wire_body_and_uses_fixed_local_message(mode):
    from src.core.error_report_transport import ErrorReportTransport

    response = FakeResponse(
        status_code=200,
        body={"service": "issue-relay", "schema": 1, "mode": mode},
    )
    session = FakeSession([response])

    result = authorized_transport(REPORT_URL, session=session).health()

    assert result.success is True
    assert result.message == f"Relay is {mode}."
    assert result.issue_url == ""
    assert result.status_code == 200
    assert session.calls[0][1] == HEALTH_URL
    assert response.closed is True


@pytest.mark.parametrize(
    "status_code,body",
    [
        (201, {"service": "issue-relay", "schema": 1, "mode": "active"}),
        (200, {"service": "wrong", "schema": 1, "mode": "active"}),
        (200, {"service": "issue-relay", "schema": 2, "mode": "active"}),
        (200, {"service": "issue-relay", "schema": 1, "mode": "ACTIVE"}),
        (200, {"service": "issue-relay", "schema": 1, "mode": []}),
        (200, {"service": "issue-relay", "schema": 1, "mode": "active", "message": "remote"}),
        (200, {"status": "active"}),
    ],
)
def test_health_rejects_http_key_value_and_mode_mismatches(status_code, body):
    from src.core.error_report_transport import ErrorReportTransport

    result = authorized_transport(
        REPORT_URL,
        session=FakeSession([FakeResponse(status_code=status_code, body=body)]),
    ).health()

    assert result.success is False
    assert result.issue_url == ""


@pytest.mark.parametrize(
    "status_code,raw_body,method",
    [
        (
            200,
            b'{"service":"issue-relay","schema":1,"mode":"active","mode":"active"}',
            "health",
        ),
        (
            202,
            b'{"status":"accepted","status":"accepted","receipt":"'
            + RECEIPT.encode("ascii")
            + b'"}',
            "submit",
        ),
    ],
)
def test_wire_contract_rejects_duplicate_json_members(status_code, raw_body, method):
    from src.core.error_report_transport import ErrorReportTransport

    transport = authorized_transport(
        REPORT_URL,
        session=FakeSession(
            [FakeResponse(status_code=status_code, chunks=[raw_body])]
        ),
    )

    result = (
        transport.health()
        if method == "health"
        else transport.submit({"safe": True})
    )

    assert result.success is False
    assert result.message == "Relay returned an invalid response."
    assert result.issue_url == ""


@pytest.mark.parametrize(
    "status_code,body,expected_message,expected_url",
    [
        (202, {"status": "accepted", "receipt": RECEIPT}, "Report accepted.", ""),
        (201, {"status": "created", "issue_url": ISSUE_URL}, "Report created.", ISSUE_URL),
        (200, {"status": "updated", "issue_url": ISSUE_URL}, "Existing issue updated.", ISSUE_URL),
        (200, {"status": "duplicate", "issue_url": ISSUE_URL}, "Duplicate report accepted.", ISSUE_URL),
    ],
)
def test_submit_accepts_only_canonical_success_contracts(
    status_code, body, expected_message, expected_url
):
    from src.core.error_report_transport import ErrorReportTransport

    result = authorized_transport(
        REPORT_URL,
        session=FakeSession([FakeResponse(status_code=status_code, body=body)]),
    ).submit({"safe": True})

    assert result.success is True
    assert result.message == expected_message
    assert result.issue_url == expected_url
    assert result.status_code == status_code


@pytest.mark.parametrize(
    "status_code,body",
    [
        (200, {"status": "accepted", "receipt": RECEIPT}),
        (202, {"status": "accepted"}),
        (202, {"status": "accepted", "receipt": "11111111-1111-1111-8111-111111111111"}),
        (202, {"status": "accepted", "receipt": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}),
        (202, {"status": "accepted", "receipt": RECEIPT, "issue_url": ISSUE_URL}),
        (202, {"status": "accepted", "receipt": RECEIPT, "message": "remote"}),
        (200, {"status": "created", "issue_url": ISSUE_URL}),
        (201, {"status": "created"}),
        (201, {"status": "created", "issue_url": ISSUE_URL, "receipt": RECEIPT}),
        (201, {"status": "updated", "issue_url": ISSUE_URL}),
        (200, {"status": "updated"}),
        (200, {"status": "duplicate"}),
        (200, {"status": "queued", "receipt": RECEIPT}),
        (200, {"status": "active", "issue_url": ISSUE_URL}),
        (200, {"status": [], "issue_url": ISSUE_URL}),
    ],
)
def test_submit_rejects_every_http_status_key_and_status_mismatch(status_code, body):
    from src.core.error_report_transport import ErrorReportTransport

    result = authorized_transport(
        REPORT_URL,
        session=FakeSession([FakeResponse(status_code=status_code, body=body)]),
    ).submit({"safe": True})

    assert result.success is False
    assert result.message == "Relay returned an invalid response."
    assert result.issue_url == ""


@pytest.mark.parametrize(
    "issue_url",
    [
        "https://github.com.evil.example/sanghyun-io/tunnelforge/issues/1",
        "https://user@github.com/sanghyun-io/tunnelforge/issues/1",
        "https://github.com:444/sanghyun-io/tunnelforge/issues/1",
        "https://github.com/sanghyun-io/tunnelforge/issues/1?next=evil",
        "https://github.com/sanghyun-io/tunnelforge/issues/1#fragment",
        "https://github.com/sanghyun-io/other/issues/1",
        "https://github.com/sanghyun-io/tunnelforge/issues/0",
        "https://github.com/sanghyun-io/tunnelforge/issues/01",
        "https://github.com/sanghyun-io/tunnelforge/issues/1\x00",
        "https://github.com/sanghyun-io/tunnelforge/issues/1\n",
        "https://github.com/sanghyun-io/tunnelforge/issues /1",
    ],
)
def test_submit_rejects_noncanonical_or_tainted_issue_urls(issue_url):
    from src.core.error_report_transport import ErrorReportTransport

    result = authorized_transport(
        REPORT_URL,
        session=FakeSession([
            FakeResponse(
                status_code=201,
                body={"status": "created", "issue_url": issue_url},
            )
        ]),
    ).submit({"safe": True})

    assert result.success is False
    assert result.issue_url == ""


def test_malformed_deep_or_oversized_response_fails_closed_and_closes():
    from src.core.error_report_transport import MAX_RESPONSE_BYTES, ErrorReportTransport

    responses = [
        FakeResponse(chunks=[b"{not-json"]),
        FakeResponse(chunks=[b"[" * 7000 + b"0" + b"]" * 7000]),
        FakeResponse(chunks=[b"x" * (MAX_RESPONSE_BYTES + 1)]),
        FakeResponse(
            chunks=[b"{}"],
            headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)},
        ),
    ]
    transport = authorized_transport(REPORT_URL, session=FakeSession(responses))

    results = [transport.submit({}) for _ in responses]

    assert all(result.success is False for result in results)
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("failure_site", ["send", "iter_content"])
def test_submit_request_exception_retries_once_and_closes(failure_site):
    from src.core.error_report_transport import ErrorReportTransport

    failed_response = FakeResponse(
        chunks=[requests.exceptions.ChunkedEncodingError("stream-secret")]
    )
    first = (
        requests.ConnectionError("send-secret")
        if failure_site == "send"
        else failed_response
    )
    session = FakeSession([first, accepted_response()])
    backoffs = []

    result = authorized_transport(
        REPORT_URL,
        session=session,
        backoff=lambda milliseconds: backoffs.append(milliseconds),
    ).submit({"safe": True})

    assert result.success is True
    assert len(session.calls) == 2
    assert backoffs == [200]
    if failure_site == "iter_content":
        assert failed_response.closed is True


def test_health_get_request_exception_retries_once():
    from src.core.error_report_transport import ErrorReportTransport

    session = FakeSession([
        requests.ConnectionError("get-secret"),
        FakeResponse(
            status_code=200,
            body={"service": "issue-relay", "schema": 1, "mode": "active"},
        ),
    ])

    result = authorized_transport(
        REPORT_URL,
        session=session,
        backoff=lambda _milliseconds: None,
    ).health()

    assert result.success is True
    assert [call[0] for call in session.calls] == ["GET", "GET"]


@pytest.mark.parametrize("status_code", [502, 503, 504])
def test_gateway_failure_retries_once(status_code):
    from src.core.error_report_transport import ErrorReportTransport

    responses = [
        FakeResponse(status_code=status_code, body={}),
        FakeResponse(status_code=status_code, body={}),
    ]
    session = FakeSession(responses)

    result = authorized_transport(
        REPORT_URL, session=session, backoff=lambda _milliseconds: None
    ).submit({"safe": True})

    assert result.success is False
    assert result.status_code == status_code
    assert len(session.calls) == 2
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422, 429, 500])
def test_non_retryable_http_status_has_no_immediate_retry(status_code):
    from src.core.error_report_transport import ErrorReportTransport

    session = FakeSession([FakeResponse(status_code=status_code, body={})])

    result = authorized_transport(REPORT_URL, session=session).submit({"safe": True})

    assert result.success is False
    assert result.status_code == status_code
    assert len(session.calls) == 1


def test_transport_never_logs_payload_response_or_network_exception(caplog):
    from src.core.error_report_transport import ErrorReportTransport

    payload_secret = "payload-secret-value"
    response_secret = "response-secret-value"
    exception_secret = "exception-secret-value"
    session = FakeSession([
        requests.ConnectionError(exception_secret),
        FakeResponse(chunks=[response_secret.encode("ascii")]),
    ])
    caplog.set_level(logging.DEBUG, logger="tunnelforge.error_report_transport")

    result = authorized_transport(
        REPORT_URL,
        session=session,
        backoff=lambda _milliseconds: None,
    ).submit({"message": payload_secret})

    assert result.success is False
    assert payload_secret not in caplog.text
    assert response_secret not in caplog.text
    assert exception_secret not in caplog.text
