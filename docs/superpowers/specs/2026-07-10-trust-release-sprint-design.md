# Trust & Release Sprint Design

## Goal

Prepare TunnelForge for a trustworthy `2.3.1` release candidate by closing the
two confirmed execution-safety gaps, making public capability claims accurate,
and adding stable repository-side regression gates before release validation.

## Scope

This sprint covers:

1. TF-STATUS-079: verify downloaded update packages before they can run.
2. TF-STATUS-080: require confirmation for dangerous operations when the
   environment is unknown or missing, including direct Import flows without a
   tunnel configuration.
3. TF-STATUS-083: add an always-on full Python regression job and preserve the
   existing Rust Core regression job so both can become required checks.
4. TF-STATUS-082: remove public claims that the disabled Schedule feature is
   currently available.
5. TF-STATUS-081: bump all version sources to `2.3.1` only after the preceding
   changes are reviewed and green.

The sprint does not close TF-STATUS-008. The final real-Mac report and a manual
workflow run for the final merged release-candidate SHA remain external release
evidence. It also does not close TF-STATUS-083 until the new checks have enough
successful GitHub runs to be promoted in branch protection.

## Architecture

### Update package integrity

GitHub's Release Asset API already returns `digest: sha256:<64 lowercase hex>`
for current Windows and macOS assets. The release response is the integrity
manifest for this sprint. A selected release asset is valid only when all of
the following bind together:

- expected release tag;
- exact platform and architecture asset name;
- GitHub release download URL;
- positive expected size;
- valid SHA-256 digest.

`src/update_integrity.py` is a dependency-light shared module used by the main
application and the standalone bootstrapper. It parses GitHub digests and
verifies a file's byte count and SHA-256 digest. It imports only Python standard
library modules so the bootstrapper PyInstaller build stays small.

Downloads are written into a unique temporary directory using a `.part` suffix.
After download, the file is verified and atomically renamed to its final name.
Missing, malformed, size-mismatched, or digest-mismatched metadata fails closed,
deletes partial output, and never emits a successful download result.

The main Settings dialog stores the expected digest and size from the worker and
re-verifies immediately before opening or executing the package. This second
check covers local tampering between download completion and the user's Install
action. Verification failure keeps the app running and prevents every launch
API (`subprocess.Popen` and `QDesktopServices.openUrl`).

The bootstrapper applies the same metadata and file-verification contract before
launch. The modular `bootstrapper/downloader.py` and bundled
`bootstrapper/bootstrapper.py` both use `src/update_integrity.py`; duplicated
download orchestration remains only where the current single-entry bootstrapper
build requires it.

This design protects against transfer/cache corruption and package substitution
that does not also alter authenticated GitHub release metadata. Compromise of the
GitHub repository/release authority requires a later public-key-signed manifest
and is explicitly outside this sprint.

### Dangerous-operation defaults

`Environment.DEVELOPMENT` remains the only environment that may bypass the
generic dangerous-operation confirmation. `PRODUCTION` keeps schema-name entry,
and `STAGING` keeps its default-No confirmation. `UNKNOWN`, including a missing
key, `None`, or an unrecognized string, gets a default-No warning that clearly
states the environment is not classified.

Direct Import flows that do not carry a tunnel configuration no longer return
`True` before consulting `ProductionGuard`. They pass an empty configuration,
which follows the `UNKNOWN` confirmation policy. This preserves explicit
development ergonomics while preventing absence of metadata from becoming an
authorization decision.

### Regression gates

The existing `.github/workflows/version-gate.yml` already contains the
`rust-core-regression-gate` PR job. This sprint adds a separate
`python-regression` job to the same workflow. It checks out the exact PR SHA,
sets up Python 3.12 and Rust, installs `.[dev]`, builds `tunnelforge-core` in
release mode, and runs `pytest -q` with `QT_QPA_PLATFORM=offscreen`.

Repository tests parse the workflow and lock the job names and commands. Branch
protection is not changed by repository code. After enough stable PR runs,
`python-regression` and `rust-core-regression-gate` can be added as required
contexts alongside `version-gate`.

### Public capability truth

English and Korean READMEs stop presenting Scheduled Backups & Queries as a
currently available feature or usage tip. They state that the implementation is
disabled in the default UI pending reactivation verification and point to
`SCHEDULE.md` for internal status. Schedule code and flags do not change.

### Release candidate

After all functional and workflow changes pass task reviews, the three version
sources are bumped from `2.3.0` to `2.3.1` using
`python scripts/bump_version.py --bump-type patch`. The feature branch itself is
the release candidate; no tag or GitHub Release is created in this sprint.

## Error Handling

- Release asset digest absent or malformed: report a download-integrity error;
  offer the releases page only as a manual fallback; do not auto-open it.
- Download byte count or SHA-256 mismatch: delete `.part` and final output,
  report failure, and keep the current application alive.
- Launch-time re-verification failure: delete the package, reset install UI,
  show an error, and do not disconnect tunnels or quit the app.
- Unknown environment confirmation rejected: return `False` and do not start
  SQL, cell-edit commit, or Import execution.

## Testing

- Unit tests for digest parsing, size verification, hash verification, unique
  partial-file handling, cancellation cleanup, and exact asset selection.
- Settings tests proving no process/open/quit call occurs after tampering.
- Bootstrapper tests proving missing digest and tampering prevent launch.
- ProductionGuard tests for missing, `None`, invalid, staging, production, and
  explicit development environments.
- Import-dialog test proving missing tunnel metadata invokes unknown confirmation.
- Workflow parser tests for `python-regression` and existing
  `rust-core-regression-gate` contracts.
- README/Schedule contract tests in both languages.
- Full `pytest -q`, Rust Core regression gate, version sync test, and
  `git diff --check` before final review.

## Deferred Work

- Public-key-signed release manifests and certificate pinning.
- OS credential-vault migration.
- Schedule reactivation and One-Click scope expansion.
- Rust Core multiplexing or process pools without measured blocking evidence.
- Branch-protection mutation before stable GitHub run evidence.
- macOS production-ready claim before TF-STATUS-008's final evidence passes.
