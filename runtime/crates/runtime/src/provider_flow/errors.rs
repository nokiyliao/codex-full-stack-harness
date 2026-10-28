//! Provider failure helpers.

use chrono::{DateTime, Utc};
use lifecycle::RuntimeState;
use std::time::Duration;

use lifecycle::{RuntimeAggregate, RuntimeError, UsageReport};

pub(crate) fn runtime_timeout(runtime: &RuntimeAggregate) -> std::time::Duration {
    std::time::Duration::from_millis(runtime.provider.base.time_out_ms.max(1_000))
}

pub(crate) fn provider_failure_output(error: &tura_llm_rust::TuraError) -> serde_json::Value {
    let mut output = serde_json::json!({"error": error.to_string()});
    if let Some(snapshot) = error.stream_failure_snapshot() {
        output["provider_stream_failure"] = serde_json::json!(snapshot);
    }
    if error.model_attempt_not_started() {
        output["provider_call_observation"] = serde_json::json!({
            "schema_version": "provider_call_observation_v1",
            "scope": "runtime_provider_call",
            "stage": "authentication",
            "model_provider_invoked": false,
        });
    }
    output
}

pub(crate) fn provider_failure_output_with_runtime_context(
    error: &tura_llm_rust::TuraError,
    runtime_id: &str,
    command_effect_started: bool,
) -> serde_json::Value {
    let mut output = provider_failure_output(error);
    if let Some(snapshot) = output.get_mut("provider_stream_failure") {
        snapshot["runtime_id"] = serde_json::json!(runtime_id);
        snapshot["command_effect_started"] = serde_json::json!(command_effect_started);
    }
    output
}

pub(crate) fn provider_failure_observation(error: &tura_llm_rust::TuraError) -> Option<lifecycle::ProviderObservation> {
    if error.failed_attempts().is_empty() || error.model_attempt_not_started() { return None; }
    Some(lifecycle::ProviderObservation {
        schema_version: "provider_observation_v1".into(),
        source: "provider_failure".into(),
        response_id: None, model: None, service_tier: None,
        failed_attempts: super::usage::failure_attempt_values(error.failed_attempts()),
    })
}

pub(crate) fn finish_runtime_failure(
    runtime: &mut RuntimeAggregate,
    finished_at: DateTime<Utc>,
    error_code: &str,
    error_text: String,
    terminal_state: RuntimeState,
) -> Result<(), String> {
    finish_runtime_failure_with_usage(
        runtime,
        finished_at,
        error_code,
        error_text,
        terminal_state,
        None,
    )
}

pub(crate) fn finish_runtime_failure_with_usage(
    runtime: &mut RuntimeAggregate,
    finished_at: DateTime<Utc>,
    error_code: &str,
    error_text: String,
    terminal_state: RuntimeState,
    usage: Option<UsageReport>,
) -> Result<(), String> {
    finish_runtime_failure_with_policy(
        runtime,
        finished_at,
        error_code,
        error_text,
        terminal_state,
        RuntimeFailurePolicy {
            usage,
            retry_allowed: true,
            fallback_allowed: true,
        },
    )
}

pub(crate) fn finish_provider_call_failure(
    runtime: &mut RuntimeAggregate,
    finished_at: DateTime<Utc>,
    error: &tura_llm_rust::TuraError,
    terminal_state: RuntimeState,
) -> Result<(), String> {
    let retry_allowed = !error.is_non_retryable_provider_failure();
    finish_runtime_failure_with_policy(
        runtime,
        finished_at,
        "CALL_FAILED",
        error.to_string(),
        terminal_state,
        RuntimeFailurePolicy {
            usage: None,
            retry_allowed,
            fallback_allowed: retry_allowed,
        },
    )
}

pub(crate) fn finish_provider_call_failure_after_command_effect(
    runtime: &mut RuntimeAggregate,
    finished_at: DateTime<Utc>,
    error: &tura_llm_rust::TuraError,
    terminal_state: RuntimeState,
) -> Result<(), String> {
    finish_runtime_failure_with_policy(
        runtime,
        finished_at,
        "CALL_FAILED_AFTER_COMMAND_EFFECT",
        error.to_string(),
        terminal_state,
        RuntimeFailurePolicy {
            usage: None,
            retry_allowed: false,
            fallback_allowed: false,
        },
    )
}

pub(crate) fn finish_runtime_failure_after_command_effect(
    runtime: &mut RuntimeAggregate,
    finished_at: DateTime<Utc>,
    error_code: &str,
    error_text: String,
    terminal_state: RuntimeState,
) -> Result<(), String> {
    finish_runtime_failure_with_policy(
        runtime,
        finished_at,
        error_code,
        error_text,
        terminal_state,
        RuntimeFailurePolicy {
            usage: None,
            retry_allowed: false,
            fallback_allowed: false,
        },
    )
}

pub(crate) fn finish_runtime_failure_with_retry_policy(
    runtime: &mut RuntimeAggregate,
    finished_at: DateTime<Utc>,
    error_code: &str,
    error_text: String,
    terminal_state: RuntimeState,
    retry_allowed: bool,
) -> Result<(), String> {
    finish_runtime_failure_with_policy(
        runtime,
        finished_at,
        error_code,
        error_text,
        terminal_state,
        RuntimeFailurePolicy {
            usage: None,
            retry_allowed,
            fallback_allowed: false,
        },
    )
}

struct RuntimeFailurePolicy {
    usage: Option<UsageReport>,
    retry_allowed: bool,
    fallback_allowed: bool,
}

fn finish_runtime_failure_with_policy(
    runtime: &mut RuntimeAggregate,
    finished_at: DateTime<Utc>,
    error_code: &str,
    error_text: String,
    terminal_state: RuntimeState,
    policy: RuntimeFailurePolicy,
) -> Result<(), String> {
    let err = RuntimeError {
        error_code: Some(error_code.to_string()),
        error_text: Some(error_text),
        retry_allowed: policy.retry_allowed,
        fallback_allowed: policy.fallback_allowed,
        fallback_to_id: None,
    };
    runtime
        .finish_failure(finished_at, err, terminal_state, policy.usage)
        .map_err(|e| format!("failed to finish runtime failure: {e}"))
}

pub(crate) fn provider_timeout_retry_wait(retry_count: u8) -> Option<Duration> {
    if let Some(duration) = provider_retry_wait_override(retry_count) {
        return Some(duration);
    }
    match retry_count {
        0 => Some(Duration::from_secs(5)),
        1 => Some(Duration::from_secs(15)),
        2 => Some(Duration::from_secs(45)),
        _ => None,
    }
}

fn provider_retry_wait_override(retry_count: u8) -> Option<Duration> {
    let raw = std::env::var("TURA_PROVIDER_RETRY_BACKOFF_MS").ok()?;
    let values = raw
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .filter_map(|value| value.parse::<u64>().ok())
        .collect::<Vec<_>>();
    values
        .get(usize::from(retry_count))
        .copied()
        .map(Duration::from_millis)
}

pub(crate) fn runtime_failure_allows_retry(runtime: &RuntimeAggregate) -> bool {
    matches!(runtime.state, RuntimeState::Failed | RuntimeState::TimedOut)
        && runtime
            .error
            .as_ref()
            .map(|error| error.retry_allowed)
            .unwrap_or(false)
}

pub(crate) fn runtime_failure_requires_exact_input(runtime: &RuntimeAggregate) -> bool {
    let Some(error) = runtime.error.as_ref() else {
        return false;
    };
    if error.error_code.as_deref() == Some("OFFICIAL_CODEX_APP_SERVER_FAILED") {
        return true;
    }
    // Provider-only failures need no new model instructions. Keep supported
    // media adaptation on its existing route; this never grants retry authority.
    error.error_code.as_deref() == Some("CALL_FAILED")
        && error.retry_allowed
        && error.error_text.as_deref().is_some_and(|text| {
            tura_llm_rust::provider_media_fallback(text).is_none()
        })
}

pub(crate) fn runtime_failure_text(runtime: &RuntimeAggregate) -> Option<String> {
    runtime
        .error
        .as_ref()
        .and_then(|error| error.error_text.clone())
        .or_else(|| {
            runtime
                .output
                .as_ref()
                .and_then(|output| output.get("error"))
                .and_then(serde_json::Value::as_str)
                .map(ToString::to_string)
        })
}

#[cfg(test)]
mod tests {
    use chrono::Utc;
    use lifecycle::RuntimeState;
    use lifecycle::{ProviderConfig, ToolChoice};
    use lifecycle::{RuntimeAggregate, RuntimeError, RuntimeProviderConfig};
    use serde_json::json;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn stream_failure_snapshot_survives_aggregation_and_binds_only_observed_runtime_context() {
        let snapshot = tura_llm_rust::ProviderStreamFailureSnapshot {
            schema_version: "provider_stream_failure_snapshot_v1".into(),
            event_count: 3, completed_seen: false, completion_valid: false,
            completed_frame_ended: false, pending_bytes: 7,
            request_id: Some("req_fixture".into()), runtime_id: None,
            command_effect_started: None,
        };
        let plain = tura_llm_rust::TuraError::Network { message: "stream interrupted".into() };
        let wrapped = tura_llm_rust::TuraError::StreamFailure {
            source: Box::new(plain), snapshot: snapshot.clone(),
        };
        assert_eq!(wrapped.to_string(), "network error: stream interrupted");
        assert!(!wrapped.is_non_retryable_provider_failure());
        assert!(!wrapped.model_attempt_not_started());
        assert!(super::provider_failure_output(&wrapped)["provider_stream_failure"]["runtime_id"].is_null());
        let aggregated = tura_llm_rust::TuraError::ObservedFailure {
            source: Box::new(tura_llm_rust::TuraError::AllProvidersFailed { message: "stream interrupted".into() }),
            attempts: vec![tura_llm_rust::failure_usage::FailedProviderAttempt {
                stream_failure_snapshot: Some(snapshot), ..Default::default()
            }],
        };
        assert_eq!(aggregated.failed_attempts().len(), 1);
        for effect_started in [false, true] {
            let output = super::provider_failure_output_with_runtime_context(
                &aggregated, "runtime-fixture", effect_started,
            );
            assert_eq!(output["error"], aggregated.to_string());
            assert_eq!(output["provider_stream_failure"]["runtime_id"], "runtime-fixture");
            assert_eq!(output["provider_stream_failure"]["command_effect_started"], effect_started);
            assert_eq!(output["provider_stream_failure"]["pending_bytes"], 7);
            let mut runtime = runtime_for_retry_test("snapshot-retry-policy");
            if effect_started {
                super::finish_provider_call_failure_after_command_effect(
                    &mut runtime, Utc::now(), &aggregated, RuntimeState::Failed,
                ).expect("post-effect failure");
            } else {
                super::finish_provider_call_failure(
                    &mut runtime, Utc::now(), &aggregated, RuntimeState::Failed,
                ).expect("pre-effect failure");
            }
            assert_eq!(super::runtime_failure_allows_retry(&runtime), !effect_started);
            assert!(runtime.usage.is_none());
        }
        let denied = tura_llm_rust::TuraError::StreamFailure {
            source: Box::new(tura_llm_rust::TuraError::HttpStatus { status: 401, body: "denied".into() }),
            snapshot: aggregated.stream_failure_snapshot().unwrap().clone(),
        };
        assert!(denied.is_non_retryable_provider_failure());
        assert_eq!(denied.to_string(), "http status 401: denied");
    }

    #[test]
    fn native_auth_repair_is_terminal_without_retry_fallback_or_fabricated_usage() {
        let error = tura_llm_rust::TuraError::AuthenticationBeforeModel {
            source: Box::new(tura_llm_rust::TuraError::NativeAuthRepairRequired {
                source: Box::new(tura_llm_rust::TuraError::io(
                    std::io::Error::from(std::io::ErrorKind::PermissionDenied))),
            }),
        };
        let mut runtime = runtime_for_retry_test("native-auth-repair");
        runtime.set_output(super::provider_failure_output(&error)).expect("auth output");
        super::finish_provider_call_failure(&mut runtime, Utc::now(), &error, RuntimeState::Failed)
            .expect("terminal auth failure");
        let snapshot = serde_json::to_value(&runtime).expect("snapshot");
        let restored: RuntimeAggregate = serde_json::from_value(snapshot).expect("restore");
        assert!(!super::runtime_failure_allows_retry(&restored));
        assert!(!restored.error.as_ref().unwrap().fallback_allowed);
        assert!(restored.usage.is_none());
        assert_eq!(restored.output.unwrap()["provider_call_observation"]["model_provider_invoked"], false);
    }

    #[test]
    fn failure_usage_is_durable_separate_evidence_not_success_or_zero_billing() {
        let error = tura_llm_rust::TuraError::ObservedFailure {
            source: Box::new(tura_llm_rust::TuraError::HttpStatus { status: 403, body: "denied".into() }),
            attempts: vec![tura_llm_rust::failure_usage::FailedProviderAttempt {
                provider: "codex".into(), requested_model: "fixture".into(),
                usage: Some(tura_llm_rust::failure_usage::FailedUsage {
                    input_tokens: Some(12), ..Default::default()
                }), ..Default::default()
            }],
        };
        let mut runtime = runtime_for_retry_test("failed-usage");
        runtime.set_output_with_provider_observation(
            super::provider_failure_output(&error), super::provider_failure_observation(&error),
        ).expect("output");
        super::finish_provider_call_failure(&mut runtime, Utc::now(), &error, RuntimeState::Failed).expect("failure");
        assert!(!super::runtime_failure_allows_retry(&runtime));
        assert!(runtime.usage.is_none());
        let snapshot = serde_json::to_value(&runtime).expect("snapshot");
        let restored: RuntimeAggregate = serde_json::from_value(snapshot).expect("restore");
        let observed = restored.provider_observation.unwrap();
        assert_eq!(observed.failed_attempts[0]["usage"]["input_tokens"], 12);
        assert!(observed.failed_attempts[0]["usage"]["total_tokens"].is_null());
        assert!(observed.failed_attempts[0].get("cost").is_none());
        assert_eq!(restored.output.unwrap(), json!({"error":"http status 403: denied"}));
    }

    #[test]
    fn authentication_observation_persists_without_accepting_effects_or_inventing_usage() {
        let error = tura_llm_rust::TuraError::AuthenticationBeforeModel {
            source: Box::new(tura_llm_rust::TuraError::Network { message: "auth network failure".into() }),
        };
        let mut runtime = runtime_for_retry_test("authentication-stage");
        let output = super::provider_failure_output(&error);
        assert_eq!(output["error"], error.to_string());
        assert_eq!(output["provider_call_observation"], json!({
            "schema_version": "provider_call_observation_v1", "scope": "runtime_provider_call",
            "stage": "authentication", "model_provider_invoked": false,
        }));
        runtime.set_output(output).expect("stage output");
        super::finish_provider_call_failure(&mut runtime, Utc::now(), &error, RuntimeState::Failed)
            .expect("auth failure");
        let serialized = serde_json::to_value(&runtime).expect("serialized runtime evidence");
        assert_eq!(serialized["output"]["provider_call_observation"]["model_provider_invoked"], false);
        assert!(runtime.usage.is_none());
        assert_eq!(runtime.error.as_ref().unwrap().error_code.as_deref(), Some("CALL_FAILED"));
        assert!(super::runtime_failure_allows_retry(&runtime));
        let model = tura_llm_rust::TuraError::Network { message: "model stream interrupted".into() };
        assert_eq!(super::provider_failure_output(&model), json!({"error": model.to_string()}));
        assert!(super::provider_failure_output(&model).get("provider_call_observation").is_none());
        let mut effected = runtime_for_retry_test("authentication-stage-after-effect");
        super::finish_provider_call_failure_after_command_effect(&mut effected, Utc::now(), &error, RuntimeState::Failed)
            .expect("effect failure");
        assert!(!super::runtime_failure_allows_retry(&effected));
        assert!(!effected.error.as_ref().unwrap().fallback_allowed);
        assert!(effected.usage.is_none());
    }

    #[test]
    fn provider_timeout_retry_waits_use_three_step_backoff() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        let previous = std::env::var_os("TURA_PROVIDER_RETRY_BACKOFF_MS");
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_PROVIDER_RETRY_BACKOFF_MS")
        };
        assert_eq!(
            super::provider_timeout_retry_wait(0),
            Some(std::time::Duration::from_secs(5))
        );
        assert_eq!(
            super::provider_timeout_retry_wait(1),
            Some(std::time::Duration::from_secs(15))
        );
        assert_eq!(
            super::provider_timeout_retry_wait(2),
            Some(std::time::Duration::from_secs(45))
        );
        assert_eq!(super::provider_timeout_retry_wait(3), None);
        restore_env("TURA_PROVIDER_RETRY_BACKOFF_MS", previous);
    }

    #[test]
    fn provider_timeout_retry_wait_allows_fast_business_test_override() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        let previous = std::env::var_os("TURA_PROVIDER_RETRY_BACKOFF_MS");
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_PROVIDER_RETRY_BACKOFF_MS", "0,1,2")
        };

        assert_eq!(
            super::provider_timeout_retry_wait(0),
            Some(std::time::Duration::from_millis(0))
        );
        assert_eq!(
            super::provider_timeout_retry_wait(1),
            Some(std::time::Duration::from_millis(1))
        );
        assert_eq!(
            super::provider_timeout_retry_wait(2),
            Some(std::time::Duration::from_millis(2))
        );
        assert_eq!(super::provider_timeout_retry_wait(3), None);

        restore_env("TURA_PROVIDER_RETRY_BACKOFF_MS", previous);
    }

    #[test]
    fn provider_schema_error_removes_rejected_media_content_type() {
        let error = "http status 400: Invalid value: 'input_file'. Supported values are: 'input_text', 'input_image'";
        assert_eq!(
            tura_llm_rust::provider_unsupported_content_type(error),
            Some("input_file")
        );

        let mut messages = vec![json!({
            "type": "function_call_output",
            "call_id": "call_1",
            "output": [
                { "type": "input_text", "text": "kept" },
                { "type": "input_file", "filename": "tone.mp3", "file_data": "data:audio/mpeg;base64,QUJD" },
                { "type": "input_image", "image_url": "data:image/jpeg;base64,AAA" }
            ]
        })];

        let removed = tura_llm_rust::replace_unsupported_content_type_in_messages(
            &mut messages,
            "input_file",
        );
        assert_eq!(removed, 1);
        let serialized = serde_json::to_string(&messages).expect("serialize");
        assert!(serialized.contains("Unsupported media omitted"));
        assert!(serialized.contains("input_image"));
        assert!(!serialized.contains("file_data"));
        assert!(!serialized.contains("tone.mp3"));
    }

    #[test]
    fn retry_allowed_failed_runtime_uses_provider_retry_path() {
        let mut runtime = runtime_for_retry_test("retryable-runtime");
        runtime
            .finish_failure(
                runtime.created_at,
                RuntimeError {
            error_code: Some("CALL_FAILED".to_string()),
            error_text: Some(
                "all providers failed: openai:gpt-5.1 => network error: error decoding response body"
                    .to_string(),
            ),
            retry_allowed: true,
            fallback_allowed: true,
            fallback_to_id: None,
                },
                RuntimeState::Failed,
                None,
            )
            .expect("mark runtime failed");

        assert!(super::runtime_failure_allows_retry(&runtime));
        assert_eq!(
            super::runtime_failure_text(&runtime).as_deref(),
            Some(
                "all providers failed: openai:gpt-5.1 => network error: error decoding response body"
            )
        );
    }

    #[test]
    fn non_retryable_failed_runtime_does_not_use_provider_retry_path() {
        let mut runtime = runtime_for_retry_test("non-retryable-runtime");
        runtime
            .finish_failure(
                runtime.created_at,
                RuntimeError {
                    error_code: Some("CALL_FAILED".to_string()),
                    error_text: Some("provider rejected invalid request".to_string()),
                    retry_allowed: false,
                    fallback_allowed: false,
                    fallback_to_id: None,
                },
                RuntimeState::Failed,
                None,
            )
            .expect("mark runtime failed");

        assert!(!super::runtime_failure_allows_retry(&runtime));
        assert_eq!(
            super::runtime_failure_text(&runtime).as_deref(),
            Some("provider rejected invalid request")
        );
    }

    #[test]
    fn command_effect_failure_is_not_retryable() {
        let mut runtime = runtime_for_retry_test("command-effect-runtime");
        super::finish_runtime_failure_after_command_effect(
            &mut runtime,
            Utc::now(),
            "CALL_TIMED_OUT_AFTER_COMMAND_EFFECT",
            "command receipt is uncertain".to_string(),
            RuntimeState::TimedOut,
        )
        .expect("command-effect failure should be recorded");

        assert!(!super::runtime_failure_allows_retry(&runtime));
        let error = runtime.error.expect("runtime error");
        assert_eq!(
            error.error_code.as_deref(),
            Some("CALL_TIMED_OUT_AFTER_COMMAND_EFFECT")
        );
        assert!(!error.retry_allowed);
        assert!(!error.fallback_allowed);
    }

    #[test]
    fn timed_out_runtime_requires_explicit_retry_policy() {
        let mut runtime = runtime_for_retry_test("timeout-retry-policy");
        super::finish_runtime_failure(
            &mut runtime,
            Utc::now(),
            "CALL_TIMED_OUT",
            "provider timed out before commands".to_string(),
            RuntimeState::TimedOut,
        )
        .expect("pre-effect timeout");
        assert!(super::runtime_failure_allows_retry(&runtime));
        runtime.error.as_mut().unwrap().retry_allowed = false;
        assert!(!super::runtime_failure_allows_retry(&runtime));
        runtime.error = None;
        assert!(!super::runtime_failure_allows_retry(&runtime));
    }

    #[test]
    fn official_codex_transport_retry_preserves_exact_input_policy() {
        let mut runtime = runtime_for_retry_test("official-codex-transport-runtime");
        super::finish_runtime_failure_with_retry_policy(
            &mut runtime,
            Utc::now(),
            "OFFICIAL_CODEX_APP_SERVER_FAILED",
            "official Codex App Server closed stdout before turn completion".to_string(),
            RuntimeState::Failed,
            true,
        )
        .expect("official Codex transport failure should be recorded");

        assert!(super::runtime_failure_allows_retry(&runtime));
        assert!(super::runtime_failure_requires_exact_input(&runtime));
        assert!(!runtime.error.as_ref().unwrap().fallback_allowed);
    }

    #[test]
    fn provider_transport_retry_preserves_input_and_unknown_usage() {
        let mut runtime = runtime_for_retry_test("transport-retry-input");
        let error = tura_llm_rust::TuraError::Network {
            message: "provider transport failure: phase=responses-sse category=decode cause=unexpected-eof".to_string(),
        };
        super::finish_provider_call_failure(
            &mut runtime, Utc::now(), &error, RuntimeState::Failed,
        ).expect("pre-effect transport failure");
        assert!(super::runtime_failure_allows_retry(&runtime));
        assert!(super::runtime_failure_requires_exact_input(&runtime));
        assert!(runtime.usage.is_none());
        assert_eq!(runtime.error.as_ref().unwrap().error_code.as_deref(), Some("CALL_FAILED"));
    }

    #[test]
    fn media_failure_retains_the_existing_input_adaptation_route() {
        let mut runtime = runtime_for_retry_test("media-retry-input");
        let error = tura_llm_rust::TuraError::ProviderRequest {
            provider: "openai".to_string(),
            message: "Invalid value: 'input_file'".to_string(),
        };
        super::finish_provider_call_failure(
            &mut runtime, Utc::now(), &error, RuntimeState::Failed,
        ).expect("media failure");
        assert!(super::runtime_failure_allows_retry(&runtime));
        assert!(!super::runtime_failure_requires_exact_input(&runtime));
    }

    #[test]
    fn auth_failure_and_command_effects_do_not_gain_replay_authority() {
        let mut denied = runtime_for_retry_test("auth-retry-input");
        super::finish_provider_call_failure(
            &mut denied, Utc::now(),
            &tura_llm_rust::TuraError::HttpStatus { status: 401, body: "unauthorized".to_string() },
            RuntimeState::Failed,
        ).expect("auth failure");
        assert!(!super::runtime_failure_allows_retry(&denied));
        assert!(!super::runtime_failure_requires_exact_input(&denied));

        let mut effected = runtime_for_retry_test("effect-retry-input");
        super::finish_provider_call_failure_after_command_effect(
            &mut effected, Utc::now(),
            &tura_llm_rust::TuraError::Network { message: "connection reset".to_string() },
            RuntimeState::Failed,
        ).expect("command effect failure");
        assert!(!super::runtime_failure_allows_retry(&effected));
        assert!(!super::runtime_failure_requires_exact_input(&effected));
    }

    fn runtime_for_retry_test(runtime_id: &str) -> RuntimeAggregate {
        let now = Utc::now();
        let provider = "openai".to_string();
        RuntimeAggregate::new(
            runtime_id.to_string(),
            "session-for-retry-test".to_string(),
            "session-for-retry-test".to_string(),
            RuntimeProviderConfig {
                base: ProviderConfig {
                    tura_llm_name: provider.clone(),
                    default_model_tier: None,
                    current_model: None,
                    stream: true,
                    temperature: 0.0,
                    max_tokens: 0,
                    tool_choice: ToolChoice::Auto,
                    time_out_ms: 120_000,
                },
                thinking: false,
                provider_name: provider.clone(),
                model_name: "gpt-5.1".to_string(),
                provider_url_name: provider.clone(),
                llm_provider_name: provider,
            },
            now,
        )
    }

    fn restore_env(key: &str, previous: Option<std::ffi::OsString>) {
        if let Some(previous) = previous {
            // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::set_var(key, previous)
            };
        } else {
            // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::remove_var(key)
            };
        }
    }
}
