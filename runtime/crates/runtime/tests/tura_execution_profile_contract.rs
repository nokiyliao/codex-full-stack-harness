use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

const PROFILE_PATH: &str = "crates/runtime/tests/fixtures/tura_execution_profile_v1.json";

#[derive(Debug, Deserialize)]
struct BoundFile {
    path: String,
    bytes: usize,
    sha256: String,
}

#[derive(Debug, Deserialize)]
struct ProfileMember {
    path: String,
    role: String,
    bytes: usize,
    sha256: String,
}

#[derive(Debug, Deserialize)]
struct ExecutionProfile {
    schema_version: String,
    profile_id: String,
    source_head: String,
    behavior_fixture: BoundFile,
    member_count: usize,
    members: Vec<ProfileMember>,
    profile_sha256: String,
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn sha256(bytes: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(bytes.as_ref()))
}

fn assert_bound_file(root: &Path, bound: &BoundFile) -> Vec<u8> {
    assert!(
        !Path::new(&bound.path).is_absolute(),
        "absolute profile path"
    );
    assert!(
        !bound.path.split('/').any(|segment| segment == ".."),
        "profile path escapes the source root: {}",
        bound.path
    );
    let bytes = fs::read(root.join(&bound.path)).expect("read profile-bound file");
    assert_eq!(bytes.len(), bound.bytes, "byte drift: {}", bound.path);
    assert_eq!(sha256(&bytes), bound.sha256, "hash drift: {}", bound.path);
    bytes
}

fn profile_preimage(profile: &ExecutionProfile) -> String {
    let mut canonical = format!(
        "schema_version={}\nprofile_id={}\nsource_head={}\nbehavior_fixture={}|{}|{}\n",
        profile.schema_version,
        profile.profile_id,
        profile.source_head,
        profile.behavior_fixture.path,
        profile.behavior_fixture.bytes,
        profile.behavior_fixture.sha256
    );
    let mut members = profile.members.iter().collect::<Vec<_>>();
    members.sort_by(|left, right| left.path.cmp(&right.path));
    for member in members {
        canonical.push_str(&format!(
            "member={}|{}|{}|{}\n",
            member.path, member.role, member.bytes, member.sha256
        ));
    }
    canonical
}

#[test]
fn performance_critical_tura_execution_profile_is_hash_bound() {
    let root = repo_root();
    let profile_bytes = fs::read(root.join(PROFILE_PATH)).expect("read execution profile");
    let profile: ExecutionProfile =
        serde_json::from_slice(&profile_bytes).expect("parse execution profile");

    assert_eq!(profile.schema_version, "tura_execution_profile_v1");
    assert_eq!(profile.profile_id, "balanced-command-graph-v1");
    assert_eq!(profile.source_head.len(), 40);
    assert_eq!(profile.member_count, profile.members.len());
    assert_eq!(profile.member_count, 47);

    let behavior_bytes = assert_bound_file(&root, &profile.behavior_fixture);
    let behavior: Value = serde_json::from_slice(&behavior_bytes).expect("parse behavior fixture");
    assert_eq!(
        behavior["schema_version"],
        "tura_execution_profile_behavior_v1"
    );
    assert_eq!(behavior["profile_id"], profile.profile_id);
    assert_eq!(behavior["cases"].as_array().map(Vec::len), Some(5));

    let case_ids = behavior["cases"]
        .as_array()
        .expect("behavior cases")
        .iter()
        .filter_map(|case| case["case_id"].as_str())
        .collect::<BTreeSet<_>>();
    for required in [
        "no_tool_completion_requires_user_response",
        "same_step_reads_then_write_barrier",
        "previous_step_output_binding",
        "settled_command_receipt_is_not_replayed",
        "task_status_compact_context_checkpoint",
    ] {
        assert!(
            case_ids.contains(required),
            "missing behavior case {required}"
        );
    }

    let mut paths = BTreeSet::new();
    let mut roles = BTreeSet::new();
    for member in &profile.members {
        assert!(paths.insert(member.path.as_str()), "duplicate member path");
        roles.insert(member.role.as_str());
        assert_bound_file(
            &root,
            &BoundFile {
                path: member.path.clone(),
                bytes: member.bytes,
                sha256: member.sha256.clone(),
            },
        );
    }
    for required in [
        "agent_config",
        "agent_prompt",
        "command_graph_schema",
        "command_graph_policy",
        "command_graph_runtime",
        "shell_executor",
        "durable_command_receipts",
        "runtime_command_graph",
        "checkpoint_recovery",
        "task_status",
        "prompt_context",
        "completion_policy",
        "prompt_manual_compiler",
        "prompt_manual_identity",
        "prompt_manual",
    ] {
        assert!(roles.contains(required), "missing profile role {required}");
    }

    assert_eq!(
        sha256(profile_preimage(&profile)),
        profile.profile_sha256,
        "execution profile identity drift"
    );
}

#[test]
fn profile_preserves_balanced_command_graph_semantics() {
    let root = repo_root();
    let agent: Value = serde_json::from_slice(
        &fs::read(root.join("agents/src/balanced/agent_config.json")).expect("agent config"),
    )
    .expect("parse agent config");
    assert_eq!(
        agent["provider"]["current_model"],
        "official_codex_app_server/gpt-5.6-sol"
    );
    assert_eq!(agent["provider"]["model_reasoning_effort"], "high");
    assert_eq!(agent["provider"]["service_tier"], "priority");

    let capabilities = agent["agent_capabilities"]
        .as_array()
        .expect("agent capabilities")
        .iter()
        .filter_map(|capability| capability["capability_name"].as_str())
        .collect::<BTreeSet<_>>();
    for required in ["apply_patch", "shells", "web_discover", "task_status"] {
        assert!(
            capabilities.contains(required),
            "missing capability {required}"
        );
    }

    let schema: Value = serde_json::from_slice(
        &fs::read(root.join("crates/tools/src/command_run/schema.json"))
            .expect("command_run schema"),
    )
    .expect("parse command_run schema");
    assert_eq!(
        schema["input_schema"]["properties"]["commands"]["maxItems"],
        20
    );

    let policy = fs::read_to_string(root.join("crates/tools/src/command_run/policy.toml"))
        .expect("command_run policy");
    for required in [
        "same_step_read_concurrency = true",
        "mutating_commands_are_barriers = true",
        "read = \"shared\"",
        "write = \"exclusive\"",
        "unknown_mutating_shell = \"workspace_exclusive\"",
    ] {
        assert!(
            policy.contains(required),
            "missing command graph policy {required}"
        );
    }

    let binding = fs::read_to_string(root.join("crates/tools/src/command_run/output_binding.rs"))
        .expect("output binding source");
    assert!(binding.contains("const PLACEHOLDER_OPEN: &str = \"#@#${\";"));
    assert!(binding.contains("const PLACEHOLDER_CLOSE: &str = \"#@#$\";"));

    let terminal =
        fs::read_to_string(root.join("crates/runtime/src/prompt_style/terminal_final_response.rs"))
            .expect("terminal response policy");
    assert!(terminal.contains("send the user-facing assistant reply directly"));
    assert!(terminal.contains("without calling tools"));
}
