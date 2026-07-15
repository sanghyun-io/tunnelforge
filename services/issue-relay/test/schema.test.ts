import { afterAll, describe, expect, it, vi } from "vitest";

import invalidFixture from "../../../contracts/error-reporting/v1/invalid-cases.json";
import validFullFixture from "../../../contracts/error-reporting/v1/valid-full.json";
import validMinimalFixture from "../../../contracts/error-reporting/v1/valid-minimal.json";
import { computeFingerprint } from "../src/fingerprint";
import {
  createRelayObservability,
  RELAY_COUNTER_NAMES,
} from "../src/observability";
import { MAX_REPORT_BYTES, parseReport } from "../src/schema";
import type {
  ErrorReport,
  ParseErrorCode,
  ParseReportResult,
  RelayCounterName,
} from "../src/types";

const fixtureSnapshots = {
  invalid: JSON.stringify(invalidFixture),
  full: JSON.stringify(validFullFixture),
  minimal: JSON.stringify(validMinimalFixture),
};

function clone<T>(value: T): T {
  return structuredClone(value);
}

function reportFrom(value: unknown): ErrorReport {
  return clone(value) as ErrorReport;
}

async function withMatchingFingerprint(value: unknown): Promise<ErrorReport> {
  const report = reportFrom(value);
  report.report.error_fingerprint = await computeFingerprint(report);
  return report;
}

function requestForBody(
  body: BodyInit | null,
  contentType: string | null = "application/json",
  method = "POST",
  extraHeaders?: HeadersInit,
): Request {
  const headers = new Headers(extraHeaders);
  if (contentType !== null) {
    headers.set("content-type", contentType);
  }
  return new Request("https://relay.example.test/v1/reports", {
    method,
    headers,
    body,
  });
}

function requestForJson(payload: unknown, contentType?: string): Request {
  return requestForBody(JSON.stringify(payload), contentType);
}

function streamFromChunks(chunks: readonly Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(chunk);
      }
      controller.close();
    },
  });
}

async function expectFailure(
  resultOrPromise: ParseReportResult | Promise<ParseReportResult>,
  status: number,
  code: ParseErrorCode,
): Promise<Response> {
  const result = await resultOrPromise;
  expect(result.ok).toBe(false);
  if (result.ok) {
    throw new Error("expected parse failure");
  }

  expect(result.response.status).toBe(status);
  expect(result.response.headers.get("content-type")).toBe(
    "application/json; charset=utf-8",
  );
  expect(await result.response.clone().json()).toEqual({
    error: { code, retryable: false },
  });
  expect(await result.response.clone().text()).toBe(
    JSON.stringify({ error: { code, retryable: false } }),
  );
  return result.response;
}

type InvalidMutation = {
  name: string;
  mutate: (report: Record<string, any>) => void;
};

const schemaInvalidMutations: readonly InvalidMutation[] = [
  {
    name: "report schema version type",
    mutate: (value) => (value.report.report_schema_version = "1"),
  },
  {
    name: "report schema version value",
    mutate: (value) => (value.report.report_schema_version = 2),
  },
  {
    name: "installation UUID type",
    mutate: (value) => (value.report.anonymous_installation_id = 7),
  },
  {
    name: "installation UUID format",
    mutate: (value) =>
      (value.report.anonymous_installation_id =
        "00000000-0000-4000-8000-000000000000"),
  },
  {
    name: "fingerprint type",
    mutate: (value) => (value.report.error_fingerprint = 7),
  },
  {
    name: "fingerprint length",
    mutate: (value) => (value.report.error_fingerprint = "0".repeat(63)),
  },
  {
    name: "fingerprint pattern",
    mutate: (value) => (value.report.error_fingerprint = "A".repeat(64)),
  },
  {
    name: "application version type",
    mutate: (value) => (value.app.version = 1),
  },
  {
    name: "application version empty",
    mutate: (value) => (value.app.version = ""),
  },
  {
    name: "application version maximum",
    mutate: (value) => (value.app.version = `1.${"1".repeat(31)}`),
  },
  {
    name: "application version pattern",
    mutate: (value) => (value.app.version = "v2.3.1"),
  },
  {
    name: "package kind type",
    mutate: (value) => (value.app.package_kind = false),
  },
  {
    name: "package kind enum",
    mutate: (value) => (value.app.package_kind = "portable"),
  },
  {
    name: "UI language type",
    mutate: (value) => (value.app.ui_language = 1),
  },
  {
    name: "UI language empty",
    mutate: (value) => (value.app.ui_language = ""),
  },
  {
    name: "UI language pattern",
    mutate: (value) => (value.app.ui_language = "EN_us"),
  },
  {
    name: "OS family type",
    mutate: (value) => (value.system.os_family = 1),
  },
  {
    name: "OS family enum",
    mutate: (value) => (value.system.os_family = "freebsd"),
  },
  {
    name: "OS version type",
    mutate: (value) => (value.system.os_version = 1),
  },
  {
    name: "OS version empty",
    mutate: (value) => (value.system.os_version = ""),
  },
  {
    name: "OS version maximum",
    mutate: (value) => (value.system.os_version = `A${"1".repeat(64)}`),
  },
  {
    name: "OS version pattern",
    mutate: (value) => (value.system.os_version = "private build"),
  },
  {
    name: "architecture type",
    mutate: (value) => (value.system.architecture = 1),
  },
  {
    name: "architecture enum",
    mutate: (value) => (value.system.architecture = "x86"),
  },
  {
    name: "locale type",
    mutate: (value) => (value.system.locale = 1),
  },
  {
    name: "locale empty",
    mutate: (value) => (value.system.locale = ""),
  },
  {
    name: "locale pattern",
    mutate: (value) => (value.system.locale = "english_US"),
  },
  {
    name: "UTC offset type",
    mutate: (value) => (value.system.utc_offset_minutes = true),
  },
  {
    name: "UTC offset lower bound",
    mutate: (value) => (value.system.utc_offset_minutes = -841),
  },
  {
    name: "UTC offset upper bound",
    mutate: (value) => (value.system.utc_offset_minutes = 841),
  },
  ...["python_version", "qt_version", "rust_core_version"].flatMap(
    (field): InvalidMutation[] => [
      {
        name: `${field} type`,
        mutate: (value) => (value.runtime[field] = 1),
      },
      {
        name: `${field} empty`,
        mutate: (value) => (value.runtime[field] = ""),
      },
      {
        name: `${field} maximum`,
        mutate: (value) => (value.runtime[field] = `1.${"1".repeat(31)}`),
      },
      {
        name: `${field} pattern`,
        mutate: (value) => (value.runtime[field] = "3.12rc1"),
      },
    ],
  ),
  {
    name: "operation kind type",
    mutate: (value) => (value.operation.kind = 1),
  },
  {
    name: "operation kind enum",
    mutate: (value) => (value.operation.kind = "migrate"),
  },
  {
    name: "database engine type",
    mutate: (value) => (value.operation.db_engine = 1),
  },
  {
    name: "database engine enum",
    mutate: (value) => (value.operation.db_engine = "sqlite"),
  },
  {
    name: "database version type",
    mutate: (value) => (value.operation.db_server_version = 16),
  },
  {
    name: "database version minimum",
    mutate: (value) => (value.operation.db_server_version = "1"),
  },
  {
    name: "database version maximum",
    mutate: (value) =>
      (value.operation.db_server_version = `1.${"1".repeat(31)}`),
  },
  {
    name: "database version pattern",
    mutate: (value) => (value.operation.db_server_version = "16.3.1"),
  },
  {
    name: "operation phase type",
    mutate: (value) => (value.operation.phase = 1),
  },
  {
    name: "operation phase enum",
    mutate: (value) => (value.operation.phase = "connect"),
  },
  {
    name: "exception class type",
    mutate: (value) => (value.error.exception_class = 1),
  },
  {
    name: "exception class empty",
    mutate: (value) => (value.error.exception_class = ""),
  },
  {
    name: "exception class maximum",
    mutate: (value) => (value.error.exception_class = "E".repeat(129)),
  },
  {
    name: "exception class pattern",
    mutate: (value) => (value.error.exception_class = "bad class"),
  },
  {
    name: "error code type",
    mutate: (value) => (value.error.error_code = 1),
  },
  {
    name: "error code empty",
    mutate: (value) => (value.error.error_code = ""),
  },
  {
    name: "error code maximum",
    mutate: (value) => (value.error.error_code = "E".repeat(65)),
  },
  {
    name: "error code pattern",
    mutate: (value) => (value.error.error_code = "BAD CODE"),
  },
  {
    name: "sanitized message type",
    mutate: (value) => (value.error.sanitized_message = 1),
  },
  {
    name: "sanitized message maximum",
    mutate: (value) => (value.error.sanitized_message = "x".repeat(2001)),
  },
  {
    name: "application frames type",
    mutate: (value) => (value.error.app_frames = {}),
  },
  {
    name: "application frames maximum",
    mutate: (value) =>
      (value.error.app_frames = Array.from({ length: 21 }, () => ({
        module: "src.core.worker",
        function: "run",
        line: 1,
      }))),
  },
  {
    name: "frame type",
    mutate: (value) => (value.error.app_frames = ["frame"]),
  },
  {
    name: "frame module type",
    mutate: (value) => (value.error.app_frames[0].module = 1),
  },
  {
    name: "frame module empty",
    mutate: (value) => (value.error.app_frames[0].module = ""),
  },
  {
    name: "frame module maximum",
    mutate: (value) =>
      (value.error.app_frames[0].module = `src.${"m".repeat(157)}`),
  },
  {
    name: "frame module pattern",
    mutate: (value) => (value.error.app_frames[0].module = "external.worker"),
  },
  {
    name: "frame function type",
    mutate: (value) => (value.error.app_frames[0].function = 1),
  },
  {
    name: "frame function empty",
    mutate: (value) => (value.error.app_frames[0].function = ""),
  },
  {
    name: "frame function maximum",
    mutate: (value) =>
      (value.error.app_frames[0].function = "f".repeat(129)),
  },
  {
    name: "frame function pattern",
    mutate: (value) => (value.error.app_frames[0].function = "bad function"),
  },
  {
    name: "frame line type",
    mutate: (value) => (value.error.app_frames[0].line = true),
  },
  {
    name: "frame line lower bound",
    mutate: (value) => (value.error.app_frames[0].line = 0),
  },
  {
    name: "frame line upper bound",
    mutate: (value) => (value.error.app_frames[0].line = 10_000_001),
  },
];

describe("parseReport request boundary", () => {
  it.each([
    ["GET"],
    ["PUT"],
    ["PATCH"],
    ["DELETE"],
  ])("rejects the %s method with one fixed response", async (method) => {
    await expectFailure(
      parseReport(requestForBody(null, "application/json", method)),
      405,
      "method_not_allowed",
    );
  });

  it.each([
    null,
    "text/plain",
    "text/json",
    "application/problem+json",
    "application/jsonp",
    "application/json; charset=utf-16",
    "application/json; boundary=secret",
    "application/json; charset=utf-8; boundary=secret",
  ])("rejects a malicious or unsupported content type: %s", async (contentType) => {
    const report = await withMatchingFingerprint(validMinimalFixture);
    await expectFailure(
      parseReport(requestForBody(JSON.stringify(report), contentType)),
      415,
      "unsupported_media_type",
    );
  });

  it.each([
    "application/json",
    "APPLICATION/JSON",
    "application/json; charset=utf-8",
    "application/json;charset=UTF-8",
    'application/json; charset="utf-8"',
  ])("accepts JSON with a reasonable UTF-8 content type: %s", async (contentType) => {
    const report = await withMatchingFingerprint(validMinimalFixture);
    const result = await parseReport(requestForJson(report, contentType));

    expect(result.ok).toBe(true);
  });

  it("rejects an oversized Content-Length before reading the body", async () => {
    const request = requestForBody("{}", "application/json");
    request.headers.set("content-length", String(MAX_REPORT_BYTES + 1));

    await expectFailure(
      parseReport(request),
      413,
      "payload_too_large",
    );
  });

  it("rejects an oversized streamed body without a Content-Length", async () => {
    const request = requestForBody(
      streamFromChunks([
        new Uint8Array(MAX_REPORT_BYTES),
        new Uint8Array([0x20]),
      ]),
      "application/json",
    );

    await expectFailure(
      parseReport(request),
      413,
      "payload_too_large",
    );
  });

  it("accepts a streamed body at the exact 16 KiB byte boundary", async () => {
    const report = await withMatchingFingerprint(validMinimalFixture);
    const encoded = new TextEncoder().encode(JSON.stringify(report));
    const padding = new Uint8Array(MAX_REPORT_BYTES - encoded.byteLength).fill(0x20);
    const request = requestForBody(
      streamFromChunks([encoded, padding]),
      "application/json",
    );

    const result = await parseReport(request);

    expect(encoded.byteLength).toBeLessThan(MAX_REPORT_BYTES);
    expect(result.ok).toBe(true);
  });

  it("maps a body reader error to one fixed non-echoing response", async () => {
    const secret = "SyntheticReaderFailure-Do-Not-Echo";
    const stream = new ReadableStream<Uint8Array>({
      pull() {
        throw new Error(secret);
      },
    });

    const response = await expectFailure(
      parseReport(requestForBody(stream)),
      400,
      "invalid_json",
    );

    expect(await response.text()).not.toContain(secret);
  });

  it("keeps the oversized response fixed when stream cancellation rejects", async () => {
    const secret = "SyntheticCancellationFailure-Do-Not-Echo";
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(MAX_REPORT_BYTES));
        controller.enqueue(new Uint8Array([0x20]));
      },
      cancel() {
        cancelled = true;
        return Promise.reject(new Error(secret));
      },
    });

    const response = await expectFailure(
      parseReport(requestForBody(stream)),
      413,
      "payload_too_large",
    );

    expect(cancelled).toBe(true);
    expect(await response.text()).not.toContain(secret);
  });

  it.each([
    "",
    "{",
    '{"report":',
    '{"report":1} trailing',
    '"unterminated',
    '{"value":"bad\u0000control"}',
  ])("rejects malformed JSON without exposing parser detail: %s", async (body) => {
    await expectFailure(
      parseReport(requestForBody(body)),
      400,
      "invalid_json",
    );
  });

  it("rejects malformed UTF-8", async () => {
    await expectFailure(
      parseReport(requestForBody(new Uint8Array([0xc3, 0x28]))),
      400,
      "invalid_json",
    );
  });

  it("accepts a JSON string token at the parser maximum", async () => {
    await expectFailure(
      parseReport(requestForBody(JSON.stringify("x".repeat(4096)))),
      422,
      "invalid_report",
    );
  });

  it("rejects a JSON string token over the parser maximum", async () => {
    await expectFailure(
      parseReport(requestForBody(JSON.stringify("x".repeat(4097)))),
      400,
      "invalid_json",
    );
  });

  it("accepts a JSON number token at the parser maximum", async () => {
    await expectFailure(
      parseReport(requestForBody("1".repeat(32))),
      422,
      "invalid_report",
    );
  });

  it("rejects a JSON number token over the parser maximum", async () => {
    await expectFailure(
      parseReport(requestForBody("1".repeat(33))),
      400,
      "invalid_json",
    );
  });

  it.each([null, [], "report", 1, true])(
    "rejects a non-object JSON root: %s",
    async (root) => {
      await expectFailure(
        parseReport(requestForJson(root)),
        422,
        "invalid_report",
      );
    },
  );

  it.each([
    '{"report":{},"report":{}}',
    '{"report":{},"\\u0072eport":{}}',
    '{"report":{"error_fingerprint":"a","error_fingerprint":"a"}}',
  ])("rejects duplicate JSON members: %s", async (body) => {
    await expectFailure(
      parseReport(requestForBody(body)),
      400,
      "invalid_json",
    );
  });

  it("rejects excessive JSON depth", async () => {
    const body = `${"[".repeat(13)}null${"]".repeat(13)}`;
    await expectFailure(
      parseReport(requestForBody(body)),
      400,
      "invalid_json",
    );
  });

  it("rejects excessive JSON node count", async () => {
    const body = JSON.stringify(Array.from({ length: 300 }, () => null));
    await expectFailure(
      parseReport(requestForBody(body)),
      400,
      "invalid_json",
    );
  });

  it.each([
    JSON.stringify(Array.from({ length: 33 }, () => null)),
    JSON.stringify(
      Object.fromEntries(Array.from({ length: 33 }, (_, index) => [`k${index}`, 1])),
    ),
  ])("rejects oversized JSON collections", async (body) => {
    await expectFailure(
      parseReport(requestForBody(body)),
      400,
      "invalid_json",
    );
  });
});

describe("parseReport strict schema", () => {
  it.each([
    ["minimal", validMinimalFixture],
    ["full", validFullFixture],
  ])("accepts and reconstructs the shared %s fixture without free text", async (_name, fixture) => {
    const report = await withMatchingFingerprint(fixture);
    const result = await parseReport(requestForJson(report));
    const expected = structuredClone(report);
    expected.error.sanitized_message = "";

    expect(result.ok).toBe(true);
    if (!result.ok) {
      throw new Error("expected parse success");
    }
    expect(result.report).toEqual(expected);
    expect(result.report).not.toBe(report);
    expect(result.report.error.app_frames).not.toBe(report.error.app_frames);
  });

  it.each(invalidFixture.cases)(
    "rejects shared invalid fixture: $name",
    async ({ payload }) => {
      await expectFailure(
        parseReport(requestForJson(payload)),
        422,
        "invalid_report",
      );
    },
  );

  it.each(schemaInvalidMutations)("enforces $name", async ({ mutate }) => {
    const report = clone(validFullFixture) as Record<string, any>;
    mutate(report);

    await expectFailure(
      parseReport(requestForJson(report)),
      422,
      "invalid_report",
    );
  });

  it.each([
    [
      "report schema version",
      '"report_schema_version":1',
      '"report_schema_version":1.0000000000000001',
    ],
    [
      "UTC offset",
      '"utc_offset_minutes":540',
      '"utc_offset_minutes":840.0000000000000001',
    ],
    [
      "frame line",
      '"line":241',
      '"line":241.0000000000000001',
    ],
  ])("rejects a mathematically fractional %s token", async (_name, before, after) => {
    const body = JSON.stringify(validFullFixture).replace(before, after);

    expect(body).toContain(after);
    await expectFailure(
      parseReport(requestForBody(body)),
      422,
      "invalid_report",
    );
  });

  it("accepts and canonicalizes mathematically integral JSON number forms", async () => {
    const report = await withMatchingFingerprint(validFullFixture);
    report.system.utc_offset_minutes = -840;
    let body = JSON.stringify(report);
    body = body.replace('"report_schema_version":1', '"report_schema_version":1.0');
    body = body.replace('"utc_offset_minutes":-840', '"utc_offset_minutes":-8.40e2');
    body = body.replace('"line":241', '"line":2.4100e2');

    expect(body).toContain('"report_schema_version":1.0');
    expect(body).toContain('"utc_offset_minutes":-8.40e2');
    expect(body).toContain('"line":2.4100e2');

    const result = await parseReport(requestForBody(body));

    expect(result.ok).toBe(true);
    if (!result.ok) {
      throw new Error("expected parse success");
    }
    expect(result.report.report.report_schema_version).toBe(1);
    expect(result.report.system.utc_offset_minutes).toBe(-840);
    expect(result.report.error.app_frames[0]!.line).toBe(241);
  });

  it.each([
    ["root", (value: Record<string, any>) => (value.extra = true)],
    ["report", (value: Record<string, any>) => (value.report.extra = true)],
    ["app", (value: Record<string, any>) => (value.app.extra = true)],
    ["system", (value: Record<string, any>) => (value.system.extra = true)],
    ["runtime", (value: Record<string, any>) => (value.runtime.extra = true)],
    ["operation", (value: Record<string, any>) => (value.operation.extra = true)],
    ["error", (value: Record<string, any>) => (value.error.extra = true)],
    [
      "frame",
      (value: Record<string, any>) => (value.error.app_frames[0].extra = true),
    ],
  ])("rejects unknown fields on the %s object", async (_name, mutate) => {
    const report = clone(validFullFixture) as Record<string, any>;
    mutate(report);

    await expectFailure(
      parseReport(requestForJson(report)),
      422,
      "invalid_report",
    );
  });

  it("accepts schema boundary values", async () => {
    for (const offset of [-840, 840]) {
      const report = reportFrom(validFullFixture) as any;
      report.app.version = `1.${"1".repeat(30)}`;
      report.system.os_version = `A${"1".repeat(63)}`;
      report.system.utc_offset_minutes = offset;
      report.runtime.python_version = `1.${"1".repeat(30)}`;
      report.runtime.qt_version = `1.${"1".repeat(30)}`;
      report.runtime.rust_core_version = `1.${"1".repeat(30)}`;
      report.operation.db_server_version = `1.${"1".repeat(30)}`;
      report.error.exception_class = "E".repeat(128);
      report.error.error_code = "E".repeat(64);
      report.error.sanitized_message = "x".repeat(2000);
      report.error.app_frames = Array.from({ length: 20 }, (_, index) => ({
        module: `src.${"m".repeat(156)}`,
        function: "f".repeat(128),
        line: index === 0 ? 1 : 10_000_000,
      }));
      report.report.error_fingerprint = await computeFingerprint(report);

      const result = await parseReport(requestForJson(report));
      expect(result.ok).toBe(true);
    }
  });

  it("rejects a client fingerprint that differs from the canonical report", async () => {
    await expectFailure(
      parseReport(requestForJson(validMinimalFixture)),
      422,
      "fingerprint_mismatch",
    );
  });
});

describe("canonical fingerprint", () => {
  it("matches the fixed Python no-frame vector", async () => {
    const report = reportFrom(validMinimalFixture);
    expect(await computeFingerprint(report)).toBe(
      "89f9596300ecce6534542780e6960c23e52742ca268e1ec2f0914f78cf78f8bc",
    );
  });

  it("matches the fixed Python mixed-traceback vector", async () => {
    const report = reportFrom(validMinimalFixture);
    report.error.exception_class = "src.core.synthetic_worker.CodedError";
    report.error.error_code = "DUMP_IMPORT_FAILED";
    report.error.app_frames = [
      { module: "src.core.synthetic_outer", function: "app_outer", line: 2 },
      { module: "src.core.synthetic_inner", function: "app_inner", line: 2 },
    ];

    expect(await computeFingerprint(report)).toBe(
      "e21dd0c10e819627a76cccd0664db74b11d395e81728449c9d6e44c146043a88",
    );
  });

  it("excludes message, installation UUID, and phase but includes every routing field", async () => {
    const baseline = reportFrom(validFullFixture);
    const baselineFingerprint = await computeFingerprint(baseline);

    const excluded = reportFrom(baseline);
    excluded.report.anonymous_installation_id = "550e8400-e29b-41d4-a716-446655440000";
    excluded.error.sanitized_message = "A different safe message.";
    excluded.operation.phase = "dump.run";
    expect(await computeFingerprint(excluded)).toBe(baselineFingerprint);

    const mutations: Array<(report: ErrorReport) => void> = [
      (report) => (report.operation.kind = "export"),
      (report) => (report.operation.db_engine = "mysql"),
      (report) => (report.error.exception_class = "OtherError"),
      (report) => (report.error.error_code = "OTHER_CODE"),
      (report) => (report.error.app_frames[0]!.line += 1),
    ];
    for (const mutate of mutations) {
      const changed = reportFrom(baseline);
      mutate(changed);
      expect(await computeFingerprint(changed)).not.toBe(baselineFingerprint);
    }
  });
});

describe("safe errors and observability", () => {
  it("never echoes a payload, header, UUID, fingerprint, or parser exception", async () => {
    const secret = "SyntheticCredential-Do-Not-Echo";
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const request = requestForBody(
      `{"password":"${secret}",`,
      "application/json",
      "POST",
      { authorization: `Bearer ${secret}`, "x-private": secret },
    );

    const response = await expectFailure(
      parseReport(request),
      400,
      "invalid_json",
    );
    const exposed = `${await response.text()} ${JSON.stringify(response.headers)}`;
    expect(exposed).not.toContain(secret);
    expect(log).not.toHaveBeenCalled();
    expect(info).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });

  it("records only fixed enum counters and ignores forged names", () => {
    const observability = createRelayObservability();
    for (const counter of RELAY_COUNTER_NAMES) {
      observability.increment(counter);
    }
    observability.increment("credential=SyntheticSecret" as RelayCounterName);

    const snapshot = observability.snapshot();
    expect(Object.keys(snapshot).sort()).toEqual([...RELAY_COUNTER_NAMES].sort());
    expect(Object.values(snapshot)).toEqual(
      expect.arrayContaining(Array(RELAY_COUNTER_NAMES.length).fill(1)),
    );
    expect(JSON.stringify(snapshot)).not.toContain("SyntheticSecret");
  });
});

afterAll(() => {
  expect(JSON.stringify(invalidFixture)).toBe(fixtureSnapshots.invalid);
  expect(JSON.stringify(validFullFixture)).toBe(fixtureSnapshots.full);
  expect(JSON.stringify(validMinimalFixture)).toBe(fixtureSnapshots.minimal);
});
