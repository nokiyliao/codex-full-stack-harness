use runtime::native_codex_runner::{
    NativeCodexContext, NativeCodexProviderProfile, NativeCodexRunRequest, NativeCodexRunner,
    NativeCodexSandbox, NATIVE_ONCE_PROMPT_SHA256, TURA_EXECUTION_PROFILE_SHA256,
};
use runtime_contract::{ModelServiceTier, NativeCodexTaskDelta, TaskContextCapsule};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs, os::unix::fs::PermissionsExt, path::Path, time::Duration};

fn sealed(mut value: Value) -> Value {
    value["semantic_sha256"] = json!(
        runtime_contract::task_context_semantic_sha256_v1(&value).unwrap()
    );
    value
}

fn request(explicit_profile: bool, root: &Path) -> NativeCodexRunRequest {
    let executable = root.join("fake-codex");
    let script = r#"#!/bin/sh
if [ "$1" = mcp ]; then printf '[]\n'; exit 0; fi
cat > "$0.prompt"
printf '%s\n' '{"type":"thread.started","thread_id":"fixture"}'
printf '%s\n' '{"type":"item.completed","item":{"id":"message","type":"agent_message","text":"fixture done"}}'
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}'
"#;
    fs::write(&executable, script).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
    let capsule = TaskContextCapsule::from_value(sealed(json!({
        "schema_version":"task_context_capsule_v1",
        "mission":{"mission_id":"mission", "task_id":"task", "mode":"DELIVERY",
            "current_predicate":"fix", "objective":"Fix and verify a bounded task"},
        "context_summary":"Use scoped source only.",
        "dcf_generation":{"generation_id":"test"},
        "surface":{"workspace":"task-scoped"},
        "authority":{"authority_effect":"none"},
        "evidence_refs":[], "focused_verifiers":[],
        "jspace_semantic_sha256":"a".repeat(64)
    }))).unwrap();
    let delta = NativeCodexTaskDelta::from_value(sealed(json!({
        "schema_version":"tura_native_codex_task_delta_v1",
        "mission_id":"mission", "task_id":"task",
        "mission_revision_sha256":"c".repeat(64),
        "current_predicate":"fix", "instruction":"Read, patch, test; report evidence."
    }))).unwrap();
    let profile = explicit_profile.then(|| NativeCodexProviderProfile {
        model: "gpt-6-astra".into(), reasoning_effort: "high".into(),
        service_tier: ModelServiceTier::Default, model_provider: None,
    });
    let profile_sha = profile.as_ref().map(|p| p.semantic_sha256().unwrap())
        .unwrap_or_else(|| TURA_EXECUTION_PROFILE_SHA256.into());
    NativeCodexRunRequest {
        codex_executable: executable,
        codex_executable_sha256: format!("{:x}", Sha256::digest(script.as_bytes())),
        workspace: root.into(),
        session_id: "session".into(), task_id: "task".into(),
        execution_id: "execution".into(), lease_id: "lease".into(),
        context: NativeCodexContext {
            provider_profile: profile, execution_profile_sha256: profile_sha,
            execution_binding_sha256: "d".repeat(64),
            expected_task_context_capsule_sha256: capsule.semantic_sha256.clone(),
            expected_task_delta_sha256: delta.semantic_sha256.clone(),
            expected_jspace_semantic_sha256: capsule.jspace_semantic_sha256.clone(),
            task_context_capsule: capsule, task_delta: delta,
        },
        sandbox: NativeCodexSandbox::ReadOnly, timeout: Duration::from_secs(10),
        command_graph: None,
    }
}

#[tokio::test]
async fn explicit_profile_compiles_capability_consistent_prompt() {
    let root = tempfile::tempdir().unwrap();
    NativeCodexRunner::run(request(true, root.path())).await.unwrap();
    let input = fs::read_to_string(root.path().join("fake-codex.prompt")).unwrap();
    assert!(input.contains(NATIVE_ONCE_PROMPT_SHA256));
    assert!(input.contains("The only execution tool is tura_command_graph"));
    assert!(input.contains("Do not invoke task_status, compact_context, read_media"));
    assert!(input.contains("The parent Codex task owns"));
    assert!(input.contains("Independent reads with no output dependency share a"));
    assert!(!input.contains("include task_status `task_group`"));
    assert!(!input.contains("must precede `apply_patch`"));
    assert!(input.contains("Read, patch, test; report evidence."));
}

#[tokio::test]
async fn frozen_legacy_prompt_is_not_rewritten() {
    let root = tempfile::tempdir().unwrap();
    NativeCodexRunner::run(request(false, root.path())).await.unwrap();
    let input = fs::read_to_string(root.path().join("fake-codex.prompt")).unwrap();
    assert!(input.contains("include task_status `task_group`"));
    assert!(!input.contains(NATIVE_ONCE_PROMPT_SHA256));
}

#[tokio::test]
async fn explicit_profile_rejects_old_prompt_identity() {
    let root = tempfile::tempdir().unwrap();
    let mut req = request(true, root.path());
    req.context.execution_profile_sha256 = TURA_EXECUTION_PROFILE_SHA256.into();
    assert!(NativeCodexRunner::run(req).await.unwrap_err()
        .contains("EXECUTION_PROFILE_IDENTITY_MISMATCH"));
}
