use super::{
    build_chat_payload, build_codex_oauth_payload, build_responses_payload_for_provider,
    normalize_messages_for_provider, process_chat_stream_line_for_test, should_pass_service_tier,
};
use crate::tura_llm::CallOptions;
use serde_json::json;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::{OnceLock, mpsc};
use tokio::sync::Mutex;

async fn codex_endpoint_env_lock() -> tokio::sync::MutexGuard<'static, ()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(())).lock().await
}

#[test]
fn openai_compatible_chat_messages_preserve_native_roles() {
    let messages = vec![
        json!({"role": "system", "content": "Use tools carefully."}),
        json!({"role": "assistant", "content": null}),
        json!({"role": "user", "content": "Inspect files."}),
    ];

    let normalized = normalize_messages_for_provider("minimax", &messages);

    assert_eq!(normalized.len(), 2);
    assert_eq!(normalized[0]["role"], "system");
    assert_eq!(normalized[0]["content"], "Use tools carefully.");
    assert_eq!(normalized[1]["role"], "user");
    assert_eq!(normalized[1]["content"], "Inspect files.");
}

#[test]
fn openai_compatible_chat_messages_keep_assistant_tool_calls() {
    let messages = vec![
        json!({"role": "user", "content": "run pwd"}),
        json!({
            "role": "assistant",
            "content": null,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "command_run", "arguments": "{}"}
            }]
        }),
        json!({"role": "tool", "tool_call_id": "call_1", "content": "/tmp"}),
    ];

    let normalized = normalize_messages_for_provider("minimax", &messages);

    assert_eq!(normalized.len(), 3);
    assert_eq!(normalized[1]["role"], "assistant");
    assert_eq!(normalized[1]["content"], "");
    assert_eq!(normalized[1]["tool_calls"][0]["id"], "call_1");
    assert_eq!(normalized[2]["role"], "tool");
    assert_eq!(normalized[2]["tool_call_id"], "call_1");
    assert_eq!(normalized[2]["content"], "/tmp");
}

#[test]
fn openai_compatible_chat_messages_preserve_tool_call_and_output_pairs() {
    let messages = vec![
        json!({
            "type": "function_call",
            "name": "command_run",
            "call_id": "call_abc",
            "arguments": "{\"commands\":[]}",
            "status": "completed"
        }),
        json!({
            "type": "function_call_output",
            "call_id": "call_abc",
            "output": "Exit code: 0\nOutput:\nTURA_PROBE_OK\n"
        }),
    ];

    let normalized = normalize_messages_for_provider("minimax", &messages);

    assert_eq!(normalized.len(), 2);
    assert_eq!(normalized[0]["role"], "assistant");
    assert_eq!(normalized[0]["tool_calls"][0]["id"], "call_abc");
    assert_eq!(
        normalized[0]["tool_calls"][0]["function"]["name"],
        "command_run"
    );
    assert_eq!(normalized[1]["role"], "tool");
    assert_eq!(normalized[1]["tool_call_id"], "call_abc");
    let content = normalized[1]["content"]
        .as_str()
        .expect("normalized tool content should be a string");
    assert!(content.contains("TURA_PROBE_OK"));
}

#[test]
fn non_minimax_keeps_assistant_empty_content_for_openai_compatibility() {
    let messages = vec![json!({"role": "assistant", "content": null})];

    let normalized = normalize_messages_for_provider("openai", &messages);

    assert_eq!(normalized[0]["role"], "assistant");
    assert_eq!(normalized[0]["content"], "");
}

#[test]
fn service_tier_is_limited_to_openai_gpt_family_models() {
    assert!(should_pass_service_tier("openai", "gpt-5.2"));
    assert!(should_pass_service_tier("openai", "o3"));
    assert!(should_pass_service_tier("openai", "gpt-5.3-codex"));
    assert!(!should_pass_service_tier("openrouter", "openai/gpt-5.2"));
    assert!(!should_pass_service_tier("minimax", "minimax-m2.5"));
}

#[test]
fn chat_stream_counts_reasoning_deltas_as_output_activity() {
    // OpenRouter/DeepSeek-style reasoning field.
    let (event, content, reasoning) = process_chat_stream_line_for_test(
        r#"data: {"choices":[{"delta":{"reasoning":"thinking hard"}}]}"#,
    );
    assert!(event, "reasoning delta must count as output activity");
    assert!(content.is_empty(), "reasoning is not assistant content");
    assert_eq!(reasoning, "thinking hard");

    // Alternate `reasoning_content` field used by some providers.
    let (event2, _, reasoning2) = process_chat_stream_line_for_test(
        r#"data: {"choices":[{"delta":{"reasoning_content":"step 1"}}]}"#,
    );
    assert!(event2);
    assert_eq!(reasoning2, "step 1");

    // Empty reasoning must not be treated as activity.
    let (event3, _, reasoning3) =
        process_chat_stream_line_for_test(r#"data: {"choices":[{"delta":{"reasoning":""}}]}"#);
    assert!(!event3);
    assert!(reasoning3.is_empty());
}

#[test]
fn responses_payload_only_forwards_service_tier_for_openai_family() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        service_tier: Some("priority".to_string()),
        ..CallOptions::default()
    };
    // OpenAI family (codex/chatgpt) accepts service_tier.
    let codex = build_responses_payload_for_provider("codex", "gpt-5.2", &messages, &options);
    assert_eq!(codex["service_tier"], "priority");
    // xAI rejects it with 400 Argument not supported: service_tier.
    let grok = build_responses_payload_for_provider("xai", "grok-4.3", &messages, &options);
    assert!(grok.get("service_tier").is_none());
    // Qwen Responses branch must also omit it.
    let qwen = build_responses_payload_for_provider("qwen", "qwen3.7-max", &messages, &options);
    assert!(qwen.get("service_tier").is_none());
}

#[test]
fn responses_payload_preserves_canonical_media_content() {
    let messages = vec![json!({
        "role": "user",
        "content": [
            { "type": "input_text", "text": "see image" },
            { "type": "input_image", "image_url": "data:image/jpeg;base64,AAA" }
        ]
    })];

    let payload = build_responses_payload_for_provider(
        "chatgpt",
        "gpt-5.2",
        &messages,
        &CallOptions::default(),
    );

    assert_eq!(payload["input"][0]["content"][0]["type"], "input_text");
    assert_eq!(payload["input"][0]["content"][1]["type"], "input_image");
    assert_eq!(
        payload["input"][0]["content"][1]["image_url"],
        "data:image/jpeg;base64,AAA"
    );
}

#[test]
fn provider_payload_passes_reasoning_and_acceleration_for_openai_gpt() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some("high".to_string()),
        service_tier: Some("priority".to_string()),
        ..CallOptions::default()
    };

    let payload = build_chat_payload("openai", "gpt-5.2", &messages, &options);

    assert_eq!(payload["reasoning_effort"], "high");
    assert_eq!(payload["service_tier"], "priority");
}

#[test]
fn provider_payload_maps_highest_reasoning_to_xhigh() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some("highest".to_string()),
        ..CallOptions::default()
    };

    let payload = build_chat_payload("openai", "gpt-5.2", &messages, &options);

    assert_eq!(payload["reasoning_effort"], "xhigh");
}

#[test]
fn provider_payload_keeps_max_reasoning_for_gpt_5_6_family() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some("max".to_string()),
        ..CallOptions::default()
    };

    for model in ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"] {
        let chat_payload = build_chat_payload("openai", model, &messages, &options);
        let codex_payload = build_codex_oauth_payload(model, &messages, &options);

        assert_eq!(chat_payload["reasoning_effort"], "max", "chat {model}");
        assert_eq!(codex_payload["reasoning"]["effort"], "max", "codex {model}");
    }
}

#[test]
fn provider_payload_keeps_max_reasoning_for_supported_gpt_6_models() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some("max".to_string()),
        ..CallOptions::default()
    };
    for model in ["gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"] {
        let chat = build_chat_payload("openai", model, &messages, &options);
        let codex = build_codex_oauth_payload(model, &messages, &options);
        assert_eq!(chat["reasoning_effort"], "max", "chat {model}");
        assert_eq!(codex["reasoning"]["effort"], "max", "codex {model}");
        assert_eq!(codex["include"], json!(["reasoning.encrypted_content"]));
    }
}

#[test]
fn provider_payload_keeps_explicit_ultra_reasoning_for_gpt_6() {
    let options = CallOptions {
        reasoning_effort: Some("ultra".to_string()),
        ..CallOptions::default()
    };
    let messages = vec![json!({"role": "user", "content": "ping"})];
    for model in ["gpt-6-astra", "gpt-6.1-sol"] {
        assert_eq!(build_codex_oauth_payload(model, &messages, &options)["reasoning"]["effort"], "ultra");
    }
}

#[test]
fn provider_payload_maps_max_reasoning_to_xhigh_for_legacy_unsupported_models() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some("max".to_string()),
        ..CallOptions::default()
    };

    let chat_payload = build_chat_payload("openai", "gpt-5.5", &messages, &options);
    let codex_payload = build_codex_oauth_payload("gpt-5.5", &messages, &options);

    assert_eq!(chat_payload["reasoning_effort"], "xhigh");
    assert_eq!(codex_payload["reasoning"]["effort"], "xhigh");
}

#[test]
fn provider_payload_omits_default_reasoning_and_acceleration() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some(" default ".to_string()),
        service_tier: Some("default".to_string()),
        ..CallOptions::default()
    };

    let payload = build_chat_payload("openai", "gpt-5.2", &messages, &options);

    assert!(payload.get("reasoning_effort").is_none());
    assert!(payload.get("service_tier").is_none());
}

#[test]
fn provider_payload_does_not_pass_acceleration_to_non_openai_models() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some("medium".to_string()),
        service_tier: Some("priority".to_string()),
        ..CallOptions::default()
    };

    let payload = build_chat_payload("minimax", "minimax-m2.5", &messages, &options);

    assert_eq!(payload["reasoning_effort"], "medium");
    assert!(payload.get("service_tier").is_none());
}

#[tokio::test]
async fn direct_provider_call_sends_reasoning_and_acceleration() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("local addr");
    let (tx, rx) = mpsc::channel();

    std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let mut buffer = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let read = stream.read(&mut chunk).expect("read request");
            if read == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..read]);
            if let Some(header_end) = find_header_end(&buffer) {
                let headers = String::from_utf8_lossy(&buffer[..header_end]);
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                let body_start = header_end + 4;
                if buffer.len() >= body_start + content_length {
                    let body =
                        String::from_utf8(buffer[body_start..body_start + content_length].to_vec())
                            .expect("utf8 body");
                    tx.send(body).expect("send request body");
                    break;
                }
            }
        }

        let response = concat!(
            "HTTP/1.1 200 OK\r\n",
            "Content-Type: application/json\r\n",
            "Content-Length: 69\r\n",
            "\r\n",
            r#"{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":1}}"#
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
    });

    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some("high".to_string()),
        service_tier: Some("priority".to_string()),
        ..CallOptions::default()
    };

    super::call(
        &format!("http://{addr}"),
        "gpt-5.2",
        "openai",
        "test-key",
        &messages,
        &options,
    )
    .await
    .expect("provider call");

    let body: serde_json::Value =
        serde_json::from_str(&rx.recv().expect("request body")).expect("json body");
    assert_eq!(body["reasoning_effort"], "high");
    assert_eq!(body["service_tier"], "priority");
}

#[tokio::test]
async fn streaming_provider_drains_usage_after_tool_arguments_complete() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("local addr");

    std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let mut buffer = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let read = stream.read(&mut chunk).expect("read request");
            if read == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..read]);
            if let Some(header_end) = find_header_end(&buffer) {
                let headers = String::from_utf8_lossy(&buffer[..header_end]);
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                let body_start = header_end + 4;
                if buffer.len() >= body_start + content_length {
                    break;
                }
            }
        }

        let first = json!({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "grep",
                            "arguments": "{\"pattern\""
                        }
                    }]
                }
            }]
        });
        let second = json!({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {
                            "arguments": ":\"foo\"}"
                        }
                    }]
                }
            }]
        });
        let late_text = json!({
            "choices": [{
                "delta": {
                    "content": "late text after tool call"
                }
            }]
        });
        let usage = json!({
            "choices": [],
            "usage": {
                "prompt_tokens": 3000,
                "completion_tokens": 8,
                "total_tokens": 3008,
                "prompt_tokens_details": {"cached_tokens": 2048}
            }
        });
        let body = format!(
            "data: {first}\n\ndata: {second}\n\ndata: {late_text}\n\ndata: {usage}\n\ndata: [DONE]\n\n"
        );
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
    });

    let messages = vec![json!({"role": "user", "content": "search"})];
    let options = CallOptions {
        stream: Some(true),
        ..CallOptions::default()
    };

    let result = super::call(
        &format!("http://{addr}"),
        "gpt-test",
        "openai",
        "test-key",
        &messages,
        &options,
    )
    .await
    .expect("provider call");

    assert_eq!(result.content["tool_calls"][0]["function"]["name"], "grep");
    assert_eq!(
        result.content["tool_calls"][0]["function"]["arguments"]["pattern"],
        "foo"
    );
    assert!(
        !result
            .content
            .to_string()
            .contains("late text after tool call")
    );
    let metrics = result.metrics.expect("metrics");
    assert_eq!(metrics.usage.input_tokens, Some(3000));
    assert_eq!(metrics.usage.cached_input_tokens, Some(2048));
    assert!(metrics.cache_hit);
}

#[tokio::test]
async fn streaming_provider_reads_usage_and_cached_tokens() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("local addr");
    let (tx, rx) = mpsc::channel();

    std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let mut buffer = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let read = stream.read(&mut chunk).expect("read request");
            if read == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..read]);
            if let Some(header_end) = find_header_end(&buffer) {
                let headers = String::from_utf8_lossy(&buffer[..header_end]);
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                let body_start = header_end + 4;
                if buffer.len() >= body_start + content_length {
                    let body =
                        String::from_utf8(buffer[body_start..body_start + content_length].to_vec())
                            .expect("utf8 body");
                    tx.send(body).expect("send request body");
                    break;
                }
            }
        }

        let content = json!({
            "choices": [{
                "delta": {"content": "ok"}
            }]
        });
        let usage = json!({
            "choices": [],
            "usage": {
                "prompt_tokens": 3000,
                "completion_tokens": 3,
                "total_tokens": 3003,
                "prompt_tokens_details": {"cached_tokens": 2048}
            }
        });
        let body = format!("data: {content}\n\ndata: {usage}\n\ndata: [DONE]\n\n");
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
    });

    let messages = vec![json!({"role": "user", "content": "cache"})];
    let options = CallOptions {
        stream: Some(true),
        stream_options: Some(json!({ "include_usage": true })),
        ..CallOptions::default()
    };

    let result = super::call(
        &format!("http://{addr}"),
        "gpt-test",
        "openai",
        "test-key",
        &messages,
        &options,
    )
    .await
    .expect("provider call");

    let request_body: serde_json::Value =
        serde_json::from_str(&rx.recv().expect("request body")).expect("json body");
    assert_eq!(request_body["stream_options"]["include_usage"], true);
    let metrics = result.metrics.expect("metrics");
    assert_eq!(metrics.usage.input_tokens, Some(3000));
    assert_eq!(metrics.usage.output_tokens, Some(3));
    assert_eq!(metrics.usage.cached_input_tokens, Some(2048));
    assert!(metrics.cache_hit);
}

#[test]
fn qwen_stream_options_request_usage_for_cache_accounting() {
    let payload = build_chat_payload(
        "qwen",
        "qwen3-max-2026-01-23",
        &[json!({"role": "user", "content": "cache"})],
        &CallOptions {
            stream: Some(true),
            stream_options: Some(json!({ "include_usage": true })),
            ..CallOptions::default()
        },
    );

    assert_eq!(payload["stream"], true);
    assert_eq!(payload["stream_options"]["include_usage"], true);
}

#[test]
fn metrics_read_minimax_anthropic_cache_usage_fields() {
    let metrics = crate::metrics::extract_openapi_metrics(
        &json!({
            "usage": {
                "input_tokens": 108,
                "output_tokens": 91,
                "cache_creation_input_tokens": 512,
                "cache_read_input_tokens": 14813
            }
        }),
        None,
    );

    assert_eq!(metrics.usage.input_tokens, Some(108));
    assert_eq!(metrics.usage.output_tokens, Some(91));
    assert_eq!(metrics.usage.cached_input_tokens, Some(14813));
    assert_eq!(metrics.usage.cache_write_tokens, Some(512));
    assert_eq!(metrics.usage.total_tokens, Some(15524));
    assert!(metrics.cache_hit);
}

#[tokio::test]
async fn codex_oauth_call_sends_responses_reasoning_and_acceleration() {
    assert_codex_oauth_request("gpt-5.1-codex", "high").await;
}

#[tokio::test]
async fn codex_oauth_call_sends_gpt_6_1_max_without_downgrade() {
    assert_codex_oauth_request("gpt-6.1-sol", "max").await;
}

async fn assert_codex_oauth_request(model: &str, effort: &str) {
    let _env_guard = codex_endpoint_env_lock().await;
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("local addr");
    let endpoint = format!("http://{addr}/backend-api/codex/responses");
    let previous_endpoint = std::env::var_os("OPENAI_CODEX_ENDPOINT");
    let previous_account_id = std::env::var_os("OPENAI_ACCOUNT_ID");
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::set_var("OPENAI_CODEX_ENDPOINT", &endpoint)
    };
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::set_var("OPENAI_ACCOUNT_ID", "acct-probe")
    };
    let (tx, rx) = mpsc::channel();

    std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let mut buffer = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let read = stream.read(&mut chunk).expect("read request");
            if read == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..read]);
            if let Some(header_end) = find_header_end(&buffer) {
                let headers = String::from_utf8_lossy(&buffer[..header_end]);
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                let body_start = header_end + 4;
                if buffer.len() >= body_start + content_length {
                    let body =
                        String::from_utf8(buffer[body_start..body_start + content_length].to_vec())
                            .expect("utf8 body");
                    tx.send(format!("{headers}\n\n{body}"))
                        .expect("send request");
                    break;
                }
            }
        }

        let body = concat!(
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"ok\"}\n\n",
            "data: {\"type\":\"response.completed\",\"response\":{\"output_text\":\"ok\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1,\"total_tokens\":2}}}\n\n",
            "data: [DONE]\n\n"
        );
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
    });

    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some(effort.to_string()),
        service_tier: Some("priority".to_string()),
        ..CallOptions::default()
    };

    let result =
        super::codex_oauth_call(model, "test-token", &messages, &options, None).await;

    match previous_endpoint {
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        Some(value) => {
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::set_var("OPENAI_CODEX_ENDPOINT", value)
            }
        }
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        None => {
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::remove_var("OPENAI_CODEX_ENDPOINT")
            }
        }
    }
    match previous_account_id {
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        Some(value) => {
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::set_var("OPENAI_ACCOUNT_ID", value)
            }
        }
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        None => {
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::remove_var("OPENAI_ACCOUNT_ID")
            }
        }
    }

    result.expect("codex oauth call");
    let request = rx.recv().expect("request");
    let (headers, body_text) = request.split_once("\n\n").expect("headers and body");
    assert_eq!(header_value(headers, "originator"), Some("codex_cli_rs"));
    assert_eq!(
        header_value(headers, "User-Agent"),
        Some("codex_cli_rs/0.0.0 (Windows 10.0; x86_64)")
    );
    assert_eq!(
        header_value(headers, "ChatGPT-Account-Id"),
        Some("acct-probe")
    );
    let body: serde_json::Value = serde_json::from_str(body_text).expect("json body");
    assert!(body.get("reasoning_effort").is_none());
    assert_eq!(body["reasoning"]["effort"], effort);
    assert_eq!(body["model"], model);
    assert_eq!(body["include"], json!(["reasoning.encrypted_content"]));
    assert_eq!(body["service_tier"], "priority");
    assert_eq!(body["input"][0]["content"][0]["type"], "input_text");
    assert_eq!(body["input"][0]["content"][0]["text"], "ping");
}

#[tokio::test]
async fn codex_oauth_stream_reads_completed_usage_after_tool_call() {
    let _env_guard = codex_endpoint_env_lock().await;
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("local addr");
    let endpoint = format!("http://{addr}/backend-api/codex/responses");
    let previous_endpoint = std::env::var_os("OPENAI_CODEX_ENDPOINT");
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::set_var("OPENAI_CODEX_ENDPOINT", &endpoint)
    };

    std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let mut buffer = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let read = stream.read(&mut chunk).expect("read request");
            if read == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..read]);
            if let Some(header_end) = find_header_end(&buffer) {
                let headers = String::from_utf8_lossy(&buffer[..header_end]);
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                let body_start = header_end + 4;
                if buffer.len() >= body_start + content_length {
                    break;
                }
            }
        }

        let args = r#"{"commands":[{"step":1,"command":"rg","command_line":"rg -n bug ."}]}"#;
        let tool_event = json!({
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "command_run",
                "arguments": args
            }
        });
        let completed = json!({
            "type": "response.completed",
            "response": {
                "output": [{
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "command_run",
                    "arguments": args
                }],
                "usage": {
                    "input_tokens": 3000,
                    "input_tokens_details": {"cached_tokens": 2048},
                    "output_tokens": 20,
                    "total_tokens": 3020
                }
            }
        });
        let body = format!("data: {tool_event}\n\ndata: {completed}\n\ndata: [DONE]\n\n");
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
    });

    let messages = vec![json!({"role": "user", "content": "run command"})];
    let options = CallOptions {
        tools: Some(vec![json!({
            "type": "function",
            "function": {
                "name": "command_run",
                "parameters": {"type": "object"}
            }
        })]),
        ..CallOptions::default()
    };

    let result = super::codex_oauth_call(
        "gpt-5.1-codex-mini",
        "test-token",
        &messages,
        &options,
        None,
    )
    .await;

    match previous_endpoint {
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        Some(value) => {
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::set_var("OPENAI_CODEX_ENDPOINT", value)
            }
        }
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        None => {
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::remove_var("OPENAI_CODEX_ENDPOINT")
            }
        }
    }

    let metrics = result.expect("codex oauth call").metrics.expect("metrics");
    assert_eq!(metrics.usage.cached_input_tokens, Some(2048));
    assert!(metrics.cache_hit);
}

#[test]
fn codex_oauth_payload_omits_default_reasoning_and_acceleration() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        reasoning_effort: Some("default".to_string()),
        service_tier: Some(" default ".to_string()),
        ..CallOptions::default()
    };

    let payload = build_codex_oauth_payload("gpt-5.1-codex", &messages, &options);

    assert!(payload.get("reasoning").is_none());
    assert!(payload.get("reasoning_effort").is_none());
    assert!(payload.get("service_tier").is_none());
}

#[test]
fn codex_oauth_payload_passes_prompt_cache_key_only() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        prompt_cache_key: Some("turaosv2:test:abc".to_string()),
        ..CallOptions::default()
    };

    let payload = build_codex_oauth_payload("gpt-5.1-codex-mini", &messages, &options);

    assert_eq!(payload["prompt_cache_key"], "turaosv2:test:abc");
    assert!(payload.get("prompt_cache_retention").is_none());
}

#[test]
fn gpt_6_codex_payload_keeps_supported_cache_key_without_rewriting_tool_outputs() {
    let messages = vec![
        json!({"role": "system", "content": "stable instructions"}),
        json!({"type": "function_call_output", "call_id": "call_1", "output": "one"}),
        json!({"type": "function_call_output", "call_id": "call_2", "output": "two"}),
        json!({"type": "function_call_output", "call_id": "call_3", "output": "three"}),
    ];
    let options = CallOptions {
        prompt_cache_key: Some("turaosv2:test:gpt6".to_string()),
        ..CallOptions::default()
    };

    let payload = build_codex_oauth_payload("gpt-6-astra", &messages, &options);

    assert_eq!(payload["prompt_cache_key"], "turaosv2:test:gpt6");
    assert!(payload.get("prompt_cache_options").is_none());
    assert_eq!(payload["input"][1]["output"], "one");
    assert_eq!(payload["input"][2]["output"], "two");
    assert_eq!(payload["input"][3]["output"], "three");
}

#[test]
fn gpt_6_responses_payload_keeps_tool_output_shape_before_later_user_message() {
    let messages = vec![
        json!({"role": "system", "content": "stable instructions"}),
        json!({"type": "function_call_output", "call_id": "call_1", "output": "tool result"}),
        json!({"role": "assistant", "content": "progress"}),
        json!({"role": "user", "content": "follow-up"}),
    ];
    let options = CallOptions {
        prompt_cache_key: Some("turaosv2:test:gpt6-followup".to_string()),
        ..CallOptions::default()
    };

    let payload = build_codex_oauth_payload("gpt-6-astra", &messages, &options);

    assert_eq!(payload["input"][1]["output"], "tool result");
    assert!(!payload.to_string().contains("prompt_cache_breakpoint"));
}

#[test]
fn provider_qualified_gpt_6_codex_model_keeps_supported_cache_key() {
    let messages = vec![json!({"role": "user", "content": "ping"})];
    let options = CallOptions {
        prompt_cache_key: Some("turaosv2:test:qualified-gpt6".to_string()),
        ..CallOptions::default()
    };

    let payload = build_codex_oauth_payload("codex/gpt-6-astra", &messages, &options);

    assert_eq!(payload["prompt_cache_key"], "turaosv2:test:qualified-gpt6");
    assert!(payload.get("prompt_cache_options").is_none());
}

#[test]
fn pre_gpt_5_6_responses_payload_keeps_legacy_tool_output_shape() {
    let messages = vec![
        json!({"role": "system", "content": "stable instructions"}),
        json!({"type": "function_call_output", "call_id": "call_1", "output": "one"}),
    ];
    let options = CallOptions {
        prompt_cache_key: Some("turaosv2:test:legacy".to_string()),
        ..CallOptions::default()
    };

    let payload = build_codex_oauth_payload("gpt-5.5", &messages, &options);

    assert_eq!(payload["prompt_cache_key"], "turaosv2:test:legacy");
    assert!(payload.get("prompt_cache_options").is_none());
    assert_eq!(payload["input"][1]["output"], "one");
}

#[test]
fn codex_oauth_payload_keeps_system_messages_in_input() {
    let messages = vec![
        json!({"role": "system", "content": "You are Tura an agent based on gpt-5.1-codex from LLM provider: openai."}),
        json!({"role": "user", "content": "task"}),
        json!({"role": "system", "content": "dynamic runtime state"}),
        json!({"role": "assistant", "content": "progress"}),
    ];

    let payload =
        build_codex_oauth_payload("gpt-5.1-codex-mini", &messages, &CallOptions::default());

    assert_eq!(
        payload["instructions"],
        "Follow the user request and Operation Manual, and answer concisely."
    );
    assert_eq!(payload["input"][0]["role"], "system");
    assert_eq!(
        payload["input"][0]["content"],
        json!([{ "type": "input_text", "text": "You are Tura an agent based on gpt-5.1-codex from LLM provider: openai." }])
    );
    assert_eq!(payload["input"][1]["role"], "user");
    assert_eq!(
        payload["input"][1]["content"],
        json!([{ "type": "input_text", "text": "task" }])
    );
    assert_eq!(payload["input"][2]["role"], "system");
    assert_eq!(
        payload["input"][2]["content"],
        json!([{ "type": "input_text", "text": "dynamic runtime state" }])
    );
    assert_eq!(payload["input"][3]["role"], "assistant");
    assert_eq!(
        payload["input"][3]["content"],
        json!([{ "type": "output_text", "text": "progress" }])
    );
    assert_eq!(payload["tool_choice"], "auto");
}

fn header_value<'a>(headers: &'a str, name: &str) -> Option<&'a str> {
    headers.lines().find_map(|line| {
        let (candidate, value) = line.split_once(':')?;
        candidate.eq_ignore_ascii_case(name).then_some(value.trim())
    })
}

#[test]
fn command_run_streaming_waits_for_complete_json_arguments() {
    let call = json!({
        "type": "function",
        "function": {
            "name": "command_run",
            "arguments": r#"{"commands":[{"step":1,"command":"rg","command_line":"rg -n bug ."},"#
        }
    });

    assert!(super::ready_streaming_tool_call(call).is_none());
}

#[test]
fn command_run_command_streaming_emits_each_complete_command_object() {
    let mut collector = super::CodexCommandRunCommandCollector::default();
    collector.push_event(&json!({
        "type": "response.output_item.added",
        "item": {
            "id": "fc_1",
            "call_id": "call_1",
            "type": "function_call",
            "name": "command_run"
        }
    }));
    let first = collector.push_event(&json!({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": "{\"commands\":[{\"step\":1,\"command_type\":\"shell_command\",\"command_line\":\"echo {one}\"},"
        }));
    assert_eq!(first.len(), 1);
    assert_eq!(command_index_for_test(&first[0]), Some(0));

    let second = collector.push_event(&json!({
        "type": "response.function_call_arguments.delta",
        "item_id": "fc_1",
        "delta": "{\"step\":2,\"command_type\":\"shell_command\",\"command_line\":\"echo two\"}"
    }));
    assert_eq!(second.len(), 1);
    assert_eq!(command_index_for_test(&second[0]), Some(1));

    let done = collector.push_event(&json!({
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "arguments": "{\"commands\":[{\"step\":1,\"command_type\":\"shell_command\",\"command_line\":\"echo {one}\"},{\"step\":2,\"command_type\":\"shell_command\",\"command_line\":\"echo two\"}]}"
        }));
    assert!(done.is_empty());
}

#[test]
fn command_run_command_streaming_inherits_top_level_timeout_before_dispatch() {
    let mut collector = super::CodexCommandRunCommandCollector::default();
    collector.push_event(&json!({
        "type": "response.output_item.added",
        "item": {
            "id": "fc_timeout",
            "call_id": "call_timeout",
            "type": "function_call",
            "name": "command_run"
        }
    }));
    let ready = collector.push_event(&json!({
        "type": "response.function_call_arguments.delta",
        "item_id": "fc_timeout",
        "delta": "{\"timeout_ms\":25000,\"commands\":[{\"step\":1,\"command_type\":\"shell_command\",\"command_line\":\"sleep 16\"},"
    }));

    let command = match &ready[0] {
        crate::tura_llm::ProviderStreamEvent::CommandRunCommandReady { command, .. } => command,
        other => panic!("unexpected provider event: {other:?}"),
    };
    assert_eq!(command["timeout_ms"], 25_000);
}

#[test]
fn command_run_command_streaming_emits_split_python_command_object() {
    let mut collector = super::CodexCommandRunCommandCollector::default();
    collector.push_event(&json!({
        "type": "response.output_item.added",
        "item": {
            "id": "fc_stream_probe",
            "call_id": "call_stream_probe",
            "type": "function_call",
            "name": "command_run",
            "arguments": ""
        }
    }));
    let open = collector.push_event(&json!({
        "type": "response.function_call_arguments.delta",
        "item_id": "fc_stream_probe",
        "delta": "{\"commands\":["
    }));
    assert!(open.is_empty());
    let first_command = json!({
            "step": 1,
            "command_type": "shell_command",
            "command_line": json!({
                "command": "python -c \"from pathlib import Path; Path('streamed-first.txt').write_text('first')\"",
                "timeout_ms": 20000
            }).to_string()
        })
        .to_string()
            + ",";
    let first = collector.push_event(&json!({
        "type": "response.function_call_arguments.delta",
        "item_id": "fc_stream_probe",
        "delta": first_command
    }));
    assert_eq!(first.len(), 1);
    assert_eq!(command_index_for_test(&first[0]), Some(0));
}

fn command_index_for_test(event: &crate::tura_llm::ProviderStreamEvent) -> Option<usize> {
    match event {
        crate::tura_llm::ProviderStreamEvent::CommandRunCommandReady { command_index, .. } => {
            Some(*command_index)
        }
        crate::tura_llm::ProviderStreamEvent::ProviderOutputStarted
        | crate::tura_llm::ProviderStreamEvent::TextDelta { .. } => None,
    }
}

#[test]
fn command_run_streaming_emits_complete_json_arguments() {
    let call = json!({
        "type": "function",
        "function": {
            "name": "command_run",
            "arguments": r#"{"commands":[{"step":1,"command":"npm","command_line":"npm test"}]}"#
        }
    });
    let ready = super::ready_streaming_tool_call(call).expect("complete command_run call");

    assert_eq!(
        ready["function"]["arguments"]["commands"]
            .as_array()
            .expect("ready command_run commands should be an array")
            .len(),
        1
    );
    assert_eq!(
        ready["function"]["arguments"]["commands"][0]["command"],
        "npm"
    );
    assert!(ready["function"]["arguments"].get("commands").is_some());
}

#[test]
fn codex_event_tool_calls_accumulates_argument_deltas_before_emit() {
    let events = vec![
        json!({
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "command_run",
                "arguments": ""
            }
        }),
        json!({
            "type": "response.function_call_arguments.delta",
            "delta": "{\"commands\":[{\"step\":1,"
        }),
        json!({
            "type": "response.function_call_arguments.delta",
            "delta": "\"command\":\"shell_command\",\"command_line\":\"pwd\"}]}"
        }),
    ];
    let calls = super::codex_event_tool_calls(&events);
    let ready = calls
        .into_iter()
        .filter_map(super::ready_streaming_tool_call)
        .collect::<Vec<_>>();

    assert_eq!(ready.len(), 1);
    assert_eq!(
        ready[0]["function"]["arguments"]["commands"][0]["command"],
        "shell_command"
    );
}

#[test]
fn codex_stream_collector_emits_on_arguments_done_before_completed_response() {
    let mut collector = super::CodexToolCallStreamCollector::default();
    let added = json!({
        "type": "response.output_item.added",
        "item": {
            "type": "function_call",
            "id": "fc_early",
            "call_id": "call_early",
            "name": "command_run",
            "arguments": ""
        }
    });
    let delta = json!({
        "type": "response.function_call_arguments.delta",
        "item_id": "call_early",
        "delta": "{\"commands\":[{\"command_type\":\"shell_command\","
    });
    let done = json!({
        "type": "response.function_call_arguments.done",
        "item_id": "call_early",
        "arguments": "{\"commands\":[{\"command_type\":\"shell_command\",\"command_line\":\"pwd\"}]}"
    });

    assert!(collector.push_event(&added).is_empty());
    assert!(collector.push_event(&delta).is_empty());
    let ready = collector.push_event(&done);

    assert_eq!(ready.len(), 1);
    assert_eq!(ready[0]["function"]["name"], "command_run");
    assert_eq!(
        ready[0]["function"]["arguments"]["commands"][0]["command_type"],
        "shell_command"
    );
}

#[test]
fn codex_stream_collector_does_not_emit_incomplete_arguments() {
    let mut collector = super::CodexToolCallStreamCollector::default();
    let added = json!({
        "type": "response.output_item.added",
        "item": {
            "type": "function_call",
            "id": "fc_incomplete",
            "call_id": "call_incomplete",
            "name": "command_run",
            "arguments": ""
        }
    });
    let delta = json!({
        "type": "response.function_call_arguments.delta",
        "item_id": "call_incomplete",
        "delta": "{\"commands\":["
    });

    assert!(collector.push_event(&added).is_empty());
    assert!(collector.push_event(&delta).is_empty());
    assert!(collector.finish().is_empty());
}

#[test]
fn codex_responses_stream_tool_call_does_not_pollute_output_text() {
    let events = vec![
        json!({
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_real",
                "call_id": "call_real",
                "name": "command_run",
                "arguments": ""
            }
        }),
        json!({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_real",
            "delta": "{\"commands\":[{\"command_type\":\"shell_command\","
        }),
        json!({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_real",
            "delta": "\"command_line\":\"Get-Content -Raw src/app.txt\"}]}"
        }),
        json!({
            "type": "response.function_call_arguments.done",
            "item_id": "fc_real",
            "arguments": "{\"commands\":[{\"command_type\":\"shell_command\",\"command_line\":\"Get-Content -Raw src/app.txt\"}]}"
        }),
        json!({
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_real",
                "call_id": "call_real",
                "name": "command_run",
                "status": "completed",
                "arguments": "{\"commands\":[{\"command_type\":\"shell_command\",\"command_line\":\"Get-Content -Raw src/app.txt\"}]}"
            }
        }),
    ];

    let mut output_text = String::new();
    for event in &events {
        super::append_codex_stream_text(event, &mut output_text);
    }
    assert!(output_text.is_empty());

    let normalized = super::normalize_codex_response_content(&json!({
        "events": events,
        "output_text": output_text,
    }));
    let tool_calls = normalized["tool_calls"]
        .as_array()
        .expect("Responses function_call events should normalize to tool_calls");

    assert_eq!(tool_calls.len(), 1);
    assert_eq!(tool_calls[0]["id"], "call_real");
    assert_eq!(tool_calls[0]["function"]["name"], "command_run");
    assert_eq!(
        tool_calls[0]["function"]["arguments"]["commands"][0]["command_type"],
        "shell_command"
    );
    assert!(normalized.get("text").is_none());
}

#[test]
fn codex_responses_stream_preserves_tool_call_after_completed_message_item() {
    let events = vec![
        json!({
            "type": "response.output_item.added",
            "item": {
                "type": "message",
                "id": "msg_1"
            }
        }),
        json!({
            "type": "response.output_text.delta",
            "delta": "I will inspect the files first."
        }),
        json!({
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "id": "msg_1",
                "content": [{
                    "type": "output_text",
                    "text": "I will inspect the files first."
                }]
            }
        }),
        json!({
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_late",
                "call_id": "call_late",
                "name": "command_run",
                "arguments": ""
            }
        }),
        json!({
            "type": "response.function_call_arguments.done",
            "item_id": "fc_late",
            "arguments": "{\"commands\":[{\"command_type\":\"shell_command\",\"command_line\":\"pwd\"}]}"
        }),
        json!({
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_late",
                "call_id": "call_late",
                "name": "command_run",
                "status": "completed",
                "arguments": "{\"commands\":[{\"command_type\":\"shell_command\",\"command_line\":\"pwd\"}]}"
            }
        }),
    ];

    let normalized = super::normalize_codex_response_content(&json!({
        "events": events,
        "output_text": "I will inspect the files first."
    }));

    assert_eq!(normalized["text"], "I will inspect the files first.");
    assert_eq!(normalized["tool_calls"][0]["id"], "call_late");
    assert_eq!(
        normalized["tool_calls"][0]["function"]["arguments"]["commands"][0]["command_line"],
        "pwd"
    );
}

#[test]
fn codex_event_tool_calls_does_not_emit_incomplete_command_run_arguments() {
    let events = vec![
        json!({
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "command_run",
                "arguments": ""
            }
        }),
        json!({
            "type": "response.function_call_arguments.delta",
            "delta": "{\"commands\":["
        }),
    ];
    let ready = super::codex_event_tool_calls(&events)
        .into_iter()
        .filter_map(super::ready_streaming_tool_call)
        .collect::<Vec<_>>();

    assert!(ready.is_empty());
}

#[test]
fn codex_event_tool_calls_prefers_done_arguments_over_added_empty_arguments() {
    let events = vec![
        json!({
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "id": "fc_1",
                "name": "command_run",
                "arguments": ""
            }
        }),
        json!({
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "arguments": "{\"commands\":[{\"step\":1,\"command\":\"echo ok\"}]}"
        }),
        json!({
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "id": "fc_1",
                "name": "command_run",
                "status": "completed",
                "arguments": "{\"commands\":[{\"step\":1,\"command\":\"echo ok\"}]}"
            }
        }),
    ];
    let ready = super::complete_codex_tool_calls(&json!({ "events": events }));

    assert_eq!(ready.len(), 1);
    assert_eq!(
        ready[0]["function"]["arguments"]["commands"][0]["command"],
        "echo ok"
    );
}

#[test]
fn streaming_tool_call_buffer_waits_for_complete_json_arguments() {
    let mut buffer = super::StreamingToolCall {
        id: Some("call_1".to_string()),
        name: Some("command_run".to_string()),
        arguments: r#"{"commands":[{"step":1,"command":"rg","command_line":"rg -n bug ."},"#
            .to_string(),
        emitted: false,
    };
    let mut calls = Vec::new();

    assert!(!super::emit_completed_tool_call(&mut buffer, &mut calls));
    assert!(calls.is_empty());
}

#[test]
fn streaming_tool_call_buffer_emits_complete_json_arguments() {
    let mut buffer = super::StreamingToolCall {
        id: Some("call_1".to_string()),
        name: Some("command_run".to_string()),
        arguments: r#"{"commands":[{"step":1,"command":"npm","command_line":"npm test"}]}"#
            .to_string(),
        emitted: false,
    };
    let mut calls = Vec::new();

    assert!(super::emit_completed_tool_call(&mut buffer, &mut calls));
    assert_eq!(calls.len(), 1);
    assert_eq!(
        calls[0]["function"]["arguments"]["commands"][0]["command"],
        "npm"
    );
}

#[test]
fn minimax_xml_streaming_tool_call_supports_complete_command_run() {
    let text = r#"<minimax:tool_call><invoke name="command_run"><parameter name="commands">[{"step":1,"command":"npm","command_line":"npm test"}]</parameter></invoke></minimax:tool_call>"#;
    let (name, arguments) = super::last_complete_minimax_invoke(text).expect("xml tool call");

    assert_eq!(name, "command_run");
    assert_eq!(arguments["commands"][0]["command"], "npm");
}

// Loopback-only HTTP fixtures exercise actual reqwest errors without a model or
// upstream service. Consume the full request before closing a truncated body.
fn responses_transport_fixture(
    status: u16,
    body: &str,
    missing_bytes: usize,
) -> (String, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind offline fixture");
    let addr = listener.local_addr().expect("fixture addr");
    let response = format!(
        "HTTP/1.1 {status} Fixture\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nSet-Cookie: COOKIE_SENTINEL\r\nConnection: close\r\n\r\n{body}",
        body.len() + missing_bytes,
    );
    let join = std::thread::spawn(move || {
        use std::io::BufRead;

        let (mut stream, _) = listener.accept().expect("fixture request");
        stream
            .set_read_timeout(Some(std::time::Duration::from_secs(5)))
            .expect("fixture read bound");
        let mut reader = std::io::BufReader::new(&mut stream);
        let mut content_length = 0;
        loop {
            let mut line = String::new();
            assert!(reader.read_line(&mut line).expect("request header") > 0);
            if line == "\r\n" {
                break;
            }
            if let Some((name, value)) = line.split_once(':')
                && name.eq_ignore_ascii_case("content-length")
            {
                content_length = value.trim().parse::<usize>().expect("request length");
            }
        }
        let mut request_body = vec![0; content_length];
        reader.read_exact(&mut request_body).expect("request body");
        drop(reader);
        stream.write_all(response.as_bytes()).expect("fixture body");
    });
    (format!("http://{addr}/URL_SENTINEL"), join)
}

fn assert_safe_transport_message(error: crate::tura_llm::TuraError) -> String {
    let crate::tura_llm::TuraError::Network { message } = error else {
        panic!("expected transport failure");
    };
    assert!(message.len() <= 128);
    assert!(message.contains("cause="));
    for sensitive in [
        "URL_SENTINEL",
        "PROMPT_SENTINEL",
        "REASONING_SENTINEL",
        "AUTH_SENTINEL",
        "COOKIE_SENTINEL",
        "127.0.0.1",
        "http://",
    ] {
        assert!(!message.contains(sensitive));
    }
    message
}

#[tokio::test]
async fn response_body_decode_failure_keeps_safe_phase_category_and_cause() {
    let (endpoint, join) = responses_transport_fixture(
        200,
        r#"{"prompt":"PROMPT_SENTINEL","reasoning":"REASONING_SENTINEL","auth":"AUTH_SENTINEL","cookie":"COOKIE_SENTINEL""#,
        0,
    );
    let response = crate::streaming::send_provider_request_first_response(
        reqwest::Client::builder()
            .no_proxy()
            .build()
            .expect("fixture client")
            .get(&endpoint)
            .bearer_auth("AUTH_SENTINEL")
            .header("cookie", "COOKIE_SENTINEL"),
    )
    .await
    .expect("fixture response");
    let error = crate::streaming::read_provider_response_body(response.json::<serde_json::Value>())
        .await
        .expect_err("incomplete JSON should remain a decode failure");
    join.join().expect("fixture server");
    assert_eq!(
        assert_safe_transport_message(error),
        "provider transport failure: phase=response-body category=decode cause=json-eof"
    );
}

#[tokio::test]
async fn responses_sse_preserves_unicode_at_every_byte_split() {
    let text = "let text = \"\u{e9}\u{4e2d}\u{1f980} e\u{301}\u{fffd}\";\n";
    let delta = json!({"type": "response.output_text.delta", "delta": text});
    let completed = json!({
        "type": "response.completed",
        "response": {"status": "completed", "output": []}
    });
    let body = format!("data: {delta}\n\ndata: {completed}\n\ndata: [DONE]\n\n");
    let bytes = body.as_bytes();
    for split in 0..=bytes.len() {
        let (tx, rx) = mpsc::channel();
        let sink: crate::tura_llm::ProviderStreamEventSink = std::sync::Arc::new(move |event| {
            tx.send(event).expect("stream event receiver");
        });
        let chunks = futures_util::stream::iter([
            Ok::<_, reqwest::Error>(&bytes[..split]),
            Ok(&bytes[split..]),
        ]);
        let root = super::response::parse_codex_response_chunks(chunks, Some(sink))
            .await
            .expect("valid split UTF-8 stream");
        assert_eq!(root["output_text"], text, "split {split}");
        assert_eq!(root["events"], json!([delta, completed]), "split {split}");
        let streamed: String = rx
            .try_iter()
            .filter_map(|event| match event {
                crate::tura_llm::ProviderStreamEvent::TextDelta { text } => Some(text),
                _ => None,
            })
            .collect();
        assert_eq!(streamed, text, "split {split}");
    }
}

#[tokio::test]
async fn responses_sse_bytewise_command_arguments_remain_exact_and_stream_early() {
    let command = json!({
        "step": 1,
        "command_type": "shell_command",
        "command_line": "printf '\u{e9} \u{4e2d} \u{1f680}\\n'"
    });
    let arguments = json!({"commands": [command]}).to_string();
    let split = arguments.find('\u{4e2d}').expect("Unicode argument");
    let added = json!({
        "type": "response.output_item.added",
        "item": {"id": "fc_unicode", "call_id": "call_unicode",
            "type": "function_call", "name": "command_run", "arguments": ""}
    });
    let first = json!({"type": "response.function_call_arguments.delta",
        "item_id": "fc_unicode", "delta": &arguments[..split]});
    let second = json!({"type": "response.function_call_arguments.delta",
        "item_id": "fc_unicode", "delta": &arguments[split..]});
    let prefix = format!("data: {added}\n\ndata: {first}\n\ndata: {second}\n\n");
    let done = json!({"type": "response.function_call_arguments.done",
        "item_id": "fc_unicode", "arguments": arguments});
    let item = json!({"id": "fc_unicode", "call_id": "call_unicode",
        "type": "function_call", "name": "command_run", "arguments": arguments});
    let item_done = json!({"type": "response.output_item.done", "item": item});
    let completed = json!({"type": "response.completed",
        "response": {"status": "completed", "output": [item]}});
    let body = format!("{prefix}data: {done}\n\ndata: {item_done}\n\ndata: {completed}");
    let (tx, rx) = mpsc::channel();
    let sink: crate::tura_llm::ProviderStreamEventSink = std::sync::Arc::new(move |event| {
        tx.send(event).expect("stream event receiver");
    });
    let chunks = futures_util::stream::iter(
        body.as_bytes().chunks(1).enumerate().map(|(index, chunk)| {
            if index == prefix.len() {
                // Check before arguments.done or response.completed is even read.
                let ready: Vec<_> = rx
                    .try_iter()
                    .filter_map(|event| match event {
                        crate::tura_llm::ProviderStreamEvent::CommandRunCommandReady {
                            command_index,
                            command,
                            ..
                        } => Some((command_index, command)),
                        _ => None,
                    })
                    .collect();
                assert_eq!(ready.len(), 1);
                assert_eq!(ready[0].0, 0);
                assert_eq!(ready[0].1["command_line"], command["command_line"]);
            }
            Ok::<_, reqwest::Error>(chunk)
        }),
    );
    let root = super::response::parse_codex_response_chunks(chunks, Some(sink))
        .await
        .expect("bytewise command stream");
    assert_eq!(root["output"][0]["arguments"], arguments);
    let calls = super::response::complete_codex_tool_calls(&root);
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0]["function"]["arguments"]["commands"][0], command);
    assert!(
        rx.try_iter()
            .all(|event| command_index_for_test(&event).is_none())
    );
}

#[tokio::test]
async fn responses_sse_rejects_invalid_and_partial_utf8_even_after_completion() {
    for line in [
        b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"PROMPT_SENTINEL \xff\"}\n".as_slice(),
        b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"AUTH_SENTINEL \xff\"}".as_slice(),
        b": REASONING_SENTINEL \xc0\xaf\r\n".as_slice(),
        b": COOKIE_SENTINEL \xed\xa0\x80\n".as_slice(),
        b": PROMPT_SENTINEL AUTH_SENTINEL \xf0\x9f".as_slice(),
        b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"COOKIE_SENTINEL \xe2\x82".as_slice(),
    ] {
        let mut body = b"data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"output\":[]}}\n\n".to_vec();
        body.extend_from_slice(line);
        let chunks = futures_util::stream::iter(body.chunks(1).map(Ok::<_, reqwest::Error>));
        let error = super::response::parse_codex_response_chunks(chunks, None)
            .await
            .expect_err("invalid UTF-8 must not become replacement characters or success");
        assert_eq!(
            assert_safe_transport_message(error),
            "provider stream failure: phase=responses-sse category=decode cause=invalid-utf8"
        );
    }
}

#[tokio::test]
async fn responses_sse_rejects_clean_eof_without_valid_completion() {
    let delta = json!({"type": "response.output_text.delta",
        "delta": "PROMPT_SENTINEL REASONING_SENTINEL AUTH_SENTINEL COOKIE_SENTINEL"});
    let completed = json!({"type": "response.completed",
        "response": {"status": "completed", "output": []}});
    let mut bodies = vec![String::new(), format!("data: {delta}")];
    for event in [
        json!({"type": "response.created", "response": {"status": "in_progress"}}),
        json!({"type": "response.in_progress", "response": {"status": "queued"}}),
        json!({"type": "response.created", "response": {"status": "completed"}}),
        json!({"type": "response.completed"}),
        json!({"type": "response.completed", "response": null}),
        json!({"type": "response.completed", "response": "AUTH_SENTINEL"}),
        json!({"type": "response.completed", "response": {"status": null}}),
        json!({"type": "response.completed", "response": {"status": "in_progress"}}),
        json!({"type": "response.completed", "response": {
            "status": "completed", "error": {"message": "AUTH_SENTINEL"}}}),
        json!({"type": "response.failed", "response": {}}),
        json!({"type": "response.incomplete", "response": {}}),
        json!({"type": "error", "message": "AUTH_SENTINEL"}),
    ] {
        bodies.push(format!("data: {delta}\n\ndata: {event}"));
    }
    bodies.push(format!(
        "data: {completed}\n\ndata: {{\"type\":\"error\",\"message\":\"AUTH_SENTINEL\"}}"
    ));
    for body in bodies {
        for suffix in ["", "\n\ndata: [DONE]\n\n"] {
            let body = format!("{body}{suffix}");
            let chunks = futures_util::stream::iter([Ok::<_, reqwest::Error>(body.as_bytes())]);
            let error = super::response::parse_codex_response_chunks(chunks, None)
                .await
                .expect_err("clean EOF is not proof of completion");
            assert_eq!(
                assert_safe_transport_message(error),
                "provider stream failure: phase=responses-sse category=protocol cause=missing-completed"
            );
        }
    }
}

#[tokio::test]
async fn responses_sse_accepts_completed_crlf_and_unterminated_lines_with_optional_status() {
    let text = "\u{e9}\u{4e2d}\u{1f680}";
    let delta = json!({"type": "response.output_text.delta", "delta": text});
    for response in [
        json!({"output": []}),
        json!({"status": "completed", "output": []}),
    ] {
        let completed = json!({"type": "response.completed", "response": response});
        for ending in ["", "\r", "\n", "\r\n", "\r\n\r\ndata: [DONE]"] {
            let body = format!(
                ": heartbeat\r\n\r\ndata: {delta}\r\n\r\ndata: {completed}{ending}"
            );
            let chunks = futures_util::stream::iter(
                body.as_bytes().chunks(1).map(Ok::<_, reqwest::Error>),
            );
            let root = super::response::parse_codex_response_chunks(chunks, None)
                .await
                .expect("completed stream with compatible line endings and status");
            assert_eq!(root["output_text"], text);
            assert_eq!(root["events"], json!([delta, completed]));
        }
    }
}

#[tokio::test]
async fn responses_sse_keeps_provider_failure_snapshot_for_status_validation() {
    for (event_type, status) in [
        ("response.failed", "failed"),
        ("response.incomplete", "incomplete"),
        ("error", "failed"),
    ] {
        let response = json!({"status": status, "output": [],
            "error": {"code": "fixture_failure"},
            "incomplete_details": {"reason": "fixture_incomplete"}});
        let event = json!({"type": event_type, "response": response});
        let body = format!("data: {event}\n\n");
        let chunks = futures_util::stream::iter([Ok::<_, reqwest::Error>(body.as_bytes())]);
        let root = super::response::parse_codex_response_chunks(chunks, None)
            .await
            .expect("retain provider failure for existing caller status validation");
        assert_eq!(root["status"], status);
        assert_eq!(root["error"], response["error"]);
        assert_eq!(root["incomplete_details"], response["incomplete_details"]);
        assert_eq!(root["events"], json!([event]));
    }
}

#[tokio::test]
async fn responses_sse_decode_failure_keeps_safe_phase_category_and_cause() {
    let (endpoint, join) = responses_transport_fixture(200, "{", 0);
    let response = reqwest::Client::builder()
        .no_proxy()
        .build()
        .expect("fixture client")
        .get(&endpoint)
        .send()
        .await
        .expect("fixture response");
    // A real reqwest decode error, injected into an otherwise valid SSE stream;
    // this is a category regression, not a claim about the historical cause.
    let decode_error = response
        .json::<serde_json::Value>()
        .await
        .expect_err("incomplete JSON");
    join.join().expect("fixture server");
    assert!(decode_error.is_decode());
    let chunks = futures_util::stream::iter([
        Ok::<_, reqwest::Error>(
            b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"PROMPT_SENTINEL REASONING_SENTINEL AUTH_SENTINEL COOKIE_SENTINEL\"}\n\n"
                .as_slice(),
        ),
        Err(decode_error),
    ]);
    let error = super::response::parse_codex_response_chunks(chunks, None)
        .await
        .expect_err("SSE decode failure must not become partial success");
    assert_eq!(
        assert_safe_transport_message(error),
        "provider transport failure: phase=responses-sse category=decode cause=json-eof"
    );
}

#[tokio::test]
async fn responses_truncated_stream_keeps_safe_phase_and_transport_category() {
    let body = concat!(
        "data: {\"type\":\"response.output_text.delta\",\"delta\":\"PROMPT_SENTINEL REASONING_SENTINEL AUTH_SENTINEL COOKIE_SENTINEL\"}\n\n",
        "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"output\":[]}}\n\n",
        "data: [DONE]\n\n",
    );
    let (endpoint, join) = responses_transport_fixture(200, body, 1);
    let result = super::response::responses_api_key_call(
        "openai",
        &endpoint,
        "offline-fixture",
        "AUTH_SENTINEL",
        &[json!({"role": "user", "content": "PROMPT_SENTINEL"})],
        &CallOptions::default(),
        None,
    )
    .await;
    join.join().expect("fixture server");
    let message = assert_safe_transport_message(result.err().expect("truncated HTTP body"));
    // reqwest can expose a wire-body error directly or wrap it as a decode
    // error. Neither representation establishes the historical upstream cause.
    assert!(
        message.starts_with("provider transport failure: phase=responses-sse category=body ")
            || message.starts_with("provider transport failure: phase=responses-sse category=decode ")
    );
}

#[tokio::test]
async fn responses_valid_sse_and_existing_error_variants_are_preserved() {
    let body = concat!(
        ": heartbeat\r\n\r\n",
        "data: {\"type\":\"response.output_text.delta\",\"delta\":\"ok\"}\r\n\r\n",
        "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"output\":[],\"output_text\":\"ok\"}}\r\n\r\n",
        "data: [DONE]",
    );
    let (endpoint, join) = responses_transport_fixture(200, body, 0);
    let result = super::response::responses_api_key_call(
        "openai",
        &endpoint,
        "offline-fixture",
        "fixture-key",
        &[],
        &CallOptions::default(),
        None,
    )
    .await;
    join.join().expect("fixture server");
    let content = result.expect("valid SSE").content;
    assert_eq!(content, json!("ok"));

    let (endpoint, join) = responses_transport_fixture(200, "data: {", 0);
    let result = super::response::responses_api_key_call(
        "openai",
        &endpoint,
        "offline-fixture",
        "fixture-key",
        &[],
        &CallOptions::default(),
        None,
    )
    .await;
    join.join().expect("fixture server");
    assert!(matches!(result, Err(crate::tura_llm::TuraError::Json(_))));

    let (endpoint, join) = responses_transport_fixture(401, "fixture auth rejection", 0);
    let result = super::response::responses_api_key_call(
        "openai",
        &endpoint,
        "offline-fixture",
        "fixture-key",
        &[],
        &CallOptions::default(),
        None,
    )
    .await;
    join.join().expect("fixture server");
    assert!(matches!(
        result,
        Err(crate::tura_llm::TuraError::HttpStatus { status: 401, body })
            if body == "fixture auth rejection"
    ));
}

fn find_header_end(buffer: &[u8]) -> Option<usize> {
    buffer.windows(4).position(|window| window == b"\r\n\r\n")
}
