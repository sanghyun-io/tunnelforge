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
  createRelayWorker,
  type RelayEnv,
  type RelayWorkerOptions,
} from "../src/index";
import {
  parseRelayMode,
  verifyCanaryAuthorization,
  type RelayMode,
} from "../src/modes";
import { createEdgeRateLimiter } from "../src/quotas";
import {
  consumeGlobalWriteBudget,
  consumeInstallationHourlyQuota,
} from "../src/quotas";
import { RELAY_CLEANUP_BATCH_SIZE, RelayStore } from "../src/store";
import type {
  ErrorReport,
  ParseReportResult,
} from "../src/types";

declare global {
  namespace Cloudflare {
    interface Env {
      DB: D1Database;
      TEST_MIGRATIONS: D1Migration[];
    }
  }
}

const FIXED_RECEIPT = "123e4567-e89b-42d3-a456-426614174000";
const ACCEPTED_BODY = JSON.stringify({
  status: "accepted",
  receipt: FIXED_RECEIPT,
});
const UNAVAILABLE_BODY = JSON.stringify({
  error: { code: "service_unavailable", retryable: true },
});
const UNAUTHORIZED_BODY = JSON.stringify({
  error: { code: "unauthorized", retryable: false },
});
const RATE_LIMITED_BODY = JSON.stringify({
  error: { code: "rate_limited", retryable: true },
});
const NOT_FOUND_BODY = JSON.stringify({
  error: { code: "not_found", retryable: false },
});
const METHOD_NOT_ALLOWED_BODY = JSON.stringify({
  error: { code: "method_not_allowed", retryable: false },
});
const HTTPS_REQUIRED_BODY = JSON.stringify({
  error: { code: "https_required", retryable: false },
});
const ROUTE_UNKNOWN_BODY = JSON.stringify({
  error: { code: "route_unknown", retryable: true },
});
const INTERNAL_ERROR_BODY = JSON.stringify({
  error: { code: "internal_error", retryable: true },
});
const NOW_SECONDS = 20_000 * 24 * 60 * 60;

function makeReport(
  installationId = "4d951671-4580-4b5f-9a96-8f92a38d4f77",
  fingerprint = "a".repeat(64),
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
      utc_offset_minutes: 0,
    },
    runtime: {
      python_version: "3.13.5",
      qt_version: "6.9.1",
      rust_core_version: "2.3.1",
    },
    operation: {
      kind: "export",
      db_engine: "postgresql",
      phase: "dump.run",
    },
    error: {
      exception_class: "RuntimeError",
      sanitized_message: "",
      app_frames: [],
    },
  };
}

function successfulParser(
  report = makeReport(),
): RelayWorkerOptions["parseReport"] {
  return vi.fn(async (): Promise<ParseReportResult> => ({ ok: true, report }));
}

function fixedOptions(
  mode: RelayMode,
  report = makeReport(),
): RelayWorkerOptions {
  return {
    mode,
    parseReport: successfulParser(report),
    receipt: () => FIXED_RECEIPT,
    now: () => NOW_SECONDS,
    edgeLimiter: createEdgeRateLimiter(),
  };
}

function request(
  body = "TOP-SECRET-PAYLOAD",
  headers: Record<string, string> = {},
  url = "https://relay.example/v1/reports",
): Request {
  return new Request(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "cf-connecting-ip": "203.0.113.20",
      ...headers,
    },
    body,
  });
}

function throwingBindings(): RelayEnv {
  return new Proxy({} as RelayEnv, {
    get(_target, property) {
      throw new Error(`binding accessed: ${String(property)}`);
    },
  });
}

function activeBindings(extra: Partial<RelayEnv> = {}): RelayEnv {
  return {
    DB: env.DB,
    INSTALLATION_ID_HMAC_KEY: "local-hmac-test-key",
    CANARY_ADMIN_TOKEN: "local-canary-token",
    ...extra,
  };
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

describe("mode parsing and health", () => {
  it("fails closed for missing or unknown rollout modes", () => {
    expect(parseRelayMode(undefined)).toBe("off");
    expect(parseRelayMode("ACTIVE")).toBe("off");
    expect(parseRelayMode("unexpected")).toBe("off");
    expect(parseRelayMode("active")).toBe("active");
  });

  it.each<RelayMode>(["off", "shadow", "canary", "active"])(
    "returns the exact %s health contract without touching bindings",
    async (mode) => {
      const worker = createRelayWorker({ mode });
      const response = await worker.fetch(
        new Request("https://relay.example/health"),
        throwingBindings(),
      );

      expect(response.status).toBe(200);
      expect(await response.text()).toBe(
        `{"service":"issue-relay","schema":1,"mode":"${mode}"}`,
      );
    },
  );

  it("uses off for an invalid environment mode without reading other bindings", async () => {
    const touched: string[] = [];
    const bindings = new Proxy({} as RelayEnv, {
      get(_target, property) {
        touched.push(String(property));
        if (property === "RELAY_MODE") {
          return "invalid";
        }
        throw new Error(`unexpected binding: ${String(property)}`);
      },
    });
    const response = await createRelayWorker().fetch(request(), bindings);

    expect(response.status).toBe(503);
    expect(await response.text()).toBe(UNAVAILABLE_BODY);
    expect(touched).toEqual(["RELAY_MODE"]);
  });

  it("rejects non-HTTPS health before touching bindings", async () => {
    const response = await createRelayWorker({ mode: "active" }).fetch(
      new Request("http://relay.example/health"),
      throwingBindings(),
    );

    expect(response.status).toBe(400);
    expect(await response.text()).toBe(HTTPS_REQUIRED_BODY);
  });
});

describe("HTTPS and report routing", () => {
  it("rejects non-HTTPS reports before parsing the body or touching bindings", async () => {
    const parse = successfulParser();
    const incoming = request(
      "TOP-SECRET-PAYLOAD",
      {},
      "http://relay.example/v1/reports",
    );
    const response = await createRelayWorker({
      ...fixedOptions("active"),
      parseReport: parse,
    }).fetch(incoming, throwingBindings());

    expect(response.status).toBe(400);
    expect(await response.text()).toBe(HTTPS_REQUIRED_BODY);
    expect(incoming.bodyUsed).toBe(false);
    expect(parse).not.toHaveBeenCalled();
    expect(HTTPS_REQUIRED_BODY).not.toContain("TOP-SECRET-PAYLOAD");
  });

  it("keeps the old unversioned report path at a fixed 404", async () => {
    const parse = successfulParser();
    const incoming = request(
      "TOP-SECRET-PAYLOAD",
      {},
      "https://relay.example/reports",
    );
    const response = await createRelayWorker({
      ...fixedOptions("active"),
      parseReport: parse,
    }).fetch(incoming, throwingBindings());

    expect(response.status).toBe(404);
    expect(await response.text()).toBe(NOT_FOUND_BODY);
    expect(incoming.bodyUsed).toBe(false);
    expect(parse).not.toHaveBeenCalled();
  });

  it("allows only POST on the versioned report route", async () => {
    const parse = successfulParser();
    const incoming = new Request("https://relay.example/v1/reports");
    const response = await createRelayWorker({
      ...fixedOptions("shadow"),
      parseReport: parse,
    }).fetch(incoming, throwingBindings());

    expect(response.status).toBe(405);
    expect(await response.text()).toBe(METHOD_NOT_ALLOWED_BODY);
    expect(parse).not.toHaveBeenCalled();
  });
});

describe("off mode", () => {
  it("returns before parsing the body or touching any binding", async () => {
    const parse = successfulParser();
    const worker = createRelayWorker({
      ...fixedOptions("off"),
      parseReport: parse,
    });
    const incoming = request();
    const response = await worker.fetch(incoming, throwingBindings());

    expect(response.status).toBe(503);
    expect(await response.text()).toBe(UNAVAILABLE_BODY);
    expect(incoming.bodyUsed).toBe(false);
    expect(parse).not.toHaveBeenCalled();
  });
});

describe("shadow mode", () => {
  it("validates and accepts without D1, HMAC, canary, or GitHub bindings", async () => {
    const parse = successfulParser();
    const worker = createRelayWorker({
      ...fixedOptions("shadow"),
      parseReport: parse,
    });
    const response = await worker.fetch(request(), throwingBindings());

    expect(parse).toHaveBeenCalledOnce();
    expect(response.status).toBe(202);
    expect(await response.text()).toBe(ACCEPTED_BODY);
  });

  it("passes only process-local derived IP and installation keys to the limiter", async () => {
    const rawIp = "203.0.113.20";
    const rawInstallation = "4d951671-4580-4b5f-9a96-8f92a38d4f77";
    const ipKeys: string[] = [];
    const installationKeys: string[] = [];
    const response = await createRelayWorker({
      ...fixedOptions("shadow", makeReport(rawInstallation)),
      edgeLimiter: {
        checkIp(key) {
          ipKeys.push(key);
          return { allowed: true };
        },
        checkInstallation(key) {
          installationKeys.push(key);
          return { allowed: true };
        },
      },
    }).fetch(request(), throwingBindings());

    expect(response.status).toBe(202);
    expect(ipKeys).toHaveLength(1);
    expect(installationKeys).toHaveLength(1);
    expect(ipKeys[0]).toMatch(/^[0-9a-f]{64}$/);
    expect(installationKeys[0]).toMatch(/^[0-9a-f]{64}$/);
    expect(ipKeys[0]).not.toBe(rawIp);
    expect(installationKeys[0]).not.toBe(rawInstallation);
    expect(ipKeys[0]).not.toBe(installationKeys[0]);
  });

  it("enforces burst, minute, and installation controls without D1", async () => {
    let nowSeconds = 100;
    let reportIndex = 0;
    const reports = Array.from({ length: 20 }, (_, index) =>
      makeReport(
        `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
        index.toString(16).padStart(64, "0"),
      ),
    );
    const parse = vi.fn(async (): Promise<ParseReportResult> => ({
      ok: true,
      report: reports[reportIndex++] ?? reports[0],
    }));
    const burstWorker = createRelayWorker({
      ...fixedOptions("shadow"),
      parseReport: parse,
      now: () => nowSeconds,
    });
    for (let count = 0; count < 3; count += 1) {
      expect((await burstWorker.fetch(request(), throwingBindings())).status).toBe(
        202,
      );
    }
    expect((await burstWorker.fetch(request(), throwingBindings())).status).toBe(
      429,
    );

    reportIndex = 0;
    const minuteWorker = createRelayWorker({
      ...fixedOptions("shadow"),
      parseReport: parse,
      now: () => nowSeconds,
    });
    const minuteTimes = [100, 100, 100, 110, 110, 110, 120, 120, 120, 130];
    for (const timestamp of minuteTimes) {
      nowSeconds = timestamp;
      expect((await minuteWorker.fetch(request(), throwingBindings())).status).toBe(
        202,
      );
    }
    expect((await minuteWorker.fetch(request(), throwingBindings())).status).toBe(
      429,
    );

    const installationWorker = createRelayWorker({
      ...fixedOptions("shadow"),
      now: () => 100,
    });
    for (let count = 0; count < 3; count += 1) {
      expect(
        (
          await installationWorker.fetch(
            request("{}", {
              "cf-connecting-ip": `198.51.100.${count + 1}`,
            }),
            throwingBindings(),
          )
        ).status,
      ).toBe(202);
    }
    expect(
      (
        await installationWorker.fetch(
          request("{}", { "cf-connecting-ip": "198.51.100.4" }),
          throwingBindings(),
        )
      ).status,
    ).toBe(429);
  });

  it("returns validation failures without echoing the payload", async () => {
    const failureBody = JSON.stringify({
      error: { code: "invalid_report", retryable: false },
    });
    const parse = vi.fn(
      async (): Promise<ParseReportResult> => ({
        ok: false,
        response: new Response(failureBody, { status: 422 }),
      }),
    );
    const worker = createRelayWorker({
      ...fixedOptions("shadow"),
      parseReport: parse,
    });
    const response = await worker.fetch(
      request("TOP-SECRET-PAYLOAD"),
      throwingBindings(),
    );

    expect(response.status).toBe(422);
    expect(await response.text()).toBe(failureBody);
    expect(failureBody).not.toContain("TOP-SECRET-PAYLOAD");
  });

  it("contains unexpected validation failures in the stable 500 contract", async () => {
    const worker = createRelayWorker({
      ...fixedOptions("shadow"),
      parseReport: vi.fn(async () => {
        throw new Error("TOP-SECRET-VALIDATION-FAILURE");
      }),
    });
    const response = await worker.fetch(
      request("TOP-SECRET-PAYLOAD"),
      throwingBindings(),
    );

    expect(response.status).toBe(500);
    expect(await response.text()).toBe(INTERNAL_ERROR_BODY);
    expect(INTERNAL_ERROR_BODY).not.toMatch(/TOP-SECRET|Failure/);
  });

  it("generates canonical, non-reused UUIDv4 receipts", async () => {
    const responses = await Promise.all(
      Array.from({ length: 12 }, () =>
        createRelayWorker({
          mode: "shadow",
          parseReport: successfulParser(),
        }).fetch(request(), throwingBindings()),
      ),
    );
    const receipts = await Promise.all(
      responses.map(async (response) => {
        expect(response.status).toBe(202);
        const body = (await response.json()) as {
          status: string;
          receipt: string;
        };
        expect(body.status).toBe("accepted");
        expect(body.receipt).toMatch(
          /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
        );
        return body.receipt;
      }),
    );
    expect(new Set(receipts)).toHaveLength(receipts.length);
  });
});

describe("canary mode", () => {
  it("verifies the complete bearer value with a fixed-length digest comparison", async () => {
    expect(
      await verifyCanaryAuthorization(
        "Bearer local-canary-token",
        "local-canary-token",
      ),
    ).toBe(true);
    expect(
      await verifyCanaryAuthorization(
        "Bearer local-canary-tokeN",
        "local-canary-token",
      ),
    ).toBe(false);
    expect(
      await verifyCanaryAuthorization(undefined, "local-canary-token"),
    ).toBe(false);
    expect(
      await verifyCanaryAuthorization("local-canary-token", "local-canary-token"),
    ).toBe(false);
  });

  it("rejects missing authorization before parsing or D1 access", async () => {
    const parse = successfulParser();
    const touched: string[] = [];
    const bindings = new Proxy({} as RelayEnv, {
      get(_target, property) {
        touched.push(String(property));
        if (property === "CANARY_ADMIN_TOKEN") {
          return "local-canary-token";
        }
        throw new Error(`unexpected binding: ${String(property)}`);
      },
    });
    const incoming = request();
    const response = await createRelayWorker({
      ...fixedOptions("canary"),
      parseReport: parse,
    }).fetch(incoming, bindings);

    expect(response.status).toBe(401);
    expect(await response.text()).toBe(UNAUTHORIZED_BODY);
    expect(parse).not.toHaveBeenCalled();
    expect(incoming.bodyUsed).toBe(false);
    expect(touched).toEqual(["CANARY_ADMIN_TOKEN"]);
  });

  it("applies the per-edge IP gate before repeated canary authentication", async () => {
    const worker = createRelayWorker(fixedOptions("canary"));
    for (let count = 0; count < 3; count += 1) {
      const response = await worker.fetch(request(), activeBindings());
      expect(response.status).toBe(401);
      expect(await response.text()).toBe(UNAUTHORIZED_BODY);
    }

    const denied = await worker.fetch(request(), activeBindings());
    expect(denied.status).toBe(429);
    expect(await denied.text()).toBe(RATE_LIMITED_BODY);
  });

  it("accepts the exact bearer token and then follows the active path", async () => {
    const response = await createRelayWorker(fixedOptions("canary")).fetch(
      request("{}", {
        authorization: "Bearer local-canary-token",
      }),
      activeBindings(),
    );

    expect(response.status).toBe(503);
    expect(await response.text()).toBe(UNAVAILABLE_BODY);
    expect(
      await env.DB.prepare("SELECT state FROM issue_routes").first("state"),
    ).toBe("failed");
  });
});

describe("active mode", () => {
  it("uses only the dedicated reporter App bindings after public authorization", async () => {
    const base = activeBindings({
      GITHUB_APP_ID: "100001",
      GITHUB_APP_INSTALLATION_ID: "200001",
      GITHUB_APP_PRIVATE_KEY: "unused synthetic key",
    });
    const touched: string[] = [];
    const bindings = new Proxy(base, {
      get(target, property, receiver) {
        touched.push(String(property));
        if (
          property === "CANARY_ADMIN_TOKEN" ||
          property === "GITHUB_TOKEN"
        ) {
          throw new Error(`forbidden binding: ${String(property)}`);
        }
        return Reflect.get(target, property, receiver);
      },
    });
    const response = await createRelayWorker({
      ...fixedOptions("active"),
      github: {
        fetch: vi.fn(async () =>
          Response.json({ number: 42 }, { status: 201 }),
        ) as unknown as typeof fetch,
        getInstallationToken: vi.fn(async () => "synthetic-token"),
      },
    }).fetch(request(), bindings);

    expect(response.status).toBe(201);
    expect(await response.text()).toBe(
      JSON.stringify({
        status: "created",
        issue_url: "https://github.com/sanghyun-io/tunnelforge/issues/42",
      }),
    );
    expect(touched).toContain("INSTALLATION_ID_HMAC_KEY");
    expect(touched).toContain("DB");
    expect(touched).toContain("GITHUB_APP_ID");
    expect(touched).toContain("GITHUB_APP_INSTALLATION_ID");
    expect(touched).toContain("GITHUB_APP_PRIVATE_KEY");
    expect(touched).not.toContain("CANARY_ADMIN_TOKEN");
    expect(touched).not.toContain("GITHUB_TOKEN");
  });

  it("returns the accepted contract only for a live concurrent pending lease", async () => {
    const report = makeReport();
    const route = await new RelayStore(env.DB).acquireIssueLease(
      report.report.error_fingerprint,
      NOW_SECONDS,
    );
    expect(route.status).toBe("acquired");
    const workers = Array.from({ length: 8 }, () =>
      createRelayWorker(fixedOptions("active", report)),
    );
    const responses = await Promise.all(
      workers.map((worker, index) =>
        worker.fetch(
          request("{}", {
            "cf-connecting-ip": `198.51.100.${index + 1}`,
          }),
          activeBindings(),
        ),
      ),
    );

    for (const response of responses) {
      expect(response.status).toBe(202);
      expect(await response.text()).toBe(ACCEPTED_BODY);
    }
    expect(await env.DB.prepare("SELECT COUNT(*) AS total FROM issue_routes").first("total")).toBe(1);
    expect(await env.DB.prepare("SELECT COUNT(*) AS total FROM report_actions").first("total")).toBe(0);
  });

  it("returns a stable retryable error for a terminal unknown route", async () => {
    const report = makeReport();
    const store = new RelayStore(env.DB);
    const lease = await store.acquireIssueLease(
      report.report.error_fingerprint,
      NOW_SECONDS,
    );
    if (lease.status !== "acquired") {
      throw new Error("expected an acquired lease");
    }
    await store.markIssueUnknown(
      report.report.error_fingerprint,
      lease.leaseToken,
    );

    const response = await createRelayWorker(
      fixedOptions("active", report),
    ).fetch(request(), activeBindings());
    expect(response.status).toBe(503);
    expect(await response.text()).toBe(ROUTE_UNKNOWN_BODY);
    expect(await env.DB.prepare("SELECT COUNT(*) AS total FROM report_actions").first("total")).toBe(0);
  });

  it("returns the exact edge rate-limit error", async () => {
    const worker = createRelayWorker(fixedOptions("active"));
    for (let count = 0; count < 3; count += 1) {
      expect((await worker.fetch(request(), activeBindings())).status).toBe(503);
    }
    const denied = await worker.fetch(request(), activeBindings());
    expect(denied.status).toBe(429);
    expect(await denied.text()).toBe(RATE_LIMITED_BODY);
  });

  it("returns the exact per-install hourly quota error", async () => {
    for (let count = 0; count < 10; count += 1) {
      const report = makeReport(
        "4d951671-4580-4b5f-9a96-8f92a38d4f77",
        count.toString(16).padStart(64, "0"),
      );
      const worker = createRelayWorker(fixedOptions("active", report));
      expect(
        (
          await worker.fetch(
            request("{}", {
              "cf-connecting-ip": `192.0.2.${count + 1}`,
            }),
            activeBindings(),
          )
        ).status,
      ).toBe(503);
    }

    const denied = await createRelayWorker(
      fixedOptions(
        "active",
        makeReport(
          "4d951671-4580-4b5f-9a96-8f92a38d4f77",
          "f".repeat(64),
        ),
      ),
    ).fetch(
      request("{}", { "cf-connecting-ip": "192.0.2.100" }),
      activeBindings(),
    );
    expect(denied.status).toBe(429);
    expect(await denied.text()).toBe(RATE_LIMITED_BODY);
  });

  it("returns stable route errors without parsing or echoing payloads", async () => {
    const parse = successfulParser();
    const worker = createRelayWorker({
      ...fixedOptions("active"),
      parseReport: parse,
    });
    const response = await worker.fetch(
      new Request("https://relay.example/not-reports", {
        method: "POST",
        body: "TOP-SECRET-PAYLOAD",
      }),
      activeBindings(),
    );

    expect(response.status).toBe(404);
    expect(await response.text()).toBe(NOT_FOUND_BODY);
    expect(parse).not.toHaveBeenCalled();
    expect(NOT_FOUND_BODY).not.toContain("TOP-SECRET-PAYLOAD");
  });

  it("contains binding failures without logging secrets or exception objects", async () => {
    const spies = [
      vi.spyOn(console, "debug").mockImplementation(() => undefined),
      vi.spyOn(console, "info").mockImplementation(() => undefined),
      vi.spyOn(console, "log").mockImplementation(() => undefined),
      vi.spyOn(console, "warn").mockImplementation(() => undefined),
      vi.spyOn(console, "error").mockImplementation(() => undefined),
    ];
    const bindings = {
      INSTALLATION_ID_HMAC_KEY: "local-hmac-test-key",
      DB: new Proxy({} as D1Database, {
        get() {
          throw new Error("TOP-SECRET-EXCEPTION");
        },
      }),
    } as RelayEnv;
    const response = await createRelayWorker(fixedOptions("active")).fetch(
      request("TOP-SECRET-PAYLOAD"),
      bindings,
    );

    expect(response.status).toBe(500);
    expect(await response.text()).toBe(INTERNAL_ERROR_BODY);
    expect(INTERNAL_ERROR_BODY).not.toMatch(/TOP-SECRET|Exception/);
    for (const spy of spies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });
});

describe("scheduled cleanup", () => {
  it("uses only D1 and reconciles expired relay state", async () => {
    const store = new RelayStore(env.DB);
    const pending = await store.acquireIssueLease("1".repeat(64), NOW_SECONDS);
    const failed = await store.acquireIssueLease("2".repeat(64), NOW_SECONDS);
    const ready = await store.acquireIssueLease("3".repeat(64), NOW_SECONDS);
    const unknown = await store.acquireIssueLease("4".repeat(64), NOW_SECONDS);
    if (
      pending.status !== "acquired" ||
      failed.status !== "acquired" ||
      ready.status !== "acquired" ||
      unknown.status !== "acquired"
    ) {
      throw new Error("expected cleanup route leases");
    }
    await store.markIssueFailed("2".repeat(64), failed.leaseToken);
    await store.markIssueReady("3".repeat(64), ready.leaseToken, 93);
    await store.markIssueUnknown("4".repeat(64), unknown.leaseToken);
    await store.claimReportAction(
      "5".repeat(64),
      "6".repeat(64),
      "comment",
      NOW_SECONDS,
    );
    await consumeInstallationHourlyQuota(
      env.DB,
      "5".repeat(64),
      NOW_SECONDS,
    );
    await consumeGlobalWriteBudget(env.DB, "create", NOW_SECONDS);

    const touched: string[] = [];
    const bindings = new Proxy({ DB: env.DB } as RelayEnv, {
      get(target, property, receiver) {
        touched.push(String(property));
        if (property !== "DB") {
          throw new Error(`unexpected binding: ${String(property)}`);
        }
        return Reflect.get(target, property, receiver);
      },
    });
    await createRelayWorker({
      mode: "off",
      now: () => NOW_SECONDS + 24 * 60 * 60,
    }).scheduled(
      { cron: "*/15 * * * *", scheduledTime: 0 } as ScheduledController,
      bindings,
    );

    expect(touched).toEqual(["DB"]);
    expect(
      await env.DB.prepare(
        "SELECT fingerprint, state FROM issue_routes ORDER BY fingerprint",
      ).all(),
    ).toMatchObject({
      results: [
        { fingerprint: "1".repeat(64), state: "unknown" },
        { fingerprint: "3".repeat(64), state: "ready" },
        { fingerprint: "4".repeat(64), state: "unknown" },
      ],
    });
    expect(await env.DB.prepare("SELECT COUNT(*) AS total FROM report_actions").first("total")).toBe(0);
    expect(await env.DB.prepare("SELECT COUNT(*) AS total FROM write_budgets").first("total")).toBe(0);
  });

  it("drains an expired action backlog across bounded scheduled calls", async () => {
    await env.DB.prepare(
      `WITH RECURSIVE sequence(n) AS (
         SELECT 1
         UNION ALL SELECT n + 1 FROM sequence WHERE n < ?
       )
       INSERT INTO report_actions (
         installation_hmac, fingerprint, window, kind, state, expires_at,
         route_lease_token
       )
       SELECT printf('%064x', n), printf('%064x', n + 1000),
              ? + n, 'comment', 'pending', ?, NULL
       FROM sequence`,
    )
      .bind(RELAY_CLEANUP_BATCH_SIZE + 1, NOW_SECONDS, NOW_SECONDS)
      .run();
    const worker = createRelayWorker({
      mode: "off",
      now: () => NOW_SECONDS + 1,
    });
    const controller = {
      cron: "*/15 * * * *",
      scheduledTime: 0,
    } as ScheduledController;

    await worker.scheduled(controller, { DB: env.DB });
    expect(
      await env.DB.prepare("SELECT COUNT(*) AS total FROM report_actions").first(
        "total",
      ),
    ).toBe(1);

    await worker.scheduled(controller, { DB: env.DB });
    expect(
      await env.DB.prepare("SELECT COUNT(*) AS total FROM report_actions").first(
        "total",
      ),
    ).toBe(0);
  });
});
