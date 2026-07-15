export const EDGE_IP_BURST_LIMIT = 3;
export const EDGE_IP_BURST_SECONDS = 10;
export const EDGE_IP_MINUTE_LIMIT = 10;
export const EDGE_INSTALLATION_MINUTE_LIMIT = 3;
export const EDGE_RATE_LIMITER_MAX_KEYS = 4_096;
export const INSTALLATION_HOURLY_LIMIT = 10;

export const GLOBAL_WRITE_LIMITS = Object.freeze({
  create: Object.freeze({ hour: 5, day: 20 }),
  comment: Object.freeze({ hour: 20, day: 100 }),
});

export type GlobalWriteKind = keyof typeof GLOBAL_WRITE_LIMITS;
export type EdgeLimitReason =
  | "ip_burst"
  | "ip_minute"
  | "installation_minute"
  | "edge_capacity";

export type EdgeLimitResult =
  | { readonly allowed: true }
  | { readonly allowed: false; readonly reason: EdgeLimitReason };

export interface EdgeRateLimiter {
  checkIp(ipKey: string, nowSeconds: number): EdgeLimitResult;
  checkInstallation(
    installationHmac: string,
    nowSeconds: number,
  ): EdgeLimitResult;
}

export interface EdgeRateLimiterOptions {
  readonly maxKeys?: number;
  readonly onCapacitySweep?: (kind: "ip" | "installation") => void;
}

export type EdgeKeyNamespace = "ip" | "installation";
export type EdgeKeyDeriver = (
  namespace: EdgeKeyNamespace,
  value: string,
) => Promise<string>;

interface BudgetRequest {
  readonly bucket: string;
  readonly kind: "installation" | GlobalWriteKind;
  readonly hardLimit: number;
  readonly expiresAt: number;
}

type BudgetEligibility =
  | {
      readonly kind: "lease";
      readonly fingerprint: string;
      readonly leaseToken: string;
      readonly nowSeconds: number;
    }
  | {
      readonly kind: "route";
      readonly fingerprint: string;
      readonly issueNumber: number;
    };

const MINUTE_SECONDS = 60;
const HOUR_SECONDS = 60 * MINUTE_SECONDS;
const DAY_SECONDS = 24 * HOUR_SECONDS;
const SHA256_HEX = /^[0-9a-f]{64}$/;

function requireEpochSeconds(value: number): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError("invalid quota timestamp");
  }
}

function windowIndex(nowSeconds: number, sizeSeconds: number): number {
  return Math.floor(nowSeconds / sizeSeconds);
}

function windowEnd(index: number, sizeSeconds: number): number {
  return (index + 1) * sizeSeconds;
}

function pruneWindow(
  entries: readonly number[] | undefined,
  nowSeconds: number,
  windowSeconds: number,
): number[] {
  if (entries === undefined) {
    return [];
  }
  const threshold = nowSeconds - windowSeconds;
  return entries.filter((timestamp) => timestamp > threshold);
}

function pruneMap(
  store: WindowStore,
  nowSeconds: number,
  windowSeconds: number,
): void {
  let nextExpiry = Number.POSITIVE_INFINITY;
  for (const [key, timestamps] of store.entries) {
    const retained = pruneWindow(timestamps, nowSeconds, windowSeconds);
    if (retained.length === 0) {
      store.entries.delete(key);
    } else {
      store.entries.set(key, retained);
      nextExpiry = Math.min(nextExpiry, retained[0] + windowSeconds);
    }
  }
  store.nextExpiry = nextExpiry;
}

interface WindowStore {
  readonly entries: Map<string, number[]>;
  nextExpiry: number;
}

function currentWindow(
  store: WindowStore,
  key: string,
  nowSeconds: number,
  windowSeconds: number,
): number[] {
  const retained = pruneWindow(
    store.entries.get(key),
    nowSeconds,
    windowSeconds,
  );
  if (retained.length === 0) {
    store.entries.delete(key);
  } else {
    store.entries.set(key, retained);
  }
  return retained;
}

function canTrackKey(
  store: WindowStore,
  key: string,
  nowSeconds: number,
  windowSeconds: number,
  maxKeys: number,
  onSweep: () => void,
): boolean {
  if (store.entries.has(key) || store.entries.size < maxKeys) {
    return true;
  }
  if (nowSeconds < store.nextExpiry) {
    return false;
  }
  pruneMap(store, nowSeconds, windowSeconds);
  onSweep();
  return store.entries.size < maxKeys;
}

function recordWindow(
  store: WindowStore,
  key: string,
  timestamps: number[],
  nowSeconds: number,
  windowSeconds: number,
): void {
  timestamps.push(nowSeconds);
  store.entries.set(key, timestamps);
  store.nextExpiry = Math.min(store.nextExpiry, nowSeconds + windowSeconds);
}

export function createEdgeRateLimiter(
  options: EdgeRateLimiterOptions = {},
): EdgeRateLimiter {
  const maxKeys = options.maxKeys ?? EDGE_RATE_LIMITER_MAX_KEYS;
  if (!Number.isSafeInteger(maxKeys) || maxKeys <= 0) {
    throw new TypeError("invalid edge limiter capacity");
  }
  const ipRequests: WindowStore = {
    entries: new Map<string, number[]>(),
    nextExpiry: Number.POSITIVE_INFINITY,
  };
  const installationRequests: WindowStore = {
    entries: new Map<string, number[]>(),
    nextExpiry: Number.POSITIVE_INFINITY,
  };

  return {
    checkIp(ipKey: string, nowSeconds: number): EdgeLimitResult {
      requireEpochSeconds(nowSeconds);
      const minute = currentWindow(
        ipRequests,
        ipKey,
        nowSeconds,
        MINUTE_SECONDS,
      );
      if (
        !canTrackKey(
          ipRequests,
          ipKey,
          nowSeconds,
          MINUTE_SECONDS,
          maxKeys,
          () => options.onCapacitySweep?.("ip"),
        )
      ) {
        return { allowed: false, reason: "edge_capacity" };
      }
      const burstCount = minute.filter(
        (timestamp) => timestamp > nowSeconds - EDGE_IP_BURST_SECONDS,
      ).length;
      if (burstCount >= EDGE_IP_BURST_LIMIT) {
        return { allowed: false, reason: "ip_burst" };
      }
      if (minute.length >= EDGE_IP_MINUTE_LIMIT) {
        return { allowed: false, reason: "ip_minute" };
      }
      recordWindow(ipRequests, ipKey, minute, nowSeconds, MINUTE_SECONDS);
      return { allowed: true };
    },

    checkInstallation(
      installationHmac: string,
      nowSeconds: number,
    ): EdgeLimitResult {
      requireEpochSeconds(nowSeconds);
      if (!SHA256_HEX.test(installationHmac)) {
        throw new TypeError("invalid installation digest");
      }
      const minute = currentWindow(
        installationRequests,
        installationHmac,
        nowSeconds,
        MINUTE_SECONDS,
      );
      if (
        !canTrackKey(
          installationRequests,
          installationHmac,
          nowSeconds,
          MINUTE_SECONDS,
          maxKeys,
          () => options.onCapacitySweep?.("installation"),
        )
      ) {
        return { allowed: false, reason: "edge_capacity" };
      }
      if (minute.length >= EDGE_INSTALLATION_MINUTE_LIMIT) {
        return { allowed: false, reason: "installation_minute" };
      }
      recordWindow(
        installationRequests,
        installationHmac,
        minute,
        nowSeconds,
        MINUTE_SECONDS,
      );
      return { allowed: true };
    },
  };
}

function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

export function createProcessLocalEdgeKeyDeriver(): EdgeKeyDeriver {
  const encoder = new TextEncoder();
  let key: Promise<CryptoKey> | undefined;

  return async (namespace, value) => {
    key ??= crypto.subtle.importKey(
      "raw",
      crypto.getRandomValues(new Uint8Array(32)),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const signature = await crypto.subtle.sign(
      "HMAC",
      await key,
      encoder.encode(`${namespace}\u0000${value}`),
    );
    return toHex(new Uint8Array(signature));
  };
}

export async function hmacInstallationId(
  installationId: string,
  secret: string,
): Promise<string> {
  if (secret.length === 0) {
    throw new TypeError("missing installation HMAC key");
  }
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, encoder.encode(installationId)),
  );
  return toHex(signature);
}

export async function consumeInstallationHourlyQuota(
  db: D1Database,
  installationHmac: string,
  nowSeconds: number,
): Promise<boolean> {
  if (!SHA256_HEX.test(installationHmac)) {
    throw new TypeError("invalid installation digest");
  }
  requireEpochSeconds(nowSeconds);
  const hour = windowIndex(nowSeconds, HOUR_SECONDS);
  return consumeAtomicBudgets(db, [
    {
      bucket: `installation:${installationHmac}:hour:${hour}`,
      kind: "installation",
      hardLimit: INSTALLATION_HOURLY_LIMIT,
      expiresAt: windowEnd(hour, HOUR_SECONDS),
    },
  ]);
}

export async function consumeGlobalWriteBudget(
  db: D1Database,
  kind: GlobalWriteKind,
  nowSeconds: number,
): Promise<boolean> {
  requireEpochSeconds(nowSeconds);
  const hour = windowIndex(nowSeconds, HOUR_SECONDS);
  const day = windowIndex(nowSeconds, DAY_SECONDS);
  const limits = GLOBAL_WRITE_LIMITS[kind];
  return consumeAtomicBudgets(db, [
    {
      bucket: `global:hour:${hour}`,
      kind,
      hardLimit: limits.hour,
      expiresAt: windowEnd(hour, HOUR_SECONDS),
    },
    {
      bucket: `global:day:${day}`,
      kind,
      hardLimit: limits.day,
      expiresAt: windowEnd(day, DAY_SECONDS),
    },
  ]);
}

export async function consumeGlobalWriteBudgetForCurrentLease(
  db: D1Database,
  kind: GlobalWriteKind,
  fingerprint: string,
  leaseToken: string,
  nowSeconds: number,
): Promise<boolean> {
  if (!SHA256_HEX.test(fingerprint)) {
    throw new TypeError("invalid route fingerprint");
  }
  if (leaseToken.length === 0 || leaseToken.length > 128) {
    throw new TypeError("invalid route lease token");
  }
  requireEpochSeconds(nowSeconds);
  const hour = windowIndex(nowSeconds, HOUR_SECONDS);
  const day = windowIndex(nowSeconds, DAY_SECONDS);
  const limits = GLOBAL_WRITE_LIMITS[kind];
  return consumeAtomicBudgets(
    db,
    [
      {
        bucket: `global:hour:${hour}`,
        kind,
        hardLimit: limits.hour,
        expiresAt: windowEnd(hour, HOUR_SECONDS),
      },
      {
        bucket: `global:day:${day}`,
        kind,
        hardLimit: limits.day,
        expiresAt: windowEnd(day, DAY_SECONDS),
      },
    ],
    {
      kind: "lease",
      fingerprint,
      leaseToken,
      nowSeconds,
    },
  );
}

export async function consumeGlobalWriteBudgetForCurrentRoute(
  db: D1Database,
  kind: GlobalWriteKind,
  fingerprint: string,
  issueNumber: number,
  nowSeconds: number,
): Promise<boolean> {
  if (!SHA256_HEX.test(fingerprint)) {
    throw new TypeError("invalid route fingerprint");
  }
  if (!Number.isSafeInteger(issueNumber) || issueNumber <= 0) {
    throw new TypeError("invalid route issue number");
  }
  requireEpochSeconds(nowSeconds);
  const hour = windowIndex(nowSeconds, HOUR_SECONDS);
  const day = windowIndex(nowSeconds, DAY_SECONDS);
  const limits = GLOBAL_WRITE_LIMITS[kind];
  return consumeAtomicBudgets(
    db,
    [
      {
        bucket: `global:hour:${hour}`,
        kind,
        hardLimit: limits.hour,
        expiresAt: windowEnd(hour, HOUR_SECONDS),
      },
      {
        bucket: `global:day:${day}`,
        kind,
        hardLimit: limits.day,
        expiresAt: windowEnd(day, DAY_SECONDS),
      },
    ],
    { kind: "route", fingerprint, issueNumber },
  );
}

async function consumeAtomicBudgets(
  db: D1Database,
  requests: readonly BudgetRequest[],
  eligibility?: BudgetEligibility,
): Promise<boolean> {
  if (requests.length === 0) {
    return true;
  }
  const values = requests.map(() => "(?, ?, ?, ?)").join(", ");
  const bindings = requests.flatMap((request) => [
    request.bucket,
    request.kind,
    request.hardLimit,
    request.expiresAt,
  ]);
  const routeEligibility =
    eligibility?.kind === "lease"
      ? `AND EXISTS (
             SELECT 1
             FROM issue_routes
             WHERE fingerprint = ?
               AND state = 'pending'
               AND lease_token = ?
               AND lease_until > ?
           )`
      : eligibility?.kind === "route"
        ? `AND EXISTS (
             SELECT 1
             FROM issue_routes
             WHERE fingerprint = ?
               AND state = 'ready'
               AND issue_number = ?
           )`
        : "";
  const routeBindings =
    eligibility?.kind === "lease"
      ? [
          eligibility.fingerprint,
          eligibility.leaseToken,
          eligibility.nowSeconds,
        ]
      : eligibility?.kind === "route"
        ? [eligibility.fingerprint, eligibility.issueNumber]
        : [];
  const result = await db
    .prepare(
      `WITH requested(bucket, kind, hard_limit, expires_at) AS (
         VALUES ${values}
       ), eligibility(allowed) AS (
         SELECT CASE
           WHEN COUNT(*) = SUM(
             CASE
               WHEN COALESCE(existing.used, 0) < requested.hard_limit THEN 1
               ELSE 0
             END
            ) ${routeEligibility} THEN 1
           ELSE 0
         END
         FROM requested
         LEFT JOIN write_budgets AS existing
           ON existing.bucket = requested.bucket
          AND existing.kind = requested.kind
       )
       INSERT INTO write_budgets (bucket, kind, used, hard_limit, expires_at)
       SELECT requested.bucket, requested.kind, 1,
              requested.hard_limit, requested.expires_at
       FROM requested
       CROSS JOIN eligibility
       WHERE eligibility.allowed = 1
       ON CONFLICT(bucket, kind) DO UPDATE SET
         used = write_budgets.used + 1,
         hard_limit = excluded.hard_limit,
         expires_at = excluded.expires_at
       RETURNING bucket`,
    )
    .bind(...bindings, ...routeBindings)
    .all<{ bucket: string }>();
  return result.results.length === requests.length;
}
