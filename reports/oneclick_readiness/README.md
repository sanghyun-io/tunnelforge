# One-Click Readiness Evidence

This directory stores machine-checkable One-Click evidence. Phase A supports
dry-run preview only; non-dry-run capture and apply are disabled.

## Current Evidence

`oneclick-dry-run-evidence.json` is the current readiness artifact. It was
captured from a local MySQL container through Rust Core `oneclick.run` with
dry-run enabled and records preflight, analysis, recommendation, preview,
validation, and final result events without DB mutation.

Validate it with:

```powershell
python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json
```

## Historical Archive

The following are archived historical evidence from the retired open apply
path:

- `oneclick-real-execution-evidence.json` and its template
- `oneclick-charset-evidence.json` and its template
- `oneclick-charset-derivation-evidence.json`

These files preserve controlled local results for audit and regression-shape
compatibility. They are not current live-success or apply-readiness proof.
Their validators check archive integrity only and do not bypass Phase A.

Archive validation remains available:

```powershell
python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json
python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.json
python scripts\validate-oneclick-charset-derivation-evidence.py reports\oneclick_readiness\oneclick-charset-derivation-evidence.json
```

## Refresh

Refresh only dry-run evidence during Phase A:

```powershell
python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json
python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json
```

Do not refresh real-execution, charset-mutation, or PyQt-triggered mutation
evidence. Each retired mutation capture command deliberately returns
`oneclick_apply_disabled` before container seeding, DB access, or artifact
output.

## Phase B Prerequisites

Mutation evidence may be replanned only after exact-plan approval exists and
TF-STATUS-098 is complete. Phase B must also bind target identity and plan hash,
recheck immediate preconditions, preserve no-retry indeterminate outcomes, and
resolve the strong-fencing gate before any capture instruction can invoke
mutation again.

Until those prerequisites pass, use the dry-run artifact and the archived
historical evidence only. Never use production databases for readiness
evidence.
