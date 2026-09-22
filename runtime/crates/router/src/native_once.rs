//! One request, one Native worker, one terminal. No AppState, Gateway,
//! Session DB, recovery scan, callbacks, planner, or continuation dispatcher.
//! This is an entry mode of the existing Router effect owner, not a daemon.
use anyhow::{Context, Result, anyhow, bail};
use router_contract::{IpcRequest, IpcResponse};
use runtime_contract::{NativeCodexTaskDelta, NativeCodexTerminalEnvelope, TaskContextCapsule};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{path::{Path, PathBuf}, process::Stdio, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream}, process::Command, task::JoinSet,
};
use crate::services::{command_run::CommandRunService,
    process_scope::{attach_child_scope, configure_scoped_spawn}};

const MAX_REQUEST: usize = 2 * 1024 * 1024;
const MAX_TERMINAL: usize = 16 * 1024 * 1024;
const CLEANUP_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone)]
struct BoundTask {
    session: String,
    execution: String,
    workspace: PathBuf,
    allowed_commands: Value,
    jspace: Value,
}

fn required<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value.get(key).and_then(Value::as_str).filter(|s| !s.trim().is_empty())
        .ok_or_else(|| anyhow!("NATIVE_ONCE_FIELD_MISSING:{key}"))
}

impl BoundTask {
    fn from_request(value: &Value) -> Result<Self> {
        if value.get("schema_version").and_then(Value::as_str) != Some("tura_native_codex_worker_request_v3") {
            bail!("NATIVE_ONCE_REQUEST_SCHEMA_UNSUPPORTED");
        }
        let mut binding = value.clone();
        binding.as_object_mut().context("NATIVE_ONCE_REQUEST_OBJECT_REQUIRED")?
            .remove("execution_binding_sha256");
        let digest = runtime_contract::task_context_semantic_sha256_v1(&binding).map_err(|e| anyhow!(e))?;
        if required(value, "execution_binding_sha256")? != digest {
            bail!("NATIVE_ONCE_EXECUTION_BINDING_MISMATCH");
        }
        let workspace = PathBuf::from(required(value, "workspace")?);
        if !workspace.is_absolute() || !workspace.is_dir() { bail!("NATIVE_ONCE_WORKSPACE_INVALID"); }
        let workspace = workspace.canonicalize()?;
        let capsule = TaskContextCapsule::from_value(value["task_context_capsule"].clone()).map_err(|e| anyhow!(e))?;
        let delta = NativeCodexTaskDelta::from_value(value["task_delta"].clone()).map_err(|e| anyhow!(e))?;
        delta.bind_capsule(&capsule).map_err(|e| anyhow!(e))?;
        if delta.task_id != required(value, "task_id")?
            || delta.semantic_sha256 != required(value, "expected_task_delta_sha256")?
            || capsule.semantic_sha256 != required(value, "expected_task_context_capsule_sha256")?
            || capsule.jspace_semantic_sha256 != required(value, "expected_jspace_semantic_sha256")?
            || Path::new(required(&value["task_context_capsule"]["surface"], "repo_root")?).canonicalize()? != workspace
        { bail!("NATIVE_ONCE_CONTEXT_BINDING_MISMATCH"); }
        required(value, "lease_id")?;
        let graph = &value["command_graph"];
        let jspace = graph["jspace_contract"].clone();
        capsule.bind_jspace(Some(&jspace)).map_err(|e| anyhow!(e))?;
        tura_path::jspace::JSpaceMatcher::from_value(&workspace, &jspace).map_err(|e| anyhow!(e.to_string()))?;
        let allowed_commands = graph["allowed_commands"].clone();
        let commands = allowed_commands.as_array().context("NATIVE_ONCE_COMMANDS_INVALID")?;
        if commands.is_empty() || commands.iter().any(|v| !matches!(v.as_str(),
            Some("bash" | "zsh" | "shell_command" | "apply_patch" | "read_media" | "web_discover"))) {
            bail!("NATIVE_ONCE_ORCHESTRATION_COMMAND_NOT_ALLOWED");
        }
        Ok(Self { session: required(value, "session_id")?.to_string(),
            execution: required(value, "execution_id")?.to_string(), workspace,
            allowed_commands, jspace })
    }

    fn admit(&self, request: &IpcRequest) -> Result<Value> {
        if request.kind != "call" || request.method != "execution.command_run" {
            bail!("NATIVE_ONCE_EFFECT_METHOD_REQUIRED");
        }
        let p = &request.payload;
        let commands = p["allowed_commands"].as_array().context("NATIVE_ONCE_COMMANDS_INVALID")?;
        let expected = self.allowed_commands.as_array().context("NATIVE_ONCE_COMMANDS_INVALID")?;
        let actual_set: std::collections::BTreeSet<_> = commands.iter().filter_map(Value::as_str).collect();
        let expected_set: std::collections::BTreeSet<_> = expected.iter().filter_map(Value::as_str).collect();
        if required(p, "session_id")? != self.session
            || required(p, "runtime_id")? != self.execution
            || Path::new(required(p, "session_directory")?).canonicalize()? != self.workspace
            || actual_set != expected_set || actual_set.len() != commands.len()
            || serde_json::to_string(&p["jspace_contract"])? != serde_json::to_string(&self.jspace)?
            || p.get("sandbox").and_then(Value::as_bool) != Some(true)
            || p.get("command_env").and_then(Value::as_object).is_none_or(|v| !v.is_empty())
        { bail!("NATIVE_ONCE_EFFECT_BINDING_MISMATCH"); }
        let arguments = &p["arguments"];
        let batch = required(arguments, "execution_id")?;
        if !batch.starts_with(&format!("native-graph:{}:", self.execution)) || request.request_id != batch {
            bail!("NATIVE_ONCE_EFFECT_IDENTITY_MISMATCH");
        }
        Ok(json!({"session_id":self.session,"runtime_id":self.execution,
            "session_directory":self.workspace,"arguments":arguments,
            "allowed_commands":self.allowed_commands,"jspace_contract":self.jspace,
            "command_env":{},"sandbox":true}))
    }
}

async fn bounded_read(reader: impl AsyncRead + Unpin, limit: usize) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    reader.take(limit as u64 + 1).read_to_end(&mut bytes).await?;
    if bytes.len() > limit { bail!("NATIVE_ONCE_OUTPUT_TOO_LARGE"); }
    Ok(bytes)
}

// Diagnostic retention is bounded; consumption must continue to EOF so a
// verbose worker cannot deadlock or fail merely because its stderr is large.
async fn drain_stderr(mut reader: impl AsyncRead + Unpin) -> Result<Vec<u8>> {
    let mut retained = Vec::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let read = reader.read(&mut buffer).await?;
        if read == 0 { return Ok(retained); }
        let remaining = (1024 * 1024_usize).saturating_sub(retained.len());
        retained.extend_from_slice(&buffer[..read.min(remaining)]);
    }
}

async fn handle_connection(stream: TcpStream, task: Arc<BoundTask>, service: CommandRunService) -> Result<()> {
    let (read, mut write) = stream.into_split();
    let mut reader = BufReader::new(read).take(MAX_REQUEST as u64 + 1);
    let mut line = Vec::new();
    reader.read_until(b'\n', &mut line).await?;
    if line.len() > MAX_REQUEST { bail!("NATIVE_ONCE_RPC_TOO_LARGE"); }
    let request: IpcRequest = serde_json::from_slice(&line)?;
    let response = match task.admit(&request) {
        Ok(payload) => match service.execute_with_request_id(payload, Some(&request.request_id)).await {
            Ok(result) => IpcResponse::ok(&request.request_id, result),
            Err(error) => IpcResponse::error(&request.request_id, error.to_string()),
        },
        Err(error) => IpcResponse::error(&request.request_id, error.to_string()),
    };
    let mut bytes = serde_json::to_vec(&response)?; bytes.push(b'\n');
    write.write_all(&bytes).await?; write.shutdown().await?;
    Ok(())
}

async fn cancellation_signal() -> Result<()> {
    #[cfg(unix)] {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        tokio::select! { r = tokio::signal::ctrl_c() => { r?; }, _ = term.recv() => {} }
    }
    #[cfg(not(unix))] { tokio::signal::ctrl_c().await?; }
    Ok(())
}

async fn confirm_worker_group_idle(pgid: u32) -> Result<()> {
    confirm_worker_group_idle_with_timeout(pgid, CLEANUP_TIMEOUT).await
}

// Like services/process_scope.rs, this narrow process boundary uses the OS
// process-group API. No unsafe code is permitted elsewhere in this module.
#[allow(unsafe_code)]
async fn confirm_worker_group_idle_with_timeout(pgid: u32, limit: Duration) -> Result<()> {
    #[cfg(unix)] {
        let mut observation_error = None;
        let result = tokio::time::timeout(limit, async {
            loop {
                unsafe extern "C" { fn kill(pid: i32, signal: i32) -> i32; }
                let group = i32::try_from(pgid).context("NATIVE_ONCE_PROCESS_GROUP_INVALID")?;
                if group <= 0 { bail!("NATIVE_ONCE_PROCESS_GROUP_INVALID"); }
                // SAFETY: group is the positive PID returned by our scoped
                // spawn; signal zero performs an existence check, no delivery.
                // It inspects only our owned group, not the global process table.
                let observed = unsafe { kill(-group, 0) };
                if observed != 0 {
                    let error = std::io::Error::last_os_error();
                    // Darwin also returns EPERM for a zombie-only group. Wait
                    // for reaping within the existing deadline; only ESRCH is
                    // proof of absence, never EPERM itself.
                    if error.raw_os_error() == Some(3) { return Ok(()); }
                    if error.raw_os_error() != Some(1) {
                        return Err(error).context("NATIVE_ONCE_PROCESS_OBSERVATION_FAILED");
                    }
                    observation_error = Some(error);
                } else {
                    observation_error = None;
                }
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
        }).await;
        match result {
            Ok(result) => result,
            Err(_) => match observation_error {
                Some(error) => Err(error).context("NATIVE_ONCE_PROCESS_OBSERVATION_FAILED"),
                None => bail!("NATIVE_ONCE_PROCESS_GROUP_UNSETTLED"),
            },
        }
    }
    #[cfg(not(unix))] { let _ = (pgid, limit); bail!("NATIVE_ONCE_PLATFORM_NOT_SUPPORTED"); }
}

pub(crate) async fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(2).collect();
    if args.len() != 4 || args[0] != "--worker" || args[2] != "--worker-sha256" {
        bail!("usage: tura_router native-once --worker ABSOLUTE_PATH --worker-sha256 SHA256");
    }
    let worker = Path::new(&args[1]);
    if !worker.is_absolute() || !worker.is_file() { bail!("NATIVE_ONCE_WORKER_PATH_INVALID"); }
    let mut file = std::fs::File::open(worker)?;
    let mut digest = Sha256::new();
    std::io::copy(&mut file, &mut digest)?;
    if format!("{:x}", digest.finalize()) != args[3] { bail!("NATIVE_ONCE_WORKER_DIGEST_MISMATCH"); }
    let raw = bounded_read(tokio::io::stdin(), MAX_REQUEST).await?;
    let request: Value = serde_json::from_slice(&raw)?;
    let bound = Arc::new(BoundTask::from_request(&request)?);
    let timeout_ms = request["timeout_ms"].as_u64().filter(|v| *v > 0 && *v <= 86_400_000)
        .context("NATIVE_ONCE_TIMEOUT_INVALID")?;
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let service = CommandRunService::new();
    let mut command = Command::new(worker);
    command.current_dir(&bound.workspace).env("TURA_ROUTER_ADDR", listener.local_addr()?.to_string())
        .env("TURA_COMMAND_RUN_SANDBOX", "true")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    // Do not inherit an alternative MCP command graph from the parent shell.
    for key in ["TURA_FORCED_CAPABILITY_DIRECTORIES", "TURA_MCP_STDIO_BRIDGE_BIN", "TURA_MCP_SERVER_COMMAND",
        "TURA_MCP_SERVER_ARGS_JSON", "TURA_MCP_SERVER_NAME", "TURA_MCP_SERVER_TRANSPORT",
        "TURA_MCP_COMMAND_ID", "TURA_MCP_BROKER_ADDR", "TURA_MCP_BROKER_TOKEN"] { command.env_remove(key); }
    configure_scoped_spawn(&mut command);
    let mut child = command.spawn().context("NATIVE_ONCE_WORKER_SPAWN_FAILED")?;
    let pid = child.id().context("NATIVE_ONCE_WORKER_PID_MISSING")?;
    let process_scope = match attach_child_scope(&child) {
        Ok(Some(scope)) => scope,
        _ => { let _ = child.kill().await; bail!("NATIVE_ONCE_PROCESS_SCOPE_UNAVAILABLE"); }
    };
    let mut stdin = child.stdin.take().context("NATIVE_ONCE_STDIN_MISSING")?;
    let stdout = child.stdout.take().context("NATIVE_ONCE_STDOUT_MISSING")?;
    let stderr = child.stderr.take().context("NATIVE_ONCE_STDERR_MISSING")?;
    let mut connections = JoinSet::new();
    let outcome = {
        let io = tokio::time::timeout(Duration::from_millis(timeout_ms) + Duration::from_secs(5), async {
            tokio::try_join!(
                async { stdin.write_all(&raw).await?; drop(stdin); Ok::<_, anyhow::Error>(()) },
                bounded_read(stdout, MAX_TERMINAL), drain_stderr(stderr),
                async { child.wait().await.map_err(anyhow::Error::from) },
            )
        });
        tokio::pin!(io);
        let cancelled = cancellation_signal(); tokio::pin!(cancelled);
        loop {
            tokio::select! {
                result = &mut io => break result.context("NATIVE_ONCE_DEADLINE_EXCEEDED").and_then(|r| r),
                _ = &mut cancelled => break Err(anyhow!("NATIVE_ONCE_CANCELLED")),
                accepted = listener.accept() => match accepted {
                    Ok((stream,_)) => { connections.spawn(handle_connection(stream, Arc::clone(&bound), service.clone())); },
                    Err(error) => break Err(error.into()),
                },
                _ = connections.join_next(), if !connections.is_empty() => {},
            }
        }
    };
    // Stop admission before cancellation: no late socket task may create work
    // after the command service was observed idle.
    drop(listener);
    connections.abort_all();
    while connections.join_next().await.is_some() {}
    service.cancel_session(&bound.session);
    let effects_idle = tokio::time::timeout(CLEANUP_TIMEOUT, service.wait_for_session_idle(&bound.session)).await;
    process_scope.terminate();
    let reaped = tokio::time::timeout(CLEANUP_TIMEOUT, child.wait()).await;
    code_tools::shell_executor::terminate_retained_shell_process_scopes();
    effects_idle.context("NATIVE_ONCE_EFFECTS_UNSETTLED")?;
    reaped.context("NATIVE_ONCE_WORKER_REAP_TIMEOUT")??;
    confirm_worker_group_idle(pid).await?;
    let ((), output, stderr, status) = outcome?;
    if !status.success() { bail!("NATIVE_ONCE_WORKER_FAILED:{}", String::from_utf8_lossy(&stderr)); }
    let terminal: NativeCodexTerminalEnvelope = serde_json::from_slice(&output).context("NATIVE_ONCE_TERMINAL_INVALID")?;
    terminal.validate_shape().map_err(|e| anyhow!(e))?;
    for (key, actual) in [
        ("task_id", terminal.task_id.as_str()), ("execution_id", terminal.execution_id.as_str()),
        ("lease_id", terminal.lease_id.as_str()), ("execution_profile_sha256", terminal.execution_profile_sha256.as_str()),
        ("execution_binding_sha256", terminal.execution_binding_sha256.as_str()),
        ("expected_task_context_capsule_sha256", terminal.task_context_capsule_sha256.as_str()),
        ("expected_task_delta_sha256", terminal.task_delta_sha256.as_str()),
    ] { if required(&request,key)? != actual { bail!("NATIVE_ONCE_TERMINAL_BINDING_MISMATCH:{key}"); } }
    let mut out = tokio::io::stdout();
    out.write_all(&output).await?; out.flush().await?;
    Ok(())
}

#[cfg(all(test, target_os = "macos"))]
mod cleanup_tests {
    use super::*;

    async fn zombie_group() -> (tokio::process::Child, u32) {
        // The fixture parent stays outside the child's group and controls when
        // the zombie is reaped. No model, broker, or tool effects are involved.
        let mut parent = Command::new("/usr/bin/python3")
            .args(["-B", "-c", "import os,sys,time\np=os.fork()\nif p==0:\n os.setpgid(0,0); os._exit(0)\ntime.sleep(.05)\nprint(p,flush=True)\nsys.stdin.readline()\nos.waitpid(p,0)"])
            .stdin(Stdio::piped()).stdout(Stdio::piped()).kill_on_drop(true)
            .spawn().expect("fixture starts");
        let mut line = String::new();
        BufReader::new(parent.stdout.take().expect("stdout"))
            .read_line(&mut line).await.expect("fixture PID");
        (parent, line.trim().parse().expect("positive PID"))
    }

    #[tokio::test]
    async fn zombie_group_waits_until_reaped() {
        let (mut parent, pgid) = zombie_group().await;
        let reap = async {
            tokio::time::sleep(Duration::from_millis(100)).await;
            parent.stdin.take().expect("stdin").write_all(b"\n").await.expect("reap");
            parent.wait().await.expect("parent reaped")
        };
        let (result, status) = tokio::join!(
            confirm_worker_group_idle_with_timeout(pgid, Duration::from_secs(2)), reap);
        assert!(status.success());
        assert!(result.is_ok(), "{result:?}");
    }

    #[tokio::test]
    async fn persistent_eperm_is_not_success() {
        let (mut parent, pgid) = zombie_group().await;
        let result = confirm_worker_group_idle_with_timeout(pgid, Duration::from_millis(75)).await;
        parent.stdin.take().expect("stdin").write_all(b"\n").await.expect("reap");
        assert!(parent.wait().await.expect("parent reaped").success());
        let error = result.expect_err("unreaped group must fail closed");
        assert!(error.to_string().contains("NATIVE_ONCE_PROCESS_OBSERVATION_FAILED"));
        assert_eq!(error.root_cause().downcast_ref::<std::io::Error>()
            .and_then(std::io::Error::raw_os_error), Some(1));
    }

    #[tokio::test]
    async fn live_group_and_invalid_identity_fail_closed() {
        let mut command = Command::new("/bin/sleep");
        command.arg("30");
        configure_scoped_spawn(&mut command);
        let mut child = command.spawn().expect("owned sleep");
        let pid = child.id().expect("child PID");
        let result = confirm_worker_group_idle_with_timeout(pid, Duration::from_millis(75)).await;
        child.kill().await.expect("owned child cleanup");
        assert!(result.expect_err("live group must fail").to_string()
            .contains("NATIVE_ONCE_PROCESS_GROUP_UNSETTLED"));
        assert!(confirm_worker_group_idle(0).await.is_err());
    }
}
