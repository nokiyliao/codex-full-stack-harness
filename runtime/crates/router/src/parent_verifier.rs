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

pub(crate) async fn request(
    matcher: &JSpaceMatcher, call_id: &str, index: usize,
    cancellation: &code_tools::runtime::tool::CancellationToken,
) -> Result<VerifierObservation, String> {
    let duration = matcher.check_verifier_command(index).map_err(|e| e.to_string())?.timeout_seconds;
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
    exchange(&mut channel, &matcher.authorization_semantic_sha256(), call_id, index, duration, cancellation).await
}

async fn exchange(
    channel: &mut Option<tokio::net::UnixStream>, binding: &str, call_id: &str,
    index: usize, duration: u64,
    cancellation: &code_tools::runtime::tool::CancellationToken,
) -> Result<VerifierObservation, String> {
    // Removing it poisons reuse on cancellation, panic, partial send or bad reply.
    let mut stream = channel.take().ok_or("VERIFIER_CHANNEL_UNSETTLED")?;
    let mut bytes = serde_json::to_vec(&json!({"version":1,"binding":binding,
        "call_id":call_id,"verifier_index":index})).map_err(|e| e.to_string())?;
    bytes.push(b'\n');
    if bytes.len() > 4096 { return Err("VERIFIER_REQUEST_TOO_LARGE".into()) }
    let transaction = async {
        stream.write_all(&bytes).await.map_err(|e| e.to_string())?;
        let mut response = Vec::new();
        loop {
            let byte = stream.read_u8().await.map_err(|e| e.to_string())?;
            if byte == b'\n' { break; }
            if response.len() >= 262143 { return Err("VERIFIER_REPLY_TOO_LARGE".into()) }
            response.push(byte);
        }
        let reply: Reply = serde_json::from_slice(&response).map_err(|e| e.to_string())?;
        if reply.version != 1 || reply.binding != binding || reply.call_id != call_id || reply.verifier_index != index {
            return Err("VERIFIER_REPLY_IDENTITY_MISMATCH".into());
        }
        Ok(reply.result)
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
        let legacy = plan_value(&request).unwrap();
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
        assert_eq!(plan_value(&request).unwrap(), expected);
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
    Ok(value)
}
