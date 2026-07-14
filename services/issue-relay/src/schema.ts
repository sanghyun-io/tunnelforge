import { computeFingerprint } from "./fingerprint";
import { createRelayObservability } from "./observability";
import { sanitizeReport } from "./sanitize";
import type {
  Architecture,
  DatabaseEngine,
  ErrorReport,
  OperationKind,
  OperationPhase,
  OsFamily,
  PackageKind,
  ParseErrorCode,
  ParseReportFailure,
  ParseReportResult,
  RelayCounterName,
} from "./types";

export const MAX_REPORT_BYTES = 16 * 1024;

const MAX_JSON_DEPTH = 12;
const MAX_JSON_NODES = 256;
const MAX_COLLECTION_ITEMS = 32;
const MAX_JSON_STRING_LENGTH = 4096;
const MAX_NUMBER_TOKEN_LENGTH = 32;

const PACKAGE_KINDS = new Set<PackageKind>(["source", "frozen"]);
const OS_FAMILIES = new Set<OsFamily>(["windows", "macos", "linux"]);
const ARCHITECTURES = new Set<Architecture>(["x86_64", "arm64"]);
const OPERATION_KINDS = new Set<OperationKind>(["export", "import"]);
const DB_ENGINES = new Set<DatabaseEngine>(["mysql", "postgresql"]);
const OPERATION_PHASES = new Set<OperationPhase>(["dump.run", "dump.import"]);

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const EMPTY_UUID_V4 = "00000000-0000-4000-8000-000000000000";
const DOTTED_VERSION_PATTERN = /^[0-9]+(?:\.[0-9]+){1,3}$/;
const MAJOR_MINOR_PATTERN = /^[0-9]+\.[0-9]+$/;
const LANGUAGE_PATTERN = /^[a-z]{2,3}(?:-[A-Z]{2})?$/;
const LOCALE_PATTERN = /^[A-Za-z]{2,3}(?:[-_][A-Za-z]{2})?$/;
const SAFE_OS_VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const EXCEPTION_CLASS_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$/;
const ERROR_CODE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/;
const FRAME_MODULE_PATTERN = /^src(?:\.[A-Za-z_][A-Za-z0-9_]*)+$/;
const FRAME_FUNCTION_PATTERN = /^(?:[A-Za-z_][A-Za-z0-9_]*|<module>)$/;
const JSON_CONTENT_TYPE = /^\s*application\/json\s*(?:;\s*charset\s*=\s*(?:utf-8|"utf-8")\s*)?$/i;

const TOP_LEVEL_FIELDS = ["report", "app", "system", "runtime", "operation", "error"] as const;
const REPORT_FIELDS = ["report_schema_version", "anonymous_installation_id", "error_fingerprint"] as const;
const APP_FIELDS = ["version", "package_kind", "ui_language"] as const;
const SYSTEM_FIELDS = ["os_family", "os_version", "architecture", "locale", "utc_offset_minutes"] as const;
const RUNTIME_FIELDS = ["python_version", "qt_version", "rust_core_version"] as const;
const OPERATION_REQUIRED_FIELDS = ["kind", "db_engine", "phase"] as const;
const OPERATION_FIELDS = [...OPERATION_REQUIRED_FIELDS, "db_server_version"] as const;
const ERROR_REQUIRED_FIELDS = ["exception_class", "sanitized_message", "app_frames"] as const;
const ERROR_FIELDS = [...ERROR_REQUIRED_FIELDS, "error_code"] as const;
const FRAME_FIELDS = ["module", "function", "line"] as const;

const observability = createRelayObservability();

const ERROR_SPECS: Readonly<
  Record<
    ParseErrorCode,
    { readonly status: number; readonly retryable: boolean; readonly counter: RelayCounterName }
  >
> = {
  method_not_allowed: {
    status: 405,
    retryable: false,
    counter: "method_rejected",
  },
  unsupported_media_type: {
    status: 415,
    retryable: false,
    counter: "media_type_rejected",
  },
  payload_too_large: {
    status: 413,
    retryable: false,
    counter: "body_too_large",
  },
  invalid_json: { status: 400, retryable: false, counter: "json_rejected" },
  invalid_report: {
    status: 422,
    retryable: false,
    counter: "schema_rejected",
  },
  fingerprint_mismatch: {
    status: 422,
    retryable: false,
    counter: "fingerprint_rejected",
  },
  internal_error: { status: 500, retryable: true, counter: "internal_error" },
};

const ERROR_BODIES = Object.fromEntries(
  Object.entries(ERROR_SPECS).map(([code, spec]) => [
    code,
    JSON.stringify({ error: { code, retryable: spec.retryable } }),
  ]),
) as Record<ParseErrorCode, string>;

function failure(code: ParseErrorCode): ParseReportFailure {
  const spec = ERROR_SPECS[code];
  observability.increment(spec.counter);
  return {
    ok: false,
    response: new Response(ERROR_BODIES[code], {
      status: spec.status,
      headers: {
        "cache-control": "no-store",
        "content-type": "application/json; charset=utf-8",
      },
    }),
  };
}

class JsonBoundaryError extends Error {
  constructor() {
    super("invalid bounded JSON");
  }
}

class JsonNumberToken {
  constructor(readonly source: string) {}
}

class BoundedJsonParser {
  private index = 0;
  private nodes = 0;

  constructor(private readonly source: string) {}

  parse(): unknown {
    this.skipWhitespace();
    const value = this.parseValue(1);
    this.skipWhitespace();
    if (this.index !== this.source.length) {
      throw new JsonBoundaryError();
    }
    return value;
  }

  private parseValue(depth: number): unknown {
    if (depth > MAX_JSON_DEPTH || ++this.nodes > MAX_JSON_NODES) {
      throw new JsonBoundaryError();
    }
    const character = this.source[this.index];
    if (character === "{") {
      return this.parseObject(depth);
    }
    if (character === "[") {
      return this.parseArray(depth);
    }
    if (character === '"') {
      return this.parseString();
    }
    if (character === "t") {
      return this.parseLiteral("true", true);
    }
    if (character === "f") {
      return this.parseLiteral("false", false);
    }
    if (character === "n") {
      return this.parseLiteral("null", null);
    }
    if (character === "-" || (character !== undefined && /[0-9]/.test(character))) {
      return this.parseNumber();
    }
    throw new JsonBoundaryError();
  }

  private parseObject(depth: number): Record<string, unknown> {
    this.index += 1;
    this.skipWhitespace();
    const result = Object.create(null) as Record<string, unknown>;
    const keys = new Set<string>();
    let members = 0;
    if (this.source[this.index] === "}") {
      this.index += 1;
      return result;
    }

    while (this.index < this.source.length) {
      if (this.source[this.index] !== '"' || ++members > MAX_COLLECTION_ITEMS) {
        throw new JsonBoundaryError();
      }
      const key = this.parseString();
      if (keys.has(key)) {
        throw new JsonBoundaryError();
      }
      keys.add(key);
      this.skipWhitespace();
      if (this.source[this.index] !== ":") {
        throw new JsonBoundaryError();
      }
      this.index += 1;
      this.skipWhitespace();
      result[key] = this.parseValue(depth + 1);
      this.skipWhitespace();
      const separator = this.source[this.index++];
      if (separator === "}") {
        return result;
      }
      if (separator !== ",") {
        throw new JsonBoundaryError();
      }
      this.skipWhitespace();
    }
    throw new JsonBoundaryError();
  }

  private parseArray(depth: number): unknown[] {
    this.index += 1;
    this.skipWhitespace();
    const result: unknown[] = [];
    if (this.source[this.index] === "]") {
      this.index += 1;
      return result;
    }

    while (this.index < this.source.length) {
      if (result.length >= MAX_COLLECTION_ITEMS) {
        throw new JsonBoundaryError();
      }
      result.push(this.parseValue(depth + 1));
      this.skipWhitespace();
      const separator = this.source[this.index++];
      if (separator === "]") {
        return result;
      }
      if (separator !== ",") {
        throw new JsonBoundaryError();
      }
      this.skipWhitespace();
    }
    throw new JsonBoundaryError();
  }

  private parseString(): string {
    if (this.source[this.index++] !== '"') {
      throw new JsonBoundaryError();
    }
    let result = "";
    let length = 0;
    while (this.index < this.source.length) {
      const character = this.source[this.index++];
      if (character === '"') {
        return result;
      }
      if (character === undefined) {
        break;
      }

      let decoded: string;
      if (character === "\\") {
        decoded = this.parseEscape();
      } else {
        const code = character.charCodeAt(0);
        if (code <= 0x1f || (code >= 0xdc00 && code <= 0xdfff)) {
          throw new JsonBoundaryError();
        }
        if (code >= 0xd800 && code <= 0xdbff) {
          const low = this.source[this.index++];
          if (
            low === undefined ||
            low.charCodeAt(0) < 0xdc00 ||
            low.charCodeAt(0) > 0xdfff
          ) {
            throw new JsonBoundaryError();
          }
          decoded = character + low;
        } else {
          decoded = character;
        }
      }
      result += decoded;
      length += 1;
      if (length > MAX_JSON_STRING_LENGTH) {
        throw new JsonBoundaryError();
      }
    }
    throw new JsonBoundaryError();
  }

  private parseEscape(): string {
    const escaped = this.source[this.index++];
    const simple: Readonly<Record<string, string>> = {
      '"': '"',
      "\\": "\\",
      "/": "/",
      b: "\b",
      f: "\f",
      n: "\n",
      r: "\r",
      t: "\t",
    };
    if (escaped !== undefined && simple[escaped] !== undefined) {
      return simple[escaped];
    }
    if (escaped !== "u") {
      throw new JsonBoundaryError();
    }

    const high = this.parseHexCodeUnit();
    if (high >= 0xdc00 && high <= 0xdfff) {
      throw new JsonBoundaryError();
    }
    if (high < 0xd800 || high > 0xdbff) {
      return String.fromCharCode(high);
    }
    if (this.source.slice(this.index, this.index + 2) !== "\\u") {
      throw new JsonBoundaryError();
    }
    this.index += 2;
    const low = this.parseHexCodeUnit();
    if (low < 0xdc00 || low > 0xdfff) {
      throw new JsonBoundaryError();
    }
    return String.fromCodePoint(
      0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00),
    );
  }

  private parseHexCodeUnit(): number {
    const digits = this.source.slice(this.index, this.index + 4);
    if (!/^[0-9A-Fa-f]{4}$/.test(digits)) {
      throw new JsonBoundaryError();
    }
    this.index += 4;
    return Number.parseInt(digits, 16);
  }

  private parseNumber(): JsonNumberToken {
    const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(
      this.source.slice(this.index),
    );
    if (match === null || match[0].length > MAX_NUMBER_TOKEN_LENGTH) {
      throw new JsonBoundaryError();
    }
    this.index += match[0].length;
    return new JsonNumberToken(match[0]);
  }

  private parseLiteral<T>(token: string, value: T): T {
    if (this.source.slice(this.index, this.index + token.length) !== token) {
      throw new JsonBoundaryError();
    }
    this.index += token.length;
    return value;
  }

  private skipWhitespace(): void {
    while (
      this.source[this.index] === " " ||
      this.source[this.index] === "\t" ||
      this.source[this.index] === "\r" ||
      this.source[this.index] === "\n"
    ) {
      this.index += 1;
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  required: readonly string[],
  allowed: readonly string[] = required,
): boolean {
  const keys = Object.keys(value);
  return (
    keys.every((key) => allowed.includes(key)) &&
    required.every((key) => Object.hasOwn(value, key))
  );
}

function stringLength(value: string): number {
  return Array.from(value).length;
}

function isString(
  value: unknown,
  minimum: number,
  maximum: number,
  pattern?: RegExp,
): value is string {
  return (
    typeof value === "string" &&
    stringLength(value) >= minimum &&
    stringLength(value) <= maximum &&
    (pattern === undefined || pattern.test(value))
  );
}

function integerValue(
  value: unknown,
  minimum: number,
  maximum: number,
): number | null {
  if (!(value instanceof JsonNumberToken)) {
    return null;
  }
  const match = /^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?)([0-9]+))?$/.exec(
    value.source,
  );
  if (match === null) {
    return null;
  }

  const fraction = match[3] ?? "";
  let digits = `${match[2]}${fraction}`;
  if (/^0+$/.test(digits)) {
    return minimum <= 0 && maximum >= 0 ? 0 : null;
  }

  let exponent = BigInt(match[5] ?? "0");
  if (match[4] === "-") {
    exponent = -exponent;
  }
  const decimalPlaces = BigInt(fraction.length) - exponent;
  if (decimalPlaces > 0n) {
    if (decimalPlaces > BigInt(digits.length)) {
      return null;
    }
    const count = Number(decimalPlaces);
    if (!digits.endsWith("0".repeat(count))) {
      return null;
    }
    digits = digits.slice(0, -count) || "0";
  } else if (decimalPlaces < 0n) {
    const zeroCount = -decimalPlaces;
    const significant = digits.replace(/^0+/, "");
    const largestMagnitude = Math.max(Math.abs(minimum), Math.abs(maximum));
    if (
      BigInt(significant.length) + zeroCount >
      BigInt(String(largestMagnitude).length)
    ) {
      return null;
    }
    digits = significant + "0".repeat(Number(zeroCount));
  }

  digits = digits.replace(/^0+/, "") || "0";
  const integer = BigInt(`${match[1]}${digits}`);
  if (integer < BigInt(minimum) || integer > BigInt(maximum)) {
    return null;
  }
  return Number(integer);
}

function validateReportMetadata(value: unknown): ErrorReport["report"] | null {
  if (!isRecord(value) || !hasExactKeys(value, REPORT_FIELDS)) {
    return null;
  }
  const schemaVersion = integerValue(value.report_schema_version, 1, 1);
  if (
    schemaVersion !== 1 ||
    !isString(value.anonymous_installation_id, 36, 36, UUID_V4_PATTERN) ||
    value.anonymous_installation_id === EMPTY_UUID_V4 ||
    !isString(value.error_fingerprint, 64, 64, SHA256_PATTERN)
  ) {
    return null;
  }
  return {
    report_schema_version: 1,
    anonymous_installation_id: value.anonymous_installation_id,
    error_fingerprint: value.error_fingerprint,
  };
}

function validateApp(value: unknown): ErrorReport["app"] | null {
  if (!isRecord(value) || !hasExactKeys(value, APP_FIELDS)) {
    return null;
  }
  if (
    !isString(value.version, 1, 32, DOTTED_VERSION_PATTERN) ||
    typeof value.package_kind !== "string" ||
    !PACKAGE_KINDS.has(value.package_kind as PackageKind) ||
    !isString(value.ui_language, 1, 16, LANGUAGE_PATTERN)
  ) {
    return null;
  }
  return {
    version: value.version,
    package_kind: value.package_kind as PackageKind,
    ui_language: value.ui_language,
  };
}

function validateSystem(value: unknown): ErrorReport["system"] | null {
  if (!isRecord(value) || !hasExactKeys(value, SYSTEM_FIELDS)) {
    return null;
  }
  const utcOffsetMinutes = integerValue(value.utc_offset_minutes, -840, 840);
  if (
    typeof value.os_family !== "string" ||
    !OS_FAMILIES.has(value.os_family as OsFamily) ||
    !isString(value.os_version, 1, 64, SAFE_OS_VERSION_PATTERN) ||
    typeof value.architecture !== "string" ||
    !ARCHITECTURES.has(value.architecture as Architecture) ||
    !isString(value.locale, 1, 16, LOCALE_PATTERN) ||
    utcOffsetMinutes === null
  ) {
    return null;
  }
  return {
    os_family: value.os_family as OsFamily,
    os_version: value.os_version,
    architecture: value.architecture as Architecture,
    locale: value.locale,
    utc_offset_minutes: utcOffsetMinutes,
  };
}

function validateRuntime(value: unknown): ErrorReport["runtime"] | null {
  if (!isRecord(value) || !hasExactKeys(value, RUNTIME_FIELDS)) {
    return null;
  }
  if (
    !isString(value.python_version, 1, 32, DOTTED_VERSION_PATTERN) ||
    !isString(value.qt_version, 1, 32, DOTTED_VERSION_PATTERN) ||
    !isString(value.rust_core_version, 1, 32, DOTTED_VERSION_PATTERN)
  ) {
    return null;
  }
  return {
    python_version: value.python_version,
    qt_version: value.qt_version,
    rust_core_version: value.rust_core_version,
  };
}

function validateOperation(value: unknown): ErrorReport["operation"] | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, OPERATION_REQUIRED_FIELDS, OPERATION_FIELDS) ||
    typeof value.kind !== "string" ||
    !OPERATION_KINDS.has(value.kind as OperationKind) ||
    typeof value.db_engine !== "string" ||
    !DB_ENGINES.has(value.db_engine as DatabaseEngine) ||
    typeof value.phase !== "string" ||
    !OPERATION_PHASES.has(value.phase as OperationPhase)
  ) {
    return null;
  }
  if (
    Object.hasOwn(value, "db_server_version") &&
    !isString(value.db_server_version, 3, 32, MAJOR_MINOR_PATTERN)
  ) {
    return null;
  }
  const operation: ErrorReport["operation"] = {
    kind: value.kind as OperationKind,
    db_engine: value.db_engine as DatabaseEngine,
    phase: value.phase as OperationPhase,
  };
  if (typeof value.db_server_version === "string") {
    operation.db_server_version = value.db_server_version;
  }
  return operation;
}

function validateFrame(value: unknown): ErrorReport["error"]["app_frames"][number] | null {
  if (!isRecord(value) || !hasExactKeys(value, FRAME_FIELDS)) {
    return null;
  }
  const line = integerValue(value.line, 1, 10_000_000);
  if (
    !isString(value.module, 1, 160, FRAME_MODULE_PATTERN) ||
    !isString(value.function, 1, 128, FRAME_FUNCTION_PATTERN) ||
    line === null
  ) {
    return null;
  }
  return { module: value.module, function: value.function, line };
}

function validateError(value: unknown): ErrorReport["error"] | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ERROR_REQUIRED_FIELDS, ERROR_FIELDS) ||
    !isString(value.exception_class, 1, 128, EXCEPTION_CLASS_PATTERN) ||
    !isString(value.sanitized_message, 0, 2000) ||
    !Array.isArray(value.app_frames) ||
    value.app_frames.length > 20
  ) {
    return null;
  }
  if (
    Object.hasOwn(value, "error_code") &&
    !isString(value.error_code, 1, 64, ERROR_CODE_PATTERN)
  ) {
    return null;
  }
  const frames = [];
  for (const candidate of value.app_frames) {
    const frame = validateFrame(candidate);
    if (frame === null) {
      return null;
    }
    frames.push(frame);
  }
  const error: ErrorReport["error"] = {
    exception_class: value.exception_class,
    sanitized_message: value.sanitized_message,
    app_frames: frames,
  };
  if (typeof value.error_code === "string") {
    error.error_code = value.error_code;
  }
  return error;
}

function validatePayload(value: unknown): ErrorReport | null {
  if (!isRecord(value) || !hasExactKeys(value, TOP_LEVEL_FIELDS)) {
    return null;
  }
  const report = validateReportMetadata(value.report);
  const app = validateApp(value.app);
  const system = validateSystem(value.system);
  const runtime = validateRuntime(value.runtime);
  const operation = validateOperation(value.operation);
  const error = validateError(value.error);
  if (
    report === null ||
    app === null ||
    system === null ||
    runtime === null ||
    operation === null ||
    error === null
  ) {
    return null;
  }
  return { report, app, system, runtime, operation, error };
}

type BodyReadResult =
  | { readonly ok: true; readonly bytes: Uint8Array }
  | { readonly ok: false; readonly code: "payload_too_large" | "invalid_json" };

function contentLengthTooLarge(request: Request): boolean {
  const header = request.headers.get("content-length");
  if (header === null) {
    return false;
  }
  const trimmed = header.trim();
  if (!/^[0-9]+$/.test(trimmed)) {
    return false;
  }
  try {
    return BigInt(trimmed) > BigInt(MAX_REPORT_BYTES);
  } catch {
    return true;
  }
}

async function readBoundedBody(request: Request): Promise<BodyReadResult> {
  if (request.body === null) {
    return { ok: true, bytes: new Uint8Array() };
  }
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const item = await reader.read();
      if (item.done) {
        break;
      }
      if (!(item.value instanceof Uint8Array)) {
        return { ok: false, code: "invalid_json" };
      }
      if (item.value.byteLength > MAX_REPORT_BYTES - total) {
        try {
          await reader.cancel();
        } catch {
          // The fixed oversized response is authoritative even if cancellation fails.
        }
        return { ok: false, code: "payload_too_large" };
      }
      chunks.push(item.value.slice());
      total += item.value.byteLength;
    }
  } catch {
    return { ok: false, code: "invalid_json" };
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { ok: true, bytes };
}

function parseBoundedJson(bytes: Uint8Array): unknown {
  const text = new TextDecoder("utf-8", {
    fatal: true,
    ignoreBOM: false,
  }).decode(bytes);
  return new BoundedJsonParser(text).parse();
}

function equalFingerprint(left: string, right: string): boolean {
  if (left.length !== right.length) {
    return false;
  }
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

export async function parseReport(request: Request): Promise<ParseReportResult> {
  if (request.method !== "POST") {
    return failure("method_not_allowed");
  }
  const contentType = request.headers.get("content-type");
  if (contentType === null || !JSON_CONTENT_TYPE.test(contentType)) {
    return failure("unsupported_media_type");
  }
  if (contentLengthTooLarge(request)) {
    return failure("payload_too_large");
  }

  const body = await readBoundedBody(request);
  if (!body.ok) {
    return failure(body.code);
  }

  let decoded: unknown;
  try {
    decoded = parseBoundedJson(body.bytes);
  } catch {
    return failure("invalid_json");
  }
  const validated = validatePayload(decoded);
  if (validated === null) {
    return failure("invalid_report");
  }

  const canonical = sanitizeReport(validated);
  let serverFingerprint: string;
  try {
    serverFingerprint = await computeFingerprint(canonical);
  } catch {
    return failure("internal_error");
  }
  if (!equalFingerprint(canonical.report.error_fingerprint, serverFingerprint)) {
    return failure("fingerprint_mismatch");
  }
  canonical.report.error_fingerprint = serverFingerprint;
  observability.increment("report_accepted");
  return { ok: true, report: canonical };
}
