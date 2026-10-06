use super::CommandResponse;
use crate::runtime::tool::{
    FunctionToolOutput, ToolCall, ToolContext, ToolError, ToolHandler, ToolPayload,
};
use crate::shell_executor;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

const MAX_PATH_BYTES: usize = 512;
const MAX_FILE_BYTES: u64 = 1024 * 1024;
const MAX_LINES: usize = 200;
const MAX_TEXT_BYTES: usize = 6144;
const MAX_RESULT_BYTES: usize = 8192;
const MAX_RANGE_TEXT_BYTES: usize = 12288;
const MAX_RANGE_RESULT_BYTES: usize = 16384;
const TERMINATION_ORIGIN: &str = "in_process_source_read";

pub fn output_limits_description() -> String {
    format!(
        "Line modes: At most {MAX_LINES} complete lines in both modes. \
         Range reads: {MAX_RANGE_TEXT_BYTES} text bytes, \
         {MAX_RANGE_RESULT_BYTES} receipt-inclusive serialized result bytes. \
         Search reads: {MAX_TEXT_BYTES} text bytes, \
         {MAX_RESULT_BYTES} receipt-inclusive serialized result bytes. \
         JSON projection reads: {MAX_RANGE_TEXT_BYTES} text bytes, \
         {MAX_RANGE_RESULT_BYTES} receipt-inclusive serialized result bytes. \
         Oversized JSON projections fail without partial output."
    )
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceReadRequest {
    path: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    start_line: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    end_line: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    expected_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    line_numbers: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    search_terms: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    context_lines: Option<usize>,
    #[serde(default, deserialize_with = "deserialize_json_pointer", skip_serializing_if = "Option::is_none")]
    json_pointer: Option<String>,
}

fn deserialize_json_pointer<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    // Omission selects line mode; a present pointer must be a string, including "".
    String::deserialize(deserializer).map(Some)
}

impl SourceReadRequest {
    pub fn path(&self) -> &str {
        &self.path
    }

    fn byte_limits(&self) -> (usize, usize) {
        if self.search_terms.is_some() {
            (MAX_TEXT_BYTES, MAX_RESULT_BYTES)
        } else {
            (MAX_RANGE_TEXT_BYTES, MAX_RANGE_RESULT_BYTES)
        }
    }
}

pub fn parse_command_line(raw: &str) -> Result<SourceReadRequest, String> {
    parse_command_line_typed(raw).map_err(|error| error.to_string())
}

#[derive(Debug)]
pub enum SourceReadParseError {
    ArgumentsTooLarge,
    Json(serde_json::Error),
    InvalidRequest(String),
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub enum SourceReadRejectionKind {
    #[serde(rename = "json_syntax_or_shape")]
    JsonSyntaxOrShape,
}

impl SourceReadParseError {
    pub fn rejection_kind(&self) -> Option<SourceReadRejectionKind> {
        match self {
            Self::Json(error) if error.is_syntax() || error.is_data() || error.is_eof() =>
                Some(SourceReadRejectionKind::JsonSyntaxOrShape),
            _ => None,
        }
    }
}

impl std::fmt::Display for SourceReadParseError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ArgumentsTooLarge => formatter.write_str("SOURCE_READ_ARGUMENTS_TOO_LARGE"),
            Self::Json(error) => write!(formatter, "SOURCE_READ_ARGUMENTS_INVALID: {error}"),
            Self::InvalidRequest(error) => formatter.write_str(error),
        }
    }
}

pub fn parse_command_line_typed(raw: &str) -> Result<SourceReadRequest, SourceReadParseError> {
    if raw.len() > 16384 {
        return Err(SourceReadParseError::ArgumentsTooLarge);
    }
    let request: SourceReadRequest = serde_json::from_str(raw)
        .map_err(SourceReadParseError::Json)?;
    validate_request(&request).map_err(SourceReadParseError::InvalidRequest)?;
    Ok(request)
}

/// A non-attempt, not a successful read, retry permission, or task-quality proof.
/// Only the router publishes this witness, before batch admission/execution.
/// Authentication requires the immutable witness in the process-owned receipt
/// store; an identical-looking callback, assistant text, or diagnostic is not proof.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceReadPreExecutionRejection {
    schema_version: String,
    owner: String,
    rejection_kind: SourceReadRejectionKind,
    session_id: String,
    runtime_id: String,
    execution_id: String,
    call_id: String,
    authorization_semantic_sha256: String,
    command_sha256: String,
    step: u64,
    error_message: String,
    effect_state: String,
    process_started: bool,
    source_content_read: bool,
    mutation_count: u64,
    authority_effect: String,
}

pub fn source_read_recovery_authorization(contract: &Value) -> Option<&str> {
    let authorization = contract["authorization_semantic_sha256"].as_str()?;
    let allowed = contract["allowed_operations"].as_array()?;
    let denied = contract["denied_operations"].as_array()?;
    let scopes = contract["read_scopes"].as_array()?;
    (contract["source_read"] == true && allowed.iter().any(|value| value == "read")
        && !denied.iter().any(|value| value == "read") && !scopes.is_empty()
        && authorization.len() == 64
        && authorization.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)))
        .then_some(authorization)
}

fn rejection_reporting_field(key: &str) -> bool {
    matches!(key, "command_id" | "command_run_id" | "provider_tool_call_id" | "command_index")
}

fn rejected_command_fingerprint(command: &Value) -> Option<(u64, String)> {
    let object = command.as_object()?;
    let line = command["command_line"].as_str()?;
    let step = match command.get("step") {
        None => 1,
        Some(value) => value.as_u64().filter(|step| *step > 0)?,
    };
    // Deliberately narrow: no aliases, inline arguments, workdir overrides, or
    // unknown command shape. Native reporting fields are not execution inputs.
    if command["command_type"] != "source_read" || line.len() > 16384
        || command.get("command").is_some_and(|value| value != "source_read")
        || object.keys().any(|key| !rejection_reporting_field(key)
            && !matches!(key.as_str(), "command_type" | "command" | "command_line" | "step" | "id" | "timeout_ms" | "stall_timeout_ms"))
        || command.get("id").is_some_and(|value| !value.is_null()
            && value.as_str().is_none_or(|id| id.trim().is_empty() || id.len() > 256))
        || ["timeout_ms", "stall_timeout_ms"].iter().any(|key|
            command.get(*key).is_some_and(|value| !value.is_null() && value.as_u64().is_none())) {
        return None;
    }
    // The provider adds this matching alias; native context drops it again.
    // Validate it above, then hash the same execution semantics on both sides.
    let mut semantic = object.iter().filter(|(key, _)| !rejection_reporting_field(key) && key.as_str() != "command")
        .map(|(key, value)| (key.as_str(), value.clone()))
        .collect::<std::collections::BTreeMap<_, _>>();
    semantic.insert("step", json!(step));
    let bytes = serde_json::to_vec(&semantic).ok()?;
    Some((step, format!("{:x}", Sha256::digest(bytes))))
}

impl SourceReadPreExecutionRejection {
    pub fn new(
        session_id: &str, runtime_id: &str, execution_id: &str, call_id: &str,
        authorization: &str, command: &Value, rejection_kind: SourceReadRejectionKind,
        error_message: String,
    ) -> Option<Self> {
        let (step, command_sha256) = rejected_command_fingerprint(command)?;
        if [session_id, runtime_id, execution_id, call_id].iter()
            .any(|id| id.trim().is_empty() || id.len() > 256)
            || error_message.is_empty() || error_message.len() > 1024 {
            return None;
        }
        Some(Self {
            schema_version: "nokiy_source_read_pre_execution_rejection_v1".to_owned(),
            owner: "router".to_owned(), rejection_kind,
            session_id: session_id.to_owned(), runtime_id: runtime_id.to_owned(),
            execution_id: execution_id.to_owned(), call_id: call_id.to_owned(),
            authorization_semantic_sha256: authorization.to_owned(), command_sha256,
            step, error_message, effect_state: "not_started".to_owned(),
            process_started: false, source_content_read: false, mutation_count: 0,
            authority_effect: "none".to_owned(),
        })
    }

    pub fn parse_bounded(value: &Value) -> Option<Self> {
        let bytes = serde_json::to_vec(value).ok()?;
        (bytes.len() <= 4096).then(|| serde_json::from_slice(&bytes).ok()).flatten()
    }

    pub fn call_id(&self) -> &str { &self.call_id }

    pub fn execution_id(&self) -> &str { &self.execution_id }

    pub fn receipt_name(&self) -> String {
        let identity = serde_json::to_vec(&(
            &self.session_id, &self.runtime_id, &self.execution_id, &self.call_id,
        )).expect("string tuple serialization");
        format!("source-read-preexecution-{:x}.json", Sha256::digest(identity))
    }

    pub fn publish(&self, store: &tura_path::command_receipts::ReceiptStore) -> std::io::Result<()> {
        let bytes = serde_json::to_vec(self).map_err(std::io::Error::other)?;
        store.publish_new(&self.receipt_name(), &bytes)
    }

    pub fn failed_result(&self) -> Value {
        json!({"command_type": "source_read", "step": self.step, "success": false,
            "error": self.error_message, "jspace_error_code": "JSPACE_SOURCE_READ_INVALID",
            "output": {"pre_execution_rejection": self}})
    }

    pub fn authenticate(
        &self, store: &tura_path::command_receipts::ReceiptStore,
        session_id: &str, runtime_id: &str, authorization: &str,
        command: &Value, result: &Value,
    ) -> bool {
        let Some(kind) = command["command_line"].as_str()
            .and_then(|line| parse_command_line_typed(line).err())
            .and_then(|error| error.rejection_kind()) else { return false; };
        let Some(expected) = Self::new(session_id, runtime_id, &self.execution_id, &self.call_id,
            authorization, command, kind, self.error_message.clone()) else { return false; };
        if self != &expected { return false; }
        let Some(object) = result.as_object() else { return false; };
        let mut canonical = object.clone();
        canonical.retain(|key, _| !rejection_reporting_field(key));
        if let Some(id) = canonical.remove("id") && command.get("id") != Some(&id) { return false; }
        if let Some(reported) = canonical.remove("command")
            && rejected_command_fingerprint(&reported) != rejected_command_fingerprint(command) { return false; }
        if Value::Object(canonical) != self.failed_result() { return false; }
        store.read(&self.receipt_name()).ok()
            .and_then(|bytes| serde_json::from_slice::<Self>(&bytes).ok())
            .is_some_and(|stored| stored == *self)
    }
}

fn validate_request(request: &SourceReadRequest) -> Result<(), String> {
    let path = request.path.as_str();
    if path.is_empty()
        || path.len() > MAX_PATH_BYTES
        || path.contains('\\')
        || path.contains('\0')
        || path
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
        || Path::new(path).is_absolute()
        || !Path::new(path)
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err("SOURCE_READ_PATH_INVALID".to_string());
    }
    if let Some(pointer) = &request.json_pointer {
        if request.start_line.is_some()
            || request.end_line.is_some()
            || request.line_numbers.is_some()
            || request.search_terms.is_some()
            || request.context_lines.is_some()
        {
            return Err("SOURCE_READ_MODE_CONFLICT".to_string());
        }
        validate_json_pointer(pointer)?;
    } else if request.start_line == Some(0)
        || match &request.search_terms {
            Some(terms) => {
                request.end_line.is_some()
                    || request.line_numbers == Some(false)
                    || terms.is_empty()
                    || terms.len() > 16
                    || terms.iter().any(String::is_empty)
                    || terms.iter().map(String::len).sum::<usize>() > 2048
                    || request.context_lines.is_some_and(|context| context > 5)
            }
            None => {
                request.context_lines.is_some()
                    || !matches!((request.start_line, request.end_line), (Some(start), Some(end)) if end >= start)
            }
        }
    {
        return Err("SOURCE_READ_RANGE_INVALID".to_string());
    }
    if let Some(expected_sha256) = &request.expected_sha256 {
        if expected_sha256.len() != 64
            || !expected_sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err("SOURCE_READ_SHA256_INVALID".to_string());
        }
    }
    Ok(())
}

fn validate_json_pointer(pointer: &str) -> Result<(), String> {
    if !pointer.is_empty() && !pointer.starts_with('/') {
        return Err("SOURCE_READ_JSON_POINTER_INVALID".to_string());
    }
    // Value::pointer performs lookup/unescaping but does not reject every bad escape.
    let mut bytes = pointer.bytes();
    while let Some(byte) = bytes.next() {
        if byte == b'~' && !matches!(bytes.next(), Some(b'0' | b'1')) {
            return Err("SOURCE_READ_JSON_POINTER_INVALID".to_string());
        }
    }
    Ok(())
}

pub fn target_path(workspace: &Path, request: &SourceReadRequest) -> Result<PathBuf, String> {
    validate_request(request)?;
    let root = workspace
        .canonicalize()
        .map_err(|error| format!("SOURCE_READ_WORKSPACE_INVALID: {error}"))?;
    if !root.is_dir()
        || workspace
            .symlink_metadata()
            .map(|metadata| metadata.file_type().is_symlink())
            .unwrap_or(true)
    {
        return Err("SOURCE_READ_WORKSPACE_INVALID".to_string());
    }
    let mut target = root;
    for component in Path::new(&request.path).components() {
        let Component::Normal(part) = component else {
            return Err("SOURCE_READ_PATH_INVALID".to_string());
        };
        target.push(part);
        let metadata = target
            .symlink_metadata()
            .map_err(|error| format!("SOURCE_READ_TARGET_INVALID: {error}"))?;
        if metadata.file_type().is_symlink() {
            return Err("SOURCE_READ_SYMLINK_DENIED".to_string());
        }
    }
    let metadata = target
        .metadata()
        .map_err(|error| format!("SOURCE_READ_TARGET_INVALID: {error}"))?;
    if !metadata.is_file() {
        return Err("SOURCE_READ_NOT_REGULAR_FILE".to_string());
    }
    if metadata.len() > MAX_FILE_BYTES {
        return Err("SOURCE_READ_FILE_TOO_LARGE".to_string());
    }
    Ok(target)
}

pub struct SourceReadHandler;

#[async_trait::async_trait]
impl ToolHandler for SourceReadHandler {
    fn tool_name(&self) -> &str {
        "source_read"
    }

    async fn is_mutating(&self, _call: &ToolCall, _ctx: &ToolContext) -> bool {
        false
    }

    async fn handle(
        &self,
        call: ToolCall,
        ctx: ToolContext,
    ) -> Result<FunctionToolOutput, ToolError> {
        let Some(root) = ctx.source_read_root() else {
            return Err(ToolError::RespondToModel(
                "SOURCE_READ_JSPACE_REQUIRED".to_string(),
            ));
        };
        let request = match &call.payload {
            ToolPayload::Function { arguments } => {
                serde_json::from_value::<SourceReadRequest>(arguments.clone()).map_err(|error| {
                    ToolError::RespondToModel(format!("SOURCE_READ_ARGUMENTS_INVALID: {error}"))
                })?
            }
            ToolPayload::Freeform { .. } => {
                return Err(ToolError::RespondToModel(
                    "SOURCE_READ_ARGUMENTS_INVALID".to_string(),
                ));
            }
        };
        validate_request(&request).map_err(ToolError::RespondToModel)?;
        let (_, max_result_bytes) = request.byte_limits();
        let minimum = shell_executor::preview_in_process_terminal_response(
            &ctx,
            &result_too_large_response(),
            TERMINATION_ORIGIN,
        )
        .map_err(|error| {
            ToolError::RespondToModel(format!("SOURCE_READ_RECEIPT_INVALID: {error}"))
        })?;
        if result_too_large(&minimum, max_result_bytes) {
            return Err(ToolError::RespondToModel(
                "SOURCE_READ_RECEIPT_ENVELOPE_TOO_LARGE".to_string(),
            ));
        }
        let response = shell_executor::run_in_process_command_with_terminal_receipt(
            &ctx,
            TERMINATION_ORIGIN,
            || fit_response_to_envelope(execute(&request, root), &ctx, max_result_bytes),
        );
        let body = shell_executor::json_like_output(
            response.exit_code,
            response.stdout,
            response.stderr,
            response.output,
            response.changes,
        );
        Ok(FunctionToolOutput::from_value(body, Some(response.success)))
    }
}

fn result_too_large_response() -> CommandResponse {
    let error = "SOURCE_READ_RESULT_TOO_LARGE";
    CommandResponse {
        success: false,
        exit_code: 1,
        stdout: String::new(),
        stderr: error.to_string(),
        output: json!({"error_type": error}),
        changes: Vec::new(),
    }
}

fn fit_response_to_envelope(
    mut response: CommandResponse,
    ctx: &ToolContext,
    max_result_bytes: usize,
) -> CommandResponse {
    let preview = |response: &CommandResponse| {
        shell_executor::preview_in_process_terminal_response(ctx, response, TERMINATION_ORIGIN)
            .map(|body| !result_too_large(&body, max_result_bytes))
            .unwrap_or(false)
    };
    if preview(&response) {
        return response;
    }
    if response.success && response.output["mode"] != "json_projection" {
        let original_stdout = response.stdout.clone();
        let lines: Vec<&str> = if original_stdout.is_empty() {
            Vec::new()
        } else {
            original_stdout.split_inclusive('\n').collect()
        };
        let start_line = response.output["start_line"].as_u64().unwrap_or(0);
        let numbered = response.output["line_numbers"] == true;
        for kept in (1..lines.len()).rev() {
            response.stdout = lines[..kept].concat();
            let end_line = if numbered {
                lines[kept - 1]
                    .split_once(": ")
                    .and_then(|(label, _)| label.parse::<u64>().ok())
                    .expect("numbered source line")
            } else {
                start_line + kept as u64 - 1
            };
            if let Some(metadata) = response.output.as_object_mut() {
                metadata.insert("end_line".to_string(), json!(end_line));
                metadata.insert("next_line".to_string(), json!(end_line + 1));
                if let Some(matches) = metadata.get_mut("search_matches").and_then(Value::as_array_mut) {
                    matches.retain(|number| number.as_u64().is_some_and(|number| number <= end_line));
                }
                metadata.insert("truncated".to_string(), json!(true));
                metadata.insert("truncation_reason".to_string(), json!("response_limit"));
                metadata.insert("at_eof".to_string(), json!(false));
                metadata.insert(
                    "ends_with_newline".to_string(),
                    json!(response.stdout.ends_with('\n')),
                );
            }
            if preview(&response) {
                return response;
            }
        }
    }
    result_too_large_response()
}

fn result_too_large(value: &Value, max_result_bytes: usize) -> bool {
    serde_json::to_vec(value)
        .map(|bytes| bytes.len() > max_result_bytes)
        .unwrap_or(true)
}

fn execute(request: &SourceReadRequest, root: &File) -> CommandResponse {
    match read_range_from_root(request, root) {
        Ok((text, metadata)) => CommandResponse {
            success: true,
            exit_code: 0,
            stdout: text,
            stderr: String::new(),
            output: metadata,
            changes: Vec::new(),
        },
        Err(error) => CommandResponse {
            success: false,
            exit_code: 1,
            stdout: String::new(),
            stderr: error.clone(),
            output: json!({"error_type": error}),
            changes: Vec::new(),
        },
    }
}

#[cfg(test)]
fn read_range(request: &SourceReadRequest, workspace: &Path) -> Result<(String, Value), String> {
    target_path(workspace, request)?;
    let canonical = workspace
        .canonicalize()
        .map_err(|_| "SOURCE_READ_WORKSPACE_INVALID".to_string())?;
    let root = open_admitted_root(&canonical)?;
    read_range_from_root(request, &root)
}

fn read_range_from_root(
    request: &SourceReadRequest,
    root: &File,
) -> Result<(String, Value), String> {
    validate_request(request)?;
    let file = open_no_follow(root, &request.path)?;
    let metadata = file
        .metadata()
        .map_err(|_| "SOURCE_READ_TARGET_INVALID".to_string())?;
    if !metadata.is_file() {
        return Err("SOURCE_READ_NOT_REGULAR_FILE".to_string());
    }
    if metadata.len() > MAX_FILE_BYTES {
        return Err("SOURCE_READ_FILE_TOO_LARGE".to_string());
    }
    let mut bytes = Vec::new();
    file.take(MAX_FILE_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "SOURCE_READ_IO_ERROR".to_string())?;
    if bytes.len() as u64 > MAX_FILE_BYTES {
        return Err("SOURCE_READ_FILE_TOO_LARGE".to_string());
    }
    let file_bytes = bytes.len();
    let sha256 = format!("{:x}", Sha256::digest(&bytes));
    if request
        .expected_sha256
        .as_ref()
        .is_some_and(|expected| !expected.eq_ignore_ascii_case(&sha256))
    {
        return Err("SOURCE_READ_SHA256_MISMATCH".to_string());
    }
    if let Some(pointer) = &request.json_pointer {
        return read_json_projection(request, pointer, &bytes, &sha256);
    }
    let content = std::str::from_utf8(&bytes).map_err(|_| "SOURCE_READ_NOT_UTF8".to_string())?;
    let lines: Vec<&str> = if content.is_empty() {
        Vec::new()
    } else {
        content.split_inclusive('\n').collect()
    };
    if let Some(terms) = &request.search_terms {
        return read_search(request, terms, &lines, file_bytes, &sha256);
    }
    let start_line = request.start_line.expect("validated range");
    let requested_end = request.end_line.expect("validated range");
    if start_line > lines.len() {
        return Err("SOURCE_READ_RANGE_OUT_OF_BOUNDS".to_string());
    }
    let bounded_end = requested_end.min(start_line.saturating_add(MAX_LINES - 1));
    let available_end = bounded_end.min(lines.len());
    let (max_text_bytes, _) = request.byte_limits();
    let mut text = String::new();
    let mut end_line = start_line - 1;
    for number in start_line..=available_end {
        if !append_line(
            &mut text,
            lines[number - 1],
            number,
            request.line_numbers == Some(true),
            max_text_bytes,
        )? {
            break;
        }
        end_line = number;
    }
    let truncation_reason = if end_line < available_end {
        Some("response_limit")
    } else if end_line < requested_end && end_line == lines.len() {
        Some("end_of_file")
    } else if end_line < requested_end {
        Some("line_limit")
    } else {
        None
    };
    let next_line = (end_line < lines.len()).then_some(end_line + 1);
    let mut result = json!({
            "path": request.path,
            "start_line": start_line,
            "end_line": end_line,
            "requested_end_line": requested_end,
            "truncated": truncation_reason.is_some(),
            "truncation_reason": truncation_reason,
            "next_line": next_line,
            "at_eof": next_line.is_none(),
            "total_lines": lines.len(),
            "file_bytes": file_bytes,
            "source_sha256": sha256,
            "ends_with_newline": lines[end_line - 1].ends_with('\n'),
        });
    if request.line_numbers == Some(true) {
        result["line_numbers"] = json!(true);
    }
    Ok((text, result))
}

fn read_json_projection(
    request: &SourceReadRequest,
    pointer: &str,
    bytes: &[u8],
    sha256: &str,
) -> Result<(String, Value), String> {
    let document: Value = serde_json::from_slice(bytes)
        .map_err(|_| "SOURCE_READ_JSON_INVALID".to_string())?;
    let selected = document.pointer(pointer)
        .ok_or_else(|| "SOURCE_READ_JSON_POINTER_NOT_FOUND".to_string())?;
    let text = serde_json::to_string(selected)
        .map_err(|_| "SOURCE_READ_JSON_SERIALIZATION_FAILED".to_string())?;
    let (max_text_bytes, _) = request.byte_limits();
    if text.len() > max_text_bytes
        || serde_json::to_string(&text)
            .map(|encoded| encoded.len() > max_text_bytes)
            .unwrap_or(true)
    {
        return Err("SOURCE_READ_JSON_VALUE_TOO_LARGE".to_string());
    }
    Ok((text, json!({
        "mode": "json_projection",
        "path": request.path,
        "json_pointer": pointer,
        "file_bytes": bytes.len(),
        "source_sha256": sha256,
        "truncated": false,
    })))
}

fn append_line(
    text: &mut String,
    line: &str,
    number: usize,
    numbered: bool,
    max_text_bytes: usize,
) -> Result<bool, String> {
    let display = if numbered {
        format!("{number}: {line}")
    } else {
        line.to_string()
    };
    let mut candidate = text.clone();
    candidate.push_str(&display);
    if candidate.len() > max_text_bytes
        || serde_json::to_string(&candidate)
            .map(|encoded| encoded.len() > max_text_bytes)
            .unwrap_or(true)
    {
        if text.is_empty() {
            return Err("SOURCE_READ_LINE_TOO_LONG".to_string());
        }
        return Ok(false);
    }
    *text = candidate;
    Ok(true)
}

fn read_search(
    request: &SourceReadRequest,
    terms: &[String],
    lines: &[&str],
    file_bytes: usize,
    sha256: &str,
) -> Result<(String, Value), String> {
    let start = request.start_line.unwrap_or(1);
    // A cursor immediately after EOF is a successful empty continuation.
    if start > lines.len().saturating_add(1) {
        return Err("SOURCE_READ_RANGE_OUT_OF_BOUNDS".to_string());
    }
    let context = request.context_lines.unwrap_or(0);
    let (max_text_bytes, _) = request.byte_limits();
    let mut selected = vec![false; lines.len()];
    let mut matches = vec![false; lines.len()];
    for index in start.saturating_sub(context + 1)..lines.len() {
        if terms.iter().any(|term| lines[index].contains(term)) {
            if index >= start - 1 {
                matches[index] = true;
            }
            let from = index.saturating_sub(context).max(start - 1);
            let to = index.saturating_add(context).saturating_add(1).min(lines.len());
            selected[from..to].fill(true);
        }
    }
    let mut text = String::new();
    let mut matched_lines = Vec::new();
    let mut end = start - 1;
    let mut kept = 0;
    let mut reason = None;
    for index in start - 1..lines.len() {
        if !selected[index] {
            continue;
        }
        if kept == MAX_LINES {
            reason = Some("line_limit");
            break;
        }
        if !append_line(&mut text, lines[index], index + 1, true, max_text_bytes)? {
            reason = Some("response_limit");
            break;
        }
        kept += 1;
        end = index + 1;
        if matches[index] {
            matched_lines.push(index + 1);
        }
    }
    let next_line = reason.map(|_| end + 1);
    Ok((text, json!({
        "path": request.path,
        "start_line": start,
        "end_line": end,
        "line_numbers": true,
        "search_matches": matched_lines,
        "context_lines": context,
        "truncated": reason.is_some(),
        "truncation_reason": reason,
        "next_line": next_line,
        "at_eof": next_line.is_none(),
        "total_lines": lines.len(),
        "file_bytes": file_bytes,
        "source_sha256": sha256,
        "ends_with_newline": kept > 0 && lines[end - 1].ends_with('\n'),
    })))
}

#[cfg(unix)]
pub fn open_admitted_root(root: &Path) -> Result<File, String> {
    use rustix::fs::{CWD, Mode, OFlags, openat};
    if !root.is_absolute() {
        return Err("SOURCE_READ_WORKSPACE_INVALID".to_string());
    }
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW;
    let mut directory = openat(CWD, Path::new("/"), flags, Mode::empty())
        .map_err(|_| "SOURCE_READ_WORKSPACE_INVALID".to_string())?;
    for component in root.components() {
        let part = match component {
            Component::RootDir => continue,
            Component::Normal(part) => part,
            _ => return Err("SOURCE_READ_WORKSPACE_INVALID".to_string()),
        };
        directory = openat(&directory, part, flags, Mode::empty())
            .map_err(|_| "SOURCE_READ_WORKSPACE_INVALID".to_string())?;
    }
    Ok(directory.into())
}

#[cfg(not(unix))]
pub fn open_admitted_root(_root: &Path) -> Result<File, String> {
    Err("SOURCE_READ_PLATFORM_UNSUPPORTED".to_string())
}

#[cfg(unix)]
fn open_no_follow(root: &File, relative: &str) -> Result<File, String> {
    use rustix::fs::{Mode, OFlags, openat};
    let mut directory = root
        .try_clone()
        .map_err(|_| "SOURCE_READ_WORKSPACE_INVALID".to_string())?;
    let parts: Vec<_> = Path::new(relative).components().collect();
    for (index, component) in parts.iter().enumerate() {
        let Component::Normal(part) = component else {
            return Err("SOURCE_READ_PATH_INVALID".to_string());
        };
        let last = index + 1 == parts.len();
        let flags = if last {
            OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK
        } else {
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC | OFlags::NOFOLLOW
        };
        let next = openat(&directory, *part, flags, Mode::empty())
            .map_err(|_| "SOURCE_READ_TARGET_INVALID".to_string())?;
        if last {
            return Ok(next.into());
        }
        directory = next.into();
    }
    Err("SOURCE_READ_PATH_INVALID".to_string())
}

#[cfg(not(unix))]
fn open_no_follow(_root: &File, _relative: &str) -> Result<File, String> {
    Err("SOURCE_READ_PLATFORM_UNSUPPORTED".to_string())
}

#[cfg(test)]
mod tests {
    use super::{
        SourceReadHandler, execute, open_admitted_root, parse_command_line, read_range,
        read_range_from_root, target_path,
    };
    use crate::runtime::tool::{ToolCall, ToolContext, ToolHandler, ToolPayload};
    use crate::shell_executor;
    use serde_json::json;
    use sha2::{Digest, Sha256};
    use std::fs;
    use std::sync::Arc;

    #[test]
    fn rejected_read_fingerprint_normalizes_only_default_step_and_reporting_alias() {
        let bare = json!({"command_type":"source_read", "command_line":
            "{\"path\":\"answer.txt\",\"start_line\":1,\"end_line\": ninety}"});
        let mut explicit = bare.clone();
        explicit["step"] = json!(1);
        explicit["command"] = json!("source_read");
        assert_eq!(super::rejected_command_fingerprint(&bare), super::rejected_command_fingerprint(&explicit));
        assert_eq!(super::rejected_command_fingerprint(&bare).unwrap().1,
            "bc9280e89420013017fe22950605b1a35db245145a3a4589534aa6d912bf0c76");
        explicit["step"] = json!(2);
        assert_ne!(super::rejected_command_fingerprint(&bare), super::rejected_command_fingerprint(&explicit));
    }

    #[test]
    fn typed_json_rejection_never_classifies_semantic_validation_or_size_errors() {
        use super::{SourceReadRejectionKind, parse_command_line_typed};
        for raw in [
            r#"{"path":"file","start_line":1,"end_line":ninety}"#,
            r#"{"path":"file","start_line":1,"end_line":"ninety"}"#,
            r#"{"path":"file","start_line":1,"end_line":90"#,
        ] {
            let error = parse_command_line_typed(raw).unwrap_err();
            assert_eq!(error.rejection_kind(), Some(SourceReadRejectionKind::JsonSyntaxOrShape));
            assert_eq!(parse_command_line(raw).unwrap_err(), error.to_string());
        }
        for raw in [
            r#"{"path":"../secret","start_line":1,"end_line":2}"#.to_owned(),
            r#"{"path":"file","start_line":0,"end_line":2}"#.to_owned(),
            r#"{"path":"file","start_line":1,"end_line":2,"expected_sha256":"bad"}"#.to_owned(),
            " ".repeat(16385),
        ] {
            assert_eq!(parse_command_line_typed(&raw).unwrap_err().rejection_kind(), None);
        }
        assert!(parse_command_line_typed(r#"{"path":"file","start_line":1,"end_line":90}"#).is_ok());
        assert!(parse_command_line_typed(r#"["file",1,90]"#).is_ok());
    }

    #[test]
    fn syntax_recovery_authorization_never_relaxes_read_permissions() {
        let contract = json!({"source_read":true, "allowed_operations":["read"],
            "denied_operations":[], "read_scopes":["file"], "authorization_semantic_sha256":"a".repeat(64)});
        assert!(super::source_read_recovery_authorization(&contract).is_some());
        for (key, value) in [
            ("source_read", json!(false)), ("allowed_operations", json!(["command"])),
            ("denied_operations", json!(["read"])), ("read_scopes", json!([])),
            ("authorization_semantic_sha256", json!("unknown")),
        ] {
            let mut denied = contract.clone();
            denied[key] = value;
            assert!(super::source_read_recovery_authorization(&denied).is_none(), "{key}");
        }
    }

    #[test]
    fn pre_execution_witness_requires_durable_exact_router_evidence() {
        use super::{SourceReadPreExecutionRejection, SourceReadRejectionKind};
        let root = tempfile::tempdir().expect("workspace");
        let directory = root.path().canonicalize().expect("physical workspace");
        let store = tura_path::command_receipts::ReceiptStore::open(&directory).expect("receipt store");
        let command = json!({"command_type":"source_read", "step":1,
            "command_line":r#"{"path":"file","start_line":1,"end_line":ninety}"#});
        let authorization = "a".repeat(64);
        let mut conflicting = command.clone();
        conflicting["command"] = json!("shell_command");
        assert!(SourceReadPreExecutionRejection::new("session", "runtime", "execution", "call",
            &authorization, &conflicting, SourceReadRejectionKind::JsonSyntaxOrShape,
            "conflicting command".to_owned()).is_none());
        let proof = SourceReadPreExecutionRejection::new("session", "runtime", "execution", "call",
            &authorization, &command, SourceReadRejectionKind::JsonSyntaxOrShape,
            "syntax diagnostic (not an authority signal)".to_owned()).expect("witness");
        let result = proof.failed_result();
        assert!(!proof.authenticate(&store, "session", "runtime", &authorization, &command, &result));
        proof.publish(&store).expect("router publication");
        assert!(proof.authenticate(&store, "session", "runtime", &authorization, &command, &result));
        assert!(proof.publish(&store).is_err(), "immutable publication never overwrites evidence");
        for (field, value) in [
            ("owner", json!("model")), ("runtime_id", json!("foreign-runtime")),
            ("session_id", json!("foreign-session")), ("execution_id", json!("foreign-execution")),
            ("call_id", json!("foreign-call")), ("command_sha256", json!("b".repeat(64))),
            ("authorization_semantic_sha256", json!("b".repeat(64))),
            ("process_started", json!(true)), ("source_content_read", json!(true)),
            ("mutation_count", json!(1)), ("authority_effect", json!("write")),
            ("effect_state", json!("unknown")), ("error_message", json!("changed")),
        ] {
            let mut forged = serde_json::to_value(&proof).expect("witness JSON");
            forged[field] = value;
            let forged = SourceReadPreExecutionRejection::parse_bounded(&forged).expect("well-shaped forgery");
            assert!(!forged.authenticate(&store, "session", "runtime", &authorization,
                &command, &forged.failed_result()), "{field}");
        }
        let mut unknown = serde_json::to_value(&proof).unwrap();
        unknown["rejection_kind"] = json!("permission_denial");
        assert!(SourceReadPreExecutionRejection::parse_bounded(&unknown).is_none());
        let mut unknown = serde_json::to_value(&proof).unwrap();
        unknown["unresolved_effects"] = json!([]);
        assert!(SourceReadPreExecutionRejection::parse_bounded(&unknown).is_none());
        let mut success = result.clone();
        success["success"] = json!(true);
        success["error"] = serde_json::Value::Null;
        assert!(!proof.authenticate(&store, "session", "runtime", &authorization, &command, &success));
        store.replace(&proof.receipt_name(), b"{}").expect("simulate corrupt durable evidence");
        assert!(!proof.authenticate(&store, "session", "runtime", &authorization, &command, &result));
    }

    #[test]
    fn positional_ranges_preserve_field_order_and_optional_defaults() {
        let hash = format!("{:x}", Sha256::digest(b"source"));
        for (array, expected_sha256, line_numbers) in [
            (json!(["file", 2, 3]), None, None),
            (json!(["file", 2, 3, null]), None, None),
            (json!(["file", 2, 3, null, null]), None, None),
            (json!(["file", 2, 3, null, false]), None, Some(false)),
            (json!(["file", 2, 3, null, true]), None, Some(true)),
            (json!(["file", 2, 3, hash]), Some(hash.as_str()), None),
            (json!(["file", 2, 3, hash, null]), Some(hash.as_str()), None),
            (
                json!(["file", 2, 3, hash, false]),
                Some(hash.as_str()),
                Some(false),
            ),
            (
                json!(["file", 2, 3, hash, true]),
                Some(hash.as_str()),
                Some(true),
            ),
            (json!(["file", 2, 3, null, null, null, null]), None, None),
        ] {
            let positional = parse_command_line(&array.to_string()).expect("positional range");
            let named = parse_command_line(
                &json!({
                    "path": "file", "start_line": 2, "end_line": 3,
                    "expected_sha256": expected_sha256, "line_numbers": line_numbers,
                })
                .to_string(),
            )
            .expect("named range");
            assert_eq!(positional.path(), "file");
            assert_eq!(positional.start_line, Some(2));
            assert_eq!(positional.end_line, Some(3));
            assert_eq!(positional.expected_sha256.as_deref(), expected_sha256);
            assert_eq!(positional.line_numbers, line_numbers);
            assert!(positional.search_terms.is_none());
            assert!(positional.context_lines.is_none());
            assert_eq!(
                serde_json::to_value(&positional).unwrap(),
                serde_json::to_value(&named).unwrap(),
                "{array}"
            );
        }
    }

    #[test]
    fn positional_ranges_match_named_validation_errors() {
        let non_hex_hash = "g".repeat(64);
        for (path, start, end, hash, error) in [
            (
                "../secret",
                Some(1),
                Some(2),
                None,
                "SOURCE_READ_PATH_INVALID",
            ),
            (
                "/secret",
                Some(1),
                Some(2),
                None,
                "SOURCE_READ_PATH_INVALID",
            ),
            (
                "src//file",
                Some(1),
                Some(2),
                None,
                "SOURCE_READ_PATH_INVALID",
            ),
            (
                "src\\file",
                Some(1),
                Some(2),
                None,
                "SOURCE_READ_PATH_INVALID",
            ),
            ("file", Some(0), Some(2), None, "SOURCE_READ_RANGE_INVALID"),
            ("file", Some(3), Some(2), None, "SOURCE_READ_RANGE_INVALID"),
            ("file", None, Some(2), None, "SOURCE_READ_RANGE_INVALID"),
            ("file", Some(1), None, None, "SOURCE_READ_RANGE_INVALID"),
            (
                "file",
                Some(1),
                Some(2),
                Some("abcd"),
                "SOURCE_READ_SHA256_INVALID",
            ),
            (
                "file",
                Some(1),
                Some(2),
                Some(non_hex_hash.as_str()),
                "SOURCE_READ_SHA256_INVALID",
            ),
        ] {
            for arguments in [
                json!([path, start, end, hash]),
                json!({
                    "path": path, "start_line": start, "end_line": end,
                    "expected_sha256": hash,
                }),
            ] {
                assert_eq!(
                    parse_command_line(&arguments.to_string()).unwrap_err(),
                    error,
                    "{arguments}"
                );
            }
        }
    }

    #[test]
    fn positional_ranges_reject_malformed_mistyped_and_extra_fields() {
        for raw in [
            r#"[]"#,
            r#"["file"]"#,
            r#"["file",1]"#,
            r#"["file",1,2"#,
            r#"["file",1,2,]"#,
            r#"["file",1,2] []"#,
            r#"[null,1,2]"#,
            r#"[7,1,2]"#,
            r#"["file","1",2]"#,
            r#"["file",-1,2]"#,
            r#"["file",1.5,2]"#,
            r#"["file",1,"2"]"#,
            r#"["file",1,false]"#,
            r#"["file",1,2,true]"#,
            r#"["file",1,2,42]"#,
            r#"["file",1,2,null,"true"]"#,
            r#"["file",1,2,null,1]"#,
            r#"["file",1,2,null,null,true]"#,
            r#"["file",1,2,null,null,null,"1"]"#,
            // An extra null must not be ignored or treated as a json_pointer string.
            r#"["file",1,2,null,null,null,null,null]"#,
        ] {
            assert!(parse_command_line(raw).is_err(), "{raw}");
        }
    }

    #[test]
    fn positional_ranges_match_named_pagination_and_limits() {
        let root = tempfile::tempdir().expect("workspace");
        for (content, first_reason) in [
            ("x\n".repeat(301), "line_limit"),
            (
                format!("{}\n", "x".repeat(160)).repeat(100),
                "response_limit",
            ),
        ] {
            fs::write(root.path().join("file"), &content).expect("source");
            let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
            let total_lines = content.lines().count();
            let requested_end = total_lines + 1;
            for line_numbers in [None, Some(false), Some(true)] {
                let mut start = 1;
                loop {
                    let positional = parse_command_line(
                        &json!(["file", start, requested_end, hash, line_numbers]).to_string(),
                    )
                    .expect("positional page");
                    let named = parse_command_line(
                        &json!({
                            "path": "file", "start_line": start, "end_line": requested_end,
                            "expected_sha256": hash, "line_numbers": line_numbers,
                        })
                        .to_string(),
                    )
                    .expect("named page");
                    let page = read_range(&positional, root.path());
                    assert_eq!(page, read_range(&named, root.path()));
                    let (text, metadata) = page.expect("bounded page");
                    let end = metadata["end_line"].as_u64().expect("end line") as usize;
                    assert!(end >= start && end - start + 1 <= super::MAX_LINES);
                    assert!(text.len() <= super::MAX_RANGE_TEXT_BYTES);
                    assert!(serde_json::to_string(&text).unwrap().len() <= super::MAX_RANGE_TEXT_BYTES);
                    let expected: String = content
                        .split_inclusive('\n')
                        .enumerate()
                        .skip(start - 1)
                        .take(end - start + 1)
                        .map(|(index, line)| {
                            if line_numbers == Some(true) {
                                format!("{}: {line}", index + 1)
                            } else {
                                line.to_string()
                            }
                        })
                        .collect();
                    assert_eq!(text, expected);
                    assert_eq!(metadata["source_sha256"], hash);
                    assert_eq!(metadata["start_line"], start);
                    assert_eq!(metadata["requested_end_line"], requested_end);
                    if start == 1 {
                        assert_eq!(metadata["truncation_reason"], first_reason);
                    }
                    if metadata["next_line"].is_null() {
                        assert_eq!(end, total_lines);
                        assert_eq!(metadata["at_eof"], true);
                        assert_eq!(metadata["truncation_reason"], "end_of_file");
                        break;
                    }
                    assert_eq!(metadata["at_eof"], false);
                    assert_eq!(metadata["next_line"], end + 1);
                    start = end + 1;
                }
            }
        }
    }

    #[test]
    fn positional_ranges_match_named_read_failures() {
        let root = tempfile::tempdir().expect("workspace");
        let old_hash = format!("{:x}", Sha256::digest(b"old source\n"));
        for (content, start, end, hash, error) in [
            (
                "changed\n".to_string(),
                1,
                1,
                Some(old_hash.as_str()),
                "SOURCE_READ_SHA256_MISMATCH",
            ),
            (
                "x".repeat(super::MAX_RANGE_TEXT_BYTES + 1),
                1,
                1,
                None,
                "SOURCE_READ_LINE_TOO_LONG",
            ),
            (
                "one\ntwo\n".to_string(),
                3,
                3,
                None,
                "SOURCE_READ_RANGE_OUT_OF_BOUNDS",
            ),
            (
                "x".repeat(super::MAX_FILE_BYTES as usize + 1),
                1,
                1,
                None,
                "SOURCE_READ_FILE_TOO_LARGE",
            ),
        ] {
            fs::write(root.path().join("file"), content).expect("source");
            for arguments in [
                json!(["file", start, end, hash, true]),
                json!({
                    "path": "file", "start_line": start, "end_line": end,
                    "expected_sha256": hash, "line_numbers": true,
                }),
            ] {
                let request = parse_command_line(&arguments.to_string()).expect("valid range");
                assert_eq!(read_range(&request, root.path()).unwrap_err(), error);
            }
        }
    }

    #[test]
    fn exact_utf8_lines_and_expected_hash_are_preserved() {
        let root = tempfile::tempdir().expect("workspace");
        fs::create_dir(root.path().join("src")).expect("source directory");
        let content = "alpha\n中文\nfinal";
        fs::write(root.path().join("src/main.rs"), content).expect("source file");
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let request = parse_command_line(
            &json!({
                "path": "src/main.rs", "start_line": 2, "end_line": 3,
                "expected_sha256": hash,
            })
            .to_string(),
        )
        .expect("valid read");
        let (text, metadata) = read_range(&request, root.path()).expect("read range");
        assert_eq!(text, "中文\nfinal");
        assert_eq!(metadata["total_lines"], 3);
        assert_eq!(metadata["source_sha256"], hash);
        assert_eq!(metadata["ends_with_newline"], false);
        let admitted_root =
            open_admitted_root(&root.path().canonicalize().expect("canonical root"))
                .expect("admitted root");
        assert!(execute(&request, &admitted_root).success);
    }

    #[test]
    fn directory_file_reads_preserve_sha_pagination_and_output_limits() {
        let root = tempfile::tempdir().expect("workspace");
        fs::create_dir(root.path().join("src")).expect("directory read root");
        let path = "src/discovered-later.txt";
        let content: String = (1..=205).map(|line| format!("needle-{line}\n")).collect();
        fs::write(root.path().join(path), &content).expect("discovered file");
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let range = parse_command_line(&json!({
            "path": path, "start_line": 1, "end_line": 205, "line_numbers": true,
            "expected_sha256": hash,
        }).to_string()).unwrap();
        let (text, metadata) = read_range(&range, root.path()).unwrap();
        assert_eq!(text.lines().count(), super::MAX_LINES);
        assert!(text.len() <= super::MAX_RANGE_TEXT_BYTES);
        assert_eq!(metadata["source_sha256"], hash);
        assert_eq!(metadata["next_line"], 201);
        assert_eq!(metadata["truncation_reason"], "line_limit");
        let resume = parse_command_line(&json!({
            "path": path, "start_line": metadata["next_line"], "end_line": 205,
            "expected_sha256": metadata["source_sha256"],
        }).to_string()).unwrap();
        let (text, metadata) = read_range(&resume, root.path()).unwrap();
        assert_eq!(text, "needle-201\nneedle-202\nneedle-203\nneedle-204\nneedle-205\n");
        assert_eq!(metadata["source_sha256"], hash);
        assert_eq!(metadata["next_line"], serde_json::Value::Null);
        let search = parse_command_line(&json!({
            "path": path, "search_terms": ["needle"], "expected_sha256": hash,
        }).to_string()).unwrap();
        let (text, metadata) = read_range(&search, root.path()).unwrap();
        assert_eq!(text.lines().count(), super::MAX_LINES);
        assert!(text.len() <= super::MAX_TEXT_BYTES);
        assert_eq!(metadata["source_sha256"], hash);
        assert_eq!(metadata["next_line"], 201);
        let admitted_root = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        // Even unchanged requested lines cannot be read with a stale whole-file SHA.
        fs::write(root.path().join(path), format!("{content}changed\n")).unwrap();
        for request in [&range, &resume, &search] {
            let response = execute(request, &admitted_root);
            assert!(!response.success);
            assert!(response.stdout.is_empty());
            assert_eq!(response.output["error_type"], "SOURCE_READ_SHA256_MISMATCH");
        }
    }

    #[test]
    fn legacy_and_numbered_ranges_preserve_source_and_physical_labels() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("file"), "one\r\n中🙂\r\nlast").expect("source");
        let legacy = parse_command_line(r#"{"path":"file","start_line":2,"end_line":3}"#)
            .expect("legacy request");
        let (text, metadata) = read_range(&legacy, root.path()).expect("legacy read");
        assert_eq!(text, "中🙂\r\nlast");
        assert!(metadata.get("line_numbers").is_none());
        let numbered = parse_command_line(
            r#"{"path":"file","start_line":2,"end_line":3,"line_numbers":true}"#,
        )
        .expect("numbered request");
        let (text, metadata) = read_range(&numbered, root.path()).expect("numbered read");
        assert_eq!(text, "2: 中🙂\r\n3: last");
        assert_eq!(metadata["end_line"], 3);
        assert_eq!(metadata["ends_with_newline"], false);
        let search = parse_command_line(
            r#"{"path":"file","search_terms":["中🙂"],"context_lines":1}"#,
        )
        .expect("unicode literal search");
        let (text, metadata) = read_range(&search, root.path()).expect("unicode search");
        assert_eq!(text, "1: one\r\n2: 中🙂\r\n3: last");
        assert_eq!(metadata["search_matches"], json!([2]));
        assert_eq!(metadata["source_sha256"],
            format!("{:x}", Sha256::digest("one\r\n中🙂\r\nlast".as_bytes())));
    }

    #[test]
    fn literal_search_merges_overlaps_and_keeps_sparse_labels() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("file"), "one\ntarget\nother\nterm\nfive\nsix\nseven\ntarget")
            .expect("source");
        let search = parse_command_line(
            r#"{"path":"file","search_terms":["target","term"],"context_lines":1}"#,
        )
        .expect("search");
        let (text, metadata) = read_range(&search, root.path()).expect("search read");
        assert_eq!(text, "1: one\n2: target\n3: other\n4: term\n5: five\n7: seven\n8: target");
        assert_eq!(metadata["search_matches"], json!([2, 4, 8]));
        assert_eq!(metadata["end_line"], 8);
        assert_eq!(metadata["at_eof"], true);
        assert_eq!(metadata["ends_with_newline"], false);

        let no_match = parse_command_line(r#"{"path":"file","search_terms":["absent"]}"#)
            .expect("valid search");
        let (text, metadata) = read_range(&no_match, root.path()).expect("successful no match");
        assert!(text.is_empty());
        assert_eq!(metadata["search_matches"], json!([]));
        assert_eq!(metadata["end_line"], 0);
        assert_eq!(metadata["next_line"], serde_json::Value::Null);
        assert_eq!(metadata["truncated"], false);
        assert_eq!(metadata["ends_with_newline"], false);
        fs::write(root.path().join("file"), "").expect("empty source");
        let (text, metadata) = read_range(&no_match, root.path()).expect("empty search");
        assert!(text.is_empty());
        assert_eq!(metadata["total_lines"], 0);
    }

    #[test]
    fn resumed_search_keeps_context_from_a_prior_match_without_repeating_lines() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("file"), "one\nhit\nthree\nfour\nfive\n")
            .expect("source");
        let request = parse_command_line(
            r#"{"path":"file","start_line":3,"search_terms":["hit"],"context_lines":2}"#,
        )
        .expect("resumed search");
        let (text, metadata) = read_range(&request, root.path()).expect("context continuation");
        assert_eq!(text, "3: three\n4: four\n");
        assert_eq!(metadata["search_matches"], json!([]));
        assert_eq!(metadata["end_line"], 4);
        assert_eq!(metadata["next_line"], serde_json::Value::Null);
    }

    #[test]
    fn sparse_search_truncation_resumes_without_repeating_lines() {
        let root = tempfile::tempdir().expect("workspace");
        let content = (1..=501)
            .map(|n| if n % 2 == 0 { "hit\n" } else { "miss\n" })
            .collect::<String>();
        fs::write(root.path().join("file"), &content).expect("source");
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let search = parse_command_line(r#"{"path":"file","search_terms":["hit"]}"#)
            .expect("search");
        let (text, metadata) = read_range(&search, root.path()).expect("bounded search");
        assert_eq!(text.lines().next(), Some("2: hit"));
        assert_eq!(text.lines().count(), super::MAX_LINES);
        assert_eq!(metadata["end_line"], 400);
        assert_eq!(metadata["next_line"], 401);
        assert_eq!(metadata["truncation_reason"], "line_limit");
        let resume = parse_command_line(
            &json!({"path":"file", "search_terms":["hit"],
                "start_line":metadata["next_line"], "expected_sha256":hash}).to_string(),
        )
        .expect("resume");
        let (text, metadata) = read_range(&resume, root.path()).expect("resumed search");
        assert_eq!(text.lines().next(), Some("402: hit"));
        assert_eq!(metadata["search_matches"][0], 402);
        assert_eq!(metadata["end_line"], 500);
        assert_eq!(metadata["next_line"], serde_json::Value::Null);
        fs::write(root.path().join("file"), "changed\n").expect("changed source");
        assert_eq!(read_range(&resume, root.path()).unwrap_err(), "SOURCE_READ_SHA256_MISMATCH");
    }

    #[test]
    fn search_line_numbers_true_matches_omission_across_pages() {
        let root = tempfile::tempdir().expect("workspace");
        let content = (1..=501)
            .map(|n| if n % 2 == 0 { "hit\n" } else { "miss\n" })
            .collect::<String>();
        fs::write(root.path().join("file"), &content).expect("source");
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let mut raw = json!({"path":"file", "search_terms":["hit"]});
        for (start, end, next_line) in [(1, 400, Some(401)), (401, 500, None)] {
            let omitted = parse_command_line(&raw.to_string()).expect("omitted numbering");
            let mut numbered_raw = raw.clone();
            numbered_raw["line_numbers"] = json!(true);
            let numbered = parse_command_line(&numbered_raw.to_string()).expect("explicit numbering");
            let (text, metadata) = read_range(&omitted, root.path()).expect("default search");
            let (numbered_text, numbered_metadata) =
                read_range(&numbered, root.path()).expect("numbered search");
            assert_eq!(numbered_text, text);
            assert_eq!(numbered_metadata, metadata);
            let expected_text = (start..=end)
                .filter(|line| line % 2 == 0)
                .map(|line| format!("{line}: hit\n"))
                .collect::<String>();
            assert_eq!(text, expected_text);
            assert_eq!(metadata["line_numbers"], true);
            assert_eq!(metadata["source_sha256"], hash);
            assert_eq!(metadata["start_line"], start);
            assert_eq!(metadata["end_line"], end);
            assert_eq!(metadata["next_line"], json!(next_line));
            assert_eq!(metadata["truncated"], next_line.is_some());
            assert_eq!(metadata["at_eof"], next_line.is_none());
            if next_line.is_some() {
                assert_eq!(metadata["truncation_reason"], "line_limit");
                raw["start_line"] = metadata["next_line"].clone();
                raw["expected_sha256"] = metadata["source_sha256"].clone();
            } else {
                assert_eq!(metadata["truncation_reason"], serde_json::Value::Null);
            }
        }
    }

    #[test]
    fn search_rejects_false_and_wrong_typed_line_numbers() {
        let false_request = r#"{"path":"file","search_terms":["hit"],"line_numbers":false}"#;
        assert_eq!(
            parse_command_line(false_request).unwrap_err(),
            "SOURCE_READ_RANGE_INVALID"
        );
        for line_numbers in [
            json!("true"),
            json!("false"),
            json!(0),
            json!(1),
            json!([]),
            json!({}),
        ] {
            let raw = json!({
                "path": "file",
                "search_terms": ["hit"],
                "line_numbers": line_numbers,
            });
            let error = parse_command_line(&raw.to_string()).unwrap_err();
            assert!(error.starts_with("SOURCE_READ_ARGUMENTS_INVALID:"), "{error}");
        }
    }

    #[test]
    fn arguments_and_ranges_fail_closed() {
        for raw in [
            json!({"path":"../secret", "start_line":1, "end_line":1}).to_string(),
            json!({"path":"/secret", "start_line":1, "end_line":1}).to_string(),
            json!({"path":"src//main.rs", "start_line":1, "end_line":1}).to_string(),
            json!({"path":"src/main.rs", "start_line":0, "end_line":1}).to_string(),
            json!({"path":"src/main.rs", "start_line":2, "end_line":1}).to_string(),
            json!({"path":"src/main.rs", "start_line":1, "end_line":1, "extra":true}).to_string(),
            json!({"path":"src/main.rs"}).to_string(),
            json!({"path":"src/main.rs", "start_line":1}).to_string(),
            json!({"path":"src/main.rs", "end_line":2, "search_terms":["x"]}).to_string(),
            json!({"path":"src/main.rs", "search_terms":[]}).to_string(),
            json!({"path":"src/main.rs", "search_terms":[""]}).to_string(),
            json!({"path":"src/main.rs", "search_terms":["x"], "context_lines":6}).to_string(),
            json!({"path":"src/main.rs", "start_line":1, "end_line":2, "context_lines":1}).to_string(),
            json!({"path":"src/main.rs", "search_terms":["x"], "extra":1}).to_string(),
            json!({"path":"src/main.rs", "search_terms":["x"], "line_numbers":false}).to_string(),
            json!({"path":"src/../main.rs", "search_terms":["x"]}).to_string(),
            json!({"path":"/etc/passwd", "search_terms":["x"]}).to_string(),
            json!({"path":"src/main.rs", "search_terms":vec!["a"; 17]}).to_string(),
            json!({"path":"src/main.rs", "search_terms":["a".repeat(2049)]}).to_string(),
        ] {
            assert!(parse_command_line(&raw).is_err(), "{raw}");
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn numbered_exact_range_completes_in_one_read_without_growing_search_pages() {
        let root = tempfile::tempdir().expect("workspace");
        let lines: Vec<String> = (1..=149)
            .map(|number| format!("source-{number:03}: {}\n", "x".repeat(41)))
            .collect();
        let content = lines.concat();
        fs::write(root.path().join("lines.txt"), &content).expect("source file");
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let expected: String = (24..=149)
            .map(|number| format!("{number}: {}", lines[number - 1]))
            .collect();
        assert_eq!(expected.len(), 7358);

        let mut legacy_pages = Vec::new();
        let mut page = String::new();
        for number in 24..=149 {
            if !super::append_line(
                &mut page, &lines[number - 1], number, true, super::MAX_TEXT_BYTES,
            ).expect("legacy bound") {
                legacy_pages.push(std::mem::take(&mut page));
                assert!(super::append_line(
                    &mut page, &lines[number - 1], number, true, super::MAX_TEXT_BYTES,
                ).expect("complete legacy line"));
            }
        }
        legacy_pages.push(page);
        assert_eq!(legacy_pages.len(), 2);
        assert_eq!(legacy_pages.concat(), expected);

        let canonical = root.path().canonicalize().expect("physical workspace");
        let admitted_root = Arc::new(open_admitted_root(&canonical).expect("admitted root"));
        let ctx = ToolContext::new(canonical)
            .with_source_read_root(Some(admitted_root.clone()))
            .with_call_id("range-7kb".to_string());
        let output = SourceReadHandler.handle(ToolCall {
            tool_name: "source_read".to_string(),
            call_id: "range-7kb".to_string(),
            payload: ToolPayload::Function { arguments: json!({
                "path":"lines.txt", "start_line":24, "end_line":149,
                "line_numbers":true, "expected_sha256":hash,
            }) },
        }, ctx).await.expect("one complete exact-range read");
        assert_eq!(output.success, Some(true));
        assert_eq!(output.body["stdout"], expected);
        assert_eq!(output.body["source_sha256"], hash);
        assert_eq!(output.body["start_line"], 24);
        assert_eq!(output.body["end_line"], 149);
        assert_eq!(output.body["requested_end_line"], 149);
        assert_eq!(output.body["line_numbers"], true);
        assert_eq!(output.body["truncated"], false);
        assert_eq!(output.body["next_line"], serde_json::Value::Null);
        assert_eq!(output.body["at_eof"], true);
        assert!(!super::result_too_large(&output.body, super::MAX_RANGE_RESULT_BYTES));
        assert_eq!(output.body["terminal_receipt"]["terminal_state"], "completed");

        let mut arguments = json!({"path":"lines.txt", "search_terms":["source-"],
            "start_line":24, "expected_sha256":hash});
        let mut end = 23;
        for (index, expected_page) in legacy_pages.iter().enumerate() {
            let request = parse_command_line(&arguments.to_string()).expect("search page");
            let (text, metadata) = read_range_from_root(&request, &admitted_root)
                .expect("unchanged compact search");
            assert_eq!(&text, expected_page);
            assert!(text.len() <= super::MAX_TEXT_BYTES);
            assert!(serde_json::to_string(&text).unwrap().len() <= super::MAX_TEXT_BYTES);
            end += text.lines().count();
            assert_eq!(metadata["end_line"], end);
            assert_eq!(metadata["source_sha256"], hash);
            if index == 0 {
                assert_eq!(metadata["truncation_reason"], "response_limit");
                assert_eq!(metadata["next_line"], end + 1);
                arguments["start_line"] = metadata["next_line"].clone();
            } else {
                assert_eq!(metadata["truncated"], false);
                assert_eq!(metadata["next_line"], serde_json::Value::Null);
                assert_eq!(end, 149);
            }
        }
    }

    #[test]
    fn advertised_limits_match_parsed_request_budgets() {
        let description = super::output_limits_description();
        assert!(description.contains(&format!(
            "At most {} complete lines in both modes.",
            super::MAX_LINES,
        )));
        for (mode, arguments) in [
            (
                "Range",
                json!({"path":"limits.txt", "start_line":1, "end_line":2}),
            ),
            ("Search", json!({"path":"limits.txt", "search_terms":["x"]})),
            ("JSON projection", json!({"path":"limits.txt", "json_pointer":"/x"})),
        ] {
            let request = parse_command_line(&arguments.to_string()).expect("valid request");
            let (max_text_bytes, max_result_bytes) = request.byte_limits();
            assert!(
                description.contains(&format!(
                    "{mode} reads: {max_text_bytes} text bytes, {max_result_bytes} receipt-inclusive serialized result bytes.",
                )),
                "{mode} limits disagree with parsed request: {description}",
            );
        }
    }

    #[test]
    fn request_budgets_bound_raw_and_json_escaped_complete_lines() {
        let root = tempfile::tempdir().expect("workspace");
        for (arguments, limits) in [
            (json!({"path":"limits.txt", "start_line":1, "end_line":300,
                "line_numbers":true}), (12288, 16384)),
            (json!({"path":"limits.txt", "search_terms":["x"]}), (6144, 8192)),
        ] {
            let request = parse_command_line(&arguments.to_string()).expect("valid request");
            assert_eq!(request.byte_limits(), limits);
            let (max_text_bytes, max_result_bytes) = limits;
            assert!(!super::result_too_large(
                &json!("x".repeat(max_result_bytes - 2)), max_result_bytes,
            ));
            assert!(super::result_too_large(
                &json!("x".repeat(max_result_bytes - 1)), max_result_bytes,
            ));

            fs::write(root.path().join("limits.txt"), "x".repeat(max_text_bytes - 5))
                .expect("boundary line");
            let (text, _) = read_range(&request, root.path()).expect("inclusive escaped bound");
            assert_eq!(text.len(), max_text_bytes - 2);
            assert_eq!(serde_json::to_string(&text).unwrap().len(), max_text_bytes);

            for line in [
                format!("x{}\n", "x".repeat(90)),
                format!("x{}\n", "\"".repeat(90)),
            ] {
                let content = line.repeat(300);
                fs::write(root.path().join("limits.txt"), &content).expect("source file");
                let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
                let (text, metadata) = read_range(&request, root.path()).expect("bounded page");
                let end = metadata["end_line"].as_u64().unwrap() as usize;
                assert!(end > 0 && end < super::MAX_LINES);
                let expected: String = (1..=end).map(|n| format!("{n}: {line}")).collect();
                assert_eq!(text, expected);
                assert!(text.len() <= max_text_bytes);
                assert!(serde_json::to_string(&text).unwrap().len() <= max_text_bytes);
                let candidate = format!("{text}{}: {line}", end + 1);
                assert!(candidate.len() > max_text_bytes
                    || serde_json::to_string(&candidate).unwrap().len() > max_text_bytes);
                assert_eq!(metadata["truncation_reason"], "response_limit");
                assert_eq!(metadata["next_line"], end + 1);
                assert_eq!(metadata["source_sha256"], hash);
                let mut resume = request.clone();
                resume.start_line = Some(end + 1);
                resume.expected_sha256 = Some(hash);
                let (next, metadata) = read_range(&resume, root.path()).expect("complete-line resume");
                assert!(next.starts_with(&format!("{}: {line}", end + 1)));
                assert_eq!(metadata["start_line"], end + 1);
            }

            for line in [
                "x".repeat(max_text_bytes + 1),
                format!("x{}", "\"".repeat(max_text_bytes / 2)),
            ] {
                fs::write(root.path().join("limits.txt"), line).expect("oversized line");
                assert_eq!(read_range(&request, root.path()).unwrap_err(), "SOURCE_READ_LINE_TOO_LONG");
            }
        }
    }

    #[test]
    fn never_returns_partial_or_oversized_content() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(
            root.path().join("line.txt"),
            "x".repeat(super::MAX_RANGE_TEXT_BYTES + 1),
        )
        .expect("long line");
        let request = parse_command_line(r#"{"path":"line.txt","start_line":1,"end_line":1}"#)
            .expect("request");
        assert_eq!(
            read_range(&request, root.path()).unwrap_err(),
            "SOURCE_READ_LINE_TOO_LONG"
        );
        fs::write(root.path().join("line.txt"), "one\ntwo\n").expect("short file");
        let request = parse_command_line(r#"{"path":"line.txt","start_line":2,"end_line":3}"#)
            .expect("request");
        let (text, metadata) = read_range(&request, root.path()).expect("clipped at EOF");
        assert_eq!(text, "two\n");
        assert_eq!(metadata["end_line"], 2);
        assert_eq!(metadata["requested_end_line"], 3);
        assert_eq!(metadata["truncation_reason"], "end_of_file");
        assert_eq!(metadata["next_line"], serde_json::Value::Null);
        assert_eq!(metadata["at_eof"], true);
        let request = parse_command_line(r#"{"path":"line.txt","start_line":3,"end_line":3}"#)
            .expect("request");
        assert_eq!(
            read_range(&request, root.path()).unwrap_err(),
            "SOURCE_READ_RANGE_OUT_OF_BOUNDS"
        );
        fs::write(root.path().join("line.txt"), vec![b'x'; 1024 * 1024 + 1])
            .expect("oversized file");
        let request = parse_command_line(r#"{"path":"line.txt","start_line":1,"end_line":1}"#)
            .expect("request");
        assert_eq!(
            target_path(root.path(), &request).unwrap_err(),
            "SOURCE_READ_FILE_TOO_LARGE"
        );
    }

    #[test]
    fn oversized_range_returns_complete_lines_and_a_resume_position() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(
            root.path().join("lines.txt"),
            "x".repeat(super::MAX_RANGE_TEXT_BYTES + 1),
        )
        .expect("source file");
        let request = parse_command_line(r#"{"path":"lines.txt","start_line":1,"end_line":1}"#)
            .expect("request");
        assert_eq!(
            read_range(&request, root.path()).unwrap_err(),
            "SOURCE_READ_LINE_TOO_LONG"
        );

        let content = "x".repeat(160);
        let content = format!("{}\n", content).repeat(100);
        fs::write(root.path().join("lines.txt"), &content).expect("source file");
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let request = parse_command_line(r#"{"path":"lines.txt","start_line":1,"end_line":100}"#)
            .expect("request");
        let (text, metadata) = read_range(&request, root.path()).expect("bounded read");
        assert_eq!(
            text.lines().count(),
            metadata["end_line"].as_u64().unwrap() as usize
        );
        assert!(text.len() <= super::MAX_RANGE_TEXT_BYTES);
        assert!(serde_json::to_string(&text).unwrap().len() <= super::MAX_RANGE_TEXT_BYTES);
        assert!(metadata["end_line"].as_u64().unwrap() < 100);
        assert_eq!(metadata["truncated"], true);
        assert_eq!(metadata["truncation_reason"], "response_limit");
        assert_eq!(
            metadata["next_line"].as_u64().unwrap(),
            metadata["end_line"].as_u64().unwrap() + 1
        );
        assert_eq!(metadata["at_eof"], false);
        assert_eq!(metadata["source_sha256"], hash);
        let resume = parse_command_line(
            &json!({"path":"lines.txt", "start_line":metadata["next_line"],
                "end_line":100, "expected_sha256":hash}).to_string(),
        )
        .expect("resume");
        let (next, metadata) = read_range(&resume, root.path()).expect("resumed range");
        assert_eq!(text + &next, content);
        assert_eq!(metadata["end_line"], 100);
        assert_eq!(metadata["next_line"], serde_json::Value::Null);
        assert_eq!(metadata["source_sha256"], hash);
    }

    #[test]
    fn long_ordered_ranges_are_clipped_to_200_lines() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("lines.txt"), "x\n".repeat(301)).expect("source file");

        let request = parse_command_line(r#"{"path":"lines.txt","start_line":1,"end_line":201}"#)
            .expect("201-line range is valid");
        let (text, metadata) = read_range(&request, root.path()).expect("bounded read");
        assert_eq!(text, "x\n".repeat(200));
        assert_eq!(metadata["end_line"], 200);
        assert_eq!(metadata["requested_end_line"], 201);
        assert_eq!(metadata["truncated"], true);
        assert_eq!(metadata["truncation_reason"], "line_limit");
        assert_eq!(metadata["next_line"], 201);
        assert_eq!(metadata["at_eof"], false);

        let request = parse_command_line(
            &json!({"path":"lines.txt", "start_line":51, "end_line":usize::MAX}).to_string(),
        )
        .expect("large end line is valid");
        let (text, metadata) = read_range(&request, root.path()).expect("bounded read");
        assert_eq!(text, "x\n".repeat(200));
        assert_eq!(metadata["end_line"], 250);
        assert_eq!(metadata["requested_end_line"], usize::MAX as u64);
        assert_eq!(metadata["truncation_reason"], "line_limit");
        assert_eq!(metadata["next_line"], 251);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn receipt_bearing_response_stays_bounded_and_resumable() {
        for search in [false, true] {
            let root = tempfile::tempdir().expect("workspace");
            // These valid, short filesystem components expand in the JSON envelope.
            let long_path = format!(
                "{}/{}/{}.txt",
                "\u{1}".repeat(190), "\u{2}".repeat(190), "\u{3}".repeat(125)
            );
            let source = root.path().join(&long_path);
            fs::create_dir_all(source.parent().expect("source parent")).expect("source dirs");
            let line = format!("{}\n", "x".repeat(119));
            let content = line.repeat(200);
            fs::write(&source, &content).expect("source file");
            let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
            let canonical = root.path().canonicalize().expect("physical workspace");
            let admitted_root = Arc::new(open_admitted_root(&canonical).expect("admitted root"));
            let session_dir = canonical.join("s".repeat(180)).join("t".repeat(180));
            fs::create_dir_all(&session_dir).expect("session directory");
            let ctx = ToolContext::new(session_dir)
                .with_source_read_root(Some(admitted_root.clone()))
                .with_call_id("r".repeat(220));
            let arguments = if search {
                json!({"path":long_path, "search_terms":["x"]})
            } else {
                json!({"path":long_path, "start_line":1, "end_line":500})
            };
            let request = parse_command_line(&arguments.to_string()).expect("valid request");
            let (max_text_bytes, max_result_bytes) = request.byte_limits();
            let raw = execute(&request, &admitted_root);
            assert!(raw.success);
            assert!(raw.stdout.len() <= max_text_bytes);
            assert!(serde_json::to_string(&raw.stdout).unwrap().len() <= max_text_bytes);
            let oversized = shell_executor::preview_in_process_terminal_response(
                &ctx, &raw, super::TERMINATION_ORIGIN,
            ).expect("preview");
            assert!(super::result_too_large(&oversized, max_result_bytes),
                "preview bytes: {}", serde_json::to_vec(&oversized).unwrap().len());

            let output = SourceReadHandler.handle(ToolCall {
                tool_name: "source_read".to_string(),
                call_id: "r".repeat(220),
                payload: ToolPayload::Function { arguments: arguments.clone() },
            }, ctx).await.expect("bounded source read");
            assert_eq!(output.success, Some(true));
            assert!(!super::result_too_large(&output.body, max_result_bytes));
            assert_eq!(output.body["terminal_receipt"]["terminal_state"], "completed");
            assert_eq!(output.body["terminal_receipt"]["exit_code"], 0);
            let end = output.body["end_line"].as_u64().expect("returned end line");
            assert!(end < raw.output["end_line"].as_u64().unwrap());
            assert!(end < 200);
            assert_eq!(output.body["next_line"], end + 1);
            assert_eq!(output.body["truncation_reason"], "response_limit");
            assert_eq!(output.body["source_sha256"], hash);
            let expected: String = (1..=200).map(|n| {
                if search { format!("{n}: {line}") } else { line.clone() }
            }).collect();
            let printed = output.body["stdout"].as_str().unwrap();
            let expected_page: String = expected.split_inclusive('\n').take(end as usize).collect();
            assert_eq!(printed, expected_page);
            let receipt_path = output.body["terminal_receipt_path"].as_str().expect("receipt path");
            let durable: serde_json::Value = serde_json::from_slice(
                &fs::read(receipt_path).expect("durable receipt"),
            ).expect("receipt JSON");
            assert_eq!(durable, output.body["terminal_receipt"]);

            let mut assembled = printed.to_string();
            let mut resume = arguments;
            resume["start_line"] = json!(end + 1);
            resume["expected_sha256"] = json!(hash);
            loop {
                let request = parse_command_line(&resume.to_string()).expect("resume request");
                let (next, metadata) = read_range_from_root(&request, &admitted_root)
                    .expect("complete-line resume");
                assert_eq!(metadata["source_sha256"], hash);
                assembled.push_str(&next);
                if metadata["next_line"].is_null() { break; }
                resume["start_line"] = metadata["next_line"].clone();
            }
            assert_eq!(assembled, expected);
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn receipt_fitting_keeps_sparse_physical_line_identity() {
        let root = tempfile::tempdir().expect("workspace");
        let long_path = format!(
            "{}/{}/{}.txt", "a".repeat(190), "b".repeat(190), "c".repeat(125)
        );
        let source = root.path().join(&long_path);
        fs::create_dir_all(source.parent().expect("parent")).expect("dirs");
        let content = (1..=250).map(|n| {
            if n % 2 == 0 { format!("needle{}\n", "x".repeat(90)) }
            else { "skip\n".to_string() }
        }).collect::<String>();
        fs::write(&source, &content).expect("source");
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let canonical = root.path().canonicalize().expect("canonical workspace");
        let admitted_root = Arc::new(open_admitted_root(&canonical).expect("admitted root"));
        let session_dir = canonical.join("s".repeat(180)).join("t".repeat(180));
        fs::create_dir_all(&session_dir).expect("session directory");
        let ctx = ToolContext::new(session_dir)
            .with_source_read_root(Some(admitted_root.clone()))
            .with_call_id("r".repeat(220));
        let arguments = json!({"path":long_path, "search_terms":["needle"]});
        let request = parse_command_line(&arguments.to_string()).expect("valid search");
        let raw = execute(&request, &admitted_root);
        assert!(raw.success);
        let oversized = shell_executor::preview_in_process_terminal_response(
            &ctx, &raw, super::TERMINATION_ORIGIN,
        ).expect("preview");
        assert!(super::result_too_large(&oversized, super::MAX_RESULT_BYTES));
        let output = SourceReadHandler.handle(ToolCall {
            tool_name: "source_read".to_string(),
            call_id: "r".repeat(220),
            payload: ToolPayload::Function { arguments },
        }, ctx).await.expect("bounded search");
        assert_eq!(output.success, Some(true));
        assert!(!super::result_too_large(&output.body, super::MAX_RESULT_BYTES));
        let end = output.body["end_line"].as_u64().expect("physical end");
        assert_eq!(end % 2, 0);
        assert!(end < raw.output["end_line"].as_u64().unwrap());
        assert_eq!(output.body["next_line"], end + 1);
        assert_eq!(output.body["truncation_reason"], "response_limit");
        let printed = output.body["stdout"].as_str().expect("numbered stdout");
        assert!(printed.starts_with("2: needle"));
        assert!(printed.ends_with(&format!("{end}: needle{}\n", "x".repeat(90))));
        assert_eq!(output.body["search_matches"].as_array().unwrap().last(), Some(&json!(end)));
        let resume = parse_command_line(&json!({"path":long_path, "search_terms":["needle"],
            "start_line":end + 1, "expected_sha256":hash}).to_string()).expect("resume");
        let (next, _) = read_range_from_root(&resume, &admitted_root).expect("resumed search");
        assert!(next.starts_with(&format!("{}: needle", end + 2)));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn line_that_cannot_fit_envelope_has_matching_failed_receipt() {
        for search in [false, true] {
            let root = tempfile::tempdir().expect("workspace");
            let long_path = format!(
                "{}/{}/{}.txt",
                "\u{1}".repeat(190), "\u{2}".repeat(190), "\u{3}".repeat(125)
            );
            let source = root.path().join(&long_path);
            fs::create_dir_all(source.parent().expect("source parent")).expect("source dirs");
            let arguments = if search {
                json!({"path":long_path, "search_terms":["x"]})
            } else {
                json!({"path":long_path, "start_line":1, "end_line":1})
            };
            let request = parse_command_line(&arguments.to_string()).expect("valid request");
            let (max_text_bytes, max_result_bytes) = request.byte_limits();
            fs::write(source, format!("{}\n", "x".repeat(max_text_bytes - 32)))
                .expect("source file");
            let canonical = root.path().canonicalize().expect("physical workspace");
            let admitted_root = Arc::new(open_admitted_root(&canonical).expect("admitted root"));
            let session_dir = canonical.join("s".repeat(180)).join("t".repeat(180));
            fs::create_dir_all(&session_dir).expect("session directory");
            let ctx = ToolContext::new(session_dir)
                .with_source_read_root(Some(admitted_root.clone()))
                .with_call_id("r".repeat(220));
            let raw = execute(&request, &admitted_root);
            assert!(raw.success, "the complete line must fit its selected text budget");
            let oversized = shell_executor::preview_in_process_terminal_response(
                &ctx, &raw, super::TERMINATION_ORIGIN,
            ).expect("preview");
            assert!(super::result_too_large(&oversized, max_result_bytes));
            let output = SourceReadHandler.handle(ToolCall {
                tool_name: "source_read".to_string(),
                call_id: "r".repeat(220),
                payload: ToolPayload::Function { arguments },
            }, ctx).await.expect("typed result");
            assert_eq!(output.success, Some(false));
            assert!(!super::result_too_large(&output.body, max_result_bytes));
            assert_eq!(output.body["error_type"], "SOURCE_READ_RESULT_TOO_LARGE");
            assert_eq!(output.body["terminal_receipt"]["terminal_state"], "failed");
            assert_eq!(output.body["terminal_receipt"]["exit_code"], 1);
            let receipt_path = output.body["terminal_receipt_path"].as_str().expect("receipt path");
            let durable: serde_json::Value = serde_json::from_slice(
                &fs::read(receipt_path).expect("durable receipt"),
            ).expect("receipt JSON");
            assert_eq!(durable, output.body["terminal_receipt"]);
        }
    }

    #[test]
    fn serialized_result_size_is_checked_at_each_request_budget() {
        for arguments in [
            json!({"path":"file", "start_line":1, "end_line":1}),
            json!({"path":"file", "search_terms":["x"]}),
        ] {
            let request = parse_command_line(&arguments.to_string()).expect("valid request");
            let (_, max_result_bytes) = request.byte_limits();
            for (serialized_bytes, too_large) in [
                (max_result_bytes - 1, false),
                (max_result_bytes, false),
                (max_result_bytes + 1, true),
            ] {
                let value = json!("x".repeat(serialized_bytes - 2));
                assert_eq!(
                    serde_json::to_vec(&value).expect("serialized result").len(),
                    serialized_bytes,
                );
                assert_eq!(super::result_too_large(&value, max_result_bytes), too_large);
            }
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn oversized_receipt_call_id_is_rejected_for_each_request_budget() {
        for arguments in [
            json!({"path":"file", "start_line":1, "end_line":1}),
            json!({"path":"file", "search_terms":["x"]}),
        ] {
            let root = tempfile::tempdir().expect("workspace");
            fs::write(root.path().join("file"), "x\n").expect("source file");
            let request = parse_command_line(&arguments.to_string()).expect("valid request");
            let (_, max_result_bytes) = request.byte_limits();
            let canonical = root.path().canonicalize().expect("physical workspace");
            let admitted_root = Arc::new(open_admitted_root(&canonical).expect("admitted root"));
            let call_id = "r".repeat(max_result_bytes);
            let ctx = ToolContext::new(canonical)
                .with_source_read_root(Some(admitted_root))
                .with_call_id(call_id.clone());
            let result = SourceReadHandler.handle(ToolCall {
                tool_name: "source_read".to_string(), call_id,
                payload: ToolPayload::Function { arguments },
            }, ctx).await;
            assert!(matches!(
                result,
                Err(super::ToolError::RespondToModel(error))
                    if error == "SOURCE_READ_RECEIPT_INVALID: receipt name must be one bounded ASCII filename component"
            ));
        }
    }

    #[cfg(unix)]
    #[test]
    fn symlink_to_in_scope_file_is_denied() {
        use std::os::unix::fs::symlink;
        let root = tempfile::tempdir().expect("workspace");
        fs::create_dir(root.path().join("src")).expect("source directory");
        fs::write(root.path().join("src/real.txt"), "allowed\n").expect("source file");
        symlink("real.txt", root.path().join("src/alias.txt")).expect("symlink");
        let request = parse_command_line(r#"{"path":"src/alias.txt","start_line":1,"end_line":1}"#)
            .expect("request");
        assert_eq!(
            target_path(root.path(), &request).unwrap_err(),
            "SOURCE_READ_SYMLINK_DENIED"
        );
        assert_eq!(
            read_range(&request, root.path()).unwrap_err(),
            "SOURCE_READ_SYMLINK_DENIED"
        );
        let admitted_root =
            open_admitted_root(&root.path().canonicalize().expect("canonical root"))
                .expect("admitted root");
        assert_eq!(
            read_range_from_root(&request, &admitted_root).unwrap_err(),
            "SOURCE_READ_TARGET_INVALID"
        );
        let search = parse_command_line(
            r#"{"path":"src/alias.txt","search_terms":["allowed"]}"#,
        )
        .expect("valid search syntax");
        assert_eq!(target_path(root.path(), &search).unwrap_err(), "SOURCE_READ_SYMLINK_DENIED");
        assert_eq!(read_range_from_root(&search, &admitted_root).unwrap_err(), "SOURCE_READ_TARGET_INVALID");
    }

    #[cfg(unix)]
    #[test]
    fn root_fd_stays_bound_when_an_ancestor_is_replaced_with_a_symlink() {
        use std::os::unix::fs::symlink;

        let base = tempfile::tempdir().expect("base");
        let parent = base.path().join("parent");
        let workspace = parent.join("workspace");
        fs::create_dir_all(workspace.join("src")).expect("admitted source directory");
        fs::write(workspace.join("src/main.rs"), "admitted\n").expect("admitted source");
        let admitted_workspace = workspace.canonicalize().expect("canonical workspace");
        let admitted_root = open_admitted_root(&admitted_workspace).expect("admitted root fd");

        let replacement = base.path().join("replacement");
        fs::create_dir_all(replacement.join("workspace/src"))
            .expect("replacement source directory");
        fs::write(replacement.join("workspace/src/main.rs"), "outside\n")
            .expect("replacement source");
        fs::rename(&parent, base.path().join("former-parent")).expect("move admitted parent");
        symlink(&replacement, &parent).expect("replace parent with symlink");

        let request = parse_command_line(r#"{"path":"src/main.rs","start_line":1,"end_line":1}"#)
            .expect("request");
        let (text, _) = read_range_from_root(&request, &admitted_root).expect("bound read");
        assert_eq!(text, "admitted\n");
        assert_eq!(
            open_admitted_root(&admitted_workspace).unwrap_err(),
            "SOURCE_READ_WORKSPACE_INVALID"
        );
    }

    #[test]
    fn json_projection_reads_tiny_field_from_oversized_single_line() {
        let root = tempfile::tempdir().expect("workspace");
        let content = json!({
            "padding": "x".repeat(super::MAX_RANGE_TEXT_BYTES + 1),
            "answer": "中文 ✅",
        }).to_string();
        assert!(content.len() > 12 * 1024);
        assert!(!content.contains('\n'));
        fs::write(root.path().join("capsule.json"), &content).expect("capsule");
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        for arguments in [
            json!({"path":"capsule.json", "start_line":1, "end_line":1}),
            json!({"path":"capsule.json", "search_terms":["answer"]}),
        ] {
            let request = parse_command_line(&arguments.to_string()).expect("line request");
            assert_eq!(read_range(&request, root.path()).unwrap_err(), "SOURCE_READ_LINE_TOO_LONG");
        }
        let request = parse_command_line(&json!({
            "path":"capsule.json", "json_pointer":"/answer", "expected_sha256":hash.to_uppercase(),
        }).to_string()).expect("projection request");
        assert_eq!(request.byte_limits(), (super::MAX_RANGE_TEXT_BYTES, super::MAX_RANGE_RESULT_BYTES));
        let (text, metadata) = read_range(&request, root.path()).expect("tiny projection");
        assert_eq!(text, json!("中文 ✅").to_string());
        assert_eq!(metadata, json!({
            "mode":"json_projection", "path":"capsule.json", "json_pointer":"/answer",
            "source_sha256":hash, "file_bytes":content.len(), "truncated":false,
        }));
    }

    #[test]
    fn json_projection_preserves_nested_values_and_pointer_escapes() {
        let root = tempfile::tempdir().expect("workspace");
        let values = json!([null, false, 0, "中文 🦀", {"key":[1, 2]}]);
        let item = json!({"a/b":{"~key":values}});
        let document = json!({"nested":{"items":[item]}, "~1":"literal ~1", "":"empty key", "中文":42});
        fs::write(root.path().join("values.json"), document.to_string()).expect("JSON");
        for (pointer, expected) in [
            ("", document.clone()),
            ("/nested/items", json!([item])),
            ("/nested/items/0", item),
            ("/nested/items/0/a~1b/~0key", values),
            ("/nested/items/0/a~1b/~0key/0", json!(null)),
            ("/nested/items/0/a~1b/~0key/1", json!(false)),
            ("/nested/items/0/a~1b/~0key/2", json!(0)),
            ("/nested/items/0/a~1b/~0key/3", json!("中文 🦀")),
            ("/nested/items/0/a~1b/~0key/4/key/1", json!(2)),
            ("/~01", json!("literal ~1")),
            ("/", json!("empty key")),
            ("/中文", json!(42)),
        ] {
            let request = parse_command_line(&json!({"path":"values.json", "json_pointer":pointer}).to_string())
                .expect("pointer request");
            let (text, metadata) = read_range(&request, root.path()).expect("selected value");
            assert_eq!(serde_json::from_str::<serde_json::Value>(&text).unwrap(), expected, "{pointer}");
            assert_eq!(metadata["json_pointer"], pointer);
        }
        for scalar in [json!(null), json!(false), json!(0), json!("Unicode 中文"), json!([null, false, 0])] {
            fs::write(root.path().join("values.json"), scalar.to_string()).expect("root JSON");
            let request = parse_command_line(r#"{"path":"values.json","json_pointer":""}"#).unwrap();
            assert_eq!(read_range(&request, root.path()).unwrap().0, scalar.to_string());
        }
    }

    #[test]
    fn json_projection_rejects_invalid_pointers_and_conflicting_modes() {
        use super::parse_command_line_typed;
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("values.json"), r#"{"value":null}"#).expect("JSON");
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        let mut invalid = Vec::new();
        for pointer in ["value", "#/value", "/value~", "/value~2", "/~x", "/~00~"] {
            invalid.push((json!({"path":"values.json", "json_pointer":pointer}), "SOURCE_READ_JSON_POINTER_INVALID"));
        }
        for (key, value) in [
            ("start_line", json!(1)), ("start_line", json!(0)), ("end_line", json!(1)),
            ("line_numbers", json!(false)), ("line_numbers", json!(true)),
            ("search_terms", json!([])), ("search_terms", json!(["value"])), ("context_lines", json!(0)),
        ] {
            let mut arguments = json!({"path":"values.json", "json_pointer":"/value"});
            arguments[key] = value;
            invalid.push((arguments, "SOURCE_READ_MODE_CONFLICT"));
        }
        for (arguments, expected) in invalid {
            let error = parse_command_line_typed(&arguments.to_string()).unwrap_err();
            assert_eq!(error.to_string(), expected);
            assert_eq!(error.rejection_kind(), None);
            // The owning validator also rejects a direct typed handler request.
            let request = serde_json::from_value(arguments).unwrap();
            let response = execute(&request, &admitted);
            assert!(!response.success);
            assert!(response.stdout.is_empty());
            assert_eq!(response.stderr, expected);
            assert_eq!(response.output, json!({"error_type":expected}));
        }
        for pointer in [json!(null), json!(false), json!(0), json!([])] {
            let error = parse_command_line_typed(&json!({"path":"values.json", "json_pointer":pointer}).to_string())
                .unwrap_err();
            assert!(matches!(error, super::SourceReadParseError::Json(_)));
        }
    }

    #[test]
    fn json_projection_errors_do_not_expose_input_or_confuse_null_with_absence() {
        let root = tempfile::tempdir().expect("workspace");
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/value"}"#).unwrap();
        for content in [
            b"".as_slice(), b"{\"secret\":\"do-not-leak\",".as_slice(),
            b"{\"value\":null} trailing".as_slice(), b"true false".as_slice(),
            b"{\"value\":\"\xff\"}".as_slice(),
        ] {
            fs::write(root.path().join("values.json"), content).expect("invalid JSON");
            let response = execute(&request, &admitted);
            assert!(!response.success);
            assert_eq!(response.exit_code, 1);
            assert!(response.stdout.is_empty());
            assert_eq!(response.stderr, "SOURCE_READ_JSON_INVALID");
            assert_eq!(response.output, json!({"error_type":"SOURCE_READ_JSON_INVALID"}));
        }
        fs::write(root.path().join("values.json"), r#"{"value":null,"array":[0],"secret":"do-not-leak"}"#).unwrap();
        assert_eq!(execute(&request, &admitted).stdout, "null");
        for pointer in ["/missing", "/value/child", "/array/1", "/array/-", "/array/01", "/array/x"] {
            let request = parse_command_line(&json!({"path":"values.json", "json_pointer":pointer}).to_string()).unwrap();
            let response = execute(&request, &admitted);
            assert!(!response.success);
            assert!(response.stdout.is_empty());
            assert_eq!(response.stderr, "SOURCE_READ_JSON_POINTER_NOT_FOUND");
            assert_eq!(response.output, json!({"error_type":"SOURCE_READ_JSON_POINTER_NOT_FOUND"}));
        }
    }

    #[test]
    fn json_projection_checks_source_sha_before_parsing_or_selection() {
        let root = tempfile::tempdir().expect("workspace");
        let source = root.path().join("values.json");
        let content = " { \"value\" : 0 } \n";
        fs::write(&source, content).unwrap();
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let request = parse_command_line(&json!({
            "path":"values.json", "json_pointer":"/value", "expected_sha256":hash,
        }).to_string()).unwrap();
        let (text, metadata) = read_range(&request, root.path()).unwrap();
        assert_eq!(text, "0");
        assert_eq!(metadata["source_sha256"], hash);
        for changed in [r#"{"value":false}"#, "invalid JSON: do-not-leak"] {
            fs::write(&source, changed).unwrap();
            assert_eq!(read_range(&request, root.path()).unwrap_err(), "SOURCE_READ_SHA256_MISMATCH");
        }
        assert_eq!(parse_command_line(r#"{"path":"values.json","json_pointer":"","expected_sha256":"bad"}"#)
            .unwrap_err(), "SOURCE_READ_SHA256_INVALID");
    }

    #[test]
    fn json_projection_output_limits_are_atomic_and_count_escaped_bytes() {
        let root = tempfile::tempdir().expect("workspace");
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        for value in [
            "x".repeat(super::MAX_RANGE_TEXT_BYTES + 1),
            "\"".repeat(super::MAX_RANGE_TEXT_BYTES / 3),
            "界".repeat(super::MAX_RANGE_TEXT_BYTES / 3),
        ] {
            fs::write(root.path().join("values.json"), json!({"value":value, "secret":"do-not-leak"}).to_string()).unwrap();
            for pointer in ["/value", ""] {
                let request = parse_command_line(&json!({"path":"values.json", "json_pointer":pointer}).to_string()).unwrap();
                let response = execute(&request, &admitted);
                assert!(!response.success);
                assert!(response.stdout.is_empty());
                assert_eq!(response.stderr, "SOURCE_READ_JSON_VALUE_TOO_LARGE");
                assert_eq!(response.output, json!({"error_type":"SOURCE_READ_JSON_VALUE_TOO_LARGE"}));
            }
        }
        let value = "x".repeat(super::MAX_RANGE_TEXT_BYTES - 6);
        fs::write(root.path().join("values.json"), json!(value).to_string()).unwrap();
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":""}"#).unwrap();
        let (text, _) = read_range_from_root(&request, &admitted).expect("exact escaped text budget");
        assert_eq!(serde_json::to_string(&text).unwrap().len(), super::MAX_RANGE_TEXT_BYTES);
        fs::write(root.path().join("values.json"), json!(format!("{value}x")).to_string()).unwrap();
        assert_eq!(read_range_from_root(&request, &admitted).unwrap_err(), "SOURCE_READ_JSON_VALUE_TOO_LARGE");
    }

    #[test]
    fn json_projection_preserves_one_mib_file_bound() {
        let root = tempfile::tempdir().expect("workspace");
        let mut content = json!({"tiny":false,"padding":""}).to_string();
        let padding = "x".repeat(super::MAX_FILE_BYTES as usize - content.len());
        content = json!({"tiny":false,"padding":padding}).to_string();
        assert_eq!(content.len(), super::MAX_FILE_BYTES as usize);
        fs::write(root.path().join("large.json"), &content).unwrap();
        let request = parse_command_line(r#"{"path":"large.json","json_pointer":"/tiny"}"#).unwrap();
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        assert_eq!(read_range_from_root(&request, &admitted).unwrap().0, "false");
        content.push(' ');
        fs::write(root.path().join("large.json"), content).unwrap();
        assert_eq!(read_range_from_root(&request, &admitted).unwrap_err(), "SOURCE_READ_FILE_TOO_LARGE");
    }

    #[cfg(unix)]
    #[test]
    fn json_projection_keeps_literal_paths_and_no_follow_admission() {
        use std::os::unix::fs::symlink;
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("$HOME.json"), r#"{"value":false}"#).unwrap();
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        let request = parse_command_line(r#"{"path":"$HOME.json","json_pointer":"/value"}"#).unwrap();
        assert_eq!(read_range_from_root(&request, &admitted).unwrap().0, "false");
        symlink("$HOME.json", root.path().join("alias.json")).unwrap();
        let request = parse_command_line(r#"{"path":"alias.json","json_pointer":"/value"}"#).unwrap();
        assert_eq!(target_path(root.path(), &request).unwrap_err(), "SOURCE_READ_SYMLINK_DENIED");
        assert_eq!(read_range_from_root(&request, &admitted).unwrap_err(), "SOURCE_READ_TARGET_INVALID");
        for path in ["../values.json", "/values.json", "src//values.json", "src/./values.json", "src\\values.json"] {
            assert_eq!(parse_command_line(&json!({"path":path,"json_pointer":""}).to_string()).unwrap_err(),
                "SOURCE_READ_PATH_INVALID");
        }
    }

    #[tokio::test]
    async fn json_projection_handler_has_bounded_matching_terminal_receipts() {
        let root = tempfile::tempdir().expect("workspace");
        let content = json!({"tiny":null,"large":"x".repeat(super::MAX_RANGE_TEXT_BYTES + 1)}).to_string();
        fs::write(root.path().join("capsule.json"), &content).unwrap();
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let canonical = root.path().canonicalize().unwrap();
        let admitted = Arc::new(open_admitted_root(&canonical).unwrap());
        for (index, (arguments, error)) in [
            (json!({"path":"capsule.json","json_pointer":"/tiny","expected_sha256":hash}), None),
            (json!({"path":"capsule.json","json_pointer":"/missing"}), Some("SOURCE_READ_JSON_POINTER_NOT_FOUND")),
            (json!({"path":"capsule.json","json_pointer":"/large"}), Some("SOURCE_READ_JSON_VALUE_TOO_LARGE")),
            (json!({"path":"capsule.json","json_pointer":"/tiny","expected_sha256":"0".repeat(64)}), Some("SOURCE_READ_SHA256_MISMATCH")),
        ].into_iter().enumerate() {
            let call_id = format!("json-receipt-{index}");
            let ctx = ToolContext::new(canonical.clone()).with_source_read_root(Some(admitted.clone()))
                .with_call_id(call_id.clone());
            let output = SourceReadHandler.handle(ToolCall {
                tool_name:"source_read".to_string(), call_id,
                payload:ToolPayload::Function { arguments },
            }, ctx).await.expect("projection receipt");
            assert_eq!(output.success, Some(error.is_none()));
            assert!(!super::result_too_large(&output.body, super::MAX_RANGE_RESULT_BYTES));
            if let Some(error) = error {
                assert_eq!(output.body["error_type"], error);
                assert_eq!(output.body["stdout"], "");
                assert_eq!(output.body["terminal_receipt"]["terminal_state"], "failed");
                assert_eq!(output.body["terminal_receipt"]["exit_code"], 1);
            } else {
                assert_eq!(output.body["stdout"], "null");
                assert_eq!(output.body["mode"], "json_projection");
                assert_eq!(output.body["json_pointer"], "/tiny");
                assert_eq!(output.body["source_sha256"], hash);
                assert_eq!(output.body["terminal_receipt"]["terminal_state"], "completed");
                assert_eq!(output.body["terminal_receipt"]["exit_code"], 0);
            }
            for key in ["start_line", "end_line", "requested_end_line", "next_line", "total_lines", "line_numbers", "at_eof"] {
                assert!(output.body.get(key).is_none(), "no fabricated line coverage: {key}");
            }
            let receipt_path = output.body["terminal_receipt_path"].as_str().unwrap();
            let durable: serde_json::Value = serde_json::from_slice(&fs::read(receipt_path).unwrap()).unwrap();
            assert_eq!(durable, output.body["terminal_receipt"]);
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn json_projection_that_cannot_fit_receipt_envelope_fails_atomically() {
        let root = tempfile::tempdir().expect("workspace");
        let path = format!("{}/{}/{}.txt", "\u{1}".repeat(190), "\u{2}".repeat(190), "\u{3}".repeat(125));
        let source = root.path().join(&path);
        fs::create_dir_all(source.parent().unwrap()).unwrap();
        let key = "\u{4}".repeat(1200);
        let selected = json!(["x".repeat(super::MAX_RANGE_TEXT_BYTES - 64), null, false]);
        let document = json!({(key.clone()):selected});
        fs::write(source, document.to_string()).unwrap();
        let arguments = json!({"path":path,"json_pointer":format!("/{key}")});
        let request = parse_command_line(&arguments.to_string()).expect("bounded arguments");
        let canonical = root.path().canonicalize().unwrap();
        let admitted = Arc::new(open_admitted_root(&canonical).unwrap());
        let ctx = ToolContext::new(canonical).with_source_read_root(Some(admitted.clone()))
            .with_call_id("json-envelope".to_string());
        let raw = execute(&request, &admitted);
        assert!(raw.success);
        assert_eq!(raw.stdout, selected.to_string());
        assert!(serde_json::to_string(&raw.stdout).unwrap().len() <= super::MAX_RANGE_TEXT_BYTES);
        let preview = shell_executor::preview_in_process_terminal_response(&ctx, &raw, super::TERMINATION_ORIGIN)
            .expect("preview");
        assert!(super::result_too_large(&preview, super::MAX_RANGE_RESULT_BYTES));
        let output = SourceReadHandler.handle(ToolCall {
            tool_name:"source_read".to_string(), call_id:"json-envelope".to_string(),
            payload:ToolPayload::Function { arguments },
        }, ctx).await.expect("failed projection receipt");
        assert_eq!(output.success, Some(false));
        assert_eq!(output.body["stdout"], "");
        assert_eq!(output.body["error_type"], "SOURCE_READ_RESULT_TOO_LARGE");
        assert!(!super::result_too_large(&output.body, super::MAX_RANGE_RESULT_BYTES));
        assert_eq!(output.body["terminal_receipt"]["terminal_state"], "failed");
        assert_eq!(output.body["terminal_receipt"]["exit_code"], 1);
        let receipt_path = output.body["terminal_receipt_path"].as_str().unwrap();
        let durable: serde_json::Value = serde_json::from_slice(&fs::read(receipt_path).unwrap()).unwrap();
        assert_eq!(durable, output.body["terminal_receipt"]);
    }
}
