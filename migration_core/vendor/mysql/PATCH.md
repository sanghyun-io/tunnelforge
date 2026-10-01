# Vendored `mysql` 26.0.1

Upstream crate (registry sources, `src/` + `Cargo.toml` + `build.rs`) plus two small
TunnelForge patches. Every patched spot carries a `TunnelForge patch` comment
(`grep -rn "TunnelForge patch" migration_core/vendor/mysql`). `migration_core/Cargo.toml` points
`[patch.crates-io] mysql` at this directory.

## 1. TLS server name (TF-STATUS-110)

- **What:** `SslOpts::with_tls_server_name(Option<String>)` / `tls_server_name()`, used in
  `src/io/tls/native_tls_io.rs`: the certificate is verified against this name instead of the
  connect host.
- **Why:** through an SSH tunnel the connect host is `127.0.0.1` while the certificate is issued
  for the remote DB host; upstream offers no way to separate the two.
- **Remove when:** upstream gains an equivalent option.

## 2. Server transaction flag (TF-STATUS-131)

- **What:** `Conn::server_in_transaction()` in `src/conn/mod.rs` (next to `no_backslash_escape`):
  true when the last OK/EOF packet carried `SERVER_STATUS_IN_TRANS` (or the read-only variant).
- **Why:** MySQL has no `@@in_transaction`; the status flag is the only authoritative answer to
  "does this session have an open transaction". Upstream keeps `status_flags` private. The core
  reports it as `in_transaction` in `query.execute` results. An error/KILL reply clears the flags,
  so the core first runs `DO 0` (no table access, returns an OK packet) when it needs the value
  after a cancel or error.
- **Remove when:** upstream exposes the status flags (for example a public `Conn::status_flags()`),
  then call that instead.

If both are gone, drop this directory and the `[patch.crates-io]` entry.
