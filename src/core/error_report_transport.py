"""Bounded HTTPS transport for anonymous error reports."""

import json
import re
import uuid
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from PyQt6.QtCore import QThread

from src.core.error_reporting_config import ERROR_REPORT_RELAY_URL
from src.core.logger import get_logger


logger = get_logger("error_report_transport")

REQUEST_TIMEOUT = (3.05, 8.0)
RETRY_BACKOFF_MS = 200
MAX_RESPONSE_BYTES = 16 * 1024
MAX_ISSUE_URL_LENGTH = 2048
_RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})
_HEALTH_MODES = frozenset({"off", "shadow", "canary", "active"})
_SUBMIT_CONTRACTS = {
    (202, "accepted"): ("Report accepted.", False, True),
    (201, "created"): ("Report created.", True, False),
    (200, "updated"): ("Existing issue updated.", True, False),
    (200, "duplicate"): ("Duplicate report accepted.", True, False),
}
_CANONICAL_ISSUE_URL = re.compile(
    r"https://github\.com/sanghyun-io/tunnelforge/issues/[1-9][0-9]*"
)


@dataclass(frozen=True)
class RelayResult:
    success: bool
    message: str
    issue_url: str
    status_code: Optional[int]


def _reject_duplicate_json_members(pairs):
    parsed = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError("Duplicate JSON member")
        parsed[key] = value
    return parsed


def _has_forbidden_url_character(value: str) -> bool:
    return any(
        ord(character) < 32
        or ord(character) == 127
        or character.isspace()
        for character in value
    )


def is_valid_relay_url(url: object) -> bool:
    if (
        type(url) is not str
        or not url
        or _has_forbidden_url_character(url)
    ):
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
        and (port is None or 1 <= port <= 65535)
    )


def _qthread_backoff(milliseconds: int) -> None:
    QThread.msleep(milliseconds)


class ErrorReportTransport:
    """Submit reports without exposing response bodies or request payloads."""

    __slots__ = (
        "__relay_url",
        "session",
        "_backoff",
        "_submission_authorizer",
    )

    def __init__(
        self,
        relay_url: str,
        *,
        session: Optional[requests.Session] = None,
        backoff: Optional[Callable[[int], None]] = None,
        submission_authorizer: Optional[Callable[[], bool]] = None,
    ):
        if not is_valid_relay_url(relay_url):
            raise ValueError("A configured HTTPS relay URL is required")
        self.__relay_url = relay_url
        self.session = session if session is not None else requests.Session()
        self._backoff = backoff if backoff is not None else _qthread_backoff
        self._submission_authorizer = submission_authorizer

    def __setattr__(self, name, value):
        if (
            name == "_ErrorReportTransport__relay_url"
            and hasattr(self, name)
        ):
            raise AttributeError("relay URL is immutable")
        object.__setattr__(self, name, value)

    def submit(self, payload) -> RelayResult:
        relay_url = self._validated_relay_url()
        if relay_url is None:
            return _invalid_relay_url_result()
        if self._submission_authorizer is None:
            return RelayResult(False, "Relay request cancelled.", "", None)
        try:
            allowed = self._submission_authorizer()
        except Exception:
            logger.warning("Anonymous error relay authorization failed")
            allowed = False
        if allowed is not True:
            return RelayResult(False, "Relay request cancelled.", "", None)
        return self._request("POST", relay_url, payload=payload)

    def health(self) -> RelayResult:
        relay_url = self._validated_relay_url()
        if relay_url is None:
            return _invalid_relay_url_result()
        parsed = urlsplit(relay_url)
        health_url = urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))
        return self._request("GET", health_url)

    def _validated_relay_url(self):
        try:
            relay_url = self.__relay_url
        except AttributeError:
            return None
        return relay_url if is_valid_relay_url(relay_url) else None

    def _request(self, method: str, url: str, payload=None) -> RelayResult:
        for attempt in range(2):
            response = None
            retry = False
            try:
                response = self._initiate_request(method, url, payload)
                status_code = response.status_code
                if status_code in _RETRYABLE_STATUS_CODES and attempt == 0:
                    retry = True
                elif not 200 <= status_code < 300:
                    logger.warning(
                        "Anonymous error relay returned HTTP status %s", status_code
                    )
                    return RelayResult(
                        False,
                        f"Relay returned HTTP {status_code}.",
                        "",
                        status_code,
                    )
                else:
                    parsed_body = self._read_response(response)
                    result = self._validated_result(method, parsed_body, status_code)
                    if result is not None:
                        return result
                    logger.warning(
                        "Anonymous error relay returned an invalid response"
                    )
                    return RelayResult(
                        False, "Relay returned an invalid response.", "", status_code
                    )
            except requests.RequestException:
                if attempt == 0:
                    retry = True
                else:
                    logger.warning("Anonymous error relay network request failed")
                    return RelayResult(
                        False, "Relay network request failed.", "", None
                    )
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        logger.warning(
                            "Anonymous error relay response cleanup failed"
                        )

            if retry:
                self._backoff(RETRY_BACKOFF_MS)

        return RelayResult(False, "Relay request failed.", "", None)

    def _initiate_request(self, method: str, url: str, payload):
        request_kwargs = {
            "method": method,
            "url": url,
            "headers": {"Accept-Encoding": "identity"},
        }
        if method == "POST":
            request_kwargs["json"] = payload
        prepared_request = requests.Request(**request_kwargs).prepare()

        environment_session = requests.Session()
        try:
            environment_settings = environment_session.merge_environment_settings(
                prepared_request.url,
                proxies={},
                stream=True,
                verify=True,
                cert=None,
            )
        finally:
            environment_session.close()

        return self.session.send(
            prepared_request,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            **environment_settings,
        )

    @staticmethod
    def _read_response(response):
        content_encoding = response.headers.get("Content-Encoding")
        if (
            content_encoding is not None
            and (
                not isinstance(content_encoding, str)
                or content_encoding.strip().casefold() != "identity"
            )
        ):
            return None

        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                declared_length = None
            if declared_length is not None and (
                declared_length < 0 or declared_length > MAX_RESPONSE_BYTES
            ):
                return None

        body = bytearray()
        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue
            remaining = MAX_RESPONSE_BYTES + 1 - len(body)
            body.extend(chunk[:remaining])
            if len(body) > MAX_RESPONSE_BYTES:
                return None
        try:
            parsed = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_members,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return None
        return parsed if type(parsed) is dict else None

    @staticmethod
    def _validated_result(method, parsed_body, status_code):
        if parsed_body is None:
            return None
        if method == "GET":
            if (
                status_code != 200
                or set(parsed_body) != {"service", "schema", "mode"}
                or parsed_body["service"] != "issue-relay"
                or type(parsed_body["schema"]) is not int
                or parsed_body["schema"] != 1
                or type(parsed_body["mode"]) is not str
                or parsed_body["mode"] not in _HEALTH_MODES
            ):
                return None
            mode = parsed_body["mode"]
            return RelayResult(True, f"Relay is {mode}.", "", status_code)

        status = parsed_body.get("status")
        if type(status) is not str:
            return None
        contract = _SUBMIT_CONTRACTS.get((status_code, status))
        if contract is None:
            return None
        message, needs_issue_url, needs_receipt = contract
        if needs_issue_url:
            if set(parsed_body) != {"status", "issue_url"}:
                return None
            issue_url = parsed_body["issue_url"]
            if not _is_canonical_issue_url(issue_url):
                return None
            return RelayResult(True, message, issue_url, status_code)
        if needs_receipt:
            if set(parsed_body) != {"status", "receipt"}:
                return None
            if not _is_canonical_uuid_v4(parsed_body["receipt"]):
                return None
            return RelayResult(True, message, "", status_code)
        return None


def _is_canonical_uuid_v4(value):
    if type(value) is not str:
        return False
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        return False
    return (
        parsed.version == 4
        and parsed.variant == uuid.RFC_4122
        and str(parsed) == value
    )


def _is_canonical_issue_url(url):
    if (
        type(url) is not str
        or len(url) > MAX_ISSUE_URL_LENGTH
        or _has_forbidden_url_character(url)
        or _CANONICAL_ISSUE_URL.fullmatch(url) is None
    ):
        return False
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and parsed.query == ""
        and parsed.fragment == ""
    )


def _invalid_relay_url_result():
    logger.warning("Anonymous error relay URL failed validation")
    return RelayResult(False, "Relay URL is invalid.", "", None)
