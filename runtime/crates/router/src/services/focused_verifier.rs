use code_tools::runtime::tool::{CancellationToken, ToolContext};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::path::Path;
use std::sync::Arc;
use tura_path::command_receipts::ReceiptStore;
use tura_path::jspace::{JSpaceMatcher, VerifierCommand};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Invocation {
    #[serde(default)]
    command_type: Option<String>,
    #[serde(default)]
    command: Option<String>,
    command_line: String,
    #[serde(default)]
    step: Option<u64>,
    #[serde(default)]
    id: Option<String>,
    #[serde(default)]
    timeout_ms: Option<u64>,
    #[serde(default)]
    stall_timeout_ms: Option<u64>,
    // Existing runtime stream attribution is not an execution override.
    #[serde(default, rename = "command_id")]
    _command_id: Option<String>,
    #[serde(default, rename = "command_run_id")]
    _command_run_id: Option<String>,
    #[serde(default, rename = "provider_tool_call_id")]
    _provider_tool_call_id: Option<String>,
    #[serde(default, rename = "command_index")]
    _command_index: Option<u64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Selection {
    verifier_index: usize,
}

#[derive(Clone, Debug)]
pub(super) struct PreparedVerifier {
    call_id: String,
    index: usize,
    step: u64,
    id: Option<String>,
    command: VerifierCommand,
}

const PYTHON_IMPORT_ROOTS_PARENT_REQUIRED: &str = "VERIFIER_PYTHON_IMPORT_ROOTS_PARENT_REQUIRED";

pub(super) fn prepare_batch(
    arguments: &Value,
    matcher: Option<&JSpaceMatcher>,
    allowed_commands: Option<&BTreeSet<String>>,
    call_ids: &[String],
) -> Result<Option<Vec<PreparedVerifier>>, String> {
    prepare_batch_with_parent_channel(
        arguments, matcher, allowed_commands, call_ids, crate::parent_verifier::available(),
    )
}

fn prepare_batch_with_parent_channel(
    arguments: &Value,
    matcher: Option<&JSpaceMatcher>,
    allowed_commands: Option<&BTreeSet<String>>,
    call_ids: &[String],
    parent_channel_available: bool,
) -> Result<Option<Vec<PreparedVerifier>>, String> {
    let Some(commands) = arguments.get("commands").and_then(Value::as_array) else {
        return Ok(None);
    };
    if !commands.iter().any(|value| {
        ["command_type", "command"].iter().any(|key| {
            value.get(key).and_then(Value::as_str).is_some_and(|name| {
                code_tools::commands::canonical_command(name) == "focused_verifier"
            })
        })
    }) {
        return Ok(None);
    }
    let matcher = matcher.ok_or("VERIFIER_JSPACE_GRANT_REQUIRED")?;
    if allowed_commands.is_some_and(|allowed| !allowed.contains("focused_verifier")) {
        return Err("VERIFIER_TOOL_NOT_ALLOWED".into());
    }
    if commands.len() != call_ids.len() || commands.len() > 8 {
        return Err("VERIFIER_BATCH_INVALID:bounded verifier-only batch required".into());
    }
    for key in [
        "timeout_ms",
        "stall_timeout_ms",
        "timeoutMs",
        "stallTimeoutMs",
        "workdir",
        "cwd",
    ] {
        if arguments.get(key).is_some_and(|value| !value.is_null()) {
            return Err("VERIFIER_TIMEOUT_OVERRIDE_DENIED".into());
        }
    }
    let mut selected = BTreeSet::new();
    let mut prepared = Vec::with_capacity(commands.len());
    for (command, call_id) in commands.iter().zip(call_ids) {
        let invocation: Invocation = serde_json::from_value(command.clone())
            .map_err(|error| format!("VERIFIER_BATCH_INVALID:{error}"))?;
        if (invocation.command_type.is_none() && invocation.command.is_none())
            || [&invocation.command_type, &invocation.command].into_iter()
                .flatten().any(|name| name != "focused_verifier")
            || invocation.timeout_ms.is_some()
            || invocation.stall_timeout_ms.is_some()
            || invocation.step == Some(0)
        {
            return Err(
                "VERIFIER_BATCH_INVALID:exact verifier command without overrides required".into(),
            );
        }
        let selection: Selection = serde_json::from_str(&invocation.command_line)
            .map_err(|error| format!("VERIFIER_SELECTION_INVALID:{error}"))?;
        if !selected.insert(selection.verifier_index) {
            return Err("VERIFIER_BATCH_INVALID:duplicate verifier selection".into());
        }
        let grant = matcher
            .check_verifier_command(selection.verifier_index)
            .map_err(|error| error.to_string())?;
        if grant.python_import_roots.is_some() && !parent_channel_available {
            return Err(PYTHON_IMPORT_ROOTS_PARENT_REQUIRED.into());
        }
        prepared.push(PreparedVerifier {
            call_id: call_id.clone(),
            index: selection.verifier_index,
            step: invocation.step.unwrap_or(1),
            id: invocation.id,
            command: grant.clone(),
        });
    }
    if !parent_channel_available && !code_tools::shell_executor::focused_verifier_sandbox_available() {
        return Err("VERIFIER_READ_SCOPE_OS_POLICY_UNAVAILABLE".into());
    }
    matcher
        .revalidate_verifier_paths()
        .map_err(|error| error.to_string())?;
    Ok(Some(prepared))
}

pub(super) async fn execute_batch(
    commands: Vec<PreparedVerifier>,
    matcher: &JSpaceMatcher,
    session_directory: &Path,
    execution_id: &str,
    cancellation: CancellationToken,
    receipt_store: Arc<ReceiptStore>,
) -> Value {
    let mut results = Vec::with_capacity(commands.len());
    for (batch_index, item) in commands.into_iter().enumerate() {
        let ctx = ToolContext::new_with_lock_scope_and_cancellation(
            session_directory.to_path_buf(),
            None,
            cancellation.clone(),
        )
        .with_call_id(item.call_id.clone())
        .with_receipt_store(Some(Arc::clone(&receipt_store)));
        let accepted = code_tools::shell_executor::mark_command_run_batch_call_accepted(
            &receipt_store,
            execution_id,
            &item.call_id,
        );
        let response = if let Err(error) = accepted {
            code_tools::shell_executor::focused_verifier_not_started(
                &ctx,
                &format!("VERIFIER_BATCH_CLAIM_FAILED:{error}"),
                item.command.timeout_seconds,
            )
        } else if cancellation.is_cancelled() {
            code_tools::shell_executor::focused_verifier_not_started(
                &ctx,
                "VERIFIER_CANCELLED_BEFORE_EXECUTION",
                item.command.timeout_seconds,
            )
        } else if let Err(error) = matcher.revalidate_verifier_paths() {
            code_tools::shell_executor::focused_verifier_not_started(
                &ctx,
                &error.to_string(),
                item.command.timeout_seconds,
            )
        } else if crate::parent_verifier::available() {
            code_tools::shell_executor::execute_remote_focused_verifier(
                item.command.timeout_seconds, &ctx,
                crate::parent_verifier::request(matcher, &item.call_id, item.index, &cancellation),
            ).await
        } else if item.command.python_import_roots.is_some() {
            // The direct executor cannot install explicit PYTHONPATH; never drop it.
            code_tools::shell_executor::focused_verifier_not_started(
                &ctx, PYTHON_IMPORT_ROOTS_PARENT_REQUIRED, item.command.timeout_seconds,
            )
        } else {
            code_tools::shell_executor::execute_focused_verifier(
                &item.command.argv,
                &item.command.scratch_root,
                matcher.repo_root(),
                &matcher.scope_projection().read_scopes,
                item.command.timeout_seconds,
                &ctx,
            )
            .await
        };
        // Match command_run's result envelope so the existing model feedback
        // and binding path sees test failures instead of silently dropping them.
        let success = response.success;
        let output = code_tools::shell_executor::shell_output_value(response);
        results.push(json!({
            "index": batch_index, "step": item.step, "id": item.id,
            "command_type": "focused_verifier", "verifier_index": item.index,
            "call_id": item.call_id, "success": success, "output": output,
            "error": if success { None } else { Some("focused verifier failed; inspect output") },
        }));
    }
    json!({"results": results})
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use crate::services::command_run::CommandRunService;
    use sha2::{Digest, Sha256};
    use std::fs;
    use tura_path::jspace::{authorization_semantic_sha256, semantic_sha256};

    fn digest(path: &Path) -> String {
        format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
    }

    fn fixture(root: &Path) -> (Value, std::path::PathBuf) {
        let root = root.canonicalize().unwrap();
        let workspace = root.join("workspace");
        let artifacts = root.join("artifacts");
        let scratch = artifacts.join("verifier-0");
        fs::create_dir_all(workspace.join("src")).unwrap();
        fs::create_dir_all(&scratch).unwrap();
        let pinned = workspace.join("src/check.txt");
        fs::write(&pinned, "verified\n").unwrap();
        let executable = Path::new("/bin/cp").canonicalize().unwrap();
        let mut contract = json!({
            "schema_version":"jspace_contract_v2", "repo_root":workspace,
            "dcf_generation":{"repo_root":workspace,"generation_id":"fixture","required_domain_bindings":{}},
            "provenance":{}, "matched_surface_ids":[], "read_scopes":["src/**"],
            "write_scopes":[], "allowed_operations":["read","command"],
            "denied_operations":["network","install","system_mutation"],
            "command_templates":[], "focused_verifiers":[], "declared_targets":[],
            "expansion":{"mode":"exact_target_only","error_code":"JSPACE_EXPANSION_REQUIRED","mutation_on_expansion":false},
            "verifier_artifact_root":artifacts, "verifier_commands":[{
                "argv":[executable,pinned,scratch.join("done")],"executable_sha256":digest(&executable),
                "pinned_files":[{"path":pinned,"sha256":digest(&pinned)}],
                "timeout_seconds":10,"scratch_root":scratch,"network":false
            }]
        });
        contract["authorization_semantic_sha256"] =
            json!(authorization_semantic_sha256(&contract).unwrap());
        contract["content_sha256"] = json!(semantic_sha256(&contract));
        (contract, workspace)
    }

    fn arguments() -> Value {
        json!({"execution_id":"verifier-batch-1","commands":[{
            "command_type":"focused_verifier","command_line":"{\"verifier_index\":0}"
        }]})
    }

    #[test]
    fn import_roots_require_parent_channel_and_keep_selection_index_only() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempfile::tempdir().unwrap();
        let (mut contract, workspace) = fixture(root.path());
        let legacy = JSpaceMatcher::from_value(&workspace, &contract).unwrap();
        let allowed = BTreeSet::from(["focused_verifier".to_string()]);
        let args = arguments();
        let (_, ids) = code_tools::command_run::command_run_batch_identity(&args).unwrap();
        let old = prepare_batch_with_parent_channel(&args, Some(&legacy), Some(&allowed), &ids, true)
            .unwrap().unwrap();
        assert!(old[0].command.python_import_roots.is_none());

        let executable = workspace.join("python3.13");
        fs::write(&executable, [0xfe, 0xed, 0xfa, 0xcf, 1, 2, 3, 4]).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
        contract["verifier_commands"][0]["argv"][0] = json!(executable);
        contract["verifier_commands"][0]["executable_sha256"] = json!(digest(&executable));
        contract["verifier_commands"][0]["python_import_roots"] = json!([workspace.join("src")]);
        contract["authorization_semantic_sha256"] = json!(authorization_semantic_sha256(&contract).unwrap());
        contract.as_object_mut().unwrap().remove("content_sha256");
        contract["content_sha256"] = json!(semantic_sha256(&contract));
        let matcher = JSpaceMatcher::from_value(&workspace, &contract).unwrap();
        assert_eq!(
            prepare_batch_with_parent_channel(&args, Some(&matcher), Some(&allowed), &ids, false).unwrap_err(),
            PYTHON_IMPORT_ROOTS_PARENT_REQUIRED,
        );
        let prepared = prepare_batch_with_parent_channel(&args, Some(&matcher), Some(&allowed), &ids, true)
            .unwrap().unwrap();
        assert_eq!(prepared.len(), 1);
        assert_eq!(prepared[0].index, 0);
        assert_eq!(prepared[0].call_id, ids[0]);
        assert_eq!(prepared[0].command, matcher.verifier_commands()[0]);
        assert_eq!(args["commands"][0]["command_line"], "{\"verifier_index\":0}");
        for selection in [
            json!({"verifier_index":0,"python_import_roots":[workspace.join("src")]}),
            json!({"verifier_index":0,"argv":[executable]}),
        ] {
            let mut invalid = args.clone();
            invalid["commands"][0]["command_line"] = json!(selection.to_string());
            assert!(prepare_batch_with_parent_channel(&invalid, Some(&matcher), Some(&allowed), &ids, true).is_err());
        }
        let mut invalid = args;
        invalid["commands"][0]["python_import_roots"] = json!([workspace.join("src")]);
        assert!(prepare_batch_with_parent_channel(&invalid, Some(&matcher), Some(&allowed), &ids, true).is_err());
    }

    #[test]
    fn runtime_stream_attribution_preserves_index_only_admission() {
        let root = tempfile::tempdir().unwrap();
        let (contract, workspace) = fixture(root.path());
        let matcher = JSpaceMatcher::from_value(&workspace, &contract).unwrap();
        let allowed = BTreeSet::from(["focused_verifier".to_string()]);
        let raw = json!({"command":"focused_verifier","command_line":"{\"verifier_index\":0}",
            "step":1,"command_id":"run:tool:0","command_run_id":"run",
            "provider_tool_call_id":"tool","command_index":0});
        let normalized = code_tools::command_run::normalize_command_value_for_execution(raw, 0).unwrap();
        let mut args = json!({"execution_id":"run:tool:0","commands":[normalized]});
        let (_, ids) = code_tools::command_run::command_run_batch_identity(&args).unwrap();
        assert!(prepare_batch(&args, Some(&matcher), Some(&allowed), &ids).unwrap().is_some());
        args["commands"][0]["command_type"] = json!("shell_command");
        assert!(prepare_batch(&args, Some(&matcher), Some(&allowed), &ids).is_err());
        args["commands"][0]["command_type"] = json!("focused_verifier");
        args["commands"][0]["workdir"] = json!(workspace);
        assert!(prepare_batch(&args, Some(&matcher), Some(&allowed), &ids).is_err());
    }

    #[test]
    fn public_schema_selection_rejects_override_mixed_batch_and_stale_pins() {
        let root = tempfile::tempdir().unwrap();
        let (contract, workspace) = fixture(root.path());
        let matcher = JSpaceMatcher::from_value(&workspace, &contract).unwrap();
        let allowed = BTreeSet::from(["focused_verifier".to_string()]);
        let args = arguments();
        let (_, ids) = code_tools::command_run::command_run_batch_identity(&args).unwrap();
        if code_tools::shell_executor::focused_verifier_sandbox_available() {
            assert_eq!(
                prepare_batch(&args, Some(&matcher), Some(&allowed), &ids)
                    .unwrap()
                    .unwrap()
                    .len(),
                1
            );
        }
        assert!(
            prepare_batch(&args, None, Some(&allowed), &ids)
                .unwrap_err()
                .contains("GRANT_REQUIRED")
        );
        for field in ["argv", "workdir", "verifier_index"] {
            let mut invalid = args.clone();
            invalid["commands"][0][field] = json!("override");
            assert!(prepare_batch(&invalid, Some(&matcher), Some(&allowed), &ids).is_err());
        }
        let mut invalid = args.clone();
        invalid["commands"][0]["command_line"] = json!("{\"verifier_index\":0,\"argv\":[]}");
        assert!(prepare_batch(&invalid, Some(&matcher), Some(&allowed), &ids).is_err());
        let mut invalid = args.clone();
        invalid["commands"][0]["timeout_ms"] = json!(10000);
        assert!(prepare_batch(&invalid, Some(&matcher), Some(&allowed), &ids).is_err());
        fs::write(workspace.join("src/check.txt"), "drift").unwrap();
        if code_tools::shell_executor::focused_verifier_sandbox_available() {
            assert!(
                prepare_batch(&args, Some(&matcher), Some(&allowed), &ids)
                    .unwrap_err()
                    .contains("STALE")
            );
        }
    }

    #[tokio::test]
    async fn router_runs_public_schema_and_rejects_duplicate_without_reexecuting() {
        if !code_tools::shell_executor::focused_verifier_sandbox_available() {
            return;
        }
        let root = tempfile::tempdir().unwrap();
        let (contract, workspace) = fixture(root.path());
        let output = std::path::PathBuf::from(
            contract["verifier_commands"][0]["argv"][2]
                .as_str()
                .unwrap(),
        );
        let request = json!({
            "session_id":"verifier-session","runtime_id":"verifier-runtime",
            "session_directory":workspace,"arguments":arguments(),
            "allowed_commands":["focused_verifier"],"jspace_contract":contract,"sandbox":false
        });
        let service = CommandRunService::new();
        let result = service.execute(request.clone()).await.unwrap();
        assert_eq!(result["result"]["results"][0]["success"], true, "{result}");
        assert_eq!(result["result"]["results"][0]["output"]["exit_code"], 0);
        assert_eq!(
            result["result"]["results"][0]["output"]["terminal_receipt"]["terminal_state"],
            "completed"
        );
        assert_eq!(fs::read_to_string(&output).unwrap(), "verified\n");
        fs::write(&output, "replay sentinel").unwrap();
        let replay = service.execute(request).await.unwrap();
        assert_eq!(replay["result"]["results"][0]["success"], false, "{replay}");
        assert!(
            replay
                .to_string()
                .contains("COMMAND_EXECUTION_ALREADY_CLAIMED")
        );
        assert_eq!(fs::read_to_string(&output).unwrap(), "replay sentinel");
    }
}
