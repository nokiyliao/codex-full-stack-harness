use runtime::native_codex_runner::{
    NativeCodexCommandGraph, NativeCodexContext, NativeCodexRunRequest, NativeCodexRunner,
    NativeCodexSandbox, NativeCodexProviderProfile,
};
use runtime_contract::{NativeCodexTaskDelta, TaskContextCapsule};
use serde::Deserialize;
use serde_json::Value;
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const REQUEST_SCHEMA: &str = "tura_native_codex_worker_request_v3";
const LEGACY_REQUEST_SCHEMA: &str = "tura_native_codex_worker_request_v2";
const MAX_REQUEST_BYTES: u64 = 2 * 1024 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WireCommandGraph {
    executable: String,
    executable_sha256: String,
    allowed_commands: BTreeSet<String>,
    jspace_contract: Value,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WireRequest {
    schema_version: String,
    #[serde(default)]
    provider_profile: Option<NativeCodexProviderProfile>,
    codex_executable: String,
    codex_executable_sha256: String,
    workspace: String,
    session_id: String,
    task_id: String,
    execution_id: String,
    lease_id: String,
    execution_profile_sha256: String,
    execution_binding_sha256: String,
    expected_task_context_capsule_sha256: String,
    expected_task_delta_sha256: String,
    expected_jspace_semantic_sha256: String,
    task_context_capsule: Value,
    task_delta: Value,
    sandbox: String,
    timeout_ms: u64,
    #[serde(default)]
    command_graph: Option<WireCommandGraph>,
}

impl WireRequest {
    fn into_runner_request(self) -> Result<NativeCodexRunRequest, String> {
        match self.schema_version.as_str() {
            REQUEST_SCHEMA if self.provider_profile.is_some() => {},
            LEGACY_REQUEST_SCHEMA if self.provider_profile.is_none() => {},
            _ => return Err("NATIVE_CODEX_WORKER_REQUEST_SCHEMA_PROFILE_MISMATCH".to_string()),
        }
        let sandbox = match self.sandbox.as_str() {
            "read_only" => NativeCodexSandbox::ReadOnly,
            "workspace_write" => NativeCodexSandbox::WorkspaceWrite,
            _ => return Err("NATIVE_CODEX_WORKER_SANDBOX_INVALID".to_string()),
        };
        let task_context_capsule = TaskContextCapsule::from_value(self.task_context_capsule)?;
        let task_delta = NativeCodexTaskDelta::from_value(self.task_delta)?;
        Ok(NativeCodexRunRequest {
            codex_executable: PathBuf::from(self.codex_executable),
            codex_executable_sha256: self.codex_executable_sha256,
            workspace: PathBuf::from(self.workspace),
            session_id: self.session_id,
            task_id: self.task_id,
            execution_id: self.execution_id,
            lease_id: self.lease_id,
            context: NativeCodexContext {
                provider_profile: self.provider_profile,
                execution_profile_sha256: self.execution_profile_sha256,
                execution_binding_sha256: self.execution_binding_sha256,
                expected_task_context_capsule_sha256: self.expected_task_context_capsule_sha256,
                expected_task_delta_sha256: self.expected_task_delta_sha256,
                expected_jspace_semantic_sha256: self.expected_jspace_semantic_sha256,
                task_context_capsule,
                task_delta,
            },
            sandbox,
            timeout: Duration::from_millis(self.timeout_ms),
            command_graph: self
                .command_graph
                .map(|command_graph| NativeCodexCommandGraph {
                    executable: PathBuf::from(command_graph.executable),
                    executable_sha256: command_graph.executable_sha256,
                    allowed_commands: command_graph.allowed_commands,
                    jspace_contract: command_graph.jspace_contract,
                }),
        })
    }
}

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("{error}");
        std::process::exit(2);
    }
}

async fn run() -> Result<(), String> {
    let mut input = Vec::new();
    tokio::io::stdin()
        .take(MAX_REQUEST_BYTES + 1)
        .read_to_end(&mut input)
        .await
        .map_err(|error| format!("NATIVE_CODEX_WORKER_REQUEST_READ_FAILED:{error}"))?;
    if input.len() as u64 > MAX_REQUEST_BYTES {
        return Err("NATIVE_CODEX_WORKER_REQUEST_TOO_LARGE".to_string());
    }
    let request: WireRequest = serde_json::from_slice(&input)
        .map_err(|error| format!("NATIVE_CODEX_WORKER_REQUEST_INVALID:{error}"))?;
    let envelope = NativeCodexRunner::run(request.into_runner_request()?).await?;
    let mut output = serde_json::to_vec(&envelope)
        .map_err(|error| format!("NATIVE_CODEX_WORKER_TERMINAL_ENCODE_FAILED:{error}"))?;
    output.push(b'\n');
    let mut stdout = tokio::io::stdout();
    stdout
        .write_all(&output)
        .await
        .map_err(|error| format!("NATIVE_CODEX_WORKER_TERMINAL_WRITE_FAILED:{error}"))?;
    stdout
        .flush()
        .await
        .map_err(|error| format!("NATIVE_CODEX_WORKER_TERMINAL_FLUSH_FAILED:{error}"))
}
