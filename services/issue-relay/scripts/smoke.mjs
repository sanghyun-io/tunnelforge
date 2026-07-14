import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

const MODES = new Set(["off", "shadow", "canary", "active"]);
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const ISSUE_URL = /^https:\/\/github\.com\/sanghyun-io\/tunnelforge\/issues\/[1-9][0-9]*$/;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);

function fail(message) {
  throw new Error(`relay smoke failed: ${message}`);
}

function configuration() {
  const endpointValue = process.env.RELAY_ENDPOINT;
  const mode = process.env.RELAY_MODE;
  if (typeof endpointValue !== "string" || endpointValue.length === 0) {
    fail("RELAY_ENDPOINT is required");
  }
  if (typeof mode !== "string" || !MODES.has(mode)) {
    fail("RELAY_MODE must be off, shadow, canary, or active");
  }

  const endpoint = new URL(endpointValue);
  const loopback = LOOPBACK_HOSTS.has(endpoint.hostname);
  if (endpoint.protocol !== "https:" && !(loopback && endpoint.protocol === "http:")) {
    fail("endpoint must use HTTPS; HTTP is allowed only for loopback mocks");
  }
  if (
    endpoint.username !== "" ||
    endpoint.password !== "" ||
    (endpoint.pathname !== "" && endpoint.pathname !== "/") ||
    endpoint.search !== "" ||
    endpoint.hash !== ""
  ) {
    fail("endpoint must be an origin without credentials, path, query, or fragment");
  }
  return { origin: endpoint.origin, mode, loopback };
}

function exactKeys(value, expected, name) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    fail(`${name} is not an object`);
  }
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (JSON.stringify(actual) !== JSON.stringify(wanted)) {
    fail(`${name} has unexpected fields`);
  }
}

async function responseJson(response) {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("application/json")) {
    fail("response is not JSON");
  }
  const text = await response.text();
  if (text.length > 4_096) {
    fail("response is oversized");
  }
  try {
    return JSON.parse(text);
  } catch {
    fail("response contains invalid JSON");
  }
}

async function request(url, init) {
  return fetch(url, {
    ...init,
    redirect: "error",
    signal: AbortSignal.timeout(10_000),
  });
}

function asciiJson(value) {
  return JSON.stringify(value).replace(/[\u0080-\uffff]/g, (character) =>
    `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

async function syntheticReport() {
  const fixtureUrl = new URL(
    "../../../contracts/error-reporting/v1/valid-minimal.json",
    import.meta.url,
  );
  const report = JSON.parse(await readFile(fixtureUrl, "utf8"));
  report.error.sanitized_message = "Synthetic smoke fixture.";
  const canonical = {
    app_frame_signature: report.error.app_frames.map(
      (frame) => `${frame.module}:${frame.function}:${frame.line}`,
    ),
    db_engine: report.operation.db_engine,
    error_code: report.error.error_code ?? "",
    exception_class: report.error.exception_class,
    operation_kind: report.operation.kind,
  };
  report.report.error_fingerprint = createHash("sha256")
    .update(asciiJson(canonical), "utf8")
    .digest("hex");
  return report;
}

function expectFixedError(status, body, expectedStatus, code, retryable) {
  if (status !== expectedStatus) {
    fail(`expected HTTP ${expectedStatus}, received ${status}`);
  }
  exactKeys(body, ["error"], "error response");
  exactKeys(body.error, ["code", "retryable"], "error detail");
  if (body.error.code !== code || body.error.retryable !== retryable) {
    fail(`unexpected ${code} response contract`);
  }
}

async function main() {
  const { origin, mode, loopback } = configuration();
  const healthResponse = await request(`${origin}/health`);
  const health = await responseJson(healthResponse);
  exactKeys(health, ["service", "schema", "mode"], "health response");
  if (
    healthResponse.status !== 200 ||
    health.service !== "issue-relay" ||
    health.schema !== 1 ||
    health.mode !== mode
  ) {
    fail("health contract or mode mismatch");
  }

  if (mode === "active" && !loopback) {
    console.log(`mode=${mode} synthetic smoke passed (health only)`);
    return;
  }

  const reportResponse = await request(`${origin}/v1/reports`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(await syntheticReport()),
  });
  const body = await responseJson(reportResponse);

  if (mode === "off") {
    expectFixedError(reportResponse.status, body, 503, "service_unavailable", true);
  } else if (mode === "canary") {
    expectFixedError(reportResponse.status, body, 401, "unauthorized", false);
  } else if (mode === "shadow") {
    if (reportResponse.status !== 202) {
      fail(`expected HTTP 202, received ${reportResponse.status}`);
    }
    exactKeys(body, ["status", "receipt"], "shadow response");
    if (body.status !== "accepted" || !UUID_V4.test(body.receipt)) {
      fail("invalid shadow response contract");
    }
  } else {
    if (reportResponse.status !== 201) {
      fail(`expected HTTP 201 from active loopback mock, received ${reportResponse.status}`);
    }
    exactKeys(body, ["status", "issue_url"], "active response");
    if (body.status !== "created" || !ISSUE_URL.test(body.issue_url)) {
      fail("invalid active response contract");
    }
  }

  console.log(`mode=${mode} synthetic smoke passed`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : "relay smoke failed");
  process.exitCode = 1;
});
