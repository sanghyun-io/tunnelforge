export type PackageKind = "source" | "frozen";
export type OsFamily = "windows" | "macos" | "linux";
export type Architecture = "x86_64" | "arm64";
export type OperationKind = "export" | "import";
export type DatabaseEngine = "mysql" | "postgresql";
export type OperationPhase = "dump.run" | "dump.import";

export interface ReportMetadata {
  report_schema_version: 1;
  anonymous_installation_id: string;
  error_fingerprint: string;
}

export interface AppMetadata {
  version: string;
  package_kind: PackageKind;
  ui_language: string;
}

export interface SystemMetadata {
  os_family: OsFamily;
  os_version: string;
  architecture: Architecture;
  locale: string;
  utc_offset_minutes: number;
}

export interface RuntimeMetadata {
  python_version: string;
  qt_version: string;
  rust_core_version: string;
}

export interface OperationMetadata {
  kind: OperationKind;
  db_engine: DatabaseEngine;
  db_server_version?: string;
  phase: OperationPhase;
}

export interface AppFrame {
  module: string;
  function: string;
  line: number;
}

export interface ErrorMetadata {
  exception_class: string;
  error_code?: string;
  sanitized_message: string;
  app_frames: AppFrame[];
}

export interface ErrorReport {
  report: ReportMetadata;
  app: AppMetadata;
  system: SystemMetadata;
  runtime: RuntimeMetadata;
  operation: OperationMetadata;
  error: ErrorMetadata;
}

export type ParseErrorCode =
  | "method_not_allowed"
  | "unsupported_media_type"
  | "payload_too_large"
  | "invalid_json"
  | "invalid_report"
  | "fingerprint_mismatch"
  | "internal_error";

export interface ParseReportSuccess {
  ok: true;
  report: ErrorReport;
}

export interface ParseReportFailure {
  ok: false;
  response: Response;
}

export type ParseReportResult = ParseReportSuccess | ParseReportFailure;

export type RelayCounterName =
  | "report_accepted"
  | "method_rejected"
  | "media_type_rejected"
  | "body_too_large"
  | "json_rejected"
  | "schema_rejected"
  | "fingerprint_rejected"
  | "internal_error";

export interface RelayObservability {
  increment(counter: RelayCounterName): void;
  snapshot(): Readonly<Record<RelayCounterName, number>>;
}
