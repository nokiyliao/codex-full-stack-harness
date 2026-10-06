//! The OpenAI-style **Responses API** tier (`/responses`, SSE stream).
//!
//! This is the shared core for codex (OAuth) and the API-key Responses
//! sub-providers — `chatgpt` (OpenAI), `grok` (xAI), `qwen` (DashScope
//! international). All of them speak the same request/stream shape; the few
//! per-provider divergences are captured by [`ResponsesProfile`] as a small
//! provider quirk layer.

use serde_json::{Value, json};
use std::time::Instant;

use super::common::{
    insert_opt, message_content_text, normalized_reasoning_effort, normalized_service_tier,
};
use crate::metrics::extract_openapi_metrics;
use crate::streaming::{
    ProviderTransportPhase, next_provider_stream_chunk, provider_transport_error,
    send_provider_request_first_response,
};
use crate::tura_llm::{
    CostDetails, ProviderResponse, ProviderStreamEvent, ProviderStreamEventSink, TuraError,
    normalize_response_content,
};
use crate::utils::{deep_merge_json, openai_responses_content_from_canonical, strip_json_fence};

pub(crate) async fn codex_oauth_call(
    model: &str,
    access_token: &str,
    messages: &[Value],
    options: &CallOptions,
    stream_events: Option<ProviderStreamEventSink>,
) -> Result<ProviderResponse, TuraError> {
    let client = reqwest::Client::builder()
        .build()
        .map_err(|err| provider_transport_error(ProviderTransportPhase::ClientBuild, &err))?;
    let payload = build_codex_oauth_payload(model, messages, options);

    let mut request = client
        .post(openai_codex_endpoint())
        .bearer_auth(access_token)
        .header("originator", "codex_cli_rs")
        .header("User-Agent", codex_cli_user_agent())
        .json(&payload);

    let header_profile = std::env::var("TURA_CODEX_HEADER_PROFILE")
        .unwrap_or_else(|_| "tura".to_string())
        .to_ascii_lowercase();
    if header_profile != "official" {
        request = request.header("session_id", "tura-codex-validation");
    }

    if let Ok(account_id) = std::env::var("OPENAI_ACCOUNT_ID")
        && !account_id.trim().is_empty()
    {
        request = request.header("ChatGPT-Account-Id", account_id);
    }

    let resp = send_provider_request_first_response(request).await?;
    let status = resp.status();
    let req_id = resp
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    if !status.is_success() {
        let body = resp
            .text()
            .await
            .map_err(|err| provider_transport_error(ProviderTransportPhase::ResponseBody, &err))?;
        return Err(TuraError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }

    let data = parse_codex_response_stream(resp, stream_events).await?;
    validate_responses_status("codex", &data)?;
    let mut content = normalize_codex_response_content(&data);
    if let Some(text) = content.as_str() {
        content = Value::String(strip_json_fence(text));
    }
    let mut metrics = extract_openapi_metrics(&data, options.context_window);
    metrics.cost = CostDetails::default();
    metrics.provider_request_id = req_id;
    Ok(ProviderResponse {
        content,
        raw: data,
        metrics: Some(metrics),
    })
}

/// Drive a standard (API-key) OpenAI-style **Responses API** endpoint
/// (`{base_url}/responses`). This is the shared core for the non-codex
/// Responses tier — `chatgpt` (OpenAI key), `grok` (xAI), `qwen` (DashScope
/// international). It reuses the codex Responses payload/stream machinery; the
/// only per-provider divergence is captured by [`ResponsesProfile`].
pub(crate) async fn responses_api_key_call(
    provider: &str,
    base_url: &str,
    model: &str,
    api_key: &str,
    messages: &[Value],
    options: &CallOptions,
    stream_events: Option<ProviderStreamEventSink>,
) -> Result<ProviderResponse, TuraError> {
    let profile = ResponsesProfile::for_provider(provider);
    let client = reqwest::Client::builder()
        .build()
        .map_err(|err| provider_transport_error(ProviderTransportPhase::ClientBuild, &err))?;
    let payload = build_responses_payload(profile, model, messages, options);

    let endpoint = format!("{}/responses", base_url.trim_end_matches('/'));
    let request = client.post(endpoint).bearer_auth(api_key).json(&payload);

    let resp = send_provider_request_first_response(request).await?;
    let status = resp.status();
    let req_id = resp
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    if !status.is_success() {
        let body = resp
            .text()
            .await
            .map_err(|err| provider_transport_error(ProviderTransportPhase::ResponseBody, &err))?;
        return Err(TuraError::HttpStatus {
            status: status.as_u16(),
            body,
        });
    }

    let data = parse_codex_response_stream(resp, stream_events).await?;
    validate_responses_status(profile.provider, &data)?;
    let mut content = normalize_codex_response_content(&data);
    if let Some(text) = content.as_str() {
        content = Value::String(strip_json_fence(text));
    }
    let mut metrics = extract_openapi_metrics(&data, options.context_window);
    metrics.provider_request_id = req_id;
    Ok(ProviderResponse {
        content,
        raw: data,
        metrics: Some(metrics),
    })
}

fn codex_cli_user_agent() -> String {
    "codex_cli_rs/0.0.0 (Windows 10.0; x86_64)".to_string()
}

/// Per-provider behaviour of the shared Responses-API payload builder. Codex
/// (OAuth) and the API-key Responses tier (`chatgpt`, `grok`, `qwen`) share the
/// same request shape; the few divergences live in this quirk layer.
#[derive(Clone, Copy)]
struct ResponsesProfile {
    /// Provider id, used for quirk dispatch and diagnostics.
    provider: &'static str,
    /// Request `reasoning.encrypted_content` in `include`. Required for the
    /// OpenAI family (codex/chatgpt) when running stateless (`store:false`) so
    /// reasoning can be carried across turns. xAI/Qwen don't accept it.
    include_encrypted_reasoning: bool,
    /// Forward the `service_tier` acceleration knob. Only the OpenAI family
    /// (codex/chatgpt) accepts it; xAI rejects it with
    /// `400 Argument not supported: service_tier`, and Qwen ignores/rejects it.
    include_service_tier: bool,
}

impl ResponsesProfile {
    fn for_provider(provider: &str) -> Self {
        let canonical = canonical_responses_provider(provider);
        let is_openai_family = matches!(canonical, "codex" | "chatgpt" | "openai");
        Self {
            provider: canonical,
            include_encrypted_reasoning: is_openai_family,
            include_service_tier: is_openai_family,
        }
    }
}

/// Map a runtime provider id onto its canonical Responses sub-branch.
fn canonical_responses_provider(provider: &str) -> &'static str {
    match provider.to_ascii_lowercase().as_str() {
        "codex" => "codex",
        "openai" | "openai-api" | "openai-oauth" | "chatgpt" => "chatgpt",
        "xai" | "grok" => "grok",
        "qwen" | "qwen_cn" | "qwen-cn" => "qwen",
        _ => "chatgpt",
    }
}

pub(crate) fn build_codex_oauth_payload(
    model: &str,
    messages: &[Value],
    options: &CallOptions,
) -> Value {
    build_responses_payload(
        ResponsesProfile::for_provider("codex"),
        model,
        messages,
        options,
    )
}

#[cfg(test)]
pub(crate) fn build_responses_payload_for_provider(
    provider: &str,
    model: &str,
    messages: &[Value],
    options: &CallOptions,
) -> Value {
    build_responses_payload(
        ResponsesProfile::for_provider(provider),
        model,
        messages,
        options,
    )
}

fn build_responses_payload(
    profile: ResponsesProfile,
    model: &str,
    messages: &[Value],
    options: &CallOptions,
) -> Value {
    let mut input = Vec::new();
    let instructions =
        "Follow the user request and Operation Manual, and answer concisely.".to_string();
    for message in messages {
        if message.get("type").and_then(Value::as_str) == Some("reasoning")
            && message.get("role").is_none()
        {
            if profile.include_encrypted_reasoning
                && message
                    .get("id")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty())
                && message
                    .get("encrypted_content")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty())
                && message.get("summary").is_some_and(Value::is_array)
            {
                input.push(message.clone());
            }
            continue;
        }
        if matches!(
            message.get("type").and_then(Value::as_str),
            Some("function_call" | "function_call_output")
        ) {
            input.push(message.clone());
            continue;
        }
        let role = message
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("user");
        let content_text = message_content_text(message.get("content")).unwrap_or_default();
        let content = if profile.provider == "codex" {
            codex_responses_content_from_canonical(role, message.get("content"))
                .unwrap_or_else(|| codex_text_content(role, content_text.clone()))
        } else {
            openai_responses_content_from_canonical(message.get("content"))
                .unwrap_or_else(|| Value::String(content_text.clone()))
        };
        input.push(json!({
            "role": codex_input_role(role),
            "content": content,
        }));
    }
    let mut payload = json!({
        "model": model,
        "instructions": instructions,
        "store": false,
        "stream": true,
        "input": if input.is_empty() {
            vec![json!({"role": "user", "content": ""})]
        } else {
            input
        },
    });
    if let Some(tools) = &options.tools {
        payload["tools"] = Value::Array(tools.iter().map(codex_tool_schema).collect());
    }
    if let Some(reasoning_effort) = normalized_reasoning_effort(options, model) {
        payload["reasoning"] = json!({ "effort": reasoning_effort });
        if profile.include_encrypted_reasoning {
            payload["include"] = json!(["reasoning.encrypted_content"]);
        }
    }
    let _ = profile.provider;
    payload["tool_choice"] = options
        .tool_choice
        .as_ref()
        .map(normalize_codex_tool_choice)
        .unwrap_or_else(|| Value::String("auto".to_string()));
    insert_opt(
        &mut payload,
        "parallel_tool_calls",
        options.parallel_tool_calls.map(Value::from),
    );
    insert_opt(
        &mut payload,
        "prompt_cache_key",
        options.prompt_cache_key.clone().map(Value::from),
    );
    insert_opt(
        &mut payload,
        "metadata",
        options.metadata.clone().map(|v| json!(v)),
    );
    if profile.include_service_tier {
        insert_opt(
            &mut payload,
            "service_tier",
            normalized_service_tier(options).map(Value::from),
        );
    }
    if let Some(extra_body) = &options.extra_body {
        deep_merge_json(&mut payload, extra_body.clone());
    }

    payload
}

fn codex_responses_content_from_canonical(role: &str, content: Option<&Value>) -> Option<Value> {
    match openai_responses_content_from_canonical(content)? {
        Value::String(text) => Some(codex_text_content(role, text)),
        Value::Array(items) => Some(Value::Array(
            items
                .into_iter()
                .map(|item| codex_content_item_for_role(role, item))
                .collect(),
        )),
        other => Some(other),
    }
}

fn codex_content_item_for_role(role: &str, item: Value) -> Value {
    let Some(kind) = item.get("type").and_then(Value::as_str) else {
        return item;
    };
    let text = item
        .get("text")
        .and_then(Value::as_str)
        .or_else(|| item.get("content").and_then(Value::as_str));
    if matches!(kind, "input_text" | "text" | "output_text")
        && let Some(text) = text
    {
        return codex_text_item(role, text.to_string());
    }
    item
}

fn codex_text_content(role: &str, text: String) -> Value {
    Value::Array(vec![codex_text_item(role, text)])
}

fn codex_text_item(role: &str, text: String) -> Value {
    let kind = if role == "assistant" {
        "output_text"
    } else {
        "input_text"
    };
    json!({ "type": kind, "text": text })
}

async fn parse_codex_response_stream(
    resp: reqwest::Response,
    stream_events: Option<ProviderStreamEventSink>,
) -> Result<Value, TuraError> {
    parse_codex_response_chunks(resp.bytes_stream(), stream_events).await
}

pub(crate) async fn parse_codex_response_chunks<S, B>(
    mut stream: S,
    stream_events: Option<ProviderStreamEventSink>,
) -> Result<Value, TuraError>
where
    S: futures_util::Stream<Item = Result<B, reqwest::Error>> + Unpin,
    B: AsRef<[u8]>,
{
    let mut pending = Vec::new();
    let mut output_text = String::new();
    let mut completed = None;
    let mut events = Vec::new();
    let mut command_collector = CodexCommandRunCommandCollector::default();
    let mut saw_output = false;
    let mut last_output_at = Instant::now();

    while let Some(chunk) =
        next_provider_stream_chunk(&mut stream, saw_output, last_output_at).await?
    {
        let chunk = chunk
            .map_err(|err| provider_transport_error(ProviderTransportPhase::ResponsesSse, &err))?;
        pending.extend_from_slice(chunk.as_ref());

        while let Some(line_end) = pending.iter().position(|byte| *byte == b'\n') {
            let output_event = process_codex_sse_line(
                decode_codex_sse_line(&pending[..line_end])?,
                &mut output_text,
                &mut completed,
                &mut events,
                &mut command_collector,
                stream_events.as_ref(),
            )?;
            pending.drain(..=line_end);
            if output_event {
                saw_output = true;
                last_output_at = Instant::now();
            }
        }
    }

    if !pending.is_empty() {
        let _ = process_codex_sse_line(
            decode_codex_sse_line(&pending)?,
            &mut output_text,
            &mut completed,
            &mut events,
            &mut command_collector,
            stream_events.as_ref(),
        )?;
    }

    // Keep provider failures available to the caller's existing status validator.
    // Otherwise a snapshot or [DONE] alone is not proof of completed output.
    let provider_failure = completed
        .as_ref()
        .and_then(|response| response.get("status"))
        .and_then(Value::as_str)
        .is_some_and(|status| !matches!(status, "completed" | "in_progress" | "queued"));
    let valid_completion = completed.as_ref().is_some_and(|response| {
        response.is_object()
            && response.get("status").is_none_or(|status| status == "completed")
            && response.get("error").is_none_or(Value::is_null)
            && events.iter().any(|event| {
                event["type"] == "response.completed" && event.get("response") == Some(response)
            })
    }) && !events.iter().any(|event| {
        matches!(
            event.get("type").and_then(Value::as_str),
            Some("response.failed" | "response.incomplete" | "error")
        )
    });
    if !provider_failure && !valid_completion {
        return Err(TuraError::Network {
            message: "provider stream failure: phase=responses-sse category=protocol cause=missing-completed"
                .to_string(),
        });
    }

    Ok(build_codex_stream_root(output_text, completed, events))
}

fn decode_codex_sse_line(bytes: &[u8]) -> Result<&str, TuraError> {
    std::str::from_utf8(bytes)
        .map(|line| line.trim_end_matches('\r'))
        .map_err(|_| TuraError::Network {
            message: "provider stream failure: phase=responses-sse category=decode cause=invalid-utf8"
                .to_string(),
        })
}

fn process_codex_sse_line(
    line: &str,
    output_text: &mut String,
    completed: &mut Option<Value>,
    events: &mut Vec<Value>,
    command_collector: &mut CodexCommandRunCommandCollector,
    stream_events: Option<&ProviderStreamEventSink>,
) -> Result<bool, TuraError> {
    let line = line.trim_start();
    let Some(data) = line.strip_prefix("data:") else {
        return Ok(false);
    };
    let data = data.trim();
    if data.is_empty() || data == "[DONE]" {
        return Ok(false);
    }

    let value: Value = serde_json::from_str(data).map_err(TuraError::Json)?;
    append_codex_stream_text(&value, output_text);
    if let Some(response) = value.get("response") {
        *completed = Some(response.clone());
    }
    let output_event = is_codex_stream_output_start(&value);
    if let Some(sink) = stream_events {
        if output_event {
            sink(ProviderStreamEvent::ProviderOutputStarted);
        }
        if value.get("type").and_then(Value::as_str) == Some("response.output_text.delta")
            && let Some(delta) = value
                .get("delta")
                .and_then(Value::as_str)
                .filter(|delta| !delta.is_empty())
        {
            sink(ProviderStreamEvent::TextDelta {
                text: delta.to_string(),
            });
        }
        for event in command_collector.push_event(&value) {
            sink(event);
        }
    }
    events.push(value);

    Ok(output_event)
}

fn is_codex_stream_output_start(value: &Value) -> bool {
    matches!(
        value.get("type").and_then(Value::as_str),
        Some(
            "response.output_text.delta"
                | "response.function_call_arguments.delta"
                | "response.output_item.added"
                | "response.content_part.added"
        )
    )
}

pub(crate) fn append_codex_stream_text(value: &Value, output_text: &mut String) {
    if matches!(
        value.get("type").and_then(Value::as_str),
        Some(
            "response.function_call_arguments.delta"
                | "response.function_call_arguments.done"
                | "response.output_item.added"
                | "response.output_item.done"
        )
    ) {
        return;
    }

    if let Some(delta) = value
        .get("type")
        .and_then(Value::as_str)
        .filter(|event_type| event_type.ends_with(".delta"))
        .and_then(|_| value.get("delta").and_then(Value::as_str))
    {
        output_text.push_str(delta);
        return;
    }

    if let Some(delta) = value
        .get("delta")
        .and_then(Value::as_str)
        .or_else(|| value.pointer("/response/delta").and_then(Value::as_str))
    {
        output_text.push_str(delta);
    }
}

fn build_codex_stream_root(
    output_text: String,
    completed: Option<Value>,
    events: Vec<Value>,
) -> Value {
    let mut root = completed.unwrap_or_else(|| json!({ "output": [] }));
    if let Some(items) = completed_stream_output(&root, &events) {
        root["output"] = Value::Array(items);
        root["stream_output_source"] = json!("completed_output_item_events");
    }
    if !output_text.is_empty() {
        root["output_text"] = Value::String(output_text);
    }
    root["events"] = Value::Array(events);
    root
}

fn completed_stream_output(final_response: &Value, events: &[Value]) -> Option<Vec<Value>> {
    if final_response["status"] != "completed"
        || final_response.get("error").is_some_and(|e| !e.is_null())
    {
        return None;
    }
    if let Some(output) = final_response.get("output") {
        if !output.as_array().is_some_and(Vec::is_empty) {
            return None;
        }
    }
    let response_id = final_response["id"].as_str().filter(|id| !id.is_empty())?;
    let mut created = false;
    let mut finished = false;
    let mut added = std::collections::BTreeMap::new();
    let mut done = std::collections::BTreeMap::new();
    for event in events {
        if event
            .get("response")
            .is_some_and(|r| r["id"] != response_id)
        {
            return None;
        }
        match event["type"].as_str() {
            Some("response.created") => {
                if created || finished || !added.is_empty() {
                    return None;
                }
                created = true;
            }
            Some("response.completed") => {
                if !created || finished || event["response"] != *final_response {
                    return None;
                }
                finished = true;
            }
            Some("response.failed" | "response.incomplete" | "error") => return None,
            Some("response.output_item.added" | "response.output_item.done") => {
                if !created || finished {
                    return None;
                }
                let index = event["output_index"].as_u64()?;
                let item = &event["item"];
                let id = item["id"].as_str().filter(|id| !id.is_empty())?;
                let kind = item["type"].as_str().filter(|kind| !kind.is_empty())?;
                let identity = (id.to_owned(), kind.to_owned());
                if event["type"] == "response.output_item.added" {
                    if done.contains_key(&index) {
                        return None;
                    }
                    if added
                        .insert(index, identity.clone())
                        .is_some_and(|old| old != identity)
                    {
                        return None;
                    }
                } else {
                    if added.get(&index) != Some(&identity) {
                        return None;
                    }
                    if done
                        .insert(index, item.clone())
                        .is_some_and(|old| old != *item)
                    {
                        return None;
                    }
                }
            }
            _ => {}
        }
    }
    if !created || !finished || done.is_empty() || added.len() != done.len() {
        return None;
    }
    let mut ids = std::collections::HashSet::new();
    for (expected, (index, item)) in done.iter().enumerate() {
        let identity = (
            item["id"].as_str()?.to_owned(),
            item["type"].as_str()?.to_owned(),
        );
        if *index != expected as u64
            || added.get(index) != Some(&identity)
            || !ids.insert(identity.0)
        {
            return None;
        }
    }
    Some(done.into_values().collect())
}

fn validate_responses_status(provider: &str, data: &Value) -> Result<(), TuraError> {
    let Some(status) = data.get("status").and_then(Value::as_str) else {
        return Ok(());
    };
    if matches!(status, "completed" | "in_progress" | "queued") {
        return Ok(());
    }

    let detail = data
        .get("incomplete_details")
        .or_else(|| data.get("error"))
        .map(|value| value.to_string())
        .unwrap_or_else(|| "no detail".to_string());
    Err(TuraError::ProviderRequest {
        provider: provider.to_string(),
        message: format!("responses api returned status '{status}': {detail}"),
    })
}

fn openai_codex_endpoint() -> String {
    std::env::var("OPENAI_CODEX_ENDPOINT")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "https://chatgpt.com/backend-api/codex/responses".to_string())
}

fn codex_input_role(role: &str) -> &str {
    match role {
        "assistant" => "assistant",
        "system" => "system",
        "developer" => "developer",
        _ => "user",
    }
}

pub(crate) fn normalize_codex_response_content(data: &Value) -> Value {
    if let Some(content) = normalize_codex_response_event_content(data) {
        return content;
    }
    normalize_response_content(data)
}

pub(crate) fn normalize_codex_response_event_content(data: &Value) -> Option<Value> {
    let tool_calls = complete_codex_tool_calls(data);
    if !tool_calls.is_empty() {
        let mut object = serde_json::Map::new();
        if let Some(text) = data.get("output_text").and_then(Value::as_str)
            && !text.trim().is_empty()
        {
            object.insert("text".to_string(), Value::String(text.to_string()));
        }
        object.insert("tool_calls".to_string(), Value::Array(tool_calls));
        return Some(Value::Object(object));
    }

    if let Some(text) = data.get("output_text").and_then(Value::as_str) {
        return Some(Value::String(text.to_string()));
    }
    if let Some(text) = data
        .get("output")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .flat_map(|item| {
            item.get("content")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
        })
        .find_map(|content| {
            content
                .get("text")
                .and_then(Value::as_str)
                .or_else(|| content.get("content").and_then(Value::as_str))
        })
    {
        return Some(Value::String(text.to_string()));
    }
    None
}

fn codex_tool_schema(tool: &Value) -> Value {
    if tool.get("name").and_then(Value::as_str).is_some() {
        return tool.clone();
    }

    let Some(function) = tool.get("function").and_then(Value::as_object) else {
        return tool.clone();
    };
    let mut converted = serde_json::Map::new();
    converted.insert(
        "type".to_string(),
        tool.get("type")
            .cloned()
            .unwrap_or_else(|| Value::String("function".to_string())),
    );
    for key in ["name", "description", "parameters", "strict"] {
        if let Some(value) = function.get(key) {
            converted.insert(key.to_string(), value.clone());
        }
    }
    Value::Object(converted)
}

fn normalize_codex_tool_choice(tool_choice: &Value) -> Value {
    if tool_choice.get("type").and_then(Value::as_str) == Some("function")
        && let Some(name) = tool_choice
            .get("function")
            .and_then(|function| function.get("name"))
            .and_then(Value::as_str)
            .filter(|name| !name.trim().is_empty())
    {
        return json!({
            "type": "function",
            "name": name,
        });
    }
    tool_choice.clone()
}

fn extract_codex_tool_calls(data: &Value) -> Vec<Value> {
    let mut calls = Vec::new();
    if let Some(output) = data.get("output").and_then(Value::as_array) {
        for item in output {
            if let Some(call) = codex_output_item_tool_call(item) {
                calls.push(call);
            }
        }
    }
    if let Some(events) = data.get("events").and_then(Value::as_array) {
        calls.extend(codex_event_tool_calls(events));
    }
    dedupe_tool_calls(calls)
}

pub(crate) fn complete_codex_tool_calls(data: &Value) -> Vec<Value> {
    extract_codex_tool_calls(data)
        .into_iter()
        .filter_map(ready_streaming_tool_call)
        .collect()
}

pub(crate) fn ready_streaming_tool_call(call: Value) -> Option<Value> {
    let name = call
        .get("function")
        .and_then(|function| function.get("name"))
        .and_then(Value::as_str)?;
    let arguments = call
        .get("function")
        .and_then(|function| function.get("arguments"))?
        .clone();

    if name == "command_run" {
        let text = match &arguments {
            Value::String(text) => text.as_str(),
            other => return Some(call_with_arguments(call, other.clone())),
        };
        if let Ok(arguments) = serde_json::from_str::<Value>(text) {
            return Some(call_with_arguments(call, arguments));
        }
        return None;
    }

    tool_call_arguments_complete(&arguments).then_some(call)
}

fn call_with_arguments(mut call: Value, arguments: Value) -> Value {
    if let Some(function) = call.get_mut("function").and_then(Value::as_object_mut) {
        function.insert("arguments".to_string(), arguments);
    }
    call
}

fn tool_call_arguments_complete(arguments: &Value) -> bool {
    match arguments {
        Value::String(text) => serde_json::from_str::<Value>(text).is_ok(),
        Value::Object(_) => true,
        _ => false,
    }
}

fn codex_output_item_tool_call(item: &Value) -> Option<Value> {
    let item_type = item.get("type").and_then(Value::as_str)?;
    if !matches!(item_type, "function_call" | "tool_call") {
        return None;
    }
    let name = item.get("name").and_then(Value::as_str)?;
    let arguments = item
        .get("arguments")
        .cloned()
        .or_else(|| item.get("input").cloned())
        .unwrap_or_else(|| json!({}));
    let arguments = match arguments {
        Value::String(text) => Value::String(text),
        other => Value::String(other.to_string()),
    };
    let id = item
        .get("call_id")
        .or_else(|| item.get("id"))
        .and_then(Value::as_str)
        .unwrap_or("codex_tool_call");
    Some(codex_tool_call_value(id, name, arguments))
}

#[path = "response_tool_stream.rs"]
mod response_tool_stream;
#[cfg(test)]
pub(crate) use response_tool_stream::CodexToolCallStreamCollector;
pub(crate) use response_tool_stream::{CodexCommandRunCommandCollector, codex_event_tool_calls};
fn codex_tool_call_value(id: &str, name: &str, arguments: Value) -> Value {
    json!({
        "id": id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        }
    })
}

fn dedupe_tool_calls(calls: Vec<Value>) -> Vec<Value> {
    let mut positions = std::collections::HashMap::<String, usize>::new();
    let mut unique = Vec::new();
    for call in calls {
        let key = call
            .get("id")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .unwrap_or_else(|| call.to_string());
        if let Some(index) = positions.get(&key).copied() {
            if tool_call_argument_score(&call) >= tool_call_argument_score(&unique[index]) {
                unique[index] = call;
            }
        } else {
            positions.insert(key, unique.len());
            unique.push(call);
        }
    }
    unique
}

#[cfg(test)]
mod continuity_tests {
    use super::*;

    fn completed_item_stream() -> (Value, Vec<Value>, Vec<Value>) {
        let items = vec![
            json!({"type":"reasoning", "id":"r1", "summary":[], "encrypted_content":"exact-opaque"}),
            json!({"type":"function_call", "id":"fc1", "call_id":"c1", "name":"command_run", "arguments":"{}"}),
        ];
        let final_response =
            json!({"id":"resp1", "status":"completed", "model":"model", "output":[]});
        let mut events = vec![
            json!({"type":"response.created", "response":{"id":"resp1", "status":"in_progress"}}),
        ];
        for (index, item) in items.iter().enumerate() {
            events.push(json!({"type":"response.output_item.added", "output_index":index, "item":{"id":item["id"], "type":item["type"]}}));
            events.push(
                json!({"type":"response.output_item.done", "output_index":index, "item":item}),
            );
        }
        events.push(json!({"type":"response.completed", "response":final_response}));
        (final_response, events, items)
    }

    #[test]
    fn completed_stream_restores_exact_done_items_when_final_snapshot_is_empty() {
        let (final_response, events, items) = completed_item_stream();
        let root = build_codex_stream_root(String::new(), Some(final_response), events);
        assert_eq!(root["output"], json!(items));
        assert_eq!(root["output"][0]["encrypted_content"], "exact-opaque");
    }

    #[test]
    fn completed_stream_keeps_authoritative_snapshot_and_rejects_uncertain_events() {
        let (mut final_response, events, _) = completed_item_stream();
        final_response["output"] = json!([{"type":"message", "id":"canonical"}]);
        assert!(completed_stream_output(&final_response, &events).is_none());
        let (final_response, events, _) = completed_item_stream();
        let mut cross_response = events.clone();
        cross_response[0]["response"]["id"] = json!("other-response");
        assert!(completed_stream_output(&final_response, &cross_response).is_none());
        for case in 0..11 {
            let mut invalid = events.clone();
            match case {
                0 => {
                    invalid.remove(0);
                }
                1 => {
                    invalid.pop();
                }
                2 => {
                    invalid.remove(2);
                }
                3 => {
                    invalid[3]["output_index"] = json!(9);
                    invalid[4]["output_index"] = json!(9);
                }
                4 => {
                    let mut conflict = invalid[2].clone();
                    conflict["item"]["encrypted_content"] = json!("changed");
                    invalid.insert(3, conflict);
                }
                5 => {
                    invalid[3]["item"]["id"] = json!("mismatch");
                }
                6 => {
                    invalid.swap(1, 2);
                }
                7 => {
                    invalid.swap(4, 5);
                }
                8 => {
                    invalid.swap(0, 1);
                }
                9 => {
                    invalid[5]["response"]["model"] = json!("other-model");
                }
                _ => {
                    invalid.insert(3, invalid[1].clone());
                }
            }
            assert!(
                completed_stream_output(&final_response, &invalid).is_none(),
                "case {case}"
            );
        }
        let mut partial = final_response.clone();
        partial["status"] = json!("in_progress");
        assert!(completed_stream_output(&partial, &events).is_none());
    }

    #[test]
    fn completed_stream_duplicate_done_is_idempotent_and_sse_parser_keeps_items() {
        let (final_response, mut events, items) = completed_item_stream();
        events.insert(3, events[2].clone());
        assert_eq!(
            completed_stream_output(&final_response, &events),
            Some(items.clone())
        );
        let mut output_text = String::new();
        let mut completed = None;
        let mut parsed = Vec::new();
        let mut collector = CodexCommandRunCommandCollector::default();
        for event in events {
            process_codex_sse_line(
                &format!("data: {event}"),
                &mut output_text,
                &mut completed,
                &mut parsed,
                &mut collector,
                None,
            )
            .unwrap();
        }
        let root = build_codex_stream_root(output_text, completed, parsed);
        assert_eq!(root["output"], json!(items));
        assert_eq!(root["model"], "model");
    }

    #[test]
    fn responses_continuity_builder_keeps_exact_items_and_call_order() {
        let item = json!({"type":"reasoning", "id":"r1", "summary":[],
            "encrypted_content":"synthetic-opaque", "status":"completed"});
        let input = vec![
            item.clone(),
            json!({"type":"function_call", "name":"command_run", "call_id":"c1", "arguments":"{}"}),
            json!({"type":"function_call_output", "call_id":"c1", "output":"ok"}),
        ];
        let payload = build_codex_oauth_payload("model", &input, &CallOptions::default());
        assert_eq!(payload["input"], json!(input));
        assert_eq!(payload["store"], false);
    }

    #[test]
    fn responses_continuity_builder_excludes_plaintext_and_incompatible_provider() {
        let opaque = json!({"type":"reasoning", "id":"r1", "summary":[], "encrypted_content":"synthetic-opaque"});
        let plain = json!({"type":"reasoning", "id":"r2", "summary":[{"text":"not opaque"}]});
        let user = json!({"role":"user", "content":"question"});
        let payload =
            build_codex_oauth_payload("model", &[plain, user.clone()], &CallOptions::default());
        assert_eq!(payload["input"].as_array().unwrap().len(), 1);
        assert_eq!(payload["input"][0]["role"], "user");
        let other = build_responses_payload_for_provider(
            "grok",
            "model",
            &[opaque, user],
            &CallOptions::default(),
        );
        assert_eq!(other["input"].as_array().unwrap().len(), 1);
        assert_eq!(other["input"][0]["role"], "user");
    }

    #[test]
    fn responses_continuity_keeps_existing_role_based_system_context() {
        let message = json!({"role":"system", "type":"reasoning", "content":"existing context"});
        let payload = build_codex_oauth_payload("model", &[message], &CallOptions::default());
        assert_eq!(payload["input"][0]["role"], "system");
        assert_eq!(
            payload["input"][0]["content"][0]["text"],
            "existing context"
        );
    }
}

fn tool_call_argument_score(call: &Value) -> usize {
    let Some(arguments) = call
        .get("function")
        .and_then(|function| function.get("arguments"))
    else {
        return 0;
    };
    match arguments {
        Value::String(text) => text.trim().len(),
        Value::Object(object) => object.len().max(1),
        Value::Array(array) => array.len().max(1),
        _ => 0,
    }
}

use crate::tura_llm::CallOptions;
