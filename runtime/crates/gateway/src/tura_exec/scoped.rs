use std::fs::File;
use std::io::Read;
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::time::Duration;

use runtime_contract::TaskContextCapsule;
use serde_json::Value;

use super::cli::CliConfig;

const MAX_BINDING_BYTES: u64 = 512 * 1024;

pub(crate) fn parse_router_address(raw: &str) -> Result<SocketAddr, String> {
    let address: SocketAddr = raw
        .parse()
        .map_err(|_| "SCOPED_ROUTER_ADDRESS_INVALID: expected loopback IP:port".to_string())?;
    if !address.ip().is_loopback() || address.port() == 0 {
        return Err("SCOPED_ROUTER_ADDRESS_INVALID: expected loopback IP and nonzero port".into());
    }
    Ok(address)
}

pub(crate) fn validate_options(config: &CliConfig) -> Result<(), String> {
    if config.embedded && config.router_address.is_some() {
        return Err("SCOPED_EXECUTION_OPTIONS: explicit Router cannot use --embedded".into());
    }
    match (&config.task_context_capsule, &config.jspace_contract) {
        (None, None) => return Ok(()),
        (Some(_), Some(_)) => {}
        _ => {
            return Err(
                "SCOPED_EXECUTION_OPTIONS: capsule and J-Space must be supplied together".into(),
            );
        }
    }
    if config.router_address.is_none()
        || config.embedded
        || config.goal_mode
        || !config.command_run_sandbox
        || config.disable_permission_restrictions == Some(true)
    {
        return Err("SCOPED_EXECUTION_OPTIONS: requires explicit Router and --sandbox; no embedded, goal or permission bypass".into());
    }
    if !matches!(config.agent.as_deref(), Some("direct" | "balanced")) {
        return Err("SCOPED_EXECUTION_OPTIONS: select direct or balanced explicitly".into());
    }
    Ok(())
}

pub(crate) fn bind_context(config: &CliConfig, payload: &mut Value) -> Result<(), String> {
    validate_options(config)?;
    let (Some(capsule_path), Some(jspace_path)) =
        (&config.task_context_capsule, &config.jspace_contract)
    else {
        return Ok(());
    };
    let capsule_value = read_binding(capsule_path)?;
    let jspace = read_binding(jspace_path)?;
    let capsule = TaskContextCapsule::from_value(capsule_value.clone())?;
    capsule.bind_jspace(Some(&jspace))?;
    let matcher = tura_path::jspace::JSpaceMatcher::from_value(&config.cwd, &jspace)
        .map_err(|error| error.to_string())?;
    let context_root = capsule
        .surface
        .get("repo_root")
        .and_then(Value::as_str)
        .ok_or_else(|| "SCOPED_CONTEXT_ROOT_MISSING".to_string())?;
    if std::fs::canonicalize(context_root).map_err(|error| error.to_string())?
        != matcher.repo_root()
    {
        return Err("SCOPED_CONTEXT_ROOT_MISMATCH".into());
    }
    payload["task_context_capsule"] = capsule_value;
    payload["jspace_contract"] = jspace;
    if let Some(task_id) = capsule.mission.task_id {
        payload["task_id"] = Value::String(task_id);
    }
    Ok(())
}

fn read_binding(path: &Path) -> Result<Value, String> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("SCOPED_BINDING_READ_FAILED: {}: {error}", path.display()))?;
    if !metadata.is_file() || metadata.len() > MAX_BINDING_BYTES {
        return Err("SCOPED_BINDING_INVALID: expected bounded regular JSON file".into());
    }
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| file.take(MAX_BINDING_BYTES + 1).read_to_end(&mut bytes))
        .map_err(|error| format!("SCOPED_BINDING_READ_FAILED: {error}"))?;
    if bytes.len() as u64 > MAX_BINDING_BYTES {
        return Err("SCOPED_BINDING_INVALID: file exceeds size limit".into());
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("SCOPED_BINDING_INVALID: {error}"))?;
    if !value.is_object() {
        return Err("SCOPED_BINDING_INVALID: expected JSON object".into());
    }
    Ok(value)
}

pub(crate) fn connect_router(address: SocketAddr) -> Result<TcpStream, String> {
    // No discovery or detached-process fallback: the caller owns this endpoint's lifetime.
    TcpStream::connect_timeout(&address, Duration::from_secs(3))
        .map_err(|error| format!("SCOPED_ROUTER_UNAVAILABLE: {address}: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::net::TcpListener;

    fn args() -> Vec<String> {
        [
            "--router-address",
            "127.0.0.1:12345",
            "--sandbox",
            "-a",
            "direct",
            "--task-context-capsule",
            "capsule.json",
            "--jspace-contract",
            "jspace.json",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect()
    }

    fn capsule() -> Value {
        let mut value = json!({
            "schema_version": "task_context_capsule_v1",
            "mission": {"mission_id":"mission-1", "task_id":"task-1", "mode":"GOVERNANCE",
                "current_predicate":"source.compiles", "objective":"Compile source"},
            "context_summary":"Use the bound workspace and focused tests.",
            "dcf_generation": {}, "surface": {}, "authority": {},
            "evidence_refs": [], "focused_verifiers": [],
            "jspace_semantic_sha256": "a".repeat(64)
        });
        use sha2::{Digest, Sha256};
        value.sort_all_objects();
        value["semantic_sha256"] = json!(format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&value).unwrap())
        ));
        value
    }

    #[test]
    fn scoped_cli_accepts_both_full_core_profiles() {
        for agent in ["direct", "balanced"] {
            let mut arguments = args();
            arguments[4] = agent.to_string();
            let config = CliConfig::parse(arguments).unwrap();
            assert_eq!(config.agent.as_deref(), Some(agent));
            assert!(config.router_address.is_some());
        }
    }

    #[test]
    fn scoped_cli_rejects_unowned_and_bypass_modes() {
        for flag in [
            "--embedded",
            "--goal",
            "--dangerously-bypass-approvals-and-sandbox",
        ] {
            let mut arguments = args();
            arguments.push(flag.into());
            assert!(
                CliConfig::parse(arguments)
                    .unwrap_err()
                    .contains("SCOPED_EXECUTION_OPTIONS")
            );
        }
        let mut unowned = args();
        unowned.drain(0..2);
        assert!(CliConfig::parse(unowned).is_err());
        let mut unsandboxed = args();
        unsandboxed.remove(2);
        assert!(CliConfig::parse(unsandboxed).is_err());
        let mut unpaired = args();
        unpaired.truncate(7);
        assert!(CliConfig::parse(unpaired).is_err());
    }

    #[test]
    fn scoped_router_rejects_remote_and_discovery_addresses() {
        for address in [
            "0.0.0.0:1234",
            "192.0.2.1:1234",
            "localhost:1234",
            "127.0.0.1:0",
        ] {
            assert!(parse_router_address(address).is_err());
        }
        assert!(parse_router_address("[::1]:1234").is_ok());
    }

    #[test]
    fn scoped_router_connects_only_to_supplied_listener() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let stream = connect_router(address).unwrap();
        assert_eq!(stream.peer_addr().unwrap(), address);
        let (accepted, _) = listener.accept().unwrap();
        drop(accepted);
        drop(stream);
        drop(listener);
        assert!(
            connect_router(address)
                .unwrap_err()
                .contains("SCOPED_ROUTER_UNAVAILABLE")
        );
    }

    #[test]
    fn scoped_binding_preserves_capsule_contract_and_task_identity() {
        let directory = tempfile::tempdir().unwrap();
        let mut config = CliConfig::parse(args()).unwrap();
        config.cwd = directory.path().to_path_buf();
        let mut authority = json!({
            "schema_version":"jspace_contract_v1", "repo_root":config.cwd,
            "dcf_generation":{}, "provenance":{}, "matched_surface_ids":[],
            "read_scopes":["target.py"], "write_scopes":["target.py"],
            "allowed_operations":["read","modify"], "denied_operations":["delete"],
            "command_prefixes":[], "focused_verifiers":[], "declared_targets":["target.py"],
            "expansion":{"mode":"exact_target_only","error_code":"JSPACE_EXPANSION_REQUIRED","mutation_on_expansion":false}
        });
        authority["semantic_sha256"] = json!(tura_path::jspace::semantic_sha256(&authority));
        let mut context = capsule();
        context["surface"] = json!({"repo_root":config.cwd});
        context["jspace_semantic_sha256"] = authority["semantic_sha256"].clone();
        context.as_object_mut().unwrap().remove("semantic_sha256");
        context["semantic_sha256"] = json!(tura_path::jspace::semantic_sha256(&context));
        let capsule_path = directory.path().join("capsule.json");
        let jspace_path = directory.path().join("jspace.json");
        std::fs::write(&capsule_path, serde_json::to_vec(&context).unwrap()).unwrap();
        std::fs::write(&jspace_path, serde_json::to_vec(&authority).unwrap()).unwrap();
        config.task_context_capsule = Some(capsule_path);
        config.jspace_contract = Some(jspace_path.clone());
        let mut payload = json!({"prompt":"task", "model":"gpt-6-astra", "agent":"direct"});
        bind_context(&config, &mut payload).unwrap();
        assert_eq!(payload["task_context_capsule"], context);
        assert_eq!(payload["jspace_contract"], authority);
        assert_eq!(payload["task_id"], "task-1");
        assert_eq!(payload["agent"], "direct");
        assert!(payload.get("native_codex_execution").is_none());
        std::fs::write(
            &jspace_path,
            serde_json::to_vec(&json!({"authorization_semantic_sha256":"b".repeat(64)})).unwrap(),
        )
        .unwrap();
        assert!(
            bind_context(&config, &mut json!({}))
                .unwrap_err()
                .contains("BINDING_MISMATCH")
        );
    }

    #[test]
    fn scoped_binding_rejects_tampering_before_router_or_session_contact() {
        let directory = tempfile::tempdir().unwrap();
        let mut config = CliConfig::parse(args()).unwrap();
        let mut context = capsule();
        context["context_summary"] = json!("tampered");
        let capsule_path = directory.path().join("capsule.json");
        let jspace_path = directory.path().join("jspace.json");
        std::fs::write(&capsule_path, serde_json::to_vec(&context).unwrap()).unwrap();
        std::fs::write(&jspace_path, b"{}").unwrap();
        config.task_context_capsule = Some(capsule_path);
        config.jspace_contract = Some(jspace_path);
        let error =
            super::super::router::run_via_router(&config, "not-created", "test").unwrap_err();
        assert!(
            error.contains("TASK_CONTEXT_SEMANTIC_DIGEST_MISMATCH"),
            "{error}"
        );
    }

    #[test]
    fn scoped_binding_rejects_nonobjects_and_oversized_files() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("input.json");
        std::fs::write(&path, b"[]").unwrap();
        assert!(read_binding(&path).is_err());
        File::create(&path)
            .unwrap()
            .set_len(MAX_BINDING_BYTES + 1)
            .unwrap();
        assert!(read_binding(&path).is_err());
        assert!(read_binding(directory.path()).is_err());
    }

    #[test]
    fn unscoped_legacy_cli_does_not_acquire_new_context() {
        let config = CliConfig::parse(vec!["--embedded".into()]).unwrap();
        let mut payload = json!({"prompt":"unchanged"});
        bind_context(&config, &mut payload).unwrap();
        assert_eq!(payload, json!({"prompt":"unchanged"}));
    }
}
