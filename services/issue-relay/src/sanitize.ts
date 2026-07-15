import type { ErrorReport } from "./types";

export function sanitizeReport(report: ErrorReport): ErrorReport {
  const operation: ErrorReport["operation"] = {
    kind: report.operation.kind,
    db_engine: report.operation.db_engine,
    phase: report.operation.phase,
  };
  if (report.operation.db_server_version !== undefined) {
    operation.db_server_version = report.operation.db_server_version;
  }

  const error: ErrorReport["error"] = {
    exception_class: report.error.exception_class,
    sanitized_message: "",
    app_frames: report.error.app_frames.map((frame) => ({
      module: frame.module,
      function: frame.function,
      line: frame.line,
    })),
  };
  if (report.error.error_code !== undefined) {
    error.error_code = report.error.error_code;
  }

  return {
    report: {
      report_schema_version: 1,
      anonymous_installation_id: report.report.anonymous_installation_id,
      error_fingerprint: report.report.error_fingerprint,
    },
    app: {
      version: report.app.version,
      package_kind: report.app.package_kind,
      ui_language: report.app.ui_language,
    },
    system: {
      os_family: report.system.os_family,
      os_version: report.system.os_version,
      architecture: report.system.architecture,
      locale: report.system.locale,
      utc_offset_minutes: report.system.utc_offset_minutes,
    },
    runtime: {
      python_version: report.runtime.python_version,
      qt_version: report.runtime.qt_version,
      rust_core_version: report.runtime.rust_core_version,
    },
    operation,
    error,
  };
}
