use runtime_contract::{
    ModelServiceTier, NATIVE_CODEX_TERMINAL_ENVELOPE_SCHEMA_VERSION, NativeCodexTaskDelta,
    NativeCodexTerminalEnvelope, NativeCodexTerminalState, NativeCodexToolObservation,
    TaskContextCapsule,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

pub use runtime_contract::NativeCodexSandbox;

pub const TURA_EXECUTION_PROFILE_SHA256: &str =
    "a47f5013e32b44e507a38650e83949e3949664f3484bac65cf44a0d86d4fd7a8";
pub const TURA_BALANCED_PROMPT_SHA256: &str =
    "0e79ace9135dd9e28166ba16bcd71e5f2cd34a42d38788a2e4b6ff9419c193af";
pub const TURA_PROFILE_MODEL: &str = "gpt-5.6-sol";
pub const TURA_PROFILE_REASONING_EFFORT: &str = "high";

const TURA_BALANCED_PROMPT: &str = include_str!("../../../agents/src/balanced/prompt.md");
pub const NATIVE_ONCE_PROMPT_SHA256: &str =
    "3843b747f35f0d744fcb3c4808f303a8a74afe20d4f323d4e40b2a4ed81d1f28";
const NATIVE_ONCE_PROMPT: &str = include_str!("../../../agents/src/native_once/prompt.md");
const MAX_TASK_CONTEXT_CAPSULE_BYTES: usize = 256 * 1024;
const MAX_COMPILED_PROVIDER_INPUT_BYTES: usize = 1024 * 1024;
const MAX_EVENT_STREAM_BYTES: usize = 16 * 1024 * 1024;
const MAX_STDERR_BYTES: usize = 1024 * 1024;

/// Explicit per-execution provider selection. It cannot silently inherit the
/// historical benchmark model when the outer Codex caller selected another one.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeCodexProviderProfile {
    pub model: String,
    pub reasoning_effort: String,
    pub service_tier: ModelServiceTier,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_provider: Option<String>,
}

impl NativeCodexProviderProfile {
    pub fn semantic_sha256(&self) -> Result<String, String> {
        if self.model.is_empty()
            || self.model.len() > 128
            || !self.model.bytes().all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
        {
            return Err("NATIVE_CODEX_PROVIDER_MODEL_INVALID".into());
        }
        if !matches!(self.reasoning_effort.as_str(), "low" | "medium" | "high" | "xhigh" | "max" | "ultra") {
            return Err("NATIVE_CODEX_PROVIDER_EFFORT_INVALID".into());
        }
        if let Some(provider) = &self.model_provider {
            if provider.is_empty() || provider.len() > 128
                || !provider.bytes().all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c)) {
                return Err("NATIVE_CODEX_MODEL_PROVIDER_INVALID".into());
            }
        }
        let mut identity = serde_json::json!({
            "schema_version": "tura_native_execution_profile_v1",
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "service_tier": self.service_tier,
            "balanced_prompt_sha256": NATIVE_ONCE_PROMPT_SHA256,
            "sandbox": "read_only",
            "tool_surface": "tura_command_graph",
            "execution_model": "single_task"
        });
        if let Some(provider) = &self.model_provider {
            identity["model_provider"] = Value::String(provider.clone());
        }
        runtime_contract::task_context_semantic_sha256_v1(&identity)
    }
}

#[derive(Debug, Clone)]
pub struct NativeCodexCommandGraph {
    pub executable: PathBuf,
    pub executable_sha256: String,
    pub allowed_commands: BTreeSet<String>,
    pub jspace_contract: Value,
}

#[derive(Debug, Clone)]
pub struct NativeCodexContext {
    pub provider_profile: Option<NativeCodexProviderProfile>,
    pub execution_profile_sha256: String,
    pub execution_binding_sha256: String,
    pub expected_task_context_capsule_sha256: String,
    pub expected_task_delta_sha256: String,
    pub expected_jspace_semantic_sha256: String,
    pub task_context_capsule: TaskContextCapsule,
    pub task_delta: NativeCodexTaskDelta,
}

impl NativeCodexContext {
    fn execution_prompt(&self) -> (&'static str, &'static str) {
        // Frozen legacy executions retain their original prompt and identity.
        if self.provider_profile.is_some() {
            (NATIVE_ONCE_PROMPT, NATIVE_ONCE_PROMPT_SHA256)
        } else {
            (TURA_BALANCED_PROMPT, TURA_BALANCED_PROMPT_SHA256)
        }
    }

    fn compile_for_task(&self, task_id: &str) -> Result<String, String> {
        let expected_profile = match &self.provider_profile {
            Some(profile) => profile.semantic_sha256()?,
            None => TURA_EXECUTION_PROFILE_SHA256.to_string(),
        };
        if self.execution_profile_sha256 != expected_profile {
            return Err("NATIVE_CODEX_EXECUTION_PROFILE_IDENTITY_MISMATCH".to_string());
        }
        let (prompt, prompt_sha256) = self.execution_prompt();
        if sha256(prompt.as_bytes()) != prompt_sha256 {
            return Err("NATIVE_CODEX_EXECUTION_PROFILE_BYTES_DRIFT".to_string());
        }
        self.task_context_capsule.validate()?;
        if self.task_context_capsule.semantic_sha256 != self.expected_task_context_capsule_sha256 {
            return Err("NATIVE_CODEX_TASK_CONTEXT_EXPECTED_DIGEST_MISMATCH".to_string());
        }
        if self.task_context_capsule.jspace_semantic_sha256 != self.expected_jspace_semantic_sha256
        {
            return Err("NATIVE_CODEX_TASK_CONTEXT_JSPACE_EXPECTED_DIGEST_MISMATCH".to_string());
        }
        self.task_delta.bind_capsule(&self.task_context_capsule)?;
        if self.task_delta.task_id != task_id {
            return Err("NATIVE_CODEX_TASK_CONTEXT_TASK_ID_MISMATCH".to_string());
        }
        if self.task_delta.semantic_sha256 != self.expected_task_delta_sha256 {
            return Err("NATIVE_CODEX_TASK_DELTA_EXPECTED_DIGEST_MISMATCH".to_string());
        }

        let capsule_json = serde_json::to_string(&self.task_context_capsule)
            .map_err(|error| format!("NATIVE_CODEX_TASK_CONTEXT_ENCODE_FAILED:{error}"))?;
        if capsule_json.len() > MAX_TASK_CONTEXT_CAPSULE_BYTES {
            return Err("NATIVE_CODEX_TASK_CONTEXT_TOO_LARGE".to_string());
        }
        // Preserve canonical artifact size and identity checks above. Only the
        // model presentation is projected; worker and Native share this path.
        let capsule_presentation = self.task_context_capsule.provider_context();
        let delta_json = serde_json::to_string(&self.task_delta)
            .map_err(|error| format!("NATIVE_CODEX_TASK_DELTA_ENCODE_FAILED:{error}"))?;

        let provider_input = format!(
            "[TURA_IMMUTABLE_INSTRUCTION_PREFIX_V1]\nexecution_profile_sha256={}\nbalanced_prompt_sha256={}\n{}\n[TASK_CONTEXT_CAPSULE_V1]\n{}\n\n[CURRENT_TASK_DELTA_V1]\n{}",
            self.execution_profile_sha256,
            prompt_sha256,
            prompt,
            capsule_presentation,
            delta_json,
        );
        if provider_input.len() > MAX_COMPILED_PROVIDER_INPUT_BYTES {
            return Err("NATIVE_CODEX_PROVIDER_INPUT_TOO_LARGE".to_string());
        }
        Ok(provider_input)
    }
}

#[derive(Debug, Clone)]
pub struct NativeCodexRunRequest {
    pub codex_executable: PathBuf,
    pub codex_executable_sha256: String,
    pub workspace: PathBuf,
    pub session_id: String,
    pub task_id: String,
    pub execution_id: String,
    pub lease_id: String,
    pub context: NativeCodexContext,
    pub sandbox: NativeCodexSandbox,
    pub timeout: Duration,
    pub command_graph: Option<NativeCodexCommandGraph>,
}

impl NativeCodexRunRequest {
    fn validated_provider_input(&self) -> Result<String, String> {
        for (name, value) in [
            ("session_id", self.session_id.as_str()),
            ("task_id", self.task_id.as_str()),
            ("execution_id", self.execution_id.as_str()),
            ("lease_id", self.lease_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(format!("NATIVE_CODEX_RUNNER_IDENTITY_MISSING:{name}"));
            }
        }
        validate_absolute_file("codex_executable", &self.codex_executable)?;
        validate_file_sha256(
            "codex_executable",
            &self.codex_executable,
            &self.codex_executable_sha256,
        )?;
        if !self.workspace.is_absolute() || !self.workspace.is_dir() {
            return Err("NATIVE_CODEX_RUNNER_WORKSPACE_INVALID".to_string());
        }
        if self.timeout.is_zero() || self.timeout > Duration::from_secs(24 * 60 * 60) {
            return Err("NATIVE_CODEX_RUNNER_TIMEOUT_INVALID".to_string());
        }
        if self.sandbox != NativeCodexSandbox::ReadOnly {
            return Err("NATIVE_CODEX_RUNNER_REQUIRES_READ_ONLY_SANDBOX".to_string());
        }
        if let Some(command_graph) = &self.command_graph {
            validate_absolute_file("command_graph_executable", &command_graph.executable)?;
            validate_file_sha256(
                "command_graph_executable",
                &command_graph.executable,
                &command_graph.executable_sha256,
            )?;
            if command_graph.allowed_commands.is_empty()
                || command_graph
                    .allowed_commands
                    .iter()
                    .any(|command| command.trim().is_empty())
            {
                return Err("NATIVE_CODEX_COMMAND_GRAPH_ALLOWLIST_INVALID".to_string());
            }
            self.context
                .task_context_capsule
                .bind_jspace(Some(&command_graph.jspace_contract))?;
        }
        self.context.compile_for_task(&self.task_id)
    }
}

#[derive(Default)]
struct EventSummary {
    // Keep the exact observed byte identity without retaining the raw transcript.
    event_stream_hasher: Sha256,
    event_stream_bytes: usize,
    worker_thread_id: Option<String>,
    final_text: Option<String>,
    tool_observations: Vec<NativeCodexToolObservation>,
    usage: Option<Value>,
    turn_completed: bool,
    terminal_error: Option<String>,
}

pub struct NativeCodexRunner;

impl NativeCodexRunner {
    pub async fn run(
        request: NativeCodexRunRequest,
    ) -> Result<NativeCodexTerminalEnvelope, String> {
        let provider_input = request.validated_provider_input()?;
        let deadline = tokio::time::Instant::now() + request.timeout;
        let inherited_mcp = native_mcp_server_names(
            &request.codex_executable, &request.workspace, deadline,
        ).await?;
        let mut command = Command::new(&request.codex_executable);
        command
            .arg("exec")
            .arg("--disable")
            .arg("shell_tool")
            .arg("--disable")
            .arg("unified_exec")
            .arg("--disable")
            .arg("multi_agent")
            .arg("--disable")
            .arg("view_image")
            .arg("--disable")
            .arg("default_mode_request_user_input")
            .arg("--disable")
            .arg("apps")
            .arg("--disable")
            .arg("plugins")
            .arg("--disable")
            .arg("remote_plugin")
            .arg("--disable")
            .arg("skill_mcp_dependency_install")
            .arg("--strict-config")
            .arg("--ephemeral")
            .arg("--json")
            .arg("--skip-git-repo-check")
            .arg("--color")
            .arg("never")
            .arg("-C")
            .arg(&request.workspace)
            .arg("-m")
            .arg(request.context.provider_profile.as_ref()
                .map_or(TURA_PROFILE_MODEL, |profile| profile.model.as_str()))
            .arg("-s")
            .arg(request.sandbox.as_str())
            .arg("-c")
            .arg("skills.include_instructions=false")
            .arg("-c")
            .arg("include_apps_instructions=false")
            .arg("-c")
            .arg("include_collaboration_mode_instructions=false")
            .arg("-c")
            .arg("include_environment_context=false")
            .arg("-c")
            .arg("web_search=\"disabled\"")
            .arg("-c")
            .arg("agents.enabled=false")
            .arg("-c")
            .arg("tools.experimental_request_user_input.enabled=false")
            .arg("-c")
            .arg(format!(
                "model_reasoning_effort={}",
                toml_string(request.context.provider_profile.as_ref()
                    .map_or(TURA_PROFILE_REASONING_EFFORT, |profile| profile.reasoning_effort.as_str()))
            ))
            .arg("-c")
            .arg(format!(
                "service_tier={}",
                toml_string(request.context.provider_profile.as_ref()
                    .map_or(ModelServiceTier::Priority, |profile| profile.service_tier).as_str())
            ));

        // Native merges MCP tables; an empty table does not remove inherited
        // servers. Disable the effective catalog without discarding user rules.
        for name in inherited_mcp {
            command.arg("-c").arg(format!("mcp_servers.{name}.enabled=false"));
        }
        if let Some(provider) = request.context.provider_profile.as_ref()
            .and_then(|profile| profile.model_provider.as_ref()) {
            command.arg("-c").arg(format!("model_provider={}", toml_string(provider)));
        }

        if let Some(command_graph) = &request.command_graph {
            command
                .arg("-c")
                .arg(format!(
                    "mcp_servers.tura_command_graph.command={}",
                    toml_string(&command_graph.executable.to_string_lossy())
                ))
                .arg("-c")
                .arg("mcp_servers.tura_command_graph.args=[]")
                .arg("-c")
                .arg("mcp_servers.tura_command_graph.enabled=true")
                .arg("-c")
                .arg("mcp_servers.tura_command_graph.required=true")
                .arg("-c")
                .arg("mcp_servers.tura_command_graph.enabled_tools=[\"tura_command_graph\"]")
                .arg("-c")
                .arg(
                    "mcp_servers.tura_command_graph.env_vars=[\"TURA_NATIVE_SESSION_ID\",\"TURA_NATIVE_TASK_ID\",\"TURA_NATIVE_EXECUTION_ID\",\"TURA_NATIVE_LEASE_ID\",\"TURA_NATIVE_WORKSPACE\",\"TURA_NATIVE_ALLOWED_COMMANDS_JSON\",\"TURA_NATIVE_JSPACE_CONTRACT_JSON\",\"TURA_ROUTER_ADDR\",\"TURA_COMMAND_RUN_SANDBOX\"]",
                )
                .env("TURA_NATIVE_SESSION_ID", &request.session_id)
                .env("TURA_NATIVE_TASK_ID", &request.task_id)
                .env("TURA_NATIVE_EXECUTION_ID", &request.execution_id)
                .env("TURA_NATIVE_LEASE_ID", &request.lease_id)
                .env("TURA_NATIVE_WORKSPACE", &request.workspace)
                .env(
                    "TURA_NATIVE_ALLOWED_COMMANDS_JSON",
                    serde_json::to_string(&command_graph.allowed_commands)
                        .map_err(|error| format!("encode command allowlist: {error}"))?,
                )
                .env(
                    "TURA_NATIVE_JSPACE_CONTRACT_JSON",
                    serde_json::to_string(&command_graph.jspace_contract)
                        .map_err(|error| format!("encode J-Space contract: {error}"))?,
                );
        }

        command
            .arg("-")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);

        let mut child = command
            .spawn()
            .map_err(|error| format!("NATIVE_CODEX_RUNNER_SPAWN_FAILED:{error}"))?;
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| "NATIVE_CODEX_RUNNER_STDIN_UNAVAILABLE".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "NATIVE_CODEX_RUNNER_STDOUT_UNAVAILABLE".to_string())?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| "NATIVE_CODEX_RUNNER_STDERR_UNAVAILABLE".to_string())?;
        let mut summary = EventSummary::default();

        // One structured lifetime covers prompt writes, both output streams and
        // process exit. No spawned reader can outlive cancellation of this run.
        // Router retains ownership of the surrounding worker/process-tree scope.
        let completion = tokio::time::timeout_at(deadline, async {
            tokio::try_join!(
                async {
                    stdin
                        .write_all(provider_input.as_bytes())
                        .await
                        .map_err(|error| {
                            format!("NATIVE_CODEX_RUNNER_PROMPT_WRITE_FAILED:{error}")
                        })?;
                    stdin.shutdown().await.map_err(|error| {
                        format!("NATIVE_CODEX_RUNNER_PROMPT_CLOSE_FAILED:{error}")
                    })?;
                    // ChildStdin shutdown alone does not necessarily close the
                    // pipe. Release the handle before waiting for child EOF/exit.
                    drop(stdin);
                    Ok::<(), String>(())
                },
                read_event_stream(stdout, &request.execution_id, &mut summary),
                read_bounded(stderr, MAX_STDERR_BYTES),
                async {
                    child
                        .wait()
                        .await
                        .map_err(|error| format!("NATIVE_CODEX_RUNNER_WAIT_FAILED:{error}"))
                },
            )
        })
        .await;
        let (status, timed_out, stderr) = match completion {
            Ok(Ok(((), (), stderr, status))) => (status, false, stderr),
            Ok(Err(error)) => {
                stop_and_reap_native_child(&mut child).await?;
                return Err(error);
            }
            Err(_) => (
                stop_and_reap_native_child(&mut child).await?,
                true,
                Vec::new(),
            ),
        };
        let exit_code = status.code();

        let terminal_state = if timed_out {
            NativeCodexTerminalState::Interrupted
        } else if exit_code == Some(0)
            && summary.turn_completed
            && summary.final_text.is_some()
            && summary.terminal_error.is_none()
        {
            NativeCodexTerminalState::Completed
        } else {
            NativeCodexTerminalState::Failed
        };
        let error = if terminal_state == NativeCodexTerminalState::Completed {
            None
        } else if timed_out {
            Some("NATIVE_CODEX_RUNNER_TIMEOUT".to_string())
        } else {
            summary.terminal_error.clone().or_else(|| {
                let stderr = String::from_utf8_lossy(&stderr).trim().to_string();
                (!stderr.is_empty()).then_some(stderr)
            })
        };
        let final_text_sha256 = summary
            .final_text
            .as_ref()
            .map(|text| sha256(text.as_bytes()));
        let envelope = NativeCodexTerminalEnvelope {
            schema_version: NATIVE_CODEX_TERMINAL_ENVELOPE_SCHEMA_VERSION.to_string(),
            task_id: request.task_id,
            execution_id: request.execution_id,
            lease_id: request.lease_id,
            execution_profile_sha256: request.context.execution_profile_sha256,
            execution_binding_sha256: request.context.execution_binding_sha256,
            task_context_capsule_sha256: request.context.task_context_capsule.semantic_sha256,
            task_delta_sha256: request.context.task_delta.semantic_sha256,
            provider_input_sha256: sha256(provider_input.as_bytes()),
            worker_thread_id: summary.worker_thread_id,
            terminal_state,
            final_text: summary.final_text,
            final_text_sha256,
            tool_observations: summary.tool_observations,
            event_stream_sha256: format!("{:x}", summary.event_stream_hasher.finalize()),
            process_exit_code: exit_code,
            process_reaped: true,
            terminal: true,
            execution_lease_active: false,
            ephemeral: true,
            private_codex_state_write_count: 0,
            usage: summary.usage,
            error,
        };
        envelope.validate_shape()?;
        Ok(envelope)
    }
}

async fn native_mcp_server_names(
    executable: &Path, workspace: &Path, execution_deadline: tokio::time::Instant,
) -> Result<BTreeSet<String>, String> {
    let mut child = Command::new(executable)
        .args(["mcp", "list", "--json"])
        // Plugin-owned servers disappear with these execution features off.
        // Discovering them first would create transport-less disable overrides.
        .args(["--disable", "apps", "--disable", "plugins", "--disable", "remote_plugin",
               "--disable", "skill_mcp_dependency_install"])
        .current_dir(workspace)
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .kill_on_drop(true).spawn()
        .map_err(|_| "NATIVE_CODEX_MCP_CATALOG_SPAWN_FAILED".to_string())?;
    let stdout = child.stdout.take().ok_or("NATIVE_CODEX_MCP_CATALOG_STDOUT_MISSING")?;
    let stderr = child.stderr.take().ok_or("NATIVE_CODEX_MCP_CATALOG_STDERR_MISSING")?;
    let deadline = execution_deadline.min(tokio::time::Instant::now() + Duration::from_secs(5));
    let result = tokio::time::timeout_at(deadline, async {
        tokio::try_join!(
            read_bounded(stdout, 1024 * 1024), read_bounded(stderr, MAX_STDERR_BYTES),
            async { child.wait().await.map_err(|_| "NATIVE_CODEX_MCP_CATALOG_WAIT_FAILED".to_string()) },
        )
    }).await;
    let (stdout, _, status) = match result {
        Ok(Ok(output)) => output,
        Ok(Err(error)) => { stop_and_reap_native_child(&mut child).await?; return Err(error); }
        Err(_) => {
            stop_and_reap_native_child(&mut child).await?;
            return Err("NATIVE_CODEX_MCP_CATALOG_TIMEOUT".into());
        }
    };
    if !status.success() { return Err("NATIVE_CODEX_MCP_CATALOG_FAILED".into()); }
    // Only names are retained. Native transport configuration never enters the
    // prompt, terminal receipt or logs.
    let rows: Vec<Value> = serde_json::from_slice(&stdout)
        .map_err(|_| "NATIVE_CODEX_MCP_CATALOG_INVALID".to_string())?;
    let mut names = BTreeSet::new();
    for row in rows {
        let name = row.get("name").and_then(Value::as_str)
            .ok_or("NATIVE_CODEX_MCP_CATALOG_INVALID")?;
        if name.is_empty() || name.len() > 256
            || !name.bytes().all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c)) {
            return Err("NATIVE_CODEX_MCP_NAME_UNSUPPORTED".into());
        }
        // An inherited same-name server may carry conflicting transport/env
        // fields that Native would merge into the bound tool's configuration.
        if name == "tura_command_graph" {
            return Err("NATIVE_CODEX_MCP_GRAPH_NAME_COLLISION".into());
        }
        names.insert(name.to_string());
    }
    Ok(names)
}

async fn stop_and_reap_native_child(
    child: &mut tokio::process::Child,
) -> Result<std::process::ExitStatus, String> {
    if let Some(status) = child
        .try_wait()
        .map_err(|error| format!("NATIVE_CODEX_RUNNER_REAP_FAILED:{error}"))?
    {
        return Ok(status);
    }
    if let Err(error) = child.start_kill() {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("NATIVE_CODEX_RUNNER_REAP_FAILED:{error}"))?
        {
            return Ok(status);
        }
        return Err(format!("NATIVE_CODEX_RUNNER_KILL_FAILED:{error}"));
    }
    // Never manufacture a terminal/reaped envelope if the OS cannot confirm it.
    tokio::time::timeout(Duration::from_secs(5), child.wait())
        .await
        .map_err(|_| "NATIVE_CODEX_RUNNER_REAP_TIMEOUT".to_string())?
        .map_err(|error| format!("NATIVE_CODEX_RUNNER_REAP_FAILED:{error}"))
}

async fn read_event_stream(
    stdout: impl AsyncRead + Unpin,
    execution_id: &str,
    summary: &mut EventSummary,
) -> Result<(), String> {
    let mut reader = BufReader::new(stdout);
    let mut line = Vec::new();
    loop {
        let available = reader
            .fill_buf()
            .await
            .map_err(|error| format!("NATIVE_CODEX_EVENT_READ_FAILED:{error}"))?;
        if available.is_empty() {
            if !line.is_empty() {
                apply_event_line(summary, &line, execution_id)?;
            }
            return Ok(());
        }
        let take = available
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(available.len(), |index| index + 1);
        // Check the existing total-stream limit before allocating a partial line.
        // No smaller per-event limit changes the previously accepted protocol.
        if summary.event_stream_bytes.saturating_add(take) > MAX_EVENT_STREAM_BYTES {
            return Err("NATIVE_CODEX_EVENT_STREAM_TOO_LARGE".to_string());
        }
        summary.event_stream_hasher.update(&available[..take]);
        summary.event_stream_bytes += take;
        line.extend_from_slice(&available[..take]);
        let complete = line.last() == Some(&b'\n');
        reader.consume(take);
        if complete {
            apply_event_line(summary, &line, execution_id)?;
            line.clear();
        }
    }
}

fn apply_event_line(
    summary: &mut EventSummary,
    line: &[u8],
    execution_id: &str,
) -> Result<(), String> {
    let line = std::str::from_utf8(line)
        .map_err(|error| format!("NATIVE_CODEX_EVENT_READ_FAILED:{error}"))?;
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return Ok(());
    }
    let event: Value = serde_json::from_str(trimmed)
        .map_err(|error| format!("NATIVE_CODEX_EVENT_INVALID_JSON:{error}"))?;
    apply_event(summary, &event, execution_id)
}

fn apply_event(
    summary: &mut EventSummary,
    event: &Value,
    execution_id: &str,
) -> Result<(), String> {
    match event
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
    {
        "thread.started" => {
            summary.worker_thread_id = event
                .get("thread_id")
                .and_then(Value::as_str)
                .map(str::to_string);
        }
        "item.completed" => {
            if let Some(item) = event.get("item") {
                apply_completed_item(summary, item, execution_id)?;
            }
        }
        "turn.completed" => {
            summary.turn_completed = true;
            summary.usage = event.get("usage").cloned();
        }
        "turn.failed" | "error" => {
            summary.terminal_error = event
                .get("error")
                .and_then(|error| {
                    error
                        .get("message")
                        .and_then(Value::as_str)
                        .or_else(|| error.as_str())
                })
                .or_else(|| event.get("message").and_then(Value::as_str))
                .filter(|message| !message.trim().is_empty())
                .map(str::to_string)
                .or_else(|| Some("NATIVE_CODEX_TERMINAL_FAILURE_EVENT".to_string()));
        }
        _ => {}
    }
    Ok(())
}

fn apply_completed_item(
    summary: &mut EventSummary,
    item: &Value,
    execution_id: &str,
) -> Result<(), String> {
    let item_type = item.get("type").and_then(Value::as_str).unwrap_or_default();
    if item_type == "agent_message" {
        summary.final_text = item
            .get("text")
            .and_then(Value::as_str)
            .or_else(|| item.get("content").and_then(Value::as_str))
            .map(str::to_string);
        return Ok(());
    }
    if matches!(
        item_type,
        "command_execution" | "file_change" | "tool_call" | "collab_tool_call" | "web_search"
    ) {
        return Err(format!("NATIVE_CODEX_TOOL_OWNERSHIP_VIOLATION:{item_type}"));
    }
    if matches!(item_type, "reasoning" | "todo_list" | "error") {
        return Ok(());
    }
    if item_type != "mcp_tool_call" {
        return Err(format!(
            "NATIVE_CODEX_EVENT_ITEM_TYPE_UNSUPPORTED:{item_type}"
        ));
    }

    let item_id = item
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| "NATIVE_CODEX_TOOL_ITEM_ID_MISSING".to_string())?;
    let server_name = item
        .get("server")
        .and_then(Value::as_str)
        .ok_or_else(|| "NATIVE_CODEX_MCP_SERVER_NAME_MISSING".to_string())?;
    if server_name != "tura_command_graph" {
        return Err("NATIVE_CODEX_MCP_SERVER_OWNERSHIP_VIOLATION".to_string());
    }
    let tool_name = item
        .get("tool")
        .and_then(Value::as_str)
        .or_else(|| item.get("tool_name").and_then(Value::as_str))
        .or_else(|| item.get("name").and_then(Value::as_str))
        .ok_or_else(|| "NATIVE_CODEX_TOOL_NAME_MISSING".to_string())?;
    if tool_name != "tura_command_graph" {
        return Err("NATIVE_CODEX_MCP_TOOL_OWNERSHIP_VIOLATION".to_string());
    }
    let input = item
        .get("arguments")
        .or_else(|| item.get("input"))
        .or_else(|| item.get("command"))
        .cloned()
        .unwrap_or(Value::Null);
    let output = item
        .get("result")
        .or_else(|| item.get("output"))
        .or_else(|| item.get("aggregated_output"))
        .cloned()
        .unwrap_or(Value::Null);
    let status = item
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| "NATIVE_CODEX_MCP_TOOL_STATUS_MISSING".to_string())?;
    let transport_error = match status {
        "completed" => false,
        "failed" => true,
        _ => return Err("NATIVE_CODEX_MCP_TOOL_STATUS_INVALID".to_string()),
    };
    // A completed MCP transport may still carry a semantic tool failure.
    // Keep the transport status and the tool-result truth as distinct fields.
    let result_error = match output.get("isError") {
        Some(Value::Bool(value)) => *value,
        None => false,
        Some(_) => return Err("NATIVE_CODEX_MCP_RESULT_ERROR_FLAG_INVALID".to_string()),
    };
    let is_error = transport_error || result_error;
    let input_sha256 = sha256(
        &serde_json::to_vec(&input)
            .map_err(|error| format!("NATIVE_CODEX_TOOL_INPUT_ENCODE_FAILED:{error}"))?,
    );
    let output_sha256 = sha256(
        &serde_json::to_vec(&output)
            .map_err(|error| format!("NATIVE_CODEX_TOOL_OUTPUT_ENCODE_FAILED:{error}"))?,
    );
    let effect_id = format!(
        "native-codex-effect-{}",
        sha256(format!("{execution_id}|{item_id}|{item_type}|{input_sha256}").as_bytes())
    );
    summary.tool_observations.push(NativeCodexToolObservation {
        item_id: item_id.to_string(),
        item_type: item_type.to_string(),
        tool_name: tool_name.to_string(),
        effect_id,
        input_sha256,
        output_sha256,
        status: status.to_string(),
        is_error,
        exit_code: item
            .get("exit_code")
            .and_then(Value::as_i64)
            .and_then(|value| i32::try_from(value).ok()),
    });
    Ok(())
}

async fn read_bounded(mut reader: impl AsyncRead + Unpin, limit: usize) -> Result<Vec<u8>, String> {
    let mut output = Vec::new();
    let mut chunk = [0_u8; 8192];
    loop {
        let read = reader
            .read(&mut chunk)
            .await
            .map_err(|error| format!("NATIVE_CODEX_STDERR_READ_FAILED:{error}"))?;
        if read == 0 {
            break;
        }
        let remaining = limit.saturating_sub(output.len());
        output.extend_from_slice(&chunk[..read.min(remaining)]);
        // Retention is bounded, not consumption: drain until EOF so a verbose
        // child cannot deadlock or receive EPIPE merely from diagnostic volume.
    }
    Ok(output)
}

fn validate_absolute_file(name: &str, path: &Path) -> Result<(), String> {
    if !path.is_absolute() || !path.is_file() {
        return Err(format!("NATIVE_CODEX_RUNNER_PATH_INVALID:{name}"));
    }
    Ok(())
}

fn validate_file_sha256(name: &str, path: &Path, expected: &str) -> Result<(), String> {
    if expected.len() != 64
        || !expected
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(format!("NATIVE_CODEX_RUNNER_FILE_DIGEST_INVALID:{name}"));
    }
    let mut file = fs::File::open(path)
        .map_err(|error| format!("NATIVE_CODEX_RUNNER_FILE_READ_FAILED:{name}:{error}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = std::io::Read::read(&mut file, &mut buffer)
            .map_err(|error| format!("NATIVE_CODEX_RUNNER_FILE_READ_FAILED:{name}:{error}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    if format!("{:x}", hasher.finalize()) != expected {
        return Err(format!("NATIVE_CODEX_RUNNER_FILE_DIGEST_MISMATCH:{name}"));
    }
    Ok(())
}

fn toml_string(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_string())
}

fn sha256(bytes: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(bytes.as_ref()))
}

#[cfg(test)]
mod tests {
    use super::{EventSummary, apply_event};
    use serde_json::json;

    #[tokio::test]
    async fn native_harness_hashes_fragmented_raw_stream_exactly() {
        use tokio::io::AsyncWriteExt;
        let raw = concat!(
            " \r\n",
            "{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"雪\"}}\r\n",
            "{\"type\":\"turn.completed\"}"
        ).as_bytes();
        let (mut writer, reader) = tokio::io::duplex(3);
        let producer = async {
            for byte in raw {
                writer
                    .write_all(&[*byte])
                    .await
                    .expect("write fragmented byte");
            }
            drop(writer);
        };
        let mut summary = EventSummary::default();
        let ((), result) = tokio::join!(
            producer,
            super::read_event_stream(reader, "fragmented", &mut summary)
        );
        result.expect("fragmented event stream");
        assert_eq!(summary.final_text.as_deref(), Some("雪"));
        assert!(summary.turn_completed);
        assert_eq!(summary.event_stream_bytes, raw.len());
        use sha2::Digest;
        assert_eq!(
            format!("{:x}", summary.event_stream_hasher.finalize()),
            super::sha256(raw)
        );
    }

    #[tokio::test]
    async fn native_harness_bounds_unterminated_event_before_allocation() {
        use tokio::io::AsyncReadExt;
        let mut summary = EventSummary::default();
        let input = tokio::io::repeat(b' ').take(super::MAX_EVENT_STREAM_BYTES as u64 + 1);
        let error = super::read_event_stream(input, "oversize", &mut summary)
            .await
            .expect_err("over-limit frame must not wait indefinitely for a newline");
        assert_eq!(error, "NATIVE_CODEX_EVENT_STREAM_TOO_LARGE");
        assert_eq!(summary.event_stream_bytes, super::MAX_EVENT_STREAM_BYTES);
    }

    #[tokio::test]
    async fn native_harness_stderr_retains_prefix_but_consumes_every_byte() {
        for limit in [0, 3, 20] {
            let mut reader = &b"abcdefgh"[..];
            let retained = super::read_bounded(&mut reader, limit)
                .await
                .expect("drain");
            assert_eq!(retained, b"abcdefgh"[..limit.min(8)]);
            assert!(
                reader.is_empty(),
                "retention limit must not close the producer pipe"
            );
        }
    }

    #[test]
    fn native_harness_completed_mcp_transport_preserves_semantic_failure() {
        let mut event = json!({"type":"item.completed","item":{
            "id":"mcp-error","type":"mcp_tool_call","server":"tura_command_graph",
            "tool":"tura_command_graph","arguments":{},
            "result":{"isError":true,"content":[]},"status":"completed"
        }});
        for flag in [true, false] {
            event["item"]["result"]["isError"] = json!(flag);
            let mut summary = EventSummary::default();
            apply_event(&mut summary, &event, "semantic-error").expect("MCP observation");
            assert_eq!(summary.tool_observations[0].status, "completed");
            assert_eq!(summary.tool_observations[0].is_error, flag);
        }
        event["item"]["result"]["isError"] = json!("false");
        let error = apply_event(&mut EventSummary::default(), &event, "invalid-flag")
            .expect_err("non-boolean semantic truth is not accepted");
        assert_eq!(error, "NATIVE_CODEX_MCP_RESULT_ERROR_FLAG_INVALID");
    }

    #[test]
    fn native_harness_empty_terminal_failure_has_a_typed_error() {
        for event_type in ["turn.failed", "error"] {
            let mut summary = EventSummary::default();
            apply_event(&mut summary, &json!({"type":event_type}), "empty-error")
                .expect("recognized terminal event");
            assert_eq!(
                summary.terminal_error.as_deref(),
                Some("NATIVE_CODEX_TERMINAL_FAILURE_EVENT")
            );
        }
    }

    #[test]
    fn native_harness_executable_hash_streams_across_buffer_boundaries() {
        let root = tempfile::tempdir().expect("tempdir");
        let path = root.path().join("binary-fixture");
        let mut bytes = (0..131_099)
            .map(|index| (index % 251) as u8)
            .collect::<Vec<_>>();
        std::fs::write(&path, &bytes).expect("fixture bytes");
        let expected = super::sha256(&bytes);
        super::validate_file_sha256("fixture", &path, &expected).expect("exact streamed digest");
        bytes[65_536] ^= 1;
        std::fs::write(&path, &bytes).expect("same-size corrupted fixture");
        let error = super::validate_file_sha256("fixture", &path, &expected)
            .expect_err("streaming must not weaken byte verification");
        assert_eq!(error, "NATIVE_CODEX_RUNNER_FILE_DIGEST_MISMATCH:fixture");
    }

    #[test]
    fn stable_exec_json_events_map_to_terminal_observations() {
        let mut summary = EventSummary::default();
        for event in [
            json!({"type":"thread.started","thread_id":"thread-native-1"}),
            json!({
                "type":"item.completed",
                "item":{
                    "id":"item-tool-1",
                    "type":"mcp_tool_call",
                    "server":"tura_command_graph",
                    "tool":"tura_command_graph",
                    "arguments":{"commands":[{"command_type":"zsh","command_line":"pwd","step":1}]},
                    "result":{"content":[{"type":"text","text":"ok"}]},
                    "status":"completed"
                }
            }),
            json!({"type":"item.completed","item":{"id":"item-message-1","type":"agent_message","text":"done"}}),
            json!({"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}),
        ] {
            apply_event(&mut summary, &event, "execution-1").expect("event mapping");
        }
        assert_eq!(summary.worker_thread_id.as_deref(), Some("thread-native-1"));
        assert_eq!(summary.final_text.as_deref(), Some("done"));
        assert!(summary.turn_completed);
        assert_eq!(summary.tool_observations.len(), 1);
        assert_eq!(summary.tool_observations[0].tool_name, "tura_command_graph");
        assert!(!summary.tool_observations[0].is_error);
        assert!(
            summary.tool_observations[0]
                .effect_id
                .starts_with("native-codex-effect-")
        );
    }

    #[test]
    fn mcp_tool_result_error_status_is_preserved_and_required() {
        let mut summary = EventSummary::default();
        apply_event(
            &mut summary,
            &json!({
                "type":"item.completed",
                "item":{
                    "id":"item-tool-error",
                    "type":"mcp_tool_call",
                    "server":"tura_command_graph",
                    "tool":"tura_command_graph",
                    "arguments":{"commands":[]},
                    "result":{"content":[{"type":"text","text":"typed failure"}]},
                    "status":"failed"
                }
            }),
            "execution-error",
        )
        .expect("typed MCP error result remains an observation");
        assert_eq!(summary.tool_observations[0].status, "failed");
        assert!(summary.tool_observations[0].is_error);

        let error = apply_event(
            &mut EventSummary::default(),
            &json!({
                "type":"item.completed",
                "item":{
                    "id":"item-tool-missing-status",
                    "type":"mcp_tool_call",
                    "server":"tura_command_graph",
                    "tool":"tura_command_graph",
                    "arguments":{"commands":[]},
                    "result":{"content":[]}
                }
            }),
            "execution-missing-status",
        )
        .expect_err("MCP result without isError is not valid evidence");
        assert_eq!(error, "NATIVE_CODEX_MCP_TOOL_STATUS_MISSING");
    }

    #[test]
    fn core_effect_tools_are_rejected_as_ownership_violations() {
        for item_type in [
            "command_execution",
            "file_change",
            "tool_call",
            "collab_tool_call",
            "web_search",
        ] {
            let error = apply_event(
                &mut EventSummary::default(),
                &json!({
                    "type":"item.completed",
                    "item":{
                        "id":format!("item-{item_type}"),
                        "type":item_type,
                        "status":"completed"
                    }
                }),
                "execution-ownership-violation",
            )
            .expect_err("core effect tool must not bypass the Tura command graph");
            assert_eq!(
                error,
                format!("NATIVE_CODEX_TOOL_OWNERSHIP_VIOLATION:{item_type}")
            );
        }

        let error = apply_event(
            &mut EventSummary::default(),
            &json!({
                "type":"item.completed",
                "item":{"id":"item-future","type":"future_tool_variant"}
            }),
            "execution-future-variant",
        )
        .expect_err("unknown future item variants must fail closed");
        assert_eq!(
            error,
            "NATIVE_CODEX_EVENT_ITEM_TYPE_UNSUPPORTED:future_tool_variant"
        );
    }
}
