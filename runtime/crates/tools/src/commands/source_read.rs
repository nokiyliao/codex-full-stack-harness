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
use tura_path::mapped_source_read::MappedSourceReadBinding;

const MAX_PATH_BYTES: usize = 512;
const MAX_FILE_BYTES: u64 = 1024 * 1024;
const MAX_LINES: usize = 200;
const MAX_RANGE_LINES: usize = 512;
const MAX_TEXT_BYTES: usize = 6144;
const MAX_RESULT_BYTES: usize = 8192;
const MAX_RANGE_TEXT_BYTES: usize = 12288;
const MAX_RANGE_RESULT_BYTES: usize = 16384;
/// Text budget for JSON pointer reads (projections, string pages, children pages). The
/// runtime context keeps at most 10,000 characters of a command's stdout and cuts the
/// middle of anything longer; only line-range pages are exempt. Matching that cap means
/// whatever a JSON read returns is what the model sees.
pub const MAX_JSON_TEXT_BYTES: usize = 10_000;
const MAX_JSON_RECOVERY_BYTES: usize = 2048;
const MAX_JSON_RECOVERY_CHILDREN: usize = 8;
const MAX_JSON_RECOVERY_KEY_BYTES: usize = 128;
const MAX_JSON_RECOVERY_POINTER_BYTES: usize = 256;
// Children pages: a child serializing to at most this many bytes is shown complete;
// larger children are summarized with their own pointer for a follow-up selection.
const JSON_CHILD_INLINE_BYTES: usize = 1024;
const JSON_CHILD_KEY_BYTES: usize = 512;
const JSON_CHILD_POINTER_BYTES: usize = 1024;
// A search line longer than this shows only an excerpt around its first matching term, so one
// long line (JSONL record, minified source, CSV row) neither fails the page nor fills it.
const SEARCH_LINE_EXCERPT_THRESHOLD: usize = 2048;
const SEARCH_EXCERPT_BYTES: usize = 1024;
const TERMINATION_ORIGIN: &str = "in_process_source_read";

pub fn output_limits_description() -> String {
    format!(
        "Line modes: At most {MAX_RANGE_LINES} complete lines for ranges and {MAX_LINES} for searches. \
         Range reads: {MAX_RANGE_TEXT_BYTES} text bytes, \
         {MAX_RANGE_RESULT_BYTES} receipt-inclusive serialized result bytes. \
         Search reads: {MAX_TEXT_BYTES} text bytes, \
         {MAX_RESULT_BYTES} receipt-inclusive serialized result bytes. \
         JSON pointer reads: {MAX_JSON_TEXT_BYTES} text bytes, \
         {MAX_RANGE_RESULT_BYTES} receipt-inclusive serialized result bytes, \
         for projections and string or children pages. \
         Oversized JSON projections fail without partial output. \
         A selected JSON string also takes start_line and end_line to page its decoded \
         lines like a file; positions are string_* fields, not physical lines. \
         A selected JSON object or array also takes children_offset to list its children \
         (object keys sorted, arrays by index), one JSON line each: complete when the child \
         serializes to at most {JSON_CHILD_INLINE_BYTES} bytes, otherwise summarized with its \
         own json_pointer; resume with children_next_offset. \
         Search lines longer than {SEARCH_LINE_EXCERPT_THRESHOLD} bytes show a \
         {SEARCH_EXCERPT_BYTES}-byte excerpt around the first matching term, cut sides marked [...] \
         and listed in excerpted_lines; read such a line in full with a range read. \
         A search end_line bounds the scanned lines. \
         Range reads include physical line numbers by default; \
         set line_numbers:false for raw text."
    )
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    expected_sha256_ref: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    children_offset: Option<usize>,
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

    pub fn expected_sha256(&self) -> Option<&str> {
        self.expected_sha256.as_deref()
    }

    pub fn uses_admitted_digest(&self) -> bool {
        self.expected_sha256_ref.as_deref() == Some("admitted")
    }

    pub fn uses_observed_digest(&self) -> bool {
        self.expected_sha256_ref.as_deref() == Some("observed")
    }

    fn normalized(&self) -> Self {
        let mut request = self.clone();
        request.expected_sha256 = request.expected_sha256.map(|sha| sha.to_ascii_lowercase());
        if request.json_pointer.is_none() {
            request.line_numbers = Some(request.line_numbers.unwrap_or(true));
            if request.search_terms.is_some() {
                request.start_line = Some(request.start_line.unwrap_or(1));
                request.context_lines = Some(request.context_lines.unwrap_or(0));
            }
        } else if request.start_line.is_some() {
            request.line_numbers = Some(request.line_numbers.unwrap_or(true));
        }
        request
    }

    fn byte_limits(&self) -> (usize, usize) {
        if self.search_terms.is_some() {
            (MAX_TEXT_BYTES, MAX_RESULT_BYTES)
        } else if self.json_pointer.is_some() {
            (MAX_JSON_TEXT_BYTES, MAX_RANGE_RESULT_BYTES)
        } else {
            (MAX_RANGE_TEXT_BYTES, MAX_RANGE_RESULT_BYTES)
        }
    }
}

/// Reference provenance is a page-integrity hint, never a retry or authority grant.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceReadDigestReference {
    schema_version: String,
    pub request: SourceReadRequest,
    pub expected_sha256: String,
}

impl SourceReadDigestReference {
    pub fn parse_bounded(value: &Value, kind: &str) -> Option<Self> {
        let bytes = serde_json::to_vec(value).ok()?;
        if bytes.len() > 4096 { return None; }
        let reference: Self = serde_json::from_slice(&bytes).ok()?;
        validate_request(&reference.request).ok()?;
        if !matches!(kind, "admitted" | "observed")
            || reference.request.expected_sha256_ref.as_deref() != Some(kind)
            || reference.schema_version != format!("nokiy_{kind}_source_digest_v1")
            || reference.expected_sha256.len() != 64
            || !reference.expected_sha256.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) {
            return None;
        }
        Some(reference)
    }

    pub fn matches_range(&self, path: &str, start: u64, requested_end: u64, numbered: bool) -> bool {
        let request = &self.request;
        request.path == path && request.search_terms.is_none() && request.json_pointer.is_none()
            && request.start_line.map(|line| line as u64) == Some(start)
            && request.end_line.map(|line| line as u64) == Some(requested_end)
            && request.line_numbers.unwrap_or(true) == numbered
    }

    pub fn matches_command(&self, line: &str) -> bool {
        parse_command_line(line).is_ok_and(|request| request.normalized() == self.request.normalized())
    }
}

/// Content observation from this operation's exact buffer, not a retry grant.
/// Consumers must authenticate it with the operation's existing terminal receipt.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceReadContentEvidence {
    schema_version: String,
    pub request: SourceReadRequest,
    pub source_sha256: String,
    pub content_satisfied: bool,
}

impl SourceReadContentEvidence {
    fn new(request: &SourceReadRequest, source_sha256: String, content_satisfied: bool) -> Self {
        Self { schema_version: "nokiy_source_read_content_v1".to_owned(),
            request: request.normalized(), source_sha256, content_satisfied }
    }

    pub fn parse_bounded(value: &Value) -> Option<Self> {
        let bytes = serde_json::to_vec(value).ok()?;
        if bytes.len() > 4096 { return None; }
        let proof: Self = serde_json::from_slice(&bytes).ok()?;
        validate_request(&proof.request).ok()?;
        let expected = proof.request.expected_sha256()?;
        if proof.schema_version != "nokiy_source_read_content_v1"
            || proof.request != proof.request.normalized()
            || proof.source_sha256.len() != 64
            || !proof.source_sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || proof.content_satisfied != (expected == proof.source_sha256) {
            return None;
        }
        Some(proof)
    }

    pub fn matches_command(&self, line: &str) -> bool {
        parse_command_line(line).is_ok_and(|request| request.normalized() == self.request)
    }

    pub fn satisfies(&self, failed: &Self) -> bool {
        let mut selected = self.request.clone();
        let mut original = failed.request.clone();
        selected.expected_sha256 = None;
        original.expected_sha256 = None;
        self.content_satisfied && !failed.content_satisfied
            && self.source_sha256 == failed.source_sha256 && selected == original
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
    InvalidExpectedSha256,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub enum SourceReadRejectionKind {
    #[serde(rename = "json_syntax_or_shape")]
    JsonSyntaxOrShape,
    #[serde(rename = "invalid_expected_sha256")]
    InvalidExpectedSha256,
}

impl SourceReadParseError {
    pub fn rejection_kind(&self) -> Option<SourceReadRejectionKind> {
        match self {
            Self::Json(error) if error.is_syntax() || error.is_data() || error.is_eof() =>
                Some(SourceReadRejectionKind::JsonSyntaxOrShape),
            Self::InvalidExpectedSha256 => Some(SourceReadRejectionKind::InvalidExpectedSha256),
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
            Self::InvalidExpectedSha256 => formatter.write_str("SOURCE_READ_SHA256_INVALID"),
        }
    }
}

pub fn parse_command_line_typed(raw: &str) -> Result<SourceReadRequest, SourceReadParseError> {
    if raw.len() > 16384 {
        return Err(SourceReadParseError::ArgumentsTooLarge);
    }
    let request = match parse_json_request(raw) {
        Err(SourceReadParseError::Json(error)) if error.is_eof() || error.is_syntax() => {
            // Smaller models sometimes drop the closing quote of a SHA-256 pin
            // (["a.py",1,50,"<64 hex>]). Restore exactly that quote; anything else stays rejected.
            match close_unterminated_sha_pin(raw).map(|repaired| parse_json_request(&repaired)) {
                Some(Ok(request)) => request,
                _ => return Err(SourceReadParseError::Json(error)),
            }
        }
        result => result?,
    };
    validate_request(&request)?;
    Ok(request)
}

fn parse_json_request(raw: &str) -> Result<SourceReadRequest, SourceReadParseError> {
    if raw.trim_start().starts_with('[') {
        let value = serde_json::from_str(raw).map_err(SourceReadParseError::Json)?;
        decode_request(value).map_err(SourceReadParseError::Json)
    } else {
        serde_json::from_str(raw).map_err(SourceReadParseError::Json)
    }
}

/// A quote followed by exactly 64 hexadecimal digits and then `]`, `}` or `,` is a SHA-256 pin
/// whose closing quote is missing. Insert that one quote when the pattern occurs exactly once.
fn close_unterminated_sha_pin(raw: &str) -> Option<String> {
    let bytes = raw.as_bytes();
    let mut found = None;
    for (index, byte) in bytes.iter().enumerate() {
        let end = index + 65;
        if *byte == b'"' && end < bytes.len()
            && bytes[index + 1..end].iter().all(u8::is_ascii_hexdigit)
            && matches!(bytes[end], b']' | b'}' | b',')
        {
            if found.is_some() {
                return None;
            }
            found = Some(end);
        }
    }
    let end = found?;
    Some(format!("{}\"{}", &raw[..end], &raw[end..]))
}

fn decode_request(value: Value) -> Result<SourceReadRequest, serde_json::Error> {
    // Keep positional arity stable; digest references use the named form only.
    if value.as_array().is_some_and(|items| items.len() > 8) {
        return Err(<serde_json::Error as serde::de::Error>::custom(
            "source_read positional arguments exceed eight fields"));
    }
    serde_json::from_value(value)
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
    #[expect(
        clippy::too_many_arguments,
        reason = "the rejection record mirrors its serialized fields"
    )]
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

fn validate_request(request: &SourceReadRequest) -> Result<(), SourceReadParseError> {
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
        return Err(SourceReadParseError::InvalidRequest("SOURCE_READ_PATH_INVALID".to_string()));
    }
    if request.children_offset.is_some() && request.json_pointer.is_none() {
        return Err(SourceReadParseError::InvalidRequest("SOURCE_READ_MODE_CONFLICT".to_string()));
    }
    if let Some(pointer) = &request.json_pointer {
        // A selected JSON string may be paged by its decoded lines like a file, and a
        // selected container by children_offset; other line or search options conflict.
        let string_range = match (request.start_line, request.end_line) {
            (None, None) => false,
            (Some(_), Some(_)) => true,
            _ => return Err(SourceReadParseError::InvalidRequest("SOURCE_READ_MODE_CONFLICT".to_string())),
        };
        if (!string_range && request.line_numbers.is_some())
            || request.search_terms.is_some()
            || request.context_lines.is_some()
            || (string_range && request.children_offset.is_some())
        {
            return Err(SourceReadParseError::InvalidRequest("SOURCE_READ_MODE_CONFLICT".to_string()));
        }
        validate_json_pointer(pointer).map_err(SourceReadParseError::InvalidRequest)?;
        if string_range
            && !matches!((request.start_line, request.end_line), (Some(start), Some(end)) if start >= 1 && end >= start)
        {
            return Err(SourceReadParseError::InvalidRequest("SOURCE_READ_RANGE_INVALID".to_string()));
        }
    } else if request.start_line == Some(0)
        || match &request.search_terms {
            Some(terms) => {
                request.end_line.is_some_and(|end| end < request.start_line.unwrap_or(1))
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
        return Err(SourceReadParseError::InvalidRequest("SOURCE_READ_RANGE_INVALID".to_string()));
    }
    if request.expected_sha256_ref.as_deref().is_some_and(|value| !matches!(value, "admitted" | "observed"))
        || (request.expected_sha256_ref.is_some() && request.expected_sha256.is_some())
    {
        return Err(SourceReadParseError::InvalidRequest("SOURCE_READ_DIGEST_REFERENCE_INVALID".to_owned()));
    }
    if let Some(expected_sha256) = &request.expected_sha256
        && (expected_sha256.len() != 64
            || !expected_sha256.bytes().all(|byte| byte.is_ascii_hexdigit()))
        {
            return Err(SourceReadParseError::InvalidExpectedSha256);
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

#[derive(Debug)]
pub enum SourceReadTargetError {
    NotFound,
    Invalid(String),
}

impl From<String> for SourceReadTargetError {
    fn from(error: String) -> Self {
        Self::Invalid(error)
    }
}

impl std::fmt::Display for SourceReadTargetError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotFound => formatter.write_str("SOURCE_READ_TARGET_NOT_FOUND"),
            Self::Invalid(error) => formatter.write_str(error),
        }
    }
}

fn target_resolution_error(error: std::io::Error) -> SourceReadTargetError {
    if error.kind() == std::io::ErrorKind::NotFound {
        SourceReadTargetError::NotFound
    } else {
        SourceReadTargetError::Invalid(format!("SOURCE_READ_TARGET_INVALID: {error}"))
    }
}

pub fn target_path(workspace: &Path, request: &SourceReadRequest) -> Result<PathBuf, String> {
    target_path_typed(workspace, request).map_err(|error| error.to_string())
}

pub fn target_path_typed(
    workspace: &Path,
    request: &SourceReadRequest,
) -> Result<PathBuf, SourceReadTargetError> {
    validate_request(request).map_err(|error| error.to_string())?;
    let root = workspace
        .canonicalize()
        .map_err(|error| format!("SOURCE_READ_WORKSPACE_INVALID: {error}"))?;
    if !root.is_dir()
        || workspace
            .symlink_metadata()
            .map(|metadata| metadata.file_type().is_symlink())
            .unwrap_or(true)
    {
        return Err("SOURCE_READ_WORKSPACE_INVALID".to_string().into());
    }
    let mut target = root;
    for component in Path::new(&request.path).components() {
        let Component::Normal(part) = component else {
            return Err("SOURCE_READ_PATH_INVALID".to_string().into());
        };
        target.push(part);
        let metadata = target
            .symlink_metadata()
            .map_err(target_resolution_error)?;
        if metadata.file_type().is_symlink() {
            return Err("SOURCE_READ_SYMLINK_DENIED".to_string().into());
        }
    }
    let metadata = target
        .metadata()
        .map_err(target_resolution_error)?;
    if !metadata.is_file() {
        return Err("SOURCE_READ_NOT_REGULAR_FILE".to_string().into());
    }
    if metadata.len() > MAX_FILE_BYTES {
        return Err("SOURCE_READ_FILE_TOO_LARGE".to_string().into());
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
                decode_request(arguments.clone()).map_err(|error| {
                    ToolError::RespondToModel(format!("SOURCE_READ_ARGUMENTS_INVALID: {error}"))
                })?
            }
            ToolPayload::Freeform { .. } => {
                return Err(ToolError::RespondToModel(
                    "SOURCE_READ_ARGUMENTS_INVALID".to_string(),
                ));
            }
        };
        validate_request(&request).map_err(|error| ToolError::RespondToModel(error.to_string()))?;
        let mut resolved = request.clone();
        if request.expected_sha256_ref.is_some() {
            let digest = if request.uses_admitted_digest() {
                ctx.source_read_digest(request.path()).map(str::to_owned)
            } else {
                observed_digest(&ctx, request.path())
            }.ok_or_else(||
                ToolError::RespondToModel("SOURCE_READ_DIGEST_REFERENCE_UNAVAILABLE".to_owned()))?;
            resolved.expected_sha256 = Some(digest);
            resolved.expected_sha256_ref = None;
            validate_request(&resolved).map_err(|error| ToolError::RespondToModel(error.to_string()))?;
        }
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
            || {
                let mut response = execute_with_mapped_source_reads(&resolved, root, ctx.mapped_source_reads());
                if request.expected_sha256_ref.is_some() {
                    // Explicit reference provenance is not a legacy literal-hash recovery proof.
                    response.output.as_object_mut().expect("source read output").remove("source_read_evidence");
                    let (key, schema) = if request.uses_admitted_digest() {
                        ("admitted_digest_reference", "nokiy_admitted_source_digest_v1")
                    } else {
                        ("observed_digest_reference", "nokiy_observed_source_digest_v1")
                    };
                    response.output[key] = json!({
                        "schema_version": schema,
                        "request": request, "expected_sha256": resolved.expected_sha256,
                    });
                }
                let response = fit_response_to_envelope(response, &ctx, max_result_bytes);
                attach_navigation(response, &ctx, &request, max_result_bytes)
            },
        );
        if response.success
            && response.output["terminal_receipt"]["terminal_state"] == "completed"
            && response.output["terminal_receipt"]["termination_proven"] == true
            && let Some(digest) = response.output["source_sha256"].as_str() {
                remember_observed_digest(&ctx, request.path(), digest);
            }
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

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ObservedDigest {
    schema_version: String,
    owner: String,
    workspace: PathBuf,
    path: String,
    sha256: String,
}

fn observation_key(ctx: &ToolContext, path: &str) -> Option<(String, String)> {
    let owner = ctx.source_observation_owner()?;
    ctx.receipt_store()?;
    if ctx.mapped_source_reads().is_some_and(|(_, bindings)| bindings.iter().any(|binding|
        binding.requested_logical_path == path || binding.logical_path == path)) {
        return None;
    }
    let identity = serde_json::to_vec(&(owner, &ctx.session_dir, path)).ok()?;
    Some((format!("{:x}.source-digest", Sha256::digest(identity)), owner.to_owned()))
}

fn observed_digest(ctx: &ToolContext, path: &str) -> Option<String> {
    let (key, owner) = observation_key(ctx, path)?;
    let bytes = ctx.receipt_store()?.read_optional(&key).ok()??;
    if bytes.len() > 8192 { return None; }
    let record: ObservedDigest = serde_json::from_slice(&bytes).ok()?;
    if record.schema_version != "nokiy_observed_source_digest_v1"
        || record.owner != owner || record.workspace != ctx.session_dir || record.path != path
        || record.sha256.len() != 64
        || !record.sha256.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) {
        return None;
    }
    Some(record.sha256)
}

fn remember_observed_digest(ctx: &ToolContext, path: &str, digest: &str) {
    let Some((key, owner)) = observation_key(ctx, path) else { return; };
    let record = ObservedDigest {
        schema_version: "nokiy_observed_source_digest_v1".to_owned(),
        owner, workspace: ctx.session_dir.clone(), path: path.to_owned(), sha256: digest.to_owned(),
    };
    if let Ok(bytes) = serde_json::to_vec(&record) {
        // Create-only: later reads, mutations and races cannot silently repin a path.
        // Optional recording failure never turns a successful source read into failure.
        let _ = ctx.receipt_store().expect("bound observation store").publish_new(&key, &bytes);
    }
}

fn attach_navigation(mut response: CommandResponse, ctx: &ToolContext,
                     request: &SourceReadRequest, max_result_bytes: usize) -> CommandResponse {
    // Optional hints must never evict source text or affect read admission.
    if !response.success || response.output["truncated"] != false
        || request.search_terms.is_some() || request.json_pointer.is_some() { return response; }
    let hint = ctx.source_navigation().and_then(|catalog| {
        catalog.hints(&ctx.session_dir, request.path(),
            response.output["source_sha256"].as_str()?,
            response.output["start_line"].as_u64()?,
            response.output["end_line"].as_u64()?)
    });
    if let Some(hint) = hint {
        response.output["support_navigation"] = hint;
        let fits = shell_executor::preview_in_process_terminal_response(ctx, &response, TERMINATION_ORIGIN)
            .is_ok_and(|body| !result_too_large(&body, max_result_bytes));
        if !fits {
            response.output.as_object_mut().expect("source read output").remove("support_navigation");
        }
    }
    response
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
    if let Some(recovery) = response.output.get_mut("json_recovery") {
        // Optional guidance must not replace the original failure with a size error.
        recovery["children"] = json!([]);
        recovery["incomplete"] = json!(true);
        if preview(&response) {
            return response;
        }
        response.output.as_object_mut().expect("source read output").remove("json_recovery");
        if preview(&response) {
            return response;
        }
    }
    if response.success && response.output["mode"] == "json_children" {
        // Each line is one child; keep a prefix of whole children and move the cursor.
        let original_stdout = response.stdout.clone();
        let lines: Vec<&str> = original_stdout.split_inclusive('\n').collect();
        let offset = response.output["children_offset"].as_u64().unwrap_or(0);
        for kept in (1..lines.len()).rev() {
            response.stdout = lines[..kept].concat();
            let summarized = lines[..kept].iter()
                .filter(|line| serde_json::from_str::<Value>(line).is_ok_and(|child| child.get("value").is_none()))
                .count();
            if let Some(metadata) = response.output.as_object_mut() {
                metadata.insert("children_returned".to_string(), json!(kept));
                metadata.insert("children_summarized".to_string(), json!(summarized));
                metadata.insert("children_next_offset".to_string(), json!(offset + kept as u64));
                metadata.insert("children_at_end".to_string(), json!(false));
                metadata.insert("truncated".to_string(), json!(true));
                metadata.insert("truncation_reason".to_string(), json!("response_limit"));
                metadata.remove("source_read_evidence");
            }
            if preview(&response) {
                return response;
            }
        }
        return result_too_large_response();
    }
    if response.success && response.output["mode"] != "json_projection" {
        // Decoded JSON string lines carry their positions in string_* fields.
        let prefix = if response.output["mode"] == "json_string_lines" { "string_" } else { "" };
        let original_stdout = response.stdout.clone();
        let lines: Vec<&str> = if original_stdout.is_empty() {
            Vec::new()
        } else {
            original_stdout.split_inclusive('\n').collect()
        };
        let start_line = response.output.get(format!("{prefix}start_line").as_str())
            .and_then(Value::as_u64).unwrap_or(0);
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
                metadata.insert(format!("{prefix}end_line"), json!(end_line));
                metadata.insert(format!("{prefix}next_line"), json!(end_line + 1));
                if let Some(matches) = metadata.get_mut("search_matches").and_then(Value::as_array_mut) {
                    matches.retain(|number| number.as_u64().is_some_and(|number| number <= end_line));
                }
                if let Some(excerpts) = metadata.get_mut("excerpted_lines").and_then(Value::as_array_mut) {
                    excerpts.retain(|excerpt| excerpt["line"].as_u64().is_some_and(|number| number <= end_line));
                }
                metadata.insert("truncated".to_string(), json!(true));
                metadata.remove("source_read_evidence");
                metadata.insert("truncation_reason".to_string(), json!("response_limit"));
                metadata.insert(format!("{prefix}at_eof"), json!(false));
                metadata.insert(
                    format!("{prefix}ends_with_newline"),
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

#[cfg(test)]
fn execute(request: &SourceReadRequest, root: &File) -> CommandResponse {
    execute_with_mapped_source_reads(request, root, None)
}

#[derive(Default)]
struct SourceReadFailureMetadata {
    mismatch: Option<SourceReadContentEvidence>,
    json_recovery: Option<Value>,
}

pub fn navigation_refusal_matches(proof: &Value, line: &str) -> bool {
    let Some(object) = proof.as_object() else { return false; };
    if object.len() != 4 || proof["schema_version"] != "nokiy_source_navigation_refusal_v1"
        || proof["error_type"] != "SOURCE_READ_JSON_VALUE_TOO_LARGE" {
        return false;
    }
    let Some(sha) = proof["source_sha256"].as_str() else { return false; };
    let Ok(request) = parse_command_line(line) else { return false; };
    sha.len() == 64 && sha.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        && request.json_pointer.is_some()
        && request.expected_sha256.as_deref().is_none_or(|expected| expected == sha)
        && proof["request"] == json!(request.normalized())
}

fn execute_with_mapped_source_reads(
    request: &SourceReadRequest,
    root: &File,
    mapped_source_reads: Option<(&Path, &[MappedSourceReadBinding])>,
) -> CommandResponse {
    let mut failure = SourceReadFailureMetadata::default();
    match read_range_with_mapped_source_reads(request, root, mapped_source_reads, &mut failure) {
        Ok((text, mut metadata)) => {
            // A partial page, or one with excerpted lines, cannot satisfy a previously
            // failed exact selector.
            if request.expected_sha256.is_some() && metadata["truncated"] == false
                && metadata.get("excerpted_lines").is_none()
            {
                metadata["source_read_evidence"] = json!(SourceReadContentEvidence::new(
                    request, metadata["source_sha256"].as_str().expect("read digest").to_owned(), true,
                ));
            }
            CommandResponse {
                success: true,
                exit_code: 0,
                stdout: text,
                stderr: String::new(),
                output: metadata,
                changes: Vec::new(),
            }
        },
        Err(error) => {
            let mut output = json!({"error_type": error});
            if let Some(proof) = failure.mismatch { output["source_read_evidence"] = json!(proof); }
            if let Some(recovery) = failure.json_recovery {
                if error == "SOURCE_READ_JSON_VALUE_TOO_LARGE" {
                    output["navigation_refusal"] = json!({
                        "schema_version":"nokiy_source_navigation_refusal_v1",
                        "error_type":error, "request":request.normalized(),
                        "source_sha256":recovery["source_sha256"],
                    });
                }
                output["json_recovery"] = recovery;
            }
            CommandResponse {
                success: false,
                exit_code: 1,
                stdout: String::new(),
                stderr: error.clone(),
                output,
                changes: Vec::new(),
            }
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

#[cfg(test)]
fn read_range_from_root(
    request: &SourceReadRequest,
    root: &File,
) -> Result<(String, Value), String> {
    read_range_from_root_observed(request, root, &mut SourceReadFailureMetadata::default())
}

fn read_range_from_root_observed(
    request: &SourceReadRequest,
    root: &File,
    failure: &mut SourceReadFailureMetadata,
) -> Result<(String, Value), String> {
    validate_request(request).map_err(|error| error.to_string())?;
    if request.expected_sha256_ref.is_some() {
        return Err("SOURCE_READ_DIGEST_REFERENCE_UNAVAILABLE".to_owned());
    }
    let file = open_no_follow(root, &request.path)?;
    read_range_from_file_observed(request, &file, failure)
}

fn read_range_with_mapped_source_reads(
    request: &SourceReadRequest,
    root: &File,
    mapped_source_reads: Option<(&Path, &[MappedSourceReadBinding])>,
    failure: &mut SourceReadFailureMetadata,
) -> Result<(String, Value), String> {
    validate_request(request).map_err(|error| error.to_string())?;
    let Some((repo_root, bindings)) = mapped_source_reads else {
        return read_range_from_root_observed(request, root, failure);
    };
    let mut selected = bindings.iter().filter(|binding|
        binding.requested_logical_path == request.path || binding.logical_path == request.path);
    let Some(binding) = selected.next() else {
        return read_range_from_root_observed(request, root, failure);
    };
    if selected.next().is_some() {
        return Err("MAPPED_SOURCE_READ_BINDING_AMBIGUOUS".to_string());
    }
    if binding.requested_logical_path != request.path || binding.logical_path != request.path {
        return Err("MAPPED_SOURCE_READ_REQUEST_NOT_EXACT".to_string());
    }
    let guard = binding.open_read_only(repo_root).map_err(|error| error.to_string())?;
    let result = read_range_from_file_observed(request, guard.file(), failure);
    if let Err(error) = guard.revalidate(repo_root) {
        *failure = SourceReadFailureMetadata::default();
        return Err(error.to_string());
    }
    result
}

fn read_range_from_file_observed(
    request: &SourceReadRequest,
    file: &File,
    failure: &mut SourceReadFailureMetadata,
) -> Result<(String, Value), String> {
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
    let sha256 = format!("{:x}", Sha256::digest(&bytes));
    if request
        .expected_sha256
        .as_ref()
        .is_some_and(|expected| !expected.eq_ignore_ascii_case(&sha256))
    {
        failure.mismatch = Some(SourceReadContentEvidence::new(request, sha256, false));
        return Err("SOURCE_READ_SHA256_MISMATCH".to_string());
    }
    if let Some(pointer) = &request.json_pointer {
        return read_json_projection(request, pointer, &bytes, &sha256, failure);
    }
    let content = std::str::from_utf8(&bytes).map_err(|_| "SOURCE_READ_NOT_UTF8".to_string())?;
    let result = read_line_modes(request, content, bytes.len(), &sha256);
    if matches!(&result, Err(error) if error == "SOURCE_READ_LINE_TOO_LONG") {
        // Use the projection parser and its existing resource limits. In particular,
        // from_slice rejects trailing data/JSONL rather than describing just a prefix.
        if let Ok(document) = serde_json::from_slice::<Value>(&bytes) {
            failure.json_recovery = Some(json_recovery(&document, "", &sha256));
        }
    }
    result
}

fn read_line_modes(
    request: &SourceReadRequest,
    content: &str,
    file_bytes: usize,
    sha256: &str,
) -> Result<(String, Value), String> {
    let lines: Vec<&str> = if content.is_empty() {
        Vec::new()
    } else {
        content.split_inclusive('\n').collect()
    };
    if let Some(terms) = &request.search_terms {
        return read_search(request, terms, &lines, file_bytes, sha256);
    }
    let start_line = request.start_line.expect("validated range");
    let requested_end = request.end_line.expect("validated range");
    if start_line > lines.len() {
        return Err("SOURCE_READ_RANGE_OUT_OF_BOUNDS".to_string());
    }
    let bounded_end = requested_end.min(start_line.saturating_add(MAX_RANGE_LINES - 1));
    let available_end = bounded_end.min(lines.len());
    let (max_text_bytes, _) = request.byte_limits();
    let numbered = request.line_numbers.unwrap_or(true);
    let mut text = String::new();
    let mut end_line = start_line - 1;
    for number in start_line..=available_end {
        if !append_line(
            &mut text,
            lines[number - 1],
            number,
            numbered,
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
    if numbered {
        result["line_numbers"] = json!(true);
    }
    Ok((text, result))
}

fn read_json_projection(
    request: &SourceReadRequest,
    pointer: &str,
    bytes: &[u8],
    sha256: &str,
    failure: &mut SourceReadFailureMetadata,
) -> Result<(String, Value), String> {
    let document: Value = serde_json::from_slice(bytes)
        .map_err(|_| "SOURCE_READ_JSON_INVALID".to_string())?;
    let Some(selected) = document.pointer(pointer) else {
        failure.json_recovery = Some(json_recovery(&document, pointer, sha256));
        return Err("SOURCE_READ_JSON_POINTER_NOT_FOUND".to_string());
    };
    if let Some(offset) = request.children_offset {
        if !matches!(selected, Value::Object(_) | Value::Array(_)) {
            failure.json_recovery = Some(json_recovery(&document, pointer, sha256));
            return Err("SOURCE_READ_JSON_CHILDREN_NOT_CONTAINER".to_string());
        }
        return read_json_children(request, pointer, selected, offset, bytes.len(), sha256);
    }
    if request.start_line.is_some() {
        let Value::String(content) = selected else {
            failure.json_recovery = Some(json_recovery(&document, pointer, sha256));
            return Err("SOURCE_READ_JSON_RANGE_NOT_STRING".to_string());
        };
        return read_json_string_lines(request, pointer, content, bytes.len(), sha256);
    }
    let text = serde_json::to_string(selected)
        .map_err(|_| "SOURCE_READ_JSON_SERIALIZATION_FAILED".to_string())?;
    let (max_text_bytes, _) = request.byte_limits();
    if text.len() > max_text_bytes
        || serde_json::to_string(&text)
            .map(|encoded| encoded.len() > max_text_bytes)
            .unwrap_or(true)
    {
        // No partial value; name the children, or for a string the first line page.
        failure.json_recovery = Some(json_recovery(&document, pointer, sha256));
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

/// Page a selected JSON string's decoded lines with the file line-range rules. Its
/// positions are string_* fields: they number lines of the value, not of the file.
fn read_json_string_lines(
    request: &SourceReadRequest,
    pointer: &str,
    content: &str,
    file_bytes: usize,
    sha256: &str,
) -> Result<(String, Value), String> {
    let (text, page) = read_line_modes(request, content, content.len(), sha256)?;
    let mut metadata = json!({
        "mode": "json_string_lines",
        "path": request.path,
        "json_pointer": pointer,
        "file_bytes": file_bytes,
        "source_sha256": sha256,
        "string_bytes": content.len(),
        "truncated": page["truncated"],
        "truncation_reason": page["truncation_reason"],
    });
    for key in ["start_line", "end_line", "requested_end_line", "next_line", "at_eof",
                "total_lines", "ends_with_newline"] {
        metadata[format!("string_{key}")] = page[key].clone();
    }
    if page["line_numbers"] == true {
        metadata["line_numbers"] = json!(true);
    }
    Ok((text, metadata))
}

/// Page a selected object's or array's children, one JSON line each. Small children are
/// complete; larger ones are summarized with their own pointer, so they can be selected,
/// paged by children_offset or, for strings, paged by lines.
fn read_json_children(
    request: &SourceReadRequest,
    pointer: &str,
    selected: &Value,
    offset: usize,
    file_bytes: usize,
    sha256: &str,
) -> Result<(String, Value), String> {
    let children: Vec<(Option<&str>, &Value)> = match selected {
        Value::Object(items) => items.iter().map(|(key, value)| (Some(key.as_str()), value)).collect(),
        Value::Array(items) => items.iter().map(|value| (None, value)).collect(),
        _ => return Err("SOURCE_READ_JSON_CHILDREN_NOT_CONTAINER".to_string()),
    };
    // A cursor immediately after the last child is a successful empty continuation.
    if offset > children.len() {
        return Err("SOURCE_READ_RANGE_OUT_OF_BOUNDS".to_string());
    }
    let (max_text_bytes, _) = request.byte_limits();
    let mut text = String::new();
    let (mut returned, mut summarized) = (0, 0);
    for (index, (key, value)) in children.iter().enumerate().skip(offset) {
        let (line, summary) = json_child_line(pointer, *key, index, value);
        let mut candidate = text.clone();
        candidate.push_str(&line);
        candidate.push('\n');
        if candidate.len() > max_text_bytes
            || serde_json::to_string(&candidate)
                .map(|encoded| encoded.len() > max_text_bytes)
                .unwrap_or(true)
        {
            if returned == 0 {
                return Err("SOURCE_READ_LINE_TOO_LONG".to_string());
            }
            break;
        }
        text = candidate;
        returned += 1;
        summarized += usize::from(summary);
    }
    let next = offset + returned;
    let at_end = next == children.len();
    Ok((text, json!({
        "mode": "json_children",
        "path": request.path,
        "json_pointer": pointer,
        "file_bytes": file_bytes,
        "source_sha256": sha256,
        "parent_type": json_value_type(selected),
        "child_count": children.len(),
        "children_offset": offset,
        "children_returned": returned,
        "children_summarized": summarized,
        "children_next_offset": (!at_end).then_some(next),
        "children_at_end": at_end,
        "child_inline_bytes": JSON_CHILD_INLINE_BYTES,
        "truncated": !at_end,
        "truncation_reason": (!at_end).then_some("response_limit"),
    })))
}

/// One children-page line in a fixed field order: the key (or index), then either the
/// complete value or a summary. Returns the line and whether it is a summary.
fn json_child_line(parent_pointer: &str, key: Option<&str>, index: usize, value: &Value) -> (String, bool) {
    let serialized = serde_json::to_string(value).expect("JSON child");
    let key_shown = key.is_none_or(|key| key.len() <= JSON_CHILD_KEY_BYTES);
    let mut fields: Vec<(&str, Value)> = Vec::new();
    match key {
        Some(key) if key_shown => fields.push(("key", json!(key))),
        Some(key) => {
            fields.push(("key_omitted", json!(true)));
            fields.push(("key_bytes", json!(key.len())));
        }
        None => fields.push(("index", json!(index))),
    }
    let summary = serialized.len() > JSON_CHILD_INLINE_BYTES;
    if summary {
        if key_shown {
            let token = key.map_or_else(|| index.to_string(), |key| key.replace('~', "~0").replace('/', "~1"));
            let child_pointer = format!("{parent_pointer}/{token}");
            if child_pointer.len() <= JSON_CHILD_POINTER_BYTES {
                fields.push(("json_pointer", json!(child_pointer)));
            } else {
                fields.push(("json_pointer_omitted", json!(true)));
            }
        }
        fields.push(("type", json!(json_value_type(value))));
        fields.push(("bytes", json!(serialized.len())));
        match value {
            Value::Object(items) => fields.push(("child_count", json!(items.len()))),
            Value::Array(items) => fields.push(("child_count", json!(items.len()))),
            Value::String(content) => fields.push(("string_lines", json!(content.split_inclusive('\n').count()))),
            _ => {}
        }
    }
    let mut line = String::from("{");
    for (position, (name, field)) in fields.iter().enumerate() {
        if position > 0 {
            line.push(',');
        }
        line.push_str(&json!(name).to_string());
        line.push(':');
        line.push_str(&field.to_string());
    }
    if !summary {
        line.push_str(",\"value\":");
        line.push_str(&serialized);
    }
    line.push('}');
    (line, summary)
}

fn json_value_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

fn json_recovery(document: &Value, pointer: &str, sha256: &str) -> Value {
    let mut parent = document;
    let mut resolved_end = 0;
    // Resolve one token at a time with the existing RFC6901 lookup, including its
    // array-index rules. Stop at the first failure; never search sibling subtrees.
    for token in pointer.split('/').skip(1) {
        let end = resolved_end + 1 + token.len();
        let Some(child) = parent.pointer(&pointer[resolved_end..end]) else { break; };
        parent = child;
        resolved_end = end;
    }
    let parent_pointer = &pointer[..resolved_end];
    let pointer_omitted = parent_pointer.len() > MAX_JSON_RECOVERY_POINTER_BYTES;
    let child_count = match parent {
        Value::Object(children) => children.len(),
        Value::Array(children) => children.len(),
        _ => 0,
    };
    let mut recovery = json!({
        "source_sha256": sha256,
        "parent_pointer": (!pointer_omitted).then_some(parent_pointer),
        "parent_pointer_omitted": pointer_omitted,
        "parent_type": json_value_type(parent),
        "child_count": child_count,
        "children": [],
        "incomplete": false,
    });
    if let Value::String(content) = parent {
        let lines = content.split_inclusive('\n').count();
        recovery["string_bytes"] = json!(content.len());
        recovery["string_lines"] = json!(lines);
        if !pointer_omitted && lines > 0 {
            recovery["string_page"] = json!({"json_pointer": parent_pointer, "start_line": 1,
                                             "end_line": lines.min(MAX_LINES)});
        }
    }
    if !pointer_omitted && child_count > 0 {
        recovery["children_page"] = json!({"json_pointer": parent_pointer, "children_offset": 0});
    }
    // A page hint repeats the parent pointer; a maximally escaped one may not fit the budget.
    if serde_json::to_vec(&recovery).expect("JSON recovery metadata").len() > MAX_JSON_RECOVERY_BYTES {
        let header = recovery.as_object_mut().expect("JSON recovery object");
        header.remove("string_page");
        header.remove("children_page");
    }
    // Even a maximally JSON-escaped parent pointer fits this fixed header. Count
    // serialized bytes, not raw key lengths, and serialize each bounded hint once.
    let mut used = serde_json::to_vec(&recovery).expect("JSON recovery metadata").len();
    let mut children = Vec::new();
    let mut add_child = |key: Option<&str>, index: usize, value: &Value| {
        if pointer_omitted || key.is_some_and(|key| key.len() > MAX_JSON_RECOVERY_KEY_BYTES) {
            return;
        }
        let token = match key {
            Some(key) => key.replace('~', "~0").replace('/', "~1"),
            None => index.to_string(),
        };
        let pointer = format!("{parent_pointer}/{token}");
        if pointer.len() > MAX_JSON_RECOVERY_POINTER_BYTES {
            return;
        }
        let mut hint = json!({"json_pointer": pointer, "type": json_value_type(value)});
        if let Some(key) = key { hint["key"] = json!(key); }
        else { hint["index"] = json!(index); }
        let cost = serde_json::to_vec(&hint).expect("JSON recovery child").len()
            + usize::from(!children.is_empty());
        if used + cost <= MAX_JSON_RECOVERY_BYTES {
            used += cost;
            children.push(hint);
        }
    };
    match parent {
        Value::Object(items) => {
            for (key, value) in items.iter().take(MAX_JSON_RECOVERY_CHILDREN) {
                add_child(Some(key), 0, value);
            }
        }
        Value::Array(items) => {
            for (index, value) in items.iter().take(MAX_JSON_RECOVERY_CHILDREN).enumerate() {
                add_child(None, index, value);
            }
        }
        _ => {},
    }
    // Never truncate a key or pointer into a misleading/invalid next selector.
    recovery["incomplete"] = json!(pointer_omitted || children.len() < child_count);
    recovery["children"] = json!(children);
    recovery
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
    // An end_line bounds the scan: matches and their context stop there.
    let limit = request.end_line.map_or(lines.len(), |end| end.min(lines.len()));
    let context = request.context_lines.unwrap_or(0);
    let (max_text_bytes, _) = request.byte_limits();
    let mut selected = vec![false; lines.len()];
    let mut matches = vec![false; lines.len()];
    for index in start.saturating_sub(context + 1)..limit {
        if terms.iter().any(|term| lines[index].contains(term)) {
            if index >= start - 1 {
                matches[index] = true;
            }
            let from = index.saturating_sub(context).max(start - 1);
            let to = index.saturating_add(context).saturating_add(1).min(limit);
            selected[from..to].fill(true);
        }
    }
    let mut text = String::new();
    let mut matched_lines = Vec::new();
    let mut excerpted = Vec::new();
    let mut end = start - 1;
    let mut kept = 0;
    let mut reason = None;
    for index in start - 1..limit {
        if !selected[index] {
            continue;
        }
        if kept == MAX_LINES {
            reason = Some("line_limit");
            break;
        }
        let line = lines[index];
        let excerpt = (line.len() > SEARCH_LINE_EXCERPT_THRESHOLD).then(|| search_excerpt(line, terms));
        let shown = excerpt.as_ref().map_or(line, |(text, _, _)| text.as_str());
        if !append_line(&mut text, shown, index + 1, true, max_text_bytes)? {
            reason = Some("response_limit");
            break;
        }
        if let Some((_, from, to)) = excerpt {
            excerpted.push(json!({"line": index + 1, "line_bytes": line.len(),
                                  "excerpt_start_byte": from, "excerpt_end_byte": to}));
        }
        kept += 1;
        end = index + 1;
        if matches[index] {
            matched_lines.push(index + 1);
        }
    }
    let next_line = reason.map(|_| end + 1);
    let mut metadata = json!({
        "path": request.path,
        "start_line": start,
        "end_line": end,
        "line_numbers": true,
        "search_matches": matched_lines,
        "context_lines": context,
        "truncated": reason.is_some(),
        "truncation_reason": reason,
        "next_line": next_line,
        "at_eof": next_line.is_none() && limit == lines.len(),
        "total_lines": lines.len(),
        "file_bytes": file_bytes,
        "source_sha256": sha256,
        "ends_with_newline": kept > 0 && lines[end - 1].ends_with('\n'),
    });
    if let Some(requested_end) = request.end_line {
        metadata["requested_end_line"] = json!(requested_end);
    }
    if !excerpted.is_empty() {
        metadata["excerpted_lines"] = json!(excerpted);
    }
    Ok((text, metadata))
}

/// A window of at most SEARCH_EXCERPT_BYTES around the first matching term of an over-long
/// line, each cut side marked [...], keeping the line's newline. Returns the text and the
/// window's byte span within the line.
fn search_excerpt(line: &str, terms: &[String]) -> (String, usize, usize) {
    let body = line.strip_suffix('\n').unwrap_or(line);
    let hit = terms.iter().filter_map(|term| body.find(term.as_str())).min().unwrap_or(0);
    let mut from = hit.saturating_sub(SEARCH_EXCERPT_BYTES / 2);
    while !body.is_char_boundary(from) {
        from -= 1;
    }
    let mut to = (from + SEARCH_EXCERPT_BYTES).min(body.len());
    while !body.is_char_boundary(to) {
        to -= 1;
    }
    let mut shown = String::with_capacity(to - from + 11);
    if from > 0 {
        shown.push_str("[...]");
    }
    shown.push_str(&body[from..to]);
    if to < body.len() {
        shown.push_str("[...]");
    }
    if line.ends_with('\n') {
        shown.push('\n');
    }
    (shown, from, to)
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
            .map_err(|error| if error == rustix::io::Errno::NOENT {
                "SOURCE_READ_TARGET_NOT_FOUND".to_string()
            } else {
                "SOURCE_READ_TARGET_INVALID".to_string()
            })?;
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

    #[cfg(unix)]
    struct MappedFixture {
        _tree: tempfile::TempDir,
        repo_root: std::path::PathBuf,
        physical_root: std::path::PathBuf,
        binding: tura_path::mapped_source_read::MappedSourceReadBinding,
        content: &'static str,
    }

    #[cfg(unix)]
    impl MappedFixture {
        fn new() -> Self {
            use std::os::unix::fs::symlink;
            let tree = tempfile::tempdir().expect("mapped tree");
            let tree_root = tree.path().canonicalize().expect("canonical tree");
            let repo_root = tree_root.join("repo");
            let physical_root = tree_root.join("materialized/dcf_v2");
            fs::create_dir_all(repo_root.join("config/contracts")).unwrap();
            fs::create_dir_all(repo_root.join("output")).unwrap();
            fs::create_dir_all(&physical_root).unwrap();
            symlink(physical_root.parent().unwrap(), repo_root.join("output/runtime")).unwrap();
            let content = "{\n  \"state\": {\"ready\": true},\n  \"lane\": \"mapped\"\n}\n";
            fs::write(physical_root.join("CURRENT.json"), content).unwrap();
            let contract = json!({
                "schema_version":"dcf_v2_contract_v1", "status":"active",
                "authority_effect":"none", "no_apply":true,
                "runtime": {"namespace":"output/runtime/dcf_v2",
                    "current_pointer":"output/runtime/dcf_v2/CURRENT.json"},
                "two_tier_storage_cutover": {
                    "logical_runtime_namespace":"output/runtime/dcf_v2",
                    "target_physical_runtime_root":physical_root,
                },
            });
            let bytes = serde_json::to_vec(&contract).unwrap();
            fs::write(repo_root.join("config/contracts/dcf_v2_contract.json"), &bytes).unwrap();
            let hash = format!("{:x}", Sha256::digest(&bytes));
            let binding = tura_path::mapped_source_read::MappedSourceReadBinding::derive(
                &repo_root, &bytes, &hash, "output/runtime/dcf_v2/CURRENT.json",
            ).expect("exact mapping");
            Self { _tree:tree, repo_root, physical_root, binding, content }
        }

        fn hash(&self) -> String {
            format!("{:x}", Sha256::digest(self.content.as_bytes()))
        }

        fn context(&self) -> ToolContext {
            let root = Arc::new(open_admitted_root(&self.repo_root).unwrap());
            ToolContext::new(self.repo_root.clone()).with_source_read_root(Some(root))
                .with_mapped_source_reads(self.repo_root.clone(), vec![self.binding.clone()])
        }

        fn execute(&self, arguments: serde_json::Value,
            bindings: Option<&[tura_path::mapped_source_read::MappedSourceReadBinding]>,
        ) -> super::CommandResponse {
            let request = parse_command_line(&arguments.to_string()).expect("mapped request");
            let root = open_admitted_root(&self.repo_root).unwrap();
            super::execute_with_mapped_source_reads(&request, &root,
                bindings.map(|bindings| (self.repo_root.as_path(), bindings)))
        }
    }

    #[test]
    fn admitted_digest_reference_is_explicit_and_never_an_unchecked_read() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("file"), "unchanged\n").unwrap();
        let arguments = json!({"path":"file","start_line":1,"end_line":1,
            "expected_sha256_ref":"admitted"});
        let request = parse_command_line(&arguments.to_string()).unwrap();
        assert!(request.uses_admitted_digest());
        let response = super::execute(&request, &open_admitted_root(&root.path().canonicalize().unwrap()).unwrap());
        assert!(!response.success);
        assert_eq!(response.stderr, "SOURCE_READ_DIGEST_REFERENCE_UNAVAILABLE");
        assert!(response.stdout.is_empty());
        for reference in ["latest", "", "file"] {
            let mut invalid = arguments.clone();
            invalid["expected_sha256_ref"] = json!(reference);
            assert!(parse_command_line(&invalid.to_string()).unwrap_err()
                .contains("SOURCE_READ_DIGEST_REFERENCE_INVALID"));
        }
        let mut both = arguments.clone();
        both["expected_sha256"] = json!("a".repeat(64));
        assert!(parse_command_line(&both.to_string()).is_err());
        let mut bad_literal = arguments;
        bad_literal.as_object_mut().unwrap().remove("expected_sha256_ref");
        bad_literal["expected_sha256"] = json!("short");
        assert!(parse_command_line(&bad_literal.to_string()).unwrap_err()
            .contains("SOURCE_READ_SHA256_INVALID"));
    }

    #[tokio::test]
    async fn optional_navigation_follows_real_read_and_never_grants_or_truncates() {
        let root = tempfile::tempdir().unwrap();
        let canonical = root.path().canonicalize().unwrap();
        let source = b"def helper():\n    return 1\n\ndef caller():\n    return helper()\n";
        fs::write(canonical.join("file.py"), source).unwrap();
        let digest = format!("{:x}", Sha256::digest(source));
        let catalog = crate::commands::source_navigation::Catalog::parse(&json!({
            "schema_version":"nokiy_source_navigation_v1","workspace":canonical,
            "files":{"file.py":{"sha256":digest,"owners":[
                {"range":[4,5],"definitions":[["helper","f",1,2]]}]}}
        }).to_string()).unwrap();
        let ctx = ToolContext::new(canonical.clone())
            .with_source_read_root(Some(Arc::new(open_admitted_root(&canonical).unwrap())))
            .with_source_navigation(catalog);
        let arguments = json!({"path":"file.py","start_line":4,"end_line":5,"expected_sha256":digest});
        let output = observation_read(ctx.child(), arguments.clone()).await.unwrap();
        assert_eq!(output.success, Some(true));
        assert_eq!(output.body["support_navigation"]["definitions"], json!([["helper","f",1,2]]));
        assert_eq!(output.body["stdout"], "4: def caller():\n5:     return helper()\n");
        assert_eq!(output.body["truncated"], false);
        let request = parse_command_line(&arguments.to_string()).unwrap();
        let plain = super::execute(&request, &open_admitted_root(&canonical).unwrap());
        let tiny = super::attach_navigation(plain.clone(), &ctx, &request, 1);
        assert_eq!(tiny.stdout, plain.stdout);
        assert_eq!(tiny.output, plain.output);
        fs::write(canonical.join("file.py"), b"changed\n").unwrap();
        let changed = observation_read(ctx.child(), arguments).await.unwrap();
        assert_eq!(changed.success, Some(false));
        assert!(changed.body.get("support_navigation").is_none());
        let no_root = ToolContext::new(canonical);
        assert!(observation_read(no_root, json!({"path":"file.py","start_line":1,"end_line":1})).await.is_err());
    }

    #[tokio::test]
    async fn admitted_digest_reference_checks_same_buffer_and_keeps_provenance() {
        let root = tempfile::tempdir().unwrap();
        let canonical = root.path().canonicalize().unwrap();
        fs::write(canonical.join("file"), "unchanged\n").unwrap();
        let digest = format!("{:x}", Sha256::digest(b"unchanged\n"));
        let ctx = ToolContext::new(canonical.clone())
            .with_source_read_root(Some(Arc::new(open_admitted_root(&canonical).unwrap())))
            .with_source_read_digests(std::collections::BTreeMap::from([
                ("file".to_owned(), digest.clone())]));
        let arguments = json!({"path":"file","start_line":1,"end_line":1,
            "expected_sha256_ref":"admitted"});
        let call = || ToolCall {tool_name:"source_read".to_owned(),
            call_id:"admitted-digest".to_owned(),
            payload:ToolPayload::Function {arguments:arguments.clone()}};
        let output = SourceReadHandler.handle(call(), ctx.clone()).await.unwrap();
        assert_eq!(output.success, Some(true));
        assert_eq!(output.body["stdout"], "1: unchanged\n");
        assert_eq!(output.body["admitted_digest_reference"]["expected_sha256"], digest);
        assert_eq!(output.body["admitted_digest_reference"]["request"], arguments);
        assert!(output.body.get("source_read_evidence").is_none());
        assert!(!super::result_too_large(&output.body, super::MAX_RANGE_RESULT_BYTES));
        fs::write(canonical.join("file"), "changed\n").unwrap();
        let changed = SourceReadHandler.handle(call(), ctx).await.unwrap();
        assert_eq!(changed.success, Some(false));
        assert_eq!(changed.body["error_type"], "SOURCE_READ_SHA256_MISMATCH");
        assert_eq!(changed.body["stdout"], "");
        assert!(changed.body.get("source_read_evidence").is_none());
        let no_pin = ToolContext::new(canonical.clone())
            .with_source_read_root(Some(Arc::new(open_admitted_root(&canonical).unwrap())));
        assert!(SourceReadHandler.handle(call(), no_pin).await.is_err());
    }

    fn observation_context(root: &std::path::Path, owner: &str) -> ToolContext {
        let store = tura_path::command_receipts::ReceiptStore::open_bound(root, None).unwrap();
        ToolContext::new(root.to_path_buf())
            .with_source_read_root(Some(Arc::new(open_admitted_root(root).unwrap())))
            .with_receipt_store(Some(Arc::new(store)))
            .with_source_observation_owner(Some(owner.to_owned()))
    }

    async fn observation_read(ctx: ToolContext, arguments: serde_json::Value) -> Result<crate::runtime::tool::FunctionToolOutput, crate::runtime::tool::ToolError> {
        SourceReadHandler.handle(ToolCall {
            tool_name: "source_read".to_owned(), call_id: "observation-read".to_owned(),
            payload: ToolPayload::Function { arguments },
        }, ctx).await
    }

    #[tokio::test]
    async fn observed_digest_survives_context_recreation_but_not_worker_or_path_change() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        fs::write(root.join("file"), "first\nsecond\n").unwrap();
        fs::write(root.join("other"), "first\nsecond\n").unwrap();
        let reference = json!({"path":"file", "start_line":2, "end_line":2,
            "expected_sha256_ref":"observed"});
        assert!(observation_read(observation_context(&root, "worker-a"), reference.clone()).await.is_err());
        let first = observation_read(observation_context(&root, "worker-a"),
            json!({"path":"file", "start_line":1, "end_line":1})).await.unwrap();
        assert_eq!(first.success, Some(true));
        let second = observation_read(observation_context(&root, "worker-a"), reference.clone()).await.unwrap();
        assert_eq!(second.success, Some(true));
        assert_eq!(second.body["stdout"], "2: second\n");
        assert_eq!(second.body["observed_digest_reference"]["expected_sha256"], first.body["source_sha256"]);
        assert!(second.body.get("source_read_evidence").is_none());
        assert!(observation_read(observation_context(&root, "worker-b"), reference.clone()).await.is_err());
        let mut other = reference.clone();
        other["path"] = json!("other");
        assert!(observation_read(observation_context(&root, "worker-a"), other).await.is_err());
        let unresolved = parse_command_line(&reference.to_string()).unwrap();
        let direct = super::execute(&unresolved, &open_admitted_root(&root).unwrap());
        assert!(!direct.success);
        assert_eq!(direct.stderr, "SOURCE_READ_DIGEST_REFERENCE_UNAVAILABLE");
    }

    #[tokio::test]
    async fn observed_digest_never_repins_and_failed_read_never_observes() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        fs::write(root.join("file"), "old\n").unwrap();
        let plain = json!({"path":"file", "start_line":1, "end_line":1});
        let mut wrong = plain.clone();
        wrong["expected_sha256"] = json!("0".repeat(64));
        let failed = observation_read(observation_context(&root, "worker"), wrong).await.unwrap();
        assert_eq!(failed.success, Some(false));
        assert!(super::observed_digest(&observation_context(&root, "worker"), "file").is_none());
        assert_eq!(observation_read(observation_context(&root, "worker"), plain.clone()).await.unwrap().success, Some(true));
        let pinned = super::observed_digest(&observation_context(&root, "worker"), "file").unwrap();
        fs::write(root.join("file"), "new\n").unwrap();
        assert_eq!(observation_read(observation_context(&root, "worker"), plain.clone()).await.unwrap().success, Some(true));
        assert_eq!(super::observed_digest(&observation_context(&root, "worker"), "file"), Some(pinned));
        let mut reference = plain;
        reference["expected_sha256_ref"] = json!("observed");
        let changed = observation_read(observation_context(&root, "worker"), reference).await.unwrap();
        assert_eq!(changed.success, Some(false));
        assert_eq!(changed.body["error_type"], "SOURCE_READ_SHA256_MISMATCH");
        assert_eq!(changed.body["stdout"], "");
    }

    #[tokio::test]
    async fn observed_digest_rejects_corrupt_foreign_and_symlink_records() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        fs::write(root.join("file"), "one\n").unwrap();
        let ctx = observation_context(&root, "worker");
        let plain = json!({"path":"file", "start_line":1, "end_line":1});
        assert_eq!(observation_read(ctx.clone(), plain.clone()).await.unwrap().success, Some(true));
        let (key, _) = super::observation_key(&ctx, "file").unwrap();
        let store = ctx.receipt_store().unwrap();
        let original: serde_json::Value = serde_json::from_slice(&store.read(&key).unwrap()).unwrap();
        for (field, value) in [("owner", json!("other")), ("path", json!("other")),
            ("workspace", json!("/other")), ("sha256", json!("bad")), ("extra", json!(true))] {
            let mut invalid = original.clone();
            invalid[field] = value;
            store.replace(&key, &serde_json::to_vec(&invalid).unwrap()).unwrap();
            assert!(super::observed_digest(&ctx, "file").is_none());
        }
        store.replace(&key, b"{").unwrap();
        assert!(super::observed_digest(&ctx, "file").is_none());
        // A corrupt optional record cannot break an ordinary authorized read.
        assert_eq!(observation_read(ctx.clone(), plain).await.unwrap().success, Some(true));
        assert!(super::observed_digest(&ctx, "file").is_none());
        #[cfg(unix)] {
            let name = store.display_path(&key).unwrap();
            fs::remove_file(&name).unwrap();
            std::os::unix::fs::symlink(root.join("file"), &name).unwrap();
            assert!(super::observed_digest(&ctx, "file").is_none());
        }
    }

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
            r#"{"path":"../secret","start_line":1,"end_line":2,"expected_sha256":"bad"}"#.to_owned(),
            " ".repeat(16385),
        ] {
            assert_eq!(parse_command_line_typed(&raw).unwrap_err().rejection_kind(), None);
        }
        assert!(parse_command_line_typed(r#"{"path":"file","start_line":1,"end_line":90}"#).is_ok());
        assert!(parse_command_line_typed(r#"["file",1,90]"#).is_ok());
    }

    #[test]
    fn unterminated_sha_pin_is_closed_once_and_other_syntax_errors_stay_typed() {
        use super::{SourceReadRejectionKind, parse_command_line_typed};
        // 2026-10-22: Luna workers sent ["task_a/accept_pair.py",191,350,"<sha>] about twenty times.
        let sha = "ea294ab67e9021f1686b1ac3c2adb7d603aa3173ae28d0acd3e4845c143d3550";
        let positional = parse_command_line_typed(&format!(r#"["task_a/accept_pair.py",191,350,"{sha}]"#)).unwrap();
        assert_eq!(positional.path, "task_a/accept_pair.py");
        assert_eq!(positional.expected_sha256.as_deref(), Some(sha));
        let named = parse_command_line_typed(&format!(
            r#"{{"path":"b.py","start_line":1,"end_line":9,"expected_sha256":"{sha}}}"#)).unwrap();
        assert_eq!(named.expected_sha256.as_deref(), Some(sha));
        let middle = parse_command_line_typed(&format!(r#"["a.py",1,9,"{sha},true]"#)).unwrap();
        assert_eq!(middle.expected_sha256.as_deref(), Some(sha));
        for raw in [
            format!(r#"["a.py",1,9,"{}]"#, &sha[..63]),
            format!(r#"["a.py",1,9,"{sha}]"#).replace(']', ""),
            format!(r#"["a.py",1,9,"{sha},"{sha}]"#),
            r#"["a.py",1,9,"not-a-sha]"#.to_owned(),
        ] {
            let error = parse_command_line_typed(&raw).unwrap_err();
            assert_eq!(error.rejection_kind(), Some(SourceReadRejectionKind::JsonSyntaxOrShape), "{raw}");
        }
        // A correctly quoted pin is untouched.
        assert_eq!(parse_command_line_typed(&format!(r#"["a.py",1,9,"{sha}"]"#)).unwrap().expected_sha256.as_deref(), Some(sha));
    }

    #[test]
    fn invalid_digest_format_is_typed_but_never_a_content_mismatch() {
        use super::{SourceReadParseError, SourceReadRejectionKind, parse_command_line_typed};
        for digest in [String::new(), "a".repeat(63), "a".repeat(65), "g".repeat(64),
            "5c7d6faa308bb6125375d6b1b06a0106d5e09fd22b182d51a6bf1c6af1c6af1c6af5321124".to_owned()] {
            let line = json!({"path":"file", "start_line":1, "end_line":2, "expected_sha256":digest}).to_string();
            let error = parse_command_line_typed(&line).unwrap_err();
            assert!(matches!(error, SourceReadParseError::InvalidExpectedSha256));
            assert_eq!(error.rejection_kind(), Some(SourceReadRejectionKind::InvalidExpectedSha256));
            assert_eq!(error.to_string(), "SOURCE_READ_SHA256_INVALID");
        }
        for digest in ["0".repeat(64), "A".repeat(64)] {
            let line = json!({"path":"file", "start_line":1, "end_line":2, "expected_sha256":digest}).to_string();
            assert!(parse_command_line_typed(&line).is_ok(), "content equality is not a parser decision");
        }
        let line = json!({"path":"file", "start_line":0, "end_line":2, "expected_sha256":"bad"}).to_string();
        assert_eq!(parse_command_line_typed(&line).unwrap_err().rejection_kind(), None);
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
            r#"["file",null,null,null,null,null,null,"","admitted"]"#,
        ] {
            assert!(parse_command_line(raw).is_err(), "{raw}");
        }
    }

    #[test]
    fn positional_ranges_match_named_pagination_and_limits() {
        let root = tempfile::tempdir().expect("workspace");
        for (content, first_reason) in [
            ("x\n".repeat(super::MAX_RANGE_LINES + 101), "line_limit"),
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
                    assert!(end >= start && end - start < super::MAX_RANGE_LINES);
                    assert!(text.len() <= super::MAX_RANGE_TEXT_BYTES);
                    assert!(serde_json::to_string(&text).unwrap().len() <= super::MAX_RANGE_TEXT_BYTES);
                    let expected: String = content
                        .split_inclusive('\n')
                        .enumerate()
                        .skip(start - 1)
                        .take(end - start + 1)
                        .map(|(index, line)| {
                            if line_numbers != Some(false) {
                                format!("{}: {line}", index + 1)
                            } else {
                                line.to_string()
                            }
                        })
                        .collect();
                    assert_eq!(text, expected);
                    if line_numbers == Some(false) {
                        assert!(metadata.get("line_numbers").is_none());
                    } else {
                        assert_eq!(metadata["line_numbers"], true);
                    }
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
                "expected_sha256": hash, "line_numbers": false,
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
        let count = super::MAX_RANGE_LINES + 5;
        let content: String = (1..=count).map(|line| format!("needle-{line}\n")).collect();
        fs::write(root.path().join(path), &content).expect("discovered file");
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let range = parse_command_line(&json!({
            "path": path, "start_line": 1, "end_line": count, "line_numbers": true,
            "expected_sha256": hash,
        }).to_string()).unwrap();
        let (text, metadata) = read_range(&range, root.path()).unwrap();
        assert_eq!(text.lines().count(), super::MAX_RANGE_LINES);
        assert!(text.len() <= super::MAX_RANGE_TEXT_BYTES);
        assert_eq!(metadata["source_sha256"], hash);
        assert_eq!(metadata["next_line"], super::MAX_RANGE_LINES + 1);
        assert_eq!(metadata["truncation_reason"], "line_limit");
        let resume = parse_command_line(&json!({
            "path": path, "start_line": metadata["next_line"], "end_line": count,
            "expected_sha256": metadata["source_sha256"],
        }).to_string()).unwrap();
        let (text, metadata) = read_range(&resume, root.path()).unwrap();
        let tail: String = (super::MAX_RANGE_LINES + 1..=count)
            .map(|line| format!("{line}: needle-{line}\n")).collect();
        assert_eq!(text, tail);
        assert_eq!(metadata["line_numbers"], true);
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
    fn own_edit_content_witness_preserves_failed_read_and_exact_selector() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("file"), "new\n").expect("postimage");
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        let old = format!("{:x}", Sha256::digest(b"old\n"));
        let current = format!("{:x}", Sha256::digest(b"new\n"));
        for selector in [
            json!({"path":"file", "start_line":1, "end_line":1}),
            json!({"path":"file", "search_terms":["new"]}),
        ] {
            let mut arguments = selector;
            arguments["expected_sha256"] = json!(old.to_ascii_uppercase());
            let request = parse_command_line(&arguments.to_string()).unwrap();
            let failed = execute(&request, &admitted);
            assert!(!failed.success);
            assert_eq!(failed.exit_code, 1);
            assert_eq!(failed.stderr, "SOURCE_READ_SHA256_MISMATCH");
            assert!(failed.stdout.is_empty());
            assert!(failed.changes.is_empty());
            let proof = super::SourceReadContentEvidence::parse_bounded(
                &failed.output["source_read_evidence"],
            ).expect("actual-buffer mismatch");
            assert_eq!(proof.request.expected_sha256(), Some(old.as_str()));
            assert_eq!(proof.source_sha256, current);
            assert!(!proof.content_satisfied);
            assert!(proof.matches_command(&arguments.to_string()));
            // The original request is neither repaired nor silently repinned.
            assert_eq!(request.expected_sha256(), Some(old.to_ascii_uppercase().as_str()));
            arguments["expected_sha256"] = json!(current.to_ascii_uppercase());
            let passed = execute(&parse_command_line(&arguments.to_string()).unwrap(), &admitted);
            assert!(passed.success);
            let satisfaction = super::SourceReadContentEvidence::parse_bounded(
                &passed.output["source_read_evidence"],
            ).expect("exact successful content");
            assert!(satisfaction.satisfies(&proof));
            let mut unrelated = satisfaction.clone();
            unrelated.request.path = "other".to_owned();
            assert!(!unrelated.satisfies(&proof));
            let mut forged = failed.output["source_read_evidence"].clone();
            forged["content_satisfied"] = json!(true);
            assert!(super::SourceReadContentEvidence::parse_bounded(&forged).is_none());
        }
        let partial = parse_command_line(&json!({"path":"file", "start_line":1,
            "end_line":2, "expected_sha256":current}).to_string()).unwrap();
        let response = execute(&partial, &admitted);
        assert!(response.success);
        assert!(response.output.get("source_read_evidence").is_none(), "no partial satisfaction");
        let missing = parse_command_line(r#"{"path":"missing","start_line":1,"end_line":1}"#).unwrap();
        let response = execute(&missing, &admitted);
        assert!(!response.success);
        assert_eq!(response.output.as_object().unwrap().len(), 1, "other errors stay plain");
    }

    #[test]
    fn raw_and_default_numbered_ranges_preserve_source_and_physical_labels() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("file"), "one\r\n中🙂\r\nlast").expect("source");
        let raw = parse_command_line(
            r#"{"path":"file","start_line":2,"end_line":3,"line_numbers":false}"#,
        )
        .expect("raw request");
        let (raw_text, raw_metadata) = read_range(&raw, root.path()).expect("raw read");
        assert_eq!(raw_text, "中🙂\r\nlast");
        assert!(raw_metadata.get("line_numbers").is_none());
        let default = parse_command_line(r#"{"path":"file","start_line":2,"end_line":3}"#)
            .expect("default request");
        let numbered = parse_command_line(
            r#"{"path":"file","start_line":2,"end_line":3,"line_numbers":true}"#,
        )
        .expect("numbered request");
        let default_page = read_range(&default, root.path());
        assert_eq!(default_page, read_range(&numbered, root.path()));
        let (text, metadata) = default_page.expect("default numbered read");
        assert_eq!(text, "2: 中🙂\r\n3: last");
        assert_eq!(metadata["line_numbers"], true);
        assert_eq!(metadata["start_line"], 2);
        assert_eq!(metadata["end_line"], 3);
        assert_eq!(metadata["ends_with_newline"], false);
        assert_eq!(metadata["next_line"], serde_json::Value::Null);
        assert_eq!(metadata["at_eof"], true);
        let mut unnumbered_metadata = metadata.clone();
        let _ = unnumbered_metadata.as_object_mut().unwrap().remove("line_numbers");
        assert_eq!(raw_metadata, unnumbered_metadata);
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
    fn default_ranges_match_explicit_true_across_pages() {
        let root = tempfile::tempdir().expect("workspace");
        for (content, first_reason) in [
            ("x\n".repeat(super::MAX_RANGE_LINES + 101), "line_limit"),
            (
                format!("{}last", format!("x{}\n", "\"".repeat(90)).repeat(99)),
                "response_limit",
            ),
        ] {
            fs::write(root.path().join("file"), &content).expect("source");
            let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
            let total_lines = content.split_inclusive('\n').count();
            let requested_end = total_lines + 1;
            let mut start = 24;
            let mut arguments = json!({
                "path": "file", "start_line": start, "end_line": requested_end,
                "expected_sha256": hash,
            });
            loop {
                let default = parse_command_line(&arguments.to_string()).expect("default page");
                assert!(default.line_numbers.is_none());
                let mut numbered_arguments = arguments.clone();
                numbered_arguments["line_numbers"] = json!(true);
                let numbered = parse_command_line(&numbered_arguments.to_string())
                    .expect("explicit numbered page");
                let page = read_range(&default, root.path());
                assert_eq!(page, read_range(&numbered, root.path()));
                let (text, metadata) = page.expect("bounded numbered page");
                let end = metadata["end_line"].as_u64().expect("end line") as usize;
                assert!(end >= start && end - start < super::MAX_RANGE_LINES);
                assert!(text.len() <= super::MAX_RANGE_TEXT_BYTES);
                assert!(serde_json::to_string(&text).unwrap().len() <= super::MAX_RANGE_TEXT_BYTES);
                let expected: String = content.split_inclusive('\n').enumerate()
                    .skip(start - 1).take(end - start + 1)
                    .map(|(index, line)| format!("{}: {line}", index + 1)).collect();
                assert_eq!(text, expected);
                assert_eq!(metadata["line_numbers"], true);
                assert_eq!(metadata["source_sha256"], hash);
                assert_eq!(metadata["start_line"], start);
                assert_eq!(metadata["requested_end_line"], requested_end);
                assert_eq!(metadata["ends_with_newline"], text.ends_with('\n'));
                if start == 24 {
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
                arguments["start_line"] = metadata["next_line"].clone();
                arguments["expected_sha256"] = metadata["source_sha256"].clone();
            }
        }
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
            json!({"path":"src/main.rs", "start_line":3, "end_line":2, "search_terms":["x"]}).to_string(),
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
    async fn default_numbered_exact_range_completes_in_one_read_without_growing_search_pages() {
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
                "expected_sha256":hash,
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
        assert!(description.contains("Range reads include physical line numbers by default"));
        assert!(description.contains("set line_numbers:false for raw text."));
        assert!(description.contains(&format!(
            "At most {} complete lines for ranges and {} for searches.",
            super::MAX_RANGE_LINES, super::MAX_LINES,
        )));
        for (mode, arguments) in [
            (
                "Range",
                json!({"path":"limits.txt", "start_line":1, "end_line":2}),
            ),
            ("Search", json!({"path":"limits.txt", "search_terms":["x"]})),
            ("JSON pointer", json!({"path":"limits.txt", "json_pointer":"/x"})),
            ("JSON pointer", json!({"path":"limits.txt", "json_pointer":"/x", "children_offset":0})),
        ] {
            let request = parse_command_line(&arguments.to_string()).expect("valid request");
            let (max_text_bytes, max_result_bytes) = request.byte_limits();
            assert!(
                description.contains(&format!(
                    "{mode} reads: {max_text_bytes} text bytes, {max_result_bytes} receipt-inclusive serialized result bytes",
                )),
                "{mode} limits disagree with parsed request: {description}",
            );
        }
    }

    #[test]
    fn request_budgets_bound_raw_and_json_escaped_complete_lines() {
        let root = tempfile::tempdir().expect("workspace");
        for (arguments, limits) in [
            (json!({"path":"limits.txt", "start_line":1, "end_line":300}), (12288, 16384)),
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
            let (text, metadata) = read_range(&request, root.path()).expect("inclusive escaped bound");
            if arguments.get("search_terms").is_some() {
                // A search line over the excerpt threshold is shown as an excerpt, not whole.
                assert_eq!(metadata["excerpted_lines"][0]["line_bytes"], json!(max_text_bytes - 5));
            } else {
                assert_eq!(text.len(), max_text_bytes - 2);
                assert_eq!(serde_json::to_string(&text).unwrap().len(), max_text_bytes);
            }

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
                fs::write(root.path().join("limits.txt"), &line).expect("oversized line");
                if arguments.get("search_terms").is_some() {
                    let (_, metadata) = read_range(&request, root.path()).expect("excerpted line");
                    assert_eq!(metadata["excerpted_lines"][0]["line_bytes"], json!(line.len()));
                } else {
                    assert_eq!(read_range(&request, root.path()).unwrap_err(), "SOURCE_READ_LINE_TOO_LONG");
                }
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
        assert_eq!(text, "2: two\n");
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
        let request = parse_command_line(r#"{"path":"lines.txt","start_line":1,"end_line":100,"line_numbers":false}"#)
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
                "end_line":100, "expected_sha256":hash, "line_numbers":false}).to_string(),
        )
        .expect("resume");
        let (next, metadata) = read_range(&resume, root.path()).expect("resumed range");
        assert_eq!(text + &next, content);
        assert_eq!(metadata["end_line"], 100);
        assert_eq!(metadata["next_line"], serde_json::Value::Null);
        assert_eq!(metadata["source_sha256"], hash);
    }

    #[test]
    fn short_line_ranges_reduce_pages_without_losing_content() {
        let root = tempfile::tempdir().expect("workspace");
        let content = "x\n".repeat(1000);
        fs::write(root.path().join("file"), &content).unwrap();
        let mut page_counts = Vec::new();
        for size in [200, super::MAX_RANGE_LINES] {
            let mut start = 1;
            let mut reconstructed = String::new();
            let mut pages = 0;
            loop {
                let request = parse_command_line(&json!({"path":"file", "start_line":start,
                    "end_line":start + size - 1, "line_numbers":false}).to_string()).unwrap();
                let (text, meta) = read_range(&request, root.path()).unwrap();
                assert!(text.len() <= super::MAX_RANGE_TEXT_BYTES);
                reconstructed.push_str(&text);
                pages += 1;
                match meta["next_line"].as_u64() {
                    Some(next) => start = next as usize,
                    None => break,
                }
            }
            assert_eq!(reconstructed, content);
            page_counts.push(pages);
        }
        assert_eq!(page_counts, vec![5, 2]);
    }

    #[test]
    fn long_ordered_ranges_are_clipped_to_range_line_budget() {
        let root = tempfile::tempdir().expect("workspace");
        let cap = super::MAX_RANGE_LINES;
        fs::write(root.path().join("lines.txt"), "x\n".repeat(cap + 101)).expect("source file");

        let request = parse_command_line(&json!({"path":"lines.txt","start_line":1,"end_line":cap + 1}).to_string())
            .expect("range beyond line cap is valid");
        let (text, metadata) = read_range(&request, root.path()).expect("bounded read");
        assert_eq!(text, (1..=cap).map(|line| format!("{line}: x\n")).collect::<String>());
        assert_eq!(metadata["end_line"], cap);
        assert_eq!(metadata["requested_end_line"], cap + 1);
        assert_eq!(metadata["truncated"], true);
        assert_eq!(metadata["truncation_reason"], "line_limit");
        assert_eq!(metadata["next_line"], cap + 1);
        assert_eq!(metadata["at_eof"], false);

        let request = parse_command_line(
            &json!({"path":"lines.txt", "start_line":51, "end_line":usize::MAX}).to_string(),
        )
        .expect("large end line is valid");
        let (text, metadata) = read_range(&request, root.path()).expect("bounded read");
        assert_eq!(text, (51..=50 + cap).map(|line| format!("{line}: x\n")).collect::<String>());
        assert_eq!(metadata["end_line"], 50 + cap);
        assert_eq!(metadata["requested_end_line"], usize::MAX as u64);
        assert_eq!(metadata["truncation_reason"], "line_limit");
        assert_eq!(metadata["next_line"], 51 + cap);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn receipt_bearing_response_stays_bounded_and_resumable() {
        for (search, raw_range) in [(false, false), (false, true), (true, false)] {
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
            let mut arguments = if search {
                json!({"path":long_path, "search_terms":["x"]})
            } else {
                json!({"path":long_path, "start_line":1, "end_line":500})
            };
            if raw_range {
                arguments["line_numbers"] = json!(false);
            }
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
            if raw_range {
                assert!(output.body.get("line_numbers").is_none());
            } else {
                assert_eq!(output.body["line_numbers"], true);
            }
            let expected: String = (1..=200).map(|n| {
                if raw_range { line.clone() } else { format!("{n}: {line}") }
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
            // Search shows longer lines as excerpts, so its whole line stays at the threshold and
            // escaped quotes fill the text budget instead.
            let line = if search {
                format!("x{}", "\"".repeat(super::SEARCH_LINE_EXCERPT_THRESHOLD - 2))
            } else {
                "x".repeat(max_text_bytes - 32)
            };
            fs::write(source, format!("{line}\n")).expect("source file");
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
    fn source_read_missing_target_is_not_a_policy_denial() {
        let root = tempfile::tempdir().expect("workspace");
        fs::create_dir(root.path().join("src")).expect("source directory");
        let request = parse_command_line(r#"{"path":"src/missing.rs","start_line":1,"end_line":1}"#)
            .expect("request");
        assert_eq!(target_path(root.path(), &request).unwrap_err(), "SOURCE_READ_TARGET_NOT_FOUND");
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).expect("root");
        assert_eq!(read_range_from_root(&request, &admitted).unwrap_err(), "SOURCE_READ_TARGET_NOT_FOUND");
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
        assert_eq!(text, "1: admitted\n");
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
        ] {
            let request = parse_command_line(&arguments.to_string()).expect("line request");
            assert_eq!(read_range(&request, root.path()).unwrap_err(), "SOURCE_READ_LINE_TOO_LONG");
            let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
            let response = execute(&request, &admitted);
            assert!(!response.success);
            assert_eq!(response.exit_code, 1);
            assert!(response.stdout.is_empty());
            assert!(response.changes.is_empty());
            assert_eq!(response.stderr, "SOURCE_READ_LINE_TOO_LONG");
            assert_eq!(response.output["error_type"], "SOURCE_READ_LINE_TOO_LONG");
            let recovery = &response.output["json_recovery"];
            assert_eq!(recovery["source_sha256"], hash);
            assert_eq!(recovery["parent_pointer"], "");
            assert_eq!(recovery["parent_type"], "object");
            assert_eq!(recovery["child_count"], 2);
            assert_eq!(recovery["incomplete"], false);
            let hints = recovery["children"].as_array().unwrap();
            assert_eq!(hints.len(), 2);
            for key in ["answer", "padding"] {
                assert!(hints.contains(&json!({"key":key, "json_pointer":format!("/{key}"), "type":"string"})));
            }
            assert!(!response.output.to_string().contains("中文 ✅"));
            assert!(response.output.get("source_read_evidence").is_none());
        }
        // A search finds the tiny field directly: the over-long line shows an excerpt around it.
        let request = parse_command_line(r#"{"path":"capsule.json","search_terms":["answer"]}"#).unwrap();
        let (text, metadata) = read_range(&request, root.path()).expect("search excerpt");
        assert!(text.starts_with("1: {\"answer\":\"中文 ✅\""), "{}", &text[..40]);
        assert_eq!(metadata["excerpted_lines"][0]["line_bytes"], json!(content.len()));
        let request = parse_command_line(&json!({
            "path":"capsule.json", "json_pointer":"/answer", "expected_sha256":hash.to_uppercase(),
        }).to_string()).expect("projection request");
        assert_eq!(request.byte_limits(), (super::MAX_JSON_TEXT_BYTES, super::MAX_RANGE_RESULT_BYTES));
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
            assert_eq!(response.exit_code, 1);
            assert!(response.stdout.is_empty());
            assert_eq!(response.stderr, "SOURCE_READ_JSON_POINTER_NOT_FOUND");
            assert_eq!(response.output["error_type"], "SOURCE_READ_JSON_POINTER_NOT_FOUND");
            assert!(response.output["json_recovery"].is_object());
            assert!(!response.output.to_string().contains("do-not-leak"));
            assert!(response.output.get("source_read_evidence").is_none());
        }
    }

    #[test]
    fn json_recovery_reports_nearest_parent_and_valid_escaped_children() {
        let root = tempfile::tempdir().unwrap();
        let document = json!({"result":{"a/b":{"~key":[null, {"secret":"do-not-leak"}, false]},
            "~1":{"":42}}, "scope":{}, "failure":null});
        let content = document.to_string();
        fs::write(root.path().join("values.json"), &content).unwrap();
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        for (pointer, parent, kind, children) in [
            ("/schema_version", "", "object", 3),
            ("/result/runtimes", "/result", "object", 2),
            ("/result/a~1b/missing", "/result/a~1b", "object", 1),
            ("/result/a~1b/~0key/99", "/result/a~1b/~0key", "array", 3),
            ("/result/a~1b/~0key/01", "/result/a~1b/~0key", "array", 3),
            ("/result/a~1b/~0key/-", "/result/a~1b/~0key", "array", 3),
            ("/result/a~1b/~0key/1/missing", "/result/a~1b/~0key/1", "object", 1),
            ("/result/a~1b/~0key/0/missing", "/result/a~1b/~0key/0", "null", 0),
            ("/result/~01/missing", "/result/~01", "object", 1),
            ("/result/~01//missing", "/result/~01/", "number", 0),
        ] {
            let request = parse_command_line(&json!({"path":"values.json",
                "json_pointer":pointer, "expected_sha256":hash}).to_string()).unwrap();
            let response = execute(&request, &admitted);
            assert!(!response.success);
            assert_eq!(response.exit_code, 1);
            assert_eq!(response.stderr, "SOURCE_READ_JSON_POINTER_NOT_FOUND");
            assert_eq!(response.output["error_type"], response.stderr);
            assert!(response.stdout.is_empty());
            let recovery = &response.output["json_recovery"];
            assert_eq!(recovery["source_sha256"], hash);
            assert_eq!(recovery["parent_pointer"], parent);
            assert_eq!(recovery["parent_type"], kind);
            assert_eq!(recovery["child_count"], children);
            assert_eq!(recovery["incomplete"], false);
            assert!(!response.output.to_string().contains("do-not-leak"));
            let hints = recovery["children"].as_array().unwrap();
            assert_eq!(hints.len(), children as usize);
            for hint in hints {
                let pointer = hint["json_pointer"].as_str().unwrap();
                let selected = document.pointer(pointer).expect("valid escaped hint");
                assert_eq!(hint["type"], super::json_value_type(selected));
                let next = parse_command_line(&json!({"path":"values.json",
                    "json_pointer":pointer, "expected_sha256":hash}).to_string()).unwrap();
                let read = execute(&next, &admitted);
                assert!(read.success);
                assert_eq!(serde_json::from_str::<serde_json::Value>(&read.stdout).unwrap(), *selected);
            }
            if kind == "array" {
                assert_eq!(hints, &vec![
                    json!({"index":0,"json_pointer":"/result/a~1b/~0key/0","type":"null"}),
                    json!({"index":1,"json_pointer":"/result/a~1b/~0key/1","type":"object"}),
                    json!({"index":2,"json_pointer":"/result/a~1b/~0key/2","type":"boolean"}),
                ]);
            }
            if parent == "/result" {
                assert!(hints.contains(&json!({"key":"a/b","json_pointer":"/result/a~1b","type":"object"})));
                assert!(hints.contains(&json!({"key":"~1","json_pointer":"/result/~01","type":"object"})));
            }
        }
    }

    #[test]
    fn json_recovery_requires_one_valid_json_value_and_never_exposes_scalars() {
        let root = tempfile::tempdir().unwrap();
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        let long = json!({"secret":"x".repeat(super::MAX_RANGE_TEXT_BYTES + 1)}).to_string();
        for content in [
            "not JSON ".repeat(super::MAX_RANGE_TEXT_BYTES),
            format!("{long} trailing"), format!("{long}\n{long}\n"),
            format!("{}0{}{}", "[".repeat(140), "]".repeat(140), " ".repeat(super::MAX_RANGE_TEXT_BYTES)),
        ] {
            fs::write(root.path().join("values.json"), content).unwrap();
            for arguments in [json!({"path":"values.json","start_line":1,"end_line":1}),
                json!({"path":"values.json","search_terms":["secret", "not JSON", "["]})] {
                let request = parse_command_line(&arguments.to_string()).unwrap();
                let response = execute(&request, &admitted);
                if arguments.get("search_terms").is_some() {
                    // Search shows over-long lines as excerpts instead of failing.
                    assert!(response.success, "{:?}", response.output);
                    assert!(response.output["excerpted_lines"].as_array().is_some_and(|lines| !lines.is_empty()));
                    continue;
                }
                assert!(!response.success);
                assert_eq!(response.exit_code, 1);
                assert!(response.stdout.is_empty());
                assert_eq!(response.stderr, "SOURCE_READ_LINE_TOO_LONG");
                assert_eq!(response.output, json!({"error_type":"SOURCE_READ_LINE_TOO_LONG"}));
            }
        }
        for (value, kind) in [(json!(null), "null"), (json!(false), "boolean"),
            (json!(42), "number"), (json!("do-not-leak"), "string")] {
            fs::write(root.path().join("values.json"), value.to_string()).unwrap();
            let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/missing"}"#).unwrap();
            let response = execute(&request, &admitted);
            assert_eq!(response.stderr, "SOURCE_READ_JSON_POINTER_NOT_FOUND");
            assert_eq!(response.output["json_recovery"]["parent_type"], kind);
            assert_eq!(response.output["json_recovery"]["parent_pointer"], "");
            assert_eq!(response.output["json_recovery"]["children"], json!([]));
            assert_eq!(response.output["json_recovery"]["incomplete"], false);
            assert!(!response.output.to_string().contains("do-not-leak"));
        }
        fs::write(root.path().join("values.json"), json!("x".repeat(super::MAX_RANGE_TEXT_BYTES + 1)).to_string()).unwrap();
        let request = parse_command_line(r#"{"path":"values.json","start_line":1,"end_line":1}"#).unwrap();
        let response = execute(&request, &admitted);
        assert_eq!(response.stderr, "SOURCE_READ_LINE_TOO_LONG");
        assert_eq!(response.output["json_recovery"]["parent_type"], "string");
        assert_eq!(response.output["json_recovery"]["child_count"], 0);
        assert_eq!(response.output["json_recovery"]["children"], json!([]));
    }

    #[test]
    fn json_recovery_bounds_keys_pointers_children_and_serialized_bytes() {
        use serde_json::{Map, Value};
        let many = Value::Object((0..100).map(|i| (format!("k{i:03}"), json!("do-not-leak"))).collect());
        let huge = Value::Object(Map::from_iter([("k".repeat(100_000), json!("do-not-leak"))]));
        let escaped = Value::Object((0..8).map(|i| (format!("{i}{}", "\u{1}".repeat(100)), json!(null))).collect());
        let pointer_overflow = Value::Object(Map::from_iter([("~".repeat(128), json!(null))]));
        let long_key = "p".repeat(super::MAX_JSON_RECOVERY_POINTER_BYTES + 1);
        let long_parent = Value::Object(Map::from_iter([(long_key.clone(), json!({"child":null}))]));
        let control = "\u{1}".repeat(127);
        let escaped_parent = Value::Object(Map::from_iter([(control.clone(), Value::Object(Map::from_iter([
            (control.clone(), json!({"child":null}))])))]));
        for (document, pointer, omitted) in [
            (many, "/missing".to_string(), false),
            (huge, "/missing".to_string(), false),
            (escaped, "/missing".to_string(), false),
            (Value::Array(vec![Value::Null; 100]), "/100".to_string(), false),
            (pointer_overflow, "/missing".to_string(), false),
            (long_parent, format!("/{long_key}/missing"), true),
            (escaped_parent, format!("/{control}/{control}/missing"), false),
        ] {
            let hash = format!("{:x}", Sha256::digest(document.to_string().as_bytes()));
            let recovery = super::json_recovery(&document, &pointer, &hash);
            assert_eq!(recovery, super::json_recovery(&document, &pointer, &hash), "deterministic");
            assert!(serde_json::to_vec(&recovery).unwrap().len() <= super::MAX_JSON_RECOVERY_BYTES);
            assert_eq!(recovery["parent_pointer_omitted"], omitted);
            assert_eq!(recovery["parent_pointer"].is_null(), omitted);
            assert_eq!(recovery["incomplete"], true);
            assert!(!recovery.to_string().contains("do-not-leak"));
            let hints = recovery["children"].as_array().unwrap();
            assert!(hints.len() <= super::MAX_JSON_RECOVERY_CHILDREN);
            assert!(hints.len() < recovery["child_count"].as_u64().unwrap() as usize);
            for hint in hints {
                if let Some(key) = hint["key"].as_str() {
                    assert!(key.len() <= super::MAX_JSON_RECOVERY_KEY_BYTES);
                }
                let pointer = hint["json_pointer"].as_str().unwrap();
                assert!(pointer.len() <= super::MAX_JSON_RECOVERY_POINTER_BYTES);
                assert!(document.pointer(pointer).is_some());
                super::validate_json_pointer(pointer).unwrap();
            }
        }
    }

    #[test]
    fn json_recovery_keeps_digest_file_and_request_guards() {
        let root = tempfile::tempdir().unwrap();
        let content = json!({"secret":"x".repeat(super::MAX_RANGE_TEXT_BYTES + 1)}).to_string();
        fs::write(root.path().join("values.json"), &content).unwrap();
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        for mut arguments in [json!({"path":"values.json","json_pointer":"/missing"}),
            json!({"path":"values.json","start_line":1,"end_line":1}),
            json!({"path":"values.json","search_terms":["secret"]})] {
            arguments["expected_sha256"] = json!("0".repeat(64));
            let request = parse_command_line(&arguments.to_string()).unwrap();
            let response = execute(&request, &admitted);
            assert!(!response.success);
            assert_eq!(response.exit_code, 1);
            assert!(response.stdout.is_empty());
            assert_eq!(response.stderr, "SOURCE_READ_SHA256_MISMATCH");
            assert!(response.output.get("json_recovery").is_none());
            assert_eq!(response.output["source_read_evidence"]["content_satisfied"], false);
            assert_eq!(response.output["source_read_evidence"]["source_sha256"],
                format!("{:x}", Sha256::digest(content.as_bytes())));
        }
        for (arguments, error) in [
            (json!({"path":"../values.json","json_pointer":"/missing"}), "SOURCE_READ_PATH_INVALID"),
            (json!({"path":"values.json","json_pointer":"/bad~2"}), "SOURCE_READ_JSON_POINTER_INVALID"),
            (json!({"path":"values.json","json_pointer":"/missing","expected_sha256_ref":"admitted"}), "SOURCE_READ_DIGEST_REFERENCE_UNAVAILABLE"),
        ] {
            let request = serde_json::from_value(arguments).unwrap();
            let response = execute(&request, &admitted);
            assert!(!response.success);
            assert_eq!(response.output, json!({"error_type":error}));
        }
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/missing"}"#).unwrap();
        let file = fs::OpenOptions::new().write(true).open(root.path().join("values.json")).unwrap();
        file.set_len(super::MAX_FILE_BYTES + 1).unwrap();
        assert_eq!(execute(&request, &admitted).output, json!({"error_type":"SOURCE_READ_FILE_TOO_LARGE"}));
    }

    #[test]
    fn json_recovery_envelope_fitting_preserves_original_failure() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("values.json"), r#"{"first":null,"second":false}"#).unwrap();
        let canonical = root.path().canonicalize().unwrap();
        let admitted = open_admitted_root(&canonical).unwrap();
        let ctx = ToolContext::new(canonical).with_call_id("recovery-envelope".to_string());
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/missing"}"#).unwrap();
        for keep_header in [true, false] {
            let mut expected = execute(&request, &admitted);
            if keep_header {
                expected.output["json_recovery"]["children"] = json!([]);
                expected.output["json_recovery"]["incomplete"] = json!(true);
            } else {
                expected.output.as_object_mut().unwrap().remove("json_recovery");
            }
            let body = shell_executor::preview_in_process_terminal_response(&ctx, &expected, super::TERMINATION_ORIGIN).unwrap();
            let limit = serde_json::to_vec(&body).unwrap().len();
            let response = super::fit_response_to_envelope(execute(&request, &admitted), &ctx, limit);
            assert!(!response.success);
            assert_eq!(response.exit_code, 1);
            assert!(response.stdout.is_empty());
            assert_eq!(response.stderr, "SOURCE_READ_JSON_POINTER_NOT_FOUND");
            assert_eq!(response.output, expected.output);
            let body = shell_executor::preview_in_process_terminal_response(&ctx, &response, super::TERMINATION_ORIGIN).unwrap();
            assert!(!super::result_too_large(&body, limit));
        }
    }

    #[test]
    fn json_string_pages_read_decoded_lines_like_a_file() {
        let root = tempfile::tempdir().expect("workspace");
        let patch: String = (1..=450).map(|n| format!("line {n}\n")).collect();
        let content = json!({"patch": patch, "meta": {"n": 450}}).to_string();
        fs::write(root.path().join("proposal.json"), &content).unwrap();
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let request = parse_command_line(&json!({"path":"proposal.json", "json_pointer":"/patch",
            "start_line":1, "end_line":200, "expected_sha256":hash}).to_string()).unwrap();
        let (text, metadata) = read_range(&request, root.path()).expect("first page");
        assert!(text.starts_with("1: line 1\n"));
        assert!(text.ends_with("200: line 200\n"));
        assert_eq!(metadata["mode"], "json_string_lines");
        assert_eq!(metadata["json_pointer"], "/patch");
        assert_eq!(metadata["source_sha256"], hash);
        assert_eq!(metadata["file_bytes"], json!(content.len()));
        assert_eq!(metadata["string_bytes"], json!(patch.len()));
        assert_eq!([&metadata["string_start_line"], &metadata["string_end_line"],
                    &metadata["string_total_lines"], &metadata["string_next_line"]],
                   [&json!(1), &json!(200), &json!(450), &json!(201)]);
        assert_eq!(metadata["truncated"], false);
        // String positions never claim physical file lines.
        for key in ["start_line", "end_line", "total_lines", "next_line", "at_eof"] {
            assert!(metadata.get(key).is_none(), "{key}");
        }
        let request = parse_command_line(&json!({"path":"proposal.json", "json_pointer":"/patch",
            "start_line":401, "end_line":900, "line_numbers":false}).to_string()).unwrap();
        let (text, metadata) = read_range(&request, root.path()).expect("last page");
        assert_eq!(text, (401..=450).map(|n| format!("line {n}\n")).collect::<String>());
        assert_eq!(metadata["string_end_line"], 450);
        assert_eq!(metadata["string_at_eof"], true);
        assert_eq!(metadata["truncation_reason"], "end_of_file");
    }

    #[test]
    fn json_string_pages_need_a_string_and_a_complete_range() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("values.json"), r#"{"items":[1,2],"text":"a\nb"}"#).unwrap();
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/items","start_line":1,"end_line":2}"#).unwrap();
        let response = execute(&request, &admitted);
        assert!(!response.success);
        assert!(response.stdout.is_empty());
        assert_eq!(response.stderr, "SOURCE_READ_JSON_RANGE_NOT_STRING");
        assert_eq!(response.output["json_recovery"]["parent_type"], "array");
        assert_eq!(response.output["json_recovery"]["child_count"], 2);
        for (arguments, expected) in [
            (json!({"path":"values.json","json_pointer":"/text","start_line":0,"end_line":1}), "SOURCE_READ_RANGE_INVALID"),
            (json!({"path":"values.json","json_pointer":"/text","start_line":3,"end_line":2}), "SOURCE_READ_RANGE_INVALID"),
            (json!({"path":"values.json","json_pointer":"/text","start_line":1,"end_line":2,"search_terms":["a"]}),
             "SOURCE_READ_MODE_CONFLICT"),
            (json!({"path":"values.json","json_pointer":"/text","start_line":1,"end_line":2,"context_lines":1}),
             "SOURCE_READ_MODE_CONFLICT"),
        ] {
            assert_eq!(parse_command_line(&arguments.to_string()).unwrap_err(), expected, "{arguments}");
        }
        let request = parse_command_line(
            r#"{"path":"values.json","json_pointer":"/text","start_line":2,"end_line":2,"line_numbers":false}"#,
        ).unwrap();
        assert_eq!(read_range(&request, root.path()).unwrap().0, "b");
    }

    #[test]
    fn json_string_page_envelope_fitting_updates_string_positions() {
        let root = tempfile::tempdir().unwrap();
        let text: String = (1..=120).map(|n| format!("{n:03} {}\n", "y".repeat(80))).collect();
        fs::write(root.path().join("values.json"), json!({"text": text}).to_string()).unwrap();
        let canonical = root.path().canonicalize().unwrap();
        let admitted = open_admitted_root(&canonical).unwrap();
        let ctx = ToolContext::new(canonical).with_call_id("string-envelope".to_string());
        let request = parse_command_line(
            r#"{"path":"values.json","json_pointer":"/text","start_line":1,"end_line":120}"#,
        ).unwrap();
        let full = execute(&request, &admitted);
        assert!(full.success, "{:?}", full.output);
        let body = shell_executor::preview_in_process_terminal_response(&ctx, &full, super::TERMINATION_ORIGIN).unwrap();
        let limit = serde_json::to_vec(&body).unwrap().len() - 1000;
        let response = super::fit_response_to_envelope(execute(&request, &admitted), &ctx, limit);
        assert!(response.success);
        let end = response.output["string_end_line"].as_u64().unwrap();
        assert!(end < 120);
        assert_eq!(response.output["string_next_line"], json!(end + 1));
        assert_eq!(response.output["truncated"], true);
        assert_eq!(response.output["truncation_reason"], "response_limit");
        assert_eq!(response.output["string_at_eof"], false);
        for key in ["end_line", "next_line", "at_eof", "ends_with_newline"] {
            assert!(response.output.get(key).is_none(), "{key}");
        }
    }


    #[test]
    fn bounded_search_stops_matches_and_context_at_end_line() {
        let root = tempfile::tempdir().expect("workspace");
        let content: String = (1..=20)
            .map(|n| if [3, 8, 15].contains(&n) { format!("hit {n}\n") } else { format!("line {n}\n") })
            .collect();
        fs::write(root.path().join("lines.txt"), &content).unwrap();
        let request = parse_command_line(
            r#"{"path":"lines.txt","search_terms":["hit"],"end_line":10,"context_lines":2}"#).unwrap();
        let (text, metadata) = read_range(&request, root.path()).unwrap();
        assert_eq!(metadata["search_matches"], json!([3, 8]));
        assert_eq!(text, (1..=10).map(|n| format!("{n}: {}\n", if [3, 8].contains(&n) {
            format!("hit {n}") } else { format!("line {n}") })).collect::<String>());
        assert_eq!([&metadata["end_line"], &metadata["requested_end_line"]], [&json!(10), &json!(10)]);
        assert_eq!(metadata["next_line"], serde_json::Value::Null);
        assert_eq!(metadata["at_eof"], false, "a bound below the last line is not end of file");
        let request = parse_command_line(
            r#"{"path":"lines.txt","search_terms":["hit"],"start_line":9,"end_line":99}"#).unwrap();
        let (text, metadata) = read_range(&request, root.path()).unwrap();
        assert_eq!(text, "15: hit 15\n");
        assert_eq!(metadata["at_eof"], true);
        assert_eq!(parse_command_line(
            r#"{"path":"lines.txt","search_terms":["hit"],"start_line":5,"end_line":4}"#).unwrap_err(),
            "SOURCE_READ_RANGE_INVALID");
    }

    #[test]
    fn search_excerpts_over_long_lines_around_the_first_term() {
        let root = tempfile::tempdir().expect("workspace");
        let long = format!("{}needle{}\n", "a".repeat(3000), "b".repeat(4000));
        let content = format!("short needle\n{long}tail\n");
        fs::write(root.path().join("records.jsonl"), &content).unwrap();
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        let request = parse_command_line(&json!({"path":"records.jsonl","search_terms":["needle"],
            "expected_sha256":hash}).to_string()).unwrap();
        let response = execute(&request, &admitted);
        assert!(response.success, "{:?}", response.output);
        let lines: Vec<&str> = response.stdout.lines().collect();
        assert_eq!(lines[0], "1: short needle");
        assert_eq!(lines[1], format!("2: [...]{}needle{}[...]", "a".repeat(512), "b".repeat(506)));
        assert_eq!(response.output["search_matches"], json!([1, 2]));
        assert_eq!(response.output["excerpted_lines"], json!([{"line": 2, "line_bytes": long.len(),
            "excerpt_start_byte": 2488, "excerpt_end_byte": 3512}]));
        // An excerpted page is not the complete text, so it carries no content proof.
        assert!(response.output.get("source_read_evidence").is_none());
        let request = parse_command_line(
            r#"{"path":"records.jsonl","start_line":2,"end_line":2,"line_numbers":false}"#).unwrap();
        assert_eq!(read_range(&request, root.path()).unwrap().0, long, "a range read shows the whole line");
        let exact = format!("{}needle\n", "c".repeat(super::SEARCH_LINE_EXCERPT_THRESHOLD - 7));
        fs::write(root.path().join("records.jsonl"), &exact).unwrap();
        let request = parse_command_line(r#"{"path":"records.jsonl","search_terms":["needle"]}"#).unwrap();
        let (text, metadata) = read_range(&request, root.path()).unwrap();
        assert_eq!(text, format!("1: {exact}"), "a line at the threshold is shown whole");
        assert!(metadata.get("excerpted_lines").is_none());
    }

    #[test]
    fn json_children_pages_list_complete_small_children_and_summarize_large_ones() {
        let root = tempfile::tempdir().expect("workspace");
        let mut object = serde_json::Map::new();
        for i in 0..80 {
            object.insert(format!("small{i:02}"), json!({"n": i, "tag": "x".repeat(200)}));
        }
        object.insert("big_text".to_string(), json!((1..=200).map(|n| format!("line {n}\n")).collect::<String>()));
        object.insert("big_list".to_string(), json!((0..300).collect::<Vec<_>>()));
        object.insert("a/b~c".to_string(), json!({"nested": "y".repeat(2000)}));
        let document = serde_json::Value::Object(object);
        let content = document.to_string();
        fs::write(root.path().join("values.json"), &content).unwrap();
        let hash = format!("{:x}", Sha256::digest(content.as_bytes()));
        let (mut offset, mut pages, mut seen) = (0u64, 0, Vec::new());
        loop {
            let request = parse_command_line(&json!({"path":"values.json", "json_pointer":"",
                "children_offset": offset, "expected_sha256": hash}).to_string()).unwrap();
            let (text, metadata) = read_range(&request, root.path()).expect("children page");
            pages += 1;
            assert_eq!(metadata["mode"], "json_children");
            assert_eq!(metadata["parent_type"], "object");
            assert_eq!(metadata["child_count"], 83);
            assert_eq!(metadata["children_offset"], offset);
            assert_eq!(metadata["source_sha256"], hash);
            assert!(text.len() <= super::MAX_RANGE_TEXT_BYTES);
            assert!(serde_json::to_string(&text).unwrap().len() <= super::MAX_RANGE_TEXT_BYTES);
            let lines: Vec<serde_json::Value> = text.lines().map(|line| serde_json::from_str(line).unwrap()).collect();
            assert!(!lines.is_empty());
            assert_eq!(metadata["children_returned"], json!(lines.len()));
            let summaries = lines.iter().filter(|line| line.get("value").is_none()).count();
            assert_eq!(metadata["children_summarized"], json!(summaries));
            for line in &lines {
                let key = line["key"].as_str().unwrap();
                let child = &document[key];
                let size = serde_json::to_string(child).unwrap().len();
                if let Some(value) = line.get("value") {
                    assert_eq!(value, child);
                    assert!(size <= super::JSON_CHILD_INLINE_BYTES);
                } else {
                    assert!(size > super::JSON_CHILD_INLINE_BYTES);
                    let pointer = line["json_pointer"].as_str().unwrap();
                    assert_eq!(document.pointer(pointer), Some(child));
                    assert_eq!(line["type"], super::json_value_type(child));
                    assert_eq!(line["bytes"], json!(size));
                }
                seen.push(key.to_string());
            }
            if metadata["children_at_end"] == true {
                assert_eq!(metadata["children_next_offset"], serde_json::Value::Null);
                assert_eq!(metadata["truncated"], false);
                assert_eq!(metadata["truncation_reason"], serde_json::Value::Null);
                break;
            }
            assert_eq!(metadata["truncated"], true);
            assert_eq!(metadata["truncation_reason"], "response_limit");
            let next = metadata["children_next_offset"].as_u64().unwrap();
            assert_eq!(next, offset + lines.len() as u64);
            offset = next;
        }
        assert!(pages > 1);
        let expected: Vec<String> = document.as_object().unwrap().keys().cloned().collect();
        assert_eq!(seen, expected, "every key exactly once, in key order");
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"","children_offset":0}"#).unwrap();
        let (text, _) = read_range(&request, root.path()).unwrap();
        let lines: Vec<serde_json::Value> = text.lines().map(|line| serde_json::from_str(line).unwrap()).collect();
        assert_eq!(lines[0], json!({"key":"a/b~c","json_pointer":"/a~1b~0c","type":"object",
            "bytes": serde_json::to_string(&document["a/b~c"]).unwrap().len(), "child_count":1}));
        assert_eq!(lines[1]["child_count"], 300);
        assert_eq!(lines[1]["type"], "array");
        assert_eq!(lines[2]["string_lines"], 200);
        assert_eq!(lines[2]["type"], "string");
        // A summarized string is then paged by lines, a summarized container by children.
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/big_text","start_line":199,"end_line":200}"#).unwrap();
        assert_eq!(read_range(&request, root.path()).unwrap().0, "199: line 199\n200: line 200\n");
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/big_list","children_offset":298}"#).unwrap();
        assert_eq!(read_range(&request, root.path()).unwrap().0, "{\"index\":298,\"value\":298}\n{\"index\":299,\"value\":299}\n");
    }

    #[test]
    fn json_children_need_a_container_and_a_valid_cursor() {
        let root = tempfile::tempdir().expect("workspace");
        fs::write(root.path().join("values.json"), r#"{"items":[1,2,3],"text":"a\nb","n":7,"empty":{}}"#).unwrap();
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        for arguments in [
            json!({"path":"values.json","children_offset":0}),
            json!({"path":"values.json","start_line":1,"end_line":2,"children_offset":0}),
            json!({"path":"values.json","json_pointer":"/items","start_line":1,"end_line":2,"children_offset":0}),
            json!({"path":"values.json","json_pointer":"/items","children_offset":0,"line_numbers":true}),
            json!({"path":"values.json","json_pointer":"/items","children_offset":0,"search_terms":["1"]}),
            json!({"path":"values.json","json_pointer":"/items","children_offset":0,"context_lines":1}),
        ] {
            assert_eq!(parse_command_line(&arguments.to_string()).unwrap_err(), "SOURCE_READ_MODE_CONFLICT", "{arguments}");
        }
        // Positional arguments keep their arity; the cursor is a named field only.
        assert!(parse_command_line(r#"["values.json",1,2,null,true,null,null,null,null,0]"#).is_err());
        let page = |arguments: serde_json::Value| execute(&parse_command_line(&arguments.to_string()).unwrap(), &admitted);
        let response = page(json!({"path":"values.json","json_pointer":"/items","children_offset":1}));
        assert!(response.success, "{:?}", response.output);
        assert_eq!(response.stdout, "{\"index\":1,\"value\":2}\n{\"index\":2,\"value\":3}\n");
        assert_eq!(response.output["children_at_end"], true);
        assert_eq!(response.output["children_returned"], 2);
        for (pointer, offset, count) in [("/items", 3, 3), ("/empty", 0, 0)] {
            let response = page(json!({"path":"values.json","json_pointer":pointer,"children_offset":offset}));
            assert!(response.success);
            assert!(response.stdout.is_empty());
            assert_eq!(response.output["children_returned"], 0);
            assert_eq!(response.output["child_count"], count);
            assert_eq!(response.output["children_at_end"], true);
        }
        let response = page(json!({"path":"values.json","json_pointer":"/items","children_offset":4}));
        assert_eq!(response.stderr, "SOURCE_READ_RANGE_OUT_OF_BOUNDS");
        let response = page(json!({"path":"values.json","json_pointer":"/text","children_offset":0}));
        assert!(!response.success);
        assert!(response.stdout.is_empty());
        assert_eq!(response.stderr, "SOURCE_READ_JSON_CHILDREN_NOT_CONTAINER");
        assert_eq!(response.output["json_recovery"]["string_page"],
            json!({"json_pointer":"/text","start_line":1,"end_line":2}));
        assert!(response.output["json_recovery"].get("children_page").is_none());
        let response = page(json!({"path":"values.json","json_pointer":"/n","children_offset":0}));
        assert_eq!(response.stderr, "SOURCE_READ_JSON_CHILDREN_NOT_CONTAINER");
        assert_eq!(response.output["json_recovery"]["parent_type"], "number");
        let response = page(json!({"path":"values.json","json_pointer":"/missing","children_offset":0}));
        assert_eq!(response.stderr, "SOURCE_READ_JSON_POINTER_NOT_FOUND");
        assert_eq!(response.output["json_recovery"]["children_page"], json!({"json_pointer":"","children_offset":0}));
    }

    #[test]
    fn json_children_show_keys_longer_than_recovery_hints_and_omit_unusable_ones() {
        let root = tempfile::tempdir().expect("workspace");
        let long = "k".repeat(300);
        let huge = "h".repeat(super::JSON_CHILD_KEY_BYTES + 1);
        let document = json!({"counts": {long.clone(): 3, huge.clone(): {"wide": "w".repeat(1100)}, "s/l~": "z".repeat(1500)}});
        fs::write(root.path().join("values.json"), document.to_string()).unwrap();
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        // The failure hints skip keys over the recovery bound but still name the page.
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/counts/missing"}"#).unwrap();
        let recovery = execute(&request, &admitted).output["json_recovery"].clone();
        assert_eq!(recovery["child_count"], 3);
        assert_eq!(recovery["children"], json!([{"key":"s/l~","json_pointer":"/counts/s~1l~0","type":"string"}]));
        assert_eq!(recovery["incomplete"], true);
        assert_eq!(recovery["children_page"], json!({"json_pointer":"/counts","children_offset":0}));
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/counts","children_offset":0}"#).unwrap();
        let (text, metadata) = read_range(&request, root.path()).unwrap();
        let lines: Vec<serde_json::Value> = text.lines().map(|line| serde_json::from_str(line).unwrap()).collect();
        assert_eq!(lines.len(), 3);
        assert_eq!(lines[0], json!({"key_omitted":true,"key_bytes":huge.len(),"type":"object",
            "bytes": serde_json::to_string(&document["counts"][&huge]).unwrap().len(), "child_count":1}));
        assert_eq!(lines[1], json!({"key":long,"value":3}));
        assert_eq!(lines[2], json!({"key":"s/l~","json_pointer":"/counts/s~1l~0","type":"string","bytes":1502,"string_lines":1}));
        assert_eq!(metadata["children_summarized"], 2);
        assert_eq!(metadata["children_at_end"], true);
        // Field order is fixed: the key comes first in every line.
        assert!(text.lines().all(|line| line.starts_with("{\"key")));
    }

    #[test]
    fn json_recovery_names_the_children_page_of_a_container() {
        let root = tempfile::tempdir().expect("workspace");
        let big: serde_json::Map<String, serde_json::Value> =
            (0..100).map(|i| (format!("k{i:03}"), json!("v".repeat(200)))).collect();
        let document = json!({"big": big, "small": 1});
        fs::write(root.path().join("values.json"), document.to_string()).unwrap();
        let admitted = open_admitted_root(&root.path().canonicalize().unwrap()).unwrap();
        for (arguments, error, parent) in [
            (json!({"path":"values.json","json_pointer":"/big"}), "SOURCE_READ_JSON_VALUE_TOO_LARGE", "/big"),
            (json!({"path":"values.json","json_pointer":"/big/k999"}), "SOURCE_READ_JSON_POINTER_NOT_FOUND", "/big"),
            (json!({"path":"values.json","json_pointer":"/big","start_line":1,"end_line":2}), "SOURCE_READ_JSON_RANGE_NOT_STRING", "/big"),
            (json!({"path":"values.json","start_line":1,"end_line":1}), "SOURCE_READ_LINE_TOO_LONG", ""),
        ] {
            let response = execute(&parse_command_line(&arguments.to_string()).unwrap(), &admitted);
            assert!(!response.success);
            assert_eq!(response.stderr, error, "{arguments}");
            let recovery = &response.output["json_recovery"];
            assert_eq!(recovery["children_page"], json!({"json_pointer":parent,"children_offset":0}), "{arguments}");
            assert!(serde_json::to_vec(recovery).unwrap().len() <= super::MAX_JSON_RECOVERY_BYTES);
            assert!(!response.output.to_string().contains("vvvv"));
            let page = json!({"path":"values.json","json_pointer":parent,"children_offset":0});
            let response = execute(&parse_command_line(&page.to_string()).unwrap(), &admitted);
            assert!(response.success, "{:?}", response.output);
            assert_eq!(response.output["child_count"], if parent.is_empty() { 2 } else { 100 });
        }
    }

    #[test]
    fn json_children_envelope_fitting_moves_the_cursor_by_whole_children() {
        let root = tempfile::tempdir().unwrap();
        let items: Vec<serde_json::Value> = (0..50).map(|i| json!({"id": i, "label": format!("{i:03}{}", "q".repeat(120))})).collect();
        fs::write(root.path().join("values.json"), json!({"items": items}).to_string()).unwrap();
        let canonical = root.path().canonicalize().unwrap();
        let admitted = open_admitted_root(&canonical).unwrap();
        let ctx = ToolContext::new(canonical).with_call_id("children-envelope".to_string());
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":"/items","children_offset":5}"#).unwrap();
        let full = execute(&request, &admitted);
        assert!(full.success, "{:?}", full.output);
        assert_eq!(full.output["children_at_end"], true);
        let body = shell_executor::preview_in_process_terminal_response(&ctx, &full, super::TERMINATION_ORIGIN).unwrap();
        let limit = serde_json::to_vec(&body).unwrap().len() - 1000;
        let response = super::fit_response_to_envelope(execute(&request, &admitted), &ctx, limit);
        assert!(response.success);
        let returned = response.output["children_returned"].as_u64().unwrap();
        assert!(returned > 0 && returned < 45);
        assert_eq!(response.stdout.lines().count() as u64, returned);
        let last: serde_json::Value = serde_json::from_str(response.stdout.lines().last().unwrap()).unwrap();
        assert_eq!(last["index"], json!(5 + returned - 1));
        assert_eq!(response.output["children_next_offset"], json!(5 + returned));
        assert_eq!(response.output["children_at_end"], false);
        assert_eq!(response.output["truncated"], true);
        assert_eq!(response.output["truncation_reason"], "response_limit");
        assert_eq!(response.output["children_summarized"], 0);
        let body = shell_executor::preview_in_process_terminal_response(&ctx, &response, super::TERMINATION_ORIGIN).unwrap();
        assert!(!super::result_too_large(&body, limit));
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
            "x".repeat(super::MAX_JSON_TEXT_BYTES + 1),
            "\"".repeat(super::MAX_JSON_TEXT_BYTES / 3),
            "界".repeat(super::MAX_JSON_TEXT_BYTES / 3),
        ] {
            fs::write(root.path().join("values.json"), json!({"value":value, "secret":"do-not-leak"}).to_string()).unwrap();
            for pointer in ["/value", ""] {
                let request = parse_command_line(&json!({"path":"values.json", "json_pointer":pointer}).to_string()).unwrap();
                let response = execute(&request, &admitted);
                assert!(!response.success);
                assert!(response.stdout.is_empty());
                assert_eq!(response.stderr, "SOURCE_READ_JSON_VALUE_TOO_LARGE");
                assert_eq!(response.output["error_type"], "SOURCE_READ_JSON_VALUE_TOO_LARGE");
                // Navigation, never the value: no partial text and no sibling value leaks.
                assert!(!response.output.to_string().contains("do-not-leak"));
                let recovery = &response.output["json_recovery"];
                assert_eq!(recovery["parent_pointer"], pointer);
                if pointer.is_empty() {
                    assert_eq!(recovery["parent_type"], "object");
                    assert_eq!(recovery["child_count"], 2);
                } else {
                    assert_eq!(recovery["parent_type"], "string");
                    assert_eq!(recovery["string_bytes"], json!(value.len()));
                    assert_eq!(recovery["string_page"], json!({"json_pointer":"/value","start_line":1,"end_line":1}));
                }
            }
        }
        let value = "x".repeat(super::MAX_JSON_TEXT_BYTES - 6);
        fs::write(root.path().join("values.json"), json!(value).to_string()).unwrap();
        let request = parse_command_line(r#"{"path":"values.json","json_pointer":""}"#).unwrap();
        let (text, _) = read_range_from_root(&request, &admitted).expect("exact escaped text budget");
        assert_eq!(serde_json::to_string(&text).unwrap().len(), super::MAX_JSON_TEXT_BYTES);
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
        assert!(execute(&request, &admitted).output.get("json_recovery").is_none());
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
            (json!({"path":"capsule.json","start_line":1,"end_line":1}), Some("SOURCE_READ_LINE_TOO_LONG")),
        ].into_iter().enumerate() {
            let (_, max_result_bytes) = parse_command_line(&arguments.to_string()).unwrap().byte_limits();
            let call_id = format!("json-receipt-{index}");
            let ctx = ToolContext::new(canonical.clone()).with_source_read_root(Some(admitted.clone()))
                .with_call_id(call_id.clone());
            let output = SourceReadHandler.handle(ToolCall {
                tool_name:"source_read".to_string(), call_id,
                payload:ToolPayload::Function { arguments },
            }, ctx).await.expect("projection receipt");
            assert_eq!(output.success, Some(error.is_none()));
            assert!(!super::result_too_large(&output.body, max_result_bytes));
            if let Some(error) = error {
                assert_eq!(output.body["error_type"], error);
                assert_eq!(output.body["stdout"], "");
                assert_eq!(output.body["terminal_receipt"]["terminal_state"], "failed");
                assert_eq!(output.body["terminal_receipt"]["exit_code"], 1);
                if matches!(error, "SOURCE_READ_JSON_POINTER_NOT_FOUND" | "SOURCE_READ_LINE_TOO_LONG") {
                    assert_eq!(output.body["json_recovery"]["source_sha256"], hash);
                    assert_eq!(output.body["json_recovery"]["parent_pointer"], "");
                    assert_eq!(output.body["json_recovery"]["child_count"], 2);
                    assert_eq!(output.body["json_recovery"]["incomplete"], false);
                } else if error == "SOURCE_READ_JSON_VALUE_TOO_LARGE" {
                    assert_eq!(output.body["json_recovery"]["parent_pointer"], "/large");
                    assert_eq!(output.body["json_recovery"]["parent_type"], "string");
                    assert_eq!(output.body["json_recovery"]["string_page"],
                               json!({"json_pointer":"/large","start_line":1,"end_line":1}));
                } else {
                    assert!(output.body.get("json_recovery").is_none());
                }
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
    async fn mapped_source_read_uses_existing_sha_range_json_search_and_receipts() {
        let fixture = MappedFixture::new();
        let hash = fixture.hash();
        let path = fixture.binding.logical_path.clone();
        let requests = [
            ("range", json!({"path":path,"start_line":2,"end_line":3,
                "expected_sha256":hash}), "2:   \"state\": {\"ready\": true},\n3:   \"lane\": \"mapped\"\n"),
            ("json", json!({"path":path,"json_pointer":"/state/ready",
                "expected_sha256":hash}), "true"),
            ("search", json!({"path":path,"search_terms":["mapped"],
                "expected_sha256":hash}), "3:   \"lane\": \"mapped\"\n"),
        ];
        for (id, arguments, text) in requests {
            let ctx = fixture.context().with_call_id(format!("mapped-{id}"));
            assert_eq!(ctx.child().mapped_source_reads().unwrap().1, std::slice::from_ref(&fixture.binding));
            let output = SourceReadHandler.handle(ToolCall {
                tool_name:"source_read".to_string(), call_id:format!("mapped-{id}"),
                payload:ToolPayload::Function { arguments },
            }, ctx).await.expect("mapped terminal receipt");
            assert_eq!(output.success, Some(true), "{id}: {}", output.body);
            assert_eq!(output.body["stdout"], text);
            assert_eq!(output.body["path"], path);
            assert_eq!(output.body["source_sha256"], hash);
            assert_eq!(output.body["terminal_receipt"]["terminal_state"], "completed");
            assert!(!super::result_too_large(&output.body,
                if id == "search" { super::MAX_RESULT_BYTES } else { super::MAX_RANGE_RESULT_BYTES }));
            let receipt_path = output.body["terminal_receipt_path"].as_str().unwrap();
            let durable: serde_json::Value = serde_json::from_slice(&fs::read(receipt_path).unwrap()).unwrap();
            assert_eq!(durable, output.body["terminal_receipt"]);
        }
        let mismatch = fixture.execute(json!({"path":path,"json_pointer":"/state/ready",
            "expected_sha256":"0".repeat(64)}), Some(std::slice::from_ref(&fixture.binding)));
        assert!(!mismatch.success);
        assert_eq!(mismatch.stdout, "");
        assert_eq!(mismatch.output["error_type"], "SOURCE_READ_SHA256_MISMATCH");
        assert_eq!(mismatch.output["source_read_evidence"]["source_sha256"], hash);
        assert_eq!(mismatch.output["source_read_evidence"]["content_satisfied"], false);
    }

    #[cfg(unix)]
    #[test]
    fn mapped_source_read_never_grants_siblings_aliases_or_model_supplied_bindings() {
        use std::os::unix::fs::symlink;
        let fixture = MappedFixture::new();
        let request = json!({"path":fixture.binding.logical_path,"json_pointer":""});
        for bindings in [None, Some([].as_slice())] {
            let denied = fixture.execute(request.clone(), bindings);
            assert!(!denied.success);
            assert_eq!(denied.stdout, "");
            assert_eq!(denied.output["error_type"], "SOURCE_READ_TARGET_INVALID");
            assert!(denied.output.get("json_recovery").is_none());
        }
        fs::write(fixture.physical_root.join("sibling.json"), fixture.content).unwrap();
        symlink(&fixture.physical_root, fixture.repo_root.join("other-alias")).unwrap();
        for path in ["output/runtime/dcf_v2/sibling.json", "other-alias/CURRENT.json"] {
            let denied = fixture.execute(json!({"path":path,"json_pointer":""}),
                Some(std::slice::from_ref(&fixture.binding)));
            assert!(!denied.success);
            assert_eq!(denied.stdout, "");
            assert_eq!(denied.output["error_type"], "SOURCE_READ_TARGET_INVALID");
            assert!(denied.output.get("json_recovery").is_none());
        }
        for path in [fixture.physical_root.join("CURRENT.json"),
            fixture.repo_root.join(&fixture.binding.logical_path)] {
            assert_eq!(parse_command_line(&json!({"path":path,"json_pointer":""}).to_string())
                .unwrap_err(), "SOURCE_READ_PATH_INVALID");
        }
        let mut supplied = request;
        supplied["mapped_source_reads"] = json!([fixture.binding]);
        assert!(parse_command_line(&supplied.to_string()).unwrap_err()
            .starts_with("SOURCE_READ_ARGUMENTS_INVALID:"));
    }

    #[cfg(unix)]
    #[test]
    fn mapped_source_read_rejects_binding_tamper_contract_change_and_alias_rebind() {
        use std::os::unix::fs::symlink;
        let fixture = MappedFixture::new();
        let request = json!({"path":fixture.binding.logical_path,"json_pointer":""});
        let mut tampered = fixture.binding.clone();
        tampered.physical_path = fixture.physical_root.join("other.json");
        let denied = fixture.execute(request.clone(), Some(&[tampered]));
        assert!(!denied.success);
        assert!(denied.stderr.starts_with("MAPPED_SOURCE_READ_BINDING_CHANGED:"));
        assert_eq!(denied.stdout, "");
        let mut wrong_request = fixture.binding.clone();
        wrong_request.requested_logical_path = "other-alias/CURRENT.json".to_string();
        let denied = fixture.execute(request.clone(), Some(&[wrong_request]));
        assert_eq!(denied.stderr, "MAPPED_SOURCE_READ_REQUEST_NOT_EXACT");
        let denied = fixture.execute(request.clone(), Some(&[fixture.binding.clone(), fixture.binding.clone()]));
        assert_eq!(denied.stderr, "MAPPED_SOURCE_READ_BINDING_AMBIGUOUS");
        let alias = fixture.repo_root.join("output/runtime");
        fs::remove_file(&alias).unwrap();
        symlink(fixture.physical_root.parent().unwrap(), &alias).unwrap();
        let denied = fixture.execute(request.clone(), Some(std::slice::from_ref(&fixture.binding)));
        assert!(!denied.success);
        assert!(denied.stderr.starts_with("MAPPED_SOURCE_READ_BINDING_CHANGED:"));
        assert_eq!(denied.stdout, "");

        let fixture = MappedFixture::new();
        let request = json!({"path":fixture.binding.logical_path,"json_pointer":"",
            "expected_sha256":"0".repeat(64)});
        fs::write(fixture.repo_root.join("config/contracts/dcf_v2_contract.json"), b"{}\n").unwrap();
        let denied = fixture.execute(request, Some(std::slice::from_ref(&fixture.binding)));
        assert!(!denied.success);
        assert!(denied.stderr.starts_with("MAPPED_SOURCE_READ_CONTRACT_SHA_MISMATCH:"));
        assert_eq!(denied.stdout, "");
        assert!(denied.output.get("source_read_evidence").is_none());
        assert!(denied.output.get("json_recovery").is_none());
    }

    #[cfg(unix)]
    #[test]
    fn mapped_source_read_preserves_existing_file_and_projection_limits() {
        let fixture = MappedFixture::new();
        let request = json!({"path":fixture.binding.logical_path,"json_pointer":""});
        fs::write(fixture.physical_root.join("CURRENT.json"),
            json!("x".repeat(super::MAX_RANGE_TEXT_BYTES)).to_string()).unwrap();
        let oversized_projection = fixture.execute(request.clone(), Some(std::slice::from_ref(&fixture.binding)));
        assert!(!oversized_projection.success);
        assert_eq!(oversized_projection.stdout, "");
        assert_eq!(oversized_projection.output["error_type"], "SOURCE_READ_JSON_VALUE_TOO_LARGE");
        let file = fs::OpenOptions::new().write(true).open(fixture.physical_root.join("CURRENT.json")).unwrap();
        file.set_len(super::MAX_FILE_BYTES + 1).unwrap();
        let oversized_file = fixture.execute(request, Some(std::slice::from_ref(&fixture.binding)));
        assert!(!oversized_file.success);
        assert_eq!(oversized_file.stdout, "");
        assert_eq!(oversized_file.output["error_type"], "SOURCE_READ_FILE_TOO_LARGE");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn mapped_source_read_bridge_is_native_context_only_and_old_bridge_stays_denied() {
        use crate::command_run::{execute_async_value_with_mapped_source_read_admission,
            execute_async_value_with_source_read_admission};
        use crate::runtime::tool::CancellationToken;
        use std::collections::BTreeSet;
        let fixture = MappedFixture::new();
        let mut arguments = json!({"execution_id":"mapped-bridge", "commands":[{
            "command_type":"source_read", "step":1,
            "command_line":json!({"path":fixture.binding.logical_path,"json_pointer":"/state/ready",
                "expected_sha256":fixture.hash()}).to_string(),
        }]});
        let root = Arc::new(open_admitted_root(&fixture.repo_root).unwrap());
        let allowed = Some(BTreeSet::from(["source_read".to_string()]));
        let mapped = execute_async_value_with_mapped_source_read_admission(
            arguments.clone(), fixture.repo_root.clone(), allowed.clone(), None, true,
            CancellationToken::new(), Some(root.clone()),
            Some((fixture.repo_root.clone(), vec![fixture.binding.clone()])), None,
        ).await;
        assert_eq!(mapped["results"][0]["success"], true, "{mapped}");
        assert_eq!(mapped["results"][0]["output"]["stdout"], "true");
        assert_eq!(mapped["results"][0]["output"]["terminal_receipt"]["terminal_state"], "completed");
        arguments["execution_id"] = json!("ordinary-bridge");
        let ordinary = execute_async_value_with_source_read_admission(
            arguments, fixture.repo_root.clone(), allowed, None, true,
            CancellationToken::new(), Some(root), None,
        ).await;
        assert_eq!(ordinary["results"][0]["success"], false, "{ordinary}");
        assert_eq!(ordinary["results"][0]["output"]["stdout"], "");
        assert_eq!(ordinary["results"][0]["output"]["error_type"], "SOURCE_READ_TARGET_INVALID");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn json_projection_that_cannot_fit_receipt_envelope_fails_atomically() {
        let root = tempfile::tempdir().expect("workspace");
        let path = format!("{}/{}/{}.txt", "\u{1}".repeat(190), "\u{2}".repeat(190), "\u{3}".repeat(125));
        let source = root.path().join(&path);
        fs::create_dir_all(source.parent().unwrap()).unwrap();
        let key = "\u{4}".repeat(1200);
        let selected = json!(["x".repeat(super::MAX_JSON_TEXT_BYTES - 64), null, false]);
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
        assert!(serde_json::to_string(&raw.stdout).unwrap().len() <= super::MAX_JSON_TEXT_BYTES);
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
