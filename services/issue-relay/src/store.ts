export const ISSUE_LEASE_SECONDS = 60;
export const REPORT_ACTION_TTL_SECONDS = 24 * 60 * 60;
export const RELAY_CLEANUP_BATCH_SIZE = 100;

export type ReportActionKind = "create" | "comment";
export type ReportActionState = "pending" | "complete" | "failed" | "unknown";

export type IssueLeaseResult =
  | { readonly status: "acquired"; readonly leaseToken: string }
  | { readonly status: "pending" }
  | { readonly status: "ready"; readonly issueNumber: number }
  | { readonly status: "unknown" };

export type ReportActionClaim =
  | { readonly status: "acquired"; readonly actionWindow: number }
  | { readonly status: ReportActionState };

interface IssueRouteRow {
  readonly issue_number: number | null;
  readonly state: "pending" | "ready" | "failed" | "unknown";
  readonly lease_token: string | null;
}

interface CurrentLeaseRow {
  readonly current: number;
}

interface ReportActionRow {
  readonly state: ReportActionState;
  readonly window: number;
}

interface CountRow {
  readonly total: number;
}

const SHA256_HEX = /^[0-9a-f]{64}$/;

function requireDigest(value: string): void {
  if (!SHA256_HEX.test(value)) {
    throw new TypeError("invalid relay digest");
  }
}

function requireEpochSeconds(value: number): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError("invalid relay timestamp");
  }
}

function changed(result: D1Result): boolean {
  return result.meta.changes === 1;
}

export class RelayStore {
  constructor(private readonly db: D1Database) {}

  async acquireIssueLease(
    fingerprint: string,
    nowSeconds: number,
  ): Promise<IssueLeaseResult> {
    requireDigest(fingerprint);
    requireEpochSeconds(nowSeconds);

    const leaseToken = crypto.randomUUID();
    const leaseUntil = nowSeconds + ISSUE_LEASE_SECONDS;
    const acquired = await this.db
      .prepare(
        `INSERT INTO issue_routes (
           fingerprint, issue_number, state, lease_token, lease_until
         ) VALUES (?, NULL, 'pending', ?, ?)
         ON CONFLICT(fingerprint) DO UPDATE SET
           issue_number = NULL,
           state = CASE
             WHEN issue_routes.state = 'failed' THEN 'pending'
             ELSE 'unknown'
           END,
           lease_token = CASE
             WHEN issue_routes.state = 'failed' THEN excluded.lease_token
             ELSE NULL
           END,
           lease_until = CASE
             WHEN issue_routes.state = 'failed' THEN excluded.lease_until
             ELSE NULL
           END
         WHERE issue_routes.state = 'failed'
            OR (
              issue_routes.state = 'pending'
              AND issue_routes.lease_until <= ?
            )
         RETURNING issue_number, state, lease_token`,
      )
      .bind(fingerprint, leaseToken, leaseUntil, nowSeconds)
      .first<IssueRouteRow>();

    if (acquired?.state === "pending" && acquired.lease_token === leaseToken) {
      return { status: "acquired", leaseToken };
    }

    const current = await this.db
      .prepare(
        `SELECT issue_number, state, lease_token
         FROM issue_routes
         WHERE fingerprint = ?`,
      )
      .bind(fingerprint)
      .first<IssueRouteRow>();
    if (current === null) {
      throw new Error("relay route disappeared");
    }
    if (current.state === "ready" && current.issue_number !== null) {
      return { status: "ready", issueNumber: current.issue_number };
    }
    if (current.state === "unknown") {
      return { status: "unknown" };
    }
    if (current.state === "pending") {
      return { status: "pending" };
    }
    throw new Error("relay route transition failed");
  }

  async isIssueLeaseCurrent(
    fingerprint: string,
    leaseToken: string,
    nowSeconds: number,
  ): Promise<boolean> {
    requireDigest(fingerprint);
    requireEpochSeconds(nowSeconds);
    const row = await this.db
      .prepare(
        `SELECT 1 AS current
         FROM issue_routes
         WHERE fingerprint = ?
           AND state = 'pending'
           AND lease_token = ?
           AND lease_until > ?`,
      )
      .bind(fingerprint, leaseToken, nowSeconds)
      .first<CurrentLeaseRow>();
    return row?.current === 1;
  }

  async isIssueRouteCurrent(
    fingerprint: string,
    issueNumber: number,
  ): Promise<boolean> {
    requireDigest(fingerprint);
    if (!Number.isSafeInteger(issueNumber) || issueNumber <= 0) {
      throw new TypeError("invalid issue number");
    }
    const row = await this.db
      .prepare(
        `SELECT 1 AS current
         FROM issue_routes
         WHERE fingerprint = ?
           AND state = 'ready'
           AND issue_number = ?`,
      )
      .bind(fingerprint, issueNumber)
      .first<CurrentLeaseRow>();
    return row?.current === 1;
  }

  async isIssueRoutePending(
    fingerprint: string,
    nowSeconds: number,
  ): Promise<boolean> {
    requireDigest(fingerprint);
    requireEpochSeconds(nowSeconds);
    const row = await this.db
      .prepare(
        `SELECT 1 AS current
         FROM issue_routes
         WHERE fingerprint = ?
           AND state = 'pending'
           AND lease_until > ?`,
      )
      .bind(fingerprint, nowSeconds)
      .first<CurrentLeaseRow>();
    return row?.current === 1;
  }

  async acquireIssueRecoveryLease(
    fingerprint: string,
    expectedIssueNumber: number,
    nowSeconds: number,
  ): Promise<IssueLeaseResult> {
    requireDigest(fingerprint);
    requireEpochSeconds(nowSeconds);
    if (
      !Number.isSafeInteger(expectedIssueNumber) ||
      expectedIssueNumber <= 0
    ) {
      throw new TypeError("invalid issue number");
    }

    const leaseToken = crypto.randomUUID();
    const acquired = await this.db
      .prepare(
        `UPDATE issue_routes
         SET issue_number = NULL,
             state = 'pending',
             lease_token = ?,
             lease_until = ?
         WHERE fingerprint = ?
           AND state = 'ready'
           AND issue_number = ?
         RETURNING issue_number, state, lease_token`,
      )
      .bind(
        leaseToken,
        nowSeconds + ISSUE_LEASE_SECONDS,
        fingerprint,
        expectedIssueNumber,
      )
      .first<IssueRouteRow>();
    if (acquired?.state === "pending" && acquired.lease_token === leaseToken) {
      return { status: "acquired", leaseToken };
    }
    return this.acquireIssueLease(fingerprint, nowSeconds);
  }

  async markIssueReady(
    fingerprint: string,
    leaseToken: string,
    issueNumber: number,
  ): Promise<boolean> {
    requireDigest(fingerprint);
    if (!Number.isSafeInteger(issueNumber) || issueNumber <= 0) {
      throw new TypeError("invalid issue number");
    }
    const result = await this.db
      .prepare(
        `UPDATE issue_routes
         SET issue_number = ?, state = 'ready', lease_token = NULL, lease_until = NULL
         WHERE fingerprint = ? AND state = 'pending' AND lease_token = ?`,
      )
      .bind(issueNumber, fingerprint, leaseToken)
      .run();
    return changed(result);
  }

  async finalizeCreatedIssue(
    installationHmac: string,
    fingerprint: string,
    actionWindow: number,
    leaseToken: string,
    issueNumber: number,
  ): Promise<boolean> {
    requireDigest(installationHmac);
    requireDigest(fingerprint);
    requireEpochSeconds(actionWindow);
    if (leaseToken.length === 0 || leaseToken.length > 128) {
      throw new TypeError("invalid issue lease token");
    }
    if (!Number.isSafeInteger(issueNumber) || issueNumber <= 0) {
      throw new TypeError("invalid issue number");
    }

    const results = await this.db.batch([
      this.db
        .prepare(
          `UPDATE issue_routes
           SET issue_number = ?,
               state = 'ready',
               lease_token = NULL,
               lease_until = NULL
           WHERE fingerprint = ?
             AND state = 'pending'
             AND lease_token = ?
             AND EXISTS (
               SELECT 1
               FROM report_actions
               WHERE installation_hmac = ?
                 AND fingerprint = ?
                 AND window = ?
                 AND kind = 'create'
                 AND state = 'pending'
                 AND route_lease_token = ?
             )`,
        )
        .bind(
          issueNumber,
          fingerprint,
          leaseToken,
          installationHmac,
          fingerprint,
          actionWindow,
          leaseToken,
        ),
      this.db
        .prepare(
          `UPDATE report_actions
           SET state = ?
           WHERE installation_hmac = ?
             AND fingerprint = ?
             AND window = ?
             AND kind = 'create'
             AND state = 'pending'
             AND route_lease_token = ?
             AND EXISTS (
               SELECT 1
               FROM issue_routes
               WHERE fingerprint = ?
                 AND state = 'ready'
                 AND issue_number = ?
             )`,
        )
        .bind(
          "complete",
          installationHmac,
          fingerprint,
          actionWindow,
          leaseToken,
          fingerprint,
          issueNumber,
        ),
    ]);
    return results.length === 2 && results.every(changed);
  }

  async markIssueFailed(
    fingerprint: string,
    leaseToken: string,
  ): Promise<boolean> {
    return this.finishIssueLease(fingerprint, leaseToken, "failed");
  }

  async markIssueUnknown(
    fingerprint: string,
    leaseToken: string,
  ): Promise<boolean> {
    return this.finishIssueLease(fingerprint, leaseToken, "unknown");
  }

  async markReadyIssueUnknown(
    fingerprint: string,
    expectedIssueNumber: number,
  ): Promise<boolean> {
    requireDigest(fingerprint);
    if (
      !Number.isSafeInteger(expectedIssueNumber) ||
      expectedIssueNumber <= 0
    ) {
      throw new TypeError("invalid issue number");
    }
    const result = await this.db
      .prepare(
        `UPDATE issue_routes
         SET issue_number = NULL,
             state = 'unknown',
             lease_token = NULL,
             lease_until = NULL
         WHERE fingerprint = ?
           AND state = 'ready'
           AND issue_number = ?`,
      )
      .bind(fingerprint, expectedIssueNumber)
      .run();
    return changed(result);
  }

  async claimReportAction(
    installationHmac: string,
    fingerprint: string,
    kind: ReportActionKind,
    nowSeconds: number,
    issueLeaseToken?: string,
  ): Promise<ReportActionClaim> {
    requireDigest(installationHmac);
    requireDigest(fingerprint);
    requireEpochSeconds(nowSeconds);
    const expiresAt = nowSeconds + REPORT_ACTION_TTL_SECONDS;
    const routeLeaseToken = kind === "create" ? (issueLeaseToken ?? "") : null;

    const acquired = await this.db
      .prepare(
        `WITH candidate(action_window) AS (
           SELECT max(?, coalesce(max(window) + 1, ?))
           FROM report_actions
           WHERE installation_hmac = ? AND fingerprint = ?
         )
         INSERT OR IGNORE INTO report_actions (
           installation_hmac, fingerprint, window, kind, state, expires_at,
           route_lease_token
         )
         SELECT ?, ?, candidate.action_window, ?, 'pending', ?, ?
         FROM candidate
         WHERE NOT EXISTS (
           SELECT 1
           FROM report_actions
           WHERE installation_hmac = ?
             AND fingerprint = ?
             AND expires_at > ?
             AND state <> 'failed'
             AND (
               ? = 'comment'
               OR state IN ('pending', 'unknown')
               OR route_lease_token = ?
             )
         )
           AND (
             ? = 'comment'
             OR EXISTS (
               SELECT 1
               FROM issue_routes
               WHERE fingerprint = ?
                 AND state = 'pending'
                 AND lease_token = ?
                 AND lease_until > ?
             )
           )
         RETURNING state, window`,
      )
      .bind(
        nowSeconds,
        nowSeconds,
        installationHmac,
        fingerprint,
        installationHmac,
        fingerprint,
        kind,
        expiresAt,
        routeLeaseToken,
        installationHmac,
        fingerprint,
        nowSeconds,
        kind,
        routeLeaseToken,
        kind,
        fingerprint,
        routeLeaseToken,
        nowSeconds,
      )
      .first<ReportActionRow>();
    if (acquired?.state === "pending") {
      return { status: "acquired", actionWindow: acquired.window };
    }

    const current = await this.db
      .prepare(
        `SELECT state, window
         FROM report_actions
         WHERE expires_at > ?
           AND state <> 'failed'
           AND (
             (
               ? = 'comment'
               AND installation_hmac = ?
               AND fingerprint = ?
             )
             OR (
               ? = 'create'
               AND (
                 (
                   installation_hmac = ?
                   AND fingerprint = ?
                   AND state IN ('pending', 'unknown')
                 )
                 OR (
                   fingerprint = ?
                   AND kind = 'create'
                   AND route_lease_token = ?
                 )
               )
             )
           )
         ORDER BY CASE state
           WHEN 'unknown' THEN 0
           WHEN 'pending' THEN 1
           ELSE 2
         END
         LIMIT 1`,
      )
      .bind(
        nowSeconds,
        kind,
        installationHmac,
        fingerprint,
        kind,
        installationHmac,
        fingerprint,
        fingerprint,
        routeLeaseToken,
      )
      .first<ReportActionRow>();
    if (current === null) {
      if (kind === "create") {
        return { status: "unknown" };
      }
      throw new Error("relay action disappeared");
    }
    return { status: current.state };
  }

  async markReportAction(
    installationHmac: string,
    fingerprint: string,
    kind: ReportActionKind,
    actionWindow: number,
    state: Exclude<ReportActionState, "pending">,
  ): Promise<boolean> {
    requireDigest(installationHmac);
    requireDigest(fingerprint);
    requireEpochSeconds(actionWindow);
    const result = await this.db
      .prepare(
        `UPDATE report_actions
         SET state = ?
         WHERE installation_hmac = ?
           AND fingerprint = ?
           AND window = ?
           AND kind = ?
           AND state = 'pending'`,
      )
      .bind(
        state,
        installationHmac,
        fingerprint,
        actionWindow,
        kind,
      )
      .run();
    return changed(result);
  }

  async countCompletedReportActions(
    fingerprint: string,
    nowSeconds: number,
  ): Promise<number> {
    requireDigest(fingerprint);
    requireEpochSeconds(nowSeconds);
    const row = await this.db
      .prepare(
        `SELECT COUNT(*) AS total
         FROM report_actions
         WHERE fingerprint = ?
           AND state = 'complete'
           AND expires_at > ?`,
      )
      .bind(fingerprint, nowSeconds)
      .first<CountRow>();
    const total = row?.total ?? 0;
    if (!Number.isSafeInteger(total) || total < 0) {
      throw new Error("invalid relay action count");
    }
    return Math.min(9_999, total);
  }

  private async finishIssueLease(
    fingerprint: string,
    leaseToken: string,
    state: "failed" | "unknown",
  ): Promise<boolean> {
    requireDigest(fingerprint);
    const result = await this.db
      .prepare(
        `UPDATE issue_routes
         SET issue_number = NULL, state = ?, lease_token = NULL, lease_until = NULL
         WHERE fingerprint = ?
           AND state = 'pending'
           AND lease_token = ?
           AND (
             ? = 'unknown'
             OR NOT EXISTS (
               SELECT 1
               FROM report_actions
               WHERE fingerprint = ?
                 AND kind = 'create'
                 AND state <> 'failed'
                 AND route_lease_token = ?
             )
           )`,
      )
      .bind(state, fingerprint, leaseToken, state, fingerprint, leaseToken)
      .run();
    return changed(result);
  }
}

export async function cleanupExpiredRelayState(
  db: D1Database,
  nowSeconds: number,
): Promise<{
  readonly quarantinedRoutes: number;
  readonly deletedSafeRoutes: number;
  readonly reportActions: number;
  readonly writeBudgets: number;
}> {
  requireEpochSeconds(nowSeconds);
  const results = await db.batch([
    db
      .prepare(
        `UPDATE issue_routes
         SET issue_number = NULL,
             state = 'unknown',
             lease_token = NULL,
             lease_until = NULL
         WHERE rowid IN (
           SELECT rowid
           FROM issue_routes
           WHERE state = 'pending' AND lease_until <= ?
           ORDER BY lease_until, rowid
           LIMIT ?
         )`,
      )
      .bind(nowSeconds, RELAY_CLEANUP_BATCH_SIZE),
    db
      .prepare(
        `DELETE FROM issue_routes
         WHERE rowid IN (
           SELECT rowid
           FROM issue_routes
           WHERE state = 'failed'
           ORDER BY rowid
           LIMIT ?
         )`,
      )
      .bind(RELAY_CLEANUP_BATCH_SIZE),
    db
      .prepare(
        `DELETE FROM report_actions
         WHERE rowid IN (
           SELECT action.rowid
           FROM report_actions AS action
           WHERE action.expires_at <= ?
             AND (
               action.kind <> 'create'
               OR action.state = 'failed'
               OR NOT EXISTS (
                 SELECT 1
                 FROM issue_routes AS route
                 WHERE route.state = 'pending'
                   AND route.lease_token = action.route_lease_token
               )
             )
           ORDER BY action.expires_at, action.rowid
           LIMIT ?
         )`,
      )
      .bind(nowSeconds, RELAY_CLEANUP_BATCH_SIZE),
    db
      .prepare(
        `DELETE FROM write_budgets
         WHERE rowid IN (
           SELECT rowid
           FROM write_budgets
           WHERE expires_at <= ?
           ORDER BY expires_at, rowid
           LIMIT ?
         )`,
      )
      .bind(nowSeconds, RELAY_CLEANUP_BATCH_SIZE),
  ]);
  return {
    quarantinedRoutes: results[0]?.meta.changes ?? 0,
    deletedSafeRoutes: results[1]?.meta.changes ?? 0,
    reportActions: results[2]?.meta.changes ?? 0,
    writeBudgets: results[3]?.meta.changes ?? 0,
  };
}
