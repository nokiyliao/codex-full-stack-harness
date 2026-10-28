//! Parent-executed read-only shell lines (J-Space `parent_shell`). A writing worker's shell
//! lines cannot get a per-shell OS profile inside its fenced engine, so the router sends each
//! line over the inherited parent channel; the parent applies the profile the contract binds.
use code_tools::runtime::tool::{CancellationToken, ToolContext};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tura_path::command_receipts::ReceiptStore;
use tura_path::jspace::JSpaceMatcher;

const DEFAULT_TIMEOUT_SECONDS: u64 = 600;
const MAX_TIMEOUT_SECONDS: u64 = 3600;
const MAX_LINE_BYTES: usize = 60_000;

#[derive(Debug)]
pub(super) struct PreparedShell {
    call_id: String,
    step: u64,
    id: Option<String>,
    command_type: String,
    line: String,
    workdir: Option<PathBuf>,
    timeout_seconds: u64,
}

fn command_kind(command: &Value) -> Option<&str> {
    command
        .get("command_type")
        .or_else(|| command.get("command"))
        .and_then(Value::as_str)
}

fn timeout_ms(value: &Value) -> Option<u64> {
    ["timeout_ms", "timeoutMs"].iter().find_map(|key| value.get(*key).and_then(Value::as_u64))
}

pub(super) fn prepare_batch(
    arguments: &Value,
    matcher: Option<&JSpaceMatcher>,
    call_ids: &[String],
) -> Result<Option<Vec<PreparedShell>>, String> {
    prepare_batch_with_parent_channel(arguments, matcher, call_ids, crate::parent_verifier::available())
}

fn prepare_batch_with_parent_channel(
    arguments: &Value,
    matcher: Option<&JSpaceMatcher>,
    call_ids: &[String],
    parent_channel_available: bool,
) -> Result<Option<Vec<PreparedShell>>, String> {
    let Some(matcher) = matcher.filter(|matcher| matcher.parent_shell_admitted()) else {
        return Ok(None);
    };
    let Some(commands) = arguments.get("commands").and_then(Value::as_array) else {
        return Ok(None);
    };
    let routed = commands
        .iter()
        .filter(|command| {
            let line = command.get("command_line").and_then(Value::as_str).unwrap_or_default();
            command_kind(command).is_some_and(|kind| matcher.parent_shell_routes(kind, line))
        })
        .count();
    if routed == 0 {
        return Ok(None);
    }
    // Preflight already refused a mixed batch; keep the invariant local as well.
    if routed != commands.len() || commands.len() != call_ids.len() {
        return Err("PARENT_SHELL_BATCH_MIXED: send shell lines in their own command_run call".into());
    }
    if !parent_channel_available {
        return Err("PARENT_SHELL_CHANNEL_UNAVAILABLE: this worker has no inherited parent channel".into());
    }
    let batch_timeout = timeout_ms(arguments);
    let mut prepared = Vec::with_capacity(commands.len());
    for (command, call_id) in commands.iter().zip(call_ids) {
        let raw = command.get("command_line").and_then(Value::as_str).unwrap_or_default();
        let (line, wrapped_workdir) = tura_path::jspace::shell_line_parts(raw);
        if line.trim().is_empty() || line.len() > MAX_LINE_BYTES {
            return Err(format!("PARENT_SHELL_LINE_INVALID: 1..={MAX_LINE_BYTES} bytes required"));
        }
        let workdir = ["workdir", "cwd"]
            .iter()
            .find_map(|key| command.get(*key).and_then(Value::as_str))
            .map(str::to_owned)
            .or(wrapped_workdir)
            .map(|directory| {
                let path = Path::new(&directory);
                let absolute = if path.is_absolute() { path.to_path_buf() } else { matcher.repo_root().join(path) };
                matcher.ensure_in_root(&absolute, "command").map(|()| absolute).map_err(|error| error.to_string())
            })
            .transpose()?;
        let timeout_seconds = timeout_ms(command)
            .or(batch_timeout)
            .map_or(DEFAULT_TIMEOUT_SECONDS, |milliseconds| milliseconds.div_ceil(1000))
            .clamp(1, MAX_TIMEOUT_SECONDS);
        prepared.push(PreparedShell {
            call_id: call_id.clone(),
            step: command.get("step").and_then(Value::as_u64).filter(|step| *step > 0).unwrap_or(1),
            id: command.get("id").and_then(Value::as_str).map(str::to_owned),
            command_type: command_kind(command).unwrap_or("zsh").to_owned(),
            line,
            workdir,
            timeout_seconds,
        });
    }
    Ok(Some(prepared))
}

pub(super) async fn execute_batch(
    commands: Vec<PreparedShell>,
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
                &format!("PARENT_SHELL_BATCH_CLAIM_FAILED:{error}"),
                item.timeout_seconds,
            )
        } else {
            let workdir = item.workdir.as_deref().and_then(Path::to_str);
            code_tools::shell_executor::execute_remote_parent_shell(
                item.timeout_seconds,
                &ctx,
                crate::parent_verifier::request_shell(
                    matcher, &item.call_id, &item.line, workdir, item.timeout_seconds, &cancellation,
                ),
            )
            .await
        };
        let success = response.success;
        let output = code_tools::shell_executor::shell_output_value(response);
        results.push(json!({
            "index": batch_index, "step": item.step, "id": item.id,
            "command_type": item.command_type, "call_id": item.call_id,
            "success": success, "output": output,
            "error": if success { None } else { Some("shell command failed; inspect output") },
        }));
    }
    json!({"results": results})
}

#[cfg(test)]
mod tests {
    use super::*;
    use tura_path::jspace::{authorization_semantic_sha256, semantic_sha256};

    fn contract(root: &Path, parent_shell: bool) -> Value {
        let mut value = json!({
            "schema_version":"jspace_contract_v2", "repo_root":root,
            "dcf_generation":{"repo_root":root,"generation_id":"g","required_domain_bindings":{}},
            "provenance":{}, "matched_surface_ids":[], "read_scopes":["src/**"], "write_scopes":["src/**"],
            "allowed_operations":["read","create","modify","command"],
            "denied_operations":["network","install","system_mutation"],
            "command_templates":[{"argv":["git","status","--short"],"effects":["read"],"targets":[]}],
            "focused_verifiers":[], "declared_targets":["src/main.rs"],
            "expansion":{"mode":"exact_target_only","error_code":"JSPACE_EXPANSION_REQUIRED","mutation_on_expansion":false}
        });
        if parent_shell {
            value["parent_shell"] = json!({"schema_version":"nokiy_parent_shell_v1",
                "profile_sha256":"c".repeat(64),"evidence_root":root.join("evidence"),"network":"inherit"});
        }
        value["authorization_semantic_sha256"] = json!(authorization_semantic_sha256(&value).unwrap());
        value["content_sha256"] = json!(semantic_sha256(&value));
        value
    }

    fn ids(count: usize) -> Vec<String> {
        (0..count).map(|index| format!("e:step:1:index:{index}")).collect()
    }

    #[test]
    fn only_whole_shell_batches_route_to_the_parent() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        std::fs::create_dir_all(root.join("src")).unwrap();
        let matcher = JSpaceMatcher::from_value(&root, &contract(&root, true)).unwrap();
        let shells = json!({"commands":[
            {"command_type":"zsh","command_line":"rg -n needle src | head","timeout_ms":1500},
            {"command_type":"bash","command_line":"{\"command\":\"git diff --check\",\"workdir\":\"src\"}","step":2}
        ]});
        let prepared = prepare_batch_with_parent_channel(&shells, Some(&matcher), &ids(2), true)
            .unwrap().expect("routed");
        assert_eq!(prepared[0].timeout_seconds, 2);
        assert_eq!(prepared[1].line, "git diff --check");
        assert_eq!(prepared[1].workdir.as_deref(), Some(root.join("src").as_path()));
        assert_eq!(prepared[1].step, 2);
        assert_eq!(prepared[1].timeout_seconds, DEFAULT_TIMEOUT_SECONDS);
        // Without the inherited channel the routed batch is refused, never run in the engine.
        assert!(prepare_batch_with_parent_channel(&shells, Some(&matcher), &ids(2), false)
            .unwrap_err().starts_with("PARENT_SHELL_CHANNEL_UNAVAILABLE"));
        // Templates, other tools and contracts without the policy stay on the existing route.
        let template = json!({"commands":[{"command_type":"bash","command_line":"git status --short"}]});
        assert!(prepare_batch_with_parent_channel(&template, Some(&matcher), &ids(1), true).unwrap().is_none());
        let ordinary = JSpaceMatcher::from_value(&root, &contract(&root, false)).unwrap();
        assert!(prepare_batch_with_parent_channel(&shells, Some(&ordinary), &ids(2), true).unwrap().is_none());
        let mixed = json!({"commands":[
            {"command_type":"zsh","command_line":"git diff --check"},
            {"command_type":"apply_patch","command_line":"*** Begin Patch\n*** End Patch"}
        ]});
        assert!(prepare_batch_with_parent_channel(&mixed, Some(&matcher), &ids(2), true)
            .unwrap_err().starts_with("PARENT_SHELL_BATCH_MIXED"));
        let outside = json!({"commands":[{"command_type":"zsh","command_line":"ls","workdir":"/"}]});
        assert!(prepare_batch_with_parent_channel(&outside, Some(&matcher), &ids(1), true).is_err());
    }
}
