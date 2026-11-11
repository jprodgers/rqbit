//! A session with the default, dual-stack listen address must start whether
//! or not the host can make IPv6 sockets (it cannot in some containers and on
//! kernels booted with `ipv6.disable=1`).

use tempfile::TempDir;

use crate::{ListenerOptions, Session, SessionOptions};

#[tokio::test(flavor = "multi_thread")]
async fn test_session_listens_with_or_without_ipv6() -> anyhow::Result<()> {
    let dir = TempDir::with_prefix("rqbit_session_start")?;
    let session = Session::new_with_opts(
        dir.path().into(),
        SessionOptions {
            dht: None,
            disable_local_service_discovery: true,
            // `[::]:0`, as `rqbit --listen-port` and library users ask for.
            listen: Some(ListenerOptions::default()),
            ..Default::default()
        },
    )
    .await?;
    assert!(session.listen_addr().is_some(), "not listening");
    session.stop().await;
    Ok(())
}

// With rustls and no cryptography provider installed, creating a session is
// an error that says so, not a panic inside the HTTP client.
#[cfg(feature = "pure-rust-tls-no-provider")]
#[tokio::test(flavor = "multi_thread")]
async fn test_session_without_a_tls_provider_is_an_error() -> anyhow::Result<()> {
    let dir = TempDir::with_prefix("rqbit_session_no_provider")?;
    let result = Session::new_with_opts(
        dir.path().into(),
        SessionOptions {
            dht: None,
            disable_local_service_discovery: true,
            ..Default::default()
        },
    )
    .await;
    let err = format!("{:#}", result.err().expect("the session started"));
    assert!(err.contains("CryptoProvider"), "{err}");
    Ok(())
}
