# SSH Host Trust Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace automatic SSH server-key acceptance with explicit first-use SHA-256 approval, persisted host+port trust, changed-key rejection, and pinned forwarding/preflight.

**Architecture:** A Core trust store persists only versioned host, port, algorithm, and SHA-256 fingerprint records under app support. `TunnelEngine` performs an unauthenticated SSH handshake before loading client credentials, compares the presented key, and pins the freshly probed Paramiko key into every authenticated connection. PyQt requests approval on the UI thread; workers and background reconnects fail closed instead of prompting.

**Tech Stack:** Python 3.9+, Paramiko, sshtunnel 0.4.0, PyQt6, pytest.

## Global Constraints

- Approval happens after SSH transport handshake but before client authentication, forwarding, or DB access.
- Unknown hosts display host:port, key algorithm, and `SHA256:<base64-without-padding>`.
- Changed hosts display old/new fingerprints and cannot be approved or overwritten in the connection flow.
- The trust file is not part of `config.json`, config export/import, or backup.
- Raw public-key bytes, private-key paths, usernames, and credentials are not persisted or logged by the trust path.
- Direct DB mode does not perform SSH trust work.
- Existing public tuple return shapes remain compatible.
- Rust DB Core code is unchanged.

---

### Task 1: SSH Host Trust Core

**Files:**
- Create: `src/core/ssh_host_trust.py`
- Modify: `src/core/platform_paths.py`
- Modify: `src/core/tunnel_engine.py`
- Test: `tests/test_ssh_host_trust.py`
- Test: `tests/test_platform_paths.py`
- Test: `tests/test_tunnel_engine.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class SshHostKeyCheck:
    status: str  # trusted | approval_required | changed
    host: str
    port: int
    key_type: str
    fingerprint_sha256: str
    previous_fingerprint_sha256: Optional[str] = None


class SshHostKeyTrustStore:
    def __init__(self, path: Optional[Path] = None): ...
    def check(self, host: str, port: int, key: paramiko.PKey) -> SshHostKeyCheck: ...
    def approve(self, check: SshHostKeyCheck) -> None: ...


class TunnelEngine:
    def __init__(self, trust_store=None, host_key_probe=None): ...
    def inspect_ssh_server(self, config: dict, timeout: int = 5) -> SshHostKeyCheck: ...
    def approve_ssh_server(self, check: SshHostKeyCheck) -> None: ...
```

The engine retains the freshly probed `paramiko.PKey` only in the current call. A public check object never contains serializable raw key material.

- [ ] **Step 1: Write failing fingerprint and persistence tests**

```python
def test_fingerprint_uses_openssh_sha256_without_padding(ed25519_key):
    check = SshHostKeyTrustStore.in_memory().check("bastion.example", 22, ed25519_key)
    assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]+", check.fingerprint_sha256)
    assert "=" not in check.fingerprint_sha256


def test_approval_persists_only_public_identity(tmp_path, ed25519_key):
    path = tmp_path / "ssh_host_trust.json"
    store = SshHostKeyTrustStore(path)
    check = store.check("bastion.example", 2222, ed25519_key)
    store.approve(check)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "hosts": [{
        "host": "bastion.example", "port": 2222,
        "key_type": "ssh-ed25519",
        "fingerprint_sha256": check.fingerprint_sha256,
    }]}
    assert ed25519_key.get_base64() not in path.read_text(encoding="utf-8")


def test_changed_key_cannot_be_approved(tmp_path, first_key, second_key):
    store = SshHostKeyTrustStore(tmp_path / "ssh_host_trust.json")
    store.approve(store.check("bastion.example", 22, first_key))
    changed = store.check("bastion.example", 22, second_key)
    assert changed.status == "changed"
    with pytest.raises(SshHostKeyChangedError):
        store.approve(changed)
```

Also cover host/port independence, algorithm changes, corrupted JSON, unsupported schema versions, atomic replace cleanup, and user-only permissions where the platform supports them.

- [ ] **Step 2: Run RED trust-store tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_ssh_host_trust.py tests\test_platform_paths.py -q`

Expected: collection fails because the trust module and path do not exist.

- [ ] **Step 3: Implement the platform path and atomic trust store**

```python
def ssh_host_trust_file(platform_name=None, home=None, environ=None) -> Path:
    return app_support_dir(platform_name, home, environ) / "ssh_host_trust.json"
```

The trust writer creates the parent, writes canonical UTF-8 JSON to a same-directory temporary file, flushes, calls `os.fsync`, closes, applies mode `0o600` where supported, and calls `os.replace`. A read/validation failure raises `SshHostTrustStoreError`; it never silently returns an empty store.

- [ ] **Step 4: Write failing probe and tunnel pin tests**

```python
def test_unknown_host_stops_before_private_key_load(engine, sample_tunnel_config):
    engine.inspect_ssh_server = MagicMock(return_value=unknown_check())
    engine._load_private_key = MagicMock()
    success, message = engine.start_tunnel(sample_tunnel_config, check_port=False)
    assert success is False
    assert "SHA256:" in message
    engine._load_private_key.assert_not_called()


def test_forwarder_receives_fresh_trusted_key(engine, sample_tunnel_config, server_key):
    engine._host_key_probe = MagicMock(return_value=server_key)
    trust(engine.trust_store, sample_tunnel_config, server_key)
    engine.start_tunnel(sample_tunnel_config, check_port=False)
    assert forwarder.call_args.kwargs["ssh_host_key"] is server_key


def test_target_preflight_uses_reject_policy_and_expected_key(engine, sample_tunnel_config):
    engine.test_target_reachable_from_bastion(sample_tunnel_config)
    ssh_client.set_missing_host_key_policy.assert_called_once()
    assert isinstance(ssh_client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
```

Add equivalent changed/unknown tests for `start_tunnel`, `create_temp_tunnel`, `test_connection`, and `test_target_reachable_from_bastion`. Assert trust error messages contain no `bastion_key`, DB user, or password.

- [ ] **Step 5: Run RED engine tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_tunnel_engine.py -q`

Expected: forwarding has no `ssh_host_key`, target preflight uses `AutoAddPolicy`, and credentials are loaded before trust exists.

- [ ] **Step 6: Implement pre-authentication probe and shared trust requirement**

`probe_ssh_host_key(host, port, timeout)` uses `socket.create_connection`, `paramiko.Transport`, `start_client(timeout=timeout)`, and `get_remote_server_key`, then closes transport/socket in `finally`. It never calls `auth_*` or reads a client private key.

Each SSH public operation calls `_require_trusted_host_key(config, timeout)` before `_load_private_key`. Trusted checks return the current probe key to the caller. Unknown/changed/store-corrupt checks become stable safe messages while preserving existing tuple return shapes. `_build_forwarder` requires `ssh_host_key` and passes it to `SSHTunnelForwarder`. Target preflight loads the expected key into the ephemeral Paramiko client host-key set and uses `RejectPolicy`; remove every `AutoAddPolicy` reference.

- [ ] **Step 7: Run GREEN Core tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_ssh_host_trust.py tests\test_platform_paths.py tests\test_tunnel_engine.py tests\test_connection_test_worker.py -q`

Run: `rg -n "AutoAddPolicy|ssh_host_key" src/core/tunnel_engine.py tests/test_tunnel_engine.py`

Expected: all selected tests pass; source has no `AutoAddPolicy`; every forwarder construction is pinned.

- [ ] **Step 8: Commit**

```powershell
git add src/core/ssh_host_trust.py src/core/platform_paths.py src/core/tunnel_engine.py tests/test_ssh_host_trust.py tests/test_platform_paths.py tests/test_tunnel_engine.py
git commit -m "Fix: enforce SSH host key trust"
```

### Task 2: SSH First-Use Approval UX

**Files:**
- Create: `src/ui/dialogs/ssh_host_key_dialog.py`
- Modify: `src/ui/main_window.py`
- Modify: `src/ui/dialogs/tunnel_config.py`
- Modify: `src/ui/dialogs/cross_engine_migration_endpoint_form.py`
- Modify: `src/ui/workers/test_worker.py`
- Modify: `src/core/i18n/legacy_translate.py`
- Test: `tests/test_ssh_host_key_dialog.py`
- Test: `tests/test_tunnel_config_dialog.py`
- Test: `tests/test_connection_test_worker.py`
- Test: `tests/test_main_window_export_import_labels.py`
- Test: `tests/test_cross_engine_migration_dialog.py`
- Test: `tests/test_i18n.py`

**Interfaces:**

```python
def confirm_unknown_ssh_host(parent, check: SshHostKeyCheck) -> bool: ...
def show_changed_ssh_host(parent, check: SshHostKeyCheck) -> None: ...
def ensure_ssh_host_trusted(parent, engine: TunnelEngine, config: dict) -> bool: ...
```

`confirm_unknown_ssh_host` uses a `QMessageBox` with explicit `신뢰하고 계속` and `취소`; cancel is the default and Escape action. Changed-key UI is a blocking critical message with only Close.

- [ ] **Step 1: Write failing safe-default dialog tests**

```python
def test_unknown_dialog_shows_identity_and_defaults_cancel(qtbot, unknown_check):
    box = build_unknown_ssh_host_dialog(None, unknown_check)
    assert unknown_check.fingerprint_sha256 in box.informativeText()
    assert unknown_check.key_type in box.informativeText()
    assert box.defaultButton() is box.button(QMessageBox.StandardButton.Cancel)


def test_changed_dialog_has_no_trust_button(qtbot, changed_check):
    box = build_changed_ssh_host_dialog(None, changed_check)
    assert changed_check.previous_fingerprint_sha256 in box.informativeText()
    assert changed_check.fingerprint_sha256 in box.informativeText()
    assert all(button.text() != "신뢰하고 계속" for button in box.buttons())
```

- [ ] **Step 2: Run RED dialog tests**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests\test_ssh_host_key_dialog.py tests\test_i18n.py -q`

Expected: dialog module and translations are absent.

- [ ] **Step 3: Implement shared UI-thread approval helper**

The helper calls `engine.inspect_ssh_server` synchronously only from the UI thread. Trusted returns immediately. Approval-required displays the dialog, persists through `approve_ssh_server`, and returns true. Changed/store/probe errors show a safe blocking error and return false. Add exact English translations for every visible Korean string.

- [ ] **Step 4: Write failing integration and signal tests**

```python
def test_main_window_uses_result_signal_not_qthread_finished(window, worker):
    window._run_connection_test(tunnel(), TestType.DB_ONLY, "test")
    assert worker.test_finished.connect.called
    assert worker.finished.connect.call_count == 1  # cleanup only


def test_connection_worker_never_prompts_or_accesses_credentials_for_unknown_host(worker):
    worker.engine.test_connection.return_value = (False, "SSH 호스트 키 승인이 필요합니다")
    worker.run()
    worker.config_mgr.get_tunnel_credentials.assert_not_called()


def test_cross_engine_endpoint_prompts_before_start(form, unknown_check):
    form.start_selected_tunnel()
    approve.assert_called_once_with(form, unknown_check)
```

Also cover TunnelConfigDialog preflight before worker start, manual MainWindow start, cross-engine endpoint start, and no approval from auto-reconnect/monitor paths. A worker receiving an approval-required race result emits failure and never opens a dialog.

- [ ] **Step 5: Run RED integration tests**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests\test_tunnel_config_dialog.py tests\test_connection_test_worker.py tests\test_main_window_export_import_labels.py tests\test_cross_engine_migration_dialog.py -q`

Expected: interactive entry points do not perform the shared approval step and MainWindow connects the wrong QThread signal.

- [ ] **Step 6: Implement interactive approval and background fail-closed behavior**

Call `ensure_ssh_host_trusted` before launching workers or starting tunnels in MainWindow, TunnelConfigDialog, and cross-engine endpoint form. Correct MainWindow result wiring to `worker.test_finished`; retain built-in `worker.finished` only for delete/cleanup. Do not add a QMessageBox call to `ConnectionTestWorker`, scheduler, monitor, or any other background object.

- [ ] **Step 7: Run GREEN UI tests**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests\test_ssh_host_key_dialog.py tests\test_tunnel_config_dialog.py tests\test_connection_test_worker.py tests\test_main_window_export_import_labels.py tests\test_cross_engine_migration_dialog.py tests\test_i18n.py -q`

Expected: all selected tests pass; approval is UI-thread only and default-No.

- [ ] **Step 8: Commit**

```powershell
git add src/ui/dialogs/ssh_host_key_dialog.py src/ui/main_window.py src/ui/dialogs/tunnel_config.py src/ui/dialogs/cross_engine_migration_endpoint_form.py src/ui/workers/test_worker.py src/core/i18n/legacy_translate.py tests/test_ssh_host_key_dialog.py tests/test_tunnel_config_dialog.py tests/test_connection_test_worker.py tests/test_main_window_export_import_labels.py tests/test_cross_engine_migration_dialog.py tests/test_i18n.py
git commit -m "Fix: require SSH fingerprint approval"
```

## Self-Review

- Core and UI requirements are separated into independently reviewable commits.
- Trust is checked before client authentication, forwarding, or DB credentials.
- Every interactive SSH start path can approve; every background path fails closed.
- Changed keys cannot be approved through the first-use flow.
- Existing tuple APIs and the Rust DB operation boundary remain intact.
