CREATE TABLE issue_routes (
  fingerprint TEXT PRIMARY KEY NOT NULL,
  issue_number INTEGER,
  state TEXT NOT NULL CHECK (state IN ('pending', 'ready', 'failed', 'unknown')),
  lease_token TEXT,
  lease_until INTEGER,
  CHECK (issue_number IS NULL OR issue_number > 0),
  CHECK (
    (state = 'pending' AND issue_number IS NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
    OR (state = 'ready' AND issue_number IS NOT NULL AND lease_token IS NULL AND lease_until IS NULL)
    OR (state IN ('failed', 'unknown') AND issue_number IS NULL AND lease_token IS NULL AND lease_until IS NULL)
  )
);

CREATE INDEX issue_routes_expired_pending
ON issue_routes(state, lease_until);

CREATE TABLE report_actions (
  installation_hmac TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  window INTEGER NOT NULL CHECK (window > 0),
  kind TEXT NOT NULL CHECK (kind IN ('create', 'comment')),
  state TEXT NOT NULL CHECK (state IN ('pending', 'complete', 'failed', 'unknown')),
  expires_at INTEGER NOT NULL,
  route_lease_token TEXT,
  CHECK (
    (kind = 'create' AND route_lease_token IS NOT NULL)
    OR (kind = 'comment' AND route_lease_token IS NULL)
  ),
  PRIMARY KEY (installation_hmac, fingerprint, window, kind)
);

CREATE INDEX report_actions_expiry
  ON report_actions (expires_at);

CREATE UNIQUE INDEX report_actions_create_generation
  ON report_actions (route_lease_token)
  WHERE kind = 'create';

CREATE TABLE write_budgets (
  bucket TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('installation', 'create', 'comment')),
  used INTEGER NOT NULL CHECK (used >= 0),
  hard_limit INTEGER NOT NULL CHECK (hard_limit > 0 AND used <= hard_limit),
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (bucket, kind)
);

CREATE INDEX write_budgets_expiry
  ON write_budgets (expires_at);
