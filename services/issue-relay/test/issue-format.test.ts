import { describe, expect, it } from "vitest";

import {
  MAX_ISSUE_BODY_LENGTH,
  MAX_ISSUE_TITLE_LENGTH,
  MAX_RECURRENCE_COMMENT_LENGTH,
  fingerprintMarker,
  formatIssue,
  formatRecurrenceComment,
} from "../src/issue-format";
import type { ErrorReport } from "../src/types";

const FINGERPRINT = "a".repeat(64);

function makeReport(): ErrorReport {
  return {
    report: {
      report_schema_version: 1,
      anonymous_installation_id: "4d951671-4580-4b5f-9a96-8f92a38d4f77",
      error_fingerprint: FINGERPRINT,
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
      sanitized_message: "RAW **client markdown** [secret](https://evil.invalid)",
      app_frames: [
        { module: "src.ui.dialogs.db_dialogs", function: "<module>", line: 41 },
      ],
    },
  };
}

describe("server-owned issue formatting", () => {
  it("builds a fixed title, body, and operation labels without client free text", () => {
    const report = Object.assign(makeReport(), {
      title: "CLIENT TITLE",
      body: "CLIENT BODY",
      labels: ["security"],
    });

    const issue = formatIssue(report, FINGERPRINT);

    expect(issue.title).toBe(
      "[Auto Report] Export PostgreSQL dump.run failure (RuntimeError)",
    );
    expect(issue.labels).toEqual(["bug", "export", "auto-reported"]);
    expect(issue.body).toContain("TunnelForge 2.3.1");
    expect(issue.body).toContain("Windows 11.0");
    expect(issue.body).toContain("PostgreSQL 17.5");
    expect(issue.body).toContain("TF-100");
    expect(issue.body).not.toMatch(
      /RAW|client markdown|secret|evil\.invalid|CLIENT TITLE|CLIENT BODY|security/,
    );
    expect(issue.body).not.toContain(
      report.report.anonymous_installation_id,
    );
  });

  it("escapes defensive Markdown inputs and keeps every output bounded", () => {
    const report = makeReport();
    report.error.exception_class = "Bad_[link](https://evil.invalid)<tag>";
    report.error.error_code = "code|table";
    report.error.app_frames = Array.from({ length: 20 }, (_, index) => ({
      module: `src.module_${"x".repeat(140)}${index}`,
      function: "<module>",
      line: index + 1,
    }));

    const issue = formatIssue(report, FINGERPRINT);

    expect(issue.title.length).toBeLessThanOrEqual(MAX_ISSUE_TITLE_LENGTH);
    expect(issue.body.length).toBeLessThanOrEqual(MAX_ISSUE_BODY_LENGTH);
    expect(issue.title).not.toContain("[link](https://evil.invalid)");
    expect(issue.body).not.toContain("<tag>");
    expect(issue.body).toContain("code\\|table");
    expect(issue.body).toContain("\\<module\\>");
  });

  it("keeps surrogate-pair defensive inputs within UTF-16 limits", () => {
    const report = makeReport();
    report.error.exception_class = "😀".repeat(500);
    report.system.os_version = "😀".repeat(500);

    const issue = formatIssue(report, FINGERPRINT);
    const comment = formatRecurrenceComment(report, 1);

    expect(issue.title.length).toBeLessThanOrEqual(MAX_ISSUE_TITLE_LENGTH);
    expect(issue.body.length).toBeLessThanOrEqual(MAX_ISSUE_BODY_LENGTH);
    expect(comment.length).toBeLessThanOrEqual(
      MAX_RECURRENCE_COMMENT_LENGTH,
    );
  });

  it("places the validated fingerprint only in one hidden marker", () => {
    const issue = formatIssue(makeReport(), FINGERPRINT);
    const marker = fingerprintMarker(FINGERPRINT);

    expect(marker).toBe(`<!-- tunnelforge-fingerprint:${FINGERPRINT} -->`);
    expect(issue.body.endsWith(marker)).toBe(true);
    expect(issue.body.split(FINGERPRINT)).toHaveLength(2);
  });

  it("rejects a non-canonical server fingerprint", () => {
    expect(() => formatIssue(makeReport(), "A".repeat(64))).toThrow(
      "invalid issue fingerprint",
    );
    expect(() => fingerprintMarker("a".repeat(63))).toThrow(
      "invalid issue fingerprint",
    );
  });

  it("rejects a runtime operation outside the validated label allowlist", () => {
    const report = makeReport();
    report.operation.kind = "security" as ErrorReport["operation"]["kind"];

    expect(() => formatIssue(report, FINGERPRINT)).toThrow(
      "invalid issue operation",
    );
  });

  it("formats bounded recurrence counts and structured environment summaries only", () => {
    const report = makeReport();
    const comment = formatRecurrenceComment(report, 27);

    expect(comment).toContain("27");
    expect(comment).toContain("TunnelForge 2.3.1");
    expect(comment).toContain("Windows 11.0 (x86_64)");
    expect(comment).toContain("PostgreSQL 17.5");
    expect(comment).not.toMatch(/RAW|client markdown|secret|evil\.invalid/);
    expect(comment).not.toContain(report.report.anonymous_installation_id);
    expect(comment).not.toContain(FINGERPRINT);
    expect(comment.length).toBeLessThanOrEqual(
      MAX_RECURRENCE_COMMENT_LENGTH,
    );
  });

  it("clamps untrusted recurrence counts to a fixed scalar range", () => {
    expect(formatRecurrenceComment(makeReport(), -5)).toContain(
      "Occurrences in the current relay window: 1",
    );
    expect(formatRecurrenceComment(makeReport(), 1_000_000)).toContain(
      "Occurrences in the current relay window: 9999",
    );
  });
});
