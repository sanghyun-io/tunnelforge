# Task 10 Report: GitHub App Authentication and Safe Issue Upsert

## Scope

- Worktree: `C:\Users\QESG\sh-project\tunnelforge\.claude\worktrees\worktree-trust-release-sprint`
- Base: `fa5da70657206f37e019803fd3c1dfcc34e0fca9`
- Recovered and preserved the prior uncommitted Task 10 implementation.
- Implemented only the relay authentication, formatting, GitHub issue, D1 state,
  quota, integration-test, and Task 10 report paths.
- Did not modify status documents, deployment state, real secrets, or unrelated
  files. The pre-existing untracked `services/issue-relay/node_modules/` remains
  excluded.

## RED Evidence

### Recovered Baseline

Command:

```powershell
npm test -- --run test/github-auth.test.ts test/github-issues.test.ts test/issue-format.test.ts --testTimeout=5000 --hookTimeout=5000
```

Result: exit 0, 3 files and 31 tests passed. There were no failures. This proved
the recovered tests did not expose the stalled implementation's remaining state
and timeout defects.

### Route-Bound Comment Authorization

Command:

```powershell
npm test -- --run test/github-issues.test.ts -t "does not comment after the routed issue enters recovery" --testTimeout=5000 --hookTimeout=5000
```

RED result: exit 1. The Worker returned HTTP 200 after the route changed from
ready issue 42 to a recovery lease; the test expected the canonical HTTP 202
pending-lease response. The old path also sent the stale comment.

GREEN result: 1 passed / 18 skipped after atomically making comment budget
eligibility depend on `state='ready'` and the expected issue number.

### Atomic Create Finalization

Command:

```powershell
npm test -- --run test/github-issues.test.ts -t "does not expose a ready route when create action finalization fails" --testTimeout=5000 --hookTimeout=5000
```

RED result: exit 1. After an injected action-finalization failure, the route was
`ready`; the test expected `unknown`.

GREEN result: 1 passed / 19 skipped after route readiness and create-action
completion were moved into one conditional D1 batch transaction bound to the
same installation, fingerprint, action window, lease generation, and issue.

### Response-Body Timeouts

Commands:

```powershell
npm test -- --run test/github-issues.test.ts -t "times out a stalled create response body as an ambiguous mutation" --testTimeout=1000 --hookTimeout=5000
npm test -- --run test/github-auth.test.ts -t "times out a stalled installation-token response body" --testTimeout=1000 --hookTimeout=5000
```

RED results: each command exited 1 because the operation was still pending after
50 ms even though `timeoutMs` was 5 ms. The fetch timers ended when headers
arrived and did not cover JSON body consumption.

GREEN results: each focused test passed after bounded JSON readers extended the
deadline through response parsing. A stalled create body is mutation-ambiguous;
a stalled token body is a fixed authentication-request failure.

### One Budget Reservation Across a 401 Retry

Command:

```powershell
npm test -- --run test/github-issues.test.ts -t "reuses one" --testTimeout=5000 --hookTimeout=5000
```

RED result: exit 1, 2 failed / 21 skipped. With one global slot remaining, the
create path returned 429 instead of 201 and the comment path returned 429 instead
of 200 because the single allowed 401 refresh consumed a second budget slot.

GREEN result: 2 passed / 21 skipped. The first attempt reserves one logical
mutation budget immediately before GitHub; the 401 retry refreshes once and
rechecks the current create lease or exact ready issue immediately before the
second request without spending another slot.

## Implementation

### GitHub App Authentication

- Imports only PKCS#8 `PRIVATE KEY` PEM with Web Crypto.
- Signs RS256 JWTs with `iat = now - 60`, `exp = now + 540`, and the App ID as
  `iss`.
- Uses GitHub API version `2026-03-10`.
- Requests an installation token only for repository `tunnelforge` with
  `issues: write`.
- Caches module-local installation tokens until five minutes before expiry and
  deduplicates concurrent token requests.
- Never logs PEM, JWT, installation token, headers, payloads, or upstream
  exceptions.

### Server-Owned Issue Content

- Builds title, body, labels, and recurrence comments from fixed templates and
  validated structured report fields only.
- Ignores client title/body/label fields and never emits sanitized client free
  text or installation identity.
- Escapes Markdown-sensitive scalar content and enforces fixed title, body, and
  comment bounds.
- Adds one validated hidden fingerprint marker.
- Uses only fixed labels: `bug`, the operation label, and `auto-reported`.
- Ignores upstream `html_url` and returns a canonical TunnelForge issue URL.

### Safe Upsert and D1 State

- Refreshes the installation token once only after a GitHub 401 and never after
  403 or other statuses.
- Creates only under a current route lease and an atomic global create budget.
- Comments only when D1 still routes the fingerprint to the exact ready issue;
  the route predicate and global comment budget are one atomic statement.
- Recovers closed or missing routes through one conditional recovery lease.
- Finalizes a successful create route and action atomically.
- Treats create timeout, transport failure, 5xx, malformed success, and stalled
  success body as ambiguous, moving route and action to `unknown` quarantine.
- Releases only definite pre-send failures. Ambiguous duplicate lookup never
  creates or comments.
- Returns exactly: 201 `created`, 200 `updated`, 200 `duplicate`, or the existing
  canonical 202 `accepted` lease receipt. Successful bodies contain no remote
  display message.

## Final Verification

| Command | Result |
|---|---|
| Controller: `npx vitest run test/github-auth.test.ts test/issue-format.test.ts test/github-issues.test.ts` | 3 files, 37 tests passed |
| Controller: `npm test -- --run` | 7 files, 300 tests passed |
| `npm run typecheck` | exit 0 |
| `npm audit` | 0 vulnerabilities |
| `npx wrangler deploy --dry-run` | exit 0; 89.05 KiB / 18.21 KiB gzip; D1 and off-mode bindings recognized; no deployment |
| Repository-root `git diff --check` | exit 0 |
| `rg -n 'console\.' services/issue-relay/src` | no production console calls |

## Self-Review

- Re-read the Task 10 brief and traced every GitHub POST. The installation-token
  POST is repository/permission scoped. Issue create/comment POSTs pass through
  fixed formatting, token handling, immediate authorization, and bounded fetch
  and response parsing.
- Reviewed every 401 path. There are at most two GitHub attempts, refresh is
  requested only for the second attempt after the first 401, and one logical
  mutation consumes one global reservation while retry ownership is rechecked.
- Reviewed D1 transitions under stale leases, route recovery races, budget
  denial, GitHub ambiguity, and post-GitHub persistence failure. No path can
  automatically create from a pending or unknown generation.
- Reviewed all successful response construction. Status, key names, and
  canonical URL are server-owned; no upstream body or display message is used.
- Reviewed the final working tree for scope and secret leakage. No status doc,
  credential, deployment configuration, dependency manifest, or unrelated path
  changed. No known Task 10 concern remains after the final verification.

## Review Fix Wave 1

This section supersedes the earlier "One Budget Reservation Across a 401
Retry" conclusion. Review established that each GitHub issue/comment POST,
including the one permitted 401 retry, must consume a current atomic global
budget immediately before that individual POST.

### RED Evidence

Command:

```powershell
npm test -- --run test/github-issues.test.ts -t "fresh .* budget|new-window budget" --testTimeout=5000 --hookTimeout=5000
```

RED result: exit 1, 4 failed / 21 skipped. The same-window create assertion
expected budget usage `[5, 5]` and observed `[4, 4]`; the same-window comment
assertion expected `[20, 20]` and observed `[19, 19]`. At an hourly boundary,
both the create and comment retries sent a second POST despite the new window
already being full, returning 201 and 200 instead of the expected 429.

Command:

```powershell
npm test -- --run test/github-issues.test.ts -t "completed recurrence|same installation|stale duplicate" --testTimeout=5000 --hookTimeout=5000
```

RED result: exit 1, 5 failed / 24 skipped. A completed recurrence performed no
live GET, closed and 404 routes for the same installation returned stale 200
duplicate instead of creating issue 43, and routes changed to pending or
unknown during lookup still returned stale 200 duplicate.

### GREEN Evidence

| Command | Result |
|---|---|
| Budget RED command repeated after implementation | 4 passed / 21 skipped |
| Duplicate/recovery RED command repeated after implementation | 5 passed / 24 skipped |
| `npm test -- --run test/store.test.ts test/github-issues.test.ts --testTimeout=5000 --hookTimeout=5000` | 2 files, 64 tests passed |
| `npx vitest run test/github-auth.test.ts test/issue-format.test.ts test/github-issues.test.ts --testTimeout=5000 --hookTimeout=5000` | 3 files, 43 tests passed |
| `npm test -- --run --testTimeout=5000 --hookTimeout=5000` | 7 files, 306 tests passed |
| `npm run typecheck` | exit 0 |
| `npm audit` | 0 vulnerabilities |
| `npx wrangler deploy --dry-run` | exit 0; 92.33 KiB / 18.59 KiB gzip; D1 and off-mode bindings recognized; no deployment |
| Repository-root `git diff --check` | exit 0 |
| `rg -n 'console\.' services/issue-relay/src` | no production console calls |

### Fix Summary

- Every create/comment POST attempt, including a 401 retry, now rechecks the
  current route generation and consumes the current hourly global budget in
  the atomic route-bound quota statement immediately before fetch.
- Completed-action duplicate handling re-resolves D1 before the lookup and
  again before success. Only the exact current ready issue returns 200;
  pending returns 202, unknown fails closed, and a missing or changed route is
  resolved through the lease/recovery path.
- Closed and 404 lookups can claim a new same-installation create action bound
  to the new route lease token. Pending and unknown prior actions still block,
  and the unique create-generation constraint still serializes concurrent
  claims.

### Self-Review

- Traced both mutation call sites through the 401 loop. Authorization runs for
  each POST attempt after token acquisition and immediately before fetch; a
  full new window prevents the retry POST entirely.
- Traced completed-action races before and during the remote GET. Duplicate
  success is emitted only after a second D1 read confirms the same ready issue;
  pending and unknown transitions cannot reuse the stale URL.
- Traced recovery action claims across installations and lease generations.
  A completed old generation cannot suppress a new recovery lease, while
  pending/unknown actions and same-token concurrent claims remain quarantined.
- Rechecked response contracts, ambiguity handling, secret/logging exposure,
  and file scope. No status document, real secret, deployment state,
  dependency manifest, or unrelated file was changed. No known concern remains.
