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

import validMinimalFixture from "../../../contracts/error-reporting/v1/valid-minimal.json";
import { computeFingerprint } from "../src/fingerprint";
import {
  createRelayWorker,
  type RelayEnv,
} from "../src/index";
import {
  createEdgeRateLimiter,
  GLOBAL_WRITE_LIMITS,
} from "../src/quotas";
import type { ErrorReport } from "../src/types";

declare global {
  namespace Cloudflare {
    interface Env {
      DB: D1Database;
      TEST_MIGRATIONS: D1Migration[];
    }
  }
}

const NOW_SECONDS = 1_800_000_000;
const FORBIDDEN = [
  "SyntheticSecret-Do-Not-Expose",
  "SyntheticPrivateKey-Do-Not-Expose",
  "SyntheticInstallationToken-Do-Not-Expose",
  "[click me](https://attacker.invalid)",
] as const;

function cloneReport(): ErrorReport {
  return structuredClone(validMinimalFixture) as ErrorReport;
}

async function reportFor(index = 0): Promise<ErrorReport> {
  const report = cloneReport();
  report.report.anonymous_installation_id = crypto.randomUUID();
  report.error.error_code = `SECURITY_${index}`;
  report.report.error_fingerprint = await computeFingerprint(report);
  return report;
}

function requestFor(body: string, ip = "203.0.113.10"): Request {
  return new Request("https://relay.example.test/v1/reports", {
    method: "POST",
    headers: {
      "cf-connecting-ip": ip,
      "content-type": "application/json",
      authorization: `Bearer ${FORBIDDEN[0]}`,
    },
    body,
  });
}

function bindings(): RelayEnv {
  return {
    DB: env.DB,
    INSTALLATION_ID_HMAC_KEY: "synthetic-local-hmac-key",
    GITHUB_APP_ID: "100001",
    GITHUB_APP_INSTALLATION_ID: "200001",
    GITHUB_APP_PRIVATE_KEY: FORBIDDEN[1],
  };
}

function captureConsole() {
  return [
    vi.spyOn(console, "debug").mockImplementation(() => undefined),
    vi.spyOn(console, "error").mockImplementation(() => undefined),
    vi.spyOn(console, "info").mockImplementation(() => undefined),
    vi.spyOn(console, "log").mockImplementation(() => undefined),
    vi.spyOn(console, "warn").mockImplementation(() => undefined),
  ];
}

function expectForbiddenAbsent(value: string): void {
  for (const forbidden of FORBIDDEN) {
    expect(value).not.toContain(forbidden);
  }
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

describe("relay abuse and disclosure boundary", () => {
  it("rejects malformed and structurally hostile JSON without logging or echoing it", async () => {
    const spies = captureConsole();
    const valid = cloneReport() as unknown as Record<string, unknown>;
    valid["forged"] = FORBIDDEN[0];
    const hostileBodies = [
      `{"password":"${FORBIDDEN[0]}",`,
      JSON.stringify(valid),
      JSON.stringify({
        nested: {
          a: { b: { c: { d: { e: { f: { g: { h: { i: { j: { k: {
            secret: FORBIDDEN[0],
          } } } } } } } } } } },
        },
      }),
      JSON.stringify(Array.from({ length: 33 }, () => FORBIDDEN[0])),
      JSON.stringify({ oversized: `${FORBIDDEN[0]}${"x".repeat(4_097)}` }),
      JSON.stringify({ control: `\u0000\u001b${FORBIDDEN[0]}` }),
    ];

    const worker = createRelayWorker({
      mode: "shadow",
      edgeLimiter: createEdgeRateLimiter(),
    });
    for (const [index, body] of hostileBodies.entries()) {
      const response = await worker.fetch(
        requestFor(body, `203.0.113.${index + 10}`),
        bindings(),
      );
      expect(response.status).toBeGreaterThanOrEqual(400);
      expectForbiddenAbsent(await response.text());
    }

    const captured = JSON.stringify(spies.flatMap((spy) => spy.mock.calls));
    expectForbiddenAbsent(captured);
    for (const spy of spies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });

  it("removes client Markdown and controls before constructing a GitHub issue", async () => {
    const spies = captureConsole();
    const report = await reportFor(100);
    report.error.sanitized_message = `${FORBIDDEN[3]}\n# forged heading\u0000${FORBIDDEN[0]}`;
    report.report.error_fingerprint = await computeFingerprint(report);
    const outboundBodies: string[] = [];
    const githubFetch = vi.fn(async (_input, init) => {
      outboundBodies.push(String(init?.body ?? ""));
      return Response.json({ number: 101 }, { status: 201 });
    });
    const worker = createRelayWorker({
      mode: "active",
      now: () => NOW_SECONDS,
      edgeLimiter: createEdgeRateLimiter(),
      github: {
        fetch: githubFetch as unknown as typeof fetch,
        getInstallationToken: vi.fn(async () => FORBIDDEN[2]),
      },
    });

    const response = await worker.fetch(
      requestFor(JSON.stringify(report)),
      bindings(),
    );

    expect(response.status).toBe(201);
    expect(githubFetch).toHaveBeenCalledTimes(1);
    expectForbiddenAbsent(outboundBodies.join("\n"));
    expectForbiddenAbsent(await response.text());
    expectForbiddenAbsent(JSON.stringify(spies.flatMap((spy) => spy.mock.calls)));
  });

  it.each([
    ["thrown failure", () => Promise.reject(new Error(FORBIDDEN.join(" ")))],
    [
      "rejected response",
      () => new Response(FORBIDDEN.join(" "), { status: 500 }),
    ],
  ])("contains a secret-bearing GitHub %s", async (_name, failure) => {
    const spies = captureConsole();
    const report = await reportFor(200);
    const worker = createRelayWorker({
      mode: "active",
      now: () => NOW_SECONDS,
      edgeLimiter: createEdgeRateLimiter(),
      github: {
        fetch: vi.fn(failure) as unknown as typeof fetch,
        getInstallationToken: vi.fn(async () => FORBIDDEN[2]),
      },
    });

    const response = await worker.fetch(
      requestFor(JSON.stringify(report)),
      bindings(),
    );

    expect(response.status).toBe(503);
    const responseText = await response.text();
    expect(JSON.parse(responseText)).toEqual({
      error: { code: "route_unknown", retryable: true },
    });
    expectForbiddenAbsent(responseText);
    expectForbiddenAbsent(JSON.stringify(spies.flatMap((spy) => spy.mock.calls)));
  });

  it("caps GitHub create mutations globally across forged installations and IPs", async () => {
    let issueNumber = 100;
    const githubFetch = vi.fn(async () => {
      issueNumber += 1;
      return Response.json({ number: issueNumber }, { status: 201 });
    });
    const worker = createRelayWorker({
      mode: "active",
      now: () => NOW_SECONDS,
      edgeLimiter: createEdgeRateLimiter(),
      github: {
        fetch: githubFetch as unknown as typeof fetch,
        getInstallationToken: vi.fn(async () => FORBIDDEN[2]),
      },
    });
    const statuses: number[] = [];

    for (
      let index = 0;
      index < GLOBAL_WRITE_LIMITS.create.hour + 3;
      index += 1
    ) {
      const report = await reportFor(index);
      const response = await worker.fetch(
        requestFor(JSON.stringify(report), `198.51.100.${index + 1}`),
        bindings(),
      );
      statuses.push(response.status);
    }

    expect(githubFetch).toHaveBeenCalledTimes(GLOBAL_WRITE_LIMITS.create.hour);
    expect(statuses.filter((status) => status === 201)).toHaveLength(
      GLOBAL_WRITE_LIMITS.create.hour,
    );
    expect(statuses.slice(GLOBAL_WRITE_LIMITS.create.hour)).toEqual([429, 429, 429]);
  });
});
