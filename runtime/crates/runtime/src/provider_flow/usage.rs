//! Provider usage accounting helpers.

use chrono::{DateTime, Utc};

use lifecycle::{ProviderObservation, UsageReport};

/// Only the top-level final Responses object is evidence; never search embedded events.
pub(crate) fn provider_observation(raw: &serde_json::Value) -> ProviderObservation {
    fn field(raw: &serde_json::Value, key: &str) -> Option<String> {
        raw.get(key)
            .and_then(serde_json::Value::as_str)
            .map(str::trim)
            .filter(|value| {
                !value.is_empty() && value.len() <= 256 && !value.chars().any(char::is_control)
            })
            .map(str::to_owned)
    }
    ProviderObservation {
        schema_version: "provider_observation_v1".to_string(),
        source: "provider_response".to_string(),
        response_id: field(raw, "id"),
        model: field(raw, "model"),
        service_tier: field(raw, "service_tier"),
        failed_attempts: Vec::new(),
    }
}

pub(crate) fn response_observation(response: &tura_llm_rust::ProviderResponse) -> ProviderObservation {
    let mut observation = provider_observation(&response.raw);
    observation.failed_attempts = failure_attempt_values(&response.failed_attempts);
    observation
}

pub(crate) fn attach_transport_timing(
    mut output: serde_json::Value,
    metrics: Option<&tura_llm_rust::CallMetrics>,
) -> serde_json::Value {
    let timing = metrics.and_then(|m| m.transport_timing.as_ref());
    let profile = metrics.and_then(|m| m.request_profile.as_ref());
    if timing.is_none() && profile.is_none() {
        return output;
    }
    if let Some(text) = output.as_str() {
        output = serde_json::json!({"text": text});
    }
    if let Some(object) = output.as_object_mut() {
        if let Some(timing) = timing {
            object.insert("provider_transport_timing".into(), serde_json::json!({
                "schema_version": "provider_transport_timing_v1",
                "source": "local_monotonic_clock",
                "scope": "request_dispatch_to_validated_stream_completion",
                "transport": timing.transport,
                "response_headers_ms": timing.response_headers_ms,
                "first_output_event_ms": timing.first_output_event_ms,
                "completed_ms": timing.completed_ms,
            }));
        }
        if let Some(profile) = profile {
            object.insert("provider_request_profile".into(), profile.clone());
        }
    }
    output
}

pub(crate) fn failure_attempt_values(
    attempts: &[tura_llm_rust::failure_usage::FailedProviderAttempt],
) -> Vec<serde_json::Value> {
    attempts.iter().map(|a| serde_json::json!(a)).collect()
}

pub(crate) fn usage_report_from_metrics(
    metrics: Option<tura_llm_rust::CallMetrics>,
    started_at: DateTime<Utc>,
    finished_at: DateTime<Utc>,
    first_token_at: DateTime<Utc>,
) -> Option<UsageReport> {
    let latency_ms = finished_at
        .signed_duration_since(started_at)
        .num_milliseconds()
        .max(0) as u64;
    let time_to_first_token_ms = first_token_at
        .signed_duration_since(started_at)
        .num_milliseconds()
        .max(0) as u64;
    metrics.and_then(|m| {
        let input_tokens = m.usage.input_tokens?;
        let output_tokens = m.usage.output_tokens?;
        let total_tokens = m.usage.total_tokens?;
        Some(lifecycle::UsageReport {
            input_tokens,
            output_tokens,
            total_tokens,
            cached_input_tokens: m.usage.cached_input_tokens.unwrap_or(0),
            cache_write_tokens: m.usage.cache_write_tokens.unwrap_or(0),
            reasoning_tokens: m.usage.reasoning_tokens.unwrap_or(0),
            attachment_input_tokens: 0,
            input_cost: m.cost.input_cost.unwrap_or(0.0),
            output_cost: m.cost.output_cost.unwrap_or(0.0),
            total_cost: m.cost.total_cost.unwrap_or(0.0),
            currency: m.cost.currency.unwrap_or_else(|| "USD".to_string()),
            pricing_source: "provider".to_string(),
            latency_ms,
            time_to_first_token_ms,
            token_per_second: tokens_per_second(output_tokens, latency_ms),
        })
    })
}

fn tokens_per_second(output_tokens: u64, latency_ms: u64) -> f64 {
    if output_tokens == 0 || latency_ms == 0 {
        return 0.0;
    }
    output_tokens as f64 / (latency_ms as f64 / 1000.0)
}

pub(crate) fn runtime_cache_diagnostics(
    runtime: &lifecycle::RuntimeAggregate,
) -> serde_json::Value {
    let input = runtime.input.as_ref();
    let messages = input
        .and_then(|input| input.get("messages"))
        .and_then(serde_json::Value::as_array);
    let tools = input
        .and_then(|input| input.get("tools"))
        .and_then(serde_json::Value::as_array);
    let options = input.and_then(|input| input.get("options"));
    serde_json::json!({
        "input_hash": input.map(stable_json_hash).unwrap_or_default(),
        "message_count": messages.map(|messages| messages.len()).unwrap_or_default(),
        "tool_count": tools.map(|tools| tools.len()).unwrap_or_default(),
        "first_message_hash": messages
            .and_then(|messages| messages.first())
            .map(stable_json_hash)
            .unwrap_or_default(),
        "last_message_hash": messages
            .and_then(|messages| messages.last())
            .map(stable_json_hash)
            .unwrap_or_default(),
        "tools_hash": tools
            .map(|tools| stable_json_hash(&serde_json::Value::Array(tools.clone())))
            .unwrap_or_default(),
        "prompt_cache_key": options
            .and_then(|options| options.get("prompt_cache_key"))
            .cloned()
            .unwrap_or(serde_json::Value::Null),
    })
}

fn stable_json_hash(value: &serde_json::Value) -> String {
    let serialized = serde_json::to_string(value).unwrap_or_else(|_| value.to_string());
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in serialized.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

#[cfg(test)]
mod observation_tests {
    use super::provider_observation;
    use serde_json::json;

    #[test]
    fn transport_timing_keeps_missing_unknown_and_does_not_change_usage() {
        let text = json!("answer");
        assert_eq!(super::attach_transport_timing(text.clone(), None), text);
        let metrics = tura_llm_rust::CallMetrics {
            transport_timing: Some(tura_llm_rust::TransportTiming {
                transport: "http_sse".into(), response_headers_ms: 10,
                first_output_event_ms: None, completed_ms: 30,
            }), ..Default::default()
        };
        let output = super::attach_transport_timing(text, Some(&metrics));
        assert_eq!(output["text"], "answer");
        assert!(output["provider_transport_timing"]["first_output_event_ms"].is_null());
        assert!(output.get("provider_request_profile").is_none());
        assert!(metrics.usage.total_tokens.is_none());
    }

    #[test]
    fn request_profile_is_attached_beside_the_visible_text() {
        let metrics = tura_llm_rust::CallMetrics {
            request_profile: Some(json!({"instructions_source": "model_catalog"})),
            ..Default::default()
        };
        let output = super::attach_transport_timing(json!("answer"), Some(&metrics));
        assert_eq!(output["text"], "answer");
        assert_eq!(output["provider_request_profile"]["instructions_source"], "model_catalog");
        assert!(output.get("provider_transport_timing").is_none());
    }

    #[test]
    fn failure_usage_after_fallback_is_metadata_not_model_output_or_aggregate_cost() {
        let response = tura_llm_rust::ProviderResponse {
            content: json!("answer"),
            raw: json!({"id":"success", "failed_attempts":[{"usage":{"total_tokens":999}}]}),
            failed_attempts: vec![tura_llm_rust::failure_usage::FailedProviderAttempt {
                usage: Some(tura_llm_rust::failure_usage::FailedUsage {
                    total_tokens: Some(12), ..Default::default()
                }), ..Default::default()
            }],
            ..Default::default()
        };
        let observation = super::response_observation(&response);
        assert_eq!(observation.failed_attempts.len(), 1);
        assert_eq!(observation.failed_attempts[0]["usage"]["total_tokens"], 12);
        assert_eq!(response.content, json!("answer"));
        assert!(response.metrics.is_none());
    }

    #[test]
    fn final_response_only_and_missing_fields_are_unknown() {
        let raw = json!({"id":"resp-final", "model":"gpt-6", "service_tier":"default",
            "events":[{"response":{"id":"resp-old", "service_tier":"priority"}}]});
        let observed = provider_observation(&raw);
        assert_eq!(observed.response_id.as_deref(), Some("resp-final"));
        assert_eq!(observed.service_tier.as_deref(), Some("default"));
        assert_eq!(observed.model.as_deref(), Some("gpt-6"));
        let invalid = provider_observation(&json!({"id":" ", "model":42,
            "service_tier":"x".repeat(257), "events":[{"response":{"service_tier":"priority"}}]}));
        assert_eq!(invalid.response_id, None);
        assert_eq!(invalid.model, None);
        assert_eq!(invalid.service_tier, None);
    }
}
