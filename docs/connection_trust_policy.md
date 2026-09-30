# Connection trust policy (TF-STATUS-110)

TunnelForge verifies who it is talking to on both hops of a connection.

## DB TLS

Each connection profile has a TLS mode (`db_tls_mode`) and an optional extra CA file (`db_tls_ca_file`).

| Mode | Behaviour |
| --- | --- |
| `verify_full` | Encrypts; verifies the certificate chain, expiry and the server name. **Default for new profiles.** |
| `verify_ca` | Encrypts; verifies chain and expiry, not the server name (for certificates issued to a different name). |
| `disable` | No TLS, nothing verified. Legacy behaviour. |

There is intentionally no "encrypt but do not verify" mode.

- Trust store: the operating system store (`native-tls`: Schannel / Security.framework / OpenSSL). `db_tls_ca_file` (PEM or DER, bundles allowed) is trusted **in addition**.
- Through an SSH tunnel the connect host is the local forwarded port, so the certificate is verified against the tunnel's `remote_host` automatically. MySQL uses a vendored `mysql` crate with one small patch for this (`migration_core/vendor/mysql/PATCH.md`); PostgreSQL uses a connector wrapper.
- Profiles saved before this feature have no TLS setting. They keep connecting with `disable` and show a persistent warning (tunnel list marker, profile dialog, status bar on connect). They are never upgraded silently.
- `disable` is accepted without a warning only for a **direct** connection to a loopback target (`localhost`, `127.0.0.0/8`, `::1`). SSH-tunnel profiles are never exempt.
- When TLS verification is requested and fails, the core reports `error_code` `tls_verification_failed`; when the server has no TLS at all, `tls_unavailable`. A verified mode never falls back to plaintext.

## SSH server identity (trust on first use)

- First contact: the SHA-256 fingerprint is shown and the key is stored **only after the user confirms it**. Without a confirmation prompt (scheduler, auto-reconnect) an unknown host is refused (`ssh_host_key_unknown`).
- Known host, same key: connects silently.
- Known host, different key: the connection is blocked (`ssh_host_key_changed`). The only way to replace the key is the explicit **SSH 호스트 키 확인/갱신** button in the tunnel settings, which shows both fingerprints.
- The check covers every SSH path: the paramiko reachability probe (strict `RejectPolicy`) and the `SSHTunnelForwarder`, which is pinned to the verified key.
- Known keys live in the local config under `ssh_known_hosts`. They are not exported and cannot be introduced by importing a config file.

## Encrypted private keys

The passphrase is requested when needed, kept in memory for the running session only, and never written to the config or logs.

## Live verification

`scripts/tls_live_env.sh` builds a disposable Docker environment (`tf-test-a-*`): PostgreSQL and MySQL with hot-swappable certificates (good, wrong name, expired, untrusted CA, no TLS) and an OpenSSH server with rotatable host keys. Opt-in tests:

- `migration_core/tests/live_tls.rs` (env `TF_TLS_TEST_CERT_DIR`)
- `tests/test_trust_live.py` (env `TF_TLS_TEST_KEY_DIR`)

See the header of each file for the exact commands. Remove the environment with `scripts/tls_live_env.sh down`.
