import re
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_GATE_PATH = PROJECT_ROOT / ".github" / "workflows" / "version-gate.yml"
CREATE_RELEASE_TAG_PATH = PROJECT_ROOT / ".github" / "workflows" / "create-release-tag.yml"
RELEASE_PATH = PROJECT_ROOT / ".github" / "workflows" / "release.yml"


def load_version_gate():
    workflow = yaml.safe_load(VERSION_GATE_PATH.read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 resolver parses the GitHub Actions `on` key as True.
    if True in workflow and "on" not in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def version_gate_job_text(job_name):
    workflow_text = VERSION_GATE_PATH.read_text(encoding="utf-8")
    job_start = workflow_text.index(f"  {job_name}:")
    next_job = workflow_text.find("\n  ", job_start + 1)
    while next_job != -1 and workflow_text[next_job + 3:next_job + 4] == " ":
        next_job = workflow_text.find("\n  ", next_job + 1)
    return workflow_text[job_start:] if next_job == -1 else workflow_text[job_start:next_job]


def load_workflow(path):
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in workflow and "on" not in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def assert_external_actions_are_sha_pinned(workflow_text):
    external_uses = re.findall(r"^\s*uses:\s*([^\s]+)", workflow_text, re.MULTILINE)

    assert external_uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action) for action in external_uses)


def test_version_gate_exposes_required_regression_jobs():
    jobs = load_version_gate()["jobs"]

    assert "rust-core-regression-gate" in jobs
    assert "python-regression" in jobs


def test_version_gate_defaults_to_read_only_contents_permission():
    workflow = load_version_gate()

    assert workflow["permissions"] == {"contents": "read"}


def test_pr_head_regression_jobs_use_read_only_checkout_without_credentials():
    jobs = load_version_gate()["jobs"]
    expected_pr_head_jobs = {
        "rust-core-regression-gate": {"contents": "read"},
        "python-regression": {"contents": "read"},
        "macos-app-validation": {"contents": "read"},
    }
    pr_head_jobs = {}

    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue
            if step.get("with", {}).get("ref") == "${{ github.event.pull_request.head.sha }}":
                pr_head_jobs[job_name] = (job, step)

    assert set(pr_head_jobs) == set(expected_pr_head_jobs)
    for job_name, (job, checkout) in pr_head_jobs.items():
        expected_permissions = expected_pr_head_jobs[job_name]
        job_text = version_gate_job_text(job_name)
        assert job["permissions"] == expected_permissions
        assert checkout["with"] == {
            "ref": "${{ github.event.pull_request.head.sha }}",
            "persist-credentials": False,
        }
        assert "contents: write" not in job_text
        assert "pull-requests: write" not in job_text
        assert "actions/create-github-app-token" not in job_text
        assert "token:" not in job_text
        assert "GH_TOKEN" not in job_text
        assert "secrets." not in job_text
        assert "github.token" not in job_text
        for step in job["steps"]:
            assert not any("TOKEN" in key.upper() for key in step.get("env", {}))
            assert "token" not in step.get("with", {})


def test_macos_support_gate_executes_only_trusted_base_code_with_read_token():
    job = load_version_gate()["jobs"]["macos-support-tracking-gate"]
    job_text = version_gate_job_text("macos-support-tracking-gate")
    checkouts = [
        step for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]

    assert len(checkouts) == 1
    checkout = checkouts[0]
    assert job["permissions"] == {
        "contents": "read",
        "issues": "read",
        "checks": "read",
    }
    assert checkout["with"] == {
        "ref": "${{ github.event.pull_request.base.sha }}",
        "persist-credentials": False,
    }
    assert "${{ github.event.pull_request.head.sha }}" not in job_text
    assert "git checkout" not in job_text
    assert "git switch" not in job_text
    assert "python scripts/check-macos-support-gate.py" in job_text
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in job_text


def test_write_capable_jobs_checkout_and_execute_only_trusted_base_code():
    jobs = load_version_gate()["jobs"]
    write_jobs = {
        job_name for job_name, job in jobs.items()
        if "write" in job.get("permissions", {}).values()
    }

    assert write_jobs == {"version-validation", "version-bump"}

    version_validation = jobs["version-validation"]
    assert version_validation["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
    }
    validation_checkout = next(
        step for step in version_validation["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert validation_checkout["with"] == {
        "ref": "${{ github.event.pull_request.base.sha }}",
        "persist-credentials": False,
    }

    version_bump = jobs["version-bump"]
    bump_text = version_gate_job_text("version-bump")
    assert version_bump["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    bump_checkout = next(
        step for step in version_bump["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert bump_checkout["with"] == {
        "ref": "${{ github.event.pull_request.base.sha }}",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert version_bump["needs"] == "version-validation"
    assert "git checkout" not in bump_text
    assert "git switch" not in bump_text
    assert "git reset" not in bump_text
    assert "git restore" not in bump_text
    assert "git read-tree -u" not in bump_text
    assert "scripts/bump_version.py" in bump_text
    assert "git read-tree \"$HEAD_SHA\"" in bump_text
    assert "git push" in bump_text
    assert "needs.version-validation.outputs.bump_type" in bump_text


def test_version_bump_requires_real_version_files_and_pins_token_action():
    validation_text = version_gate_job_text("version-validation")
    bump_text = version_gate_job_text("version-bump")

    assert "Commit messages are not evidence" in validation_text
    assert 'grep -q "^chore: bump version"' not in validation_text
    assert "pulls/${PR_NUMBER}/commits" not in validation_text
    assert 'CURRENT_PY=$(git show "$HEAD_SHA:src/version.py"' in bump_text
    assert 'CURRENT_PROJECT=$(git show "$HEAD_SHA:pyproject.toml"' in bump_text
    assert 'CURRENT_INSTALLER=$(git show "$HEAD_SHA:installer/TunnelForge.iss"' in bump_text
    assert '[ "$CURRENT_PY" = "$EXPECTED" ]' in bump_text
    assert '[ "$CURRENT_PROJECT" = "$EXPECTED" ]' in bump_text
    assert '[ "$CURRENT_INSTALLER" = "$EXPECTED" ]' in bump_text
    assert (
        "uses: actions/create-github-app-token@"
        "bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3"
    ) in bump_text


def test_release_tag_creation_is_manual_approved_and_sha_bound():
    workflow_text = CREATE_RELEASE_TAG_PATH.read_text(encoding="utf-8")
    workflow = load_workflow(CREATE_RELEASE_TAG_PATH)
    job = workflow["jobs"]["create-tag"]

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert "target_sha" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "version" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "confirm" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert workflow["permissions"] == {"contents": "read"}
    assert job["environment"] == "production-release"
    assert job["permissions"] == {"contents": "write"}
    validate_step = next(step for step in job["steps"] if step["name"] == "Validate target and create tag")
    assert validate_step["env"] == {
        "TARGET_SHA": "${{ inputs.target_sha }}",
        "VERSION": "${{ inputs.version }}",
        "CONFIRM": "${{ inputs.confirm }}",
        "GITHUB_TOKEN": "${{ github.token }}",
    }
    assert "github.event.pull_request" not in workflow_text
    assert "git rev-parse origin/main" in workflow_text
    assert 'TARGET_SHA" != "$MAIN_SHA' in workflow_text
    assert 'TAG="v${VERSION}"' in workflow_text
    assert '"$PYTHON_VERSION" != "$VERSION"' in workflow_text
    assert '"$PROJECT_VERSION" != "$VERSION"' in workflow_text
    assert '"$INSTALLER_VERSION" != "$VERSION"' in workflow_text
    assert "src/version.py" in workflow_text
    assert "pyproject.toml" in workflow_text
    assert "installer/TunnelForge.iss" in workflow_text
    assert "actions/create-github-app-token" not in workflow_text
    assert "RELEASER_" not in workflow_text
    assert "private-key:" not in workflow_text
    assert "GITHUB_TOKEN: ${{ github.token }}" in workflow_text
    assert "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd" in workflow_text
    assert "persist-credentials: false" in workflow_text
    assert "persist-credentials: true" not in workflow_text
    assert_external_actions_are_sha_pinned(workflow_text)


def test_release_publication_requires_approval_and_creates_draft():
    workflow_text = RELEASE_PATH.read_text(encoding="utf-8")
    workflow = load_workflow(RELEASE_PATH)
    release_job = workflow["jobs"]["create-release"]

    assert set(workflow["on"]) == {"workflow_dispatch"}
    dispatch_inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert {"tag", "version", "confirm"} <= set(dispatch_inputs)
    assert all(dispatch_inputs[name]["required"] is True for name in ("tag", "version", "confirm"))
    assert "push:" not in workflow_text
    assert "RELEASE_DRAFT" in workflow_text
    assert workflow["permissions"] == {"contents": "read"}
    assert release_job["environment"] == "production-release"
    assert release_job["permissions"] == {"contents": "write"}
    assert "softprops/action-gh-release@718ea10b132b3b2eba29c1007bb80653f286566b" in workflow_text
    release_step = next(
        step for step in release_job["steps"]
        if step.get("uses", "").startswith("softprops/action-gh-release@")
    )
    assert release_step["with"]["tag_name"] == "${{ needs.release-preflight.outputs.tag }}"
    assert "draft: true" in workflow_text
    assert "prerelease: false" in workflow_text
    assert "Apple signing certificate secret is required" in workflow_text
    assert "APPLE_ID is required for release notarization" in workflow_text
    assert "APPLE_TEAM_ID is required for release notarization" in workflow_text
    assert "APPLE_APP_SPECIFIC_PASSWORD is required for release notarization" in workflow_text


def test_release_workflow_preflight_gates_all_secret_release_work():
    workflow_text = RELEASE_PATH.read_text(encoding="utf-8")
    workflow = load_workflow(RELEASE_PATH)
    jobs = workflow["jobs"]
    preflight = jobs["release-preflight"]

    assert set(jobs) == {"release-preflight", "build-windows-installer", "build-macos-app", "create-release"}
    assert preflight["permissions"] == {"contents": "read"}
    assert preflight["runs-on"] == "ubuntu-24.04"
    preflight_checkout = next(
        step for step in preflight["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert preflight_checkout["with"] == {
        "ref": "refs/tags/${{ inputs.tag }}",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    validate_step = next(step for step in preflight["steps"] if step["name"] == "Validate release target")
    assert validate_step["env"] == {
        "TAG": "${{ inputs.tag }}",
        "VERSION": "${{ inputs.version }}",
        "CONFIRM": "${{ inputs.confirm }}",
    }
    assert "^[0-9a-f]{40}$" in workflow_text
    assert "^v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$" in workflow_text
    assert 'git rev-parse "${TAG}^{commit}"' in workflow_text
    assert "git rev-parse origin/main^{commit}" in workflow_text
    assert 'git merge-base --is-ancestor "$TAG_SHA" "$MAIN_SHA"' in workflow_text
    assert 'if [ "$CONFIRM" != "RELEASE_DRAFT" ]; then' in workflow_text
    assert 'if [ "$TAG" != "v${VERSION}" ]; then' in workflow_text
    assert "src/version.py" in workflow_text
    assert "pyproject.toml" in workflow_text
    assert "installer/TunnelForge.iss" in workflow_text
    assert "GH_APP_PRIVATE_KEY" not in workflow_text
    assert "embed_github_credentials.py" not in workflow_text
    assert "RELEASER_" not in workflow_text

    for job_name, job in jobs.items():
        if job_name == "release-preflight":
            continue
        needs = job["needs"]
        needs = [needs] if isinstance(needs, str) else needs
        assert "release-preflight" in needs
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
                assert step["with"]["ref"] == "refs/tags/${{ needs.release-preflight.outputs.tag }}"

        job_text = yaml.safe_dump(job, sort_keys=False)
        assert "needs.release-preflight.outputs.version" in job_text

    assert jobs["build-windows-installer"]["environment"] == "production-release"
    assert jobs["build-macos-app"]["environment"] == "production-release"
    assert_external_actions_are_sha_pinned(workflow_text)


def test_release_macos_artifacts_are_validated_before_upload():
    workflow_text = RELEASE_PATH.read_text(encoding="utf-8")
    verification_index = workflow_text.index("Verify signed and notarized macOS artifacts")
    upload_index = workflow_text.index("Upload macOS release artifacts")

    assert verification_index < upload_index
    verification_text = workflow_text[verification_index:upload_index]
    assert 'codesign --verify --deep --strict --verbose=2 "$APP_PATH"' in verification_text
    assert 'spctl --assess --type execute --verbose "$APP_PATH"' in verification_text
    assert 'xcrun stapler validate "$APP_PATH"' in verification_text
    assert 'codesign --verify --strict --verbose=2 "$DMG_PATH"' in verification_text
    assert 'spctl --assess --type open --context context:primary-signature --verbose "$DMG_PATH"' in verification_text
    assert 'xcrun stapler validate "$DMG_PATH"' in verification_text


def test_required_version_gate_is_terminal_and_aggregates_all_results():
    jobs = load_version_gate()["jobs"]
    job = load_version_gate()["jobs"]["version-gate"]
    job_text = version_gate_job_text("version-gate")
    expected_needs = [
        "macos-support-tracking-gate",
        "rust-core-regression-gate",
        "python-regression",
        "macos-app-validation",
        "version-validation",
        "version-bump",
    ]

    assert job["needs"] == expected_needs
    assert job["if"] == "always()"
    assert job["permissions"] == {}
    assert not any(
        step.get("uses", "").startswith("actions/checkout@")
        for step in job["steps"]
    )
    for needed_job in expected_needs:
        assert f"needs.{needed_job}.result" in job_text
    assert "needs.version-validation.outputs.bump_type" in job_text
    assert 'if [ -z "$BUMP_TYPE" ]; then' in job_text
    assert 'if [ "$VERSION_BUMP_RESULT" != "skipped" ]; then' in job_text
    assert 'if [ "$VERSION_BUMP_RESULT" != "success" ]; then' in job_text
    assert "exit 1" in job_text
    for job_name, candidate in jobs.items():
        if job_name == "version-gate":
            continue
        needs = candidate.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        assert "version-gate" not in needs


def test_rust_core_regression_gate_contract_is_preserved():
    job = load_version_gate()["jobs"]["rust-core-regression-gate"]
    job_text = version_gate_job_text("rust-core-regression-gate")

    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 5
    assert "./scripts/rust-core-regression-gate.ps1" in job_text
    assert "cargo test --manifest-path migration_core/Cargo.toml" in job_text


def test_python_regression_runs_full_suite_with_built_core():
    job = load_version_gate()["jobs"]["python-regression"]
    job_text = version_gate_job_text("python-regression")

    assert job["runs-on"] == "windows-latest"
    assert job["timeout-minutes"] == 20
    assert job["env"]["QT_QPA_PLATFORM"] == "offscreen"
    assert str(job["env"]["PYTHONUTF8"]) == "1"
    assert "${{ github.event.pull_request.head.sha }}" in job_text
    assert "actions/setup-python@v6" in job_text
    assert 'python-version: "3.12"' in job_text
    assert "rustc --version" in job_text
    assert "cargo --version" in job_text
    assert 'pip install -e ".[dev]"' in job_text
    assert "cargo build --manifest-path migration_core/Cargo.toml --release" in job_text
    assert "pytest -q" in job_text
    assert "pyinstaller bootstrapper/bootstrapper.spec" in job_text
    assert "dist\\TunnelForge-WebSetup.exe --self-check" in job_text
    assert "TUNNELFORGE_WEBSETUP_SELF_CHECK_OK" in job_text
