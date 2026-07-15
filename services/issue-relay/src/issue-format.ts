import type { ErrorReport } from "./types";

export const MAX_ISSUE_TITLE_LENGTH = 240;
export const MAX_ISSUE_BODY_LENGTH = 12_000;
export const MAX_RECURRENCE_COMMENT_LENGTH = 2_000;

const MAX_RECURRENCE_COUNT = 9_999;
const SHA256_HEX = /^[0-9a-f]{64}$/;

export interface FormattedIssue {
  readonly title: string;
  readonly body: string;
  readonly labels: readonly ["bug", "export" | "import", "auto-reported"];
}

const OS_NAMES = Object.freeze({
  windows: "Windows",
  macos: "macOS",
  linux: "Linux",
});

const DATABASE_NAMES = Object.freeze({
  mysql: "MySQL",
  postgresql: "PostgreSQL",
});

const OPERATION_NAMES = Object.freeze({
  export: "Export",
  import: "Import",
});

function requireOperationKind(
  value: ErrorReport["operation"]["kind"],
): "export" | "import" {
  if (value !== "export" && value !== "import") {
    throw new TypeError("invalid issue operation");
  }
  return value;
}

function requireFingerprint(fingerprint: string): void {
  if (!SHA256_HEX.test(fingerprint)) {
    throw new TypeError("invalid issue fingerprint");
  }
}

function truncate(value: string, maximum: number): string {
  if (value.length <= maximum) {
    return value;
  }
  const contentLimit = Math.max(0, maximum - 3);
  let result = "";
  for (const character of value) {
    if (result.length + character.length > contentLimit) {
      break;
    }
    result += character;
  }
  return `${result}...`;
}

function safeScalar(value: string, maximum = 256): string {
  const withoutControls = Array.from(value)
    .filter((character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint >= 0x20 && codePoint !== 0x7f;
    })
    .join("");
  return truncate(
    withoutControls.replace(/[\\`*_[\]{}()#!+|<>]/g, "\\$&"),
    maximum,
  );
}

function utcOffset(minutes: number): string {
  const sign = minutes < 0 ? "-" : "+";
  const absolute = Math.abs(minutes);
  const hours = Math.floor(absolute / 60).toString().padStart(2, "0");
  const remainder = (absolute % 60).toString().padStart(2, "0");
  return `UTC${sign}${hours}:${remainder}`;
}

function databaseSummary(report: ErrorReport): string {
  const name = DATABASE_NAMES[report.operation.db_engine];
  return report.operation.db_server_version === undefined
    ? name
    : `${name} ${safeScalar(report.operation.db_server_version, 32)}`;
}

function environmentSummary(report: ErrorReport): string {
  return `${OS_NAMES[report.system.os_family]} ${safeScalar(report.system.os_version, 64)} (${report.system.architecture})`;
}

export function fingerprintMarker(fingerprint: string): string {
  requireFingerprint(fingerprint);
  return `<!-- tunnelforge-fingerprint:${fingerprint} -->`;
}

export function formatIssue(
  report: ErrorReport,
  fingerprint: string,
): FormattedIssue {
  const marker = fingerprintMarker(fingerprint);
  const operationKind = requireOperationKind(report.operation.kind);
  const operation = OPERATION_NAMES[operationKind];
  const database = DATABASE_NAMES[report.operation.db_engine];
  const title = truncate(
    `[Auto Report] ${operation} ${database} ${report.operation.phase} failure (${safeScalar(report.error.exception_class, 128)})`,
    MAX_ISSUE_TITLE_LENGTH,
  );
  const errorCode =
    report.error.error_code === undefined
      ? "not provided"
      : safeScalar(report.error.error_code, 64);
  const frames =
    report.error.app_frames.length === 0
      ? "- No application frame was available."
      : report.error.app_frames
          .map(
            (frame) =>
              `- ${safeScalar(frame.module, 160)} :: ${safeScalar(frame.function, 128)} at line ${frame.line}`,
          )
          .join("\n");
  const bodyContent = [
    "## Automatic TunnelForge error report",
    "",
    `- TunnelForge ${safeScalar(report.app.version, 32)} (${report.app.package_kind}; UI ${safeScalar(report.app.ui_language, 16)})`,
    `- Operation: ${operation} / ${databaseSummary(report)} / ${report.operation.phase}`,
    `- Error: ${safeScalar(report.error.exception_class, 128)}`,
    `- Error code: ${errorCode}`,
    `- Environment: ${environmentSummary(report)}; ${safeScalar(report.system.locale, 16)}; ${utcOffset(report.system.utc_offset_minutes)}`,
    `- Runtime: Python ${safeScalar(report.runtime.python_version, 32)} / Qt ${safeScalar(report.runtime.qt_version, 32)} / Rust Core ${safeScalar(report.runtime.rust_core_version, 32)}`,
    "",
    "### Application frames",
    "",
    frames,
  ].join("\n");
  const bodyLimit = MAX_ISSUE_BODY_LENGTH - marker.length - 2;
  const body = `${truncate(bodyContent, bodyLimit)}\n\n${marker}`;

  return Object.freeze({
    title,
    body,
    labels: Object.freeze([
      "bug",
      operationKind,
      "auto-reported",
    ]) as FormattedIssue["labels"],
  });
}

export function formatRecurrenceComment(
  report: ErrorReport,
  recurrenceCount: number,
): string {
  const operationKind = requireOperationKind(report.operation.kind);
  const count = Number.isFinite(recurrenceCount)
    ? Math.min(
        MAX_RECURRENCE_COUNT,
        Math.max(1, Math.trunc(recurrenceCount)),
      )
    : 1;
  const comment = [
    "### Automatic recurrence",
    "",
    `- Occurrences in the current relay window: ${count}`,
    `- TunnelForge ${safeScalar(report.app.version, 32)} (${report.app.package_kind})`,
    `- Environment: ${environmentSummary(report)}`,
    `- Database: ${databaseSummary(report)}`,
    `- Operation: ${OPERATION_NAMES[operationKind]} / ${report.operation.phase}`,
  ].join("\n");
  return truncate(comment, MAX_RECURRENCE_COMMENT_LENGTH);
}
