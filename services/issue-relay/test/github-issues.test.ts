import {
  applyD1Migrations,
  env,
  type D1Migration,
} from "cloudflare:test";
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import {
  GitHubIssueError,
  upsertIssue,
  type GitHubIssueEnv,
} from "../src/github-issues";
import { fingerprintMarker } from "../src/issue-format";
import {
  createRelayWorker,
  type RelayEnv,
  type RelayWorkerOptions,
} from "../src/index";
import {
  consumeGlobalWriteBudget,
  createEdgeRateLimiter,
} from "../src/quotas";
import {
  REPORT_ACTION_TTL_SECONDS,
  RelayStore,
} from "../src/store";
import type { ErrorReport, ParseReportResult } from "../src/types";

declare global {
  namespace Cloudflare {
    interface Env {
      DB: D1Database;
      TEST_MIGRATIONS: D1Migration[];
    }
  }
}

const NOW_SECONDS = 1_800_000_000;
const NEXT_HOUR_SECONDS =
  (Math.floor(NOW_SECONDS / (60 * 60)) + 1) * 60 * 60;
const BEFORE_NEXT_HOUR_SECONDS = NEXT_HOUR_SECONDS - 1;
const FINGERPRINT = "b".repeat(64);
const ISSUE_URL = "https://github.com/sanghyun-io/tunnelforge/issues/42";
const ROUTE_UNKNOWN_BODY = JSON.stringify({
  error: { code: "route_unknown", retryable: true },
});
const RATE_LIMITED_BODY = JSON.stringify({
  error: { code: "rate_limited", retryable: true },
});

function makeReport(
  installationId = "4d951671-4580-4b5f-9a96-8f92a38d4f77",
  fingerprint = FINGERPRINT,
): ErrorReport {
  return {
    report: {
      report_schema_version: 1,
      anonymous_installation_id: installationId,
      error_fingerprint: fingerprint,
    },
    app: {
      version: "2.3.1",
      package_kind: "frozen",
      ui_language: "en",
    },
    system: {
      os_family: "windows",
      os_version: "11.0",
      architecture: "x86_64",
      locale: "en_US",
      utc_offset_minutes: 540,
    },
    runtime: {
      python_version: "3.13.5",
      qt_version: "6.9.1",
      rust_core_version: "2.3.1",
    },
    operation: {
      kind: "export",
      db_engine: "postgresql",
      db_server_version: "17.5",
      phase: "dump.run",
    },
    error: {
      exception_class: "RuntimeError",
      error_code: "TF-100",
      sanitized_message: "RAW CLIENT MARKDOWN **must not leave the edge**",
      app_frames: [],
    },
  };
}

function githubEnv(appId = "100001"): GitHubIssueEnv {
  return {
    GITHUB_APP_ID: appId,
    GITHUB_APP_INSTALLATION_ID: "200001",
    GITHUB_APP_PRIVATE_KEY: "unused synthetic key",
  };
}

function openIssue(number = 42, fingerprint = FINGERPRINT): Response {
  return Response.json({
    number,
    state: "open",
    body: `Server body\n\n${fingerprintMarker(fingerprint)}`,
    html_url: "https://evil.invalid/untrusted",
  });
}

function issueOptions(
  fetchMock: typeof fetch,
  extra: Record<string, unknown> = {},
) {
  return {
    env: githubEnv(),
    authorizeMutation: vi.fn(async () => true),
    fetch: fetchMock,
    getInstallationToken: vi.fn(async (_env, forceRefresh: boolean) =>
      forceRefresh ? "refreshed-token" : "cached-token",
    ),
    timeoutMs: 25,
    ...extra,
  };
}

function successfulParser(report: ErrorReport): RelayWorkerOptions["parseReport"] {
  return vi.fn(async (): Promise<ParseReportResult> => ({ ok: true, report }));
}

function relayBindings(): RelayEnv {
  return {
    DB: env.DB,
    INSTALLATION_ID_HMAC_KEY: "local-hmac-test-key",
    GITHUB_APP_ID: "100001",
    GITHUB_APP_INSTALLATION_ID: "200001",
    GITHUB_APP_PRIVATE_KEY: "unused synthetic key",
  };
}

function relayRequest(ip = "203.0.113.80"): Request {
  return new Request("https://relay.example/v1/reports", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "cf-connecting-ip": ip,
    },
    body: "{}",
  });
}

function relayWorker(
  report: ErrorReport,
  fetchMock: typeof fetch,
  timeoutMs = 25,
  now: () => number = () => NOW_SECONDS,
) {
  return createRelayWorker({
    mode: "active",
    parseReport: successfulParser(report),
    receipt: () => "123e4567-e89b-42d3-a456-426614174000",
    now,
    edgeLimiter: createEdgeRateLimiter(),
    github: {
      fetch: fetchMock,
      getInstallationToken: vi.fn(async (_env, forceRefresh: boolean) =>
        forceRefresh ? "refreshed-token" : "cached-token",
      ),
      timeoutMs,
    },
  });
}

async function clearRelayTables(): Promise<void> {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM report_actions"),
    env.DB.prepare("DELETE FROM issue_routes"),
    env.DB.prepare("DELETE FROM write_budgets"),
  ]);
}

beforeAll(async () => {
  await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);
});

beforeEach(async () => {
  await clearRelayTables();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("safe GitHub issue operations", () => {
  it("creates from server-owned content and returns only a canonical issue route", async () => {
    const order: string[] = [];
    const fetchMock = vi.fn(async (_input, init) => {
      order.push("fetch");
      expect(init?.method).toBe("POST");
      expect(init?.redirect).toBe("manual");
      const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
      expect(payload.labels).toEqual(["bug", "export", "auto-reported"]);
      expect(String(payload.body)).not.toContain("RAW CLIENT MARKDOWN");
      return Response.json(
        { number: 42, html_url: "https://evil.invalid/not-trusted" },
        { status: 201 },
      );
    });
    const authorizeMutation = vi.fn(async (kind: string) => {
      order.push(`authorize:${kind}`);
      return true;
    });

    const result = await upsertIssue(makeReport(), FINGERPRINT, {
      ...issueOptions(fetchMock as unknown as typeof fetch),
      authorizeMutation,
    });

    expect(result).toEqual({
      action: "created",
      issueNumber: 42,
      issueUrl: ISSUE_URL,
    });
    expect(order).toEqual(["authorize:create", "fetch"]);
  });

  it("updates only an open routed issue carrying the exact hidden marker", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(openIssue())
      .mockResolvedValueOnce(Response.json({ id: 9001 }, { status: 201 }));
    const authorizeMutation = vi.fn(async () => true);

    const result = await upsertIssue(makeReport(), FINGERPRINT, {
      ...issueOptions(fetchMock as unknown as typeof fetch),
      issueNumber: 42,
      recurrenceCount: 8,
      authorizeMutation,
    });

    expect(result).toEqual({
      action: "commented",
      issueNumber: 42,
      issueUrl: ISSUE_URL,
    });
    expect(authorizeMutation).toHaveBeenCalledWith("comment", false);
    const [, commentInit] = fetchMock.mock.calls[1] ?? [];
    const comment = JSON.parse(String(commentInit?.body)) as { body: string };
    expect(comment.body).toContain("current relay window: 8");
    expect(comment.body).not.toContain("RAW CLIENT MARKDOWN");
  });

  it.each([
    ["closed", Response.json({ number: 42, state: "closed", body: fingerprintMarker(FINGERPRINT) })],
    ["missing", new Response(null, { status: 404 })],
  ])("requires an atomic recovery lease for a %s routed issue", async (_case, response) => {
    const fetchMock = vi.fn(async () => response.clone());
    const authorizeMutation = vi.fn(async () => true);

    await expect(
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(fetchMock as unknown as typeof fetch),
        issueNumber: 42,
        authorizeMutation,
      }),
    ).rejects.toMatchObject({
      code: "route_recovery_required",
      ambiguous: false,
    });
    expect(authorizeMutation).not.toHaveBeenCalled();
  });

  it("treats a marker mismatch as ambiguous and never creates or comments", async () => {
    const fetchMock = vi.fn(async () => openIssue(42, "c".repeat(64)));
    const authorizeMutation = vi.fn(async () => true);

    await expect(
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(fetchMock as unknown as typeof fetch),
        issueNumber: 42,
        authorizeMutation,
      }),
    ).rejects.toMatchObject({
      code: "lookup_ambiguous",
      ambiguous: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(authorizeMutation).not.toHaveBeenCalled();
  });

  it("refreshes once and only once after a 401", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(Response.json({ number: 42 }, { status: 201 }));
    const getToken = vi.fn(async (_env, forceRefresh: boolean) =>
      forceRefresh ? "fresh-token" : "stale-token",
    );
    const authorizeMutation = vi.fn(async () => true);

    await expect(
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(fetchMock as unknown as typeof fetch),
        getInstallationToken: getToken,
        authorizeMutation,
      }),
    ).resolves.toMatchObject({ action: "created", issueNumber: 42 });
    expect(getToken.mock.calls.map((call) => call[1])).toEqual([false, true]);
    expect(authorizeMutation.mock.calls).toEqual([
      ["create", false],
      ["create", true],
    ]);
    expect(
      new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get("authorization"),
    ).toBe("Bearer fresh-token");
  });

  it("does not refresh on 403", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 403 }));
    const getToken = vi.fn(async () => "installation-token");

    await expect(
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(fetchMock as unknown as typeof fetch),
        getInstallationToken: getToken,
      }),
    ).rejects.toMatchObject({
      code: "mutation_rejected",
      status: 403,
      ambiguous: false,
    });
    expect(getToken).toHaveBeenCalledTimes(1);
    expect(getToken).toHaveBeenCalledWith(expect.anything(), false, expect.anything());
  });

  it("rejects a mutation redirect without forwarding credentials", async () => {
    const fetchMock = vi.fn(async (_input, init) => {
      expect(init?.redirect).toBe("manual");
      return new Response(null, {
        status: 302,
        headers: { location: "https://redirect.invalid/credential-target" },
      });
    });

    await expect(
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(fetchMock as unknown as typeof fetch),
      }),
    ).rejects.toMatchObject({
      code: "mutation_ambiguous",
      status: 302,
      ambiguous: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("classifies a create timeout or 5xx as unknown after mutation authorization", async () => {
    const timeoutFetch = vi.fn(
      async () => await new Promise<Response>(() => undefined),
    );
    await expect(
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(timeoutFetch as unknown as typeof fetch),
        timeoutMs: 5,
      }),
    ).rejects.toMatchObject({
      code: "mutation_ambiguous",
      ambiguous: true,
    });

    const serverErrorFetch = vi.fn(async () => new Response(null, { status: 500 }));
    await expect(
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(serverErrorFetch as unknown as typeof fetch),
      }),
    ).rejects.toMatchObject({
      code: "mutation_ambiguous",
      status: 500,
      ambiguous: true,
    });
  });

  it("times out a stalled create response body as an ambiguous mutation", async () => {
    const stalledResponse = new Response(
      new ReadableStream({
        start() {
          // Headers are available, but the upstream JSON body never completes.
        },
      }),
      {
        status: 201,
        headers: { "content-type": "application/json" },
      },
    );
    const fetchMock = vi.fn(async () => stalledResponse);

    const outcome = await Promise.race([
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(fetchMock as unknown as typeof fetch),
        timeoutMs: 5,
      }).catch((error: unknown) => error),
      new Promise<"still_pending">((resolve) => {
        setTimeout(() => resolve("still_pending"), 50);
      }),
    ]);

    expect(outcome).toMatchObject({
      code: "mutation_ambiguous",
      ambiguous: true,
    });
  });

  it("fails a timed-out duplicate lookup without authorizing any mutation", async () => {
    const fetchMock = vi.fn(
      async () => await new Promise<Response>(() => undefined),
    );
    const authorizeMutation = vi.fn(async () => true);

    await expect(
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(fetchMock as unknown as typeof fetch),
        issueNumber: 42,
        authorizeMutation,
        timeoutMs: 5,
      }),
    ).rejects.toMatchObject({
      code: "lookup_ambiguous",
      ambiguous: true,
    });
    expect(authorizeMutation).not.toHaveBeenCalled();
  });

  it("does not call GitHub when the immediate mutation authorization fails", async () => {
    const fetchMock = vi.fn(async () => Response.json({ number: 42 }, { status: 201 }));
    const authorizeMutation = vi.fn(async () => false);

    await expect(
      upsertIssue(makeReport(), FINGERPRINT, {
        ...issueOptions(fetchMock as unknown as typeof fetch),
        authorizeMutation,
      }),
    ).rejects.toMatchObject({
      code: "mutation_not_authorized",
      ambiguous: false,
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("relay GitHub integration", () => {
  it("binds a current create lease and global budget immediately before GitHub", async () => {
    const report = makeReport();
    const fetchMock = vi.fn(async (_input, init) => {
      expect(init?.method).toBe("POST");
      expect(
        await env.DB.prepare(
          "SELECT COUNT(*) AS total FROM write_budgets WHERE kind = 'create' AND used = 1",
        ).first("total"),
      ).toBe(2);
      expect(
        await env.DB.prepare(
          "SELECT state FROM issue_routes WHERE fingerprint = ?",
        )
          .bind(FINGERPRINT)
          .first("state"),
      ).toBe("pending");
      return Response.json({ number: 42 }, { status: 201 });
    });

    const response = await relayWorker(
      report,
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(201);
    expect(await response.text()).toBe(
      JSON.stringify({ status: "created", issue_url: ISSUE_URL }),
    );
    expect(
      await env.DB.prepare(
        "SELECT issue_number, state FROM issue_routes WHERE fingerprint = ?",
      )
        .bind(FINGERPRINT)
        .first(),
    ).toEqual({ issue_number: 42, state: "ready" });
    expect(
      await env.DB.prepare("SELECT state FROM report_actions").first("state"),
    ).toBe("complete");
  });

  it("consumes a fresh create budget immediately before the single 401 retry", async () => {
    for (let count = 0; count < 3; count += 1) {
      expect(
        await consumeGlobalWriteBudget(env.DB, "create", NOW_SECONDS),
      ).toBe(true);
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(Response.json({ number: 42 }, { status: 201 }));

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(201);
    expect(await response.text()).toBe(
      JSON.stringify({ status: "created", issue_url: ISSUE_URL }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(
      (
        await env.DB.prepare(
          "SELECT used FROM write_budgets WHERE kind = 'create' ORDER BY bucket",
        ).all<{ used: number }>()
      ).results.map((row) => row.used),
    ).toEqual([5, 5]);
  });

  it("denies a create retry before POST when its new-window budget is full", async () => {
    for (let count = 0; count < 5; count += 1) {
      expect(
        await consumeGlobalWriteBudget(
          env.DB,
          "create",
          NEXT_HOUR_SECONDS,
        ),
      ).toBe(true);
    }
    let currentNow = BEFORE_NEXT_HOUR_SECONDS;
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(async () => {
        currentNow = NEXT_HOUR_SECONDS;
        return new Response(null, { status: 401 });
      })
      .mockResolvedValueOnce(Response.json({ number: 42 }, { status: 201 }));

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
      25,
      () => currentNow,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(429);
    expect(await response.text()).toBe(RATE_LIMITED_BODY);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("returns duplicate without another mutation for a completed recurrence window", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ number: 42 }, { status: 201 }))
      .mockResolvedValueOnce(openIssue());
    const worker = relayWorker(makeReport(), fetchMock as unknown as typeof fetch);
    expect((await worker.fetch(relayRequest(), relayBindings())).status).toBe(201);

    const duplicate = await worker.fetch(
      relayRequest("203.0.113.81"),
      relayBindings(),
    );
    expect(duplicate.status).toBe(200);
    expect(await duplicate.text()).toBe(
      JSON.stringify({ status: "duplicate", issue_url: ISSUE_URL }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    [
      "closed",
      Response.json({
        number: 42,
        state: "closed",
        body: fingerprintMarker(FINGERPRINT),
      }),
    ],
    ["missing", new Response(null, { status: 404 })],
  ])(
    "recovers a %s issue for the same installation after a completed create",
    async (_case, lookupResponse) => {
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(Response.json({ number: 42 }, { status: 201 }))
        .mockResolvedValueOnce(lookupResponse.clone())
        .mockResolvedValueOnce(Response.json({ number: 43 }, { status: 201 }));
      const worker = relayWorker(
        makeReport(),
        fetchMock as unknown as typeof fetch,
      );
      expect((await worker.fetch(relayRequest(), relayBindings())).status).toBe(
        201,
      );

      const recovered = await worker.fetch(
        relayRequest("203.0.113.81"),
        relayBindings(),
      );

      expect(recovered.status).toBe(201);
      expect(await recovered.text()).toBe(
        JSON.stringify({
          status: "created",
          issue_url: "https://github.com/sanghyun-io/tunnelforge/issues/43",
        }),
      );
      expect(fetchMock).toHaveBeenCalledTimes(3);
      expect(
        await env.DB.prepare(
          "SELECT issue_number, state FROM issue_routes WHERE fingerprint = ?",
        )
          .bind(FINGERPRINT)
          .first(),
      ).toEqual({ issue_number: 43, state: "ready" });
      expect(
        await env.DB.prepare(
          "SELECT COUNT(*) AS total FROM report_actions WHERE state = 'complete'",
        ).first("total"),
      ).toBe(2);
    },
  );

  it.each(["pending", "unknown"] as const)(
    "does not return stale duplicate success after the route becomes %s during lookup",
    async (transition) => {
      const store = new RelayStore(env.DB);
      const fetchMock = vi.fn(async (_input, init) => {
        if (init?.method === "POST") {
          return Response.json({ number: 42 }, { status: 201 });
        }
        if (transition === "pending") {
          const recovery = await store.acquireIssueRecoveryLease(
            FINGERPRINT,
            42,
            NOW_SECONDS + 1,
          );
          expect(recovery.status).toBe("acquired");
        } else {
          expect(await store.markReadyIssueUnknown(FINGERPRINT, 42)).toBe(true);
        }
        return openIssue();
      });
      const worker = relayWorker(
        makeReport(),
        fetchMock as unknown as typeof fetch,
      );
      expect((await worker.fetch(relayRequest(), relayBindings())).status).toBe(
        201,
      );

      const response = await worker.fetch(
        relayRequest("203.0.113.81"),
        relayBindings(),
      );

      expect(response.status).toBe(transition === "pending" ? 202 : 503);
      expect(await response.text()).toBe(
        transition === "pending"
          ? JSON.stringify({
              status: "accepted",
              receipt: "123e4567-e89b-42d3-a456-426614174000",
            })
          : ROUTE_UNKNOWN_BODY,
      );
      expect(fetchMock).toHaveBeenCalledTimes(2);
    },
  );

  it("returns updated after a bounded recurrence comment", async () => {
    const store = new RelayStore(env.DB);
    const lease = await store.acquireIssueLease(FINGERPRINT, NOW_SECONDS);
    if (lease.status !== "acquired") {
      throw new Error("expected route lease");
    }
    await store.markIssueReady(FINGERPRINT, lease.leaseToken, 42);
    const report = makeReport("751726cb-eac5-420f-a668-f495b425a59f");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(openIssue())
      .mockResolvedValueOnce(Response.json({ id: 90 }, { status: 201 }));

    const response = await relayWorker(
      report,
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(200);
    expect(await response.text()).toBe(
      JSON.stringify({ status: "updated", issue_url: ISSUE_URL }),
    );
    expect(
      await env.DB.prepare(
        "SELECT COUNT(*) AS total FROM write_budgets WHERE kind = 'comment' AND used = 1",
      ).first("total"),
    ).toBe(2);
  });

  it("reports one recurrence when prior completed evidence expired before cleanup", async () => {
    const store = new RelayStore(env.DB);
    const expired = await store.claimReportAction(
      "c".repeat(64),
      FINGERPRINT,
      "comment",
      NOW_SECONDS - REPORT_ACTION_TTL_SECONDS,
    );
    if (expired.status !== "acquired") {
      throw new Error("expected expired action claim");
    }
    await store.markReportAction(
      "c".repeat(64),
      FINGERPRINT,
      "comment",
      expired.actionWindow,
      "complete",
    );
    const lease = await store.acquireIssueLease(FINGERPRINT, NOW_SECONDS);
    if (lease.status !== "acquired") {
      throw new Error("expected route lease");
    }
    await store.markIssueReady(FINGERPRINT, lease.leaseToken, 42);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(openIssue())
      .mockResolvedValueOnce(Response.json({ id: 91 }, { status: 201 }));

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(200);
    const [, commentInit] = fetchMock.mock.calls[1] ?? [];
    const comment = JSON.parse(String(commentInit?.body)) as { body: string };
    expect(comment.body).toContain("current relay window: 1");
    expect(
      await env.DB.prepare(
        "SELECT COUNT(*) AS total FROM report_actions WHERE fingerprint = ?",
      )
        .bind(FINGERPRINT)
        .first("total"),
    ).toBe(2);
  });

  it("consumes a fresh comment budget immediately before the single 401 retry", async () => {
    for (let count = 0; count < 18; count += 1) {
      expect(
        await consumeGlobalWriteBudget(env.DB, "comment", NOW_SECONDS),
      ).toBe(true);
    }
    const store = new RelayStore(env.DB);
    const lease = await store.acquireIssueLease(FINGERPRINT, NOW_SECONDS);
    if (lease.status !== "acquired") {
      throw new Error("expected route lease");
    }
    await store.markIssueReady(FINGERPRINT, lease.leaseToken, 42);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(openIssue())
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(Response.json({ id: 90 }, { status: 201 }));

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(200);
    expect(await response.text()).toBe(
      JSON.stringify({ status: "updated", issue_url: ISSUE_URL }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(
      (
        await env.DB.prepare(
          "SELECT used FROM write_budgets WHERE kind = 'comment' ORDER BY bucket",
        ).all<{ used: number }>()
      ).results.map((row) => row.used),
    ).toEqual([20, 20]);
  });

  it("denies a comment retry before POST when its new-window budget is full", async () => {
    for (let count = 0; count < 20; count += 1) {
      expect(
        await consumeGlobalWriteBudget(
          env.DB,
          "comment",
          NEXT_HOUR_SECONDS,
        ),
      ).toBe(true);
    }
    const store = new RelayStore(env.DB);
    const lease = await store.acquireIssueLease(
      FINGERPRINT,
      BEFORE_NEXT_HOUR_SECONDS,
    );
    if (lease.status !== "acquired") {
      throw new Error("expected route lease");
    }
    await store.markIssueReady(FINGERPRINT, lease.leaseToken, 42);
    let currentNow = BEFORE_NEXT_HOUR_SECONDS;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(openIssue())
      .mockImplementationOnce(async () => {
        currentNow = NEXT_HOUR_SECONDS;
        return new Response(null, { status: 401 });
      })
      .mockResolvedValueOnce(Response.json({ id: 90 }, { status: 201 }));

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
      25,
      () => currentNow,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(429);
    expect(await response.text()).toBe(RATE_LIMITED_BODY);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not comment after the routed issue enters recovery", async () => {
    const store = new RelayStore(env.DB);
    const lease = await store.acquireIssueLease(FINGERPRINT, NOW_SECONDS);
    if (lease.status !== "acquired") {
      throw new Error("expected route lease");
    }
    await store.markIssueReady(FINGERPRINT, lease.leaseToken, 42);

    const fetchMock = vi.fn(async (_input, init) => {
      if (init?.method === "GET") {
        const recovery = await store.acquireIssueRecoveryLease(
          FINGERPRINT,
          42,
          NOW_SECONDS + 1,
        );
        expect(recovery.status).toBe("acquired");
        return openIssue();
      }
      return Response.json({ id: 90 }, { status: 201 });
    });

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(202);
    expect(await response.text()).toBe(
      JSON.stringify({
        status: "accepted",
        receipt: "123e4567-e89b-42d3-a456-426614174000",
      }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      await env.DB.prepare(
        "SELECT COUNT(*) AS total FROM write_budgets WHERE kind = 'comment'",
      ).first("total"),
    ).toBe(0);
    expect(
      await env.DB.prepare("SELECT state FROM report_actions").first("state"),
    ).toBe("failed");
  });

  it.each([
    ["closed", Response.json({ number: 42, state: "closed", body: fingerprintMarker(FINGERPRINT) })],
    ["missing", new Response(null, { status: 404 })],
  ])("recovers a %s route under a new create lease", async (_case, lookupResponse) => {
    const store = new RelayStore(env.DB);
    const lease = await store.acquireIssueLease(FINGERPRINT, NOW_SECONDS);
    if (lease.status !== "acquired") {
      throw new Error("expected route lease");
    }
    await store.markIssueReady(FINGERPRINT, lease.leaseToken, 42);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(lookupResponse.clone())
      .mockResolvedValueOnce(Response.json({ number: 43 }, { status: 201 }));

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(201);
    expect(await response.text()).toBe(
      JSON.stringify({
        status: "created",
        issue_url: "https://github.com/sanghyun-io/tunnelforge/issues/43",
      }),
    );
    expect(
      await env.DB.prepare(
        "SELECT issue_number, state FROM issue_routes WHERE fingerprint = ?",
      )
        .bind(FINGERPRINT)
        .first(),
    ).toEqual({ issue_number: 43, state: "ready" });
  });

  it("quarantines an ambiguous create and blocks automatic retry", async () => {
    const fetchMock = vi.fn(
      async () => await new Promise<Response>(() => undefined),
    );
    const worker = relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
      5,
    );

    const first = await worker.fetch(relayRequest(), relayBindings());
    expect(first.status).toBe(503);
    expect(await first.text()).toBe(ROUTE_UNKNOWN_BODY);
    expect(
      await env.DB.prepare("SELECT state FROM issue_routes").first("state"),
    ).toBe("unknown");
    expect(
      await env.DB.prepare("SELECT state FROM report_actions").first("state"),
    ).toBe("unknown");

    const retry = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
      5,
    ).fetch(relayRequest("203.0.113.82"), relayBindings());
    expect(retry.status).toBe(503);
    expect(await retry.text()).toBe(ROUTE_UNKNOWN_BODY);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("quarantines a create when D1 fails after GitHub has returned success", async () => {
    const failingDb = new Proxy(env.DB, {
      get(target, property, receiver) {
        if (property === "prepare") {
          return (query: string) => {
            if (/UPDATE issue_routes\s+SET issue_number = \?/m.test(query)) {
              throw new Error("synthetic post-create D1 failure");
            }
            return target.prepare(query);
          };
        }
        const value = Reflect.get(target, property, receiver) as unknown;
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const bindings = { ...relayBindings(), DB: failingDb };
    const fetchMock = vi.fn(async () =>
      Response.json({ number: 42 }, { status: 201 }),
    );

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), bindings);

    expect(response.status).toBe(503);
    expect(await response.text()).toBe(ROUTE_UNKNOWN_BODY);
    expect(
      await env.DB.prepare("SELECT state FROM issue_routes").first("state"),
    ).toBe("unknown");
    expect(
      await env.DB.prepare("SELECT state FROM report_actions").first("state"),
    ).toBe("unknown");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not expose a ready route when create action finalization fails", async () => {
    let injectedFailure = false;
    const failingDb = new Proxy(env.DB, {
      get(target, property, receiver) {
        if (property === "prepare") {
          return (query: string) => {
            if (
              !injectedFailure &&
              /UPDATE report_actions\s+SET state = \?/m.test(query)
            ) {
              injectedFailure = true;
              throw new Error("synthetic action finalization failure");
            }
            return target.prepare(query);
          };
        }
        const value = Reflect.get(target, property, receiver) as unknown;
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const bindings = { ...relayBindings(), DB: failingDb };
    const fetchMock = vi.fn(async () =>
      Response.json({ number: 42 }, { status: 201 }),
    );

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), bindings);

    expect(response.status).toBe(503);
    expect(await response.text()).toBe(ROUTE_UNKNOWN_BODY);
    expect(injectedFailure).toBe(true);
    expect(
      await env.DB.prepare("SELECT state FROM issue_routes").first("state"),
    ).toBe("unknown");
    expect(
      await env.DB.prepare("SELECT state FROM report_actions").first("state"),
    ).toBe("unknown");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("releases a definite pre-send budget denial without calling GitHub", async () => {
    for (let count = 0; count < 5; count += 1) {
      expect(
        await consumeGlobalWriteBudget(env.DB, "create", NOW_SECONDS),
      ).toBe(true);
    }
    const fetchMock = vi.fn(async () => Response.json({ number: 42 }, { status: 201 }));

    const response = await relayWorker(
      makeReport(),
      fetchMock as unknown as typeof fetch,
    ).fetch(relayRequest(), relayBindings());

    expect(response.status).toBe(429);
    expect(await response.text()).toBe(RATE_LIMITED_BODY);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      await env.DB.prepare("SELECT state FROM issue_routes").first("state"),
    ).toBe("failed");
    expect(
      await env.DB.prepare("SELECT state FROM report_actions").first("state"),
    ).toBe("failed");
  });
});
