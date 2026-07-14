import ast
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
from threading import Thread

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pytest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.9/3.10 compatibility
    import tomli as tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DESKTOP_SCAN_PATHS = (
    "main.py",
    "src",
    "bootstrapper",
    "scripts",
    "installer",
    ".github/workflows",
    "tunnel-manager.spec",
    "pyproject.toml",
)

RETIREMENT_ALLOWLIST = {
    "RELEASER_APP_PRIVATE_KEY": {".github/workflows/version-gate.yml"},
}

RETIREMENT_PATTERNS = (
    r"GITHUB_APP_ID",
    r"GITHUB_APP_PRIVATE_KEY",
    r"GITHUB_APP_INSTALLATION_ID",
    r"GH_APP_PRIVATE_KEY",
    r"RELEASER_APP_PRIVATE_KEY",
    r"github_app_auth",
    r"github_issue_reporter",
    r"error_summary_builder",
    r"load_dotenv",
    r"load_pem_private_key",
    r"\b(?:jwt|dotenv)\.(?:encode|decode|algorithms|main)\b",
    r"\b(?:from|import)\s+(?:jwt|dotenv)(?:\.[A-Za-z_]\w*)?\b",
    r"\b(?:import_module|__import__)\(\s*['\"](?:jwt|dotenv)(?:\.[^'\"]+)?['\"]",
    r"['\"](?:jwt|dotenv)(?:\.[^'\"]+)?['\"]",
)

RETIREMENT_PATTERN = re.compile("|".join(RETIREMENT_PATTERNS), re.IGNORECASE)


def _tracked_desktop_files():
    result = subprocess.run(
        ["git", "ls-files", "--", *DESKTOP_SCAN_PATHS],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(
        (PROJECT_ROOT / relative_path)
        for relative_path in result.stdout.splitlines()
        if relative_path
    )


def _retirement_violations(file_texts):
    violations = []
    for relative_path, text in file_texts.items():
        if relative_path.endswith(".py"):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    imported_modules = ()
                    if isinstance(node, ast.Import):
                        imported_modules = tuple(
                            alias.name for alias in node.names
                        )
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_modules = (node.module,)
                    for module in imported_modules:
                        root_module = module.partition(".")[0].casefold()
                        if root_module in {"jwt", "dotenv"}:
                            violations.append((relative_path, module))
        for match in RETIREMENT_PATTERN.finditer(text):
            token = match.group(0)
            if any(
                token.upper() == allowed.upper()
                and relative_path in paths
                for allowed, paths in RETIREMENT_ALLOWLIST.items()
            ):
                continue
            violations.append((relative_path, token))
    return violations


def _runtime_dependency_names(project_text):
    project = tomllib.loads(project_text).get("project", {})
    return {
        canonicalize_name(Requirement(dependency).name)
        for dependency in project.get("dependencies", [])
    }


def test_retired_reporter_modules_and_embed_scripts_are_absent():
    retired_modules = (
        PROJECT_ROOT / "src/core/github_app_auth.py",
        PROJECT_ROOT / "src/core/github_issue_reporter.py",
        PROJECT_ROOT / "src/core/error_summary_builder.py",
    )
    retired_embed_scripts = (
        PROJECT_ROOT / "scripts/embed_github_credentials.py",
        PROJECT_ROOT / "scripts/github_app_secret_codec.py",
    )

    assert all(not path.exists() for path in (*retired_modules, *retired_embed_scripts))


def test_client_auth_references_are_retired_from_production_and_packaging():
    file_texts = {
        path.relative_to(PROJECT_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in _tracked_desktop_files()
    }

    violations = _retirement_violations(file_texts)

    assert not violations, f"retired client auth references remain: {violations}"


def test_retirement_scan_uses_all_tracked_desktop_inputs_and_excludes_non_client_code():
    scanned_paths = {
        path.relative_to(PROJECT_ROOT).as_posix() for path in _tracked_desktop_files()
    }

    assert "main.py" in scanned_paths
    assert "bootstrapper/bootstrapper.py" in scanned_paths
    assert "scripts/build-installer.ps1" in scanned_paths
    assert "installer/TunnelForge.iss" in scanned_paths
    assert ".github/workflows/version-gate.yml" in scanned_paths
    assert "src/core/error_report_builder.py" in scanned_paths
    assert "tunnel-manager.spec" in scanned_paths
    assert "pyproject.toml" in scanned_paths
    assert not any(path.startswith("docs/") for path in scanned_paths)
    assert not any(path.startswith("tests/") for path in scanned_paths)
    assert not any(path.startswith("services/issue-relay/") for path in scanned_paths)


@pytest.mark.parametrize(
    "injected_reference",
    (
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_INSTALLATION_ID",
        "GH_APP_PRIVATE_KEY",
        "from github_app_auth import GitHubAppAuth",
        "import github_issue_reporter",
        "from error_summary_builder import build_summary",
        "from jwt import encode",
        "import jwt",
        "import os, jwt",
        "from dotenv import load_dotenv",
        "import dotenv",
        "from jwt.algorithms import RSAAlgorithm",
        "from dotenv.main import dotenv_values",
        "hiddenimports = ['jwt']",
        "hiddenimports = ['dotenv.main']",
        'importlib.import_module("jwt.algorithms")',
        "__import__('dotenv.main')",
        "load_pem_private_key(data, password)",
        "jwt.encode(payload, private_key)",
    ),
)
def test_retirement_scan_rejects_injected_client_auth_reference(injected_reference):
    assert _retirement_violations({"synthetic.py": injected_reference})


def test_retirement_scan_allows_release_auth_and_github_download_constants():
    legitimate = {
        ".github/workflows/version-gate.yml": (
            "private-key: ${{ secrets.RELEASER_APP_PRIVATE_KEY }}"
        ),
        "src/version.py": (
            'GITHUB_OWNER = "sanghyun-io"\n'
            'GITHUB_REPO = "tunnelforge"\n'
            'RELEASES_PAGE_URL = "https://github.com/sanghyun-io/tunnelforge/releases"\n'
        ),
    }

    assert _retirement_violations(legitimate) == []


def test_pem_and_key_files_are_ignored_without_breaking_relay_example_exception():
    checks = {
        "secrets/legacy-private-key.pem": True,
        "secrets/legacy-private-key.key": True,
        "services/issue-relay/.dev.vars": True,
        "services/issue-relay/.dev.vars.example": False,
    }

    for relative_path, expected_ignored in checks.items():
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--", relative_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        assert (result.returncode == 0) is expected_ignored, (
            relative_path,
            result.stdout,
            result.stderr,
        )


def test_runtime_dependencies_remove_direct_auth_but_keep_requests():
    project_text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = _runtime_dependency_names(project_text)

    assert "pyjwt" not in dependencies
    assert "python-dotenv" not in dependencies
    assert "requests" in dependencies


def test_runtime_dependency_parser_handles_single_quoted_entries():
    project_text = """\
[project]
dependencies = [
    'PyJWT>=2.8.0',
    'requests>=2.28.0',
]
"""

    assert _runtime_dependency_names(project_text) == {"pyjwt", "requests"}


def test_runtime_dependency_parser_handles_pep508_marker_quotes():
    project_text = '''\
[project]
dependencies = [
    "requests>=2.28",
    "PyJWT; python_version < '3.12'",
]
'''

    assert _runtime_dependency_names(project_text) == {"pyjwt", "requests"}


@pytest.mark.parametrize("spelling", ["python_dotenv", "python.dotenv"])
def test_runtime_dependency_parser_canonicalizes_distribution_names(spelling):
    project_text = f"[project]\ndependencies = ['{spelling}>=1.0']\n"

    assert _runtime_dependency_names(project_text) == {"python-dotenv"}


def test_legacy_direct_auth_setup_is_removed_without_banning_release_history():
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    secrets_readme = (PROJECT_ROOT / "secrets/README.md").read_text(encoding="utf-8")
    readmes = "\n".join(
        (PROJECT_ROOT / name).read_text(encoding="utf-8") for name in ("README.md", "README.ko.md")
    )

    for text in (env_example, secrets_readme, readmes):
        assert "GITHUB_APP_PRIVATE_KEY" not in text
        assert "GITHUB_APP_INSTALLATION_ID" not in text
        assert "GitHub App-based error reporting" not in text
        assert "GitHub App 기반 오류 보고" not in text

    assert "RELEASER_APP_PRIVATE_KEY" in (
        PROJECT_ROOT / ".github/workflows/version-gate.yml"
    ).read_text(encoding="utf-8")


def test_relay_operator_artifacts_exist_and_cover_the_secure_lifecycle():
    relay_root = PROJECT_ROOT / "services/issue-relay"
    readme = (relay_root / "README.md").read_text(encoding="utf-8")
    error_reporting = (PROJECT_ROOT / "docs/error_reporting.md").read_text(
        encoding="utf-8"
    )
    combined = f"{readme}\n{error_reporting}"

    required_phrases = (
        "Metadata: Read-only",
        "Issues: Read and write",
        "openssl pkcs8 -topk8 -nocrypt",
        "npx wrangler login --use-keyring",
        "npx wrangler d1 create tunnelforge-issue-relay",
        "npx wrangler d1 migrations apply tunnelforge-issue-relay --remote",
        "Cloudflare Dashboard",
        "Variables and Secrets",
        "multiline encrypted secret",
        "RELAY_MODE=off",
        "RELAY_MODE=shadow",
        "RELAY_MODE=canary",
        "RELAY_MODE=active",
        "npx wrangler rollback",
        "npx wrangler secret delete",
    )
    for phrase in required_phrases:
        assert phrase in combined


def test_relay_runbook_never_places_secret_values_in_commands():
    relay_root = PROJECT_ROOT / "services/issue-relay"
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (relay_root / "README.md", PROJECT_ROOT / "docs/error_reporting.md")
    )

    assert not re.search(r"(?im)^.*\|\s*npx\s+wrangler\s+secret\s+put\b", combined)
    assert not re.search(
        r"(?im)npx[ \t]+wrangler[ \t]+secret[ \t]+put[ \t]+\S+[ \t]+\S+",
        combined,
    )
    assert "paste a key into chat" not in combined.casefold()
    assert "--private-key" not in combined
    assert "npx wrangler secret put GITHUB_APP_PRIVATE_KEY" not in combined


def test_relay_runbook_describes_canary_as_operator_control_not_fixture_enforcement():
    readme = (
        PROJECT_ROOT / "services/issue-relay/README.md"
    ).read_text(encoding="utf-8")
    guide = (PROJECT_ROOT / "docs/error_reporting.md").read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)

    assert "canary accepts authenticated operator requests" in readme
    assert (
        "operator rollout deliberately submits only the designated synthetic "
        "fixture and one recurrence"
    ) in normalized_readme
    assert "`canary` permits only" not in guide


def test_relay_runbook_uses_interactive_prompts_only_for_one_line_secrets():
    readme = (
        PROJECT_ROOT / "services/issue-relay/README.md"
    ).read_text(encoding="utf-8")

    for name in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "INSTALLATION_ID_HMAC_KEY",
        "CANARY_ADMIN_TOKEN",
    ):
        assert f"npx wrangler secret put {name}" in readme
    assert "npx wrangler secret put GITHUB_APP_PRIVATE_KEY" not in readme
    assert "Cloudflare Dashboard" in readme
    assert "multiline encrypted secret" in readme


def test_relay_smoke_reads_only_endpoint_and_mode_and_uses_synthetic_fixture():
    smoke = (
        PROJECT_ROOT / "services/issue-relay/scripts/smoke.mjs"
    ).read_text(encoding="utf-8")

    environment_reads = set(re.findall(r"process\.env\.([A-Z][A-Z0-9_]*)", smoke))
    assert environment_reads == {"RELAY_ENDPOINT", "RELAY_MODE"}
    assert "valid-minimal.json" in smoke
    assert "GITHUB_APP_PRIVATE_KEY" not in smoke
    assert "CANARY_ADMIN_TOKEN" not in smoke
    assert "process.argv" not in smoke


@contextmanager
def _synthetic_relay(mode):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/health"
            self._json_response(
                200, {"service": "issue-relay", "schema": 1, "mode": mode}
            )

        def do_POST(self):
            assert self.path == "/v1/reports"
            length = int(self.headers["content-length"])
            payload = json.loads(self.rfile.read(length))
            assert payload["report"]["report_schema_version"] == 1
            assert payload["error"]["sanitized_message"] == "Synthetic smoke fixture."
            responses = {
                "off": (
                    503,
                    {"error": {"code": "service_unavailable", "retryable": True}},
                ),
                "shadow": (
                    202,
                    {
                        "status": "accepted",
                        "receipt": "123e4567-e89b-42d3-a456-426614174000",
                    },
                ),
                "canary": (
                    401,
                    {"error": {"code": "unauthorized", "retryable": False}},
                ),
                "active": (
                    201,
                    {
                        "status": "created",
                        "issue_url": "https://github.com/sanghyun-io/tunnelforge/issues/1",
                    },
                ),
            }
            status, body = responses[mode]
            self._json_response(status, body)

        def _json_response(self, status, body):
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "synthetic relay server thread did not stop"


def _run_node_smoke(command, environment, timeout_seconds=15):
    try:
        return subprocess.run(
            command,
            cwd=PROJECT_ROOT / "services/issue-relay",
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        def output_text(value):
            if value is None:
                return "<none>"
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value

        pytest.fail(
            f"Node smoke timed out after {timeout_seconds} seconds\n"
            f"stdout:\n{output_text(error.stdout)}\n"
            f"stderr:\n{output_text(error.stderr)}"
        )


@pytest.mark.parametrize("mode", ("off", "shadow", "canary", "active"))
def test_relay_smoke_exercises_synthetic_mode_contract(mode):
    smoke = PROJECT_ROOT / "services/issue-relay/scripts/smoke.mjs"
    with _synthetic_relay(mode) as endpoint:
        environment = os.environ.copy()
        environment.update({"RELAY_ENDPOINT": endpoint, "RELAY_MODE": mode})
        result = _run_node_smoke(
            ["node", str(smoke)],
            environment,
        )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"mode={mode}" in result.stdout
    assert "synthetic smoke passed" in result.stdout


def test_relay_smoke_timeout_reports_bounded_diagnostics(monkeypatch):
    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=kwargs.get("args", ["node", "scripts/smoke.mjs"]),
            timeout=kwargs["timeout"],
            output="partial smoke stdout",
            stderr="partial smoke stderr",
        )

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    with pytest.raises(pytest.fail.Exception) as failure:
        _run_node_smoke(
            ["node", "scripts/smoke.mjs"],
            {"RELAY_ENDPOINT": "https://relay.example.test", "RELAY_MODE": "off"},
        )

    message = str(failure.value)
    assert "Node smoke timed out after 15 seconds" in message
    assert "partial smoke stdout" in message
    assert "partial smoke stderr" in message


@pytest.mark.parametrize(
    ("endpoint", "expected_error"),
    (
        (
            "http://relay.example.test",
            "endpoint must use HTTPS; HTTP is allowed only for loopback mocks",
        ),
        (
            "https://user:password@relay.example.test",
            "endpoint must be an origin without credentials, path, query, or fragment",
        ),
        (
            "https://relay.example.test/v1/reports",
            "endpoint must be an origin without credentials, path, query, or fragment",
        ),
        (
            "https://relay.example.test?mode=active",
            "endpoint must be an origin without credentials, path, query, or fragment",
        ),
    ),
)
def test_relay_smoke_executes_endpoint_rejection_guardrails(endpoint, expected_error):
    environment = os.environ.copy()
    environment.update({"RELAY_ENDPOINT": endpoint, "RELAY_MODE": "active"})

    result = _run_node_smoke(
        ["node", "scripts/smoke.mjs"],
        environment,
    )

    assert result.returncode == 1
    assert expected_error in result.stderr


def test_relay_smoke_remote_active_executes_health_only_without_report_post():
    smoke_uri = (
        PROJECT_ROOT / "services/issue-relay/scripts/smoke.mjs"
    ).resolve().as_uri()
    program = f"""
const calls = [];
globalThis.fetch = async (input, init = {{}}) => {{
  calls.push({{ url: String(input), method: init.method ?? "GET" }});
  return Response.json({{ service: "issue-relay", schema: 1, mode: "active" }});
}};
await import({json.dumps(smoke_uri)});
await new Promise((resolve) => setTimeout(resolve, 0));
if (calls.length !== 1 || calls[0].method !== "GET" || !calls[0].url.endsWith("/health")) {{
  throw new Error(`unexpected remote active calls: ${{JSON.stringify(calls)}}`);
}}
console.log("remote active health-only verified");
"""
    environment = os.environ.copy()
    environment.update(
        {
            "RELAY_ENDPOINT": "https://relay.example.test",
            "RELAY_MODE": "active",
        }
    )

    result = _run_node_smoke(
        ["node", "--input-type=module", "--eval", program],
        environment,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "remote active health-only verified" in result.stdout


def test_relay_dev_vars_example_contains_names_only_not_secret_material():
    example = (
        PROJECT_ROOT / "services/issue-relay/.dev.vars.example"
    ).read_text(encoding="utf-8")
    assignments = {
        name: value.strip()
        for name, value in re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=(.*)$", example)
    }

    assert set(assignments) == {
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "INSTALLATION_ID_HMAC_KEY",
        "CANARY_ADMIN_TOKEN",
    }
    assert all(
        not value or (value.startswith("<") and value.endswith(">"))
        for value in assignments.values()
    )
    assert "BEGIN PRIVATE KEY" not in example
    assert "BEGIN RSA PRIVATE KEY" not in example
