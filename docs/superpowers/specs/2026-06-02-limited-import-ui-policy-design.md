# Limited Import UI Policy Design

## Purpose

TunnelForge must not make a successful Export feel useless when the dump is `limited_restorable`. A limited dump is not strict enough for the recommended import path, but it can still be useful when the operator understands the limits and explicitly chooses a limited restore.

This design adds a three-state import policy UI:

- `strict_restorable`: recommended import is available.
- `limited_restorable`: import is available only after a clear limited-restore confirmation.
- `not_restorable`: import is blocked with a plain-language reason.

## Terms For Users

The UI must explain the states in plain language.

### Strict Restorable

Plain meaning:

> This dump has enough evidence for the recommended restore path.

Detailed meaning:

- The manifest format is supported.
- Required files and checksums are present.
- The dump records enough snapshot or compatibility evidence.
- The core can enforce import checks before changing the target.

Action:

- Enable Import.
- Label it as recommended.
- Use `strict_manifest=true`.

### Limited Restorable

Plain meaning:

> This dump can be imported, but it is not a fully verified snapshot.

Typical cause:

- Export used multiple database connections, such as `threads=8`.
- Those connections cannot prove that every table was read from the exact same database moment.
- Files/checksums can still be valid, but snapshot consistency is not proven.

Action:

- Enable Import.
- Show the button as a limited restore, not as the recommended strict path.
- Before starting, show a confirmation dialog in plain language.
- Use `strict_manifest=false`.

Required confirmation text must explain:

- The dump files are usable, but this is not a fully verified dump.
- If data changed during Export, different tables may represent slightly different moments.
- A strict restore requires a new Export with settings that can prove snapshot consistency.
- Continue only for test restore, controlled migration, or when the operator accepts this limit.

### Not Restorable

Plain meaning:

> This dump cannot be safely imported by this version of TunnelForge.

Reasons can include:

- The dump manifest is missing, corrupted, or unsupported.
- Required chunk files are missing.
- Checksums do not match.
- The manifest declares unsupported features.
- Object/schema information is incompatible with the target.
- Import would likely produce an incorrect target database.

Action:

- Keep Import disabled.
- Show a concise summary and a details section.
- Do not offer a bypass button from this dialog.

## UI Behavior

The compatibility panel should show three pieces of information:

1. A plain-language status.
2. A short explanation of what that status means.
3. The detailed technical reasons from `warnings` and `blockers`.

Button behavior:

- Strict: `Import 시작` enabled.
- Limited: `제한적 Import 시작` enabled.
- Not restorable: Import disabled.

The import mode descriptions should stay aligned with the policy:

- Full replace can be used for strict or acknowledged limited imports.
- Safe recreate remains unavailable when object restore rules reject it.
- Merge remains a separate operator choice and does not make a limited dump strict.

## Data Flow

Python wrapper:

- `RustDumpImporter.import_dump(...)` needs a `strict_manifest` parameter.
- Default remains `True`.
- UI passes `True` for strict dumps.
- UI passes `False` only after limited restore confirmation.

Rust core:

- `dump.import` already supports `strict_manifest=false` for limited manifests.
- `not_restorable` remains blocked before target mutation.

## Error Handling

- If manifest analysis fails, treat the dump as not restorable for UI purposes.
- If a limited import is attempted and Rust rejects it, show the Rust error directly and keep the log.
- If progress state exists, keep the existing resume/reset behavior.

## Testing

Add focused tests for:

- Limited dump enables the button but marks the import as limited.
- Limited dump import confirmation passes `strict_manifest=false`.
- Not restorable dump keeps the button disabled.
- Plain-language text includes the meaning of `not_restorable`.
- Python wrapper forwards `strict_manifest=False`.

## Out Of Scope

- Creating true cross-connection MySQL snapshot sharing for parallel Export.
- Changing Export scheduling defaults.
- Allowing `not_restorable` bypass.
