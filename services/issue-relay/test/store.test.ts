import {
  applyD1Migrations,
  env,
  type D1Migration,
} from "cloudflare:test";
import { beforeAll, beforeEach, describe, expect, it } from "vitest";

import {
  ISSUE_LEASE_SECONDS,
  RELAY_CLEANUP_BATCH_SIZE,
  REPORT_ACTION_TTL_SECONDS,
  RelayStore,
  cleanupExpiredRelayState,
} from "../src/store";
import {
  EDGE_INSTALLATION_MINUTE_LIMIT,
  EDGE_IP_BURST_LIMIT,
  EDGE_IP_MINUTE_LIMIT,
  GLOBAL_WRITE_LIMITS,
  INSTALLATION_HOURLY_LIMIT,
  consumeGlobalWriteBudget,
  consumeGlobalWriteBudgetForCurrentLease,
  consumeInstallationHourlyQuota,
  createEdgeRateLimiter,
  hmacInstallationId,
} from "../src/quotas";

declare global {
  namespace Cloudflare {
    interface Env {
      DB: D1Database;
      TEST_MIGRATIONS: D1Migration[];
    }
  }
}

const HOUR_SECONDS = 60 * 60;
const DAY_SECONDS = 24 * HOUR_SECONDS;
const BASE_DAY = 20_000 * DAY_SECONDS;

async function clearRelayTables(): Promise<void> {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM report_actions"),
    env.DB.prepare("DELETE FROM issue_routes"),
    env.DB.prepare("DELETE FROM write_budgets"),
  ]);
}

async function tableColumns(table: string): Promise<string[]> {
  const result = await env.DB.prepare(`PRAGMA table_info(${table})`).all<{
    name: string;
  }>();
  return result.results.map((column) => column.name);
}

async function rowCount(table: string): Promise<number> {
  const row = await env.DB.prepare(
    `SELECT COUNT(*) AS total FROM ${table}`,
  ).first<{ total: number }>();
  return row?.total ?? 0;
}

beforeAll(async () => {
  await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);
});

beforeEach(async () => {
  await clearRelayTables();
});

describe("D1 schema", () => {
  it("contains only bounded relay metadata columns", async () => {
    expect(await tableColumns("issue_routes")).toEqual([
      "fingerprint",
      "issue_number",
      "state",
      "lease_token",
      "lease_until",
    ]);
    expect(await tableColumns("report_actions")).toEqual([
      "installation_hmac",
      "fingerprint",
      "window",
      "kind",
      "state",
      "expires_at",
      "route_lease_token",
    ]);
    expect(await tableColumns("write_budgets")).toEqual([
      "bucket",
      "kind",
      "used",
      "hard_limit",
      "expires_at",
    ]);

    const allColumns = [
      ...(await tableColumns("issue_routes")),
      ...(await tableColumns("report_actions")),
      ...(await tableColumns("write_budgets")),
    ].join(" ");
    expect(allColumns).not.toMatch(
      /body|message|ip|anonymous(?:_|)installation(?:_|)id/i,
    );
  });
});

describe("installation identity HMAC", () => {
  it("is deterministic, key-separated, and never stores the raw UUID", async () => {
    const installationId = "4d951671-4580-4b5f-9a96-8f92a38d4f77";
    const first = await hmacInstallationId(installationId, "test-key-one");
    const repeated = await hmacInstallationId(installationId, "test-key-one");
    const otherKey = await hmacInstallationId(installationId, "test-key-two");
    const otherId = await hmacInstallationId(
      "751726cb-eac5-420f-a668-f495b425a59f",
      "test-key-one",
    );

    expect(first).toMatch(/^[0-9a-f]{64}$/);
    expect(repeated).toBe(first);
    expect(otherKey).not.toBe(first);
    expect(otherId).not.toBe(first);

    const store = new RelayStore(env.DB);
    const claim = await store.claimReportAction(
      first,
      "a".repeat(64),
      "comment",
      BASE_DAY,
    );
    expect(claim.status).toBe("acquired");
    expect(claim).toHaveProperty("actionWindow", BASE_DAY);

    const rows = await env.DB.prepare("SELECT * FROM report_actions").all();
    expect(JSON.stringify(rows.results)).toContain(first);
    expect(JSON.stringify(rows.results)).not.toContain(installationId);
  });
});

describe("issue route leases", () => {
  it("grants exactly one lease during a concurrent creation race", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "1".repeat(64);

    const results = await Promise.all(
      Array.from({ length: 24 }, () =>
        store.acquireIssueLease(fingerprint, BASE_DAY),
      ),
    );

    expect(results.filter((result) => result.status === "acquired")).toHaveLength(
      1,
    );
    expect(results.filter((result) => result.status === "pending")).toHaveLength(
      23,
    );
    const row = await env.DB.prepare(
      "SELECT state, lease_token, lease_until FROM issue_routes WHERE fingerprint = ?",
    )
      .bind(fingerprint)
      .first<{
        state: string;
        lease_token: string;
        lease_until: number;
      }>();
    expect(row?.state).toBe("pending");
    expect(row?.lease_token).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(row?.lease_until).toBe(BASE_DAY + ISSUE_LEASE_SECONDS);
  });

  it("releases failed work for retry but quarantines unknown outcomes", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "2".repeat(64);
    const first = await store.acquireIssueLease(fingerprint, BASE_DAY);
    expect(first.status).toBe("acquired");
    if (first.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }

    expect(
      await store.markIssueFailed(fingerprint, "stale-token"),
    ).toBe(false);
    expect(await store.markIssueFailed(fingerprint, first.leaseToken)).toBe(true);

    const retry = await store.acquireIssueLease(fingerprint, BASE_DAY + 1);
    expect(retry.status).toBe("acquired");
    if (retry.status !== "acquired") {
      throw new Error("expected a retry lease");
    }
    expect(retry.leaseToken).not.toBe(first.leaseToken);

    expect(await store.markIssueUnknown(fingerprint, retry.leaseToken)).toBe(true);
    expect(
      await store.acquireIssueLease(
        fingerprint,
        BASE_DAY + ISSUE_LEASE_SECONDS + 2,
      ),
    ).toEqual({ status: "unknown" });
    expect(await store.markIssueFailed(fingerprint, retry.leaseToken)).toBe(false);
  });

  it("atomically quarantines an expired pending lease without granting a retry", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "3".repeat(64);
    const original = await store.acquireIssueLease(fingerprint, BASE_DAY);
    expect(original.status).toBe("acquired");

    const results = await Promise.all(
      Array.from({ length: 12 }, () =>
        store.acquireIssueLease(
          fingerprint,
          BASE_DAY + ISSUE_LEASE_SECONDS,
        ),
      ),
    );
    expect(results).toEqual(Array.from({ length: 12 }, () => ({ status: "unknown" })));
    const row = await env.DB.prepare(
      "SELECT state, lease_token, lease_until FROM issue_routes WHERE fingerprint = ?",
    )
      .bind(fingerprint)
      .first<{
        state: string;
        lease_token: string | null;
        lease_until: number | null;
      }>();
    expect(row).toEqual({
      state: "unknown",
      lease_token: null,
      lease_until: null,
    });
  });

  it("validates only the current unexpired lease immediately before a send", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "d".repeat(64);
    const lease = await store.acquireIssueLease(fingerprint, BASE_DAY);
    expect(lease.status).toBe("acquired");
    if (lease.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }

    expect(
      await store.isIssueLeaseCurrent(
        fingerprint,
        lease.leaseToken,
        BASE_DAY + ISSUE_LEASE_SECONDS - 1,
      ),
    ).toBe(true);
    expect(
      await store.isIssueLeaseCurrent(
        fingerprint,
        "00000000-0000-4000-8000-000000000000",
        BASE_DAY,
      ),
    ).toBe(false);
    expect(
      await store.isIssueLeaseCurrent(
        fingerprint,
        lease.leaseToken,
        BASE_DAY + ISSUE_LEASE_SECONDS,
      ),
    ).toBe(false);

    expect(
      await store.acquireIssueLease(
        fingerprint,
        BASE_DAY + ISSUE_LEASE_SECONDS,
      ),
    ).toEqual({ status: "unknown" });
    expect(
      await store.isIssueLeaseCurrent(
        fingerprint,
        lease.leaseToken,
        BASE_DAY + ISSUE_LEASE_SECONDS,
      ),
    ).toBe(false);
  });

  it("publishes an issue number only for the current pending lease", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "4".repeat(64);
    const lease = await store.acquireIssueLease(fingerprint, BASE_DAY);
    expect(lease.status).toBe("acquired");
    if (lease.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }

    expect(await store.markIssueReady(fingerprint, "stale-token", 91)).toBe(
      false,
    );
    expect(await store.markIssueReady(fingerprint, lease.leaseToken, 91)).toBe(
      true,
    );
    expect(await store.acquireIssueLease(fingerprint, BASE_DAY + 1)).toEqual({
      status: "ready",
      issueNumber: 91,
    });
    expect(await store.markIssueUnknown(fingerprint, lease.leaseToken)).toBe(
      false,
    );
  });

  it("grants one new lease to recover an exactly matched closed or missing route", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "e".repeat(64);
    const original = await store.acquireIssueLease(fingerprint, BASE_DAY);
    if (original.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }
    await store.markIssueReady(fingerprint, original.leaseToken, 91);

    const results = await Promise.all(
      Array.from({ length: 12 }, () =>
        store.acquireIssueRecoveryLease(
          fingerprint,
          91,
          BASE_DAY + 1,
        ),
      ),
    );

    expect(results.filter((result) => result.status === "acquired")).toHaveLength(
      1,
    );
    expect(results.filter((result) => result.status === "pending")).toHaveLength(
      11,
    );
    expect(
      await store.acquireIssueRecoveryLease(
        fingerprint,
        90,
        BASE_DAY + 2,
      ),
    ).toEqual({ status: "pending" });
  });

  it("does not recover a changed route and can quarantine a marker mismatch", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "f".repeat(64);
    const original = await store.acquireIssueLease(fingerprint, BASE_DAY);
    if (original.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }
    await store.markIssueReady(fingerprint, original.leaseToken, 91);

    expect(
      await store.acquireIssueRecoveryLease(
        fingerprint,
        90,
        BASE_DAY + 1,
      ),
    ).toEqual({ status: "ready", issueNumber: 91 });
    expect(await store.markReadyIssueUnknown(fingerprint, 90)).toBe(false);
    expect(await store.markReadyIssueUnknown(fingerprint, 91)).toBe(true);
    expect(await store.acquireIssueLease(fingerprint, BASE_DAY + 2)).toEqual({
      status: "unknown",
    });
  });
});

describe("24-hour report actions", () => {
  it("counts only completed bounded recurrence evidence", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "9".repeat(64);
    const createLease = await store.acquireIssueLease(fingerprint, BASE_DAY);
    if (createLease.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }
    const created = await store.claimReportAction(
      "1".repeat(64),
      fingerprint,
      "create",
      BASE_DAY,
      createLease.leaseToken,
    );
    if (created.status !== "acquired") {
      throw new Error("expected create action");
    }
    await store.markReportAction(
      "1".repeat(64),
      fingerprint,
      "create",
      created.actionWindow,
      "complete",
    );
    await store.markIssueReady(fingerprint, createLease.leaseToken, 91);
    const comment = await store.claimReportAction(
      "2".repeat(64),
      fingerprint,
      "comment",
      BASE_DAY + 1,
    );
    if (comment.status !== "acquired") {
      throw new Error("expected comment action");
    }

    expect(
      await store.countCompletedReportActions(fingerprint, BASE_DAY + 1),
    ).toBe(1);
    await store.markReportAction(
      "2".repeat(64),
      fingerprint,
      "comment",
      comment.actionWindow,
      "complete",
    );
    expect(
      await store.countCompletedReportActions(fingerprint, BASE_DAY + 1),
    ).toBe(2);
  });

  it("excludes completed actions whose relay window has expired", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "a".repeat(64);
    const completed = await store.claimReportAction(
      "b".repeat(64),
      fingerprint,
      "comment",
      BASE_DAY,
    );
    if (completed.status !== "acquired") {
      throw new Error("expected comment action");
    }
    await store.markReportAction(
      "b".repeat(64),
      fingerprint,
      "comment",
      completed.actionWindow,
      "complete",
    );

    expect(
      await store.countCompletedReportActions(
        fingerprint,
        BASE_DAY + REPORT_ACTION_TTL_SECONDS,
      ),
    ).toBe(0);
  });

  it("rejects an invalid recurrence-count timestamp", async () => {
    const store = new RelayStore(env.DB);

    await expect(
      store.countCompletedReportActions("c".repeat(64), -1),
    ).rejects.toThrow("invalid relay timestamp");
  });

  it("deduplicates the same installation and fingerprint for a full 24 hours", async () => {
    const store = new RelayStore(env.DB);
    const installation = "5".repeat(64);
    const fingerprint = "6".repeat(64);

    expect(
      await store.claimReportAction(
        installation,
        fingerprint,
        "comment",
        BASE_DAY,
      ),
    ).toEqual({ status: "acquired", actionWindow: BASE_DAY });
    expect(
      await store.claimReportAction(
        installation,
        fingerprint,
        "comment",
        BASE_DAY + REPORT_ACTION_TTL_SECONDS - 1,
      ),
    ).toEqual({ status: "pending" });
    expect(
      await store.claimReportAction(
        "7".repeat(64),
        fingerprint,
        "comment",
        BASE_DAY + 1,
      ),
    ).toEqual({ status: "acquired", actionWindow: BASE_DAY + 1 });
    expect(
      await store.claimReportAction(
        installation,
        "8".repeat(64),
        "comment",
        BASE_DAY + 1,
      ),
    ).toEqual({ status: "acquired", actionWindow: BASE_DAY + 1 });
    expect(
      await store.claimReportAction(
        installation,
        fingerprint,
        "comment",
        BASE_DAY + REPORT_ACTION_TTL_SECONDS,
      ),
    ).toEqual({
      status: "acquired",
      actionWindow: BASE_DAY + REPORT_ACTION_TTL_SECONDS,
    });
  });

  it("retries failed actions, rejects stale transitions, and retains unknown outcomes", async () => {
    const store = new RelayStore(env.DB);
    const installation = "9".repeat(64);
    const fingerprint = "a".repeat(64);

    const initial = await store.claimReportAction(
      installation,
      fingerprint,
      "comment",
      BASE_DAY,
    );
    expect(initial).toEqual({ status: "acquired", actionWindow: BASE_DAY });
    if (initial.status !== "acquired") {
      throw new Error("expected an acquired action");
    }
    expect(
      await store.markReportAction(
        installation,
        fingerprint,
        "comment",
        initial.actionWindow,
        "failed",
      ),
    ).toBe(true);
    const retry = await store.claimReportAction(
      installation,
      fingerprint,
      "comment",
      BASE_DAY,
    );
    expect(retry).toEqual({
      status: "acquired",
      actionWindow: BASE_DAY + 1,
    });
    if (retry.status !== "acquired") {
      throw new Error("expected a retry action");
    }
    expect(
      await store.markReportAction(
        installation,
        fingerprint,
        "comment",
        initial.actionWindow,
        "complete",
      ),
    ).toBe(false);
    expect(
      await store.markReportAction(
        installation,
        fingerprint,
        "comment",
        retry.actionWindow,
        "unknown",
      ),
    ).toBe(true);
    expect(
      await store.claimReportAction(
        installation,
        fingerprint,
        "comment",
        BASE_DAY + REPORT_ACTION_TTL_SECONDS - 1,
      ),
    ).toEqual({ status: "unknown" });
    expect(
      await store.markReportAction(
        installation,
        fingerprint,
        "comment",
        retry.actionWindow,
        "complete",
      ),
    ).toBe(false);
    expect(
      await store.claimReportAction(
        installation,
        fingerprint,
        "comment",
        BASE_DAY + REPORT_ACTION_TTL_SECONDS + 1,
      ),
    ).toEqual({
      status: "acquired",
      actionWindow: BASE_DAY + REPORT_ACTION_TTL_SECONDS + 1,
    });
  });

  it("deduplicates installation and fingerprint across create and comment kinds", async () => {
    const store = new RelayStore(env.DB);
    const installation = "3".repeat(64);
    const fingerprint = "4".repeat(64);
    const lease = await store.acquireIssueLease(fingerprint, BASE_DAY);
    if (lease.status !== "acquired") {
      throw new Error("expected a create lease");
    }

    const create = await store.claimReportAction(
      installation,
      fingerprint,
      "create",
      BASE_DAY,
      lease.leaseToken,
    );
    expect(create).toEqual({ status: "acquired", actionWindow: BASE_DAY });
    if (create.status !== "acquired") {
      throw new Error("expected an acquired action");
    }
    expect(
      await store.markReportAction(
        installation,
        fingerprint,
        "create",
        create.actionWindow,
        "complete",
      ),
    ).toBe(true);
    expect(
      await store.claimReportAction(
        installation,
        fingerprint,
        "comment",
        BASE_DAY + 1,
      ),
    ).toEqual({ status: "complete" });
  });

  it("allows only one concurrent action claim", async () => {
    const store = new RelayStore(env.DB);
    const results = await Promise.all(
      Array.from({ length: 20 }, () =>
        store.claimReportAction(
          "b".repeat(64),
          "c".repeat(64),
          "comment",
          BASE_DAY,
        ),
      ),
    );

    expect(results.filter((result) => result.status === "acquired")).toHaveLength(
      1,
    );
    expect(results.filter((result) => result.status === "pending")).toHaveLength(
      19,
    );
  });

  it("allows only one pending create authorization per route generation", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "d".repeat(64);
    const lease = await store.acquireIssueLease(fingerprint, BASE_DAY);
    if (lease.status !== "acquired") {
      throw new Error("expected a create lease");
    }
    const results = await Promise.all(
      Array.from({ length: 20 }, (_, index) =>
        store.claimReportAction(
          index.toString(16).padStart(64, "0"),
          fingerprint,
          "create",
          BASE_DAY,
          lease.leaseToken,
        ),
      ),
    );

    expect(results.filter((result) => result.status === "acquired")).toHaveLength(
      1,
    );
    expect(results.filter((result) => result.status === "pending")).toHaveLength(
      19,
    );
  });

  it.each(["complete", "unknown"] as const)(
    "blocks a second installation when the route generation is already %s",
    async (state) => {
      const store = new RelayStore(env.DB);
      const fingerprint = state === "complete" ? "6".repeat(64) : "7".repeat(64);
      const lease = await store.acquireIssueLease(fingerprint, BASE_DAY);
      if (lease.status !== "acquired") {
        throw new Error("expected a create lease");
      }
      const first = await store.claimReportAction(
        "8".repeat(64),
        fingerprint,
        "create",
        BASE_DAY,
        lease.leaseToken,
      );
      if (first.status !== "acquired") {
        throw new Error("expected an acquired create action");
      }
      expect(
        await store.markReportAction(
          "8".repeat(64),
          fingerprint,
          "create",
          first.actionWindow,
          state,
        ),
      ).toBe(true);

      expect(
        await store.claimReportAction(
          "9".repeat(64),
          fingerprint,
          "create",
          BASE_DAY + 1,
          lease.leaseToken,
        ),
      ).toEqual({ status: state });
      expect(await rowCount("report_actions")).toBe(1);
    },
  );

  it("releases a route only after its create authorization definitely failed", async () => {
    const store = new RelayStore(env.DB);
    const installation = "e".repeat(64);
    const fingerprint = "f".repeat(64);
    const firstLease = await store.acquireIssueLease(fingerprint, BASE_DAY);
    expect(firstLease.status).toBe("acquired");
    if (firstLease.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }
    const action = await store.claimReportAction(
      installation,
      fingerprint,
      "create",
      BASE_DAY,
      firstLease.leaseToken,
    );
    expect(action).toEqual({ status: "acquired", actionWindow: BASE_DAY });
    if (action.status !== "acquired") {
      throw new Error("expected an acquired action");
    }

    expect(
      await store.markIssueFailed(fingerprint, firstLease.leaseToken),
    ).toBe(false);
    expect(
      await store.acquireIssueLease(fingerprint, BASE_DAY + 1),
    ).toEqual({ status: "pending" });
    expect(
      await store.markReportAction(
        installation,
        fingerprint,
        "create",
        action.actionWindow,
        "failed",
      ),
    ).toBe(true);
    expect(
      await store.markIssueFailed(fingerprint, firstLease.leaseToken),
    ).toBe(true);
    const retryLease = await store.acquireIssueLease(fingerprint, BASE_DAY + 1);
    expect(retryLease.status).toBe("acquired");
    if (retryLease.status !== "acquired") {
      throw new Error("expected a retry lease");
    }
    expect(retryLease.leaseToken).not.toBe(firstLease.leaseToken);
    const retryAction = await store.claimReportAction(
      "a".repeat(64),
      fingerprint,
      "create",
      BASE_DAY + 1,
      retryLease.leaseToken,
    );
    expect(retryAction).toEqual({
      status: "acquired",
      actionWindow: BASE_DAY + 1,
    });
    if (retryAction.status !== "acquired") {
      throw new Error("expected a retry action");
    }
    expect(
      await store.markReportAction(
        "a".repeat(64),
        fingerprint,
        "create",
        retryAction.actionWindow,
        "unknown",
      ),
    ).toBe(true);
    expect(
      await store.markIssueFailed(fingerprint, retryLease.leaseToken),
    ).toBe(false);
    expect(await rowCount("report_actions")).toBe(2);
  });

  it("rejects create claims with stale or expired route tokens", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "0".repeat(64);
    const lease = await store.acquireIssueLease(fingerprint, BASE_DAY);
    if (lease.status !== "acquired") {
      throw new Error("expected a create lease");
    }

    expect(
      await store.claimReportAction(
        "1".repeat(64),
        fingerprint,
        "create",
        BASE_DAY,
        "00000000-0000-4000-8000-000000000000",
      ),
    ).toEqual({ status: "unknown" });
    expect(
      await store.claimReportAction(
        "1".repeat(64),
        fingerprint,
        "create",
        BASE_DAY + ISSUE_LEASE_SECONDS,
        lease.leaseToken,
      ),
    ).toEqual({ status: "unknown" });
    expect(await rowCount("report_actions")).toBe(0);
  });
});

describe("per-edge controls", () => {
  it("limits an IP to three requests per ten seconds", () => {
    const limiter = createEdgeRateLimiter();
    for (let index = 0; index < EDGE_IP_BURST_LIMIT; index += 1) {
      expect(limiter.checkIp("203.0.113.7", 100).allowed).toBe(true);
    }
    expect(limiter.checkIp("203.0.113.7", 100)).toEqual({
      allowed: false,
      reason: "ip_burst",
    });
    expect(limiter.checkIp("203.0.113.7", 110).allowed).toBe(true);
  });

  it("limits an IP to ten requests per minute", () => {
    const limiter = createEdgeRateLimiter();
    const timestamps = [
      ...Array.from({ length: 3 }, () => 100),
      ...Array.from({ length: 3 }, () => 110),
      ...Array.from({ length: 3 }, () => 120),
      130,
    ];
    expect(timestamps).toHaveLength(EDGE_IP_MINUTE_LIMIT);
    for (const timestamp of timestamps) {
      expect(limiter.checkIp("203.0.113.8", timestamp).allowed).toBe(true);
    }
    expect(limiter.checkIp("203.0.113.8", 130)).toEqual({
      allowed: false,
      reason: "ip_minute",
    });
    expect(limiter.checkIp("203.0.113.8", 160).allowed).toBe(true);
  });

  it("limits one installation HMAC across changing IP addresses", () => {
    const limiter = createEdgeRateLimiter();
    const installation = "d".repeat(64);
    for (let index = 0; index < EDGE_INSTALLATION_MINUTE_LIMIT; index += 1) {
      expect(limiter.checkInstallation(installation, 100).allowed).toBe(true);
    }
    expect(limiter.checkInstallation(installation, 100)).toEqual({
      allowed: false,
      reason: "installation_minute",
    });
    expect(limiter.checkInstallation(installation, 160).allowed).toBe(true);
  });

  it("keeps edge counters local to each simulated location", () => {
    const locationOne = createEdgeRateLimiter();
    const locationTwo = createEdgeRateLimiter();
    for (let index = 0; index < EDGE_IP_BURST_LIMIT; index += 1) {
      expect(locationOne.checkIp("198.51.100.9", 100).allowed).toBe(true);
    }
    expect(locationOne.checkIp("198.51.100.9", 100).allowed).toBe(false);
    expect(locationTwo.checkIp("198.51.100.9", 100).allowed).toBe(true);
  });

  it("bounds unique IP and installation keys until their windows expire", () => {
    const limiter = createEdgeRateLimiter({ maxKeys: 2 });

    expect(limiter.checkIp("192.0.2.1", 100).allowed).toBe(true);
    expect(limiter.checkIp("192.0.2.2", 100).allowed).toBe(true);
    expect(limiter.checkIp("192.0.2.3", 100)).toEqual({
      allowed: false,
      reason: "edge_capacity",
    });

    expect(limiter.checkInstallation("1".repeat(64), 100).allowed).toBe(true);
    expect(limiter.checkInstallation("2".repeat(64), 100).allowed).toBe(true);
    expect(limiter.checkInstallation("3".repeat(64), 100)).toEqual({
      allowed: false,
      reason: "edge_capacity",
    });

    expect(limiter.checkIp("192.0.2.3", 160).allowed).toBe(true);
    expect(limiter.checkInstallation("3".repeat(64), 160).allowed).toBe(
      true,
    );
  });

  it("does not rescan a saturated map until its next possible expiry", () => {
    const sweeps: Array<"ip" | "installation"> = [];
    const limiter = createEdgeRateLimiter({
      maxKeys: 2,
      onCapacitySweep: (kind) => sweeps.push(kind),
    });

    expect(limiter.checkIp("1".repeat(64), 100).allowed).toBe(true);
    expect(limiter.checkIp("2".repeat(64), 100).allowed).toBe(true);
    for (let index = 0; index < 50; index += 1) {
      expect(
        limiter.checkIp(index.toString(16).padStart(64, "a"), 101),
      ).toEqual({ allowed: false, reason: "edge_capacity" });
    }
    expect(sweeps).toEqual([]);

    expect(limiter.checkIp("3".repeat(64), 160).allowed).toBe(true);
    expect(sweeps).toEqual(["ip"]);
  });
});

describe("D1 quotas", () => {
  it("has no separate unbounded quota cleanup export", async () => {
    expect(await import("../src/quotas")).not.toHaveProperty(
      "cleanupExpiredQuotaState",
    );
  });

  it("atomically limits each installation HMAC to ten reports per hour", async () => {
    const installation = "e".repeat(64);
    const results = await Promise.all(
      Array.from({ length: INSTALLATION_HOURLY_LIMIT * 2 }, () =>
        consumeInstallationHourlyQuota(env.DB, installation, BASE_DAY),
      ),
    );

    expect(results.filter(Boolean)).toHaveLength(INSTALLATION_HOURLY_LIMIT);
    expect(
      await consumeInstallationHourlyQuota(
        env.DB,
        "f".repeat(64),
        BASE_DAY,
      ),
    ).toBe(true);
    expect(
      await consumeInstallationHourlyQuota(
        env.DB,
        installation,
        BASE_DAY + HOUR_SECONDS,
      ),
    ).toBe(true);
  });

  it("atomically caps creates at five per hour", async () => {
    const attempts = await Promise.all(
      Array.from({ length: 30 }, () =>
        consumeGlobalWriteBudget(env.DB, "create", BASE_DAY),
      ),
    );
    expect(attempts.filter(Boolean)).toHaveLength(
      GLOBAL_WRITE_LIMITS.create.hour,
    );
  });

  it("atomically refuses a create budget when its route lease is no longer current", async () => {
    const store = new RelayStore(env.DB);
    const fingerprint = "c".repeat(64);
    const lease = await store.acquireIssueLease(fingerprint, BASE_DAY);
    if (lease.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }
    expect(
      await store.isIssueLeaseCurrent(
        fingerprint,
        lease.leaseToken,
        BASE_DAY + ISSUE_LEASE_SECONDS - 1,
      ),
    ).toBe(true);

    expect(
      await consumeGlobalWriteBudgetForCurrentLease(
        env.DB,
        "create",
        fingerprint,
        lease.leaseToken,
        BASE_DAY + ISSUE_LEASE_SECONDS,
      ),
    ).toBe(false);
    expect(await rowCount("write_budgets")).toBe(0);

    const currentFingerprint = "d".repeat(64);
    const current = await store.acquireIssueLease(
      currentFingerprint,
      BASE_DAY,
    );
    if (current.status !== "acquired") {
      throw new Error("expected a current lease");
    }
    expect(
      await consumeGlobalWriteBudgetForCurrentLease(
        env.DB,
        "create",
        currentFingerprint,
        current.leaseToken,
        BASE_DAY + 1,
      ),
    ).toBe(true);
    expect(await rowCount("write_budgets")).toBe(2);
  });

  it("caps creates at twenty per day without consuming a fresh hour on denial", async () => {
    for (let hour = 0; hour < 4; hour += 1) {
      for (
        let count = 0;
        count < GLOBAL_WRITE_LIMITS.create.hour;
        count += 1
      ) {
        expect(
          await consumeGlobalWriteBudget(
            env.DB,
            "create",
            BASE_DAY + hour * HOUR_SECONDS,
          ),
        ).toBe(true);
      }
    }
    expect(GLOBAL_WRITE_LIMITS.create.day).toBe(20);
    const rowsBefore = await rowCount("write_budgets");
    expect(
      await consumeGlobalWriteBudget(
        env.DB,
        "create",
        BASE_DAY + 5 * HOUR_SECONDS,
      ),
    ).toBe(false);
    expect(await rowCount("write_budgets")).toBe(rowsBefore);
  });

  it("caps comments at twenty per hour and one hundred per day", async () => {
    for (let hour = 0; hour < 5; hour += 1) {
      const attempts = await Promise.all(
        Array.from({ length: 25 }, () =>
          consumeGlobalWriteBudget(
            env.DB,
            "comment",
            BASE_DAY + hour * HOUR_SECONDS,
          ),
        ),
      );
      expect(attempts.filter(Boolean)).toHaveLength(
        GLOBAL_WRITE_LIMITS.comment.hour,
      );
    }
    expect(GLOBAL_WRITE_LIMITS.comment.day).toBe(100);
    expect(
      await consumeGlobalWriteBudget(
        env.DB,
        "comment",
        BASE_DAY + 6 * HOUR_SECONDS,
      ),
    ).toBe(false);
  });

  it("keeps the global cap across rotated IDs and simulated edge locations", async () => {
    const edgeLocations = Array.from({ length: 4 }, () =>
      createEdgeRateLimiter(),
    );
    const results: boolean[] = [];

    for (let index = 0; index < 16; index += 1) {
      const installationId = `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
      const installationHmac = await hmacInstallationId(
        installationId,
        "rotation-test-key",
      );
      const location = edgeLocations[index % edgeLocations.length];
      expect(
        location.checkIp(`192.0.2.${index + 1}`, BASE_DAY).allowed,
      ).toBe(true);
      expect(
        location.checkInstallation(installationHmac, BASE_DAY).allowed,
      ).toBe(true);
      results.push(
        await consumeGlobalWriteBudget(env.DB, "create", BASE_DAY),
      );
    }

    expect(results.filter(Boolean)).toHaveLength(
      GLOBAL_WRITE_LIMITS.create.hour,
    );
  });

  it("atomically cleans expiry state while preserving ready and unknown routes", async () => {
    const store = new RelayStore(env.DB);
    const installation = "1".repeat(64);
    const fingerprint = "2".repeat(64);
    await store.claimReportAction(
      installation,
      fingerprint,
      "comment",
      BASE_DAY,
    );
    await consumeInstallationHourlyQuota(
      env.DB,
      installation,
      BASE_DAY,
    );
    await consumeGlobalWriteBudget(env.DB, "create", BASE_DAY);
    const expiredPending = await store.acquireIssueLease(fingerprint, BASE_DAY);
    if (expiredPending.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }
    const failedFingerprint = "3".repeat(64);
    const failed = await store.acquireIssueLease(failedFingerprint, BASE_DAY);
    if (failed.status !== "acquired") {
      throw new Error("expected a failed-route lease");
    }
    await store.markIssueFailed(failedFingerprint, failed.leaseToken);
    const readyFingerprint = "4".repeat(64);
    const ready = await store.acquireIssueLease(readyFingerprint, BASE_DAY);
    if (ready.status !== "acquired") {
      throw new Error("expected a ready-route lease");
    }
    await store.markIssueReady(readyFingerprint, ready.leaseToken, 91);
    const unknownFingerprint = "5".repeat(64);
    const unknown = await store.acquireIssueLease(unknownFingerprint, BASE_DAY);
    if (unknown.status !== "acquired") {
      throw new Error("expected an unknown-route lease");
    }
    await store.markIssueUnknown(unknownFingerprint, unknown.leaseToken);

    expect(
      await cleanupExpiredRelayState(
        env.DB,
        BASE_DAY + HOUR_SECONDS - 1,
      ),
    ).toEqual({
      quarantinedRoutes: 1,
      deletedSafeRoutes: 1,
      reportActions: 0,
      writeBudgets: 0,
    });
    expect(
      await cleanupExpiredRelayState(
        env.DB,
        BASE_DAY + REPORT_ACTION_TTL_SECONDS,
      ),
    ).toEqual({
      quarantinedRoutes: 0,
      deletedSafeRoutes: 0,
      reportActions: 1,
      writeBudgets: 3,
    });
    expect(await rowCount("report_actions")).toBe(0);
    expect(await rowCount("write_budgets")).toBe(0);
    expect(
      await env.DB.prepare(
        "SELECT fingerprint, state FROM issue_routes ORDER BY fingerprint",
      ).all(),
    ).toMatchObject({
      results: [
        { fingerprint, state: "unknown" },
        { fingerprint: readyFingerprint, state: "ready" },
        { fingerprint: unknownFingerprint, state: "unknown" },
      ],
    });
  });

  it("bounds every cleanup mutation and drains a larger backlog", async () => {
    const backlogSize = RELAY_CLEANUP_BATCH_SIZE + 5;
    await env.DB.batch([
      env.DB.prepare(
        `WITH RECURSIVE sequence(n) AS (
           SELECT 1
           UNION ALL SELECT n + 1 FROM sequence WHERE n < ?
         )
         INSERT INTO issue_routes (
           fingerprint, issue_number, state, lease_token, lease_until
         )
         SELECT printf('%064x', n), NULL, 'pending', printf('lease-%d', n), ?
         FROM sequence`,
      ).bind(backlogSize, BASE_DAY),
      env.DB.prepare(
        `WITH RECURSIVE sequence(n) AS (
           SELECT 1
           UNION ALL SELECT n + 1 FROM sequence WHERE n < ?
         )
         INSERT INTO issue_routes (
           fingerprint, issue_number, state, lease_token, lease_until
         )
         SELECT printf('%064x', n + 1000), NULL, 'failed', NULL, NULL
         FROM sequence`,
      ).bind(backlogSize),
      env.DB.prepare(
        `WITH RECURSIVE sequence(n) AS (
           SELECT 1
           UNION ALL SELECT n + 1 FROM sequence WHERE n < ?
         )
         INSERT INTO report_actions (
           installation_hmac, fingerprint, window, kind, state, expires_at,
           route_lease_token
         )
         SELECT printf('%064x', n + 2000), printf('%064x', n + 3000),
                ? + n, 'comment', 'pending', ?, NULL
         FROM sequence`,
      ).bind(backlogSize, BASE_DAY, BASE_DAY),
      env.DB.prepare(
        `WITH RECURSIVE sequence(n) AS (
           SELECT 1
           UNION ALL SELECT n + 1 FROM sequence WHERE n < ?
         )
         INSERT INTO write_budgets (
           bucket, kind, used, hard_limit, expires_at
         )
         SELECT printf('cleanup:%d', n), 'installation', 1, 10, ?
         FROM sequence`,
      ).bind(backlogSize, BASE_DAY),
      env.DB.prepare(
        `INSERT INTO issue_routes
         VALUES (?, 77, 'ready', NULL, NULL)`,
      ).bind("a".repeat(64)),
      env.DB.prepare(
        `INSERT INTO issue_routes
         VALUES (?, NULL, 'unknown', NULL, NULL)`,
      ).bind("b".repeat(64)),
    ]);

    expect(
      await cleanupExpiredRelayState(env.DB, BASE_DAY + 1),
    ).toEqual({
      quarantinedRoutes: RELAY_CLEANUP_BATCH_SIZE,
      deletedSafeRoutes: RELAY_CLEANUP_BATCH_SIZE,
      reportActions: RELAY_CLEANUP_BATCH_SIZE,
      writeBudgets: RELAY_CLEANUP_BATCH_SIZE,
    });
    expect(
      await env.DB.prepare(
        "SELECT COUNT(*) AS total FROM issue_routes WHERE state = 'pending'",
      ).first("total"),
    ).toBe(5);
    expect(
      await env.DB.prepare(
        "SELECT COUNT(*) AS total FROM issue_routes WHERE state = 'failed'",
      ).first("total"),
    ).toBe(5);
    expect(await rowCount("report_actions")).toBe(5);
    expect(await rowCount("write_budgets")).toBe(5);

    expect(
      await cleanupExpiredRelayState(env.DB, BASE_DAY + 1),
    ).toEqual({
      quarantinedRoutes: 5,
      deletedSafeRoutes: 5,
      reportActions: 5,
      writeBudgets: 5,
    });
    expect(
      await cleanupExpiredRelayState(env.DB, BASE_DAY + 1),
    ).toEqual({
      quarantinedRoutes: 0,
      deletedSafeRoutes: 0,
      reportActions: 0,
      writeBudgets: 0,
    });
    expect(
      await env.DB.prepare(
        "SELECT state FROM issue_routes WHERE fingerprint = ?",
      )
        .bind("a".repeat(64))
        .first("state"),
    ).toBe("ready");
    expect(
      await env.DB.prepare(
        "SELECT state FROM issue_routes WHERE fingerprint = ?",
      )
        .bind("b".repeat(64))
        .first("state"),
    ).toBe("unknown");
  });

  it("retains a create guard until its mismatched pending route batch is quarantined", async () => {
    const store = new RelayStore(env.DB);
    const targetNumber = RELAY_CLEANUP_BATCH_SIZE + 1;
    const targetFingerprint = targetNumber.toString(16).padStart(64, "0");
    const targetLeaseToken = `guard-${String(targetNumber).padStart(3, "0")}`;
    await env.DB.batch([
      env.DB.prepare(
        `WITH RECURSIVE sequence(n) AS (
           SELECT 1
           UNION ALL SELECT n + 1 FROM sequence WHERE n < ?
         )
         INSERT INTO issue_routes (
           fingerprint, issue_number, state, lease_token, lease_until
         )
         SELECT printf('%064x', n), NULL, 'pending',
                printf('guard-%03d', n),
                CASE WHEN n = ? THEN ? + 1 ELSE ? END
         FROM sequence`,
      ).bind(targetNumber, targetNumber, BASE_DAY, BASE_DAY),
      env.DB.prepare(
        `WITH RECURSIVE sequence(n) AS (
           SELECT 1
           UNION ALL SELECT n + 1 FROM sequence WHERE n < ?
         )
         INSERT INTO report_actions (
           installation_hmac, fingerprint, window, kind, state, expires_at,
           route_lease_token
         )
         SELECT printf('%064x', n + 1000), printf('%064x', n),
                ? + n, 'create', 'complete',
                CASE WHEN n = ? THEN 1 ELSE n + 1 END,
                printf('guard-%03d', n)
         FROM sequence`,
      ).bind(targetNumber, BASE_DAY, targetNumber),
    ]);

    expect(
      await cleanupExpiredRelayState(env.DB, BASE_DAY + 1),
    ).toEqual({
      quarantinedRoutes: RELAY_CLEANUP_BATCH_SIZE,
      deletedSafeRoutes: 0,
      reportActions: RELAY_CLEANUP_BATCH_SIZE,
      writeBudgets: 0,
    });
    expect(
      await store.markIssueFailed(
        targetFingerprint,
        targetLeaseToken,
      ),
    ).toBe(false);
    expect(
      await env.DB.prepare(
        `SELECT COUNT(*) AS total
         FROM report_actions
         WHERE route_lease_token = ?`,
      )
        .bind(targetLeaseToken)
        .first("total"),
    ).toBe(1);
    expect(
      await env.DB.prepare(
        "SELECT state FROM issue_routes WHERE fingerprint = ?",
      )
        .bind(targetFingerprint)
        .first("state"),
    ).toBe("pending");

    expect(
      await cleanupExpiredRelayState(env.DB, BASE_DAY + 1),
    ).toEqual({
      quarantinedRoutes: 1,
      deletedSafeRoutes: 0,
      reportActions: 1,
      writeBudgets: 0,
    });
    expect(
      await env.DB.prepare(
        "SELECT state FROM issue_routes WHERE fingerprint = ?",
      )
        .bind(targetFingerprint)
        .first("state"),
    ).toBe("unknown");
    expect(await rowCount("report_actions")).toBe(0);
  });
});
