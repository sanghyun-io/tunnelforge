import { afterAll, describe, expect, it } from "vitest";

import redactionFixture from "../../../contracts/error-reporting/v1/redaction-cases.json";
import validFullFixture from "../../../contracts/error-reporting/v1/valid-full.json";
import validMinimalFixture from "../../../contracts/error-reporting/v1/valid-minimal.json";
import { sanitizeReport } from "../src/sanitize";
import type { ErrorReport } from "../src/types";

const fixtureSnapshots = {
  redaction: JSON.stringify(redactionFixture),
  full: JSON.stringify(validFullFixture),
  minimal: JSON.stringify(validMinimalFixture),
};

function reportWithMessage(message: string): ErrorReport {
  const report = structuredClone(validMinimalFixture) as ErrorReport;
  report.error.sanitized_message = message;
  return report;
}

describe("fail-closed free-text boundary", () => {
  it.each(redactionFixture.cases)(
    "empties every shared $category fixture: $name",
    ({ input }) => {
      const sanitized = sanitizeReport(reportWithMessage(input));

      expect(sanitized.error.sanitized_message).toBe("");
    },
  );

  it.each([
    ["safe-looking prose", "Import failed while processing an application request."],
    ["maximum message", "x".repeat(2000)],
    ["Markdown", "# heading [link](https://example.invalid) **bold**"],
    ["Unicode normalization expansion", "\ufb03".repeat(1500)],
    ["malformed credential", "Basic YTpi==="],
    ["SQL", "ANALYZE NO_WRITE_TO_BINLOG TABLE private_orders;"],
    ["DSN", "database=customer_private"],
    ["network target", "connection to \uace0\uac1d-db,\u5907\u7528-db failed"],
    [
      "quoted nested signature",
      'function "CustomerLookup"(wrapper(numeric(10,2))) does not exist',
    ],
    ["arbitrary identifier", "tenant ExampleCustomer account Synthetic Person"],
  ])("empties %s free text", (_name, message) => {
    const sanitized = sanitizeReport(reportWithMessage(message));

    expect(sanitized.error.sanitized_message).toBe("");
  });

  it("does not read the client free-text property", () => {
    const source = structuredClone(validMinimalFixture) as ErrorReport;
    let reads = 0;
    Object.defineProperty(source.error, "sanitized_message", {
      enumerable: true,
      get() {
        reads += 1;
        return "must not be accessed";
      },
    });

    const sanitized = sanitizeReport(source);

    expect(reads).toBe(0);
    expect(sanitized.error.sanitized_message).toBe("");
  });
});

describe("canonical structured reconstruction", () => {
  it("preserves allowlisted structured evidence and empties only free text", () => {
    const source = structuredClone(validFullFixture) as ErrorReport;
    source.error.sanitized_message = "safe-looking but untrusted";
    const expected = structuredClone(validFullFixture) as ErrorReport;
    expected.error.sanitized_message = "";

    const sanitized = sanitizeReport(source);

    expect(sanitized).toEqual(expected);
    expect(sanitized).not.toBe(source);
    expect(sanitized.error).not.toBe(source.error);
    expect(sanitized.error.app_frames).not.toBe(source.error.app_frames);
  });

  it("drops unknown properties at every object boundary", () => {
    const source = structuredClone(validFullFixture) as Record<string, any>;
    source.extra = "forbidden";
    source.report.extra = "forbidden";
    source.app.extra = "forbidden";
    source.system.extra = "forbidden";
    source.runtime.extra = "forbidden";
    source.operation.extra = "forbidden";
    source.error.extra = "forbidden";
    source.error.app_frames[0].extra = "forbidden";

    const sanitized = sanitizeReport(source as ErrorReport);
    const serialized = JSON.stringify(sanitized);

    expect(Object.keys(sanitized)).toEqual([
      "report",
      "app",
      "system",
      "runtime",
      "operation",
      "error",
    ]);
    expect(Object.keys(sanitized.error)).toEqual([
      "exception_class",
      "sanitized_message",
      "app_frames",
      "error_code",
    ]);
    expect(serialized).not.toContain("forbidden");
    expect(sanitized.error.sanitized_message).toBe("");
  });

  it("preserves optional structured fields only when present", () => {
    const minimal = sanitizeReport(
      structuredClone(validMinimalFixture) as ErrorReport,
    );
    const full = sanitizeReport(structuredClone(validFullFixture) as ErrorReport);

    expect("db_server_version" in minimal.operation).toBe(false);
    expect("error_code" in minimal.error).toBe(false);
    expect(full.operation.db_server_version).toBe("16.3");
    expect(full.error.error_code).toBe("DUMP_IMPORT_FAILED");
    expect(minimal.error.sanitized_message).toBe("");
    expect(full.error.sanitized_message).toBe("");
  });
});

afterAll(() => {
  expect(JSON.stringify(redactionFixture)).toBe(fixtureSnapshots.redaction);
  expect(JSON.stringify(validFullFixture)).toBe(fixtureSnapshots.full);
  expect(JSON.stringify(validMinimalFixture)).toBe(fixtureSnapshots.minimal);
});
