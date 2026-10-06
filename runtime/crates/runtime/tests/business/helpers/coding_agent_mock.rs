use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;

use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader as TokioBufReader};
use tokio::net::TcpListener as TokioTcpListener;

pub(crate) const ROUTES: &[&str] = &[
    "thinking",
    "fast",
    "codex/gpt-5.5",
    "codex/gpt-5.6",
    "codex/gpt-5.6-sol",
    "codex/gpt-5.6-terra",
    "codex/gpt-5.6-luna",
    "embedding_high",
    "embedding_low",
];

pub(crate) static ENV_LOCK: Mutex<()> = Mutex::new(());
pub(crate) static MOCK_ROUTER_ADDR: OnceLock<String> = OnceLock::new();
pub(crate) static MOCK_ROUTER_INIT: Mutex<()> = Mutex::new(());
pub(crate) const MOCK_COMMAND_TIMEOUT_MS: u64 = 3_000;
pub(crate) const MOCK_PROVIDER_TIMEOUT_MS: &str = "30000";
pub(crate) const MOCK_PROVIDER_STREAM_TIMEOUT_MS: &str = "1000";
pub(crate) const MOCK_MULTI_COMMAND_STREAM_TIMEOUT_MS: &str = "10000";
pub(crate) const MOCK_OPENAI_TOKEN_EXPIRES: &str = "4102444800000";

// Synthetic fixture data to force compaction, not a usage/cost measurement.
pub(crate) const MOCK_COMPACTION_INPUT_TOKENS: u64 = 10_000_000;
pub(crate) const MOCK_COMPACTION_REPLY: &str = "Offline no-tool compaction fixture completed.";

pub(crate) fn mock_command_run_router_addr() -> String {
    if let Some(addr) = MOCK_ROUTER_ADDR.get() {
        return addr.clone();
    }
    let _guard = MOCK_ROUTER_INIT
        .lock()
        .expect("mock command_run router init lock");
    if let Some(addr) = MOCK_ROUTER_ADDR.get() {
        return addr.clone();
    }

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind mock command_run router");
    listener
        .set_nonblocking(true)
        .expect("mock command_run router nonblocking");
    let addr = listener
        .local_addr()
        .expect("mock command_run router addr")
        .to_string();
    thread::spawn(move || {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("mock command_run router runtime");
        runtime.block_on(async move {
            let listener = TokioTcpListener::from_std(listener).expect("tokio listener");
            while let Ok((stream, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let (read, mut write) = stream.into_split();
                    let mut reader = TokioBufReader::new(read);
                    let mut line = String::new();
                    if reader.read_line(&mut line).await.unwrap_or(0) == 0 {
                        return;
                    }
                    let response = mock_command_run_router_response(&line).await;
                    let _ = write.write_all(format!("{response}\n").as_bytes()).await;
                    let _ = write.flush().await;
                });
            }
        });
    });
    MOCK_ROUTER_ADDR
        .set(addr.clone())
        .expect("mock command_run router addr set once");
    addr
}

pub(crate) async fn mock_command_run_router_response(raw: &str) -> Value {
    let request: Value = match serde_json::from_str(raw.trim()) {
        Ok(request) => request,
        Err(error) => {
            return json!({
                "request_id": "invalid",
                "ok": false,
                "error": format!("invalid request: {error}")
            });
        }
    };
    let request_id = request
        .get("request_id")
        .and_then(Value::as_str)
        .unwrap_or("missing")
        .to_string();
    if request.get("method").and_then(Value::as_str) != Some("execution.command_run") {
        return json!({
            "request_id": request_id,
            "ok": false,
            "error": "unsupported mock router method"
        });
    }
    let payload = &request["payload"];
    let Some(session_directory) = payload.get("session_directory").and_then(Value::as_str) else {
        return json!({
            "request_id": request_id,
            "ok": false,
            "error": "session_directory missing"
        });
    };
    let mut output = code_tools::command_run::execute_async_value(
        payload
            .get("arguments")
            .cloned()
            .unwrap_or_else(|| json!({})),
        PathBuf::from(session_directory),
    )
    .await;
    // Fault injection is restricted to this fixture's verification commands.
    // Real command execution still happens; no production router is changed.
    if let Some(commands) = payload.pointer("/arguments/commands").and_then(Value::as_array) {
        for (index, command) in commands.iter().enumerate() {
            let line = command["command_line"].as_str().unwrap_or_default();
            if let Some(result) = output.get_mut("results").and_then(Value::as_array_mut)
                .and_then(|results| results.get_mut(index)).and_then(Value::as_object_mut)
            {
                if line.contains("NOKIY_FIXTURE_UNKNOWN_OUTCOME") {
                    if let Some(output) = result.get_mut("output").and_then(Value::as_object_mut) {
                        output.insert("outcome".to_string(), json!("unknown"));
                    }
                } else if line.contains("NOKIY_FIXTURE_UNRESOLVED_EFFECT") {
                    if let Some(output) = result.get_mut("output").and_then(Value::as_object_mut) {
                        let receipt = output.entry("terminal_receipt".to_string())
                            .or_insert_with(|| json!({}));
                        receipt["reconcile_required"] = json!(true);
                    }
                }
            }
        }
    }
    json!({
        "request_id": request_id,
        "ok": true,
        "payload": {
            "status": "finished",
            "owner": "router",
            "result": output
        }
    })
}

pub(crate) struct MockProvider {
    pub(crate) addr: SocketAddr,
    pub(crate) requests: Arc<Mutex<Vec<Value>>>,
    pub(crate) first_command_observed_before_response_finished: Arc<AtomicBool>,
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum MockMode {
    CommandRun,
    CodexApplyPatchOnly,
    CodexStreamingProbe,
    CodexStreamingSingleTaskStatusMissingFinalToolCall,
    RateLimit,
    TaskStatusDoingWithVisibleReply,
    TaskStatusOnlyThenFinal,
    TaskStatusDoneWithShortVisibleReply,
    TaskStatusDoneWithLongVisibleReply,
    NoToolVisibleReplyWithSyntheticCompactionUsage,
    TerminalEvidence(TerminalEvidenceScenario),
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum TerminalEvidenceScenario {
    Done,
    Question,
    FailedCommand,
    UnknownOutcome,
    UnresolvedEffect,
    PendingCompact,
    AutoCompact,
    ForeignMarker,
    MissingTerminalStatus,
    UnsafeTerminalBatch,
    Ordered(OrderedEvidenceScenario),
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum OrderedEvidenceScenario {
    SeparateDone,
    Done,
    FailedCheck,
    TimedOutCheck,
    UnknownOutcome,
    UnresolvedEffect,
    SameStep,
    DoneBeforeCheck,
    ReversedSteps,
}

impl MockProvider {
    pub(crate) fn start_terminal_evidence(scenario: TerminalEvidenceScenario) -> Self {
        Self::start_with_mode(MockMode::TerminalEvidence(scenario), None)
    }

    pub(crate) fn start_command_run() -> Self {
        Self::start_with_mode(MockMode::CommandRun, None)
    }

    pub(crate) fn start_codex_apply_patch_only() -> Self {
        Self::start_with_mode(MockMode::CodexApplyPatchOnly, None)
    }

    pub(crate) fn start_codex_streaming_probe(workspace: PathBuf) -> Self {
        Self::start_with_mode(MockMode::CodexStreamingProbe, Some(workspace))
    }

    pub(crate) fn start_codex_streaming_single_task_status_missing_final_tool_call() -> Self {
        Self::start_with_mode(
            MockMode::CodexStreamingSingleTaskStatusMissingFinalToolCall,
            None,
        )
    }

    pub(crate) fn start_rate_limit() -> Self {
        Self::start_with_mode(MockMode::RateLimit, None)
    }

    pub(crate) fn start_task_status_doing_with_visible_reply() -> Self {
        Self::start_with_mode(MockMode::TaskStatusDoingWithVisibleReply, None)
    }

    pub(crate) fn start_task_status_only_then_final() -> Self {
        Self::start_with_mode(MockMode::TaskStatusOnlyThenFinal, None)
    }

    pub(crate) fn start_task_status_done_with_short_visible_reply() -> Self {
        Self::start_with_mode(MockMode::TaskStatusDoneWithShortVisibleReply, None)
    }

    pub(crate) fn start_task_status_done_with_long_visible_reply() -> Self {
        Self::start_with_mode(MockMode::TaskStatusDoneWithLongVisibleReply, None)
    }

    pub(crate) fn start_no_tool_visible_reply_with_synthetic_compaction_usage() -> Self {
        Self::start_with_mode(MockMode::NoToolVisibleReplyWithSyntheticCompactionUsage, None)
    }

    fn start_with_mode(mode: MockMode, workspace: Option<PathBuf>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("mock provider should bind");
        let addr = listener.local_addr().expect("mock provider address");
        let requests = Arc::new(Mutex::new(Vec::new()));
        let counter = Arc::new(AtomicUsize::new(0));
        let first_command_observed_before_response_finished = Arc::new(AtomicBool::new(false));
        let thread_requests = Arc::clone(&requests);
        let thread_counter = Arc::clone(&counter);
        let thread_first_command_observed =
            Arc::clone(&first_command_observed_before_response_finished);

        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                handle_provider_connection(
                    stream,
                    &thread_counter,
                    &thread_requests,
                    mode,
                    workspace.as_deref(),
                    &thread_first_command_observed,
                );
            }
        });

        Self {
            addr,
            requests,
            first_command_observed_before_response_finished,
        }
    }
}

pub(crate) fn handle_provider_connection(
    stream: TcpStream,
    counter: &AtomicUsize,
    requests: &Arc<Mutex<Vec<Value>>>,
    mode: MockMode,
    workspace: Option<&Path>,
    first_command_observed_before_response_finished: &AtomicBool,
) {
    let mut reader = BufReader::new(stream);
    let mut content_length = 0usize;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).is_err() || line == "\r\n" || line == "\n" {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse::<usize>().unwrap_or(0);
            }
        }
    }

    let mut body = vec![0; content_length];
    let _ = reader.read_exact(&mut body);
    let request = serde_json::from_slice::<Value>(&body).unwrap_or_else(|_| json!({}));

    let index = counter.fetch_add(1, Ordering::SeqCst);
    let response = provider_response(index, mode);
    requests
        .lock()
        .expect("mock provider requests lock")
        .push(request);
    let stream = reader.get_mut();
    match mode {
        MockMode::CommandRun | MockMode::TerminalEvidence(_) => {
            write_command_run_responses(stream, &response);
        }
        MockMode::CodexApplyPatchOnly => {
            if index == 0 {
                write_codex_streaming_apply_patch_missing_final_tool_call(stream);
            } else {
                write_codex_final_response(stream, "Apply-only repair completed.");
            }
        }
        MockMode::CodexStreamingProbe => {
            if index == 0 {
                write_codex_streaming_probe_response(
                    stream,
                    workspace.expect("codex streaming probe workspace"),
                    first_command_observed_before_response_finished,
                );
            } else {
                write_codex_final_response(stream, "streaming command probe completed.");
            }
        }
        MockMode::CodexStreamingSingleTaskStatusMissingFinalToolCall => {
            if index == 0 {
                write_codex_streaming_single_task_status_missing_final_tool_call(stream);
            } else {
                write_codex_final_response(stream, "I saw the streamed task_status backfill.");
            }
        }
        MockMode::RateLimit => {
            write_rate_limit_response(stream);
        }
        MockMode::TaskStatusDoingWithVisibleReply => {
            write_command_run_responses(stream, &response);
        }
        MockMode::TaskStatusOnlyThenFinal => {
            write_command_run_responses(stream, &response);
        }
        MockMode::TaskStatusDoneWithShortVisibleReply => {
            write_command_run_responses(stream, &response);
        }
        MockMode::TaskStatusDoneWithLongVisibleReply => {
            write_command_run_responses(stream, &response);
        }
        MockMode::NoToolVisibleReplyWithSyntheticCompactionUsage => {
            if index == 0 {
                write_command_run_responses(stream, &response);
            } else {
                let body = json!({
                    "error": {
                        "message": "Unexpected additional provider request in no-tool compaction fixture",
                        "type": "invalid_request_error",
                        "code": "unexpected_no_tool_compaction_request"
                    }
                })
                .to_string();
                eprintln!(
                    "no-tool compaction fixture received unexpected provider request #{}",
                    index + 1
                );
                let _ = write!(
                    stream,
                    "HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = stream.flush();
            }
        }
    }
}

pub(crate) fn provider_response(index: usize, mode: MockMode) -> Value {
    match mode {
        MockMode::TerminalEvidence(scenario) => terminal_evidence_response(index, scenario),
        MockMode::CommandRun => command_run_provider_response(index),
        MockMode::CodexApplyPatchOnly => assistant_response("Apply-only repair completed."),
        MockMode::CodexStreamingProbe => assistant_response("streaming probe completed."),
        MockMode::CodexStreamingSingleTaskStatusMissingFinalToolCall => {
            assistant_response("streamed task_status fallback completed.")
        }
        MockMode::RateLimit => json!({}),
        MockMode::TaskStatusDoingWithVisibleReply => {
            task_status_doing_with_visible_reply_response(index)
        }
        MockMode::TaskStatusOnlyThenFinal => task_status_only_then_final_response(index),
        MockMode::TaskStatusDoneWithShortVisibleReply => {
            task_status_done_with_short_visible_reply_response(index)
        }
        MockMode::TaskStatusDoneWithLongVisibleReply => {
            task_status_done_with_long_visible_reply_response(index)
        }
        MockMode::NoToolVisibleReplyWithSyntheticCompactionUsage => {
            let mut response = assistant_response(MOCK_COMPACTION_REPLY);
            response["usage"] = json!({
                "prompt_tokens": MOCK_COMPACTION_INPUT_TOKENS,
                "completion_tokens": 1,
                "total_tokens": MOCK_COMPACTION_INPUT_TOKENS + 1
            });
            response
        }
    }
}

pub(crate) fn write_rate_limit_response(stream: &mut TcpStream) {
    let body = json!({
        "error": {
            "message": "rate_limit_exceeded: retry later",
            "type": "rate_limit_exceeded",
            "code": "rate_limit_exceeded"
        }
    })
    .to_string();
    let _ = write!(
        stream,
        "HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let _ = stream.flush();
}

pub(crate) fn write_codex_streaming_probe_response(
    stream: &mut TcpStream,
    workspace: &Path,
    first_command_observed_before_response_finished: &AtomicBool,
) {
    let first_command = write_file_command("streamed-first.txt", "first");
    let second_command = write_file_command("streamed-second.txt", "second");
    let _ = write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.output_item.added",
            "item": {
                "id": "fc_stream_probe",
                "type": "function_call",
                "call_id": "call_stream_probe",
                "name": "command_run",
                "arguments": ""
            }
        }),
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_stream_probe",
            "delta": "{\"commands\":["
        }),
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_stream_probe",
            "delta": json!({
                "step": 1,
                "command_type": "shell_command",
                "command_line": json!({
                    "command": first_command,
                    "timeout_ms": MOCK_COMMAND_TIMEOUT_MS
                }).to_string()
            }).to_string() + ","
        }),
    );
    let first_path = workspace.join("streamed-first.txt");
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(8);
    while std::time::Instant::now() < deadline {
        if first_path.exists() {
            first_command_observed_before_response_finished.store(true, Ordering::SeqCst);
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
    write_codex_sse(
        stream,
        json!({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_stream_probe",
            "delta": json!({
                "step": 2,
                "command_type": "shell_command",
                "command_line": json!({
                    "command": second_command,
                    "timeout_ms": MOCK_COMMAND_TIMEOUT_MS
                }).to_string()
            }).to_string() + "]}"
        }),
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.function_call_arguments.done",
            "item_id": "fc_stream_probe",
            "arguments": json!({
                "commands": [
                    {
                        "step": 1,
                        "command_type": "shell_command",
                        "command_line": json!({
                            "command": first_command,
                            "timeout_ms": MOCK_COMMAND_TIMEOUT_MS
                        }).to_string()
                    },
                    {
                        "step": 2,
                        "command_type": "shell_command",
                        "command_line": json!({
                            "command": second_command,
                            "timeout_ms": MOCK_COMMAND_TIMEOUT_MS
                        }).to_string()
                    }
                ]
            }).to_string()
        }),
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.completed",
            "response": {
                "id": "resp_stream_probe",
                "output": [{
                    "id": "fc_stream_probe",
                    "type": "function_call",
                    "call_id": "call_stream_probe",
                    "name": "command_run",
                    "arguments": json!({
                        "commands": [
                            {
                                "step": 1,
                                "command_type": "shell_command",
                                "command_line": json!({
                                    "command": first_command,
                                    "timeout_ms": MOCK_COMMAND_TIMEOUT_MS
                                }).to_string()
                            },
                            {
                                "step": 2,
                                "command_type": "shell_command",
                                "command_line": json!({
                                    "command": second_command,
                                    "timeout_ms": MOCK_COMMAND_TIMEOUT_MS
                                }).to_string()
                            }
                        ]
                    }).to_string()
                }],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2
                }
            }
        }),
    );
    write_codex_sse_raw(stream, "data: [DONE]\n\n");
    let _ = write!(stream, "0\r\n\r\n");
    let _ = stream.flush();
}

pub(crate) fn write_codex_streaming_single_task_status_missing_final_tool_call(
    stream: &mut TcpStream,
) {
    let _ = write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.output_item.added",
            "item": {
                "id": "fc_stream_task_status_only",
                "type": "function_call",
                "call_id": "call_stream_task_status_only",
                "name": "command_run",
                "arguments": ""
            }
        }),
    );
    let command = json!({
        "step": 1,
        "command_type": "task_status",
        "command_line": json!({
            "task_group": "GUI avatar effect",
            "task_type": ["frontend", "visual"],
            "status": "doing"
        }).to_string()
    });
    write_codex_sse(
        stream,
        json!({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_stream_task_status_only",
            "delta": format!("{{\"commands\":[{command}")
        }),
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.completed",
            "response": {
                "id": "resp_stream_task_status_only",
                "output": [],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2
                }
            }
        }),
    );
    write_codex_sse_raw(stream, "data: [DONE]\n\n");
    let _ = write!(stream, "0\r\n\r\n");
    let _ = stream.flush();
}

pub(crate) fn write_codex_streaming_apply_patch_missing_final_tool_call(stream: &mut TcpStream) {
    let _ = write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.output_item.added",
            "item": {
                "id": "fc_stream_apply_patch_only",
                "type": "function_call",
                "call_id": "call_stream_apply_patch_only",
                "name": "command_run",
                "arguments": ""
            }
        }),
    );
    let patch = format!(
        "*** Begin Patch\n*** Add File: apply-only-created.txt\n+created by apply-only agent\n*** End Pat{}",
        "ch"
    );
    let command = json!({
        "step": 1,
        "command_type": "apply_patch",
        "command_line": patch
    });
    write_codex_sse(
        stream,
        json!({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_stream_apply_patch_only",
            "delta": format!("{{\"commands\":[{command}")
        }),
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.completed",
            "response": {
                "id": "resp_stream_apply_patch_only",
                "output": [],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2
                }
            }
        }),
    );
    write_codex_sse_raw(stream, "data: [DONE]\n\n");
    let _ = write!(stream, "0\r\n\r\n");
    let _ = stream.flush();
}

/// Translate the chat.completion-shaped mock `response` into the OpenAI
/// Responses-API SSE stream that the runtime now consumes for the `openai`
/// provider (non-OAuth OpenAI rides the shared Responses core).
pub(crate) fn write_command_run_responses(stream: &mut TcpStream, response: &Value) {
    let _ = write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    );

    let message = response
        .pointer("/choices/0/message")
        .cloned()
        .unwrap_or_else(|| json!({}));

    if let Some(tool_calls) = message.get("tool_calls").and_then(Value::as_array) {
        let content = message
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if !content.is_empty() {
            write_codex_sse(
                stream,
                json!({
                    "type": "response.output_text.delta",
                    "delta": content
                }),
            );
        }
        let mut output_items = Vec::new();
        if !content.is_empty() {
            output_items.push(json!({
                "id": "msg_cmd_run_visible",
                "type": "message",
                "role": "assistant",
                "content": [{
                    "type": "output_text",
                    "text": content
                }]
            }));
        }
        for (index, call) in tool_calls.iter().enumerate() {
            let call_id = call
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("call_mock")
                .to_string();
            let item_id = format!("fc_cmd_{index}");
            let name = call
                .pointer("/function/name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let arguments = call
                .pointer("/function/arguments")
                .and_then(Value::as_str)
                .unwrap_or("{}")
                .to_string();
            write_codex_sse(
                stream,
                json!({
                    "type": "response.output_item.added",
                    "item": {
                        "id": item_id,
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": ""
                    }
                }),
            );
            write_codex_sse(
                stream,
                json!({
                    "type": "response.function_call_arguments.delta",
                    "item_id": item_id,
                    "delta": arguments
                }),
            );
            write_codex_sse(
                stream,
                json!({
                    "type": "response.function_call_arguments.done",
                    "item_id": item_id,
                    "arguments": arguments
                }),
            );
            output_items.push(json!({
                "id": item_id,
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments
            }));
        }
        write_codex_sse(
            stream,
            json!({
                "type": "response.completed",
                "response": {
                    "id": "resp_cmd_run",
                    "output": output_items,
                    "usage": {
                        "input_tokens": response.pointer("/usage/prompt_tokens").and_then(Value::as_u64).unwrap_or(1),
                        "output_tokens": response.pointer("/usage/completion_tokens").and_then(Value::as_u64).unwrap_or(1),
                        "total_tokens": response.pointer("/usage/total_tokens").and_then(Value::as_u64).unwrap_or(2)
                    }
                }
            }),
        );
        write_codex_sse_raw(stream, "data: [DONE]\n\n");
        let _ = write!(stream, "0\r\n\r\n");
        let _ = stream.flush();
        return;
    }

    let content = message
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let input_tokens = response
        .pointer("/usage/prompt_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(1);
    let output_tokens = response
        .pointer("/usage/completion_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(1);
    write_command_run_final_text(stream, &content, input_tokens, output_tokens);
}

pub(crate) fn write_command_run_final_text(
    stream: &mut TcpStream,
    content: &str,
    input_tokens: u64,
    output_tokens: u64,
) {
    write_codex_sse(
        stream,
        json!({
            "type": "response.output_text.delta",
            "delta": content
        }),
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.completed",
            "response": {
                "id": "resp_cmd_run_final",
                "output": [{
                    "id": "msg_cmd_run_final",
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": content
                    }]
                }],
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens.saturating_add(output_tokens)
                }
            }
        }),
    );
    write_codex_sse_raw(stream, "data: [DONE]\n\n");
    let _ = write!(stream, "0\r\n\r\n");
    let _ = stream.flush();
}

pub(crate) fn write_codex_sse(stream: &mut TcpStream, value: Value) {
    write_codex_sse_raw(stream, &format!("data: {value}\n\n"));
}

pub(crate) fn write_codex_sse_raw(stream: &mut TcpStream, data: &str) {
    let _ = write!(stream, "{:X}\r\n{}\r\n", data.len(), data);
    let _ = stream.flush();
}

fn write_file_command(path: &str, content: &str) -> String {
    if cfg!(windows) {
        format!("Set-Content -LiteralPath '{path}' -Value '{content}'")
    } else {
        format!("printf '%s' '{content}' > {path}")
    }
}

pub(crate) fn write_codex_final_response(stream: &mut TcpStream, content: &str) {
    let _ = write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.output_text.delta",
            "delta": content
        }),
    );
    write_codex_sse(
        stream,
        json!({
            "type": "response.completed",
            "response": {
                "id": "resp_stream_probe_final",
                "output": [{
                    "id": "msg_stream_probe_final",
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": content
                    }]
                }],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2
                }
            }
        }),
    );
    write_codex_sse_raw(stream, "data: [DONE]\n\n");
    let _ = write!(stream, "0\r\n\r\n");
    let _ = stream.flush();
}

pub(crate) fn command_run_provider_response(index: usize) -> Value {
    match index {
        0 => tool_response(
            "call_command_run",
            "command_run",
            json!({
                "commands": [
                    { "command_type": "zsh", "command_line": "pwd", "timeout_ms": MOCK_COMMAND_TIMEOUT_MS, "step": 1 },
                    { "command_type": "zsh", "command_line": "echo 2", "timeout_ms": MOCK_COMMAND_TIMEOUT_MS, "step": 1 }
                ],
                "step_summary": "Call the command_run console tool as requested."
            }),
        ),
        1 => tool_response(
            "call_task_status_before_apply_patch",
            "command_run",
            json!({
                "commands": [
                    {
                        "step": 1,
                        "command_type": "task_status",
                        "command_line": json!({
                            "task_group": "runtime command run",
                            "task_type": ["debug"]
                        }).to_string()
                    }
                ],
                "step_summary": "Set task status before editing files."
            }),
        ),
        2 => tool_response(
            "call_apply_patch",
            "command_run",
            json!({
                "commands": [
                    {
                        "step": 1,
                        "command_type": "apply_patch",
                        "command_line": "*** Begin Patch\n*** Update File: src/lib.rs\n@@\n-pub fn process_manas_internal(input: &str) -> String {\n-    format!(\"processed {input}\")\n+pub fn process_manas_internal(input: &str) -> String {\n+    format!(\"processed verified {input}\")\n }\n*** End Patch"
                    },
                    {
                        "step": 2,
                        "command_type": "zsh",
                        "command_line": "cat src/lib.rs",
                        "timeout_ms": MOCK_COMMAND_TIMEOUT_MS
                    }
                ],
                "step_summary": "Patch src/lib.rs and verify the edited content."
            }),
        ),
        _ => assistant_response("done."),
    }
}

pub(crate) fn task_status_doing_with_visible_reply_response(index: usize) -> Value {
    match index {
        0 => tool_response_with_content(
            "call_task_status_doing",
            "command_run",
            json!({
                "commands": [
                    {
                        "step": 1,
                        "command_type": "task_status",
                        "command_line": json!({
                            "task_group": "商城前端",
                            "status": "doing"
                        }).to_string()
                    }
                ]
            }),
            "Done. The requested work is complete.",
        ),
        1 => tool_response_with_content(
            "call_task_status_doing_done",
            "command_run",
            json!({
                "commands": [
                    {
                        "step": 1,
                        "command_type": "task_status",
                        "command_line": json!({
                            "task_group": "商城前端",
                            "status": "done"
                        }).to_string()
                    }
                ]
            }),
            &format!(
                "I saw the doing task_status backfill. {}",
                "x".repeat(1_050)
            ),
        ),
        _ => assistant_response("Unexpected follow-up turn."),
    }
}

pub(crate) fn task_status_only_then_final_response(index: usize) -> Value {
    match index {
        0 => tool_response_message(
            "call_task_status_only",
            "command_run",
            json!({
                "commands": [
                    {
                        "step": 1,
                        "command_type": "task_status",
                        "command_line": json!({
                            "task_group": "GUI avatar effect",
                            "task_type": ["frontend", "visual"],
                            "status": "doing"
                        }).to_string()
                    }
                ]
            }),
            Value::Null,
        ),
        1 => tool_response_with_content(
            "call_task_status_only_done",
            "command_run",
            json!({
                "commands": [
                    {
                        "step": 1,
                        "command_type": "task_status",
                        "command_line": json!({
                            "task_group": "GUI avatar effect",
                            "task_type": ["frontend", "visual"],
                            "status": "done"
                        }).to_string()
                    }
                ]
            }),
            &format!(
                "I saw the task_status backfill and Operation Manual. {}",
                "x".repeat(1_050)
            ),
        ),
        _ => assistant_response("Unexpected follow-up turn."),
    }
}

pub(crate) fn task_status_done_with_long_visible_reply_response(index: usize) -> Value {
    match index {
        0 => tool_response_with_content(
            "call_task_status_done_long",
            "command_run",
            json!({
                "commands": [
                    {
                        "step": 1,
                        "command_type": "task_status",
                        "command_line": json!({
                            "task_group": "runtime backfill",
                            "status": "done"
                        }).to_string()
                    }
                ]
            }),
            &format!("Done. {}", "x".repeat(195)),
        ),
        _ => assistant_response("Unexpected follow-up turn."),
    }
}

pub(crate) fn task_status_done_with_short_visible_reply_response(index: usize) -> Value {
    match index {
        0 => tool_response_with_content(
            "call_task_status_done_short",
            "command_run",
            json!({
                "commands": [
                    {
                        "step": 1,
                        "command_type": "task_status",
                        "command_line": json!({
                            "task_group": "runtime backfill",
                            "status": "done"
                        }).to_string()
                    }
                ]
            }),
            "Done. Short visible reply.",
        ),
        _ => assistant_response("I saw the short done task_status backfill."),
    }
}

pub(crate) const TERMINAL_EVIDENCE_FINAL_REPLY: &str = "Terminal evidence fixture final assistant reply.";
pub(crate) const TERMINAL_EVIDENCE_PROGRESS_REPLY: &str = "Offline fixture: the actual patch and verification will follow.";

fn terminal_evidence_response(index: usize, scenario: TerminalEvidenceScenario) -> Value {
    if let TerminalEvidenceScenario::Ordered(ordered) = scenario {
        return ordered_evidence_response(index, ordered);
    }
    match index {
        // Use the existing actual task-status, patch, and verification loop.
        0 => tool_response_with_content("call_terminal_evidence_start", "command_run", json!({"commands":[{
            "step":1, "command_type":"task_status", "command_line":json!({
                "task_group":"runtime command run", "task_type":["debug"], "status":"doing"
            }).to_string()
        }]}), TERMINAL_EVIDENCE_PROGRESS_REPLY),
        1 => {
            let mut response = command_run_provider_response(2);
            let mut arguments: Value = serde_json::from_str(
                response.pointer("/choices/0/message/tool_calls/0/function/arguments")
                    .and_then(Value::as_str).expect("fixture arguments"),
            ).expect("fixture command JSON");
            let verify = &mut arguments["commands"][1];
            verify["command_type"] = json!("shell_command");
            verify["command_line"] = json!(match scenario {
                TerminalEvidenceScenario::FailedCommand => "exit 7".to_string(),
                TerminalEvidenceScenario::UnknownOutcome =>
                    "echo NOKIY_FIXTURE_UNKNOWN_OUTCOME".to_string(),
                TerminalEvidenceScenario::UnresolvedEffect =>
                    "echo NOKIY_FIXTURE_UNRESOLVED_EFFECT".to_string(),
                _ if cfg!(windows) => "Get-Content src/lib.rs".to_string(),
                _ => "cat src/lib.rs".to_string(),
            });
            response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] =
                json!(arguments.to_string());
            response
        }
        2 => {
            if matches!(scenario, TerminalEvidenceScenario::MissingTerminalStatus) {
                return tool_response("call_terminal_evidence_no_status", "command_run", json!({"commands":[{
                    "step":1, "command_type":"shell_command",
                    "command_line":if cfg!(windows) { "Get-Content src/lib.rs" } else { "cat src/lib.rs" }
                }]}));
            }
            let mut status = json!({"status":match scenario {
                TerminalEvidenceScenario::Question => "question",
                _ => "done",
            }});
            if matches!(scenario, TerminalEvidenceScenario::PendingCompact) {
                status["compact_context"] = json!("Offline fixture checkpoint; patch and verification already executed. Continue without replaying effects.");
            }
            let commands = if matches!(scenario, TerminalEvidenceScenario::UnsafeTerminalBatch) {
                json!([
                    {"step":1, "command_type":"shell_command", "command_line":"echo NOKIY_FIXTURE_UNKNOWN_OUTCOME"},
                    {"step":2, "command_type":"task_status", "command_line":status.to_string()}
                ])
            } else {
                json!([{"step":1, "command_type":"task_status", "command_line":status.to_string()}])
            };
            let mut response = tool_response("call_terminal_evidence_status", "command_run", json!({"commands":commands}));
            if matches!(scenario, TerminalEvidenceScenario::AutoCompact) {
                response["usage"] = json!({"prompt_tokens":MOCK_COMPACTION_INPUT_TOKENS,
                    "completion_tokens":1, "total_tokens":MOCK_COMPACTION_INPUT_TOKENS + 1});
            }
            response
        }
        _ => assistant_response(TERMINAL_EVIDENCE_FINAL_REPLY),
    }
}

fn ordered_evidence_response(index: usize, scenario: OrderedEvidenceScenario) -> Value {
    match index {
        0 => terminal_evidence_response(0, TerminalEvidenceScenario::Done),
        1 => {
            // Reuse the real patch/router loop. The check has a predetermined
            // pass/fail predicate, not stdout that needs another model review.
            let mut response = command_run_provider_response(2);
            let mut arguments: Value = serde_json::from_str(
                response.pointer("/choices/0/message/tool_calls/0/function/arguments")
                    .and_then(Value::as_str).expect("fixture arguments"),
            ).expect("fixture command JSON");
            let check = if cfg!(windows) {
                "if ((Get-Content src/lib.rs) -ccontains '    format!(\"processed verified {input}\")') { exit 0 } else { exit 7 }"
            } else {
                "grep -Fqx '    format!(\"processed verified {input}\")' src/lib.rs"
            };
            let verify = &mut arguments["commands"][1];
            verify["command_type"] = json!("shell_command");
            verify["command_line"] = json!(match scenario {
                OrderedEvidenceScenario::FailedCheck => "exit 7".to_string(),
                OrderedEvidenceScenario::TimedOutCheck if cfg!(windows) =>
                    "Start-Sleep -Seconds 30".to_string(),
                OrderedEvidenceScenario::TimedOutCheck => "sleep 30".to_string(),
                OrderedEvidenceScenario::UnknownOutcome =>
                    format!("echo NOKIY_FIXTURE_UNKNOWN_OUTCOME; {check}"),
                OrderedEvidenceScenario::UnresolvedEffect =>
                    format!("echo NOKIY_FIXTURE_UNRESOLVED_EFFECT; {check}"),
                _ => check.to_string(),
            });
            if matches!(scenario, OrderedEvidenceScenario::TimedOutCheck) {
                verify["timeout_ms"] = json!(100);
            }
            let commands = arguments["commands"].as_array_mut().expect("fixture commands");
            let mut done = json!({"step":3,
                "command_type":"task_status", "command_line":json!({"status":"done"}).to_string()});
            match scenario {
                OrderedEvidenceScenario::SeparateDone => {}
                OrderedEvidenceScenario::SameStep => {
                    done["step"] = json!(2);
                    commands.push(done);
                }
                OrderedEvidenceScenario::DoneBeforeCheck => {
                    done["step"] = json!(2);
                    commands[1]["step"] = json!(3);
                    commands.insert(1, done);
                }
                OrderedEvidenceScenario::ReversedSteps => {
                    done["step"] = json!(1);
                    commands.push(done);
                }
                _ => commands.push(done),
            }
            response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] =
                json!(arguments.to_string());
            response
        }
        2 if matches!(scenario, OrderedEvidenceScenario::SeparateDone) =>
            terminal_evidence_response(2, TerminalEvidenceScenario::Done),
        _ => assistant_response(TERMINAL_EVIDENCE_FINAL_REPLY),
    }
}

pub(crate) fn assistant_response(content: &str) -> Value {
    json!({
        "id": "chatcmpl-final",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content
            },
            "finish_reason": "stop"
        }],
        "usage": { "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2 }
    })
}

pub(crate) fn tool_response(id: &str, name: &str, arguments: Value) -> Value {
    tool_response_message(id, name, arguments, Value::Null)
}

pub(crate) fn tool_response_with_content(
    id: &str,
    name: &str,
    arguments: Value,
    content: &str,
) -> Value {
    tool_response_message(id, name, arguments, json!(content))
}

pub(crate) fn tool_response_message(
    id: &str,
    name: &str,
    arguments: Value,
    content: Value,
) -> Value {
    json!({
        "id": format!("chatcmpl-{id}"),
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": [{
                    "id": id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments.to_string()
                    }
                }]
            },
            "finish_reason": "tool_calls"
        }],
        "usage": { "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2 }
    })
}

pub(crate) fn create_rust_workspace() -> PathBuf {
    let root = std::env::temp_dir().join(format!(
        "tura-agent-lsp-e2e-{}",
        chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
    ));
    let src = root.join("src");
    std::fs::create_dir_all(&src).expect("test workspace src should be created");
    write_fixture(
        &root.join("Cargo.toml"),
        r#"[package]
name = "tura-agent-lsp-e2e"
version = "0.1.0"
edition = "2021"
"#,
    );
    write_fixture(
        &src.join("lib.rs"),
        r#"pub mod extra;
pub mod worker;

pub fn process_manas_internal(input: &str) -> String {
    format!("processed {input}")
}

pub fn run() -> String {
    worker::call_process("demo")
}
"#,
    );
    write_fixture(
        &src.join("worker.rs"),
        r#"use crate::process_manas_internal;

pub fn call_process(value: &str) -> String {
    process_manas_internal(value)
}
"#,
    );
    write_fixture(
        &src.join("extra.rs"),
        r#"pub fn second() -> String {
    crate::process_manas_internal("second")
}
"#,
    );
    std::fs::canonicalize(&root).expect("test workspace root should be canonicalized")
}

pub(crate) fn write_fixture(path: &Path, content: &str) {
    std::fs::write(path, content)
        .unwrap_or_else(|err| panic!("failed to write {}: {err}", path.display()));
}

pub(crate) fn write_llm_config(workspace: &Path, addr: SocketAddr) -> PathBuf {
    let mut routes = serde_json::Map::new();
    for route in ROUTES {
        routes.insert(
            (*route).to_string(),
            json!({
                "default_temperature": 0.0,
                "providers": [{
                    "provider": "openai",
                    "model": "mock-coder",
                    "temperature": 0.0
                }]
            }),
        );
    }
    let config = json!({
        "provider_base_url": {
            "openai": format!("http://{}", addr)
        },
        "routes": routes
    });
    let path = workspace.join("provider_config.json");
    write_fixture(
        &path,
        &serde_json::to_string_pretty(&config).expect("config should serialize"),
    );
    path
}

pub(crate) fn write_codex_llm_config(workspace: &Path, addr: SocketAddr) -> PathBuf {
    let mut routes = serde_json::Map::new();
    for route in ROUTES {
        routes.insert(
            (*route).to_string(),
            json!({
                "default_temperature": 0.0,
                "providers": [{
                    "provider": "openai",
                    "model": "mock-codex-stream",
                    "temperature": 0.0
                }]
            }),
        );
    }
    let config = json!({
        "provider_base_url": {
            "openai": format!("http://{}", addr)
        },
        "routes": routes
    });
    let path = workspace.join("tura_codex_llm_config.json");
    write_fixture(
        &path,
        &serde_json::to_string_pretty(&config).expect("config should serialize"),
    );
    path
}

pub(crate) fn tool_results(log: &[lifecycle::SessionLogEntry]) -> Vec<Value> {
    log.iter()
        .map(|entry| entry.value().clone())
        .filter(|value| value.get("type").and_then(Value::as_str) == Some("tool_result"))
        .collect()
}

pub(crate) fn assert_tool_success(tool_results: &[Value], tool_name: &str) {
    let result = tool_results
        .iter()
        .find(|result| result.get("tool_name").and_then(Value::as_str) == Some(tool_name))
        .unwrap_or_else(|| panic!("missing tool result for {tool_name}; saw {tool_results:#?}"));
    assert_eq!(
        result.get("success").and_then(Value::as_bool),
        Some(true),
        "tool {tool_name} should succeed: {result}"
    );
}

pub(crate) struct EnvGuard {
    previous: Vec<(String, Option<String>)>,
}

impl EnvGuard {
    pub(crate) fn set(values: &[(&str, &str)]) -> Self {
        let previous = values
            .iter()
            .map(|(key, _)| ((*key).to_string(), std::env::var(key).ok()))
            .collect::<Vec<_>>();
        for (key, value) in values {
            // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::set_var(key, value)
            };
        }
        Self { previous }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (key, value) in &self.previous {
            if let Some(value) = value {
                // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
                #[allow(
                    unsafe_code,
                    reason = "Rust 2024 process-environment mutation audited at the caller"
                )]
                unsafe {
                    std::env::set_var(key, value)
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
}
