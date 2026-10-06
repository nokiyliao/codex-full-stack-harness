use std::time::{Duration, Instant};

use futures_util::StreamExt;

use crate::tura_llm::TuraError;

#[derive(Clone, Copy)]
pub(crate) enum ProviderTransportPhase {
    ClientBuild,
    Request,
    ResponseBody,
    ResponsesSse,
}

impl ProviderTransportPhase {
    fn label(self) -> &'static str {
        match self {
            Self::ClientBuild => "client-build",
            Self::Request => "request",
            Self::ResponseBody => "response-body",
            Self::ResponsesSse => "responses-sse",
        }
    }
}

/// Diagnostics contain only fixed labels, never Display/Debug output or URLs.
pub(crate) fn provider_transport_error(
    phase: ProviderTransportPhase,
    error: &reqwest::Error,
) -> TuraError {
    let (cause, complete_chain) = provider_error_cause(error);
    // reqwest's timeout/connect predicates inspect sources themselves. Only
    // invoke them after establishing that the source chain fits our bound.
    let category = if error.is_decode() {
        "decode"
    } else if error.is_body() {
        "body"
    } else if complete_chain && error.is_timeout() {
        "timeout"
    } else if complete_chain && error.is_connect() {
        "connect"
    } else if error.is_builder() {
        "builder"
    } else if error.is_redirect() {
        "redirect"
    } else if error.is_status() {
        "http-status"
    } else if error.is_request() {
        "request"
    } else {
        "unknown"
    };
    TuraError::Network {
        message: format!(
            "provider transport failure: phase={} category={category} cause={cause}",
            phase.label()
        ),
    }
}

fn provider_error_cause(error: &(dyn std::error::Error + 'static)) -> (&'static str, bool) {
    let mut source = Some(error);
    let mut cause = "unknown";
    for _ in 0..8 {
        let Some(error) = source else {
            return (cause, true);
        };
        if matches!(cause, "unknown" | "io-other") {
            if let Some(error) = error.downcast_ref::<std::io::Error>() {
                cause = match error.kind() {
                    std::io::ErrorKind::UnexpectedEof => "unexpected-eof",
                    std::io::ErrorKind::ConnectionReset => "connection-reset",
                    std::io::ErrorKind::ConnectionAborted => "connection-aborted",
                    std::io::ErrorKind::ConnectionRefused => "connection-refused",
                    std::io::ErrorKind::BrokenPipe => "broken-pipe",
                    std::io::ErrorKind::NotConnected => "not-connected",
                    std::io::ErrorKind::TimedOut => "timed-out",
                    std::io::ErrorKind::InvalidData => "invalid-data",
                    _ => "io-other",
                };
            } else if let Some(error) = error.downcast_ref::<serde_json::Error>() {
                cause = match error.classify() {
                    serde_json::error::Category::Io => "json-io",
                    serde_json::error::Category::Syntax => "json-syntax",
                    serde_json::error::Category::Data => "json-data",
                    serde_json::error::Category::Eof => "json-eof",
                };
            }
        }
        source = error.source();
    }
    if source.is_some() {
        ("chain-limit", false)
    } else {
        (cause, true)
    }
}

pub fn provider_first_output_timeout() -> Duration {
    provider_timeout_from_env(
        "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
        crate::tura_llm::provider_latency_timeouts().first_output_timeout_ms,
    )
}

pub fn provider_idle_output_timeout() -> Duration {
    provider_timeout_from_env(
        "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
        crate::tura_llm::provider_latency_timeouts().idle_output_timeout_ms,
    )
}

/// Upper bound for reading a full non-streaming response body. The headers may
/// arrive promptly (passing [`send_provider_request_first_response`]) while the
/// upstream holds the connection open during a long reasoning phase; without
/// this bound `resp.json()` can hang indefinitely. Honors
/// `TURA_PROVIDER_TOTAL_TIMEOUT_MS`.
pub fn provider_total_timeout() -> Duration {
    provider_timeout_from_env(
        "TURA_PROVIDER_TOTAL_TIMEOUT_MS",
        crate::tura_llm::provider_latency_timeouts().total_timeout_ms,
    )
}

/// Await a non-streaming response body future under [`provider_total_timeout`]
/// so a stalled upstream cannot block the call forever.
pub async fn read_provider_response_body<T, F>(future: F) -> Result<T, TuraError>
where
    F: std::future::Future<Output = Result<T, reqwest::Error>>,
{
    let limit = provider_total_timeout();
    match tokio::time::timeout(limit, future).await {
        Ok(Ok(value)) => Ok(value),
        Ok(Err(err)) => Err(provider_transport_error(
            ProviderTransportPhase::ResponseBody,
            &err,
        )),
        Err(_) => Err(TuraError::Network {
            message: format!(
                "provider response body timed out after {} ms (no complete body received)",
                limit.as_millis()
            ),
        }),
    }
}

pub async fn send_provider_request_first_response(
    request: reqwest::RequestBuilder,
) -> Result<reqwest::Response, TuraError> {
    let limit = provider_first_output_timeout();
    match tokio::time::timeout(limit, request.send()).await {
        Ok(Ok(response)) => Ok(response),
        Ok(Err(err)) => Err(provider_transport_error(
            ProviderTransportPhase::Request,
            &err,
        )),
        Err(_) => Err(provider_timeout_error(false, limit)),
    }
}

pub async fn next_provider_stream_chunk<S>(
    stream: &mut S,
    saw_output: bool,
    last_output_at: Instant,
) -> Result<Option<S::Item>, TuraError>
where
    S: futures_util::Stream + Unpin,
{
    let limit = if saw_output {
        provider_idle_output_timeout()
    } else {
        provider_first_output_timeout()
    };
    let elapsed = last_output_at.elapsed();
    if elapsed >= limit {
        return Err(provider_timeout_error(saw_output, limit));
    }
    match tokio::time::timeout(limit - elapsed, stream.next()).await {
        Ok(next) => Ok(next),
        Err(_) => Err(provider_timeout_error(saw_output, limit)),
    }
}

pub fn provider_timeout_error(saw_output: bool, limit: Duration) -> TuraError {
    let phase = if saw_output {
        "new provider output"
    } else {
        "first provider output"
    };
    TuraError::Network {
        message: format!(
            "provider stream timed out waiting for {phase} after {} ms",
            limit.as_millis()
        ),
    }
}

fn provider_timeout_from_env(name: &str, default_ms: u64) -> Duration {
    std::env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|value| *value > 0)
        .map(Duration::from_millis)
        .unwrap_or_else(|| Duration::from_millis(default_ms))
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use futures_util::stream;

    use super::{
        ProviderTransportPhase, next_provider_stream_chunk, provider_error_cause,
        provider_timeout_error, provider_transport_error, read_provider_response_body,
        send_provider_request_first_response,
    };
    use crate::tura_llm::TuraError;

    fn sensitive_builder_error() -> reqwest::Error {
        reqwest::Client::new()
            .get("http://[invalid")
            .build()
            .expect_err("invalid URL")
            .with_url(
                reqwest::Url::parse(
                    "https://USER_SENTINEL:PASSWORD_SENTINEL@provider.invalid/URL_SENTINEL?prompt=PROMPT_SENTINEL&reasoning=REASONING_SENTINEL&auth=AUTH_SENTINEL&cookie=COOKIE_SENTINEL",
                )
                .expect("fixture URL"),
            )
    }

    #[test]
    fn transport_diagnostics_use_only_bounded_phase_and_category_labels() {
        let error = sensitive_builder_error();
        for (phase, label) in [
            (ProviderTransportPhase::ClientBuild, "client-build"),
            (ProviderTransportPhase::Request, "request"),
            (ProviderTransportPhase::ResponseBody, "response-body"),
            (ProviderTransportPhase::ResponsesSse, "responses-sse"),
        ] {
            let TuraError::Network { message } = provider_transport_error(phase, &error) else {
                panic!("expected network error");
            };
            assert_eq!(
                message,
                format!("provider transport failure: phase={label} category=builder cause=unknown")
            );
            assert!(message.len() <= 128);
            assert!(!message.contains("SENTINEL"));
            assert!(!message.contains("provider.invalid"));
        }
    }

    #[test]
    fn transport_cause_classification_never_formats_sources_and_bounds_cycles() {
        #[derive(Debug)]
        struct Source(std::io::Error);
        impl std::fmt::Display for Source {
            fn fmt(&self, _: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                panic!("error-source strings must never be formatted");
            }
        }
        impl std::error::Error for Source {
            fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
                Some(&self.0)
            }
        }
        for (kind, label) in [
            (std::io::ErrorKind::UnexpectedEof, "unexpected-eof"),
            (std::io::ErrorKind::ConnectionReset, "connection-reset"),
            (std::io::ErrorKind::TimedOut, "timed-out"),
            (std::io::ErrorKind::Other, "io-other"),
        ] {
            let error = Source(std::io::Error::new(
                kind,
                "https://provider.invalid/URL_SENTINEL PROMPT_SENTINEL REASONING_SENTINEL Authorization: AUTH_SENTINEL Cookie: COOKIE_SENTINEL".repeat(1024),
            ));
            assert_eq!(provider_error_cause(&error), (label, true));
        }

        #[derive(Debug)]
        struct Cycle;
        impl std::fmt::Display for Cycle {
            fn fmt(&self, _: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                panic!("cyclic source must never be formatted");
            }
        }
        impl std::error::Error for Cycle {
            fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
                Some(self)
            }
        }
        assert_eq!(provider_error_cause(&Cycle), ("chain-limit", false));
    }

    #[tokio::test]
    async fn request_and_body_helpers_preserve_safe_transport_phases() {
        let body_error = read_provider_response_body(std::future::ready(Err::<(), _>(
            sensitive_builder_error(),
        )))
        .await
        .expect_err("body failure");
        let request_error = send_provider_request_first_response(
            reqwest::Client::new()
                .post("http://[invalid")
                .bearer_auth("AUTH_SENTINEL")
                .header("cookie", "COOKIE_SENTINEL")
                .body("PROMPT_SENTINEL REASONING_SENTINEL"),
        )
        .await
        .expect_err("request failure without network I/O");
        for (error, phase) in [(body_error, "response-body"), (request_error, "request")] {
            let TuraError::Network { message } = error else {
                panic!("expected network error");
            };
            assert_eq!(
                message,
                format!("provider transport failure: phase={phase} category=builder cause=unknown")
            );
        }
    }

    #[test]
    fn timeout_error_names_first_and_idle_phases() {
        let first = provider_timeout_error(false, Duration::from_millis(7)).to_string();
        let idle = provider_timeout_error(true, Duration::from_millis(9)).to_string();

        assert!(first.contains("first provider output"));
        assert!(first.contains("7 ms"));
        assert!(idle.contains("new provider output"));
        assert!(idle.contains("9 ms"));
    }

    #[tokio::test]
    async fn next_provider_stream_chunk_returns_available_chunk() {
        let mut items = stream::iter([Ok::<_, std::io::Error>("hello")]);
        let next = next_provider_stream_chunk(&mut items, false, Instant::now())
            .await
            .expect("stream chunk result");

        assert_eq!(next.expect("chunk").expect("ok"), "hello");
    }
}
