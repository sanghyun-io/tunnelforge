# Trust & Release Sprint Historical Implementation Plan

> **Historical/superseded:** The task steps and unchecked boxes below record the
> original implementation sequence. They are not executable current guidance.
> The final contract is the superseding record immediately below.

**Goal:** Produce a reviewed `2.3.1` release-candidate branch that fails closed on unverified update packages and unknown dangerous-operation environments, exposes stable Python/Rust PR gates, and accurately documents supported features.

**Architecture:** A standard-library-only `src/update_integrity.py` module provides one digest and file-verification contract to the main app and bootstrapper. Existing UI and download orchestration remain in place but cannot report success or launch a package until verification passes. Release workflow, public documentation, and version/status changes are separate tasks so each can be independently reviewed.

**Tech Stack:** Python 3.9+, PyQt6, requests, pytest, GitHub Actions YAML, Rust/Cargo, PowerShell.

## Global Constraints

- `tunnelforge-core` remains the only DB-operation owner; do not add Python DB mutation paths, direct driver hot paths, or external dump tools.
- No new third-party runtime dependency; update integrity uses Python standard library only.
- GitHub release asset `digest` must be `sha256:` followed by exactly 64 hexadecimal characters; missing or malformed digest fails closed.
- Downloaded file size and SHA-256 must both match before download success and again before launch.
- Explicit `development` remains permissive; `production`, `staging`, and `unknown` retain distinct confirmation behavior.
- Branch protection is not mutated from implementation tasks; only repository workflow contracts are added.
- Schedule remains disabled; One-Click execution scope does not expand.
- Preserve unrelated `.claude/` files and the existing `feat/table-collation-dump-import` worktree.
- Use TDD: record the focused RED command and expected failure before production changes, then GREEN evidence.
- Each implementer runs focused tests while iterating and the full Python suite once before committing.

## Final Superseding Contract

- `VerifiedFileLease` guards verification-to-dispatch identity, and
  `OwnedTempDirectory.create_file()` plus no-clobber publish owns temporary
  file creation.
- Windows cleanup deletes only identity-matched owned children. POSIX must not
  claim equivalent deletion: `.part`, final, and temporary-directory residue
  may remain because standard POSIX lacks identity-conditional unlink.
- Failure and abandonment always prevent download success or launch on every
  platform. macOS/non-Windows automatic package dispatch is disabled; the app
  only reveals the verified package directory.
- Settings and the bootstrapper best-effort discard a completed package before
  dispatch on accept, reject, close, or cancel. The state clear is idempotent;
  a successful Windows `Popen` marks the package dispatched and is not treated
  as abandonment. Generic launch failures do not delete a verified package.
- F1 CI aggregation and bootstrapper self-check are completed historical
  release-trust gates; retain them as regression coverage rather than treating
  the original task steps as current work.

---

### Task 1: Main Application Update Integrity

**Model:** Implementer `gpt-5.6-sol` high; reviewer `gpt-5.6-sol` high.

**Files:**
- Create: `src/update_integrity.py`
- Modify: `src/core/update_downloader.py`
- Modify: `src/ui/workers/update_worker.py`
- Modify: `src/ui/dialogs/settings.py`
- Modify: `tests/test_update_downloader.py`
- Modify: `tests/test_settings_update_launch.py`

**Interfaces:**
- Produces `IntegrityError`, `parse_sha256_digest(raw: object) -> str`, and `verify_file_integrity(path: str | os.PathLike[str], expected_sha256: str, expected_size: int) -> None` in `src/update_integrity.py`.
- `UpdateDownloader.expected_sha256` contains normalized lowercase hex after `get_installer_info()`.
- `UpdateDownloader.verify_downloaded_installer(path: str) -> None` reuses the shared verifier.
- `UpdateDownloadWorker.verification_ready` emits `(sha256: str, size: int)` before download starts.
- Settings stores `_downloaded_installer_sha256` and `_downloaded_installer_size` and re-verifies immediately before any launch API.

- [ ] **Step 1: Add failing integrity and launch tests**

Add tests that require the following behavior:

Implement these concrete cases:

- `test_select_release_asset_requires_valid_sha256_digest`: provide a Windows
  asset whose `digest` is `sha256:` plus 64 hexadecimal characters and assert
  that the selected immutable asset exposes the normalized 64-character hash.
- `test_select_release_asset_rejects_missing_or_malformed_digest`: parameterize
  missing, wrong-algorithm, short, and non-hex digests and assert `DownloadError`.
- `test_download_installer_rejects_digest_mismatch_and_removes_partial_file`:
  stream four known bytes whose expected hash is different, assert
  `DownloadError`, and assert that neither the final path nor its unique `.part`
  sibling remains.
- `test_launch_installer_rechecks_integrity_before_process_start`: first record
  the expected digest and size on the Settings dialog, then tamper with the
  downloaded file and assert that `subprocess.Popen`,
  `QDesktopServices.openUrl`, `close_app`, and application quit are not called.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
pytest tests\test_update_downloader.py tests\test_settings_update_launch.py -q
```

Expected: failures because release assets do not expose digests, shared verification APIs do not exist, and Settings does not re-verify.

- [ ] **Step 3: Implement the shared verifier**

Create the dependency-light module with this contract:

```python
SHA256_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


class IntegrityError(ValueError):
    pass


def parse_sha256_digest(raw: object) -> str:
    match = SHA256_DIGEST_RE.fullmatch(str(raw or "").strip())
    if not match:
        raise IntegrityError("release asset SHA-256 digest is missing or invalid")
    return match.group(1).lower()


def verify_file_integrity(path, expected_sha256: str, expected_size: int) -> None:
    path = Path(path)
    if expected_size <= 0:
        raise IntegrityError("release asset size must be positive")
    if path.stat().st_size != expected_size:
        raise IntegrityError("downloaded file size does not match release metadata")

    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)

    if not hmac.compare_digest(hasher.hexdigest(), expected_sha256):
        raise IntegrityError("downloaded file SHA-256 does not match release metadata")
```

- [ ] **Step 4: Bind asset metadata and make download atomic**

Replace the tuple-only selected asset with an immutable `ReleaseAsset` carrying
`name`, `url`, `size`, and `sha256`. Require the exact release version in the
filename, reject `WebSetup` for the offline Windows updater, and retain macOS
architecture ranking.

`download_installer()` must:

1. create `tempfile.mkdtemp(prefix="tunnelforge-update-")`;
2. write `<filename>.part`;
3. remove partial output on cancellation or any exception;
4. call `verify_file_integrity()`;
5. atomically `os.replace()` the part file with the final path;
6. return only the verified final path.

- [ ] **Step 5: Add launch-time verification**

Add `verification_ready = pyqtSignal(str, int)` to `UpdateDownloadWorker` and
emit normalized digest and size after metadata fetch. Settings connects it to a
slot that stores both values. `_launch_installer()` verifies before showing the
final confirmation and before any process/open/quit call. On failure it removes
the package, resets install state, shows a critical integrity error, and returns.

- [ ] **Step 6: Run focused and full tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
pytest tests\test_update_downloader.py tests\test_settings_update_launch.py tests\test_settings_update_actions.py -q
pytest -q
```

Expected: all pass; no new warnings beyond the known collection warnings.

- [ ] **Step 7: Commit and report**

```powershell
git add src/update_integrity.py src/core/update_downloader.py src/ui/workers/update_worker.py src/ui/dialogs/settings.py tests/test_update_downloader.py tests/test_settings_update_launch.py
git commit -m "fix(security): 업데이트 패키지 무결성 검증"
```

---

### Task 2: Bootstrapper Update Integrity

**Model:** Implementer `gpt-5.6-terra` high; reviewer `gpt-5.6-sol` high.

**Files:**
- Modify: `bootstrapper/downloader.py`
- Modify: `bootstrapper/bootstrapper.py`
- Modify: `bootstrapper/bootstrapper.spec`
- Create: `tests/test_bootstrapper_integrity.py`

**Interfaces:**
- Consumes `parse_sha256_digest()` and `verify_file_integrity()` from `src/update_integrity.py`.
- Both bootstrapper downloader implementations store `expected_sha256` and verify size/hash before returning a final path.
- `BootstrapperApp._launch_installer()` re-verifies the file immediately before `subprocess.Popen`.

- [ ] **Step 1: Add failing bootstrapper tests**

Test exact offline-installer selection, `WebSetup` exclusion, missing/malformed
digest rejection, hash mismatch cleanup, and launch-time tamper rejection:

Implement these concrete cases in `tests/test_bootstrapper_integrity.py`:

- `test_bootstrapper_missing_digest_fails_before_download` asserts that asset
  selection raises before any network stream is opened.
- `test_bootstrapper_does_not_select_websetup_as_offline_installer` provides both
  WebSetup and offline Setup assets and asserts the exact offline Setup is used.
- `test_bootstrapper_digest_mismatch_removes_partial_output` streams known bytes,
  asserts failure, and checks both final and partial outputs are absent.
- `test_bootstrapper_launch_rejects_tampered_installer` constructs the app
  without starting Tk, records expected metadata, tampers with the file, and
  asserts `subprocess.Popen` is not called while `_show_error` is called once.

- [ ] **Step 2: Run focused tests and verify RED**

Run `pytest tests\test_bootstrapper_integrity.py -q`.

Expected: failures because bootstrapper metadata and launch do not verify digest.

- [ ] **Step 3: Implement digest-bound selection and atomic verified download**

Use the exact `TunnelForge-Setup-<latest_version>.exe` name. Do not accept
`TunnelForge-WebSetup.exe` as the offline payload. Parse and store the selected
asset digest, write to a unique `.part`, verify, then atomically rename.

Keep `bootstrapper/downloader.py` and bundled `bootstrapper/bootstrapper.py`
behaviorally aligned. Import `src.update_integrity` statically and confirm the
PyInstaller spec includes the module; add a hidden import only if static analysis
does not include it.

- [ ] **Step 4: Re-verify immediately before launch**

`BootstrapperApp._launch_installer()` must call the shared verifier with the
stored digest and size before `subprocess.Popen`. Failure deletes the file,
shows an error, and leaves the bootstrapper open.

- [ ] **Step 5: Run focused, packaging-contract, and full tests**

Run:

```powershell
pytest tests\test_bootstrapper_integrity.py tests\test_rust_core_packaging.py -q
pytest -q
```

Expected: all pass.

- [ ] **Step 6: Commit and report**

```powershell
git add bootstrapper/downloader.py bootstrapper/bootstrapper.py bootstrapper/bootstrapper.spec tests/test_bootstrapper_integrity.py
git commit -m "fix(security): 부트스트래퍼 다운로드 검증"
```

---

### Task 3: Fail-Closed Dangerous Operation Defaults

**Model:** Implementer `gpt-5.6-luna` high; reviewer `gpt-5.6-terra` high.

**Files:**
- Modify: `src/core/production_guard.py`
- Modify: `src/ui/dialogs/db_import_dialog.py`
- Modify: `src/ui/dialogs/tunnel_config.py`
- Modify: `tests/test_production_guard.py`
- Modify: `tests/test_db_import_dialog.py`
- Modify: `tests/test_tunnel_config_dialog.py`
- Modify: `tests/test_sql_editor_dialog.py`

**Interfaces:**
- `Environment.DEVELOPMENT` is the only permissive fallback.
- `Environment.UNKNOWN` uses a default-No confirmation distinct from staging.
- `RustDumpImportDialog._confirm_production_guard()` always invokes the guard;
  missing tunnel metadata is passed as `{}`.

- [ ] **Step 1: Add failing policy and Import tests**

Implement these concrete cases:

- `test_unknown_environment_requires_default_no_confirmation` parameterizes
  `{}`, `{"environment": None}`, and `{"environment": "invalid"}`; it asserts
  `QMessageBox.warning` receives `QMessageBox.StandardButton.No` as the default
  button and that a No response rejects the operation.
- `test_explicit_development_environment_remains_permissive` passes
  `{"environment": "development"}`, asserts `True`, and asserts no message box
  method is called.
- `test_import_without_tunnel_config_uses_unknown_environment_guard` creates the
  dialog with no tunnel metadata, replaces
  `ProductionGuard.confirm_dangerous_operation` with a spy returning `False`,
  and asserts it is called once with `{}` and the import operation details.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
pytest tests\test_production_guard.py tests\test_db_import_dialog.py tests\test_tunnel_config_dialog.py -q
```

Expected: unknown configurations and direct Import bypass confirmation.

- [ ] **Step 3: Implement explicit environment branches**

Keep production and staging branches unchanged. Add an unknown branch that uses
`QMessageBox.warning`, defaults to No, labels the environment as unclassified,
and includes operation/schema/details. Return `True` without a dialog only when
`environment == Environment.DEVELOPMENT`.

- [ ] **Step 4: Route direct Import through unknown confirmation**

Remove the early `if not self.tunnel_config: return True`. Invoke
`confirm_dangerous_operation(self.tunnel_config or {}, operation, schema, details)`
for every Import, using the dialog's existing operation, schema, and details
values.
Update the tunnel environment tooltip/copy so `(미설정)` states that dangerous
operations require confirmation.

Update the shared SQL Editor test fixture to declare
`{"environment": "development"}` explicitly. Those tests exercise worker and
query behavior rather than environment confirmation; leaving the field absent
would correctly open the new UNKNOWN default-No modal and block offscreen tests.

- [ ] **Step 5: Run focused, SQL Editor, and full tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
pytest tests\test_production_guard.py tests\test_db_import_dialog.py tests\test_tunnel_config_dialog.py tests\test_sql_editor_dialog.py -q
pytest -q
```

Expected: all pass; explicit development fixtures remain permissive.

- [ ] **Step 6: Commit and report**

```powershell
git add src/core/production_guard.py src/ui/dialogs/db_import_dialog.py src/ui/dialogs/tunnel_config.py tests/test_production_guard.py tests/test_db_import_dialog.py tests/test_tunnel_config_dialog.py tests/test_sql_editor_dialog.py
git commit -m "fix(security): 미분류 환경 위험 작업 확인"
```

---

### Task 4: Stable Python and Rust PR Regression Gates

**Model:** Implementer `gpt-5.6-terra` high; reviewer `gpt-5.6-terra` high.

**Files:**
- Modify: `.github/workflows/version-gate.yml`
- Create: `tests/test_ci_workflows.py`

**Interfaces:**
- Existing job name `rust-core-regression-gate` remains unchanged.
- New job name is exactly `python-regression`.
- Branch protection promotion remains an external follow-up after stable runs.

- [ ] **Step 1: Add failing workflow contract tests**

Parse YAML with `yaml.safe_load()` and assert:

```python
def test_version_gate_exposes_required_regression_jobs():
    jobs = load_version_gate()["jobs"]
    assert "rust-core-regression-gate" in jobs
    assert "python-regression" in jobs


def test_python_regression_runs_full_suite_with_built_core():
    job_text = version_gate_job_text("python-regression")
    assert "cargo build --manifest-path migration_core/Cargo.toml --release" in job_text
    assert "pytest -q" in job_text
    assert "QT_QPA_PLATFORM" in job_text
```

- [ ] **Step 2: Run focused tests and verify RED**

Run `pytest tests\test_ci_workflows.py -q`.

Expected: `python-regression` is missing.

- [ ] **Step 3: Add the Python regression job**

Add a PR job on `windows-latest`, timeout 20 minutes, with exact PR SHA checkout,
Python 3.12 setup, Rust toolchain availability, `pip install -e ".[dev]"`, Rust
release build, and `pytest -q` under `QT_QPA_PLATFORM=offscreen` and
`PYTHONUTF8=1`. Preserve existing concurrency and job names.

- [ ] **Step 4: Run workflow tests and full suite**

Run:

```powershell
pytest tests\test_ci_workflows.py tests\test_rust_core_packaging.py -q
pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit and report**

```powershell
git add .github/workflows/version-gate.yml tests/test_ci_workflows.py
git commit -m "ci: Python 전체 회귀 게이트 추가"
```

---

### Task 5: Public Schedule Capability Accuracy

**Model:** Implementer `gpt-5.6-luna` medium; reviewer `gpt-5.6-luna` high.

**Files:**
- Modify: `README.md`
- Modify: `README.ko.md`
- Modify: `tests/test_schedule_docs.py`

**Interfaces:**
- `SCHEDULE_FEATURE_ENABLED = False` remains unchanged.
- Both READMEs describe Schedule as unavailable in the default UI pending
  intentional reactivation and verification.

- [ ] **Step 1: Add failing bilingual documentation tests**

Extend `tests/test_schedule_docs.py` to assert neither README presents
Scheduled Backups & Queries as a currently available feature or recommends it
as a current usage tip. Assert both mention default-UI disabled status and
`SCHEDULE.md`.

- [ ] **Step 2: Run focused tests and verify RED**

Run `pytest tests\test_schedule_docs.py -q`.

Expected: README current-feature claims fail the new assertions.

- [ ] **Step 3: Correct English and Korean public copy**

Remove the feature-table and usage-tip claims or replace them with a concise
availability note. Do not add marketing copy or imply a reactivation date.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
pytest tests\test_schedule_docs.py tests\test_current_status_docs.py -q
pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit and report**

```powershell
git add README.md README.ko.md tests/test_schedule_docs.py
git commit -m "docs: 비활성 Schedule 지원 범위 정정"
```

---

### Task 6: Version and Canonical Status Finalization

**Model:** Implementer `gpt-5.6-terra` high; reviewer `gpt-5.6-sol` high.

**Files:**
- Modify: `src/version.py`
- Modify: `pyproject.toml`
- Modify: `installer/TunnelForge.iss`
- Modify: `docs/current_status.md`
- Modify: `tests/test_current_status_docs.py`

**Interfaces:**
- All version sources become exactly `2.3.1`.
- TF-STATUS-079, TF-STATUS-080, and TF-STATUS-082 become `closed` only with fresh
  focused and full-suite evidence.
- TF-STATUS-081 and TF-STATUS-083 become `fixed_pending_full_verify`: RC merge/tag
  and stable required-check promotion remain downstream.
- TF-STATUS-008 and TF-STATUS-078 remain open.

- [ ] **Step 1: Add failing current-status assertions**

Update current-status tests to derive the source version dynamically and require
the tracker states and recommended execution order described above. Add evidence
phrases for GitHub digest verification, unknown-environment confirmation,
`python-regression`, bilingual Schedule correction, and the `2.3.1` candidate.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
pytest tests\test_current_status_docs.py tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q
```

Expected: source version is still `2.3.0` and tracker statuses are still open.

- [ ] **Step 3: Bump the version and update canonical status**

Run:

```powershell
python scripts\bump_version.py --bump-type patch
```

Confirm output reports `new_version=2.3.1`. Update Summary, Current Baseline,
Issue Tracker, Verification Log, Recommended Execution Order, and Session Log.
Do not claim TF-STATUS-008 or branch-protection promotion complete.

- [ ] **Step 4: Run complete release-candidate verification**

Run sequentially:

```powershell
$env:PYTHONUTF8='1'
$env:QT_QPA_PLATFORM='offscreen'
pytest -q
powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1
cargo test --manifest-path migration_core\Cargo.toml
cargo build --manifest-path migration_core\Cargo.toml --release
pytest tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q
git diff --check
```

Expected: all commands pass. Record exact counts without overwriting historical
verification rows.

- [ ] **Step 5: Commit and report**

```powershell
git add src/version.py pyproject.toml installer/TunnelForge.iss docs/current_status.md tests/test_current_status_docs.py
git commit -m "chore: 2.3.1 릴리스 후보 준비"
```

---

## Final Review and Release Evidence Handoff

After all six task reviews approve:

1. Generate one whole-branch review package from the pre-plan base SHA.
2. Dispatch a `gpt-5.6-sol` final reviewer at `xhigh` or higher.
3. Fix all Critical/Important findings with one fix subagent and re-review.
4. Use `superpowers:finishing-a-development-branch` to present merge/PR options.
5. After the final commit is on the intended release-candidate branch, run the
   manual macOS workflow for that SHA. Real-Mac evidence remains a human/external
   step and must not be fabricated or marked complete.
