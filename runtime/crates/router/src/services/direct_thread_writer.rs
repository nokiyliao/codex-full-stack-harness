use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fmt;
use std::fs;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

const REQUEST_SCHEMA: &str = "tura_codex_thread_writer_request_v1";
const RESPONSE_SCHEMA: &str = "tura_codex_thread_writer_response_v1";
const PROTOCOL_VERSION: &str = "tura_codex_thread_writer_v1";
const MAX_LINE_BYTES: usize = 1024 * 1024;
const MAX_ENDPOINT_BYTES: u64 = 16 * 1024;
const REQUIRED_OPERATIONS: [&str; 3] = ["capabilities", "read_thread", "send_message_to_thread"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum DirectThreadWriterError {
    Configuration(String),
    NoCapableEndpoint(Vec<String>),
    RequestRejected { code: String, message: String },
    Protocol(String),
    DeliveryUnsettled { endpoint_pid: u32, detail: String },
}

impl fmt::Display for DirectThreadWriterError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Configuration(detail) => {
                write!(formatter, "DIRECT_THREAD_WRITER_CONFIG:{detail}")
            }
            Self::NoCapableEndpoint(details) => write!(
                formatter,
                "DIRECT_THREAD_WRITER_ENDPOINT_UNAVAILABLE:{}",
                details.join("|")
            ),
            Self::RequestRejected { code, message } => {
                write!(formatter, "DIRECT_THREAD_WRITER_REJECTED:{code}:{message}")
            }
            Self::Protocol(detail) => write!(formatter, "DIRECT_THREAD_WRITER_PROTOCOL:{detail}"),
            Self::DeliveryUnsettled {
                endpoint_pid,
                detail,
            } => write!(
                formatter,
                "DIRECT_THREAD_WRITER_DELIVERY_UNSETTLED_NO_RETRY:{endpoint_pid}:{detail}"
            ),
        }
    }
}

impl std::error::Error for DirectThreadWriterError {}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct EndpointRecord {
    socket_path: PathBuf,
    pid: u32,
    app_tools_pipe_sha256: String,
    app_tools_pipe_basename: String,
    protocol_version: String,
    created_at_ms: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WriterResponse {
    schema_version: String,
    request_id: String,
    ok: bool,
    #[serde(default)]
    result: Option<Value>,
    #[serde(default)]
    error: Option<WriterResponseError>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WriterResponseError {
    code: String,
    message: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CapabilityResult {
    protocol_version: String,
    operations: Vec<String>,
    socket_path: PathBuf,
    pid: u32,
}

#[derive(Debug, Serialize)]
struct CapabilitiesRequest<'a> {
    schema_version: &'static str,
    request_id: &'a str,
    operation: &'static str,
}

#[derive(Debug, Serialize)]
struct ReadThreadRequest<'a> {
    schema_version: &'static str,
    request_id: &'a str,
    operation: &'static str,
    target_thread_id: &'a str,
    turn_id: &'a str,
    call_id: &'a str,
}

#[derive(Debug, Serialize)]
struct SendMessageRequest<'a> {
    schema_version: &'static str,
    request_id: &'a str,
    operation: &'static str,
    target_thread_id: &'a str,
    turn_id: &'a str,
    call_id: &'a str,
    message: &'a str,
    message_sha256: String,
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct DirectThreadWriterResult {
    pub endpoint_pid: u32,
    pub result: Value,
}

#[derive(Debug, Clone)]
pub(crate) struct DirectThreadWriterClient {
    endpoint_directory: PathBuf,
    timeout: Duration,
}

impl DirectThreadWriterClient {
    pub(crate) fn from_environment() -> Result<Self, DirectThreadWriterError> {
        let home = std::env::var_os("TURA_HOME").ok_or_else(|| {
            DirectThreadWriterError::Configuration("TURA_HOME_REQUIRED".to_string())
        })?;
        let home = PathBuf::from(home);
        if !home.is_absolute() {
            return Err(DirectThreadWriterError::Configuration(
                "TURA_HOME_NOT_ABSOLUTE".to_string(),
            ));
        }
        let (endpoint_directory, require_home_containment) =
            match std::env::var_os("TURA_CODEX_THREAD_WRITER_DIR") {
                Some(value) if !value.is_empty() => (PathBuf::from(value), false),
                _ => (home.join(".tura/codex-thread-writer"), true),
            };
        if !endpoint_directory.is_absolute() {
            return Err(DirectThreadWriterError::Configuration(
                "ENDPOINT_DIRECTORY_NOT_ABSOLUTE".to_string(),
            ));
        }
        let canonical_home = fs::canonicalize(&home).map_err(|error| {
            DirectThreadWriterError::Configuration(format!("TURA_HOME_CANONICAL:{error}"))
        })?;
        let canonical_endpoint_directory =
            fs::canonicalize(&endpoint_directory).map_err(|error| {
                DirectThreadWriterError::Configuration(format!(
                    "ENDPOINT_DIRECTORY_CANONICAL:{error}"
                ))
            })?;
        if require_home_containment && !canonical_endpoint_directory.starts_with(&canonical_home) {
            return Err(DirectThreadWriterError::Configuration(
                "ENDPOINT_DIRECTORY_OUTSIDE_TURA_HOME".to_string(),
            ));
        }
        Ok(Self::new(
            canonical_endpoint_directory,
            Duration::from_secs(5),
        ))
    }

    pub(crate) fn new(endpoint_directory: PathBuf, timeout: Duration) -> Self {
        Self {
            endpoint_directory,
            timeout,
        }
    }

    pub(crate) async fn preflight(
        &self,
        identity: &str,
    ) -> Result<Vec<u32>, DirectThreadWriterError> {
        validate_identifier(identity, "request_id")?;
        let (endpoints, failures) = self.capable_endpoints(identity).await;
        if endpoints.is_empty() {
            return Err(DirectThreadWriterError::NoCapableEndpoint(failures));
        }
        Ok(endpoints.into_iter().map(|endpoint| endpoint.pid).collect())
    }

    pub(crate) async fn read_thread(
        &self,
        request_id: &str,
        target_thread_id: &str,
        turn_id: &str,
        call_id: &str,
    ) -> Result<DirectThreadWriterResult, DirectThreadWriterError> {
        for (value, name) in [
            (request_id, "request_id"),
            (target_thread_id, "target_thread_id"),
            (turn_id, "turn_id"),
            (call_id, "call_id"),
        ] {
            validate_identifier(value, name)?;
        }
        let request = ReadThreadRequest {
            schema_version: REQUEST_SCHEMA,
            request_id,
            operation: "read_thread",
            target_thread_id,
            turn_id,
            call_id,
        };
        let payload = encode_request(&request)?;
        let (endpoints, mut failures) = self.capable_endpoints(request_id).await;
        for endpoint in endpoints {
            match self
                .call_endpoint(&endpoint, &payload, request_id, false)
                .await
            {
                Ok(result) => return validate_tool_success(endpoint.pid, result),
                Err(error) => failures.push(format!("{}:{error}", endpoint.pid)),
            }
        }
        Err(DirectThreadWriterError::NoCapableEndpoint(failures))
    }

    pub(crate) async fn send_message_to_thread(
        &self,
        request_id: &str,
        target_thread_id: &str,
        turn_id: &str,
        message: &str,
    ) -> Result<DirectThreadWriterResult, DirectThreadWriterError> {
        for (value, name) in [
            (request_id, "request_id"),
            (target_thread_id, "target_thread_id"),
            (turn_id, "turn_id"),
        ] {
            validate_identifier(value, name)?;
        }
        if message.is_empty() || message.len() > MAX_LINE_BYTES / 2 {
            return Err(DirectThreadWriterError::Configuration(
                "MESSAGE_SIZE_INVALID".to_string(),
            ));
        }
        let request = SendMessageRequest {
            schema_version: REQUEST_SCHEMA,
            request_id,
            operation: "send_message_to_thread",
            target_thread_id,
            turn_id,
            call_id: request_id,
            message,
            message_sha256: sha256_hex(message.as_bytes()),
        };
        let payload = encode_request(&request)?;
        let (endpoints, mut failures) = self.capable_endpoints(request_id).await;
        for endpoint in endpoints {
            match self
                .call_endpoint(&endpoint, &payload, request_id, true)
                .await
            {
                Ok(result) => return validate_tool_success(endpoint.pid, result),
                Err(CallFailure::BeforeWrite(detail)) => {
                    failures.push(format!("{}:{detail}", endpoint.pid));
                }
                Err(CallFailure::AfterWrite(detail)) => {
                    return Err(DirectThreadWriterError::DeliveryUnsettled {
                        endpoint_pid: endpoint.pid,
                        detail,
                    });
                }
                Err(CallFailure::Rejected { code, message }) => {
                    return Err(DirectThreadWriterError::RequestRejected { code, message });
                }
            }
        }
        Err(DirectThreadWriterError::NoCapableEndpoint(failures))
    }

    async fn capable_endpoints(&self, identity: &str) -> (Vec<EndpointRecord>, Vec<String>) {
        let (candidates, mut failures) = match discover_endpoints(&self.endpoint_directory) {
            Ok(value) => value,
            Err(error) => return (Vec::new(), vec![error.to_string()]),
        };
        let mut capable = Vec::new();
        for endpoint in candidates {
            let request_id = format!(
                "preflight-{}-{}",
                &sha256_hex(identity.as_bytes())[..16],
                endpoint.pid
            );
            let request = CapabilitiesRequest {
                schema_version: REQUEST_SCHEMA,
                request_id: &request_id,
                operation: "capabilities",
            };
            let payload = match encode_request(&request) {
                Ok(value) => value,
                Err(error) => {
                    failures.push(format!("{}:{error}", endpoint.pid));
                    continue;
                }
            };
            match self
                .call_endpoint(&endpoint, &payload, &request_id, false)
                .await
            {
                Ok(result) => match validate_capabilities(&endpoint, result) {
                    Ok(()) => capable.push(endpoint),
                    Err(error) => failures.push(format!("{}:{error}", endpoint.pid)),
                },
                Err(error) => failures.push(format!("{}:{error}", endpoint.pid)),
            }
        }
        (capable, failures)
    }

    async fn call_endpoint(
        &self,
        endpoint: &EndpointRecord,
        payload: &[u8],
        request_id: &str,
        effectful: bool,
    ) -> Result<Value, CallFailure> {
        let stream = tokio::time::timeout(self.timeout, UnixStream::connect(&endpoint.socket_path))
            .await
            .map_err(|_| CallFailure::BeforeWrite("CONNECT_TIMEOUT".to_string()))?
            .map_err(|error| CallFailure::BeforeWrite(format!("CONNECT:{error}")))?;
        let mut stream = BufReader::new(stream);
        let mut written = 0;
        while written < payload.len() {
            match tokio::time::timeout(self.timeout, stream.get_mut().write(&payload[written..]))
                .await
            {
                Ok(Ok(0)) => {
                    return Err(call_failure(effectful, written, "WRITE_ZERO".to_string()));
                }
                Ok(Ok(count)) => written += count,
                Ok(Err(error)) => {
                    return Err(call_failure(effectful, written, format!("WRITE:{error}")));
                }
                Err(_) => {
                    return Err(call_failure(
                        effectful,
                        written,
                        "WRITE_TIMEOUT".to_string(),
                    ));
                }
            }
        }
        let mut line = Vec::new();
        let mut bounded = stream.take((MAX_LINE_BYTES + 1) as u64);
        let read = tokio::time::timeout(self.timeout, bounded.read_until(b'\n', &mut line))
            .await
            .map_err(|_| call_failure(effectful, written, "RESPONSE_TIMEOUT".to_string()))?
            .map_err(|error| call_failure(effectful, written, format!("READ:{error}")))?;
        if read == 0 {
            return Err(call_failure(effectful, written, "RESPONSE_EOF".to_string()));
        }
        if line.len() > MAX_LINE_BYTES || line.last() != Some(&b'\n') {
            return Err(call_failure(
                effectful,
                written,
                "RESPONSE_SIZE_OR_FRAMING_INVALID".to_string(),
            ));
        }
        let response: WriterResponse = serde_json::from_slice(&line[..line.len() - 1])
            .map_err(|error| call_failure(effectful, written, format!("RESPONSE_JSON:{error}")))?;
        decode_response(response, request_id).map_err(|error| match error {
            DirectThreadWriterError::RequestRejected { code, message } => {
                CallFailure::Rejected { code, message }
            }
            other => call_failure(effectful, written, other.to_string()),
        })
    }
}

#[derive(Debug)]
enum CallFailure {
    BeforeWrite(String),
    AfterWrite(String),
    Rejected { code: String, message: String },
}

impl fmt::Display for CallFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BeforeWrite(detail) => write!(formatter, "BEFORE_WRITE:{detail}"),
            Self::AfterWrite(detail) => write!(formatter, "AFTER_WRITE:{detail}"),
            Self::Rejected { code, message } => write!(formatter, "REJECTED:{code}:{message}"),
        }
    }
}

fn call_failure(effectful: bool, written: usize, detail: String) -> CallFailure {
    if effectful && written > 0 {
        CallFailure::AfterWrite(detail)
    } else {
        CallFailure::BeforeWrite(detail)
    }
}

fn discover_endpoints(
    directory: &Path,
) -> Result<(Vec<EndpointRecord>, Vec<String>), DirectThreadWriterError> {
    let metadata = fs::symlink_metadata(directory).map_err(|error| {
        DirectThreadWriterError::Configuration(format!("ENDPOINT_DIRECTORY:{error}"))
    })?;
    if !metadata.is_dir() || metadata.permissions().mode() & 0o777 != 0o700 {
        return Err(DirectThreadWriterError::Configuration(
            "ENDPOINT_DIRECTORY_NOT_SEALED".to_string(),
        ));
    }
    let owner = metadata.uid();
    let canonical_directory = fs::canonicalize(directory).map_err(|error| {
        DirectThreadWriterError::Configuration(format!("ENDPOINT_DIRECTORY_CANONICAL:{error}"))
    })?;
    let mut endpoints = Vec::new();
    let mut failures = Vec::new();
    for entry in fs::read_dir(directory).map_err(|error| {
        DirectThreadWriterError::Configuration(format!("ENDPOINT_DIRECTORY_READ:{error}"))
    })? {
        let entry = match entry {
            Ok(value) => value,
            Err(error) => {
                failures.push(format!("DIRECTORY_ENTRY:{error}"));
                continue;
            }
        };
        let name = entry.file_name();
        let name = name.to_string_lossy();
        let Some(pid_text) = name
            .strip_prefix("endpoint-")
            .and_then(|value| value.strip_suffix(".json"))
        else {
            continue;
        };
        let Ok(filename_pid) = pid_text.parse::<u32>() else {
            failures.push(format!("{name}:PID_INVALID"));
            continue;
        };
        match read_endpoint(&entry.path(), &canonical_directory, owner, filename_pid) {
            Ok(endpoint) => endpoints.push(endpoint),
            Err(error) => failures.push(format!("{name}:{error}")),
        }
    }
    endpoints.sort_by(|left, right| {
        right
            .created_at_ms
            .cmp(&left.created_at_ms)
            .then_with(|| right.pid.cmp(&left.pid))
    });
    Ok((endpoints, failures))
}

fn read_endpoint(
    path: &Path,
    directory: &Path,
    owner: u32,
    filename_pid: u32,
) -> Result<EndpointRecord, DirectThreadWriterError> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| DirectThreadWriterError::Protocol(format!("ENDPOINT_STAT:{error}")))?;
    if !metadata.file_type().is_file()
        || metadata.uid() != owner
        || metadata.permissions().mode() & 0o777 != 0o600
        || metadata.len() > MAX_ENDPOINT_BYTES
    {
        return Err(DirectThreadWriterError::Protocol(
            "ENDPOINT_FILE_NOT_SEALED".to_string(),
        ));
    }
    let endpoint: EndpointRecord =
        serde_json::from_slice(&fs::read(path).map_err(|error| {
            DirectThreadWriterError::Protocol(format!("ENDPOINT_READ:{error}"))
        })?)
        .map_err(|error| DirectThreadWriterError::Protocol(format!("ENDPOINT_JSON:{error}")))?;
    let expected_socket_name = format!("thread-writer-{}.sock", endpoint.pid);
    if endpoint.pid != filename_pid
        || endpoint.protocol_version != PROTOCOL_VERSION
        || endpoint.created_at_ms == 0
        || !endpoint.socket_path.is_absolute()
        || !is_sha256(&endpoint.app_tools_pipe_sha256)
        || endpoint.app_tools_pipe_basename.is_empty()
        || endpoint.app_tools_pipe_basename.contains('/')
        || endpoint.app_tools_pipe_basename.contains('\\')
        || endpoint
            .socket_path
            .parent()
            .and_then(|parent| fs::canonicalize(parent).ok())
            .as_deref()
            != Some(directory)
        || endpoint
            .socket_path
            .file_name()
            .and_then(|value| value.to_str())
            != Some(expected_socket_name.as_str())
    {
        return Err(DirectThreadWriterError::Protocol(
            "ENDPOINT_IDENTITY_INVALID".to_string(),
        ));
    }
    let socket_metadata = fs::symlink_metadata(&endpoint.socket_path)
        .map_err(|error| DirectThreadWriterError::Protocol(format!("SOCKET_STAT:{error}")))?;
    if !socket_metadata.file_type().is_socket()
        || socket_metadata.uid() != owner
        || socket_metadata.permissions().mode() & 0o777 != 0o600
    {
        return Err(DirectThreadWriterError::Protocol(
            "ENDPOINT_SOCKET_NOT_SEALED".to_string(),
        ));
    }
    Ok(endpoint)
}

fn validate_capabilities(
    endpoint: &EndpointRecord,
    result: Value,
) -> Result<(), DirectThreadWriterError> {
    let capabilities: CapabilityResult = serde_json::from_value(result)
        .map_err(|error| DirectThreadWriterError::Protocol(format!("CAPABILITIES_JSON:{error}")))?;
    let expected_operations = REQUIRED_OPERATIONS.map(str::to_string).to_vec();
    if capabilities.protocol_version != PROTOCOL_VERSION
        || capabilities.operations != expected_operations
        || capabilities.pid != endpoint.pid
        || capabilities.socket_path != endpoint.socket_path
    {
        return Err(DirectThreadWriterError::Protocol(
            "CAPABILITIES_IDENTITY_MISMATCH".to_string(),
        ));
    }
    Ok(())
}

fn validate_tool_success(
    endpoint_pid: u32,
    result: Value,
) -> Result<DirectThreadWriterResult, DirectThreadWriterError> {
    if result.get("success").and_then(Value::as_bool) != Some(true) {
        return Err(DirectThreadWriterError::Protocol(
            "HOST_TOOL_SUCCESS_MISSING".to_string(),
        ));
    }
    Ok(DirectThreadWriterResult {
        endpoint_pid,
        result,
    })
}

fn decode_response(
    response: WriterResponse,
    request_id: &str,
) -> Result<Value, DirectThreadWriterError> {
    if response.schema_version != RESPONSE_SCHEMA || response.request_id != request_id {
        return Err(DirectThreadWriterError::Protocol(
            "RESPONSE_IDENTITY_MISMATCH".to_string(),
        ));
    }
    match (response.ok, response.result, response.error) {
        (true, Some(result), None) => Ok(result),
        (false, None, Some(error)) => Err(DirectThreadWriterError::RequestRejected {
            code: error.code,
            message: error.message,
        }),
        _ => Err(DirectThreadWriterError::Protocol(
            "RESPONSE_SHAPE_INVALID".to_string(),
        )),
    }
}

fn encode_request(request: &impl Serialize) -> Result<Vec<u8>, DirectThreadWriterError> {
    let mut payload = serde_json::to_vec(request)
        .map_err(|error| DirectThreadWriterError::Protocol(format!("REQUEST_JSON:{error}")))?;
    payload.push(b'\n');
    if payload.len() > MAX_LINE_BYTES {
        return Err(DirectThreadWriterError::Configuration(
            "REQUEST_TOO_LARGE".to_string(),
        ));
    }
    Ok(payload)
}

fn validate_identifier(value: &str, name: &str) -> Result<(), DirectThreadWriterError> {
    if value.is_empty()
        || value.len() > 256
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
        || !value.as_bytes()[0].is_ascii_alphanumeric()
    {
        return Err(DirectThreadWriterError::Configuration(format!(
            "{name}_INVALID"
        )));
    }
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn sha256_hex(value: &[u8]) -> String {
    let digest = Sha256::digest(value);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::ffi::OsString;
    use std::os::unix::fs::symlink;
    use std::sync::Arc;
    use tokio::net::UnixListener;
    use tokio::sync::Mutex;

    struct WriterEnvironmentGuard {
        previous_home: Option<OsString>,
        previous_writer_directory: Option<OsString>,
    }

    impl WriterEnvironmentGuard {
        fn install(home: &Path, writer_directory: Option<&Path>) -> Self {
            let guard = Self {
                previous_home: std::env::var_os("TURA_HOME"),
                previous_writer_directory: std::env::var_os("TURA_CODEX_THREAD_WRITER_DIR"),
            };
            #[allow(unsafe_code)]
            unsafe {
                std::env::set_var("TURA_HOME", home);
                match writer_directory {
                    Some(directory) => std::env::set_var("TURA_CODEX_THREAD_WRITER_DIR", directory),
                    None => std::env::remove_var("TURA_CODEX_THREAD_WRITER_DIR"),
                }
            }
            guard
        }
    }

    impl Drop for WriterEnvironmentGuard {
        fn drop(&mut self) {
            #[allow(unsafe_code)]
            unsafe {
                match self.previous_home.take() {
                    Some(value) => std::env::set_var("TURA_HOME", value),
                    None => std::env::remove_var("TURA_HOME"),
                }
                match self.previous_writer_directory.take() {
                    Some(value) => std::env::set_var("TURA_CODEX_THREAD_WRITER_DIR", value),
                    None => std::env::remove_var("TURA_CODEX_THREAD_WRITER_DIR"),
                }
            }
        }
    }

    #[derive(Clone, Copy)]
    enum SendBehavior {
        Accept,
        DropAfterRequest,
        DisappearAfterPreflight,
    }

    struct FakeEndpoint {
        task: tokio::task::JoinHandle<()>,
        requests: Arc<Mutex<Vec<Value>>>,
    }

    impl Drop for FakeEndpoint {
        fn drop(&mut self) {
            self.task.abort();
        }
    }

    async fn fake_endpoint(
        root: &Path,
        pid: u32,
        created_at_ms: u64,
        behavior: SendBehavior,
    ) -> FakeEndpoint {
        fs::create_dir_all(root).expect("endpoint directory");
        fs::set_permissions(root, fs::Permissions::from_mode(0o700)).expect("directory mode");
        let socket_path = root.join(format!("thread-writer-{pid}.sock"));
        let listener = UnixListener::bind(&socket_path).expect("bind fake writer");
        fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600)).expect("socket mode");
        let endpoint_path = root.join(format!("endpoint-{pid}.json"));
        fs::write(
            &endpoint_path,
            serde_json::to_vec(&json!({
                "socket_path": socket_path,
                "pid": pid,
                "app_tools_pipe_sha256": "a".repeat(64),
                "app_tools_pipe_basename": "app-tools.sock",
                "protocol_version": PROTOCOL_VERSION,
                "created_at_ms": created_at_ms,
            }))
            .expect("endpoint json"),
        )
        .expect("write endpoint");
        fs::set_permissions(&endpoint_path, fs::Permissions::from_mode(0o600))
            .expect("endpoint mode");
        let requests = Arc::new(Mutex::new(Vec::new()));
        let captured = requests.clone();
        let task = tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    break;
                };
                let captured = captured.clone();
                let socket_path = socket_path.clone();
                tokio::spawn(async move {
                    let mut stream = BufReader::new(stream);
                    let mut line = Vec::new();
                    if stream
                        .read_until(b'\n', &mut line)
                        .await
                        .ok()
                        .filter(|n| *n > 0)
                        .is_none()
                    {
                        return;
                    }
                    let request: Value = serde_json::from_slice(&line).expect("request json");
                    captured.lock().await.push(request.clone());
                    let request_id = request["request_id"].as_str().expect("request id");
                    if request["operation"] == "send_message_to_thread"
                        && matches!(behavior, SendBehavior::DropAfterRequest)
                    {
                        return;
                    }
                    let result = if request["operation"] == "capabilities" {
                        json!({
                            "protocol_version": PROTOCOL_VERSION,
                            "operations": REQUIRED_OPERATIONS,
                            "socket_path": socket_path,
                            "pid": pid,
                        })
                    } else {
                        json!({"success": true, "contentItems": [{"type": "inputText", "text": "accepted"}]})
                    };
                    let response = json!({
                        "schema_version": RESPONSE_SCHEMA,
                        "request_id": request_id,
                        "ok": true,
                        "result": result,
                    });
                    let mut bytes = serde_json::to_vec(&response).expect("response json");
                    bytes.push(b'\n');
                    stream
                        .get_mut()
                        .write_all(&bytes)
                        .await
                        .expect("write response");
                    if request["operation"] == "capabilities"
                        && matches!(behavior, SendBehavior::DisappearAfterPreflight)
                    {
                        fs::remove_file(socket_path).expect("remove preflight-only socket");
                    }
                });
            }
        });
        FakeEndpoint { task, requests }
    }

    #[tokio::test]
    async fn explicit_absolute_external_endpoint_directory_is_accepted() {
        let _lock = crate::services::ROUTER_TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let temporary = tempfile::tempdir().expect("temporary root");
        let home = temporary.path().join("home");
        let external = temporary.path().join("external-writers");
        fs::create_dir_all(&home).expect("home directory");
        fs::create_dir_all(&external).expect("external writer directory");
        fs::set_permissions(&external, fs::Permissions::from_mode(0o700))
            .expect("external writer directory mode");
        let endpoint = fake_endpoint(&external, 4001, 1, SendBehavior::Accept).await;
        let _environment = WriterEnvironmentGuard::install(&home, Some(&external));

        let client = DirectThreadWriterClient::from_environment()
            .expect("explicit external writer directory should be accepted");

        assert_eq!(
            client.endpoint_directory,
            fs::canonicalize(external).expect("canonical external writer directory")
        );
        assert_eq!(
            client
                .preflight("external-preflight-1")
                .await
                .expect("sealed external endpoint must pass the normal capability preflight"),
            vec![4001]
        );
        assert_eq!(endpoint.requests.lock().await.len(), 1);
    }

    #[test]
    fn default_endpoint_directory_rejects_symlink_escape() {
        let _lock = crate::services::ROUTER_TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let temporary = tempfile::tempdir().expect("temporary root");
        let home = temporary.path().join("home");
        let external = temporary.path().join("external-writers");
        fs::create_dir_all(home.join(".tura")).expect("default writer parent");
        fs::create_dir_all(&external).expect("external writer directory");
        symlink(&external, home.join(".tura/codex-thread-writer")).expect("default writer symlink");
        let _environment = WriterEnvironmentGuard::install(&home, None);

        let error = DirectThreadWriterClient::from_environment()
            .expect_err("default writer directory must remain contained");

        assert_eq!(
            error,
            DirectThreadWriterError::Configuration(
                "ENDPOINT_DIRECTORY_OUTSIDE_TURA_HOME".to_string()
            )
        );
    }

    #[test]
    fn explicit_relative_endpoint_directory_is_rejected() {
        let _lock = crate::services::ROUTER_TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let temporary = tempfile::tempdir().expect("temporary root");
        let home = temporary.path().join("home");
        fs::create_dir_all(&home).expect("home directory");
        let _environment =
            WriterEnvironmentGuard::install(&home, Some(Path::new("relative-writers")));

        let error = DirectThreadWriterClient::from_environment()
            .expect_err("relative writer override must be rejected");

        assert_eq!(
            error,
            DirectThreadWriterError::Configuration("ENDPOINT_DIRECTORY_NOT_ABSOLUTE".to_string())
        );
    }

    #[tokio::test]
    async fn preflight_discovers_only_sealed_capable_per_process_endpoints() {
        let temporary = tempfile::tempdir().expect("temporary root");
        let directory = temporary.path().join("writers");
        let valid = fake_endpoint(&directory, 4101, 2, SendBehavior::Accept).await;
        fs::write(directory.join("endpoint-4102.json"), b"{}").expect("write unsealed endpoint");
        fs::set_permissions(
            directory.join("endpoint-4102.json"),
            fs::Permissions::from_mode(0o644),
        )
        .expect("unsealed mode");
        let client = DirectThreadWriterClient::new(directory, Duration::from_secs(1));

        assert_eq!(
            client.preflight("preflight-1").await.expect("preflight"),
            vec![4101]
        );
        assert_eq!(valid.requests.lock().await.len(), 1);
    }

    #[tokio::test]
    async fn read_thread_uses_only_the_closed_protocol_identity() {
        let temporary = tempfile::tempdir().expect("temporary root");
        let directory = temporary.path().join("writers");
        let endpoint = fake_endpoint(&directory, 4201, 1, SendBehavior::Accept).await;
        let client = DirectThreadWriterClient::new(directory, Duration::from_secs(1));

        let result = client
            .read_thread("read-1", "thread-1", "turn-1", "call-1")
            .await
            .expect("read thread");
        assert_eq!(result.endpoint_pid, 4201);
        let requests = endpoint.requests.lock().await;
        assert_eq!(requests.len(), 2);
        assert_eq!(requests[1]["operation"], "read_thread");
        assert_eq!(requests[1]["target_thread_id"], "thread-1");
        assert_eq!(requests[1]["turn_id"], "turn-1");
        assert_eq!(requests[1]["call_id"], "call-1");
    }

    #[tokio::test]
    async fn send_eof_is_unsettled_and_never_retries_another_endpoint() {
        let temporary = tempfile::tempdir().expect("temporary root");
        let directory = temporary.path().join("writers");
        let fallback = fake_endpoint(&directory, 4301, 1, SendBehavior::Accept).await;
        let selected = fake_endpoint(&directory, 4302, 2, SendBehavior::DropAfterRequest).await;
        let client = DirectThreadWriterClient::new(directory, Duration::from_secs(1));

        let error = client
            .send_message_to_thread("send-1", "thread-1", "turn-1", "bounded callback")
            .await
            .expect_err("delivery must remain unsettled");
        assert!(matches!(
            error,
            DirectThreadWriterError::DeliveryUnsettled {
                endpoint_pid: 4302,
                ..
            }
        ));
        let selected_requests = selected.requests.lock().await;
        assert_eq!(
            selected_requests
                .iter()
                .filter(|request| request["operation"] == "send_message_to_thread")
                .count(),
            1
        );
        let fallback_requests = fallback.requests.lock().await;
        assert_eq!(
            fallback_requests
                .iter()
                .filter(|request| request["operation"] == "send_message_to_thread")
                .count(),
            0
        );
    }

    #[tokio::test]
    async fn send_can_select_another_endpoint_only_before_writing() {
        let temporary = tempfile::tempdir().expect("temporary root");
        let directory = temporary.path().join("writers");
        let fallback = fake_endpoint(&directory, 4351, 1, SendBehavior::Accept).await;
        let disappearing =
            fake_endpoint(&directory, 4352, 2, SendBehavior::DisappearAfterPreflight).await;
        let client = DirectThreadWriterClient::new(directory, Duration::from_secs(1));

        let result = client
            .send_message_to_thread("send-pre-1", "thread-1", "turn-1", "bounded callback")
            .await
            .expect("fallback before send is allowed");
        assert_eq!(result.endpoint_pid, 4351);
        assert_eq!(
            disappearing
                .requests
                .lock()
                .await
                .iter()
                .filter(|request| request["operation"] == "send_message_to_thread")
                .count(),
            0
        );
        assert_eq!(
            fallback
                .requests
                .lock()
                .await
                .iter()
                .filter(|request| request["operation"] == "send_message_to_thread")
                .count(),
            1
        );
    }

    #[tokio::test]
    async fn send_binds_call_to_request_and_message_sha() {
        let temporary = tempfile::tempdir().expect("temporary root");
        let directory = temporary.path().join("writers");
        let endpoint = fake_endpoint(&directory, 4401, 1, SendBehavior::Accept).await;
        let client = DirectThreadWriterClient::new(directory, Duration::from_secs(1));

        let result = client
            .send_message_to_thread("send-2", "thread-2", "turn-2", "bounded callback")
            .await
            .expect("accepted send");
        assert_eq!(result.endpoint_pid, 4401);
        let requests = endpoint.requests.lock().await;
        let send = requests
            .iter()
            .find(|request| request["operation"] == "send_message_to_thread")
            .expect("send request");
        assert_eq!(send["call_id"], "send-2");
        assert_eq!(send["request_id"], "send-2");
        assert_eq!(send["message_sha256"], sha256_hex(b"bounded callback"));
    }
}
