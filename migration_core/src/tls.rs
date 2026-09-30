//! TLS policy for DB connections (TF-STATUS-110).
//!
//! Only `adapters::{connect_postgres, cancel_postgres_query, mysql_opts}` call into
//! this module; the rest of the core never builds a TLS connector itself.

use crate::{Endpoint, TlsMode};
use native_tls::{Certificate, TlsConnector};
use postgres::tls::MakeTlsConnect;
use postgres::Socket;
use postgres_native_tls::MakeTlsConnector;
use std::fmt;

pub const TLS_VERIFICATION_FAILED: &str = "tls_verification_failed";
pub const TLS_UNAVAILABLE: &str = "tls_unavailable";

/// Connection failure that may carry a stable `error_code` (see contracts §1).
/// The code is appended to the message as `(error_code=...)` so it survives the
/// `String` error plumbing and can be recovered with [`error_code_of`].
#[derive(Debug)]
pub(crate) struct ConnectError {
    pub code: Option<&'static str>,
    detail: String,
}

impl ConnectError {
    pub(crate) fn new(code: Option<&'static str>, detail: impl Into<String>) -> Self {
        Self { code, detail: detail.into() }
    }
}

impl fmt::Display for ConnectError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.code {
            Some(code) => write!(f, "{} (error_code={code})", self.detail),
            None => f.write_str(&self.detail),
        }
    }
}

/// Recover the stable code embedded by [`ConnectError`] / [`tls_error_suffix`].
pub fn error_code_of(message: &str) -> Option<&'static str> {
    [TLS_VERIFICATION_FAILED, TLS_UNAVAILABLE]
        .into_iter()
        .find(|code| message.contains(&format!("(error_code={code})")))
}

/// `" (error_code=...)"` suffix for a TLS-related connection failure, else empty.
pub(crate) fn tls_error_suffix(code: Option<&'static str>) -> String {
    code.map(|code| format!(" (error_code={code})")).unwrap_or_default()
}

pub(crate) fn tls_enabled(endpoint: &Endpoint) -> bool {
    endpoint.tls.mode != TlsMode::Disable
}

/// Effective name verified against the certificate.
pub(crate) fn tls_server_name(endpoint: &Endpoint) -> &str {
    endpoint
        .tls
        .server_name
        .as_deref()
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .unwrap_or(&endpoint.host)
}

/// Split a PEM bundle into individual certificates (native-tls parses only one).
fn parse_ca_bundle(data: &[u8]) -> Result<Vec<Certificate>, String> {
    if let Ok(cert) = Certificate::from_der(data) {
        return Ok(vec![cert]);
    }
    let text = std::str::from_utf8(data).map_err(|_| "CA file is neither DER nor PEM".to_string())?;
    const END: &str = "-----END CERTIFICATE-----";
    let mut certs = Vec::new();
    for block in text.split_inclusive(END) {
        if !block.contains("-----BEGIN CERTIFICATE-----") {
            continue;
        }
        let cert = Certificate::from_pem(block.trim().as_bytes()).map_err(|err| format!("invalid CA certificate: {err}"))?;
        certs.push(cert);
    }
    if certs.is_empty() {
        return Err("CA file contains no certificate".to_string());
    }
    Ok(certs)
}

fn build_connector(endpoint: &Endpoint) -> Result<TlsConnector, ConnectError> {
    let mut builder = TlsConnector::builder();
    if let Some(path) = endpoint.tls.ca_file.as_deref().filter(|p| !p.trim().is_empty()) {
        let data = std::fs::read(path).map_err(|err| ConnectError::new(None, format!("cannot read TLS CA file: {err}")))?;
        for cert in parse_ca_bundle(&data).map_err(|err| ConnectError::new(None, format!("TLS CA file error: {err}")))? {
            builder.add_root_certificate(cert);
        }
    }
    // verify_ca: chain (and expiry) only; verify_full: chain + name.
    builder.danger_accept_invalid_hostnames(endpoint.tls.mode == TlsMode::VerifyCa);
    builder.build().map_err(|err| ConnectError::new(Some(TLS_VERIFICATION_FAILED), format!("TLS setup failed: {err}")))
}

/// PostgreSQL connector that verifies `server_name` instead of the dialed host.
pub(crate) struct NamedTls {
    inner: MakeTlsConnector,
    name: String,
}

impl MakeTlsConnect<Socket> for NamedTls {
    type Stream = <MakeTlsConnector as MakeTlsConnect<Socket>>::Stream;
    type TlsConnect = <MakeTlsConnector as MakeTlsConnect<Socket>>::TlsConnect;
    type Error = <MakeTlsConnector as MakeTlsConnect<Socket>>::Error;

    fn make_tls_connect(&mut self, _domain: &str) -> Result<Self::TlsConnect, Self::Error> {
        <MakeTlsConnector as MakeTlsConnect<Socket>>::make_tls_connect(&mut self.inner, &self.name)
    }
}

/// `None` when the endpoint keeps TLS disabled.
pub(crate) fn postgres_tls(endpoint: &Endpoint) -> Result<Option<NamedTls>, ConnectError> {
    if !tls_enabled(endpoint) {
        return Ok(None);
    }
    let inner = MakeTlsConnector::new(build_connector(endpoint)?);
    Ok(Some(NamedTls { inner, name: tls_server_name(endpoint).to_string() }))
}

/// `err: source: source...` — tokio-postgres hides the real reason in `source()`.
pub(crate) fn error_chain(err: &dyn std::error::Error) -> String {
    let mut text = err.to_string();
    let mut source = err.source();
    while let Some(inner) = source {
        text.push_str(": ");
        text.push_str(&inner.to_string());
        source = inner.source();
    }
    text
}

/// Map a PostgreSQL connect failure to a TLS code (only when TLS was requested).
pub(crate) fn classify_postgres_error(endpoint: &Endpoint, err: &postgres::Error) -> Option<&'static str> {
    if !tls_enabled(endpoint) {
        return None;
    }
    let text = error_chain(err);
    if text.contains("server does not support TLS") {
        Some(TLS_UNAVAILABLE)
    } else if text.contains("TLS handshake") {
        Some(TLS_VERIFICATION_FAILED)
    } else {
        None
    }
}

/// Map a MySQL connect failure to a TLS code (only when TLS was requested).
pub(crate) fn classify_mysql_error(endpoint: &Endpoint, err: &mysql::Error) -> Option<&'static str> {
    if !tls_enabled(endpoint) {
        return None;
    }
    match err {
        mysql::Error::DriverError(mysql::DriverError::TlsNotSupported) => Some(TLS_UNAVAILABLE),
        mysql::Error::TlsError(_) => Some(TLS_VERIFICATION_FAILED),
        _ => None,
    }
}

pub(crate) fn mysql_ssl_opts(endpoint: &Endpoint) -> Option<mysql::SslOpts> {
    if !tls_enabled(endpoint) {
        return None;
    }
    let ca = endpoint
        .tls
        .ca_file
        .as_deref()
        .filter(|p| !p.trim().is_empty())
        .map(|p| std::path::PathBuf::from(p));
    Some(
        mysql::SslOpts::default()
            .with_root_cert_path(ca)
            .with_danger_skip_domain_validation(endpoint.tls.mode == TlsMode::VerifyCa)
            .with_tls_server_name(Some(tls_server_name(endpoint).to_string())),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{TlsSettings};

    fn endpoint(mode: TlsMode, server_name: Option<&str>) -> Endpoint {
        Endpoint {
            engine: "postgresql".into(),
            host: "127.0.0.1".into(),
            port: 5432,
            user: "u".into(),
            password: String::new(),
            database: "d".into(),
            schema: None,
            tls: TlsSettings { mode, ca_file: None, server_name: server_name.map(String::from) },
        }
    }

    #[test]
    fn server_name_overrides_host_and_blank_falls_back() {
        assert_eq!(tls_server_name(&endpoint(TlsMode::VerifyFull, Some("db.internal"))), "db.internal");
        assert_eq!(tls_server_name(&endpoint(TlsMode::VerifyFull, Some("  "))), "127.0.0.1");
        assert_eq!(tls_server_name(&endpoint(TlsMode::VerifyFull, None)), "127.0.0.1");
    }

    #[test]
    fn disabled_endpoint_builds_no_tls() {
        let ep = endpoint(TlsMode::Disable, None);
        assert!(postgres_tls(&ep).unwrap().is_none());
        assert!(mysql_ssl_opts(&ep).is_none());
    }

    #[test]
    fn verify_modes_configure_mysql_name_policy() {
        let full = mysql_ssl_opts(&endpoint(TlsMode::VerifyFull, Some("db.internal"))).unwrap();
        assert!(!full.skip_domain_validation());
        assert_eq!(full.tls_server_name(), Some("db.internal"));
        let ca = mysql_ssl_opts(&endpoint(TlsMode::VerifyCa, None)).unwrap();
        assert!(ca.skip_domain_validation());
        assert!(!ca.accept_invalid_certs());
    }

    #[test]
    fn ca_bundle_rejects_garbage_and_empty() {
        assert!(parse_ca_bundle(b"not a cert").is_err());
        assert!(parse_ca_bundle(b"").is_err());
    }

    #[test]
    fn error_code_round_trips_through_message() {
        let err = ConnectError::new(Some(TLS_UNAVAILABLE), "postgresql connection error: x");
        assert_eq!(error_code_of(&err.to_string()), Some(TLS_UNAVAILABLE));
        assert_eq!(error_code_of("plain failure"), None);
    }
}
