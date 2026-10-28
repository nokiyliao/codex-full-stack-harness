//! A private inherited capability, never a listener or model-selected command.
use code_tools::shell_executor::{VerifierObservation, focused_verifier_policy};
use serde::Deserialize;
use serde_json::{Value, json};
use std::io::Read;
use std::path::PathBuf;
use std::sync::OnceLock;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::Mutex;
use tura_path::jspace::JSpaceMatcher;

static CHANNEL: OnceLock<Mutex<Option<tokio::net::UnixStream>>> = OnceLock::new();

#[allow(unsafe_code, reason = "take ownership of one explicitly inherited socket; cloned fd is CLOEXEC")]
pub(crate) fn initialize() -> anyhow::Result<()> {
    use std::os::fd::FromRawFd;
    let Some(value) = std::env::var_os("NOKIY_VERIFIER_FD") else { return Ok(()) };
    anyhow::ensure!(std::env::args().nth(1).as_deref() == Some("serve-socket"), "VERIFIER_CHANNEL_WRONG_PROCESS");
    let fd: i32 = value.to_str().ok_or_else(|| anyhow::anyhow!("VERIFIER_FD_INVALID"))?.parse()?;
    anyhow::ensure!(fd >= 3, "VERIFIER_FD_INVALID");
    let inherited = unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) };
    let stream = inherited.try_clone()?;
    drop(inherited);
    stream.peer_addr()?;
    stream.set_nonblocking(true)?;
    // Tokio registration must occur inside the existing router runtime.
    RAW_CHANNEL.set(Mutex::new(Some(stream))).map_err(|_| anyhow::anyhow!("VERIFIER_CHANNEL_DUPLICATE"))?;
    Ok(())
}

static RAW_CHANNEL: OnceLock<Mutex<Option<std::os::unix::net::UnixStream>>> = OnceLock::new();

pub(crate) fn available() -> bool { RAW_CHANNEL.get().is_some() }

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Reply {
    version: u8,
    binding: String,
    call_id: String,
    verifier_index: usize,
    result: VerifierObservation,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ShellReply {
    version: u8,
    binding: String,
    call_id: String,
    shell: bool,
    result: VerifierObservation,
}

const VERIFIER_REQUEST_LIMIT: usize = 4096;
const VERIFIER_REPLY_LIMIT: usize = 262_143;
const SHELL_REQUEST_LIMIT: usize = 262_144;
const SHELL_REPLY_LIMIT: usize = 8 * 1024 * 1024;

pub(crate) async fn request(
    matcher: &JSpaceMatcher, call_id: &str, index: usize,
    cancellation: &code_tools::runtime::tool::CancellationToken,
) -> Result<VerifierObservation, String> {
    let duration = matcher.check_verifier_command(index).map_err(|e| e.to_string())?.timeout_seconds;
    let mut channel = acquire(cancellation).await?;
    exchange(&mut channel, matcher.authorization_semantic_sha256(), call_id, index, duration, cancellation).await
}

/// One parent-executed read-only shell line for a contract that admits `parent_shell`. The parent
/// applies the profile the contract binds by digest; the channel carries only the line and outcome.
pub(crate) async fn request_shell(
    matcher: &JSpaceMatcher, call_id: &str, line: &str, workdir: Option<&str>, timeout_seconds: u64,
    cancellation: &code_tools::runtime::tool::CancellationToken,
) -> Result<VerifierObservation, String> {
    if !matcher.parent_shell_admitted() {
        return Err("PARENT_SHELL_NOT_ADMITTED".into());
    }
    let mut channel = acquire(cancellation).await?;
    exchange_shell(&mut channel, matcher.authorization_semantic_sha256(), call_id, line, workdir,
        timeout_seconds, cancellation).await
}

async fn acquire(
    cancellation: &code_tools::runtime::tool::CancellationToken,
) -> Result<tokio::sync::MutexGuard<'static, Option<tokio::net::UnixStream>>, String> {
    let channel = CHANNEL.get_or_init(|| Mutex::new(None));
    let mut channel = tokio::select! {
        locked = channel.lock() => locked,
        _ = cancellation.cancelled() => return Err("VERIFIER_REPLY_CANCELLED".into()),
    };
    if cancellation.is_cancelled() { return Err("VERIFIER_REPLY_CANCELLED".into()); }
    if channel.is_none() {
        let raw = RAW_CHANNEL.get().ok_or("VERIFIER_CHANNEL_MISSING")?.lock().await.take()
            .ok_or("VERIFIER_CHANNEL_UNSETTLED")?;
        *channel = Some(tokio::net::UnixStream::from_std(raw).map_err(|e| e.to_string())?);
    }
    Ok(channel)
}

async fn exchange(
    channel: &mut Option<tokio::net::UnixStream>, binding: &str, call_id: &str,
    index: usize, duration: u64,
    cancellation: &code_tools::runtime::tool::CancellationToken,
) -> Result<VerifierObservation, String> {
    let request = json!({"version":1,"binding":binding,"call_id":call_id,"verifier_index":index});
    transact(channel, &request, VERIFIER_REQUEST_LIMIT, VERIFIER_REPLY_LIMIT, duration, cancellation, |bytes| {
        let reply: Reply = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        if reply.version != 1 || reply.binding != binding || reply.call_id != call_id || reply.verifier_index != index {
            return Err("VERIFIER_REPLY_IDENTITY_MISMATCH".into());
        }
        Ok(reply.result)
    }).await
}

#[allow(clippy::too_many_arguments)]
async fn exchange_shell(
    channel: &mut Option<tokio::net::UnixStream>, binding: &str, call_id: &str, line: &str,
    workdir: Option<&str>, timeout_seconds: u64,
    cancellation: &code_tools::runtime::tool::CancellationToken,
) -> Result<VerifierObservation, String> {
    let request = json!({"version":1,"binding":binding,"call_id":call_id,
        "shell":{"line":line,"workdir":workdir,"timeout_seconds":timeout_seconds}});
    transact(channel, &request, SHELL_REQUEST_LIMIT, SHELL_REPLY_LIMIT, timeout_seconds, cancellation, |bytes| {
        let reply: ShellReply = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        if reply.version != 1 || reply.binding != binding || reply.call_id != call_id || !reply.shell {
            return Err("PARENT_SHELL_REPLY_IDENTITY_MISMATCH".into());
        }
        Ok(reply.result)
    }).await
}

async fn transact<F>(
    channel: &mut Option<tokio::net::UnixStream>, request: &Value, request_limit: usize,
    reply_limit: usize, duration: u64,
    cancellation: &code_tools::runtime::tool::CancellationToken, parse: F,
) -> Result<VerifierObservation, String>
where F: FnOnce(&[u8]) -> Result<VerifierObservation, String> {
    let mut bytes = serde_json::to_vec(request).map_err(|e| e.to_string())?;
    bytes.push(b'\n');
    // Refused before the stream is taken, so an oversized request leaves the channel usable.
    if bytes.len() > request_limit { return Err("VERIFIER_REQUEST_TOO_LARGE".into()) }
    // Removing it poisons reuse on cancellation, panic, partial send or bad reply.
    let mut stream = channel.take().ok_or("VERIFIER_CHANNEL_UNSETTLED")?;
    let transaction = async {
        stream.write_all(&bytes).await.map_err(|e| e.to_string())?;
        let mut response = Vec::new();
        let mut chunk = vec![0u8; 65536];
        loop {
            let read = stream.read(&mut chunk).await.map_err(|e| e.to_string())?;
            if read == 0 { return Err("VERIFIER_CHANNEL_CLOSED".into()) }
            let end = chunk[..read].iter().position(|byte| *byte == b'\n');
            let take = end.unwrap_or(read);
            if response.len() + take > reply_limit { return Err("VERIFIER_REPLY_TOO_LARGE".into()) }
            response.extend_from_slice(&chunk[..take]);
            if let Some(end) = end {
                // One request, one reply line: trailing bytes would desynchronize the next call.
                if end + 1 != read { return Err("VERIFIER_REPLY_TRAILING_BYTES".into()) }
                break;
            }
        }
        parse(&response)
    };
    let result = tokio::select! {
        result = tokio::time::timeout(Duration::from_secs(duration + 20), transaction) =>
            result.map_err(|_| "VERIFIER_REPLY_TIMEOUT".to_owned())?,
        _ = cancellation.cancelled() => Err("VERIFIER_REPLY_CANCELLED".into()),
    };
    if result.as_ref().is_ok_and(|observation| observation.outcome == "known"
        && observation.process_reaped && observation.process_group_empty) {
        *channel = Some(stream);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use code_tools::runtime::tool::CancellationToken;

    #[tokio::test]
    async fn unknown_cleanup_poisoned_channel_never_sends_second_call() {
        let (client, mut server) = tokio::net::UnixStream::pair().unwrap();
        let peer = tokio::spawn(async move {
            while server.read_u8().await.unwrap() != b'\n' {}
            let mut reply = serde_json::to_vec(&json!({"version":1,"binding":"binding",
                "call_id":"first","verifier_index":0,"result":{
                    "success":false,"exit_code":-1,"stdout":"","stderr":"",
                    "outcome":"unknown","process_reaped":true,"process_group_empty":false
                }})).unwrap();
            reply.push(b'\n');
            server.write_all(&reply).await.unwrap();
            let mut byte = [0u8;1];
            assert_eq!(server.read(&mut byte).await.unwrap(), 0);
        });
        let mut channel = Some(client);
        let cancel = CancellationToken::new();
        let first = exchange(&mut channel, "binding", "first", 0, 1, &cancel).await.unwrap();
        assert_eq!(first.outcome, "unknown");
        assert!(channel.is_none());
        let second = exchange(&mut channel, "binding", "second", 0, 1, &cancel).await;
        assert_eq!(second.unwrap_err(), "VERIFIER_CHANNEL_UNSETTLED");
        tokio::time::timeout(Duration::from_secs(1), peer).await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn shell_exchange_binds_identity_and_keeps_only_a_settled_channel() {
        let (client, mut server) = tokio::net::UnixStream::pair().unwrap();
        let peer = tokio::spawn(async move {
            for (call, shell) in [("one", true), ("two", false)] {
                let mut request = Vec::new();
                loop {
                    let byte = server.read_u8().await.unwrap();
                    if byte == b'\n' { break; }
                    request.push(byte);
                }
                let request: Value = serde_json::from_slice(&request).unwrap();
                assert_eq!(request["shell"]["line"], "git diff --check");
                assert_eq!(request["call_id"], call);
                let mut reply = serde_json::to_vec(&json!({"version":1,"binding":"binding","call_id":call,
                    "shell":shell,"result":{"success":true,"exit_code":0,"stdout":"x".repeat(100_000),
                    "stderr":"","outcome":"known","process_reaped":true,"process_group_empty":true}})).unwrap();
                reply.push(b'\n');
                server.write_all(&reply).await.unwrap();
            }
        });
        let mut channel = Some(client);
        let cancel = CancellationToken::new();
        let first = exchange_shell(&mut channel, "binding", "one", "git diff --check", None, 5, &cancel)
            .await.unwrap();
        assert_eq!(first.stdout.len(), 100_000);
        assert!(channel.is_some(), "a settled reply keeps the channel");
        let second = exchange_shell(&mut channel, "binding", "two", "git diff --check", Some("/tmp"), 5, &cancel).await;
        assert_eq!(second.unwrap_err(), "PARENT_SHELL_REPLY_IDENTITY_MISMATCH");
        assert!(channel.is_none(), "a mismatched reply poisons the channel");
        tokio::time::timeout(Duration::from_secs(1), peer).await.unwrap().unwrap();
        let (client, _server) = tokio::net::UnixStream::pair().unwrap();
        let mut channel = Some(client);
        let long = "x".repeat(SHELL_REQUEST_LIMIT);
        let refused = exchange_shell(&mut channel, "binding", "three", &long, None, 5, &cancel).await;
        assert_eq!(refused.unwrap_err(), "VERIFIER_REQUEST_TOO_LARGE");
        assert!(channel.is_some(), "an oversized request never takes the stream");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn plan_preserves_legacy_shape_and_echoes_only_bound_import_roots() {
        use sha2::{Digest, Sha256};
        use std::fs;
        use std::os::unix::fs::PermissionsExt;
        use tura_path::jspace::{authorization_semantic_sha256, semantic_sha256};
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let workspace = root.join("workspace");
        let artifacts = root.join("artifacts");
        let scratch = artifacts.join("scratch");
        fs::create_dir_all(workspace.join("src")).unwrap();
        fs::create_dir_all(&scratch).unwrap();
        let executable = workspace.join("python3.13");
        let pinned = workspace.join("src/check.py");
        fs::write(&executable, [0xfe, 0xed, 0xfa, 0xcf, 1, 2, 3, 4]).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
        fs::write(&pinned, "pass\n").unwrap();
        let digest = |path: &std::path::Path| format!("{:x}", Sha256::digest(fs::read(path).unwrap()));
        let mut contract = json!({
            "schema_version":"jspace_contract_v2", "repo_root":workspace,
            "dcf_generation":{"repo_root":workspace,"generation_id":"plan","required_domain_bindings":{}},
            "provenance":{}, "matched_surface_ids":[], "read_scopes":["src/check.py"],
            "write_scopes":[], "allowed_operations":["read","command"],
            "denied_operations":["network","install","system_mutation"],
            "command_templates":[], "focused_verifiers":[], "declared_targets":[],
            "expansion":{"mode":"exact_target_only","error_code":"JSPACE_EXPANSION_REQUIRED","mutation_on_expansion":false},
            "verifier_artifact_root":artifacts, "verifier_commands":[{
                "argv":[executable,pinned],"executable_sha256":digest(&executable),
                "pinned_files":[{"path":pinned,"sha256":digest(&pinned)}],
                "timeout_seconds":10,"scratch_root":scratch,"network":false
            }]
        });
        contract["authorization_semantic_sha256"] = json!(authorization_semantic_sha256(&contract).unwrap());
        contract["content_sha256"] = json!(semantic_sha256(&contract));
        let mut request = OnceRequest {
            workspace: workspace.clone(),
            binding: contract["authorization_semantic_sha256"].as_str().unwrap().into(),
            contract, verifier_index: 0,
        };
        // The profile denies the TCP ports listening when it is built; other tests in this process
        // open and close listeners concurrently, so compare plans without that one volatile line.
        let stable = |mut plan: Value| {
            let profile = plan["profile"].as_str().unwrap_or_default().lines()
                .filter(|line| !line.starts_with("(deny network-outbound"))
                .collect::<Vec<_>>().join("\n");
            plan["profile"] = json!(profile);
            plan
        };
        let legacy = stable(plan_value(&request).unwrap());
        assert_eq!(legacy, json!({
            "binding":request.binding,"verifier_index":0,"profile":legacy["profile"],
            "argv":[executable,pinned],"scratch_root":scratch,"timeout_seconds":10
        }));
        let roots = json!([workspace.join("src")]);
        request.contract["verifier_commands"][0]["python_import_roots"] = roots.clone();
        request.contract["authorization_semantic_sha256"] =
            json!(authorization_semantic_sha256(&request.contract).unwrap());
        request.contract.as_object_mut().unwrap().remove("content_sha256");
        request.contract["content_sha256"] = json!(semantic_sha256(&request.contract));
        request.binding = request.contract["authorization_semantic_sha256"].as_str().unwrap().into();
        let mut expected = legacy.clone();
        expected["binding"] = json!(request.binding);
        expected["python_import_roots"] = roots;
        assert_eq!(stable(plan_value(&request).unwrap()), expected);
        // V318: a granted cwd joins the plan; the OS profile is unchanged by it.
        request.contract["verifier_commands"][0]["cwd"] = json!(workspace.join("src"));
        request.contract["authorization_semantic_sha256"] =
            json!(authorization_semantic_sha256(&request.contract).unwrap());
        request.contract.as_object_mut().unwrap().remove("content_sha256");
        request.contract["content_sha256"] = json!(semantic_sha256(&request.contract));
        request.binding = request.contract["authorization_semantic_sha256"].as_str().unwrap().into();
        let planned = stable(plan_value(&request).unwrap());
        assert_eq!(planned["cwd"], json!(workspace.join("src")));
        assert_eq!(planned["profile"], legacy["profile"]);
        // The profile stays byte-for-byte unchanged: roots add no OS permissions.
        request.binding = legacy["binding"].as_str().unwrap().into();
        assert_eq!(plan_value(&request).unwrap_err().to_string(), "VERIFIER_BINDING_MISMATCH");
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OnceRequest {
    workspace: PathBuf,
    contract: Value,
    binding: String,
    verifier_index: usize,
}

/// Read-only plan utility. It never spawns, builds AppState or a ReceiptStore.
pub(crate) fn plan() -> anyhow::Result<()> {
    anyhow::ensure!(std::env::args().count() == 2, "VERIFIER_HELPER_ARGS_INVALID");
    let mut raw = Vec::new();
    std::io::stdin().take(1048577).read_to_end(&mut raw)?;
    anyhow::ensure!(raw.len() <= 1048576, "VERIFIER_CONTRACT_TOO_LARGE");
    let request: OnceRequest = serde_json::from_slice(&raw)?;
    println!("{}", plan_value(&request)?);
    Ok(())
}

fn plan_value(request: &OnceRequest) -> anyhow::Result<Value> {
    let matcher = JSpaceMatcher::from_value(&request.workspace, &request.contract)?;
    anyhow::ensure!(matcher.authorization_semantic_sha256() == request.binding, "VERIFIER_BINDING_MISMATCH");
    matcher.revalidate_verifier_paths()?;
    let grant = matcher.check_verifier_command(request.verifier_index)?;
    let profile = focused_verifier_policy(std::path::Path::new(&grant.argv[0]), matcher.repo_root(),
        &grant.scratch_root, &matcher.scope_projection().read_scopes).map_err(anyhow::Error::msg)?;
    let mut value = json!({"binding":request.binding,"verifier_index":request.verifier_index,
        "profile":profile,"argv":grant.argv,"scratch_root":grant.scratch_root,
        "timeout_seconds":grant.timeout_seconds});
    if let Some(roots) = &grant.python_import_roots {
        value["python_import_roots"] = json!(roots);
    }
    if let Some(cwd) = &grant.cwd {
        value["cwd"] = json!(cwd);
    }
    Ok(value)
}
