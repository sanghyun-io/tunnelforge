# Vendored `mysql` 26.0.1

Unmodified upstream crate (registry sources, `src/` + `Cargo.toml` + `build.rs`) with one
TunnelForge patch for TF-STATUS-110:

- `SslOpts::with_tls_server_name(Option<String>)` / `tls_server_name()` and its use in
  `src/io/tls/native_tls_io.rs`: the certificate is verified against this name instead of
  the connect host. Needed because through an SSH tunnel the connect host is `127.0.0.1`
  while the certificate is issued for the remote DB host. Upstream offers no way to do this.

Grep for `TunnelForge patch`. Drop this directory (and `[patch.crates-io]` in
`migration_core/Cargo.toml`) if upstream gains an equivalent option.
