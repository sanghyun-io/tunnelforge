import { parseRelayMode, verifyCanaryAuthorization, type RelayMode } from "./modes";
import {
  GitHubIssueError,
  inspectRoutedIssue,
  upsertIssue,
  type GitHubIssueEnv,
  type InstallationTokenProvider,
} from "./github-issues";
import {
  consumeInstallationHourlyQuota,
  consumeGlobalWriteBudgetForCurrentLease,
  consumeGlobalWriteBudgetForCurrentRoute,
  createProcessLocalEdgeKeyDeriver,
  createEdgeRateLimiter,
  hmacInstallationId,
  type EdgeRateLimiter,
} from "./quotas";
import { parseReport as parseIncomingReport } from "./schema";
import {
  RelayStore,
  cleanupExpiredRelayState,
  type ReportActionKind,
} from "./store";
import type { ParseReportResult } from "./types";

export interface RelayEnv {
  RELAY_MODE?: string;
  DB?: D1Database;
  INSTALLATION_ID_HMAC_KEY?: string;
  CANARY_ADMIN_TOKEN?: string;
  GITHUB_APP_ID?: string;
  GITHUB_APP_INSTALLATION_ID?: string;
  GITHUB_APP_PRIVATE_KEY?: string;
}

export interface RelayGitHubOptions {
  readonly fetch?: typeof fetch;
  readonly getInstallationToken?: InstallationTokenProvider;
  readonly timeoutMs?: number;
  readonly now?: () => number;
}

export interface RelayWorkerOptions {
  readonly mode?: RelayMode;
  readonly parseReport?: (request: Request) => Promise<ParseReportResult>;
  readonly receipt?: () => string;
  readonly now?: () => number;
  readonly edgeLimiter?: EdgeRateLimiter;
  readonly github?: RelayGitHubOptions;
}

export interface RelayWorker {
  fetch(request: Request, env: RelayEnv): Promise<Response>;
  scheduled(controller: ScheduledController, env: RelayEnv): Promise<void>;
}

const JSON_HEADERS = Object.freeze({
  "cache-control": "no-store",
  "content-type": "application/json; charset=utf-8",
});

const ERROR_RESPONSES = Object.freeze({
  service_unavailable: Object.freeze({
    status: 503,
    body: JSON.stringify({
      error: { code: "service_unavailable", retryable: true },
    }),
  }),
  unauthorized: Object.freeze({
    status: 401,
    body: JSON.stringify({
      error: { code: "unauthorized", retryable: false },
    }),
  }),
  rate_limited: Object.freeze({
    status: 429,
    body: JSON.stringify({
      error: { code: "rate_limited", retryable: true },
    }),
  }),
  not_found: Object.freeze({
    status: 404,
    body: JSON.stringify({
      error: { code: "not_found", retryable: false },
    }),
  }),
  method_not_allowed: Object.freeze({
    status: 405,
    body: JSON.stringify({
      error: { code: "method_not_allowed", retryable: false },
    }),
  }),
  https_required: Object.freeze({
    status: 400,
    body: JSON.stringify({
      error: { code: "https_required", retryable: false },
    }),
  }),
  route_unknown: Object.freeze({
    status: 503,
    body: JSON.stringify({
      error: { code: "route_unknown", retryable: true },
    }),
  }),
  internal_error: Object.freeze({
    status: 500,
    body: JSON.stringify({
      error: { code: "internal_error", retryable: true },
    }),
  }),
});

function fixedError(kind: keyof typeof ERROR_RESPONSES): Response {
  const error = ERROR_RESPONSES[kind];
  return new Response(error.body, {
    status: error.status,
    headers: JSON_HEADERS,
  });
}

function health(mode: RelayMode): Response {
  return new Response(
    `{"service":"issue-relay","schema":1,"mode":"${mode}"}`,
    { status: 200, headers: JSON_HEADERS },
  );
}

function accepted(receipt: string): Response {
  return new Response(JSON.stringify({ status: "accepted", receipt }), {
    status: 202,
    headers: JSON_HEADERS,
  });
}

function successfulIssue(
  status: "created" | "updated" | "duplicate",
  issueUrl: string,
): Response {
  return new Response(
    JSON.stringify({ status, issue_url: issueUrl }),
    {
      status: status === "created" ? 201 : 200,
      headers: JSON_HEADERS,
    },
  );
}

function canonicalIssueUrl(issueNumber: number): string {
  return `https://github.com/sanghyun-io/tunnelforge/issues/${issueNumber}`;
}

function readGitHubEnvironment(env: RelayEnv): GitHubIssueEnv | null {
  const appId = env.GITHUB_APP_ID;
  const installationId = env.GITHUB_APP_INSTALLATION_ID;
  const privateKey = env.GITHUB_APP_PRIVATE_KEY;
  if (
    typeof appId !== "string" ||
    appId.length === 0 ||
    typeof installationId !== "string" ||
    installationId.length === 0 ||
    typeof privateKey !== "string" ||
    privateKey.length === 0
  ) {
    return null;
  }
  return {
    GITHUB_APP_ID: appId,
    GITHUB_APP_INSTALLATION_ID: installationId,
    GITHUB_APP_PRIVATE_KEY: privateKey,
  };
}

export function createRelayWorker(
  options: RelayWorkerOptions = {},
): RelayWorker {
  const parseReport = options.parseReport ?? parseIncomingReport;
  const issueReceipt = options.receipt ?? (() => crypto.randomUUID());
  const now = options.now ?? (() => Math.floor(Date.now() / 1000));
  const edgeLimiter = options.edgeLimiter ?? createEdgeRateLimiter();
  const deriveEdgeKey = createProcessLocalEdgeKeyDeriver();

  return {
    async fetch(request: Request, env: RelayEnv): Promise<Response> {
      const url = new URL(request.url);
      if (url.protocol !== "https:") {
        return fixedError("https_required");
      }

      if (request.method === "GET" && url.pathname === "/health") {
        const mode = options.mode ?? parseRelayMode(env.RELAY_MODE);
        return health(mode);
      }
      if (url.pathname !== "/v1/reports") {
        return fixedError("not_found");
      }
      if (request.method !== "POST") {
        return fixedError("method_not_allowed");
      }

      const mode = options.mode ?? parseRelayMode(env.RELAY_MODE);
      if (mode === "off") {
        return fixedError("service_unavailable");
      }

      if (mode === "shadow") {
        try {
          const nowSeconds = now();
          const ipKey = await deriveEdgeKey(
            "ip",
            request.headers.get("cf-connecting-ip") ?? "",
          );
          if (!edgeLimiter.checkIp(ipKey, nowSeconds).allowed) {
            return fixedError("rate_limited");
          }
          const parsed = await parseReport(request);
          if (!parsed.ok) {
            return parsed.response;
          }
          const installationKey = await deriveEdgeKey(
            "installation",
            parsed.report.report.anonymous_installation_id,
          );
          if (
            !edgeLimiter.checkInstallation(installationKey, nowSeconds).allowed
          ) {
            return fixedError("rate_limited");
          }
          return accepted(issueReceipt());
        } catch {
          return fixedError("internal_error");
        }
      }

      try {
        const nowSeconds = now();
        const ipKey = await deriveEdgeKey(
          "ip",
          request.headers.get("cf-connecting-ip") ?? "",
        );
        if (!edgeLimiter.checkIp(ipKey, nowSeconds).allowed) {
          return fixedError("rate_limited");
        }

        if (mode === "canary") {
          const authorized = await verifyCanaryAuthorization(
            request.headers.get("authorization") ?? undefined,
            env.CANARY_ADMIN_TOKEN,
          );
          if (!authorized) {
            return fixedError("unauthorized");
          }
        }

        const parsed = await parseReport(request);
        if (!parsed.ok) {
          return parsed.response;
        }

        if (
          typeof env.INSTALLATION_ID_HMAC_KEY !== "string" ||
          env.INSTALLATION_ID_HMAC_KEY.length === 0
        ) {
          return fixedError("internal_error");
        }
        const installationHmac = await hmacInstallationId(
          parsed.report.report.anonymous_installation_id,
          env.INSTALLATION_ID_HMAC_KEY,
        );
        if (
          !edgeLimiter.checkInstallation(installationHmac, nowSeconds).allowed
        ) {
          return fixedError("rate_limited");
        }

        if (env.DB === undefined) {
          return fixedError("internal_error");
        }
        if (
          !(await consumeInstallationHourlyQuota(
            env.DB,
            installationHmac,
            nowSeconds,
          ))
        ) {
          return fixedError("rate_limited");
        }

        const store = new RelayStore(env.DB);
        const route = await store.acquireIssueLease(
          parsed.report.report.error_fingerprint,
          nowSeconds,
        );
        if (route.status === "pending") {
          return accepted(issueReceipt());
        }
        if (route.status === "unknown") {
          return fixedError("route_unknown");
        }

        const fingerprint = parsed.report.report.error_fingerprint;

        const markDefiniteFailure = async (
          kind: ReportActionKind,
          actionWindow: number,
          leaseToken?: string,
        ): Promise<void> => {
          const failed = await store.markReportAction(
            installationHmac,
            fingerprint,
            kind,
            actionWindow,
            "failed",
          );
          if (leaseToken !== undefined) {
            if (failed) {
              await store.markIssueFailed(fingerprint, leaseToken);
            } else {
              await store.markIssueUnknown(fingerprint, leaseToken);
            }
          }
        };

        const markAmbiguous = async (
          kind: ReportActionKind,
          actionWindow: number,
          leaseToken?: string,
        ): Promise<void> => {
          await store.markReportAction(
            installationHmac,
            fingerprint,
            kind,
            actionWindow,
            "unknown",
          );
          if (leaseToken !== undefined) {
            await store.markIssueUnknown(fingerprint, leaseToken);
          }
        };

        const runCreate = async (leaseToken: string): Promise<Response> => {
          const action = await store.claimReportAction(
            installationHmac,
            fingerprint,
            "create",
            now(),
            leaseToken,
          );
          if (action.status !== "acquired") {
            await store.markIssueUnknown(fingerprint, leaseToken);
            return fixedError("route_unknown");
          }

          let githubEnv: GitHubIssueEnv | null;
          try {
            githubEnv = readGitHubEnvironment(env);
          } catch {
            githubEnv = null;
          }
          if (githubEnv === null) {
            await markDefiniteFailure(
              "create",
              action.actionWindow,
              leaseToken,
            );
            return fixedError("service_unavailable");
          }

          let authorizationFailure: "lease" | "budget" | null = null;
          let mutationCompleted = false;
          try {
            const result = await upsertIssue(parsed.report, fingerprint, {
              env: githubEnv,
              fetch: options.github?.fetch,
              getInstallationToken: options.github?.getInstallationToken,
              timeoutMs: options.github?.timeoutMs,
              now: options.github?.now ?? now,
              authorizeMutation: async (kind) => {
                if (kind !== "create") {
                  authorizationFailure = "lease";
                  return false;
                }
                const sendNow = now();
                if (
                  !(await store.isIssueLeaseCurrent(
                    fingerprint,
                    leaseToken,
                    sendNow,
                  ))
                ) {
                  authorizationFailure = "lease";
                  return false;
                }
                if (
                  !(await consumeGlobalWriteBudgetForCurrentLease(
                    env.DB as D1Database,
                    "create",
                    fingerprint,
                    leaseToken,
                    sendNow,
                  ))
                ) {
                  authorizationFailure = await store.isIssueLeaseCurrent(
                    fingerprint,
                    leaseToken,
                    sendNow,
                  )
                    ? "budget"
                    : "lease";
                  return false;
                }
                return true;
              },
            });
            mutationCompleted = true;
            if (result.action !== "created") {
              await markAmbiguous(
                "create",
                action.actionWindow,
                leaseToken,
              );
              return fixedError("route_unknown");
            }
            if (
              !(await store.finalizeCreatedIssue(
                installationHmac,
                fingerprint,
                action.actionWindow,
                leaseToken,
                result.issueNumber,
              ))
            ) {
              await markAmbiguous(
                "create",
                action.actionWindow,
                leaseToken,
              );
              return fixedError("route_unknown");
            }
            return successfulIssue("created", result.issueUrl);
          } catch (error) {
            if (!(error instanceof GitHubIssueError)) {
              if (mutationCompleted) {
                await markAmbiguous(
                  "create",
                  action.actionWindow,
                  leaseToken,
                );
                return fixedError("route_unknown");
              }
              await markDefiniteFailure(
                "create",
                action.actionWindow,
                leaseToken,
              );
              return fixedError("service_unavailable");
            }
            if (
              error.code === "mutation_ambiguous" ||
              (error.code === "mutation_not_authorized" &&
                authorizationFailure === "lease")
            ) {
              await markAmbiguous(
                "create",
                action.actionWindow,
                leaseToken,
              );
              return fixedError("route_unknown");
            }
            await markDefiniteFailure(
              "create",
              action.actionWindow,
              leaseToken,
            );
            return error.code === "mutation_not_authorized" &&
              authorizationFailure === "budget"
              ? fixedError("rate_limited")
              : fixedError("service_unavailable");
          }
        };

        const runComment = async (issueNumber: number): Promise<Response> => {
          const action = await store.claimReportAction(
            installationHmac,
            fingerprint,
            "comment",
            now(),
          );
          if (action.status === "complete") {
            let currentIssueNumber = issueNumber;
            while (true) {
              const beforeLookup = await store.acquireIssueLease(
                fingerprint,
                now(),
              );
              if (beforeLookup.status === "acquired") {
                return runCreate(beforeLookup.leaseToken);
              }
              if (beforeLookup.status === "pending") {
                return accepted(issueReceipt());
              }
              if (beforeLookup.status === "unknown") {
                return fixedError("route_unknown");
              }
              currentIssueNumber = beforeLookup.issueNumber;

              let githubEnv: GitHubIssueEnv | null;
              try {
                githubEnv = readGitHubEnvironment(env);
              } catch {
                githubEnv = null;
              }
              if (githubEnv === null) {
                return fixedError("service_unavailable");
              }

              try {
                await inspectRoutedIssue(
                  {
                    env: githubEnv,
                    fetch: options.github?.fetch,
                    getInstallationToken:
                      options.github?.getInstallationToken,
                    timeoutMs: options.github?.timeoutMs,
                    now: options.github?.now ?? now,
                  },
                  fingerprint,
                  currentIssueNumber,
                );
              } catch (error) {
                if (
                  error instanceof GitHubIssueError &&
                  error.code === "route_recovery_required"
                ) {
                  const recovery = await store.acquireIssueRecoveryLease(
                    fingerprint,
                    currentIssueNumber,
                    now(),
                  );
                  if (recovery.status === "acquired") {
                    return runCreate(recovery.leaseToken);
                  }
                  if (recovery.status === "pending") {
                    return accepted(issueReceipt());
                  }
                  if (recovery.status === "unknown") {
                    return fixedError("route_unknown");
                  }
                  currentIssueNumber = recovery.issueNumber;
                  continue;
                }
                if (
                  error instanceof GitHubIssueError &&
                  error.code === "lookup_ambiguous"
                ) {
                  await store.markReadyIssueUnknown(
                    fingerprint,
                    currentIssueNumber,
                  );
                  return fixedError("route_unknown");
                }
                return fixedError("service_unavailable");
              }

              const afterLookup = await store.acquireIssueLease(
                fingerprint,
                now(),
              );
              if (afterLookup.status === "acquired") {
                return runCreate(afterLookup.leaseToken);
              }
              if (afterLookup.status === "pending") {
                return accepted(issueReceipt());
              }
              if (afterLookup.status === "unknown") {
                return fixedError("route_unknown");
              }
              if (afterLookup.issueNumber === currentIssueNumber) {
                return successfulIssue(
                  "duplicate",
                  canonicalIssueUrl(currentIssueNumber),
                );
              }
              currentIssueNumber = afterLookup.issueNumber;
            }
          }
          if (action.status === "unknown") {
            return fixedError("route_unknown");
          }
          if (action.status !== "acquired") {
            return fixedError("service_unavailable");
          }

          let githubEnv: GitHubIssueEnv | null;
          try {
            githubEnv = readGitHubEnvironment(env);
          } catch {
            githubEnv = null;
          }
          if (githubEnv === null) {
            await markDefiniteFailure("comment", action.actionWindow);
            return fixedError("service_unavailable");
          }

          let authorizationFailure: "route" | "budget" | null = null;
          let mutationCompleted = false;
          try {
            const recurrenceCount =
              (await store.countCompletedReportActions(fingerprint, now())) + 1;
            const result = await upsertIssue(parsed.report, fingerprint, {
              env: githubEnv,
              issueNumber,
              recurrenceCount,
              fetch: options.github?.fetch,
              getInstallationToken: options.github?.getInstallationToken,
              timeoutMs: options.github?.timeoutMs,
              now: options.github?.now ?? now,
              authorizeMutation: async (kind) => {
                if (kind !== "comment") {
                  authorizationFailure = "route";
                  return false;
                }
                const sendNow = now();
                const allowed = await consumeGlobalWriteBudgetForCurrentRoute(
                  env.DB as D1Database,
                  "comment",
                  fingerprint,
                  issueNumber,
                  sendNow,
                );
                if (!allowed) {
                  authorizationFailure = await store.isIssueRouteCurrent(
                    fingerprint,
                    issueNumber,
                  )
                    ? "budget"
                    : "route";
                }
                return allowed;
              },
            });
            mutationCompleted = true;
            if (
              result.action !== "commented" ||
              !(await store.markReportAction(
                installationHmac,
                fingerprint,
                "comment",
                action.actionWindow,
                "complete",
              ))
            ) {
              await markAmbiguous("comment", action.actionWindow);
              return fixedError("route_unknown");
            }
            return successfulIssue("updated", result.issueUrl);
          } catch (error) {
            if (!(error instanceof GitHubIssueError)) {
              if (mutationCompleted) {
                await markAmbiguous("comment", action.actionWindow);
                return fixedError("route_unknown");
              }
              await markDefiniteFailure("comment", action.actionWindow);
              return fixedError("service_unavailable");
            }
            if (error.code === "route_recovery_required") {
              await markDefiniteFailure("comment", action.actionWindow);
              const recovery = await store.acquireIssueRecoveryLease(
                fingerprint,
                issueNumber,
                now(),
              );
              if (recovery.status === "acquired") {
                return runCreate(recovery.leaseToken);
              }
              if (recovery.status === "pending") {
                return accepted(issueReceipt());
              }
              return recovery.status === "unknown"
                ? fixedError("route_unknown")
                : fixedError("service_unavailable");
            }
            if (
              error.code === "mutation_not_authorized" &&
              authorizationFailure === "route"
            ) {
              await markDefiniteFailure("comment", action.actionWindow);
              return (await store.isIssueRoutePending(fingerprint, now()))
                ? accepted(issueReceipt())
                : fixedError("route_unknown");
            }
            if (
              error.code === "mutation_ambiguous" ||
              (error.code === "lookup_ambiguous" && error.ambiguous)
            ) {
              await markAmbiguous("comment", action.actionWindow);
              if (error.code === "lookup_ambiguous") {
                await store.markReadyIssueUnknown(fingerprint, issueNumber);
              }
              return fixedError("route_unknown");
            }
            await markDefiniteFailure("comment", action.actionWindow);
            return error.code === "mutation_not_authorized" &&
              authorizationFailure === "budget"
              ? fixedError("rate_limited")
              : fixedError("service_unavailable");
          }
        };

        return route.status === "acquired"
          ? runCreate(route.leaseToken)
          : runComment(route.issueNumber);
      } catch {
        return fixedError("internal_error");
      }
    },

    async scheduled(
      _controller: ScheduledController,
      env: RelayEnv,
    ): Promise<void> {
      const db = env.DB;
      if (db === undefined) {
        throw new Error("missing relay D1 binding");
      }
      await cleanupExpiredRelayState(db, now());
    },
  };
}

export default createRelayWorker();
