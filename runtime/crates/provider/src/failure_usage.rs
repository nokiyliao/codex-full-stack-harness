//! Partial provider evidence: never synthesize missing tokens or billing.
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderStreamFailureSnapshot {
    pub schema_version: String,
    pub event_count: usize,
    pub completed_seen: bool,
    pub completion_valid: bool,
    pub completed_frame_ended: bool,
    pub pending_bytes: usize,
    pub request_id: Option<String>,
    pub runtime_id: Option<String>,
    pub command_effect_started: Option<bool>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct FailedProviderAttempt {
    pub provider: String,
    pub requested_model: String,
    pub response_id: Option<String>,
    pub response_model: Option<String>,
    pub request_id: Option<String>,
    pub response_status: Option<String>,
    pub model_attempt_not_started: bool,
    pub usage: Option<FailedUsage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stream_failure_snapshot: Option<ProviderStreamFailureSnapshot>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct FailedUsage {
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub total_tokens: Option<u64>,
    pub cached_input_tokens: Option<u64>,
    pub reasoning_tokens: Option<u64>,
    pub invalid_fields: Vec<String>,
}

pub(crate) fn bounded_identity(value: Option<&str>) -> Option<String> {
    value
        .filter(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control))
        .map(str::to_owned)
}

impl FailedProviderAttempt {
    pub(crate) fn from_response(
        provider: &str,
        model: &str,
        request_id: Option<&str>,
        data: &Value,
    ) -> Self {
        let usage = data.get("usage").filter(|v| !v.is_null()).map(|raw| {
            let mut invalid_fields = Vec::new();
            let mut count = |path: &str| {
                let value = raw.pointer(path);
                let parsed = value.and_then(Value::as_u64);
                if value.is_some_and(|v| !v.is_null()) && parsed.is_none() {
                    invalid_fields.push(path.to_owned());
                }
                parsed
            };
            let mut usage = FailedUsage {
                input_tokens: count("/input_tokens"),
                output_tokens: count("/output_tokens"),
                total_tokens: count("/total_tokens"),
                cached_input_tokens: count("/input_tokens_details/cached_tokens"),
                reasoning_tokens: count("/output_tokens_details/reasoning_tokens"),
                invalid_fields: Vec::new(),
            };
            if !raw.is_object() { invalid_fields.push("usage".into()); }
            if let (Some(i), Some(o), Some(t)) = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
                && i.checked_add(o) != Some(t) { invalid_fields.push("total_tokens_mismatch".into()); }
            if matches!((usage.cached_input_tokens, usage.input_tokens), (Some(c), Some(i)) if c > i) {
                invalid_fields.push("cached_input_tokens_exceeds_input".into());
            }
            if matches!((usage.reasoning_tokens, usage.output_tokens), (Some(r), Some(o)) if r > o) {
                invalid_fields.push("reasoning_tokens_exceeds_output".into());
            }
            usage.invalid_fields = invalid_fields;
            usage
        });
        Self {
            provider: provider.into(),
            requested_model: model.into(),
            response_id: bounded_identity(data.get("id").and_then(Value::as_str)),
            response_model: bounded_identity(data.get("model").and_then(Value::as_str)),
            request_id: bounded_identity(request_id),
            response_status: bounded_identity(data.get("status").and_then(Value::as_str)),
            model_attempt_not_started: false,
            usage,
            stream_failure_snapshot: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn failure_attempt_without_stream_snapshot_remains_wire_compatible() {
        let attempt = FailedProviderAttempt::from_response("codex", "fixture", None, &json!({}));
        let legacy = serde_json::to_value(&attempt).expect("attempt JSON");
        assert!(legacy.get("stream_failure_snapshot").is_none());
        let restored: FailedProviderAttempt = serde_json::from_value(legacy).expect("legacy attempt");
        assert!(restored.stream_failure_snapshot.is_none());
    }

    #[test]
    fn failure_usage_preserves_partial_and_rejects_invalid_counters() {
        let a = FailedProviderAttempt::from_response(
            "codex",
            "requested",
            Some("request-1"),
            &json!({
                "status":"failed", "model":"observed", "usage":{"input_tokens":12,"output_tokens":-1,
                "total_tokens":"12", "input_tokens_details":{"cached_tokens":0}}
            }),
        );
        let u = a.usage.unwrap();
        assert_eq!(u.input_tokens, Some(12));
        assert_eq!(u.cached_input_tokens, Some(0));
        assert_eq!(u.output_tokens, None);
        assert_eq!(u.total_tokens, None);
        assert_eq!(u.reasoning_tokens, None);
        assert_eq!(u.invalid_fields, vec!["/output_tokens", "/total_tokens"]);
        assert_eq!(a.request_id.as_deref(), Some("request-1"));
        assert_eq!(a.response_model.as_deref(), Some("observed"));
    }

    #[test]
    fn failure_usage_missing_is_unknown_and_nested_usage_is_not_evidence() {
        for data in [
            json!({}),
            json!({"usage":null}),
            json!({"events":[{"usage":{"total_tokens":99}}]}),
        ] {
            assert!(
                FailedProviderAttempt::from_response("p", "m", None, &data)
                    .usage
                    .is_none()
            );
        }
        let a = FailedProviderAttempt::from_response(
            "p",
            "m",
            None,
            &json!({"usage":{
            "input_tokens":10,"output_tokens":2,"total_tokens":8,"input_tokens_details":{"cached_tokens":11}}}),
        );
        assert_eq!(a.usage.unwrap().invalid_fields.len(), 2);
    }
}
