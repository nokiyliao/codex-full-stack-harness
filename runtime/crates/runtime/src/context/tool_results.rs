use super::char_budget::{
    COMMAND_RUN_RESULT_OUTPUT_MAX_CHARS, CONTEXT_OUTPUT_MAX_CHARS, context_output_byte_budget,
    formatted_truncate_text, truncate_middle_with_char_budget,
};
use crate::tool_callback_sanitizer::{
    bounded_source_postimages_for_context, bounded_source_read_page, command_run_context_commands,
    command_run_results_channel, command_run_tool_identity,
};
use lifecycle::SessionManagement;

use super::media::{command_run_media_content_items_for_context, strip_read_media_payload_data};

pub(super) fn strip_context_reporting_fields(value: serde_json::Value) -> serde_json::Value {
    strip_context_reporting_fields_inner(value, false)
}

fn strip_context_reporting_fields_inner(
    value: serde_json::Value,
    task_status_context: bool,
) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => {
            let command_is_task_status = object_is_task_status_command(&map);
            let preserve_task_status_fields = task_status_context || command_is_task_status;
            serde_json::Value::Object(
                map.into_iter()
                    .filter(|(key, _)| {
                        !is_context_reporting_field_for_context(key, preserve_task_status_fields)
                    })
                    .map(|(key, value)| {
                        let child_task_status_context =
                            preserve_task_status_fields || key == "task_status";
                        let value = if key == "source_postimages" {
                            // Only a canonical successful verifier result can
                            // restore a validated projection at its output site.
                            bounded_source_postimages_for_context(&value, None, None)
                        } else {
                            strip_context_reporting_fields_inner(value, child_task_status_context)
                        };
                        (key, value)
                    })
                    .collect(),
            )
        }
        serde_json::Value::Array(items) => serde_json::Value::Array(
            items
                .into_iter()
                .map(|item| strip_context_reporting_fields_inner(item, task_status_context))
                .collect(),
        ),
        other => other,
    }
}

fn object_is_task_status_command(map: &serde_json::Map<String, serde_json::Value>) -> bool {
    map.get("command_type")
        .or_else(|| map.get("command"))
        .or_else(|| map.get("command_name"))
        .or_else(|| map.get("tool_name"))
        .and_then(serde_json::Value::as_str)
        .map(|value| value.trim().to_ascii_lowercase().replace('-', "_"))
        .as_deref()
        == Some("task_status")
}

fn is_context_reporting_field_for_context(key: &str, task_status_context: bool) -> bool {
    if task_status_context && is_task_status_context_field(key) {
        return false;
    }
    is_context_reporting_field(key)
}

fn is_task_status_context_field(key: &str) -> bool {
    matches!(
        key,
        "command" | "task_group" | "task_type" | "status" | "compact_context"
    )
}

fn is_context_reporting_field(key: &str) -> bool {
    matches!(
        key,
        "task_group"
            | "step_summary"
            | "last_tool_call_status"
            | "last_tool_call_summary"
            | "summary"
            | "description"
            | "interface"
            | "used_prompt"
            | "notes"
            | "receipt"
            | "should_register_tool"
            | "command_id"
            | "command_run_id"
            | "provider_tool_call_id"
            | "command_index"
            | "result_index"
            | "command"
            | "command_updates"
            | "messageID"
            | "partID"
            | "runtimeID"
            | "commandRunID"
            | "commandID"
            | "providerToolCallID"
            | "commandIndex"
            | "eventSeq"
            | "createdAt"
            | "updatedAt"
            | "runtime_id"
            | "created_at"
            | "updated_at"
            | "timestamp"
    )
}

fn strip_command_run_context_noise(value: serde_json::Value) -> serde_json::Value {
    strip_context_reporting_fields(value)
}

pub(super) fn last_tool_call_response_from_session(
    session: &SessionManagement,
) -> Option<serde_json::Value> {
    session
        .session_log
        .iter()
        .rev()
        .map(|entry| entry.value())
        .find(|value| value.get("type").and_then(|kind| kind.as_str()) == Some("tool_result"))
        .map(|value| {
            serde_json::json!({
                "tool_name": value.get("tool_name").cloned().unwrap_or(serde_json::Value::Null),
                "input": compact_json_for_context(strip_context_reporting_fields(value.get("input").cloned().unwrap_or(serde_json::Value::Null))),
                "output": cached_context_output_for_tool_result(value),
                "success": value.get("success").cloned().unwrap_or(serde_json::Value::Bool(true)),
                "error": cached_context_error_for_tool_result(value),
            })
        })
}

pub(super) fn tool_result_context_cache(value: &serde_json::Value) -> serde_json::Value {
    let output = compact_context_output_for_tool_result(value);
    let error = compact_json_for_context(
        value
            .get("error")
            .cloned()
            .unwrap_or(serde_json::Value::Null),
    );
    let cache_id_input = serde_json::json!({
        "version": 1,
        "sequence": value.get("sequence").cloned().unwrap_or(serde_json::Value::Null),
        "tool_name": value.get("tool_name").cloned().unwrap_or(serde_json::Value::Null),
        "input": compact_json_for_context(strip_context_reporting_fields(value.get("input").cloned().unwrap_or(serde_json::Value::Null))),
        "output": output,
        "success": value.get("success").cloned().unwrap_or(serde_json::Value::Bool(true)),
        "error": error,
    });
    serde_json::json!({
        "version": 1,
        "cache_id": stable_context_cache_id(&cache_id_input),
        "output": output,
        "error": error,
    })
}

pub(super) fn immutable_tool_result_context_message(
    value: &serde_json::Value,
) -> serde_json::Value {
    if value.get("tool_name").and_then(|name| name.as_str()) == Some("command_run") {
        return command_run_function_output_context_message(value);
    }
    serde_json::json!({
        "role": "user",
        "content": compact_json_to_string(&serde_json::json!([immutable_tool_result_context_item(value)])),
    })
}

pub(super) fn immutable_tool_result_context_messages(
    value: &serde_json::Value,
) -> Vec<serde_json::Value> {
    if value.get("tool_name").and_then(|name| name.as_str()) == Some("command_run") {
        return command_run_provider_context_items(value);
    }
    vec![immutable_tool_result_context_message(value)]
}

pub(super) fn command_run_cached_context_messages_are_valid(
    messages: &[serde_json::Value],
) -> bool {
    let mut seen_calls = std::collections::HashSet::new();
    for message in messages {
        match message.get("type").and_then(serde_json::Value::as_str) {
            Some("function_call") => {
                if message.get("name").and_then(serde_json::Value::as_str) == Some("command_run") {
                    let Some(call_id) = message
                        .get("call_id")
                        .and_then(serde_json::Value::as_str)
                        .filter(|value| !value.trim().is_empty())
                    else {
                        return false;
                    };
                    let Some(arguments) = message
                        .get("arguments")
                        .and_then(serde_json::Value::as_str)
                        .and_then(|arguments| {
                            serde_json::from_str::<serde_json::Value>(arguments).ok()
                        })
                    else {
                        return false;
                    };
                    if !(arguments.get("commands").is_some() || arguments.get("steps").is_some()) {
                        return false;
                    }
                    seen_calls.insert(call_id.to_string());
                }
            }
            Some("function_call_output") => {
                let Some(call_id) = message
                    .get("call_id")
                    .and_then(serde_json::Value::as_str)
                    .filter(|value| !value.trim().is_empty())
                else {
                    return false;
                };
                if !seen_calls.contains(call_id) {
                    return false;
                }
                if command_run_function_output_contains_command_identity(message) {
                    return false;
                }
            }
            _ => {}
        }
    }
    true
}

fn command_run_function_output_contains_command_identity(message: &serde_json::Value) -> bool {
    let Some(output) = message.get("output") else {
        return false;
    };
    match output {
        serde_json::Value::String(text) => serde_json::from_str::<serde_json::Value>(text)
            .ok()
            .is_some_and(|value| value_contains_command_identity(&value)),
        serde_json::Value::Array(items) => items.iter().any(|item| {
            item.get("text")
                .and_then(serde_json::Value::as_str)
                .and_then(|text| serde_json::from_str::<serde_json::Value>(text).ok())
                .is_some_and(|value| value_contains_command_identity(&value))
        }),
        other => value_contains_command_identity(other),
    }
}

fn value_contains_command_identity(value: &serde_json::Value) -> bool {
    match value {
        serde_json::Value::Object(map) => map.iter().any(|(field, value)| {
            matches!(field.as_str(), "step" | "command_type" | "command_line")
                || value_contains_command_identity(value)
        }),
        serde_json::Value::Array(items) => items.iter().any(value_contains_command_identity),
        _ => false,
    }
}

fn command_run_provider_context_items(value: &serde_json::Value) -> Vec<serde_json::Value> {
    let Some(call_id) = command_run_provider_call_id(value) else {
        return vec![command_run_function_output_context_message(value)];
    };
    vec![
        serde_json::json!({
            "type": "function_call",
            "call_id": call_id,
            "name": "command_run",
            "arguments": command_run_function_arguments_for_context(value),
        }),
        serde_json::json!({
            "type": "function_call_output",
            "call_id": call_id,
            "output": command_run_function_output_payload_for_context(value),
        }),
    ]
}

fn command_run_function_output_context_message(value: &serde_json::Value) -> serde_json::Value {
    if let Some(content) = command_run_media_content_items_for_context(value) {
        return serde_json::json!({
            "role": "user",
            "content": content,
        });
    }
    serde_json::json!({
        "role": "user",
        "content": command_run_function_output_for_context(value),
    })
}

fn command_run_provider_call_id(value: &serde_json::Value) -> Option<String> {
    let metadata = value.get("provider_metadata")?;
    metadata
        .get("call_id")
        .or_else(|| metadata.get("id"))
        .and_then(serde_json::Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
}

fn command_run_function_arguments_for_context(value: &serde_json::Value) -> String {
    value
        .get("input")
        .cloned()
        .map(strip_tool_reporting_fields)
        .and_then(|input| serde_json::to_string(&input).ok())
        .unwrap_or_else(|| "{}".to_string())
}

pub(super) fn command_run_function_output_for_context(value: &serde_json::Value) -> String {
    command_run_current_style_output_for_context(value)
}

pub(super) fn command_run_function_output_payload_for_context(
    value: &serde_json::Value,
) -> serde_json::Value {
    if let Some(content) = command_run_media_content_items_for_context(value) {
        return serde_json::Value::Array(content);
    }
    serde_json::Value::String(command_run_function_output_for_context(value))
}

fn command_run_current_style_output_for_context(value: &serde_json::Value) -> String {
    command_run_current_style_output_string(value).unwrap_or_else(|| {
        let output = value.get("output").unwrap_or(&serde_json::Value::Null);
        let output = strip_command_run_context_noise(output.clone());
        serde_json::to_string(&output).unwrap_or_else(|_| output.to_string())
    })
}

pub(super) fn command_run_current_style_output_string(value: &serde_json::Value) -> Option<String> {
    let output = value.get("output").unwrap_or(&serde_json::Value::Null);
    // Guard postimages against the untouched input, including streamed IDs.
    let input = value.get("input").unwrap_or(&serde_json::Value::Null);
    let input_commands = input
        .get("commands")
        .and_then(|commands| commands.as_array())
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let input_commands = command_run_results_channel(output)
        .filter(|_| command_run_tool_identity(value))
        .and_then(|channel| command_run_context_commands(channel, Some(input_commands)));
    let results = flattened_command_run_results(output);
    if results.is_empty() {
        return None;
    }
    let results = results
        .into_iter()
        .enumerate()
        .map(|(index, result)| {
            command_run_context_result(
                result,
                input_commands.and_then(|commands| commands.get(index)),
            ).0
        })
        .collect::<Vec<_>>();
    let output = serde_json::json!({ "results": results });
    serde_json::to_string(&output).ok()
}

fn immutable_tool_result_context_item(value: &serde_json::Value) -> serde_json::Value {
    let item = serde_json::json!({
        "type": "tool_result",
        "cache_id": value
            .get("context_cache")
            .and_then(|cache| cache.get("cache_id"))
            .cloned()
            .unwrap_or(serde_json::Value::Null),
        "tool_name": value.get("tool_name").cloned().unwrap_or(serde_json::Value::Null),
        "input": compact_json_for_context(strip_context_reporting_fields(value.get("input").cloned().unwrap_or(serde_json::Value::Null))),
        "output": cached_context_output_for_tool_result(value),
        "success": value.get("success").cloned().unwrap_or(serde_json::Value::Bool(true)),
        "error": cached_context_error_for_tool_result(value),
    });
    item
}

fn stable_context_cache_id(value: &serde_json::Value) -> String {
    let serialized = serde_json::to_string(value).unwrap_or_else(|_| value.to_string());
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in serialized.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn cached_context_output_for_tool_result(value: &serde_json::Value) -> serde_json::Value {
    value
        .get("context_cache")
        .and_then(|cache| cache.get("output"))
        .cloned()
        .unwrap_or_else(|| compact_context_output_for_tool_result(value))
}

fn compact_context_output_for_tool_result(value: &serde_json::Value) -> serde_json::Value {
    let (output, preserve_source_page) = context_output_for_tool_result(value);
    // command_run_context_result has already bounded each ordinary field and
    // used the shared guards at the permitted postimage/stdout sites. Recompacting
    // this generated summary as a whole would destroy the validated bounded text.
    if preserve_source_page {
        return output;
    }
    if value.get("tool_name").and_then(serde_json::Value::as_str) == Some("command_run")
        && output.get("results").and_then(serde_json::Value::as_array).is_some_and(|results| {
            results.iter().any(|result| {
                result.get("output")
                    .and_then(|output| output.get("source_postimages"))
                    .and_then(|projection| projection.get("schema_version"))
                    .and_then(serde_json::Value::as_str)
                    .is_some_and(|schema| matches!(schema, "nokiy_source_postimages_v1" | "nokiy_source_postimage_delta_v1" | "nokiy_source_postimage_delta_v2"))
            })
        })
    {
        return output;
    }
    compact_json_for_context(output)
}

fn cached_context_error_for_tool_result(value: &serde_json::Value) -> serde_json::Value {
    value
        .get("context_cache")
        .and_then(|cache| cache.get("error"))
        .cloned()
        .unwrap_or_else(|| {
            compact_json_for_context(
                value
                    .get("error")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null),
            )
        })
}

fn context_output_for_tool_result(value: &serde_json::Value) -> (serde_json::Value, bool) {
    if value.get("tool_name").and_then(|name| name.as_str()) == Some("command_run") {
        return command_run_summary_with_source_page_for_context(value);
    }
    (strip_command_run_context_noise(
        value
            .get("output")
            .cloned()
            .unwrap_or(serde_json::Value::Null),
    ), false)
}

pub(super) fn command_run_summary_for_context(value: &serde_json::Value) -> serde_json::Value {
    command_run_summary_with_source_page_for_context(value).0
}

fn command_run_summary_with_source_page_for_context(
    value: &serde_json::Value,
) -> (serde_json::Value, bool) {
    let output = value.get("output").unwrap_or(&serde_json::Value::Null);
    let input = value.get("input").unwrap_or(&serde_json::Value::Null);
    let input_commands = input
        .get("commands")
        .and_then(|commands| commands.as_array())
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let input_commands = command_run_results_channel(output)
        .filter(|_| command_run_tool_identity(value))
        .and_then(|channel| command_run_context_commands(channel, Some(input_commands)));
    let flattened = flattened_command_run_results(output);
    if flattened.is_empty() {
        let mut output = strip_context_reporting_fields(output.clone());
        strip_read_media_payload_data(&mut output);
        return (strip_command_run_context_noise(output), false);
    }

    let mut preserve_source_page = false;
    let results = flattened
        .into_iter()
        .enumerate()
        .map(|(index, result)| {
            let (projection, source_page) = command_run_context_result(
                result,
                input_commands.and_then(|commands| commands.get(index)),
            );
            preserve_source_page |= source_page;
            projection
        })
        .collect::<Vec<_>>();
    (serde_json::json!({ "results": results }), preserve_source_page)
}

pub(super) fn flattened_command_run_results(output: &serde_json::Value) -> Vec<&serde_json::Value> {
    let Some(channel) = command_run_results_channel(output) else {
        return Vec::new();
    };
    let results = channel["results"].as_array().unwrap();
    let mut flattened = Vec::new();
    for result in results {
        if result.get("mode").and_then(|mode| mode.as_str()) == Some("batch")
            && command_run_results_channel(result).is_some()
            && let Some(batch_results) =
                result.get("results").and_then(|results| results.as_array())
        {
            flattened.extend(batch_results);
            continue;
        }
        flattened.push(result);
    }
    flattened
}

fn command_run_context_result(
    result: &serde_json::Value,
    input_command: Option<&serde_json::Value>,
) -> (serde_json::Value, bool) {
    // Validate against the untouched observation, before reporting/media
    // stripping can hide unknown schema fields. Preserve existing command
    // normalization/compaction on everything except the guarded output fields.
    let preserve_source_page = bounded_source_read_page(
        result,
        Some(input_command.unwrap_or(&serde_json::Value::Null)),
    ).is_some();
    let postimages = result.get("output")
        .and_then(|output| output.get("source_postimages"))
        .map(|projection| bounded_source_postimages_for_context(
            projection,
            Some(result),
            Some(input_command.unwrap_or(&serde_json::Value::Null)),
        ));
    let mut cleaned_result = strip_context_reporting_fields(result.clone());
    strip_read_media_payload_data(&mut cleaned_result);
    let result = &cleaned_result;
    let mut item = serde_json::Map::new();
    let command_type = result
        .get("command_type")
        .or_else(|| result.get("command"))
        .or_else(|| result.get("command_name"))
        .or_else(|| result.get("tool_name"))
        .or_else(|| input_command.and_then(|input| input.get("command_type")))
        .or_else(|| input_command.and_then(|input| input.get("command")))
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    let command_type_name = command_type.as_str().map(ToString::to_string);
    item.insert(
        "success".to_string(),
        result
            .get("success")
            .cloned()
            .unwrap_or(serde_json::Value::Null),
    );
    if let Some(output) = result.get("output") {
        let mut output = strip_command_run_context_noise(output.clone());
        if command_type_name.as_deref() == Some("apply_patch") {
            output = summarize_apply_patch_output_for_context(output);
        } else if command_type_name.as_deref() == Some("source_read")
            && result
                .get("command_type")
                .and_then(serde_json::Value::as_str)
                == Some("source_read")
            && input_command
                .and_then(|input| input.get("command_type"))
                .and_then(serde_json::Value::as_str)
                == Some("source_read")
            && is_completed_source_read_result(result)
        {
            omit_source_read_audit_fields_for_context(&mut output);
        }
        item.insert(
            "output".to_string(),
            compact_command_run_context_value(output, postimages, preserve_source_page),
        );
    }
    if let Some(error) = result.get("error") {
        item.insert(
            "error".to_string(),
            compact_command_run_context_value(strip_command_run_context_noise(error.clone()), None, false),
        );
    }
    (serde_json::Value::Object(item), preserve_source_page)
}

fn is_completed_source_read_result(result: &serde_json::Value) -> bool {
    if result.get("success").and_then(serde_json::Value::as_bool) != Some(true) {
        return false;
    }
    let Some(output) = result.get("output") else {
        return false;
    };
    if output.get("exit_code").and_then(serde_json::Value::as_i64) != Some(0) {
        return false;
    }
    let Some(receipt) = output.get("terminal_receipt") else {
        return false;
    };
    [
        ("schema_version", "tura_command_terminal_receipt_v1"),
        ("termination_origin", "in_process_source_read"),
        ("outcome", "known"),
        ("terminal_state", "completed"),
        ("failure_class", "none"),
        ("authority_effect", "none"),
        ("staging_authority", "none"),
    ]
    .iter()
    .all(|(key, expected)| receipt.get(*key).and_then(serde_json::Value::as_str) == Some(*expected))
        && receipt.get("exit_code").and_then(serde_json::Value::as_i64) == Some(0)
        && ["reconcile_required", "retry_safe", "auto_retry_allowed"]
            .iter()
            .all(|key| receipt.get(*key).and_then(serde_json::Value::as_bool) == Some(false))
        && [
            "process_reaped",
            "process_group_empty",
            "termination_proven",
        ]
        .iter()
        .all(|key| receipt.get(*key).and_then(serde_json::Value::as_bool) == Some(true))
}

// Only remove these fields at their known audit locations, never in source payloads.
// The completion guard has already checked the duplicated terminal facts below;
// keep authority/replay decisions and any unrecognized receipt fields visible.
fn omit_source_read_audit_fields_for_context(output: &mut serde_json::Value) {
    let Some(output) = output.as_object_mut() else {
        return;
    };
    output.remove("terminal_receipt_path");
    if let Some(receipt) = output
        .get_mut("terminal_receipt")
        .and_then(serde_json::Value::as_object_mut)
    {
        for key in [
            "schema_version",
            "call_id",
            "pid",
            "wall_time_ms",
            "wall_timeout_ms",
            "stall_timeout_ms",
            "termination_origin",
            "outcome",
            "terminal_state",
            "exit_code",
            "failure_class",
            "process_reaped",
            "process_group_empty",
            "termination_proven",
        ] {
            receipt.remove(key);
        }
    }
}

fn summarize_apply_patch_output_for_context(value: serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) if looks_like_patch_change(&map) => {
            summarize_patch_change_object(map)
        }
        serde_json::Value::Object(map) => serde_json::Value::Object(
            map.into_iter()
                .map(|(key, value)| {
                    let value = match key.as_str() {
                        "changes" => summarize_patch_changes_value(value),
                        "failed_change" => summarize_patch_change_value(value),
                        _ => summarize_apply_patch_output_for_context(value),
                    };
                    (key, value)
                })
                .collect(),
        ),
        serde_json::Value::Array(items) => serde_json::Value::Array(
            items
                .into_iter()
                .map(summarize_apply_patch_output_for_context)
                .collect(),
        ),
        other => other,
    }
}

fn summarize_patch_changes_value(value: serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Array(items) => serde_json::Value::Array(
            items
                .into_iter()
                .map(summarize_patch_change_value)
                .collect(),
        ),
        other => summarize_patch_change_value(other),
    }
}

fn summarize_patch_change_value(value: serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => summarize_patch_change_object(map),
        other => serde_json::json!({
            "omitted_from_context": true,
            "value_type": json_value_type(&other),
        }),
    }
}

fn summarize_patch_change_object(
    map: serde_json::Map<String, serde_json::Value>,
) -> serde_json::Value {
    let hunk_count = map
        .get("hunks")
        .and_then(serde_json::Value::as_array)
        .map(|items| items.len());
    let line_count = map.get("hunks").map(count_patch_hunk_lines).unwrap_or(0);
    let mut summary = serde_json::Map::new();
    for key in ["kind", "path"] {
        if let Some(value) = map.get(key).cloned() {
            summary.insert(key.to_string(), value);
        }
    }
    if let Some(count) = hunk_count {
        summary.insert("hunk_count".to_string(), serde_json::json!(count));
    }
    if line_count > 0 {
        summary.insert("line_count".to_string(), serde_json::json!(line_count));
    }
    summary.insert(
        "hunks_omitted_from_context".to_string(),
        serde_json::Value::Bool(true),
    );
    serde_json::Value::Object(summary)
}

fn looks_like_patch_change(map: &serde_json::Map<String, serde_json::Value>) -> bool {
    map.contains_key("hunks") && (map.contains_key("path") || map.contains_key("kind"))
}

fn count_patch_hunk_lines(value: &serde_json::Value) -> usize {
    match value {
        serde_json::Value::String(_) => 1,
        serde_json::Value::Array(items) => items.iter().map(count_patch_hunk_lines).sum(),
        serde_json::Value::Object(map) => map.values().map(count_patch_hunk_lines).sum(),
        _ => 0,
    }
}

fn json_value_type(value: &serde_json::Value) -> &'static str {
    match value {
        serde_json::Value::Null => "null",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(_) => "number",
        serde_json::Value::String(_) => "string",
        serde_json::Value::Array(_) => "array",
        serde_json::Value::Object(_) => "object",
    }
}

fn compact_command_run_context_value(
    value: serde_json::Value,
    mut postimages: Option<serde_json::Value>,
    preserve_source_stdout: bool,
) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => serde_json::Value::Object(
            map.into_iter()
                .map(|(key, value)| {
                    let value = if key == "source_postimages" {
                        postimages.take().unwrap_or_else(|| bounded_source_postimages_for_context(&value, None, None))
                    } else if key == "stdout" && preserve_source_stdout {
                        value
                    } else if matches!(key.as_str(), "stdout" | "stderr" | "output") {
                        compact_command_run_context_stream_value(value)
                    } else {
                        compact_json_for_context(value)
                    };
                    (key, value)
                })
                .collect(),
        ),
        other => compact_json_for_context(other),
    }
}

fn compact_command_run_context_stream_value(value: serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::String(text) => {
            if text.contains("Total output lines:") {
                if text.len() <= COMMAND_RUN_RESULT_OUTPUT_MAX_CHARS {
                    return serde_json::Value::String(text);
                }
                return serde_json::Value::String(truncate_middle_with_char_budget(
                    &text,
                    COMMAND_RUN_RESULT_OUTPUT_MAX_CHARS,
                ));
            }
            serde_json::Value::String(formatted_truncate_text(
                &text,
                COMMAND_RUN_RESULT_OUTPUT_MAX_CHARS,
            ))
        }
        other => compact_json_for_context(other),
    }
}

pub(crate) fn strip_tool_reporting_fields(value: serde_json::Value) -> serde_json::Value {
    strip_context_reporting_fields(value)
}

fn compact_json_for_context(value: serde_json::Value) -> serde_json::Value {
    let serialized = serde_json::to_string_pretty(&value).unwrap_or_else(|_| value.to_string());
    if serialized.len() <= context_output_byte_budget() {
        return value;
    }
    serde_json::Value::String(formatted_truncate_text(
        &serialized,
        CONTEXT_OUTPUT_MAX_CHARS,
    ))
}

fn compact_json_to_string(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(text) => text.clone(),
        other => serde_json::to_string_pretty(other).unwrap_or_else(|_| other.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::{
        command_run_cached_context_messages_are_valid, command_run_function_output_for_context,
        command_run_summary_for_context, immutable_tool_result_context_messages,
        tool_result_context_cache,
    };
    use serde_json::{Value, json};
    use crate::provider_flow::streamed_command_run::{
        streamed_command_event_record, streamed_command_result_record,
    };
    use crate::tool_callback_sanitizer::{
        SOURCE_POSTIMAGE_DELTA_NOTICE, SOURCE_POSTIMAGE_DELTA_V2_NOTICE_SUFFIX, SOURCE_POSTIMAGES_NOTICE,
        sanitize_tool_callback_output, sanitize_tool_callback_result,
    };
    use crate::tool_callback_sanitizer::tests::source_read_result;

    fn parse_command_run_context(text: &str) -> Value {
        serde_json::from_str(text).expect("command_run context should be structured JSON")
    }

    fn source_postimage_projection(delta: bool, text: &str, lines: u64) -> Value {
        let mut file = json!({
            "path": "src/example.py",
            "preimage_sha256": "1".repeat(64),
            "postimage_sha256": "2".repeat(64),
        });
        if delta {
            file["coverage"] = json!("all_text_changes");
            file["hunks"] = json!([{
                "before_start_line": 7, "before_line_count": 1,
                "after_start_line": 7, "after_line_count": lines,
                "before_text": "value = 'old'\r\n", "after_text": text,
            }]);
        } else {
            file["locators"] = json!([{
                "qualified_name": "value", "kind": "binding", "line": 7,
                "start_line": 7, "end_line": 7 + lines - 1, "complete": true,
            }]);
            file["spans"] = json!([{
                "start_line": 7, "end_line": 7 + lines - 1, "text": text,
            }]);
        }
        json!({
            "schema_version": if delta { "nokiy_source_postimage_delta_v1" } else { "nokiy_source_postimages_v1" },
            "kind": if delta { "verifier_postimage_delta" } else { "verifier_postimage_context" },
            "jspace_semantic_sha256": "3".repeat(64),
            "notice": if delta { SOURCE_POSTIMAGE_DELTA_NOTICE } else { SOURCE_POSTIMAGES_NOTICE },
            "files": [file],
        })
    }

    fn focused_verifier_postimage_result(projection: Value) -> Value {
        json!({
            "tool_name": "command_run", "sequence": 1, "success": true,
            "provider_metadata": {"call_id": "call_source_postimages"},
            "input": {"commands": [{"command_type": "focused_verifier", "command_line": {"verifier_index": 0}}]},
            "output": {"results": [{
                "command_type": "focused_verifier", "success": true,
                "output": {
                    "success": true, "exit_code": 0, "outcome": "known",
                    "process_reaped": true, "process_group_empty": true,
                    "facts": {"passed": 7}, "stdout": "verifier passed\n", "stderr": "",
                    "source_postimages": projection,
                },
            }]},
        })
    }

    fn with_source_postimage_navigation(mut projection: Value) -> Value {
        projection["schema_version"] = json!("nokiy_source_postimage_delta_v2");
        projection["notice"] = json!(format!("{SOURCE_POSTIMAGE_DELTA_NOTICE}{SOURCE_POSTIMAGE_DELTA_V2_NOTICE_SUFFIX}"));
        projection["files"][0]["locators"] = json!([
            {"qualified_name": "Example", "kind": "class", "line": 2, "start_line": 1, "end_line": 5},
            {"qualified_name": "Example.run", "kind": "async_function", "line": 3, "start_line": 2, "end_line": 5},
            {"qualified_name": "build", "kind": "function", "line": 21, "start_line": 20, "end_line": 30},
            {"qualified_name": "value", "kind": "binding", "line": 40, "start_line": 40, "end_line": 40},
        ]);
        projection
    }

    fn source_postimage_projections(text: &str, lines: u64) -> [Value; 3] {
        let delta = source_postimage_projection(true, text, lines);
        [source_postimage_projection(false, text, lines), delta.clone(), with_source_postimage_navigation(delta)]
    }

    fn parent_focused_verifier_postimage_result(projection: Value) -> Value {
        let mut raw = focused_verifier_postimage_result(projection);
        let result = &mut raw["output"]["results"][0];
        result["error"] = Value::Null;
        result["output"]["executor"] = json!("parent_focused_verifier");
        result["output"]["terminal_receipt"] = json!({
            "schema_version": "tura_command_terminal_receipt_v1",
            "exit_code": 0, "outcome": "known", "terminal_state": "completed",
            "process_reaped": true, "process_group_empty": true,
            "termination_proven": true, "reconcile_required": false,
            "termination_origin": "parent_verifier", "failure_class": "workload",
            "authoritative_publication": "unproven", "authority_effect": "none",
            "auto_retry_allowed": false, "call_id": "call_source_postimages", "pid": null,
            "replay_semantics": "diagnosed_replay_only_after_no_authoritative_publication_or_idempotent_cas_proof",
            "retry_safe": false, "staging_authority": "none", "stall_timeout_ms": null,
            "wall_time_ms": 491, "wall_timeout_ms": 30000,
        });
        raw
    }

    fn standalone_streamed_verifier(projection: Value) -> (Value, Value) {
        let raw = parent_focused_verifier_postimage_result(projection);
        let mut command = raw["input"]["commands"][0].clone();
        command["step"] = json!(2);
        command["command_index"] = json!(1);
        command["command_id"] = json!("stream:verifier");
        command["command_run_id"] = json!("stream");
        command["provider_tool_call_id"] = json!("call_source_postimages");
        let mut result = raw["output"]["results"][0].clone();
        for key in ["step", "command_index", "command_id", "command_run_id", "provider_tool_call_id"] {
            result[key] = command[key].clone();
        }
        result["command"] = command.clone();
        // Match the compact parent packet, not only the older redundant facts.
        for key in ["success", "outcome", "process_reaped", "process_group_empty"] {
            result["output"].as_object_mut().unwrap().remove(key);
        }
        (command, result)
    }

    #[test]
    fn streamed_source_postimages_preserve_standalone_results_and_completed_events() {
        let text = format!("value = '{}'\n", "x".repeat(11_000));
        for projection in source_postimage_projections(&text, 1) {
            let (command, result) = standalone_streamed_verifier(projection.clone());
            assert_eq!(sanitize_tool_callback_result(&result), result);
            assert_eq!(sanitize_tool_callback_output(&result), result);
            let now = chrono::Utc::now();
            let completed = streamed_command_result_record("completed", "runtime", 1, &result, now);
            assert_eq!(completed["command_index"], 1);
            assert_eq!(completed["result_index"], 1);
            assert_eq!(completed["step"], 2);
            for event in [completed, streamed_command_event_record(
                "completed", "runtime", "call_source_postimages", 1, &command, Some(&result), now,
            )] {
                assert_eq!(event["result"], result, "the real helper sanitizes before assembly");
                let sanitized = sanitize_tool_callback_result(&event);
                assert_eq!(sanitized, event, "event sanitation must preserve IDs and raw facts");
                assert_eq!(sanitize_tool_callback_result(&sanitized), sanitized);
                assert_eq!(sanitized["result"]["output"]["source_postimages"], projection);
            }
        }
    }

    #[test]
    fn streamed_source_postimages_mixed_steps_reach_callback_provider_and_cache() {
        let text = format!("value = '{}'\n", "x".repeat(11_000));
        for projection in source_postimage_projections(&text, 1) {
            let (command, verifier) = standalone_streamed_verifier(projection.clone());
            let mut patch = command.clone();
            patch["command_type"] = json!("apply_patch");
            patch["command_line"] = json!(concat!(
                "*** Begin Patch\n*** Update File: src/example.py\n@@\n",
                "-value = 'old'\n+value = 'new'\n*** End ", "Patch\n",
            ));
            patch["command_index"] = json!(0);
            patch["command_id"] = json!("stream:patch");
            patch["step"] = json!(1);
            let patch_result = json!({
                "command_type": "apply_patch", "command": patch, "step": 1,
                "command_id": "stream:patch", "command_index": 0,
                "command_run_id": "stream", "provider_tool_call_id": "call_source_postimages",
                "success": true, "error": null, "output": {"exit_code": 0, "stdout": "updated"},
            });
            let commands = json!([patch, command]);
            let now = chrono::Utc::now();
            let events: Vec<Value> = [patch_result, verifier.clone()].iter().enumerate()
                .map(|(index, result)| {
                    let event = streamed_command_result_record("completed", "runtime", index, result, now);
                    let sanitized = sanitize_tool_callback_result(&event);
                    assert_eq!(sanitized, event);
                    assert_eq!(sanitize_tool_callback_result(&sanitized), sanitized);
                    sanitized
                }).collect();
            let results: Vec<Value> = events.iter().map(|event| event["result"].clone()).collect();
            assert_eq!(results[1], verifier);
            for batch in [false, true] {
                let results = if batch { json!([{"mode": "batch", "results": results}]) } else { json!(results) };
                let wrapper = json!({"streamed_command_run_result": {
                    "commands": commands, "command_events": events, "results": results,
                }});
                let callback = sanitize_tool_callback_output(&wrapper);
                assert_eq!(callback["streamed_command_run_result"]["command_events"]["count"], 2);
                assert_eq!(sanitize_tool_callback_output(&callback), callback);
                for output in [wrapper, callback.clone(), callback["streamed_command_run_result"].clone()] {
                    let mut raw = parent_focused_verifier_postimage_result(projection.clone());
                    raw["input"]["commands"] = commands.clone();
                    raw["output"] = output;
                    let before = raw.clone();
                    let sanitized = sanitize_tool_callback_result(&raw);
                    assert_eq!(sanitize_tool_callback_result(&sanitized), sanitized);
                    assert_eq!(super::flattened_command_run_results(&sanitized["output"])[1], &verifier);
                    let (messages, provider) = paired_postimage_output(&sanitized);
                    assert_eq!(provider["results"][1]["output"]["source_postimages"], projection);
                    assert_eq!(provider["results"][1]["output"], verifier["output"]);
                    assert_eq!(paired_postimage_output(&raw).1, provider);
                    let cache = tool_result_context_cache(&sanitized);
                    assert_eq!(cache["output"], provider);
                    assert_eq!(tool_result_context_cache(&raw)["output"], provider);
                    let mut cached = sanitized.clone();
                    cached["context_cache"] = cache;
                    assert_eq!(immutable_tool_result_context_messages(&cached), messages);
                    assert_eq!(raw, before);
                }
            }
        }
    }

    fn assert_streamed_postimage_omitted(result: &Value) {
        let before = result.clone();
        let event = streamed_command_result_record("completed", "runtime", 1, result, chrono::Utc::now());
        for observed in [sanitize_tool_callback_result(result), sanitize_tool_callback_result(&event)["result"].clone()] {
            assert_eq!(observed["output"]["source_postimages"]["omitted_from_context"], true);
            let mut expected = before.clone();
            expected["output"]["source_postimages"] = observed["output"]["source_postimages"].clone();
            assert_eq!(observed, expected, "rejection must not rewrite IDs or verifier facts");
            assert_eq!(sanitize_tool_callback_result(&observed), observed);
        }
        assert_eq!(*result, before);
    }

    #[test]
    fn streamed_source_postimages_reject_failure_incomplete_receipts_and_identity_conflicts() {
        for projection in source_postimage_projections("value = 'new'\n", 1) {
            let (_, base) = standalone_streamed_verifier(projection);
            for (site, field, replacement) in [
                ("", "success", json!(false)), ("", "success", Value::Null),
                ("", "error", json!("failed")), ("", "command_type", json!("source_read")),
                ("", "tool_name", json!("other_tool")), ("", "tool", json!("other_tool")),
                ("", "input", json!({"command_type": "source_read"})),
                ("/command", "command_type", json!("apply_patch")),
                ("/command", "tool_name", json!("other_tool")),
                ("/command", "command_id", json!("another-command")),
                ("/output", "exit_code", json!(1)), ("/output", "outcome", json!("unknown")),
                ("/output", "executor", json!("other_verifier")),
                ("/output", "terminal_receipt", Value::Null),
                ("/output", "source_postimages", json!({"schema_version": "forged"})),
                ("/output/terminal_receipt", "outcome", json!("unknown")),
                ("/output/terminal_receipt", "process_reaped", json!(false)),
                ("/output/terminal_receipt", "reconcile_required", json!(true)),
            ] {
                let mut invalid = base.clone();
                invalid.pointer_mut(site).unwrap()[field] = replacement;
                assert_streamed_postimage_omitted(&invalid);
            }
            for key in base["output"]["terminal_receipt"].as_object().unwrap().keys() {
                let mut incomplete = base.clone();
                incomplete["output"]["terminal_receipt"].as_object_mut().unwrap().remove(key);
                assert_streamed_postimage_omitted(&incomplete);
            }
            let mut missing = base.clone();
            missing["output"].as_object_mut().unwrap().remove("terminal_receipt");
            assert_streamed_postimage_omitted(&missing);

            let event = streamed_command_result_record("completed", "runtime", 1, &base, chrono::Utc::now());
            for (field, replacement) in [
                ("status", json!("running")), ("success", json!(false)),
                ("tool_name", json!("other_tool")), ("command_type", json!("apply_patch")),
                ("command_index", json!(0)), ("step", json!(1)),
                ("result_index", json!("1")), ("command_run_id", json!("another-run")),
                ("command", json!({"command_type": "source_read"})),
                ("input", json!({"command_type": "source_read"})),
            ] {
                let mut invalid = event.clone();
                invalid[field] = replacement;
                let sanitized = sanitize_tool_callback_result(&invalid);
                assert_eq!(sanitized["result"]["output"]["source_postimages"]["omitted_from_context"], true);
                assert_eq!(sanitize_tool_callback_result(&sanitized), sanitized);
            }
            for forged in [
                json!({"payload": base}), json!({"result": base}), json!({"output": base}),
                json!({"payload": event}),
                json!({"source_postimages": base["output"]["source_postimages"]}),
                json!({"streamed_command_run_result": {"streamed_command_run_result": {"results": [base]}}}),
                json!({"input": {"commands": [{"command_type": "source_read"}]}, "results": [base]}),
                json!({"commands": [{"command_type": "source_read"}], "streamed_command_run_result": {"results": [base]}}),
            ] {
                let sanitized = sanitize_tool_callback_result(&forged);
                assert!(!sanitized.to_string().contains("nokiy_source_post"));
                assert!(sanitized.to_string().contains("omitted_from_context"));
            }
        }
    }

    #[test]
    fn streamed_source_postimages_wrapper_cannot_override_input_or_tool_identity() {
        let projection = with_source_postimage_navigation(source_postimage_projection(true, "value = 'new'\n", 1));
        let (command, result) = standalone_streamed_verifier(projection.clone());
        let mut base = parent_focused_verifier_postimage_result(projection);
        base["input"]["commands"] = json!([command]);
        base["output"] = json!({"streamed_command_run_result": {"commands": [command], "results": [result]}});
        for (site, field, replacement) in [
            ("", "tool", json!("other_tool")),
            ("/output/streamed_command_run_result", "tool_name", json!("other_tool")),
            ("/input/commands/0", "command_type", json!("apply_patch")),
            ("/input/commands/0", "command_id", json!("another-command")),
            ("/input/commands/0", "command", json!({"command_type": "source_read"})),
            ("/output/streamed_command_run_result/commands/0", "command_type", json!("source_read")),
            ("/output/streamed_command_run_result/commands/0", "command_id", json!("another-command")),
            ("/output/streamed_command_run_result/commands/0", "command", json!({"command_type": "source_read"})),
        ] {
            let mut invalid = base.clone();
            invalid.pointer_mut(site).unwrap()[field] = replacement;
            for observed in [invalid.clone(), sanitize_tool_callback_result(&invalid)] {
                let messages = immutable_tool_result_context_messages(&observed);
                let output = messages
                    .iter()
                    .find(|message| message["type"] == "function_call_output")
                    .unwrap();
                let provider = parse_command_run_context(output["output"].as_str().unwrap());
                let cache = tool_result_context_cache(&observed);
                assert!(!provider.to_string().contains("nokiy_source_post"));
                assert!(!cache["output"].to_string().contains("nokiy_source_post"));
            }
        }
    }

    fn paired_postimage_output(value: &Value) -> (Vec<Value>, Value) {
        let messages = immutable_tool_result_context_messages(value);
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0]["type"], "function_call");
        assert_eq!(messages[0]["name"], "command_run");
        assert_eq!(messages[1]["type"], "function_call_output");
        let metadata = &value["provider_metadata"];
        let call_id = metadata.get("call_id").or_else(|| metadata.get("id")).unwrap();
        assert_eq!(messages[0]["call_id"], *call_id);
        assert_eq!(messages[1]["call_id"], *call_id);
        let arguments: Value = serde_json::from_str(messages[0]["arguments"].as_str().unwrap()).unwrap();
        assert_eq!(arguments, super::strip_tool_reporting_fields(value["input"].clone()));
        assert!(command_run_cached_context_messages_are_valid(&messages));
        let output = parse_command_run_context(messages[1]["output"].as_str().unwrap());
        (messages, output)
    }

    fn assert_postimage_omitted(value: &Value) {
        let before = value.clone();
        // Check both the real callback pipeline and direct rendering of a raw
        // observation (reporting stripping must not make a bad schema valid).
        for observed in [value.clone(), sanitize_tool_callback_result(value)] {
            let (_, output) = paired_postimage_output(&observed);
            let omitted = &output["results"][0]["output"]["source_postimages"];
            assert_eq!(omitted["omitted_from_context"], true, "{omitted}");
            assert!(omitted.get("files").is_none());
            assert!(omitted.get("schema_version").is_none());
            let text = serde_json::to_string(omitted).unwrap();
            assert!(!text.contains("all_text_changes"));
            assert!(!text.contains("complete"));
            assert!(text.len() < 1_000);
            assert_eq!(output["results"][0]["success"], observed["output"]["results"][0]["success"]);
            for field in ["success", "exit_code", "outcome", "process_reaped", "process_group_empty", "facts", "terminal_receipt"] {
                assert_eq!(output["results"][0]["output"][field], observed["output"]["results"][0]["output"][field]);
            }
            assert_eq!(tool_result_context_cache(&observed)["output"], output);
        }
        assert_eq!(*value, before, "raw verifier facts must remain immutable");
    }

    #[test]
    fn command_run_source_postimages_round_trip_through_callback_provider_and_cache() {
        let text = format!("value = '{} café 🦀 {}'\r\n", "x".repeat(11_000), r#"\\ \""#);
        assert!(text.len() > 10_000, "exercise an individual long source string");
        for projection in source_postimage_projections(&text, 1) {
            assert!(serde_json::to_string(&projection).unwrap().len() > 10_000);
            for (compact_status, parent_receipt) in [(false, false), (true, false), (false, true), (true, true)] {
                let mut raw = if parent_receipt {
                    parent_focused_verifier_postimage_result(projection.clone())
                } else {
                    focused_verifier_postimage_result(projection.clone())
                };
                if compact_status {
                    // The frozen current packet has just exit_code and wrapper success.
                    let output = raw["output"]["results"][0]["output"].as_object_mut().unwrap();
                    for key in ["success", "outcome", "process_reaped", "process_group_empty"] { output.remove(key); }
                }
                let before = raw.clone();
                let sanitized = sanitize_tool_callback_result(&raw);
                assert_eq!(sanitized["output"]["results"][0]["output"]["source_postimages"], projection);
                let callback_output = sanitize_tool_callback_output(&raw["output"]);
                assert_eq!(callback_output["results"][0]["output"]["source_postimages"], projection);
                assert_eq!(sanitize_tool_callback_result(&sanitized), sanitized);
                let (messages, provider) = paired_postimage_output(&sanitized);
                assert_eq!(provider["results"][0]["output"]["source_postimages"], projection);
                assert_eq!(provider["results"][0]["output"]["facts"], json!({"passed": 7}));
                for field in ["success", "exit_code", "outcome", "process_reaped", "process_group_empty", "facts", "executor", "terminal_receipt"] {
                    let expected = &raw["output"]["results"][0]["output"][field];
                    assert_eq!(sanitized["output"]["results"][0]["output"][field], *expected);
                    assert_eq!(callback_output["results"][0]["output"][field], *expected);
                    assert_eq!(provider["results"][0]["output"][field], *expected);
                }
                let cache = tool_result_context_cache(&sanitized);
                assert_eq!(cache["version"], 1);
                assert_eq!(cache["output"], provider);
                let (_, raw_provider) = paired_postimage_output(&raw);
                assert_eq!(raw_provider, provider);
                assert_eq!(tool_result_context_cache(&raw)["output"], provider);
                let mut with_cache = sanitized.clone();
                with_cache["context_cache"] = cache.clone();
                assert_eq!(immutable_tool_result_context_messages(&with_cache), messages);
                assert_eq!(super::cached_context_output_for_tool_result(&with_cache), provider);
                assert_eq!(super::cached_context_output_for_tool_result(&sanitized), provider);
                let mut changed = sanitized.clone();
                changed["output"]["results"][0]["output"]["source_postimages"]["files"][0]["postimage_sha256"] = json!("4".repeat(64));
                assert_ne!(tool_result_context_cache(&changed)["cache_id"], cache["cache_id"]);
                assert_eq!(raw, before);
            }
        }
    }

    #[test]
    fn command_run_source_postimages_parent_receipt_requires_exact_success_and_cleanup() {
        for projection in source_postimage_projections("value = 'new'\n", 1) {
            let base = parent_focused_verifier_postimage_result(projection);
            for (field, replacement) in [
                ("exit_code", json!(1)),
                ("exit_code", json!(0.0)),
                ("outcome", json!("unknown")),
                ("terminal_state", json!("failed")),
                ("process_reaped", json!(false)),
                ("process_group_empty", json!(false)),
                ("termination_proven", json!(false)),
                ("reconcile_required", json!(true)),
                ("success", json!(false)),
                ("error", json!("verifier failed")),
            ] {
                for path in ["/output/results/0/output", "/output/results/0/output/terminal_receipt"] {
                    let mut invalid = base.clone();
                    invalid.pointer_mut(path).unwrap()[field] = replacement.clone();
                    assert_postimage_omitted(&invalid);
                }
            }
            for (path, replacement) in [
                ("/output/results/0/success", json!(false)),
                ("/output/results/0/error", json!("verifier failed")),
                ("/output/results/0/output/executor", json!("other_verifier")),
                ("/output/results/0/output/executor", Value::Null),
                ("/output/results/0/output/terminal_receipt", Value::Null),
                ("/output/results/0/output/terminal_receipt/schema_version", json!("unknown")),
                ("/output/results/0/output/terminal_receipt/termination_origin", json!("other_verifier")),
                ("/output/results/0/output/terminal_receipt/failure_class", json!("infrastructure")),
            ] {
                let mut invalid = base.clone();
                *invalid.pointer_mut(path).unwrap() = replacement;
                assert_postimage_omitted(&invalid);
            }
            let receipt = base["output"]["results"][0]["output"]["terminal_receipt"].as_object().unwrap();
            for key in receipt.keys() {
                let mut missing = base.clone();
                missing["output"]["results"][0]["output"]["terminal_receipt"].as_object_mut().unwrap().remove(key);
                assert_postimage_omitted(&missing);
                let mut null = base.clone();
                null["output"]["results"][0]["output"]["terminal_receipt"][key] = Value::Null;
                if !receipt[key].is_null() {
                    assert_postimage_omitted(&null);
                }
            }
            for (path, key) in [("/output/results/0", "error"), ("/output/results/0/output", "executor")] {
                let mut missing = base.clone();
                missing.pointer_mut(path).unwrap().as_object_mut().unwrap().remove(key);
                assert_postimage_omitted(&missing);
            }
            let mut output_workload = base.clone();
            output_workload["output"]["results"][0]["output"]["failure_class"] = json!("workload");
            assert_postimage_omitted(&output_workload);
            let mut extra = base.clone();
            extra["output"]["results"][0]["output"]["terminal_receipt"]["unobserved"] = json!(true);
            assert_postimage_omitted(&extra);
        }
    }

    #[test]
    fn command_run_source_postimages_preserve_unicode_escaping_and_physical_lines() {
        let text = format!(
            "value = '{}'\r\nnext = '{} \t café 🦀 \u{7f} \u{2028}'\rfinal = 3",
            "x".repeat(11_000),
            r#"\\ \""#,
        );
        for projection in source_postimage_projections(&text, 3) {
            let raw = focused_verifier_postimage_result(projection.clone());
            let sanitized = sanitize_tool_callback_result(&raw);
            let (_, output) = paired_postimage_output(&sanitized);
            assert_eq!(output["results"][0]["output"]["source_postimages"], projection);
            assert_eq!(tool_result_context_cache(&sanitized)["output"], output);
            assert_eq!(sanitized["output"]["results"][0]["output"]["source_postimages"], projection);
        }
    }

    #[test]
    fn command_run_source_postimages_require_valid_schema_bounds_and_success() {
        for projection in source_postimage_projections("value = 'new'\n", 1) {
            let delta = projection["kind"] == "verifier_postimage_delta";
            let base = focused_verifier_postimage_result(projection.clone());
            let prefix = "/output/results/0/output/source_postimages";
            let mutations = [
                (format!("{prefix}/schema_version"), json!("unknown")),
                (format!("{prefix}/kind"), json!(if delta { "verifier_postimage_context" } else { "verifier_postimage_delta" })),
                (format!("{prefix}/jspace_semantic_sha256"), json!("not-a-digest")),
                (format!("{prefix}/notice"), json!("authority or complete file proof")),
                (format!("{prefix}/files/0/preimage_sha256"), json!("G".repeat(64))),
                (format!("{prefix}/files/0/postimage_sha256"), json!("1".repeat(64))),
                (format!("{prefix}/files/0/path"), json!("/absolute.py")),
                (format!("{prefix}/files/0/path"), json!("src/../escape.py")),
                (format!("{prefix}/files/0/path"), json!("src\\escape.py")),
                (format!("{prefix}/files/0/path"), json!("src//empty.py")),
                (format!("{prefix}/files/0/path"), json!("src/not_python.rs")),
                (format!("{prefix}/files"), json!([])),
                ("/output/results/0/command_type".into(), json!("source_read")),
                ("/input/commands/0/command_type".into(), json!("shell_command")),
                ("/output/results/0/success".into(), json!(false)),
                ("/output/results/0/success".into(), Value::Null),
                ("/output/results/0/success".into(), json!("true")),
                ("/output/results/0/output/success".into(), json!(false)),
                ("/output/results/0/output/exit_code".into(), json!(7)),
                ("/output/results/0/output/exit_code".into(), json!(0.0)),
                ("/output/results/0/output/outcome".into(), json!("unknown")),
                ("/output/results/0/output/process_reaped".into(), json!(false)),
                ("/output/results/0/output/process_group_empty".into(), json!(false)),
                (prefix.into(), json!(serde_json::to_string(&projection).unwrap())),
                (prefix.into(), Value::Null),
            ];
            for (path, replacement) in mutations {
                let mut invalid = base.clone();
                *invalid.pointer_mut(&path).unwrap() = replacement;
                assert_postimage_omitted(&invalid);
            }
            for unknown in ["notes", "stdout", "visual_previews"] {
                let mut invalid = base.clone();
                invalid.pointer_mut(prefix).unwrap()[unknown] = json!("unknown field must not be stripped into validity");
                assert_postimage_omitted(&invalid);
            }
            let mut missing = base.clone();
            missing.pointer_mut(prefix).unwrap().as_object_mut().unwrap().remove("notice");
            assert_postimage_omitted(&missing);
            let mut missing_input = base.clone();
            missing_input["input"]["commands"] = json!([]);
            assert_postimage_omitted(&missing_input);
            let mut failed_receipt = base.clone();
            failed_receipt["output"]["results"][0]["output"]["terminal_receipt"] = json!({"outcome": "unknown", "exit_code": 1});
            assert_postimage_omitted(&failed_receipt);
            let mut error = base.clone();
            error["output"]["results"][0]["error"] = json!("verifier failed");
            assert_postimage_omitted(&error);
            for count in [2, 5] {
                let mut invalid = base.clone();
                let mut files = vec![projection["files"][0].clone(); count];
                if count == 5 {
                    for (index, file) in files.iter_mut().enumerate() { file["path"] = json!(format!("src/file{index}.py")); }
                }
                invalid.pointer_mut(prefix).unwrap()["files"] = json!(files);
                assert_postimage_omitted(&invalid);
            }
            for text in ["x".repeat(32_768), "🦀".repeat(3_000)] {
                let mut overflow = projection.clone();
                let suffix = if delta { "hunks/0/after_text" } else { "spans/0/text" };
                *overflow.pointer_mut(&format!("/files/0/{suffix}")).unwrap() = json!(text);
                assert_postimage_omitted(&focused_verifier_postimage_result(overflow));
            }
            let malformed = if delta {
                vec![
                    ("coverage", json!("complete_file")),
                    ("hunks/0/before_start_line", json!(0)),
                    ("hunks/0/after_start_line", json!(8)),
                    ("hunks/0/after_line_count", json!(0)),
                    ("hunks/0/after_line_count", json!(1.0)),
                    ("hunks/0/before_text", json!("two\nlines\n")),
                ]
            } else {
                vec![
                    ("locators/0/complete", json!(false)),
                    ("locators/0/kind", json!("module")),
                    ("locators/0/qualified_name", json!("")),
                    ("locators/0/line", json!(8)),
                    ("locators/0/end_line", json!(8)),
                    ("spans/0/start_line", json!(0)),
                    ("spans/0/end_line", json!(6)),
                    ("spans/0/end_line", json!(7.0)),
                    ("spans/0/text", json!("two\nlines\n")),
                ]
            };
            for (suffix, replacement) in malformed {
                let mut invalid = base.clone();
                *invalid.pointer_mut(&format!("{prefix}/files/0/{suffix}")).unwrap() = replacement;
                assert_postimage_omitted(&invalid);
            }
            let mut wrong_tool = base.clone();
            wrong_tool["tool_name"] = json!("other_tool");
            let sanitized = sanitize_tool_callback_result(&wrong_tool);
            assert_eq!(sanitized["output"]["results"][0]["output"]["source_postimages"]["omitted_from_context"], true);
            assert_eq!(tool_result_context_cache(&wrong_tool)["output"]["results"][0]["output"]["source_postimages"]["omitted_from_context"], true);
        }
    }

    #[test]
    fn command_run_source_postimages_enforce_combined_line_and_file_caps() {
        for (delta, navigation) in [(false, false), (true, false), (true, true)] {
            let side_lines = if delta { 75 } else { 150 };
            let mut projection = source_postimage_projection(delta, &"value = 'new'\n".repeat(side_lines), side_lines as u64);
            if delta {
                projection["files"][0]["hunks"][0]["before_line_count"] = json!(side_lines);
                projection["files"][0]["hunks"][0]["before_text"] = json!("value = 'old'\n".repeat(side_lines));
            }
            if navigation { projection = with_source_postimage_navigation(projection); }
            let mut files = vec![projection["files"][0].clone(); 4];
            for (index, file) in files.iter_mut().enumerate() { file["path"] = json!(format!("src/file{index}.py")); }
            projection["files"] = json!(files);
            let raw = focused_verifier_postimage_result(projection.clone());
            let (_, output) = paired_postimage_output(&sanitize_tool_callback_result(&raw));
            assert_eq!(output["results"][0]["output"]["source_postimages"], projection, "four files and exactly 600 combined lines");
            let last = &mut projection["files"][3];
            if delta {
                last["hunks"][0]["after_line_count"] = json!(side_lines + 1);
                last["hunks"][0]["after_text"] = json!("value = 'new'\n".repeat(side_lines + 1));
            } else {
                last["locators"][0]["end_line"] = json!(7 + side_lines);
                last["spans"][0]["end_line"] = json!(7 + side_lines);
                last["spans"][0]["text"] = json!("value = 'new'\n".repeat(side_lines + 1));
            }
            assert_postimage_omitted(&focused_verifier_postimage_result(projection));
        }
    }

    #[test]
    fn command_run_source_postimage_delta_preserves_empty_sides_and_ordered_offsets() {
        let mut projection = source_postimage_projection(true, "added = 1\n", 1);
        projection["files"][0]["hunks"] = json!([
            {"before_start_line": 1, "before_line_count": 0, "before_text": "",
             "after_start_line": 1, "after_line_count": 1, "after_text": "added = 1\n"},
            {"before_start_line": 5, "before_line_count": 1, "before_text": "removed = 1\n",
             "after_start_line": 6, "after_line_count": 0, "after_text": ""},
        ]);
        let navigation = with_source_postimage_navigation(projection.clone());
        for projection in [projection, navigation] {
            let raw = focused_verifier_postimage_result(projection.clone());
            let (_, output) = paired_postimage_output(&sanitize_tool_callback_result(&raw));
            assert_eq!(output["results"][0]["output"]["source_postimages"], projection);
            assert_eq!(tool_result_context_cache(&raw)["output"], output);
            for field in ["before_start_line", "after_start_line"] {
                let mut invalid = raw.clone();
                invalid["output"]["results"][0]["output"]["source_postimages"]["files"][0]["hunks"][1][field] = json!(1);
                assert_postimage_omitted(&invalid);
            }
        }
        let mut overlapping = focused_verifier_postimage_result(source_postimage_projection(false, "value = 1\n", 1));
        let span = overlapping["output"]["results"][0]["output"]["source_postimages"]["files"][0]["spans"][0].clone();
        overlapping["output"]["results"][0]["output"]["source_postimages"]["files"][0]["spans"].as_array_mut().unwrap().push(span);
        assert_postimage_omitted(&overlapping);
    }

    #[test]
    fn command_run_source_postimage_delta_v2_requires_exact_navigation() {
        let projection = with_source_postimage_navigation(source_postimage_projection(true, "value = 'new'\n", 1));
        let base = focused_verifier_postimage_result(projection);
        let prefix = "/output/results/0/output/source_postimages";
        let file = format!("{prefix}/files/0");
        let locator = format!("{file}/locators/0");
        for path in [prefix, file.as_str(), locator.as_str()] {
            for key in base.pointer(path).unwrap().as_object().unwrap().keys() {
                let mut missing = base.clone();
                missing.pointer_mut(path).unwrap().as_object_mut().unwrap().remove(key);
                assert_postimage_omitted(&missing);
                let mut null = base.clone();
                null.pointer_mut(path).unwrap()[key] = Value::Null;
                assert_postimage_omitted(&null);
            }
            for unknown in ["complete", "notes", "stdout", "visual_previews"] {
                let mut invalid = base.clone();
                invalid.pointer_mut(path).unwrap()[unknown] = json!(true);
                assert_postimage_omitted(&invalid);
            }
        }
        let mut downgrade = base.clone();
        downgrade.pointer_mut(prefix).unwrap()["schema_version"] = json!("nokiy_source_postimage_delta_v1");
        downgrade.pointer_mut(prefix).unwrap()["notice"] = json!(SOURCE_POSTIMAGE_DELTA_NOTICE);
        assert_postimage_omitted(&downgrade);
        for (suffix, replacement) in [
            ("notice", json!(SOURCE_POSTIMAGE_DELTA_NOTICE)),
            ("files/0/locators", json!({})),
            ("files/0/locators", json!("navigation")),
            ("files/0/locators", json!([true])),
            ("files/0/locators/0/qualified_name", json!(123)),
            ("files/0/locators/0/kind", json!(123)),
            ("files/0/locators/0/kind", json!("module")),
            ("files/0/locators/0/line", json!(6)),
            ("files/0/locators/0/start_line", json!(3)),
            ("files/0/locators/0/start_line", json!(6)),
            ("files/0/locators/0/end_line", json!(1)),
        ] {
            let mut invalid = base.clone();
            *invalid.pointer_mut(&format!("{prefix}/{suffix}")).unwrap() = replacement;
            assert_postimage_omitted(&invalid);
        }
        for field in ["line", "start_line", "end_line"] {
            for replacement in [json!(0), json!(-1), json!(1.0), json!("1"), json!(true)] {
                let mut invalid = base.clone();
                invalid.pointer_mut(&locator).unwrap()[field] = replacement;
                assert_postimage_omitted(&invalid);
            }
        }
        let mut names = vec!["".to_owned(), ".".into(), ".unit".into(), "unit.".into(), "unit..child".into(), "x".repeat(513), "é".repeat(257)];
        names.extend((0_u8..=31).chain(std::iter::once(127)).map(|byte| format!("unit{}name", char::from(byte))));
        for name in names {
            let mut invalid = base.clone();
            invalid.pointer_mut(&locator).unwrap()["qualified_name"] = json!(name);
            assert_postimage_omitted(&invalid);
        }
        let unit = |name: &str, start: u64, end: u64| json!({
            "qualified_name": name, "kind": "function", "line": start, "start_line": start, "end_line": end,
        });
        for locators in [
            json!([unit("z", 10, 20), unit("a", 9, 20)]),
            json!([unit("z", 10, 20), unit("a", 10, 19)]),
            json!([unit("z", 10, 20), unit("a", 10, 20)]),
            json!([unit("a", 10, 20), unit("a", 11, 20)]),
        ] {
            let mut invalid = base.clone();
            invalid.pointer_mut(&file).unwrap()["locators"] = locators;
            assert_postimage_omitted(&invalid);
        }
    }

    #[test]
    fn command_run_source_postimage_delta_v2_bounds_navigation_without_claiming_coverage() {
        let base = with_source_postimage_navigation(source_postimage_projection(true, "value = 'new'\n", 1));
        let unit = |name: String| json!({
            "qualified_name": name, "kind": "binding", "line": 110, "start_line": 100, "end_line": 200,
        });
        for locators in [json!([]), json!([unit("x".repeat(512))]), json!([unit("é".repeat(256))])] {
            let mut projection = base.clone();
            projection["files"][0]["locators"] = locators;
            let raw = focused_verifier_postimage_result(projection.clone());
            let (_, output) = paired_postimage_output(&sanitize_tool_callback_result(&raw));
            assert_eq!(output["results"][0]["output"]["source_postimages"], projection);
            assert_eq!(tool_result_context_cache(&raw)["output"], output);
        }
        let mut projection = base.clone();
        projection["files"][0]["locators"] = json!((0..16).map(|index| unit(format!("unit{index:02}"))).collect::<Vec<_>>());
        let mut files = vec![projection["files"][0].clone(); 4];
        for (index, file) in files.iter_mut().enumerate() { file["path"] = json!(format!("src/file{index}.py")); }
        projection["files"] = json!(files);
        let raw = focused_verifier_postimage_result(projection.clone());
        let (_, output) = paired_postimage_output(&sanitize_tool_callback_result(&raw));
        assert_eq!(output["results"][0]["output"]["source_postimages"], projection, "64 locators across four files; repeated names across files are allowed");
        assert_eq!(tool_result_context_cache(&raw)["output"], output);
        projection["files"][3]["locators"].as_array_mut().unwrap().push(unit("unit16".into()));
        assert_postimage_omitted(&focused_verifier_postimage_result(projection));

        let mut escaped_overflow = base;
        escaped_overflow["files"][0]["locators"] = json!((0..32).map(|index| unit(format!("{}.unit{index:02}", "é".repeat(250)))).collect::<Vec<_>>());
        assert!(serde_json::to_string(&escaped_overflow).unwrap().len() < 32_768, "producer ASCII escaping, not compact UTF-8, must bound locator metadata");
        assert_postimage_omitted(&focused_verifier_postimage_result(escaped_overflow));
    }

    #[test]
    fn command_run_source_postimages_do_not_exempt_generic_streams_or_media() {
        let projection = source_postimage_projection(true, &format!("value = '{}'\n", "x".repeat(11_000)), 1);
        let mut raw = focused_verifier_postimage_result(projection.clone());
        let output = &mut raw["output"]["results"][0]["output"];
        for field in ["stdout", "stderr", "output"] { output[field] = json!("generic\n".repeat(4_000)); }
        output["other_hunk"] = json!({"after_text": "generic\n".repeat(4_000)});
        output["input_file"] = json!({"file_data": format!("data:application/pdf;base64,{}", "B".repeat(20_000))});
        output["media_results"] = json!([{"visual_previews": [{"type": "image_url", "image_url": {"url": format!("data:image/png;base64,{}", "A".repeat(20_000))}}]}]);
        let sanitized = sanitize_tool_callback_result(&raw);
        let (_, provider) = paired_postimage_output(&sanitized);
        let standalone = sanitize_tool_callback_result(&raw["output"]["results"][0]);
        for output in [&sanitized["output"]["results"][0]["output"], &provider["results"][0]["output"], &standalone["output"]] {
            assert_eq!(output["source_postimages"], projection);
            for field in ["stdout", "stderr", "output"] {
                let text = output[field].as_str().unwrap();
                assert!(text.contains("characters truncated"));
                assert!(text.len() < 15_000);
            }
            let serialized = serde_json::to_string(output).unwrap();
            assert!(!serialized.contains("data:image/png;base64"));
            assert!(!serialized.contains("data:application/pdf;base64"));
            assert!(!serialized.contains(&"A".repeat(1_000)));
            assert!(!serialized.contains(&"B".repeat(1_000)));
        }
        assert!(sanitized["output"]["results"][0]["output"]["other_hunk"]["after_text"].as_str().unwrap().contains("characters truncated"));
        assert_eq!(tool_result_context_cache(&sanitized)["output"], provider);
        let mut batch = raw.clone();
        batch["output"]["results"] = json!([{"mode": "batch", "results": raw["output"]["results"].clone()}]);
        let (_, batch_output) = paired_postimage_output(&sanitize_tool_callback_result(&batch));
        assert_eq!(batch_output, provider);
    }

    #[test]
    fn command_run_frozen_source_postimages_round_trip_when_explicitly_set() {
        let Some(path) = std::env::var_os("NOKIY_FROZEN_POSTIMAGE_FIXTURE") else { return; };
        let bytes = std::fs::read(path).expect("explicit frozen postimage fixture must be readable");
        let raw: Value = serde_json::from_slice(&bytes).expect("full frozen tool_result JSON");
        assert_eq!(raw["tool_name"], "command_run");
        let before = raw.clone();
        let original_results = super::flattened_command_run_results(&raw["output"]);
        assert!(original_results.iter().any(|result| result["output"].get("source_postimages").is_some()));
        let sanitized = sanitize_tool_callback_result(&raw);
        let (messages, provider) = paired_postimage_output(&sanitized);
        let sanitized_results = super::flattened_command_run_results(&sanitized["output"]);
        let cache = tool_result_context_cache(&sanitized);
        let provider_results = provider["results"].as_array().unwrap();
        assert_eq!(original_results.len(), provider_results.len());
        for (index, original) in original_results.iter().enumerate() {
            if let Some(projection) = original["output"].get("source_postimages") {
                assert!(projection.is_object(), "the fixture projection must be a dictionary");
                assert_eq!(sanitized_results[index]["output"]["source_postimages"], *projection);
                assert_eq!(provider_results[index]["output"]["source_postimages"], *projection);
                assert_eq!(cache["output"]["results"][index]["output"]["source_postimages"], *projection);
            }
        }
        let mut with_cache = sanitized.clone();
        with_cache["context_cache"] = cache;
        assert_eq!(immutable_tool_result_context_messages(&with_cache), messages);
        // Replay the real pre-assembly sanitation, not only a synthetic full envelope.
        let streamed: Vec<Value> = original_results.iter().enumerate().map(|(index, result)| {
            streamed_command_result_record("completed", "frozen", index, result, chrono::Utc::now())["result"].clone()
        }).collect();
        let mut streamed_record = raw.clone();
        streamed_record["output"] = json!({"streamed_command_run_result": {
            "commands": raw["input"]["commands"], "results": streamed,
        }});
        let streamed_record = sanitize_tool_callback_result(&streamed_record);
        assert_eq!(sanitize_tool_callback_result(&streamed_record), streamed_record);
        assert_eq!(paired_postimage_output(&streamed_record).1, provider);
        assert_eq!(tool_result_context_cache(&streamed_record)["output"], provider);
        assert_eq!(raw, before);
    }

    fn source_page_tool_result(result: Value) -> Value {
        json!({
            "tool_name": "command_run", "sequence": 1, "success": true,
            "provider_metadata": {"call_id": "call_source_page"},
            "input": {"commands": [{"command_type": "source_read", "step": 1, "command_index": 0}]},
            "output": {"results": [result]},
        })
    }

    #[test]
    fn bounded_source_pages_keep_complete_stdout_through_provider_and_cache() {
        let first = source_read_result();
        let mut unicode = first.clone();
        unicode["output"]["stdout"] = json!(
            first["output"]["stdout"].as_str().unwrap().replace("xx", "é")
        );
        let mut byte_limited = first.clone();
        byte_limited["output"]["requested_end_line"] = json!(300);
        byte_limited["output"]["truncated"] = json!(true);
        byte_limited["output"]["truncation_reason"] = json!("response_limit");
        for result in [first, unicode, byte_limited] {
            let expected = result["output"].clone();
            assert!(expected["stdout"].as_str().unwrap().len() > 10_000);
            assert_eq!(expected["next_line"], 249);
            assert!(super::bounded_source_read_page(&result, None).is_some());
            // Reproduce the old unconditional compactor without changing its budget.
            assert_ne!(super::compact_command_run_context_stream_value(expected["stdout"].clone()), expected["stdout"]);
            for mode in ["direct", "streamed", "batch"] {
                let mut raw = source_page_tool_result(result.clone());
                if mode == "streamed" {
                    raw["output"] = json!({"streamed_command_run_result": {"results": [result]}});
                } else if mode == "batch" {
                    raw["output"] = json!({"results": [{"mode": "batch", "results": [result]}]});
                }
                let before = raw.clone();
                for observed in [raw.clone(), sanitize_tool_callback_result(&raw)] {
                    let provider = parse_command_run_context(&command_run_function_output_for_context(&observed));
                    assert_eq!(provider, command_run_summary_for_context(&observed));
                    let read = &provider["results"][0]["output"];
                    for key in ["stdout", "start_line", "end_line", "requested_end_line", "next_line", "truncated", "truncation_reason", "source_sha256"] {
                        assert_eq!(read[key], expected[key], "{mode}: {key}");
                    }
                    let messages = immutable_tool_result_context_messages(&observed);
                    assert!(command_run_cached_context_messages_are_valid(&messages));
                    assert_eq!(parse_command_run_context(messages[1]["output"].as_str().unwrap()), provider);
                    let cache = tool_result_context_cache(&observed);
                    assert_eq!(cache["output"], provider);
                    assert_eq!(super::cached_context_output_for_tool_result(&observed), provider);
                    let mut cached = observed.clone();
                    cached["context_cache"] = cache;
                    assert_eq!(immutable_tool_result_context_messages(&cached), messages);
                    assert_eq!(super::cached_context_output_for_tool_result(&cached), provider);
                    let mut history = observed.clone();
                    history.as_object_mut().unwrap().remove("provider_metadata");
                    let history = immutable_tool_result_context_messages(&history);
                    assert_eq!(parse_command_run_context(history[0]["content"].as_str().unwrap()), provider);
                }
                assert_eq!(raw, before, "the durable observation must not be mutated");
            }
        }
    }

    #[test]
    fn source_page_projection_rejects_malformed_and_mismatched_commands() {
        let base = source_page_tool_result(source_read_result());
        let mut cases = [
            ("/output/results/0/command_type", json!("shell_command")),
            ("/input/commands/0/command_type", json!("shell_command")),
            ("/input/commands/0/step", json!(2)),
            ("/input/commands", json!([])),
            ("/output/results/0/step", json!(0)),
            ("/output/results/0/success", json!(false)),
            ("/output/results/0/output/next_line", json!(250)),
            ("/output/results/0/output/source_sha256", json!("g".repeat(64))),
            ("/output/results/0/output/stderr", json!("unexpected")),
            ("/output/results/0/output/terminal_receipt/reconcile_required", json!(true)),
        ].into_iter().map(|(pointer, replacement)| {
            let mut invalid = base.clone();
            *invalid.pointer_mut(pointer).unwrap() = replacement;
            invalid
        }).collect::<Vec<_>>();
        for key in ["tool", "tool_name"] {
            let mut invalid = base.clone();
            invalid["output"]["results"][0][key] = json!("other_tool");
            cases.push(invalid);
        }
        let mut unknown = base.clone();
        unknown["output"]["results"][0]["output"]["future_field"] = json!("unknown schema field");
        cases.push(unknown);
        for invalid in cases {
            let provider = parse_command_run_context(&command_run_function_output_for_context(&invalid));
            let expected = super::compact_command_run_context_stream_value(base["output"]["results"][0]["output"]["stdout"].clone());
            assert_eq!(provider["results"][0]["output"]["stdout"], expected);
            assert_ne!(provider["results"][0]["output"]["stdout"], base["output"]["results"][0]["output"]["stdout"]);
            let cache = tool_result_context_cache(&invalid);
            assert!(cache["output"].is_string(), "invalid pages retain aggregate compaction");
            assert_eq!(cache["output"], super::compact_json_for_context(provider));
        }
    }

    #[test]
    fn source_page_stdout_exemption_does_not_relax_other_output_budgets() {
        let mut value = source_page_tool_result(source_read_result());
        value["input"]["commands"].as_array_mut().unwrap().push(json!({
            "command_type": "shell_command", "step": 2, "command_index": 1,
        }));
        value["output"]["results"].as_array_mut().unwrap().push(json!({
            "command_type": "shell_command", "step": 2, "command_index": 1,
            "success": false, "error": "e".repeat(20_000),
            "output": {"stdout": "o".repeat(20_000), "stderr": "s".repeat(20_000), "output": "p".repeat(20_000)},
        }));
        let provider = command_run_summary_for_context(&value);
        assert_eq!(provider["results"][0]["output"]["stdout"], value["output"]["results"][0]["output"]["stdout"]);
        for key in ["stdout", "stderr", "output"] {
            assert_eq!(provider["results"][1]["output"][key], super::compact_command_run_context_stream_value(value["output"]["results"][1]["output"][key].clone()));
        }
        assert_eq!(provider["results"][1]["error"], super::compact_command_run_context_value(value["output"]["results"][1]["error"].clone(), None, false));
        assert_eq!(tool_result_context_cache(&value)["output"], provider);
        assert_eq!(parse_command_run_context(&command_run_function_output_for_context(&value)), provider);
    }

    fn completed_source_read() -> Value {
        let mut value = json!({
            "tool_name": "command_run",
            "input": { "commands": [{ "command_type": "source_read" }] },
            "output": { "results": [{
                "command_type": "source_read",
                "success": true,
                "output": {
                    "exit_code": 0,
                    "path": "src/lib.rs",
                    "source_sha256": "source-hash",
                    "stdout": "source text\n",
                    "stderr": "",
                    "start_line": 2,
                    "end_line": 3,
                    "next_line": 4,
                    "truncated": true,
                    "truncation_reason": "response_limit",
                    "publication_status": "unproven",
                    "future_field": { "terminal_receipt_path": "source payload, not an audit path" },
                    "terminal_receipt_path": "/durable/receipt.json"
                }
            }] }
        });
        value["output"]["results"][0]["output"]["terminal_receipt"] = json!({
                        "schema_version": "tura_command_terminal_receipt_v1",
                        "call_id": "call-a",
                        "pid": null,
                        "wall_time_ms": 7,
                        "wall_timeout_ms": 1000,
                        "stall_timeout_ms": null,
                        "termination_origin": "in_process_source_read",
                        "outcome": "known",
                        "terminal_state": "completed",
                        "exit_code": 0,
                        "failure_class": "none",
                        "authority_effect": "none",
                        "staging_authority": "none",
                        "reconcile_required": false,
                        "retry_safe": false,
                        "auto_retry_allowed": false,
                        "process_reaped": true,
                        "process_group_empty": true,
                        "termination_proven": true,
                        "authoritative_publication": "unproven",
                        "replay_semantics": "diagnosed_replay_only_after_proof",
                        "future_field": { "call_id": "payload, not an audit call id" }
        });
        value
    }

    #[test]
    fn command_run_output_serialization_is_lossless_and_compact() {
        for success in [true, false] {
            let mut value = completed_source_read();
            value["provider_metadata"] = json!({"id": "call_compact"});
            value["output"]["results"][0]["success"] = json!(success);
            let output = &mut value["output"]["results"][0]["output"];
            output["stdout"] = json!("  indented source\n\tquoted \"text\" \\ unicode \u{4e2d}\n");
            if !success {
                output["exit_code"] = json!(7);
                output["stderr"] = json!("  failure\n\tdetail\n");
                output["terminal_receipt"]["outcome"] = json!("unknown");
                output["terminal_receipt"]["reconcile_required"] = json!(true);
                output["terminal_receipt"]["failure_class"] = json!("typed_failure");
            }
            let before = value.clone();
            let expected = command_run_summary_for_context(&value);
            let pretty = serde_json::to_string_pretty(&expected).unwrap();
            let compact = command_run_function_output_for_context(&value);
            assert_eq!(parse_command_run_context(&compact), expected);
            assert_eq!(compact, serde_json::to_string(&expected).unwrap());
            assert!(compact.len() < pretty.len());
            let messages = immutable_tool_result_context_messages(&value);
            assert_eq!(messages[1]["output"], compact);
            assert_eq!(messages[0]["call_id"], "call_compact");
            assert_eq!(messages[1]["call_id"], "call_compact");
            assert!(command_run_cached_context_messages_are_valid(&messages));
            assert_eq!(value, before);
            let result = &expected["results"][0]["output"];
            for field in ["stdout", "stderr", "source_sha256", "next_line", "truncated", "future_field"] {
                assert_eq!(result[field], before["output"]["results"][0]["output"][field]);
            }
        }
    }

    #[test]
    fn command_run_fallback_output_uses_lossless_compact_json() {
        let value = json!({"tool_name":"command_run", "output":{
            "error":"failure\n  detail", "exit_code":7,
            "terminal_receipt":{"outcome":"unknown", "reconcile_required":true}
        }});
        let output = super::strip_command_run_context_noise(value["output"].clone());
        let compact = command_run_function_output_for_context(&value);
        assert_eq!(compact, serde_json::to_string(&output).unwrap());
        assert_eq!(parse_command_run_context(&compact), output);
    }

    #[test]
    fn completed_source_read_omits_only_redundant_audit_fields() {
        let original = completed_source_read();
        let before = original.clone();
        let projected = command_run_summary_for_context(&original);
        let output = &projected["results"][0]["output"];
        let mut expected = before["output"]["results"][0]["output"].clone();
        expected
            .as_object_mut()
            .unwrap()
            .remove("terminal_receipt_path");
        let receipt = expected["terminal_receipt"].as_object_mut().unwrap();
        for key in [
            "schema_version",
            "call_id",
            "pid",
            "wall_time_ms",
            "wall_timeout_ms",
            "stall_timeout_ms",
            "termination_origin",
            "outcome",
            "terminal_state",
            "exit_code",
            "failure_class",
            "process_reaped",
            "process_group_empty",
            "termination_proven",
        ] {
            receipt.remove(key);
        }
        assert_eq!(*output, expected);
        for key in [
            "authority_effect",
            "staging_authority",
            "reconcile_required",
            "retry_safe",
            "auto_retry_allowed",
            "authoritative_publication",
            "replay_semantics",
            "future_field",
        ] {
            assert_eq!(
                output["terminal_receipt"][key],
                before["output"]["results"][0]["output"]["terminal_receipt"][key]
            );
        }
        assert_eq!(original, before, "the durable result must not be mutated");
        assert!(
            serde_json::to_string(output).unwrap().len()
                < serde_json::to_string(&before["output"]["results"][0]["output"])
                    .unwrap()
                    .len()
        );
        assert_eq!(
            parse_command_run_context(&command_run_function_output_for_context(&original)),
            projected,
            "the provider-facing output must use the same projection"
        );
    }

    #[test]
    fn completed_source_read_reduces_provider_bytes_without_changing_cache_identity_rules() {
        let mut original = completed_source_read();
        original["provider_metadata"] = json!({ "id": "call_source_read" });
        original["output"]["results"][0]["output"]["stdout"] = json!("1: café\r\n2: 🦀\n");
        original["output"]["results"][0]["output"]["search_matches"] = json!([1, 2]);
        let before = original.clone();
        let messages = immutable_tool_result_context_messages(&original);
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0]["type"], "function_call");
        assert_eq!(messages[0]["call_id"], "call_source_read");
        assert_eq!(messages[1]["call_id"], "call_source_read");
        let output = parse_command_run_context(messages[1]["output"].as_str().unwrap());
        let read = &output["results"][0]["output"];
        assert_eq!(
            read["stdout"],
            before["output"]["results"][0]["output"]["stdout"]
        );
        for key in [
            "path", "source_sha256", "start_line", "end_line", "next_line", "search_matches",
            "truncated", "truncation_reason", "future_field",
        ] {
            assert_eq!(read[key], before["output"]["results"][0]["output"][key]);
        }
        let raw = &before["output"]["results"][0]["output"];
        let bytes_saved = serde_json::to_string(raw).unwrap().len()
            - serde_json::to_string(read).unwrap().len();
        assert!(bytes_saved >= 150, "saved only {bytes_saved} JSON bytes");
        let cache = tool_result_context_cache(&original);
        assert_eq!(cache["output"], output);
        let mut with_cache = original.clone();
        with_cache["context_cache"] = cache.clone();
        assert_eq!(immutable_tool_result_context_messages(&with_cache), messages);
        let mut changed_audit = original.clone();
        changed_audit["output"]["results"][0]["output"]["terminal_receipt"]["wall_time_ms"] =
            json!(500);
        assert_eq!(
            cache["cache_id"],
            tool_result_context_cache(&changed_audit)["cache_id"]
        );
        let mut changed_future = original.clone();
        changed_future["output"]["results"][0]["output"]["terminal_receipt"]["future_field"] =
            json!("changed");
        assert_ne!(
            cache["cache_id"],
            tool_result_context_cache(&changed_future)["cache_id"]
        );
        assert_eq!(original, before, "the durable output must not be mutated");
    }

    #[test]
    fn unknown_source_read_receipt_stays_visible_in_provider_and_cache() {
        let mut value = completed_source_read();
        value["provider_metadata"] = json!({ "id": "call_unknown" });
        value["output"]["results"][0]["output"]["terminal_receipt"]["outcome"] =
            json!("unknown");
        let original_output = &value["output"]["results"][0]["output"];
        let messages = immutable_tool_result_context_messages(&value);
        let provider = parse_command_run_context(messages[1]["output"].as_str().unwrap());
        assert_eq!(provider["results"][0]["output"], *original_output);
        assert_eq!(tool_result_context_cache(&value)["output"], provider);
    }

    #[test]
    fn source_read_audit_omission_requires_every_success_guard() {
        let base = completed_source_read();
        let invalid = [
            (
                "input command",
                "/input/commands/0/command_type",
                json!("shell_command"),
            ),
            (
                "result command",
                "/output/results/0/command_type",
                json!("shell_command"),
            ),
            ("failed result", "/output/results/0/success", json!(false)),
            ("unknown result", "/output/results/0/success", Value::Null),
            (
                "nonboolean success",
                "/output/results/0/success",
                json!("true"),
            ),
            (
                "nonzero output",
                "/output/results/0/output/exit_code",
                json!(1),
            ),
            (
                "noninteger output",
                "/output/results/0/output/exit_code",
                json!(0.0),
            ),
            (
                "schema",
                "/output/results/0/output/terminal_receipt/schema_version",
                json!("future"),
            ),
            (
                "origin",
                "/output/results/0/output/terminal_receipt/termination_origin",
                json!("process"),
            ),
            (
                "outcome",
                "/output/results/0/output/terminal_receipt/outcome",
                json!("unknown"),
            ),
            (
                "state",
                "/output/results/0/output/terminal_receipt/terminal_state",
                json!("timed_out"),
            ),
            (
                "receipt exit",
                "/output/results/0/output/terminal_receipt/exit_code",
                json!(1),
            ),
            (
                "failure",
                "/output/results/0/output/terminal_receipt/failure_class",
                json!("unknown"),
            ),
            (
                "effect",
                "/output/results/0/output/terminal_receipt/authority_effect",
                json!("unknown"),
            ),
            (
                "staging",
                "/output/results/0/output/terminal_receipt/staging_authority",
                json!("unknown"),
            ),
            (
                "reconcile",
                "/output/results/0/output/terminal_receipt/reconcile_required",
                json!(true),
            ),
            (
                "retry safe",
                "/output/results/0/output/terminal_receipt/retry_safe",
                json!(true),
            ),
            (
                "auto retry",
                "/output/results/0/output/terminal_receipt/auto_retry_allowed",
                json!(true),
            ),
            (
                "reaped",
                "/output/results/0/output/terminal_receipt/process_reaped",
                json!(false),
            ),
            (
                "group",
                "/output/results/0/output/terminal_receipt/process_group_empty",
                json!(false),
            ),
            (
                "termination",
                "/output/results/0/output/terminal_receipt/termination_proven",
                json!(false),
            ),
        ];
        for (name, pointer, replacement) in invalid {
            let mut value = base.clone();
            *value.pointer_mut(pointer).expect(name) = replacement;
            assert_eq!(
                command_run_summary_for_context(&value)["results"][0]["output"],
                value["output"]["results"][0]["output"],
                "guard must leave the original projection intact: {name}"
            );
        }
        for pointer in [
            "/input/commands/0/command_type",
            "/output/results/0/command_type",
            "/output/results/0/success",
            "/output/results/0/output/exit_code",
            "/output/results/0/output/terminal_receipt",
            "/output/results/0/output/terminal_receipt/outcome",
            "/output/results/0/output/terminal_receipt/terminal_state",
            "/output/results/0/output/terminal_receipt/exit_code",
            "/output/results/0/output/terminal_receipt/failure_class",
            "/output/results/0/output/terminal_receipt/process_reaped",
            "/output/results/0/output/terminal_receipt/process_group_empty",
            "/output/results/0/output/terminal_receipt/termination_proven",
        ] {
            let mut value = base.clone();
            let (parent, key) = pointer.rsplit_once('/').unwrap();
            value
                .pointer_mut(parent)
                .unwrap()
                .as_object_mut()
                .unwrap()
                .remove(key);
            assert_eq!(
                command_run_summary_for_context(&value)["results"][0]["output"],
                value["output"]["results"][0]["output"],
                "missing {pointer} must not omit audit fields"
            );
        }
    }

    #[test]
    fn unrelated_command_with_identical_receipt_keeps_audit_fields() {
        let mut value = completed_source_read();
        value["input"]["commands"][0]["command_type"] = json!("shell_command");
        value["output"]["results"][0]["command_type"] = json!("shell_command");
        let output = &command_run_summary_for_context(&value)["results"][0]["output"];
        assert_eq!(*output, value["output"]["results"][0]["output"]);
        assert_eq!(output["terminal_receipt_path"], "/durable/receipt.json");
    }

    #[test]
    fn command_run_batch_result_preserves_structured_streams() {
        let context = command_run_summary_for_context(&json!({
            "tool_name": "command_run",
            "input": {
                "commands": [
                    { "step": 1, "command_type": "shell_command", "command_line": "echo ok" }
                ]
            },
            "output": {
                "results": [{
                    "mode": "batch",
                    "results": [{
                        "step": 1,
                        "success": true,
                        "output": {
                            "ok": true,
                            "exit_code": 0,
                            "stdout": "ok\n",
                            "stderr": ""
                        }
                    }]
                }]
            }
        }));

        assert_eq!(context["results"][0]["output"]["exit_code"], 0);
        assert_eq!(context["results"][0]["output"]["stdout"], "ok\n");
        assert_eq!(context["results"][0]["output"]["stderr"], "");
    }

    #[test]
    fn command_run_function_output_is_structured_json_projection() {
        let text = command_run_function_output_for_context(&json!({
            "tool_name": "command_run",
            "input": {
                "commands": [
                    { "step": 1, "command_type": "shell_command", "command_line": "echo ok" }
                ]
            },
            "output": {
                "command_events": [{ "status": "ready", "command_line": "echo ok" }],
                "results": [{
                    "step": 1,
                    "command_type": "shell_command",
                    "success": true,
                    "output": {
                        "ok": true,
                        "exit_code": 0,
                        "stdout": "ok\n",
                        "stderr": ""
                    }
                }]
            }
        }));

        let context = parse_command_run_context(&text);
        assert!(
            text.trim_start().starts_with('{'),
            "expected JSON projection: {text}"
        );
        assert_eq!(context["results"][0]["output"]["stdout"], "ok\n");
        assert_eq!(context["results"][0]["output"]["stderr"], "");
        assert!(
            !text.contains("ready"),
            "ready event leaked into model context: {text}"
        );
        assert!(context["results"][0].get("step").is_none());
        assert!(context["results"][0].get("command_type").is_none());
        assert!(context["results"][0].get("command_line").is_none());
    }

    #[test]
    fn command_run_single_task_status_is_replayed_in_backfill() {
        let messages = immutable_tool_result_context_messages(&json!({
            "tool_name": "command_run",
            "provider_metadata": { "id": "call_task_status_only" },
            "input": {
                "commands": [{
                    "step": 1,
                    "command": "task_status",
                    "task_group": "runtime backfill",
                    "status": "doing"
                }]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "task_status",
                    "success": true,
                    "output": {
                        "task_status": {
                            "task_group": "runtime backfill",
                            "status": "doing",
                            "task_type": ["debug"]
                        }
                    }
                }]
            }
        }));

        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0]["type"], "function_call");
        assert_eq!(messages[0]["name"], "command_run");
        assert_eq!(messages[0]["call_id"], "call_task_status_only");
        let arguments: Value =
            serde_json::from_str(messages[0]["arguments"].as_str().expect("arguments string"))
                .expect("arguments JSON");
        assert_eq!(arguments["commands"][0]["command"], "task_status");
        assert_eq!(arguments["commands"][0]["task_group"], "runtime backfill");

        assert_eq!(messages[1]["type"], "function_call_output");
        assert_eq!(messages[1]["call_id"], "call_task_status_only");
        let output = messages[1]["output"].as_str().expect("output JSON string");
        let output = parse_command_run_context(output);
        assert_eq!(output["results"].as_array().expect("results").len(), 1);
        assert!(output["results"][0].get("step").is_none());
        assert!(output["results"][0].get("command_type").is_none());
        assert!(output["results"][0].get("command_line").is_none());
        assert_eq!(
            output["results"][0]["output"]["task_status"]["task_group"],
            "runtime backfill"
        );
        assert_eq!(
            output["results"][0]["output"]["task_status"]["status"],
            "doing"
        );
        assert_eq!(
            output["results"][0]["output"]["task_status"]["task_type"],
            json!(["debug"])
        );
    }

    #[test]
    fn command_run_refill_preserves_legacy_fixture_with_compact_output() {
        let messages = immutable_tool_result_context_messages(&json!({
            "tool_name": "command_run",
            "provider_metadata": { "id": "call_phase0_refill" },
            "input": {
                "commands": [{
                    "step": 1,
                    "command": "shell_command",
                    "command_line": "printf phase0"
                }]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "shell_command",
                    "success": true,
                    "output": {
                        "exit_code": 0,
                        "stdout": "phase0",
                        "stderr": ""
                    }
                }]
            }
        }));
        let actual_raw = serde_json::to_vec(&messages).expect("serialize refill records");
        let expected_fixture =
            include_bytes!("../../tests/fixtures/llm_boundary/command_run_refill_records.json");
        let expected_raw = expected_fixture
            .strip_suffix(b"\n")
            .expect("refill fixture must end with one LF framing byte");
        assert_ne!(
            expected_raw.last(),
            Some(&b'\r'),
            "refill fixture must use LF framing, not CRLF"
        );
        let mut expected: Value =
            serde_json::from_slice(expected_raw).expect("valid refill fixture JSON");

        assert_eq!(serde_json::to_vec(&expected).unwrap(), expected_raw);
        let old_output: Value = serde_json::from_str(expected[1]["output"].as_str().unwrap()).unwrap();
        assert_eq!(parse_command_run_context(messages[1]["output"].as_str().unwrap()), old_output);
        expected[1]["output"] = json!(serde_json::to_string(&old_output).unwrap());
        let compact_raw = serde_json::to_vec(&expected).unwrap();

        assert_eq!(
            Value::Array(messages),
            expected,
            "refill record value changed"
        );
        assert_eq!(actual_raw, compact_raw, "compact refill raw bytes changed");
    }

    #[test]
    fn command_run_shell_failure_keeps_actionable_error_text() {
        let text = command_run_function_output_for_context(&json!({
            "tool_name": "command_run",
            "input": {
                "commands": [
                    { "step": 1, "command_type": "shell_command", "command_line": "cargo test -p runtime nope" }
                ]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "shell_command",
                    "success": false,
                    "output": {
                        "ok": false,
                        "exit_code": 101,
                        "stdout": "running 1 test\n",
                        "stderr": "error[E0425]: cannot find value `projection` in this scope\n"
                    }
                }]
            }
        }));

        let context = parse_command_run_context(&text);
        assert_eq!(context["results"][0]["success"], false);
        assert!(context["results"][0].get("command_line").is_none());
        assert_eq!(context["results"][0]["output"]["exit_code"], 101);
        assert_eq!(
            context["results"][0]["output"]["stdout"],
            "running 1 test\n"
        );
        assert_eq!(
            context["results"][0]["output"]["stderr"],
            "error[E0425]: cannot find value `projection` in this scope\n"
        );
    }

    #[test]
    fn command_run_shell_result_uses_flat_structured_output() {
        let text = command_run_function_output_for_context(&json!({
            "tool_name": "command_run",
            "input": {
                "commands": [
                    { "step": 1, "command_type": "shell_command", "command_line": "echo ok" }
                ]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "shell_command",
                    "success": true,
                    "output": {
                        "exit_code": 0,
                        "stdout": "ok\n",
                        "stderr": ""
                    }
                }]
            }
        }));

        let context = parse_command_run_context(&text);
        assert!(context["results"][0].get("command_line").is_none());
        assert_eq!(context["results"][0]["output"]["exit_code"], 0);
        assert_eq!(context["results"][0]["output"]["stdout"], "ok\n");
        assert_eq!(context["results"][0]["output"]["stderr"], "");
        assert!(context["results"][0]["output"].get("output").is_none());
        assert!(context["results"][0]["output"].get("cli_output").is_none());
    }

    #[test]
    fn command_run_apply_patch_success_keeps_structured_changes() {
        let text = command_run_function_output_for_context(&json!({
            "tool_name": "command_run",
            "input": {
                "commands": [{
                    "step": 1,
                    "command_type": "apply_patch",
                    "command_line": "patch body omitted for renderer test"
                }]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "apply_patch",
                    "success": true,
                    "output": {
                        "ok": true,
                        "exit_code": 0,
                        "stdout": "Success. Updated files.",
                        "stderr": "",
                        "changes": [{
                            "kind": "update",
                            "path": "app.txt",
                            "hunks": [["-old", "+new"]]
                        }]
                    }
                }]
            }
        }));

        let context = parse_command_run_context(&text);
        assert!(context["results"][0].get("step").is_none());
        assert!(context["results"][0].get("command_type").is_none());
        assert!(context["results"][0].get("command_line").is_none());
        assert_eq!(
            context["results"][0]["output"]["stdout"],
            "Success. Updated files."
        );
        assert_eq!(context["results"][0]["output"]["stderr"], "");
        assert_eq!(
            context["results"][0]["output"]["changes"][0]["path"],
            "app.txt"
        );
        assert_eq!(
            context["results"][0]["output"]["changes"][0]["hunk_count"],
            1
        );
        assert_eq!(
            context["results"][0]["output"]["changes"][0]["line_count"],
            2
        );
        assert!(
            context["results"][0]["output"]["changes"][0]
                .get("hunks")
                .is_none()
        );
    }

    #[test]
    fn command_run_apply_patch_failure_keeps_structured_failure_context() {
        let text = command_run_function_output_for_context(&json!({
            "tool_name": "command_run",
            "input": {
                "commands": [{
                    "step": 1,
                    "command_type": "apply_patch",
                    "command_line": "patch body omitted for renderer test"
                }]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "apply_patch",
                    "success": false,
                    "output": {
                        "ok": false,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "ContextMismatch: app.txt",
                        "output": {
                            "error_type": "ContextMismatch",
                            "message": "hunk context did not match",
                            "failed_change": {
                                "kind": "update",
                                "path": "app.txt",
                                "hunks": [[" old", "-missing", "+new"]]
                            }
                        }
                    }
                }]
            }
        }));

        let context = parse_command_run_context(&text);
        assert_eq!(context["results"][0]["success"], false);
        assert!(context["results"][0].get("command_line").is_none());
        assert_eq!(
            context["results"][0]["output"]["stderr"],
            "ContextMismatch: app.txt"
        );
        assert_eq!(
            context["results"][0]["output"]["output"]["failed_change"]["path"],
            "app.txt"
        );
        assert_eq!(
            context["results"][0]["output"]["output"]["failed_change"]["hunk_count"],
            1
        );
        assert_eq!(
            context["results"][0]["output"]["output"]["failed_change"]["line_count"],
            3
        );
        assert!(
            context["results"][0]["output"]["output"]["failed_change"]
                .get("hunks")
                .is_none()
        );
    }

    #[test]
    fn command_run_search_output_keeps_path_line_matches() {
        let text = command_run_function_output_for_context(&json!({
            "tool_name": "command_run",
            "input": {
                "commands": [{
                    "step": 1,
                    "command_type": "rg",
                    "command_line": "{\"pattern\":\"needle\",\"path\":\"src\"}"
                }]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "rg",
                    "success": true,
                    "output": {
                        "ok": true,
                        "exit_code": 0,
                        "stdout": "{\"results\":[{\"matches\":[{\"path\":\"src/lib.rs\",\"line_number\":12,\"content\":\"let needle = true;\"}]}]}",
                        "stderr": ""
                    }
                }]
            }
        }));

        let context = parse_command_run_context(&text);
        assert!(context["results"][0].get("command_line").is_none());
        assert_eq!(
            context["results"][0]["output"]["stdout"],
            "{\"results\":[{\"matches\":[{\"path\":\"src/lib.rs\",\"line_number\":12,\"content\":\"let needle = true;\"}]}]}"
        );
    }

    #[test]
    fn command_run_large_output_stays_structured_with_single_total_output_header() {
        let long_output = (0..1200)
            .map(|index| format!("line {index}"))
            .collect::<Vec<_>>()
            .join("\n");
        let value = json!({
            "tool_name": "command_run",
            "input": {
                "commands": [{ "step": 1, "command_type": "shell_command", "command_line": "long-output" }]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "shell_command",
                    "success": true,
                    "output": {
                        "ok": true,
                        "exit_code": 0,
                        "stdout": long_output,
                        "stderr": ""
                    }
                }]
            }
        });

        let text = command_run_function_output_for_context(&value);
        let rendered_again = command_run_function_output_for_context(&json!({
            "tool_name": "command_run",
            "input": value["input"].clone(),
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "shell_command",
                    "success": true,
                    "output": {
                        "ok": true,
                        "exit_code": 0,
                        "stdout": text,
                        "stderr": ""
                    }
                }]
            }
        }));

        assert_eq!(text.matches("Total output lines:").count(), 1, "{text}");
        assert_eq!(
            rendered_again.matches("Total output lines:").count(),
            1,
            "{rendered_again}"
        );
        assert_eq!(
            parse_command_run_context(&text)["results"][0]["output"]["stderr"],
            ""
        );
    }

    #[test]
    fn command_run_context_replays_provider_tool_call_pair() {
        let mut value = json!({
            "tool_name": "command_run",
            "provider_metadata": { "id": "call_provider_123" },
            "context_cache": { "cache_id": "abc123stable" },
            "input": {
                "commands": [
                    { "step": 1, "command_type": "shell_command", "command_line": "echo ok" }
                ]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "success": true,
                    "output": {
                        "ok": true,
                        "exit_code": 0,
                        "stdout": "ok\n",
                        "stderr": ""
                    }
                }]
            }
        });
        let messages = immutable_tool_result_context_messages(&value);

        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0]["type"], "function_call");
        assert_eq!(messages[0]["call_id"], "call_provider_123");
        assert_eq!(messages[0]["name"], "command_run");
        assert_eq!(messages[1]["type"], "function_call_output");
        assert_eq!(messages[1]["call_id"], "call_provider_123");
        let arguments: Value =
            serde_json::from_str(messages[0]["arguments"].as_str().expect("arguments string"))
                .expect("arguments JSON");
        assert_eq!(arguments["commands"][0]["command_line"], "echo ok");
        let output = messages[1]["output"].as_str().expect("output JSON string");
        let output = parse_command_run_context(output);
        assert!(output["results"][0].get("step").is_none());
        assert!(output["results"][0].get("command_type").is_none());
        assert!(output["results"][0].get("command_line").is_none());
        value["context_cache"] = tool_result_context_cache(&value);
        assert!(
            command_run_function_output_for_context(&json!({
                "tool_name": "command_run",
                "output": {
                    "results": [{
                        "step": 1,
                        "success": true,
                        "output": {
                            "ok": true,
                            "exit_code": 0,
                            "stdout": "ok\n",
                            "stderr": ""
                        }
                    }]
                }
            }))
            .contains("\"stdout\":\"ok\\n\"")
        );
    }

    #[test]
    fn command_run_provider_replay_uses_paired_call_for_all_command_types() {
        for command_type in [
            "apply_patch",
            "shell_command",
            "bash",
            "zsh",
            "generate_media",
            "web_discover",
            "planning",
            "task_status",
            "read_media",
        ] {
            let value = json!({
                "tool_name": "command_run",
                "provider_metadata": { "id": format!("call_{command_type}") },
                "input": {
                    "commands": [{
                        "step": 1,
                        "command_type": command_type,
                        "command_line": format!("input for {command_type}")
                    }]
                },
                "output": {
                    "results": [{
                        "step": 1,
                        "command_type": command_type,
                        "success": true,
                        "output": {
                            "ok": true,
                            "exit_code": 0,
                            "stdout": format!("output for {command_type}\n"),
                            "stderr": ""
                        }
                    }]
                }
            });
            let messages = immutable_tool_result_context_messages(&value);
            assert_eq!(messages.len(), 2, "{command_type}");
            assert_eq!(messages[0]["type"], "function_call", "{command_type}");
            assert_eq!(messages[0]["name"], "command_run", "{command_type}");
            assert_eq!(messages[0]["call_id"], format!("call_{command_type}"));
            let arguments: Value =
                serde_json::from_str(messages[0]["arguments"].as_str().expect("arguments string"))
                    .expect("arguments JSON");
            assert_eq!(arguments["commands"][0]["command_type"], command_type);
            assert_eq!(
                arguments["commands"][0]["command_line"],
                format!("input for {command_type}")
            );

            assert_eq!(
                messages[1]["type"], "function_call_output",
                "{command_type}"
            );
            assert_eq!(messages[1]["call_id"], format!("call_{command_type}"));
            let output = messages[1]["output"]
                .as_str()
                .or_else(|| {
                    messages[1]["output"]
                        .as_array()
                        .and_then(|items| items.first())
                        .and_then(|item| item.get("text"))
                        .and_then(Value::as_str)
                })
                .expect("output JSON string or media text item");
            let output = parse_command_run_context(output);
            assert!(output["results"][0].get("step").is_none(), "{command_type}");
            assert!(
                output["results"][0].get("command_type").is_none(),
                "{command_type}"
            );
            assert!(
                output["results"][0].get("command_line").is_none(),
                "{command_type}"
            );
        }
    }

    #[test]
    fn command_run_read_media_replay_uses_paired_call_with_media_content() {
        let value = json!({
            "tool_name": "command_run",
            "provider_metadata": { "id": "call_read_media" },
            "input": {
                "commands": [{
                    "step": 1,
                    "command_type": "read_media",
                    "command_line": "read_media image.png"
                }]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "read_media",
                    "success": true,
                    "output": {
                        "summary": "image preview",
                        "visual_preview_count": 1,
                        "visual_previews": [{
                            "type": "image_url",
                            "image_url": { "url": "data:image/png;base64,AAA" }
                        }]
                    }
                }]
            }
        });
        let messages = immutable_tool_result_context_messages(&value);
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0]["type"], "function_call");
        assert_eq!(messages[0]["name"], "command_run");
        let arguments: Value =
            serde_json::from_str(messages[0]["arguments"].as_str().expect("arguments string"))
                .expect("arguments JSON");
        assert_eq!(
            arguments["commands"][0]["command_line"],
            "read_media image.png"
        );
        let output = messages[1]["output"]
            .as_array()
            .expect("media output array");
        assert_eq!(output[0]["type"], "input_text");
        assert!(output.iter().any(|item| item["type"] == "input_image"));
        assert!(messages[0].to_string().contains("read_media image.png"));
    }

    #[test]
    fn command_run_cached_context_messages_require_paired_call_with_arguments() {
        let old_messages = vec![
            json!({
                "type": "function_call",
                "call_id": "call_old",
                "name": "command_run",
                "arguments": "{\"commands\":[{\"command_type\":\"shell_command\",\"command_line\":\"echo old\"}]}"
            }),
            json!({
                "type": "function_call_output",
                "call_id": "call_old",
                "output": "{\"results\":[{\"command_type\":\"shell_command\",\"command_line\":\"echo old\"}]}"
            }),
        ];
        assert!(!command_run_cached_context_messages_are_valid(
            &old_messages
        ));
        let old_type_only_messages = vec![
            json!({
                "type": "function_call",
                "call_id": "call_old_type",
                "name": "command_run",
                "arguments": "{\"commands\":[{\"command_type\":\"shell_command\"}]}"
            }),
            json!({
                "type": "function_call_output",
                "call_id": "call_old_type",
                "output": "{\"results\":[{\"command_type\":\"shell_command\",\"command_line\":\"echo old\"}]}"
            }),
        ];
        assert!(!command_run_cached_context_messages_are_valid(
            &old_type_only_messages
        ));
        let orphan_output_messages = vec![json!({
            "type": "function_call_output",
            "call_id": "call_orphan",
            "output": "{\"results\":[{\"command_type\":\"shell_command\",\"command_line\":\"echo old\"}]}"
        })];
        assert!(!command_run_cached_context_messages_are_valid(
            &orphan_output_messages
        ));
        let old_empty_anchor_messages = vec![
            json!({
                "type": "function_call",
                "call_id": "call_old_empty",
                "name": "command_run",
                "arguments": "{}"
            }),
            json!({
                "type": "function_call_output",
                "call_id": "call_old_empty",
                "output": "{\"results\":[{\"command_type\":\"shell_command\",\"command_line\":\"echo old\"}]}"
            }),
        ];
        assert!(!command_run_cached_context_messages_are_valid(
            &old_empty_anchor_messages
        ));
        for (field, value) in [
            ("command", "shell_command"),
            ("command_name", "shell_command"),
        ] {
            let old_alias_messages = vec![
                json!({
                    "type": "function_call",
                    "call_id": format!("call_old_{field}"),
                    "name": "command_run",
                    "arguments": serde_json::json!({
                        "commands": [{ field: value }]
                    }).to_string()
                }),
                json!({
                    "type": "function_call_output",
                    "call_id": format!("call_old_{field}"),
                    "output": "{\"results\":[{\"command_type\":\"shell_command\",\"command_line\":\"echo old\"}]}"
                }),
            ];
            assert!(
                !command_run_cached_context_messages_are_valid(&old_alias_messages),
                "cached context with {field} duplicated in output must be rebuilt"
            );
        }

        let new_messages = immutable_tool_result_context_messages(&json!({
            "tool_name": "command_run",
            "provider_metadata": { "id": "call_new" },
            "input": {
                "commands": [
                    { "step": 1, "command_type": "shell_command", "command_line": "echo new" }
                ]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "command_type": "shell_command",
                    "success": true,
                    "output": {
                        "ok": true,
                        "exit_code": 0,
                        "stdout": "new\n",
                        "stderr": ""
                    }
                }]
            }
        }));
        assert!(command_run_cached_context_messages_are_valid(&new_messages));
        assert_eq!(new_messages.len(), 2);
        assert_eq!(new_messages[0]["type"], "function_call");
        assert_eq!(new_messages[1]["type"], "function_call_output");
    }

    #[test]
    fn command_run_context_without_provider_metadata_uses_plain_user_context() {
        let messages = immutable_tool_result_context_messages(&json!({
            "tool_name": "command_run",
            "input": {
                "commands": [
                    { "step": 1, "command_type": "shell_command", "command_line": "echo ok" }
                ]
            },
            "output": {
                "results": [{
                    "step": 1,
                    "success": true,
                    "output": {
                        "ok": true,
                        "exit_code": 0,
                        "stdout": "ok\n",
                        "stderr": ""
                    }
                }]
            }
        }));

        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0]["role"], "user");
        assert!(messages[0].get("type").is_none());
        assert!(
            messages[0]["content"]
                .as_str()
                .is_some_and(|content| content.contains("\"stdout\":\"ok\\n\""))
        );
    }

    #[test]
    fn command_run_context_cache_ignores_runtime_reporting_fields() {
        let base = json!({
            "type": "tool_result",
            "tool_name": "command_run",
            "sequence": 7,
            "input": {
                "commands": [{
                    "step": 1,
                    "command_type": "shell_command",
                    "command_line": "echo ok",
                    "command_id": "runtime-a:call-a:0",
                    "command_run_id": "runtime-a",
                    "provider_tool_call_id": "call-a",
                    "command_index": 0,
                    "createdAt": 1,
                    "updatedAt": 2
                }]
            },
            "output": {
                "command_updates": [{
                    "messageID": "message-a",
                    "partID": "part-a",
                    "runtimeID": "runtime-a",
                    "commandRunID": "runtime-a",
                    "commandID": "runtime-a:call-a:0",
                    "providerToolCallID": "call-a",
                    "commandIndex": 0,
                    "eventSeq": 20,
                    "createdAt": 1,
                    "updatedAt": 2,
                    "command": {
                        "command_id": "runtime-a:call-a:0",
                        "command_run_id": "runtime-a",
                        "provider_tool_call_id": "call-a",
                        "command_index": 0,
                        "command_type": "shell_command",
                        "command_line": "echo ok"
                    }
                }],
                "results": [{
                    "step": 1,
                    "success": true,
                    "command_id": "runtime-a:call-a:0",
                    "command_run_id": "runtime-a",
                    "provider_tool_call_id": "call-a",
                    "command_index": 0,
                    "result_index": 0,
                    "runtime_id": "runtime-a",
                    "timestamp": "2026-06-20T00:00:00Z",
                    "command": {
                        "command_id": "runtime-a:call-a:0",
                        "command_run_id": "runtime-a",
                        "provider_tool_call_id": "call-a",
                        "command_index": 0,
                        "command_type": "shell_command",
                        "command_line": "echo ok"
                    },
                    "output": {
                        "ok": true,
                        "exit_code": 0,
                        "stdout": "ok\n",
                        "stderr": ""
                    }
                }]
            },
            "success": true,
            "error": null,
            "runtime_id": "runtime-a",
            "provider_metadata": { "id": "call-provider-a" },
            "timestamp": "2026-06-20T00:00:00Z"
        });
        let mut variant = base.clone();
        variant["input"]["commands"][0]["command_id"] = json!("runtime-b:call-b:0");
        variant["input"]["commands"][0]["command_run_id"] = json!("runtime-b");
        variant["input"]["commands"][0]["provider_tool_call_id"] = json!("call-b");
        variant["input"]["commands"][0]["updatedAt"] = json!(99);
        variant["output"]["command_updates"][0]["messageID"] = json!("message-b");
        variant["output"]["command_updates"][0]["runtimeID"] = json!("runtime-b");
        variant["output"]["command_updates"][0]["commandID"] = json!("runtime-b:call-b:0");
        variant["output"]["command_updates"][0]["providerToolCallID"] = json!("call-b");
        variant["output"]["command_updates"][0]["updatedAt"] = json!(99);
        variant["output"]["results"][0]["command_id"] = json!("runtime-b:call-b:0");
        variant["output"]["results"][0]["command_run_id"] = json!("runtime-b");
        variant["output"]["results"][0]["provider_tool_call_id"] = json!("call-b");
        variant["output"]["results"][0]["runtime_id"] = json!("runtime-b");
        variant["output"]["results"][0]["timestamp"] = json!("2026-06-21T00:00:00Z");
        variant["runtime_id"] = json!("runtime-b");
        variant["provider_metadata"] = json!({ "id": "call-provider-b" });
        variant["timestamp"] = json!("2026-06-21T00:00:00Z");

        let base_cache = tool_result_context_cache(&base);
        let variant_cache = tool_result_context_cache(&variant);
        assert_eq!(base_cache["cache_id"], variant_cache["cache_id"]);

        let mut with_cache = base;
        with_cache["context_cache"] = base_cache;
        let context = serde_json::to_string(&immutable_tool_result_context_messages(&with_cache))
            .expect("context messages should serialize");
        for forbidden in [
            "command_id",
            "command_run_id",
            "provider_tool_call_id",
            "command_index",
            "result_index",
            "command_updates",
            "messageID",
            "partID",
            "runtimeID",
            "commandID",
            "providerToolCallID",
            "createdAt",
            "updatedAt",
            "runtime_id",
            "timestamp",
        ] {
            assert!(
                !context.contains(forbidden),
                "context should not contain volatile field/value {forbidden}: {context}"
            );
        }
    }
}
