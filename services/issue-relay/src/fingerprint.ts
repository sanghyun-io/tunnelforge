import type { ErrorReport } from "./types";

function asciiJson(value: unknown): string {
  return JSON.stringify(value).replace(/[\u0080-\uffff]/g, (character) =>
    `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

export async function computeFingerprint(report: ErrorReport): Promise<string> {
  const canonicalInput = {
    app_frame_signature: report.error.app_frames.map(
      (frame) => `${frame.module}:${frame.function}:${frame.line}`,
    ),
    db_engine: report.operation.db_engine,
    error_code: report.error.error_code ?? "",
    exception_class: report.error.exception_class,
    operation_kind: report.operation.kind,
  };
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(asciiJson(canonicalInput)),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}
