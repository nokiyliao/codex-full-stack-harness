#![cfg(unix)]

use runtime::native_codex_runner::{
    NativeCodexCommandGraph, NativeCodexContext, NativeCodexRunRequest, NativeCodexRunner,
    NativeCodexSandbox, TURA_EXECUTION_PROFILE_SHA256,
};
use runtime_contract::{
    NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION, NativeCodexTaskDelta, NativeCodexTerminalState,
    TASK_CONTEXT_CAPSULE_SCHEMA_VERSION, TaskContextCapsule,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;
use tokio::process::Command;

fn fake_codex(root: &Path, with_tool: bool) -> PathBuf {
    let path = root.join(if with_tool {
        "fake-codex-tool"
    } else {
        "fake-codex-no-tool"
    });
    let tool_event = if with_tool {
        r#"printf '%s\n' '{"type":"item.completed","item":{"id":"tool-1","type":"mcp_tool_call","server":"tura_command_graph","tool":"tura_command_graph","arguments":{"commands":[{"command_type":"zsh","command_line":"pwd","step":1}]},"result":{"content":[{"type":"text","text":"ok"}]},"status":"completed"}}'"#
    } else {
        ":"
    };
    let mcp_flag_check = if with_tool {
        r#"case "$*" in
  *mcp_servers.tura_command_graph.required=true*mcp_servers.tura_command_graph.enabled_tools=*tura_command_graph*TURA_NATIVE_SESSION_ID*TURA_NATIVE_JSPACE_CONTRACT_JSON*) ;;
  *) echo 'missing bounded MCP flags' >&2; exit 67 ;;
esac"#
    } else {
        ":"
    };
    fs::write(
        &path,
        format!(
            r#"#!/bin/sh
{NATIVE_MCP_CATALOG}
case "$*" in
  *--disable*shell_tool*--disable*unified_exec*--disable*multi_agent*--disable*view_image*--disable*default_mode_request_user_input*--disable*apps*--disable*plugins*--disable*remote_plugin*--disable*skill_mcp_dependency_install*--strict-config*--ephemeral*--json*--skip-git-repo-check*skills.include_instructions=false*web_search=*disabled*agents.enabled=false*tools.experimental_request_user_input.enabled=false*mcp_servers.unrelated-mcp.enabled=false*) ;;
  *) echo 'missing bounded native flags' >&2; exit 64 ;;
esac
{mcp_flag_check}
for arg in "$@"; do
  case "$arg" in --ignore-user-config|--ignore-rules|-a|approval_policy=*) exit 65 ;; esac
done
printf '%s\n%s\n' "$HOME" "${{CODEX_HOME-}}" >"$0.home"
printf '%s\n' "$@" >"$0.argv"
cat >"$0.prompt"
printf '%s\n' '{{"type":"thread.started","thread_id":"native-thread-1"}}'
{tool_event}
printf '%s\n' '{{"type":"item.completed","item":{{"id":"message-1","type":"agent_message","text":"native done"}}}}'
printf '%s\n' '{{"type":"turn.completed","usage":{{"input_tokens":10,"output_tokens":2}}}}'
"#
        ),
    )
    .expect("write fake Codex");
    let mut permissions = fs::metadata(&path)
        .expect("fake Codex metadata")
        .permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&path, permissions).expect("fake Codex executable mode");
    path
}

fn canonical_sha256(value: &serde_json::Value) -> String {
    fn canonical(value: &serde_json::Value) -> String {
        match value {
            serde_json::Value::Null => "null".to_string(),
            serde_json::Value::Bool(value) => value.to_string(),
            serde_json::Value::Number(value) => value.to_string(),
            serde_json::Value::String(value) => serde_json::to_string(value).unwrap(),
            serde_json::Value::Array(values) => format!(
                "[{}]",
                values.iter().map(canonical).collect::<Vec<_>>().join(",")
            ),
            serde_json::Value::Object(values) => {
                let mut keys = values.keys().collect::<Vec<_>>();
                keys.sort();
                format!(
                    "{{{}}}",
                    keys.into_iter()
                        .map(|key| format!(
                            "{}:{}",
                            serde_json::to_string(key).unwrap(),
                            canonical(&values[key])
                        ))
                        .collect::<Vec<_>>()
                        .join(",")
                )
            }
        }
    }

    format!("{:x}", Sha256::digest(canonical(value).as_bytes()))
}

fn file_sha256(path: &Path) -> String {
    format!(
        "{:x}",
        Sha256::digest(fs::read(path).expect("read executable for digest"))
    )
}

fn jspace_contract() -> serde_json::Value {
    json!({"authorization_semantic_sha256": "a".repeat(64)})
}

fn task_context_capsule(task_id: &str) -> TaskContextCapsule {
    let mut value = json!({
        "schema_version": TASK_CONTEXT_CAPSULE_SCHEMA_VERSION,
        "mission": {
            "mission_id": "mission-native-thin-kernel-1",
            "task_id": task_id,
            "mode": "DELIVERY",
            "current_predicate": "P1_BOUNDED_CONTEXT_AND_TOOL_OWNERSHIP",
            "objective": "Run the bounded Native Codex task"
        },
        "context_summary": "Use the exact task delta and hash-bound evidence only.",
        "dcf_generation": {"generation_id": "generation-native-1"},
        "surface": {"workspace": "task-scoped"},
        "authority": {"authority_effect": "none"},
        "evidence_refs": [{
            "id": "evidence-native-1",
            "kind": "artifact",
            "sha256": "b".repeat(64)
        }],
        "focused_verifiers": [{"verifier_id": "native-runner-contract"}],
        "jspace_semantic_sha256": "a".repeat(64)
    });
    value["semantic_sha256"] = json!(canonical_sha256(&value));
    TaskContextCapsule::from_value(value).expect("valid test capsule")
}

fn task_delta(task_id: &str) -> NativeCodexTaskDelta {
    let mut value = json!({
        "schema_version": NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION,
        "mission_id": "mission-native-thin-kernel-1",
        "mission_revision_sha256": "c".repeat(64),
        "task_id": task_id,
        "current_predicate": "P1_BOUNDED_CONTEXT_AND_TOOL_OWNERSHIP",
        "instruction": "Return a bounded terminal result."
    });
    value["semantic_sha256"] = json!(canonical_sha256(&value));
    NativeCodexTaskDelta::from_value(value).expect("valid test delta")
}

fn request(
    codex_executable: PathBuf,
    workspace: PathBuf,
    command_graph: Option<NativeCodexCommandGraph>,
) -> NativeCodexRunRequest {
    let capsule = task_context_capsule("task-native-1");
    let delta = task_delta("task-native-1");
    NativeCodexRunRequest {
        codex_executable_sha256: file_sha256(&codex_executable),
        codex_executable,
        workspace,
        session_id: "session-native-1".to_string(),
        task_id: "task-native-1".to_string(),
        execution_id: "execution-native-1".to_string(),
        lease_id: "lease-native-1".to_string(),
        context: NativeCodexContext {
            provider_profile: None,
            execution_profile_sha256: TURA_EXECUTION_PROFILE_SHA256.to_string(),
            execution_binding_sha256: "d".repeat(64),
            expected_task_context_capsule_sha256: capsule.semantic_sha256.clone(),
            expected_task_delta_sha256: delta.semantic_sha256.clone(),
            expected_jspace_semantic_sha256: capsule.jspace_semantic_sha256.clone(),
            task_context_capsule: capsule,
            task_delta: delta,
        },
        sandbox: NativeCodexSandbox::ReadOnly,
        timeout: Duration::from_secs(10),
        command_graph,
    }
}

const NATIVE_HARNESS_SUCCESS_EVENTS: &str = r#"
printf '%s\n' '{"type":"thread.started","thread_id":"stream-fixture"}'
printf '%s\n' '{"type":"item.completed","item":{"id":"final","type":"agent_message","text":"fixture complete"}}'
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}'
"#;

const NATIVE_MCP_CATALOG: &str = r#"
if [ "$1" = "mcp" ]; then
  printf '%s\n' '[{"name":"unrelated-mcp","enabled":true}]'
  exit 0
fi
"#;

#[tokio::test]
async fn native_runner_discovers_mcp_in_the_execution_feature_context() {
    let root = tempfile::tempdir().unwrap();
    let executable = fake_codex(root.path(), false);
    let original = fs::read_to_string(&executable).unwrap();
    let feature_sensitive_catalog = r#"
if [ "$1" = "mcp" ]; then
  case "$*" in
    *--disable\ apps*--disable\ plugins*--disable\ remote_plugin*--disable\ skill_mcp_dependency_install*)
      printf '%s\n' '[{"name":"unrelated-mcp","enabled":true}]' ;;
    *) printf '%s\n' '[{"name":"codex_app","enabled":true}]' ;;
  esac
  exit 0
fi
"#;
    fs::write(&executable, original.replace(NATIVE_MCP_CATALOG, feature_sensitive_catalog)).unwrap();
    let envelope = NativeCodexRunner::run(request(executable.clone(), root.path().into(), None)).await.unwrap();
    assert_eq!(envelope.terminal_state, NativeCodexTerminalState::Completed);
    let args = fs::read_to_string(format!("{}.argv", executable.display())).unwrap();
    assert!(!args.contains("mcp_servers.codex_app"));
    assert!(args.contains("mcp_servers.unrelated-mcp.enabled=false"));
}

#[tokio::test]
async fn native_runner_preserves_home_and_explicit_provider_without_policy_bypass() {
    use runtime::native_codex_runner::NativeCodexProviderProfile;
    use runtime_contract::ModelServiceTier;
    let root = tempfile::tempdir().unwrap();
    let executable = fake_codex(root.path(), false);
    let mut req = request(executable.clone(), root.path().to_path_buf(), None);
    let profile = NativeCodexProviderProfile {
        model: "gpt-6-astra".into(), reasoning_effort: "xhigh".into(),
        service_tier: ModelServiceTier::Default, model_provider: Some("fixture-provider".into()),
    };
    req.context.execution_profile_sha256 = profile.semantic_sha256().unwrap();
    req.context.provider_profile = Some(profile);
    let envelope = NativeCodexRunner::run(req).await.unwrap();
    assert_eq!(envelope.terminal_state, NativeCodexTerminalState::Completed);
    let home = fs::read_to_string(format!("{}.home", executable.display())).unwrap();
    assert_eq!(home, format!("{}\n{}\n", std::env::var("HOME").unwrap_or_default(),
        std::env::var("CODEX_HOME").unwrap_or_default()));
    let args = fs::read_to_string(format!("{}.argv", executable.display())).unwrap();
    assert!(args.lines().any(|arg| arg == "mcp_servers.unrelated-mcp.enabled=false"));
    assert!(args.lines().any(|arg| arg == "model_provider=\"fixture-provider\""));
    assert!(args.lines().any(|arg| arg == "model_reasoning_effort=\"xhigh\""));
    assert!(!args.contains("--ignore-rules"));
    assert!(!args.contains("--ignore-user-config"));
}

fn native_harness_script(root: &Path, name: &str, body: &str) -> PathBuf {
    let path = root.join(name);
    fs::write(&path, format!("#!/bin/sh\n{NATIVE_MCP_CATALOG}\n{body}\n")).expect("write local process fixture");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).expect("fixture executable");
    path
}

#[tokio::test]
async fn native_runner_rejects_unverifiable_or_colliding_mcp_before_provider() {
    for (catalog, blocker) in [
        ("not-json", "NATIVE_CODEX_MCP_CATALOG_INVALID"),
        (r#"[{"name":"tura_command_graph"}]"#, "NATIVE_CODEX_MCP_GRAPH_NAME_COLLISION"),
        (r#"[{"name":"ambiguous.name"}]"#, "NATIVE_CODEX_MCP_NAME_UNSUPPORTED"),
    ] {
        let root = tempfile::tempdir().unwrap();
        let executable = fake_codex(root.path(), false);
        let original = fs::read_to_string(&executable).unwrap();
        fs::write(&executable, original.replace(
            r#"[{"name":"unrelated-mcp","enabled":true}]"#, catalog,
        )).unwrap();
        let error = NativeCodexRunner::run(request(executable.clone(), root.path().into(), None))
            .await.unwrap_err();
        assert_eq!(error, blocker);
        assert!(!PathBuf::from(format!("{}.prompt", executable.display())).exists());
    }
}

#[tokio::test]
async fn native_runner_catalog_deadline_reaps_before_provider() {
    let root = tempfile::tempdir().unwrap();
    let executable = fake_codex(root.path(), false);
    let original = fs::read_to_string(&executable).unwrap();
    fs::write(&executable, original.replace(NATIVE_MCP_CATALOG,
        "if [ \"$1\" = \"mcp\" ]; then echo $$ >\"$0.catalog-pid\"; exec /bin/sleep 10; fi\n",
    )).unwrap();
    let mut req = request(executable.clone(), root.path().into(), None);
    req.timeout = Duration::from_secs(1);
    let error = tokio::time::timeout(Duration::from_secs(4), NativeCodexRunner::run(req))
        .await.unwrap().unwrap_err();
    assert_eq!(error, "NATIVE_CODEX_MCP_CATALOG_TIMEOUT");
    let pid = fs::read_to_string(format!("{}.catalog-pid", executable.display())).unwrap();
    let still_running = Command::new("/bin/kill").args(["-0", pid.trim()])
        .stdout(Stdio::null()).stderr(Stdio::null()).status().await.unwrap();
    assert!(!still_running.success());
    assert!(!PathBuf::from(format!("{}.prompt", executable.display())).exists());
}

#[tokio::test]
async fn native_harness_failure_event_never_becomes_completed() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = native_harness_script(root.path(), "failure-then-completed", &format!(
        "cat >/dev/null\nprintf '%s\\n' '{{\"type\":\"turn.failed\",\"error\":{{\"message\":\"fixture terminal failure\"}}}}'\n{NATIVE_HARNESS_SUCCESS_EVENTS}"
    ));
    let envelope = NativeCodexRunner::run(request(executable, root.path().to_path_buf(), None))
        .await.expect("typed terminal envelope");
    assert_eq!(envelope.terminal_state, NativeCodexTerminalState::Failed);
    assert_eq!(envelope.error.as_deref(), Some("fixture terminal failure"));
    assert!(envelope.process_reaped);
}

#[tokio::test]
async fn native_harness_drains_stderr_past_retention() {
    let root = tempfile::tempdir().expect("tempdir");
    // Shell builtins keep all output in this owned fixture process. The producer
    // must finish all 2 MiB even though only a 1 MiB diagnostic prefix is retained.
    let executable = native_harness_script(root.path(), "stderr-pipe-pressure", &format!(
        "cat >/dev/null\nchunk='{}'\ni=0\nwhile test $i -lt 512; do\n  printf '%s' \"$chunk\" >&2 || exit 70\n  i=$((i + 1))\ndone\n{NATIVE_HARNESS_SUCCESS_EVENTS}",
        "x".repeat(4096)
    ));
    let envelope = NativeCodexRunner::run(request(executable, root.path().to_path_buf(), None))
        .await.expect("drained terminal envelope");
    assert_eq!(envelope.terminal_state, NativeCodexTerminalState::Completed);
    assert_eq!(envelope.final_text.as_deref(), Some("fixture complete"));
    assert!(envelope.process_reaped);
}

fn native_harness_large_request(executable: PathBuf, workspace: PathBuf) -> NativeCodexRunRequest {
    let mut req = request(executable, workspace, None);
    let mut delta = serde_json::to_value(&req.context.task_delta).expect("delta value");
    delta.as_object_mut().expect("delta object").remove("semantic_sha256");
    delta["instruction"] = json!("pipe pressure ".repeat(14_000));
    delta["semantic_sha256"] = json!(canonical_sha256(&delta));
    req.context.task_delta = NativeCodexTaskDelta::from_value(delta).expect("large bounded delta");
    req.context.expected_task_delta_sha256 = req.context.task_delta.semantic_sha256.clone();
    req
}

#[tokio::test]
async fn native_harness_drains_stdout_while_sending_large_prompt() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = native_harness_script(root.path(), "bidirectional-pipe-pressure", &format!(
        "printf '%s\\n' '{}'\ncat >\"$0.prompt\"\n{NATIVE_HARNESS_SUCCESS_EVENTS}",
        " ".repeat(131_072)
    ));
    let req = native_harness_large_request(executable.clone(), root.path().to_path_buf());
    let envelope = tokio::time::timeout(Duration::from_secs(4), NativeCodexRunner::run(req))
        .await.expect("prompt/output pipe cycle must make progress").expect("terminal envelope");
    assert_eq!(envelope.terminal_state, NativeCodexTerminalState::Completed);
    assert!(fs::metadata(format!("{}.prompt", executable.display())).expect("prompt").len() > 131_072);
    assert!(envelope.process_reaped);
}

#[tokio::test]
async fn native_harness_timeout_includes_stalled_prompt_write() {
    let root = tempfile::tempdir().expect("tempdir");
    // exec keeps the sleeper in the directly owned/reaped PID, not an orphan child.
    let executable = native_harness_script(root.path(), "no-stdin-consumer", "exec /bin/sleep 10");
    let mut req = native_harness_large_request(executable, root.path().to_path_buf());
    // Include one catalog process startup without turning the test into a
    // machine-load benchmark; the provider still stalls for ten seconds.
    req.timeout = Duration::from_secs(1);
    let envelope = tokio::time::timeout(Duration::from_secs(4), NativeCodexRunner::run(req))
        .await.expect("execution deadline must include prompt writes").expect("interrupted envelope");
    assert_eq!(envelope.terminal_state, NativeCodexTerminalState::Interrupted);
    assert_eq!(envelope.error.as_deref(), Some("NATIVE_CODEX_RUNNER_TIMEOUT"));
    assert!(envelope.process_reaped);
}

#[tokio::test]
async fn native_harness_invalid_output_stops_and_reaps_without_waiting_for_deadline() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = native_harness_script(root.path(), "invalid-output-then-sleep",
        "cat >/dev/null\nprintf 'invalid-json\\n'\nexec /bin/sleep 10");
    let req = request(executable, root.path().to_path_buf(), None);
    let error = tokio::time::timeout(Duration::from_secs(4), NativeCodexRunner::run(req))
        .await.expect("parser failure must cancel the other I/O branches immediately")
        .expect_err("malformed event must not produce a successful terminal");
    assert!(error.starts_with("NATIVE_CODEX_EVENT_INVALID_JSON:"));
}

#[tokio::test]
async fn native_runner_returns_ephemeral_no_tool_terminal() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = fake_codex(root.path(), false);
    let envelope =
        NativeCodexRunner::run(request(executable.clone(), root.path().to_path_buf(), None))
            .await
            .expect("native no-tool terminal");

    assert_eq!(envelope.terminal_state, NativeCodexTerminalState::Completed);
    assert_eq!(envelope.final_text.as_deref(), Some("native done"));
    assert!(envelope.tool_observations.is_empty());
    assert!(envelope.ephemeral);
    assert_eq!(envelope.private_codex_state_write_count, 0);
    assert!(!envelope.execution_lease_active);

    let provider_input = fs::read_to_string(format!("{}.prompt", executable.display()))
        .expect("captured provider input");
    assert!(provider_input.contains("[TURA_IMMUTABLE_INSTRUCTION_PREFIX_V1]"));
    assert!(provider_input.contains(TURA_EXECUTION_PROFILE_SHA256));
    assert!(provider_input.contains("[TASK_CONTEXT_CAPSULE_V1]"));
    assert!(provider_input.contains(runtime_contract::TASK_CONTEXT_PRESENTATION_VERSION));
    assert!(provider_input.contains(&task_context_capsule("task-native-1").provider_context()));
    assert!(provider_input.contains(&"b".repeat(64)));
    assert!(provider_input.contains("[CURRENT_TASK_DELTA_V1]"));
    assert!(provider_input.contains("Return a bounded terminal result."));
    assert_eq!(
        envelope.execution_profile_sha256,
        TURA_EXECUTION_PROFILE_SHA256
    );
    assert_eq!(envelope.execution_binding_sha256, "d".repeat(64));
    assert_eq!(
        envelope.task_context_capsule_sha256,
        task_context_capsule("task-native-1").semantic_sha256
    );
    assert_eq!(
        envelope.task_delta_sha256,
        task_delta("task-native-1").semantic_sha256
    );
    assert_eq!(
        envelope.provider_input_sha256,
        format!("{:x}", Sha256::digest(provider_input.as_bytes()))
    );
}

#[tokio::test]
async fn native_runner_projects_unicode_without_losing_restrictions_or_identity() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = fake_codex(root.path(), false);
    let mut run_request = request(executable.clone(), root.path().to_path_buf(), None);
    let mut value = serde_json::to_value(&run_request.context.task_context_capsule).unwrap();
    value["mission"]["objective"] = json!("保留必要證據😀");
    value["authority"]["must_preserve"] = json!(["broker", "mining", "exact-P0"]);
    value["context_summary"] = json!(serde_json::to_string(&json!({
        "mission": value["mission"], "surface": value["surface"],
        "dcf_generation": value["dcf_generation"], "new_detail": "不可丟失"
    })).unwrap());
    value.as_object_mut().unwrap().remove("semantic_sha256");
    value["semantic_sha256"] = json!(runtime_contract::task_context_semantic_sha256_v1(&value).unwrap());
    let capsule = TaskContextCapsule::from_value(value.clone()).unwrap();
    run_request.context.expected_task_context_capsule_sha256 = capsule.semantic_sha256.clone();
    run_request.context.task_context_capsule = capsule.clone();
    let envelope = NativeCodexRunner::run(run_request).await.expect("projected request");
    let input = fs::read_to_string(format!("{}.prompt", executable.display())).unwrap();
    assert_eq!(input.matches("保留必要證據😀").count(), 1);
    for text in ["must_preserve", "broker", "mining", "exact-P0", "不可丟失", "context_summary_inherits"] {
        assert!(input.contains(text), "missing {text}");
    }
    assert_eq!(envelope.task_context_capsule_sha256, capsule.semantic_sha256);
    assert_eq!(serde_json::to_value(capsule).unwrap(), value);
    assert_eq!(envelope.provider_input_sha256, format!("{:x}", Sha256::digest(input.as_bytes())));
}

#[tokio::test]
async fn native_runner_rejects_cross_task_capsule_binding() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = fake_codex(root.path(), false);
    let mut run_request = request(executable, root.path().to_path_buf(), None);
    let capsule = task_context_capsule("task-other");
    let delta = task_delta("task-other");
    run_request.context.expected_task_context_capsule_sha256 = capsule.semantic_sha256.clone();
    run_request.context.expected_task_delta_sha256 = delta.semantic_sha256.clone();
    run_request.context.task_context_capsule = capsule;
    run_request.context.task_delta = delta;

    let error = NativeCodexRunner::run(run_request)
        .await
        .expect_err("cross-task capsule must fail closed");
    assert_eq!(error, "NATIVE_CODEX_TASK_CONTEXT_TASK_ID_MISMATCH");
}

#[tokio::test]
async fn native_runner_requires_outer_expected_capsule_identity() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = fake_codex(root.path(), false);
    let mut run_request = request(executable, root.path().to_path_buf(), None);
    run_request.context.expected_task_context_capsule_sha256 = "c".repeat(64);

    let error = NativeCodexRunner::run(run_request)
        .await
        .expect_err("self-consistent but unexpected capsule must fail closed");
    assert_eq!(error, "NATIVE_CODEX_TASK_CONTEXT_EXPECTED_DIGEST_MISMATCH");
}

#[tokio::test]
async fn native_runner_rejects_workspace_write_before_process_spawn() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = fake_codex(root.path(), false);
    let mut run_request = request(executable.clone(), root.path().to_path_buf(), None);
    run_request.sandbox = NativeCodexSandbox::WorkspaceWrite;

    let error = NativeCodexRunner::run(run_request)
        .await
        .expect_err("Native Codex core tools never own workspace writes");
    assert_eq!(error, "NATIVE_CODEX_RUNNER_REQUIRES_READ_ONLY_SANDBOX");
    assert!(
        !PathBuf::from(format!("{}.prompt", executable.display())).exists(),
        "runner must reject before spawning Codex"
    );
}

#[tokio::test]
async fn native_runner_preserves_typed_tool_and_effect_identity() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = fake_codex(root.path(), true);
    let executable_sha256 = file_sha256(&executable);
    let envelope = NativeCodexRunner::run(request(
        executable.clone(),
        root.path().to_path_buf(),
        Some(NativeCodexCommandGraph {
            executable,
            executable_sha256,
            allowed_commands: BTreeSet::from(["zsh".to_string()]),
            jspace_contract: jspace_contract(),
        }),
    ))
    .await
    .expect("native tool terminal");

    assert_eq!(envelope.tool_observations.len(), 1);
    assert_eq!(envelope.tool_observations[0].item_id, "tool-1");
    assert!(!envelope.tool_observations[0].is_error);
    assert_eq!(
        envelope.tool_observations[0].tool_name,
        "tura_command_graph"
    );
    assert!(
        envelope.tool_observations[0]
            .effect_id
            .starts_with("native-codex-effect-")
    );
}

#[tokio::test]
async fn command_graph_stdio_exposes_exactly_one_tool() {
    let root = tempfile::tempdir().expect("tempdir");
    let mut child = Command::new(env!("CARGO_BIN_EXE_tura_command_graph"))
        .env("TURA_NATIVE_SESSION_ID", "session-stdio-1")
        .env("TURA_NATIVE_TASK_ID", "task-stdio-1")
        .env("TURA_NATIVE_EXECUTION_ID", "execution-stdio-1")
        .env("TURA_NATIVE_LEASE_ID", "lease-stdio-1")
        .env("TURA_NATIVE_WORKSPACE", root.path())
        .env("TURA_NATIVE_ALLOWED_COMMANDS_JSON", r#"["zsh"]"#)
        .env(
            "TURA_NATIVE_JSPACE_CONTRACT_JSON",
            serde_json::to_string(&jspace_contract()).expect("J-Space JSON"),
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .expect("spawn command graph");
    let mut stdin = child.stdin.take().expect("command graph stdin");
    let stdout = child.stdout.take().expect("command graph stdout");
    stdin
        .write_all(
            br#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
"#,
        )
        .await
        .expect("write MCP requests");
    stdin.shutdown().await.expect("close MCP stdin");
    drop(stdin);

    let mut lines = BufReader::new(stdout).lines();
    let initialize: serde_json::Value = serde_json::from_str(
        &lines
            .next_line()
            .await
            .expect("read initialize")
            .expect("initialize response"),
    )
    .expect("initialize JSON");
    let tools: serde_json::Value = serde_json::from_str(
        &lines
            .next_line()
            .await
            .expect("read tools")
            .expect("tools response"),
    )
    .expect("tools JSON");
    let status = child.wait().await.expect("command graph terminal");

    assert!(status.success());
    assert_eq!(initialize["result"]["protocolVersion"], "2025-06-18");
    assert_eq!(tools["result"]["tools"].as_array().map(Vec::len), Some(1));
    assert_eq!(tools["result"]["tools"][0]["name"], "tura_command_graph");
}

#[tokio::test]
async fn command_graph_forwards_child_session_and_jspace_to_router() {
    let root = tempfile::tempdir().expect("tempdir");
    let marker = root.path().join("must-not-exist.txt");
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind policy router");
    let router_addr = listener.local_addr().expect("policy router address");
    let router = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accept command graph");
        let (read, mut write) = stream.into_split();
        let mut lines = BufReader::new(read).lines();
        let line = lines
            .next_line()
            .await
            .expect("read router request")
            .expect("router request");
        let request: serde_json::Value = serde_json::from_str(&line).expect("router request JSON");
        let response = json!({
            "request_id": request["request_id"],
            "ok": true,
            "payload": {
                "result": {
                    "results": [{
                        "success": false,
                        "jspace_error_code": "JSPACE_COMMAND_DENIED"
                    }]
                }
            },
            "error": null
        });
        write
            .write_all(&serde_json::to_vec(&response).expect("router response JSON"))
            .await
            .expect("write router response");
        write.write_all(b"\n").await.expect("terminate response");
        request
    });

    let contract = jspace_contract();
    let mut child = Command::new(env!("CARGO_BIN_EXE_tura_command_graph"))
        .env("TURA_NATIVE_SESSION_ID", "session-policy-1")
        .env("TURA_NATIVE_TASK_ID", "task-policy-1")
        .env("TURA_NATIVE_EXECUTION_ID", "execution-policy-1")
        .env("TURA_NATIVE_LEASE_ID", "lease-policy-1")
        .env("TURA_NATIVE_WORKSPACE", root.path())
        .env("TURA_NATIVE_ALLOWED_COMMANDS_JSON", r#"["zsh"]"#)
        .env(
            "TURA_NATIVE_JSPACE_CONTRACT_JSON",
            serde_json::to_string(&contract).expect("J-Space JSON"),
        )
        .env("TURA_ROUTER_ADDR", router_addr.to_string())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .expect("spawn command graph");
    let mut stdin = child.stdin.take().expect("command graph stdin");
    let stdout = child.stdout.take().expect("command graph stdout");
    let write_command = serde_json::to_string(&json!({
        "command": format!("touch {}", marker.display())
    }))
    .expect("write command JSON");
    let call = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "tura_command_graph",
            "arguments": {
                "commands": [{
                    "command": "zsh",
                    "command_line": write_command
                }]
            }
        }
    });
    stdin
        .write_all(&serde_json::to_vec(&call).expect("MCP call JSON"))
        .await
        .expect("write MCP call");
    stdin.write_all(b"\n").await.expect("terminate MCP call");
    stdin.shutdown().await.expect("close MCP stdin");
    drop(stdin);

    let mut lines = BufReader::new(stdout).lines();
    let response: serde_json::Value = serde_json::from_str(
        &lines
            .next_line()
            .await
            .expect("read MCP response")
            .expect("MCP response"),
    )
    .expect("MCP response JSON");
    let status = child.wait().await.expect("command graph terminal");
    let router_request = router.await.expect("policy router terminal");

    assert!(status.success());
    assert_eq!(
        response["result"]["structuredContent"]["results"][0]["jspace_error_code"],
        "JSPACE_COMMAND_DENIED"
    );
    assert_eq!(response["result"]["isError"], true);
    assert_eq!(router_request["payload"]["session_id"], "session-policy-1");
    assert_eq!(
        router_request["payload"]["runtime_id"],
        "execution-policy-1"
    );
    assert_eq!(router_request["payload"]["jspace_contract"], contract);
    assert!(!marker.exists());
}

#[tokio::test]
async fn native_worker_wire_returns_the_typed_terminal_envelope() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = fake_codex(root.path(), false);
    let executable_sha256 = file_sha256(&executable);
    let mut child = Command::new(env!("CARGO_BIN_EXE_tura_native_codex_worker"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .expect("spawn native worker");
    let request = json!({
        "schema_version": "tura_native_codex_worker_request_v2",
        "codex_executable": executable,
        "codex_executable_sha256": executable_sha256,
        "workspace": root.path(),
        "session_id": "session-wire-1",
        "task_id": "task-wire-1",
        "execution_id": "execution-wire-1",
        "lease_id": "lease-wire-1",
        "execution_profile_sha256": TURA_EXECUTION_PROFILE_SHA256,
        "execution_binding_sha256": "d".repeat(64),
        "expected_task_context_capsule_sha256": task_context_capsule("task-wire-1").semantic_sha256,
        "expected_task_delta_sha256": task_delta("task-wire-1").semantic_sha256,
        "expected_jspace_semantic_sha256": "a".repeat(64),
        "task_context_capsule": task_context_capsule("task-wire-1"),
        "task_delta": task_delta("task-wire-1"),
        "sandbox": "read_only",
        "timeout_ms": 10000
    });
    let mut stdin = child.stdin.take().expect("native worker stdin");
    stdin
        .write_all(&serde_json::to_vec(&request).expect("request JSON"))
        .await
        .expect("write worker request");
    stdin.shutdown().await.expect("close worker stdin");
    drop(stdin);
    let output = child.wait_with_output().await.expect("worker terminal");

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let envelope: runtime_contract::NativeCodexTerminalEnvelope =
        serde_json::from_slice(&output.stdout).expect("terminal envelope");
    envelope.validate_shape().expect("valid terminal envelope");
    assert_eq!(envelope.task_id, "task-wire-1");
    assert_eq!(envelope.execution_id, "execution-wire-1");
    assert_eq!(envelope.lease_id, "lease-wire-1");
}

#[tokio::test]
async fn native_worker_wire_rejects_legacy_raw_prompt() {
    let root = tempfile::tempdir().expect("tempdir");
    let executable = fake_codex(root.path(), false);
    let executable_sha256 = file_sha256(&executable);
    let mut child = Command::new(env!("CARGO_BIN_EXE_tura_native_codex_worker"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .expect("spawn native worker");
    let request = json!({
        "schema_version": "tura_native_codex_worker_request_v2",
        "codex_executable": executable,
        "codex_executable_sha256": executable_sha256,
        "workspace": root.path(),
        "session_id": "session-wire-legacy",
        "task_id": "task-wire-legacy",
        "execution_id": "execution-wire-legacy",
        "lease_id": "lease-wire-legacy",
        "prompt": "This raw prompt surface must not be admitted.",
        "execution_profile_sha256": TURA_EXECUTION_PROFILE_SHA256,
        "execution_binding_sha256": "d".repeat(64),
        "expected_task_context_capsule_sha256": task_context_capsule("task-wire-legacy").semantic_sha256,
        "expected_task_delta_sha256": task_delta("task-wire-legacy").semantic_sha256,
        "expected_jspace_semantic_sha256": "a".repeat(64),
        "task_context_capsule": task_context_capsule("task-wire-legacy"),
        "task_delta": task_delta("task-wire-legacy"),
        "sandbox": "read_only",
        "timeout_ms": 10000
    });
    let mut stdin = child.stdin.take().expect("native worker stdin");
    stdin
        .write_all(&serde_json::to_vec(&request).expect("request JSON"))
        .await
        .expect("write worker request");
    stdin.shutdown().await.expect("close worker stdin");
    drop(stdin);
    let output = child.wait_with_output().await.expect("worker terminal");

    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("unknown field `prompt`"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
