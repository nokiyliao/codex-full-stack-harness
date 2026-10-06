//! Provider-origin opaque continuity, separate from tool dispatch and visible text.
use lifecycle::{
    RuntimeAggregate, RuntimeCallResultStatus, RuntimeProviderConfig, SessionManagement,
};
use serde_json::{Value, json};

const FIELD: &str = "responses_continuity";
const SCHEMA: &str = "responses_reasoning_continuity_v1";

pub(crate) fn is_opaque_reasoning(item: &Value) -> bool {
    item.get("type").and_then(Value::as_str) == Some("reasoning")
        && item
            .get("id")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty())
        && item
            .get("encrypted_content")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty())
        && item.get("summary").is_some_and(Value::is_array)
}

pub(crate) fn attach_reasoning(
    mut output: Value,
    raw: &Value,
    provider: &RuntimeProviderConfig,
) -> Result<Value, String> {
    if provider.llm_provider_name != "codex"
        || raw.get("status").and_then(Value::as_str) != Some("completed")
    {
        return Ok(output);
    }
    let items: Vec<Value> = raw
        .get("output")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|item| is_opaque_reasoning(item))
        .cloned()
        .collect();
    if items.is_empty() {
        return Ok(output);
    }
    if raw.get("model").and_then(Value::as_str) != Some(provider.model_name.as_str()) {
        return Err("RESPONSES_REASONING_MODEL_MISMATCH".into());
    }
    let mut ids = std::collections::HashSet::new();
    if items
        .iter()
        .any(|item| !ids.insert(item["id"].as_str().unwrap()))
    {
        return Err("RESPONSES_REASONING_DUPLICATE_ID".into());
    }
    if let Some(text) = output.as_str() {
        output = json!({"text": text});
    }
    let object = output
        .as_object_mut()
        .ok_or("RESPONSES_REASONING_OUTPUT_SHAPE_INVALID")?;
    if object.contains_key(FIELD) {
        return Err("RESPONSES_REASONING_RESERVED_FIELD".into());
    }
    object.insert(
        FIELD.into(),
        json!({
            "schema_version": SCHEMA, "provider": provider.llm_provider_name,
            "model": provider.model_name, "route": provider.provider_url_name, "items": items,
        }),
    );
    Ok(output)
}

pub(crate) fn capture_reasoning(
    output: Value,
    raw: &Value,
    provider: &RuntimeProviderConfig,
) -> Value {
    let mut captured = match attach_reasoning(output.clone(), raw, provider) {
        Ok(attached) => attached,
        Err(error) => {
            // Streamed effects may already be complete. Keep their receipts and
            // checkpoint a continuity barrier instead of failing the tool turn.
            let mut preserved = match output {
                Value::String(text) => json!({"text": text}),
                other => other,
            };
            if let Some(object) = preserved.as_object_mut() {
                object.insert(
                    FIELD.into(),
                    json!({
                        "schema_version": SCHEMA, "provider": provider.llm_provider_name,
                        "model": provider.model_name, "route": provider.provider_url_name,
                        "items": [], "error": error,
                    }),
                );
            }
            preserved
        }
    };
    if provider.llm_provider_name == "codex" {
        if let Value::String(text) = &captured {
            captured = json!({"text": text});
        }
        let observation = reasoning_observation(raw, &captured);
        if let Some(object) = captured.as_object_mut() {
            object.insert("responses_continuity_observation".into(), observation);
        }
    }
    captured
}

fn reasoning_observation(raw: &Value, captured: &Value) -> Value {
    let output = raw.get("output").and_then(Value::as_array);
    let reasoning = output.map(|items| {
        items
            .iter()
            .filter(|item| item["type"] == "reasoning")
            .collect::<Vec<_>>()
    });
    let encrypted = reasoning.as_ref().map(|items| {
        items
            .iter()
            .filter(|item| {
                item["encrypted_content"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty())
            })
            .count()
    });
    let replayable = reasoning.as_ref().map(|items| {
        items
            .iter()
            .filter(|item| is_opaque_reasoning(item))
            .count()
    });
    let status = raw
        .get("status")
        .and_then(Value::as_str)
        .filter(|s| s.len() <= 256 && !s.chars().any(char::is_control));
    let packet = captured.get(FIELD);
    let captured_count = packet
        .and_then(|p| p["items"].as_array())
        .map_or(0, Vec::len);
    let disposition = if packet.is_some_and(|p| p.get("error").is_some()) {
        "continuity_barrier"
    } else if captured_count > 0 {
        "captured"
    } else if status != Some("completed") {
        "response_not_completed"
    } else if output.is_none() {
        "output_unobserved"
    } else if reasoning.as_ref().is_some_and(Vec::is_empty) {
        "no_reasoning_items"
    } else if encrypted == Some(0) {
        "encrypted_content_absent"
    } else {
        "opaque_shape_invalid"
    };
    json!({
        "schema_version": "responses_continuity_observation_v1",
        "source": "provider_response", "response_status": status,
        "observed_reasoning_effort": raw.pointer("/reasoning/effort").and_then(Value::as_str)
            .filter(|s| s.len() <= 256 && !s.chars().any(char::is_control)),
        "output_item_count": output.map(Vec::len),
        "reasoning_item_count": reasoning.as_ref().map(Vec::len),
        "encrypted_reasoning_item_count": encrypted, "replayable_reasoning_item_count": replayable,
        "captured_reasoning_item_count": captured_count, "disposition": disposition,
    })
}

pub(crate) fn accumulate_reasoning(
    session: &mut SessionManagement,
    runtime: &RuntimeAggregate,
) -> Result<(), String> {
    if runtime.call_result_status() != RuntimeCallResultStatus::Succeeded {
        return Ok(());
    }
    let Some(packet) = runtime.output.as_ref().and_then(|output| output.get(FIELD)) else {
        return Ok(());
    };
    let record = json!({"type": FIELD, "runtime_id": runtime.runtime_id, "packet": packet});
    if let Some(previous) = session
        .session_log
        .iter()
        .map(|entry| entry.value())
        .find(|value| {
            value.get("type").and_then(Value::as_str) == Some(FIELD)
                && value.get("runtime_id").and_then(Value::as_str)
                    == Some(runtime.runtime_id.as_str())
        })
    {
        if previous != &record {
            return Err("RESPONSES_REASONING_CHECKPOINT_CONFLICT".into());
        }
        return Ok(());
    }
    session.push_log(record.to_string(), chrono::Utc::now());
    Ok(())
}

pub(crate) fn record_items(
    record: &Value,
    provider: Option<&RuntimeProviderConfig>,
) -> Option<Vec<Value>> {
    if record.get("type").and_then(Value::as_str) != Some(FIELD) {
        return None;
    }
    let Some(provider) = provider else {
        return Some(Vec::new());
    };
    let packet = &record["packet"];
    if provider.llm_provider_name != "codex"
        || packet["schema_version"] != SCHEMA
        || packet["provider"] != provider.llm_provider_name
        || packet["model"] != provider.model_name
        || packet["route"] != provider.provider_url_name
    {
        return Some(Vec::new());
    }
    let Some(items) = packet["items"].as_array() else {
        return Some(Vec::new());
    };
    if items.iter().any(|item| !is_opaque_reasoning(item)) {
        return Some(Vec::new());
    }
    Some(items.clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    use lifecycle::{ProviderConfig, ToolChoice};

    fn provider() -> RuntimeProviderConfig {
        RuntimeProviderConfig {
            base: ProviderConfig {
                tura_llm_name: "codex/model".into(),
                default_model_tier: None,
                current_model: None,
                stream: true,
                temperature: 0.0,
                max_tokens: 0,
                tool_choice: ToolChoice::Auto,
                time_out_ms: 1000,
            },
            thinking: true,
            provider_name: "codex/model".into(),
            model_name: "model".into(),
            provider_url_name: "route".into(),
            llm_provider_name: "codex".into(),
        }
    }

    fn item(id: &str) -> Value {
        json!({"type":"reasoning", "id":id, "summary":[],
            "encrypted_content":format!("opaque-{id}"), "status":"completed"})
    }

    #[test]
    fn reasoning_observation_distinguishes_absence_shape_and_unknown_without_content() {
        for (raw, reason, count) in [
            (
                json!({"status":"completed", "output":[]}),
                "no_reasoning_items",
                json!(0),
            ),
            (
                json!({"status":"completed"}),
                "output_unobserved",
                Value::Null,
            ),
            (json!({"output":[]}), "response_not_completed", json!(0)),
            (
                json!({"status":"completed", "output":[{"type":"reasoning", "summary":[{"text":"private"}]}]}),
                "encrypted_content_absent",
                json!(1),
            ),
            (
                json!({"status":"completed", "output":[{"type":"reasoning", "encrypted_content":"private"}]}),
                "opaque_shape_invalid",
                json!(1),
            ),
        ] {
            let captured = capture_reasoning(
                json!({"streamed_command_run_result":{"results":["done"]}}),
                &raw,
                &provider(),
            );
            assert_eq!(
                captured["streamed_command_run_result"]["results"],
                json!(["done"])
            );
            let observed = &captured["responses_continuity_observation"];
            assert_eq!(observed["disposition"], reason);
            assert_eq!(observed["reasoning_item_count"], count);
            assert!(!observed.to_string().contains("private"));
            assert!(captured.get(FIELD).is_none());
        }
    }

    #[test]
    fn reasoning_observation_preserves_replay_and_completed_receipts() {
        let raw = json!({"status":"completed", "model":"model", "output":[item("r1")]});
        let captured = capture_reasoning(
            json!({"provider_content":{"tool_calls":[]}}),
            &raw,
            &provider(),
        );
        assert_eq!(captured[FIELD]["items"], raw["output"]);
        assert_eq!(captured["provider_content"], json!({"tool_calls":[]}));
        let observed = &captured["responses_continuity_observation"];
        assert_eq!(observed["disposition"], "captured");
        assert_eq!(observed["replayable_reasoning_item_count"], 1);
        assert_eq!(observed["captured_reasoning_item_count"], 1);
        assert!(!observed.to_string().contains("opaque-r1"));
        let mut incompatible = raw.clone();
        incompatible["model"] = json!("other");
        let barrier = capture_reasoning(json!("done"), &incompatible, &provider());
        assert_eq!(barrier["text"], "done");
        assert_eq!(
            barrier["responses_continuity_observation"]["disposition"],
            "continuity_barrier"
        );
    }

    #[test]
    fn responses_continuity_preserves_exact_order_and_visible_output() {
        let items = vec![item("r1"), item("r2")];
        let raw = json!({"status":"completed", "model":"model", "output":items});
        let output = attach_reasoning(json!("visible answer"), &raw, &provider()).unwrap();
        assert_eq!(output["text"], "visible answer");
        assert_eq!(output[FIELD]["items"], raw["output"]);
        let record = json!({"type":FIELD, "packet":output[FIELD]});
        assert_eq!(record_items(&record, Some(&provider())), Some(items));
        assert_eq!(record_items(&record, None), Some(Vec::new()));
    }

    #[test]
    fn responses_continuity_rejects_model_mismatch_and_duplicate_identity() {
        let mut raw = json!({"status":"completed", "model":"wrong", "output":[item("r1")]});
        assert_eq!(
            attach_reasoning(json!({}), &raw, &provider()).unwrap_err(),
            "RESPONSES_REASONING_MODEL_MISMATCH"
        );
        raw["model"] = json!("model");
        raw["output"] = json!([item("r1"), item("r1")]);
        assert_eq!(
            attach_reasoning(json!({}), &raw, &provider()).unwrap_err(),
            "RESPONSES_REASONING_DUPLICATE_ID"
        );
    }

    #[test]
    fn responses_continuity_does_not_attach_partial_plaintext_or_other_provider() {
        let output = json!({"tool_calls":[]});
        for status in ["in_progress", "failed", "incomplete"] {
            let raw = json!({"status":status, "model":"model", "output":[item("r1")]});
            assert_eq!(
                attach_reasoning(output.clone(), &raw, &provider()).unwrap(),
                output
            );
        }
        let raw = json!({"status":"completed", "model":"model",
            "output":[{"type":"reasoning", "id":"r1", "summary":[{"text":"plaintext"}]}]});
        assert_eq!(
            attach_reasoning(output.clone(), &raw, &provider()).unwrap(),
            output
        );
        let mut other = provider();
        other.llm_provider_name = "google".into();
        let raw = json!({"status":"completed", "model":"model", "output":[item("r1")]});
        assert_eq!(
            attach_reasoning(output.clone(), &raw, &other).unwrap(),
            output
        );
    }

    #[test]
    fn responses_continuity_checkpoint_serialization_and_route_binding() {
        let output = attach_reasoning(
            json!({"tool_calls":[]}),
            &json!({"status":"completed", "model":"model", "output":[item("r1")]}),
            &provider(),
        )
        .unwrap();
        let record = json!({"type":FIELD,"packet":output[FIELD]});
        let restored: Value = serde_json::from_str(&record.to_string()).unwrap();
        assert_eq!(
            record_items(&restored, Some(&provider())),
            Some(vec![item("r1")])
        );
        let mut changed = provider();
        changed.model_name = "different".into();
        assert_eq!(record_items(&restored, Some(&changed)), Some(vec![]));
        changed = provider();
        changed.provider_url_name = "different-route".into();
        assert_eq!(record_items(&restored, Some(&changed)), Some(vec![]));
    }

    #[test]
    fn responses_continuity_invalid_packet_preserves_completed_effects() {
        let original = json!({"provider_content":{"tool_calls":[]},
            "streamed_command_run_result":{"call_id":"call-1",
                "receipt":"original-receipt", "execution_count":1, "exit_code":0}});
        let raw = json!({"status":"completed", "model":"wrong", "output":[item("r1")]});
        let captured = capture_reasoning(original.clone(), &raw, &provider());
        assert_eq!(captured["provider_content"], original["provider_content"]);
        assert_eq!(
            captured["streamed_command_run_result"],
            original["streamed_command_run_result"]
        );
        assert_eq!(
            captured[FIELD]["error"],
            "RESPONSES_REASONING_MODEL_MISMATCH"
        );
        let record = json!({"type":FIELD, "packet":captured[FIELD]});
        assert_eq!(record_items(&record, Some(&provider())), Some(vec![]));
    }
}
