//! The OpenAI-style **Responses API** tier (`/responses`, SSE stream).
//!
//! This is the shared core for codex (OAuth) and the API-key Responses
//! sub-providers — `chatgpt` (OpenAI), `grok` (xAI), `qwen` (DashScope
//! international). All of them speak the same request/stream shape; the few
//! per-provider divergences are captured by [`ResponsesProfile`] as a small
//! provider quirk layer.

use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};
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
    let mut payload = build_codex_oauth_payload(model, messages, options);
    let base = codex_base_instructions(model);
    if let Some(base) = &base {
        payload["instructions"] = Value::String(base.text.to_string());
    }
    let header_profile = std::env::var("TURA_CODEX_HEADER_PROFILE")
        .unwrap_or_else(|_| "tura".to_string())
        .to_ascii_lowercase();
    let affinity = (header_profile != "official").then(|| codex_session_affinity(options));
    let request_profile = codex_request_profile(
        &payload,
        base.as_ref(),
        affinity.as_ref().map_or("none", |(_, source)| *source),
    );

    let mut request = client
        .post(openai_codex_endpoint())
        .bearer_auth(access_token)
        .header("originator", "codex_cli_rs")
        .header("User-Agent", codex_cli_user_agent())
        .json(&payload);

    if let Some((session, _)) = &affinity {
        request = request.header("session_id", session.as_str());
    }

    if let Ok(account_id) = std::env::var("OPENAI_ACCOUNT_ID")
        && !account_id.trim().is_empty()
    {
        request = request.header("ChatGPT-Account-Id", account_id);
    }

    let request_started = Instant::now();
    let resp = send_provider_request_first_response(request).await?;
    let response_headers_ms = elapsed_ms(request_started);
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

    let (data, timing) = parse_codex_response_chunks_timed(
        resp.bytes_stream(), stream_events, request_started, response_headers_ms,
    ).await.map_err(|error| error.with_stream_request_id(req_id.as_deref()))?;
    validate_responses_status_with_usage("codex", model, req_id.as_deref(), &data)?;
    let mut content = normalize_codex_response_content(&data);
    if let Some(text) = content.as_str() {
        content = Value::String(strip_json_fence(text));
    }
    let mut metrics = extract_openapi_metrics(&data, options.context_window);
    metrics.cost = CostDetails::default();
    metrics.provider_request_id = req_id;
    metrics.transport_timing = Some(timing);
    metrics.request_profile = Some(request_profile);
    Ok(ProviderResponse {
        content,
        raw: data,
        metrics: Some(metrics),
        failed_attempts: Vec::new(),
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

    let data = parse_codex_response_stream(resp, stream_events)
        .await.map_err(|error| error.with_stream_request_id(req_id.as_deref()))?;
    validate_responses_status_with_usage(profile.provider, model, req_id.as_deref(), &data)?;
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
        failed_attempts: Vec::new(),
    })
}

fn codex_cli_user_agent() -> String {
    "codex_cli_rs/0.0.0 (Windows 10.0; x86_64)".to_string()
}

/// Official base instructions for one model plus where they were found.
#[derive(Clone)]
struct BaseInstructions {
    text: Arc<str>,
    /// `model_catalog` (config.toml `model_catalog_json`) or `models_cache`.
    source: &'static str,
    catalog: Arc<str>,
}

fn generic_instructions_requested() -> bool {
    std::env::var("TURA_CODEX_BASE_INSTRUCTIONS")
        .is_ok_and(|value| value.trim().eq_ignore_ascii_case("generic"))
}

/// Every Nokiy worker sends the same official instructions and tool schemas, a
/// prefix no other client shares. One affinity key for all workers keeps it warm
/// across workers and runs: a per-session key (V248) measured 0 cached tokens on
/// every first call versus 7808 with this shared key, while follow-up misses
/// under the shared key were 1 of 49 calls.
const WORKER_SESSION_AFFINITY: &str = "tura-codex-validation";

fn codex_session_affinity(_options: &CallOptions) -> (String, &'static str) {
    (WORKER_SESSION_AFFINITY.to_string(), "shared")
}

/// Local provenance of the instructions a Codex request carried, so each
/// worker call can be traced to its catalog entry or the generic fallback.
fn codex_request_profile(
    payload: &Value,
    base: Option<&BaseInstructions>,
    session_affinity: &str,
) -> Value {
    let instructions = payload
        .get("instructions")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let source = match base {
        Some(base) => base.source,
        None if generic_instructions_requested() => "generic_requested",
        None => "generic_no_catalog_entry",
    };
    json!({
        "schema_version": "provider_request_profile_v1",
        "instructions_source": source,
        "instructions_catalog": base.map(|base| base.catalog.as_ref()),
        "instructions_bytes": instructions.len(),
        "instructions_sha256_16": sha256_prefix(instructions),
        "session_affinity": session_affinity,
    })
}

/// Native Codex sends each model's official base instructions as
/// `instructions`, taken from its model catalog: `model_catalog_json` in
/// `CODEX_HOME/config.toml` when configured, else `CODEX_HOME/models_cache.json`.
/// With only the generic line instead, gpt-6.1-sol at max effort spends a
/// ~516-token reasoning block (~10 s) on every exploratory call.
/// `TURA_CODEX_BASE_INSTRUCTIONS=generic` keeps the generic line.
fn codex_base_instructions(model: &str) -> Option<BaseInstructions> {
    if generic_instructions_requested() {
        return None;
    }
    static CACHE: OnceLock<Mutex<HashMap<String, Option<BaseInstructions>>>> = OnceLock::new();
    let slug = model.rsplit('/').next().unwrap_or(model).to_ascii_lowercase();
    let mut cache = CACHE
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    cache
        .entry(slug.clone())
        .or_insert_with(|| {
            let home = crate::tura_llm::codex_home_dir()?;
            let config = std::fs::read_to_string(home.join("config.toml")).unwrap_or_default();
            let catalogs = configured_model_catalog(&config, &home)
                .map(|path| ("model_catalog", path))
                .into_iter()
                .chain([("models_cache", home.join("models_cache.json"))]);
            for (source, path) in catalogs {
                let Some(text) = std::fs::read(&path)
                    .ok()
                    .and_then(|bytes| base_instructions_from_catalog(&bytes, &slug))
                else {
                    continue;
                };
                tracing::info!(
                    model = %slug,
                    catalog = %path.display(),
                    chars = text.len(),
                    sha256 = %sha256_prefix(&text),
                    "codex base instructions loaded from model catalog"
                );
                return Some(BaseInstructions {
                    text: Arc::from(text),
                    source,
                    catalog: Arc::from(path.display().to_string()),
                });
            }
            None
        })
        .clone()
}

/// Top-level `model_catalog_json` of a Codex config.toml (relative paths are
/// resolved against CODEX_HOME). Table sections end the top-level scope.
fn configured_model_catalog(config: &str, codex_home: &std::path::Path) -> Option<std::path::PathBuf> {
    for line in config.lines().map(str::trim) {
        if line.starts_with('[') {
            return None;
        }
        let Some(rest) = line.strip_prefix("model_catalog_json") else {
            continue;
        };
        let Some(value) = rest.trim_start().strip_prefix('=') else {
            continue;
        };
        let value = value.trim();
        let quote = value.chars().next().filter(|c| matches!(c, '"' | '\''))?;
        let path = value[1..].split(quote).next().filter(|path| !path.is_empty())?;
        return Some(codex_home.join(path));
    }
    None
}

fn base_instructions_from_catalog(bytes: &[u8], slug: &str) -> Option<String> {
    let catalog: Value = serde_json::from_slice(bytes).ok()?;
    let entry = catalog
        .get("models")?
        .as_array()?
        .iter()
        .find(|entry| entry.get("slug").and_then(Value::as_str) == Some(slug))?;
    ["/base_instructions", "/model_messages/instructions_template"]
        .iter()
        .filter_map(|pointer| entry.pointer(pointer).and_then(Value::as_str))
        .find(|text| !text.trim().is_empty())
        .map(str::to_string)
}

fn sha256_prefix(text: &str) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(text.as_bytes())
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect()
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
        "Follow the user request and Operation Manual. Provide the requested deliverable, coverage, format, and detail; be concise when requested.".to_string();
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

fn elapsed_ms(started: Instant) -> u64 {
    started.elapsed().as_millis().min(u128::from(u64::MAX)) as u64
}

async fn parse_codex_response_chunks_timed<S, B>(
    stream: S,
    downstream: Option<ProviderStreamEventSink>,
    started: Instant,
    response_headers_ms: u64,
) -> Result<(Value, crate::TransportTiming), TuraError>
where
    S: futures_util::Stream<Item = Result<B, reqwest::Error>> + Unpin,
    B: AsRef<[u8]>,
{
    let first = std::sync::Arc::new(std::sync::Mutex::new(None));
    let observed = first.clone();
    let sink: ProviderStreamEventSink = std::sync::Arc::new(move |event| {
        if matches!(event, ProviderStreamEvent::ProviderOutputStarted) {
            observed.lock().unwrap_or_else(|e| e.into_inner())
                .get_or_insert_with(|| elapsed_ms(started));
        }
        if let Some(sink) = &downstream {
            sink(event);
        }
    });
    let data = parse_codex_response_chunks(stream, Some(sink)).await?;
    let first_output_event_ms = *first.lock().unwrap_or_else(|e| e.into_inner());
    Ok((data, crate::TransportTiming {
        transport: "http_sse".into(), response_headers_ms,
        first_output_event_ms, completed_ms: elapsed_ms(started),
    }))
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
    let mut completed_frame_ended = false;

    // Record only parser predicates, never event bodies or undecoded bytes.
    macro_rules! observe_stream_failure {
        ($result:expr) => {
            $result.map_err(|source| TuraError::StreamFailure {
                source: Box::new(source),
                snapshot: crate::tura_llm::ProviderStreamFailureSnapshot {
                    schema_version: "provider_stream_failure_snapshot_v1".into(),
                    event_count: events.len(),
                    completed_seen: events.iter().any(|event: &Value| event["type"] == "response.completed"),
                    completion_valid: valid_codex_stream_completion(completed.as_ref(), &events),
                    completed_frame_ended,
                    pending_bytes: pending.len(),
                    request_id: None,
                    runtime_id: None,
                    command_effect_started: None,
                },
            })
        };
    }

    while let Some(chunk) =
        observe_stream_failure!(next_provider_stream_chunk(&mut stream, saw_output, last_output_at).await)?
    {
        let chunk = observe_stream_failure!(chunk
            .map_err(|err| provider_transport_error(ProviderTransportPhase::ResponsesSse, &err)))?;
        pending.extend_from_slice(chunk.as_ref());

        while let Some(line_end) = pending.iter().position(|byte| *byte == b'\n') {
            let line = decode_codex_sse_line(&pending[..line_end])?;
            completed_frame_ended |= line.is_empty()
                && events
                    .last()
                    .is_some_and(|event: &Value| event["type"] == "response.completed");
            let output_event = process_codex_sse_line(
                line,
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

        // Commit at a framed terminal event, not transport EOF. Drain the whole
        // delivered chunk first so contradictions and undecoded partial tails
        // cannot be discarded just because response.completed appeared.
        if completed_frame_ended
            && pending.is_empty()
            && valid_codex_stream_completion(completed.as_ref(), &events)
        {
            return Ok(build_codex_stream_root(output_text, completed, events));
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
    if !provider_failure && !valid_codex_stream_completion(completed.as_ref(), &events) {
        return observe_stream_failure!(Err(TuraError::Network {
            message: "provider stream failure: phase=responses-sse category=protocol cause=missing-completed"
                .to_string(),
        }));
    }

    Ok(build_codex_stream_root(output_text, completed, events))
}

fn valid_codex_stream_completion(completed: Option<&Value>, events: &[Value]) -> bool {
    let Some(response) = completed else {
        return false;
    };
    if !response.is_object()
        || response
            .get("status")
            .is_some_and(|status| status != "completed")
        || response.get("error").is_some_and(|error| !error.is_null())
        || response
            .get("id")
            .is_some_and(|id| id.as_str().is_none_or(str::is_empty))
    {
        return false;
    }

    // Completion is authoritative only for a consistent, failure-free history.
    // Missing IDs remain compatible, but an observed ID must match the terminal
    // response. A terminal event cannot be followed by a reopened response.
    let mut saw_completed = false;
    for event in events {
        let event_type = event.get("type").and_then(Value::as_str);
        if matches!(
            event_type,
            Some("response.failed" | "response.incomplete" | "error")
        ) || (saw_completed && event_type != Some("response.completed"))
            || event
                .get("response_id")
                .is_some_and(|id| Some(id) != response.get("id"))
        {
            return false;
        }
        if let Some(snapshot) = event.get("response")
            && (!snapshot.is_object()
                || snapshot.get("error").is_some_and(|error| !error.is_null())
                || snapshot.get("status").is_some_and(|status| {
                    !matches!(
                        status.as_str(),
                        Some("completed" | "in_progress" | "queued")
                    )
                })
                || snapshot
                    .get("id")
                    .is_some_and(|id| Some(id) != response.get("id")))
            {
                return false;
            }
        if event_type == Some("response.completed") {
            if event.get("response") != Some(response) {
                return false;
            }
            saw_completed = true;
        }
    }
    saw_completed
}

fn decode_codex_sse_line(bytes: &[u8]) -> Result<&str, TuraError> {
    std::str::from_utf8(bytes)
        .map(|line| line.trim_end_matches('\r'))
        .map_err(|_| TuraError::Network {
            message:
                "provider stream failure: phase=responses-sse category=decode cause=invalid-utf8"
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
    if let Some(output) = final_response.get("output")
        && !output.as_array().is_some_and(Vec::is_empty) {
            return None;
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

fn validate_responses_status_with_usage(
    provider: &str, model: &str, request_id: Option<&str>, data: &Value,
) -> Result<(), TuraError> {
    validate_responses_status(provider, data).map_err(|error| error.with_failed_attempts(vec![
        crate::failure_usage::FailedProviderAttempt::from_response(provider, model, request_id, data),
    ]))
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
        .filter(|value| !value.is_null())
        .or_else(|| data.get("error").filter(|value| !value.is_null()))
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
    if let Some(messages) = codex_public_messages(data) {
        // Keep each public message's phase associated with its text. A blank
        // final_answer must not turn another message's commentary into a final.
        let text = messages
            .iter()
            .filter_map(|message| message.get("text").and_then(Value::as_str))
            .filter(|text| !text.is_empty())
            .collect::<Vec<_>>()
            .join("\n");
        let text = if tool_calls.is_empty() {
            strip_json_fence(&text)
        } else {
            text
        };
        let mut content = json!({ "text": text, "public_messages": messages });
        if !tool_calls.is_empty() {
            content["tool_calls"] = Value::Array(tool_calls);
        }
        return Some(content);
    }
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

fn codex_public_messages(data: &Value) -> Option<Vec<Value>> {
    let is_public_message = |item: &&Value| {
        item.get("type").and_then(Value::as_str) == Some("message")
            && item
                .get("role")
                .and_then(Value::as_str)
                .is_none_or(|role| role == "assistant")
    };
    let mut items: Vec<&Value> = data
        .get("output")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(is_public_message)
        .collect();
    if items.is_empty() {
        items = data
            .get("events")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter(|event| {
                event.get("type").and_then(Value::as_str) == Some("response.output_item.done")
            })
            .filter_map(|event| event.get("item"))
            .filter(is_public_message)
            .collect();
    }
    if !items
        .iter()
        .any(|item| codex_public_message_phase(item).is_some())
    {
        // Leave legacy/unphased normalization, including its content shape,
        // unchanged. Only recognized public phases opt into this projection.
        return None;
    }
    Some(
        items
            .into_iter()
            .map(|item| {
                // Never copy raw items, reasoning, summaries, or encrypted data.
                let text: String = item
                    .get("content")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter(|part| part.get("type").and_then(Value::as_str) == Some("output_text"))
                    .filter_map(|part| part.get("text").and_then(Value::as_str))
                    .collect();
                json!({ "phase": codex_public_message_phase(item), "text": text })
            })
            .collect(),
    )
}

fn codex_public_message_phase(item: &Value) -> Option<&str> {
    item.get("phase")
        .and_then(Value::as_str)
        .filter(|phase| matches!(*phase, "commentary" | "final_answer"))
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

#[cfg(test)]
mod continuity_tests {
    use super::*;

    #[test]
    fn base_instructions_come_from_the_matching_models_cache_entry() {
        let cache = json!({"models": [
            {"slug": "gpt-6-astra", "model_messages": {"instructions_template": "astra base"}},
            {"slug": "gpt-6.1-sol", "model_messages": {"instructions_template": "sol base"}},
            {"slug": "gpt-6-luna", "base_instructions": "luna base",
             "model_messages": {"instructions_template": "luna template"}},
            {"slug": "blank", "model_messages": {"instructions_template": "  "}},
            {"slug": "bare"}
        ]})
        .to_string();
        let read = |slug| base_instructions_from_catalog(cache.as_bytes(), slug);
        assert_eq!(read("gpt-6.1-sol").as_deref(), Some("sol base"));
        assert_eq!(read("gpt-6-astra").as_deref(), Some("astra base"));
        assert_eq!(read("gpt-6-luna").as_deref(), Some("luna base"));
        assert_eq!(read("blank"), None);
        assert_eq!(read("bare"), None);
        assert_eq!(read("missing"), None);
        assert_eq!(base_instructions_from_catalog(b"not json", "gpt-6.1-sol"), None);
    }

    #[test]
    fn model_catalog_path_is_read_from_top_level_config_only() {
        let home = std::path::Path::new("/codex-home");
        let read = |config: &str| configured_model_catalog(config, home);
        assert_eq!(
            read("model = \"x\"\nmodel_catalog_json = \"/abs/unified.json\"\n"),
            Some("/abs/unified.json".into())
        );
        assert_eq!(read("model_catalog_json='rel/c.json'"), Some("/codex-home/rel/c.json".into()));
        assert_eq!(read("# model_catalog_json = \"/commented.json\"\n"), None);
        assert_eq!(read("model_catalog_json_extra = \"/other.json\"\n"), None);
        assert_eq!(read("[profiles.a]\nmodel_catalog_json = \"/scoped.json\"\n"), None);
        assert_eq!(read("model_catalog_json = \"\"\n"), None);
        assert_eq!(read(""), None);
    }

    #[test]
    fn request_profile_names_the_instruction_source_and_digest() {
        let base = BaseInstructions {
            text: Arc::from("sol base"),
            source: "model_catalog",
            catalog: Arc::from("/m/unified.json"),
        };
        let profile = codex_request_profile(&json!({"instructions": "sol base"}), Some(&base), "prompt_cache_key");
        assert_eq!(profile["instructions_source"], "model_catalog");
        assert_eq!(profile["instructions_catalog"], "/m/unified.json");
        assert_eq!(profile["instructions_bytes"], 8);
        assert_eq!(profile["instructions_sha256_16"], sha256_prefix("sol base"));
        assert_eq!(profile["instructions_sha256_16"].as_str().unwrap().len(), 16);
        assert_eq!(profile["session_affinity"], "prompt_cache_key");
        let generic = codex_request_profile(&json!({"instructions": "generic line"}), None, "fixed");
        assert!(generic["instructions_catalog"].is_null());
        assert!(generic["instructions_source"].as_str().unwrap().starts_with("generic_"));
    }

    #[test]
    fn workers_share_one_affinity_key_regardless_of_session() {
        for key in [None, Some("turaosv2:direct:full-abc:0123456789abcdef".to_string())] {
            let options = CallOptions { prompt_cache_key: key, ..CallOptions::default() };
            assert_eq!(codex_session_affinity(&options), ("tura-codex-validation".to_string(), "shared"));
        }
    }

    #[tokio::test]
    async fn transport_timing_preserves_completed_stream_and_callback_delivery() {
        let (_, events, _) = completed_item_stream();
        let bytes = events.iter().map(|e| format!("data: {e}\n\n")).collect::<String>().into_bytes();
        let expected = parse_codex_response_chunks(
            futures_util::stream::iter(vec![Ok(bytes.clone())]), None,
        ).await.unwrap();
        let seen = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let observed = seen.clone();
        let sink: ProviderStreamEventSink = std::sync::Arc::new(move |event| {
            if matches!(event, ProviderStreamEvent::ProviderOutputStarted) {
                observed.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            }
        });
        let started = Instant::now() - std::time::Duration::from_millis(20);
        let (actual, timing) = parse_codex_response_chunks_timed(
            futures_util::stream::iter(vec![Ok(bytes)]), Some(sink), started, 10,
        ).await.unwrap();
        assert_eq!(actual, expected);
        assert_eq!(timing.transport, "http_sse");
        assert_eq!(timing.response_headers_ms, 10);
        assert!(timing.first_output_event_ms.unwrap() >= 20);
        assert!(timing.first_output_event_ms.unwrap() <= timing.completed_ms);
        assert_eq!(seen.load(std::sync::atomic::Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn transport_timing_keeps_missing_output_unknown_and_rejects_truncated_stream() {
        let bytes = b"data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r\",\"status\":\"completed\",\"output\":[]}}\n\n".to_vec();
        let (_, timing) = parse_codex_response_chunks_timed(
            futures_util::stream::iter(vec![Ok(bytes)]), None, Instant::now(), 0,
        ).await.unwrap();
        assert_eq!(timing.first_output_event_ms, None);
        let invalid = b"data: {\"type\":\"response.created\"}\n\n".to_vec();
        assert!(parse_codex_response_chunks_timed(
            futures_util::stream::iter(vec![Ok(invalid)]), None, Instant::now(), 0,
        ).await.is_err());
    }

    #[test]
    fn transport_timing_old_metrics_remain_deserializable() {
        let mut old = serde_json::to_value(crate::CallMetrics::default()).unwrap();
        old.as_object_mut().unwrap().remove("transport_timing");
        let restored: crate::CallMetrics = serde_json::from_value(old).unwrap();
        assert!(restored.transport_timing.is_none());
    }

    #[test]
    fn failure_usage_does_not_change_errors_or_retry_policy() {
        let data = json!({"status":"failed", "error":{"code":"billing_error"},
            "usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}});
        let plain = validate_responses_status("codex", &data).unwrap_err();
        let observed = validate_responses_status_with_usage("codex", "requested", Some("req-1"), &data).unwrap_err();
        assert_eq!(plain.to_string(), observed.to_string());
        assert_eq!(plain.is_non_retryable_provider_failure(), observed.is_non_retryable_provider_failure());
        assert_eq!(observed.failed_attempts()[0].request_id.as_deref(), Some("req-1"));
        assert_eq!(observed.failed_attempts()[0].usage.as_ref().unwrap().total_tokens, Some(12));
    }

    #[test]
    fn failed_response_null_incomplete_details_preserves_provider_error() {
        let error = validate_responses_status("codex", &json!({
            "status": "failed", "incomplete_details": null,
            "error": {"code": "server_error", "message": "fixture failure"}
        })).unwrap_err().to_string();
        assert!(error.contains("server_error"), "{error}");
        assert!(error.contains("fixture failure"), "{error}");
    }

    #[test]
    fn failed_response_missing_detail_is_explicit_and_incomplete_reason_is_retained() {
        for data in [json!({"status": "failed"}),
                     json!({"status": "failed", "incomplete_details": null, "error": null})] {
            assert!(validate_responses_status("codex", &data).unwrap_err().to_string().contains("no detail"));
        }
        let error = validate_responses_status("codex", &json!({
            "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "error": null
        })).unwrap_err().to_string();
        assert!(error.contains("max_output_tokens"));
        for status in ["completed", "in_progress", "queued"] {
            assert!(validate_responses_status("codex", &json!({"status": status})).is_ok());
        }
    }

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
