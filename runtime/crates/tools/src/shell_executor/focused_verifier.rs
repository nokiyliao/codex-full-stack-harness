//! Exact verifier processes reuse the normal command receipt and cleanup owner.
use super::{execution, response, terminalize_pre_execution_zero_effect};
use crate::commands::CommandResponse;
use crate::runtime::tool::ToolContext;
use std::path::{Component, Path, PathBuf};
use std::time::Instant;
use serde::{Deserialize, Serialize};
use serde_json::json;

/// Subordinate process facts, never a second durable command receipt.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifierObservation {
    pub success: bool,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub process_reaped: bool,
    pub process_group_empty: bool,
    pub outcome: String,
    /// Opaque model-review context from the parent channel, never execution authority.
    #[serde(default)]
    pub source_postimages: Option<serde_json::Value>,
    /// Source-bound parent evidence; the runtime validates its binding before resolution.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub verification_evidence: Option<serde_json::Value>,
}

/// Claim before sending across the parent channel. Lost replies remain unknown.
pub async fn execute_remote_focused_verifier<F>(
    timeout_seconds: u64, ctx: &ToolContext, request: F,
) -> CommandResponse
where F: std::future::Future<Output = Result<VerifierObservation, String>> {
    let started = Instant::now();
    if ctx.cancellation.is_cancelled() {
        return focused_verifier_not_started(ctx, "VERIFIER_CANCELLED_BEFORE_EXECUTION", timeout_seconds);
    }
    let admitted = ctx.bound_receipt_store().map_err(|error| error.to_string())
        .and_then(|store| execution::reconcile_command_execution_claims(&store, ctx.current_call_id()))
        .and_then(|_| execution::claim_parent_verifier_execution(ctx, timeout_seconds));
    if let Err(error) = admitted {
        return response::failed_async_response(&format!("VERIFIER_CLAIM_FAILED:{error}"), -1);
    }
    let observed = request.await;
    let (mut response, reaped, empty, known) = match observed {
        Ok(value) => {
            let known = value.outcome == "known" && value.process_reaped && value.process_group_empty;
            let success = value.success && value.exit_code == 0 && known;
            let mut output = json!({"executor":"parent_focused_verifier"});
            if success {
                if let Some(source_postimages) = value.source_postimages {
                    output["source_postimages"] = source_postimages;
                }
                if let Some(verification_evidence) = value.verification_evidence {
                    output["verification_evidence"] = verification_evidence;
                }
            }
            (CommandResponse {
                success,
                exit_code: value.exit_code, stdout: value.stdout, stderr: value.stderr,
                output, changes: Vec::new(),
            }, value.process_reaped, value.process_group_empty, known)
        }
        Err(error) => (response::failed_async_response(&error, -1), false, false, false),
    };
    let terminal = if !known { "terminated" } else if response.success { "completed" } else { "failed" };
    if let Err(error) = execution::attach_durable_terminal_receipt(
        &mut response, ctx, None, started, timeout_seconds, None, terminal,
        if known { "workload" } else { "parent_channel_unsettled" }, "parent_verifier",
        if known { "known" } else { "unknown" }, reaped, empty,
    ) {
        response.success = false;
        response.exit_code = -1;
        response.stderr = error;
        response.output = json!({"outcome":"unknown","reconcile_required":true});
    }
    response
}

pub fn focused_verifier_sandbox_available() -> bool {
    cfg!(target_os = "macos")
        && Path::new("/usr/bin/sandbox-exec").is_file()
        && std::env::var("CODEX_SANDBOX").as_deref() != Ok("seatbelt")
        && !inherited_seatbelt_active()
}

#[cfg(target_os = "macos")]
#[allow(
    unsafe_code,
    reason = "read-only Darwin sandbox membership query; no policy mutation"
)]
fn inherited_seatbelt_active() -> bool {
    #[link(name = "sandbox")]
    unsafe extern "C" {
        fn sandbox_check(pid: i32, operation: *const std::ffi::c_char, flags: i32, ...) -> i32;
    }
    // A null operation queries membership. An unknown result is not permission
    // to attempt nested sandbox_init, which Darwin rejects for inherited scopes.
    unsafe { sandbox_check(std::process::id() as i32, std::ptr::null(), 0) != 0 }
}

#[cfg(not(target_os = "macos"))]
fn inherited_seatbelt_active() -> bool {
    true
}

pub fn focused_verifier_not_started(
    ctx: &ToolContext,
    reason: &str,
    timeout_seconds: u64,
) -> CommandResponse {
    terminalize_pre_execution_zero_effect(
        ctx,
        response::failed_async_response(reason, -1),
        timeout_seconds,
        None,
    )
}

fn canonical(path: &Path, directory: bool) -> Result<PathBuf, String> {
    if !path.is_absolute()
        || path
            .components()
            .any(|part| matches!(part, Component::ParentDir | Component::CurDir))
        || path.canonicalize().ok().as_deref() != Some(path)
        || (directory && !path.is_dir())
        || (!directory && !path.is_file())
    {
        return Err(format!("VERIFIER_NONCANONICAL_PATH:{}", path.display()));
    }
    Ok(path.to_path_buf())
}

fn rule(kind: &str, path: &Path) -> Result<String, String> {
    let text = path.to_str().ok_or("VERIFIER_NON_UTF8_PATH")?;
    let quoted = serde_json::to_string(text).map_err(|error| error.to_string())?;
    Ok(format!("({kind} {quoted})"))
}

fn directory_read_rule(path: &Path) -> Result<String, String> {
    Ok(format!(
        "(require-all {} (vnode-type DIRECTORY))",
        rule("literal", path)?
    ))
}

pub fn focused_verifier_policy(
    executable: &Path,
    workspace: &Path,
    scratch: &Path,
    read_scopes: &[String],
) -> Result<String, String> {
    canonical(executable, false)?;
    canonical(workspace, true)?;
    canonical(scratch, true)?;
    if scratch.starts_with(workspace) || workspace.starts_with(scratch) || read_scopes.is_empty() {
        return Err("VERIFIER_SCOPE_OVERLAP_OR_EMPTY".into());
    }
    let mut reads = vec![rule("literal", executable)?, rule("subpath", scratch)?];
    let mut executables = vec![rule("literal", executable)?];
    // These are OS runtime data, not all of /usr (which includes user-local data).
    for root in ["/System", "/usr/lib", "/Library/Apple"] {
        reads.push(rule("subpath", Path::new(root))?);
    }
    for path in ["/", "/dev/null", "/dev/urandom"] {
        reads.push(rule("literal", Path::new(path))?);
    }
    // O_RDONLY | O_DIRECTORY descriptor walks need directory data, not just
    // metadata. Admit exact ancestors of both roots, never their file contents
    // or subtrees; canonical validation above still rejects symlinked roots.
    for directory in workspace
        .parent()
        .into_iter()
        .chain(scratch.parent())
        .flat_map(Path::ancestors)
        .collect::<std::collections::BTreeSet<_>>()
    {
        reads.push(directory_read_rule(directory)?);
    }
    // A pinned standalone CPython needs its co-located standard library. No
    // virtualenv, user site, PYTHONPATH or arbitrary sibling prefix is admitted.
    let name = executable
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    if name.strip_prefix("python").is_some_and(|version| {
        !version.is_empty()
            && version
                .bytes()
                .all(|byte| byte.is_ascii_digit() || byte == b'.')
    }) && executable
        .parent()
        .and_then(Path::file_name)
        .and_then(|name| name.to_str())
        == Some("bin")
    {
        let prefix = executable
            .parent()
            .and_then(Path::parent)
            .ok_or("VERIFIER_TOOLCHAIN_INVALID")?;
        let libraries = prefix.join("lib");
        canonical(&libraries, true)?;
        if libraries.starts_with(workspace) || libraries.starts_with(scratch) {
            return Err("VERIFIER_TOOLCHAIN_OVERLAP".into());
        }
        reads.push(rule("subpath", &libraries)?);
        if prefix.parent().and_then(Path::file_name).is_some_and(|name| name == "Versions")
            && prefix.parent().and_then(Path::parent).and_then(Path::file_name)
                .is_some_and(|name| name == "Python.framework")
        {
            let framework = prefix.join("Python");
            canonical(&framework, false)?;
            reads.push(rule("literal", &framework)?);
            let app = prefix.join("Resources/Python.app/Contents/MacOS/Python");
            canonical(&app, false)?;
            reads.push(rule("literal", &app)?);
            executables.push(rule("literal", &app)?);
        }
    }
    for scope in read_scopes {
        let subtree = scope.ends_with("/**");
        let relative = scope.strip_suffix("/**").unwrap_or(scope);
        let path = Path::new(relative);
        if path.is_absolute()
            || relative.is_empty()
            || relative.chars().any(|c| "*?[]\\".contains(c))
            || path
                .components()
                .any(|part| !matches!(part, Component::Normal(_)))
        {
            return Err("VERIFIER_READ_SCOPE_INVALID".into());
        }
        let target = workspace.join(path);
        canonical(&target, subtree)?;
        reads.push(rule(if subtree { "subpath" } else { "literal" }, &target)?);
        // Python import discovery needs directory entries, not sibling contents.
        for directory in target.parent().into_iter().flat_map(Path::ancestors) {
            if !directory.starts_with(workspace) {
                break;
            }
            reads.push(directory_read_rule(directory)?);
        }
    }
    Ok(format!(
        "(version 1)\n(deny default)\n(allow process-exec {})\n(allow process-fork)\n\
         (allow signal (target same-sandbox))\n(allow sysctl-read)\n(allow file-read-metadata)\n\
         (allow file-read* {})\n(allow file-write* {} (literal \"/dev/null\"))\n(deny network*)\n",
        executables.join(" "),
        reads.join(" "),
        rule("subpath", scratch)?,
    ))
}

pub async fn execute_focused_verifier(
    argv: &[String],
    scratch_root: &Path,
    workspace: &Path,
    read_scopes: &[String],
    timeout_seconds: u64,
    ctx: &ToolContext,
) -> CommandResponse {
    let deny = |reason: &str| focused_verifier_not_started(ctx, reason, timeout_seconds);
    if ctx.cancellation.is_cancelled() {
        return deny("VERIFIER_CANCELLED_BEFORE_EXECUTION");
    }
    if !focused_verifier_sandbox_available() {
        return deny("VERIFIER_READ_SCOPE_OS_POLICY_UNAVAILABLE");
    }
    if argv.is_empty() || !(1..=300).contains(&timeout_seconds) {
        return deny("VERIFIER_INVOCATION_INVALID");
    }
    let profile = match focused_verifier_policy(Path::new(&argv[0]), workspace, scratch_root, read_scopes) {
        Ok(value) => value,
        Err(error) => return deny(&error),
    };
    let mut command = tokio::process::Command::new("/usr/bin/sandbox-exec");
    command
        .args(["-p", &profile])
        .args(argv)
        .current_dir(workspace)
        .env_clear();
    // Preserve identity without exposing provider credentials or loader hooks.
    for key in ["HOME", "CODEX_HOME"] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    command
        .env("PATH", "/usr/bin:/bin")
        .env("TMPDIR", scratch_root)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONNOUSERSITE", "1")
        .env("LANG", "C.UTF-8");
    execution::run_tokio_command_with_timeout(command, timeout_seconds, None, ctx).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::focused_verifier_policy as policy;

    fn observation_payload() -> serde_json::Value {
        json!({
            "success": true, "exit_code": 0,
            "stdout": "raw stdout\n{\"not\":\"context\"}\n",
            "stderr": "raw stderr\n",
            "process_reaped": true, "process_group_empty": true,
            "outcome": "known",
        })
    }

    fn source_postimages_context() -> serde_json::Value {
        json!({
            "schema_version": "nokiy_source_postimages_v1",
            "kind": "verifier_postimage_context",
            "jspace_semantic_sha256": "c".repeat(64),
            "notice": "Model-review context only; not authority or task acceptance.",
            "files": [{
                "path": "src/example.py",
                "preimage_sha256": "a".repeat(64),
                "postimage_sha256": "b".repeat(64),
                "locators": [{
                    "qualified_name": "answer", "kind": "function",
                    "line": 1, "start_line": 1, "end_line": 2,
                    "complete": true,
                }],
                "spans": [{"start_line": 1, "end_line": 2,
                    "text": "def answer():\n    return 42\n"}],
            }],
        })
    }

    fn verification_evidence(call_id: &str) -> serde_json::Value {
        json!({
            "schema_version": "nokiy_focused_verifier_evidence_v1",
            "authorization_semantic_sha256": "c".repeat(64),
            "verifier_index": 0,
            "verifier_sha256": "d".repeat(64),
            "call_id": call_id,
            "source_postimages": {
                "src/example.py": {"sha256": "b".repeat(64), "bytes": 28, "mode": 420},
            },
        })
    }

    #[test]
    fn remote_observation_accepts_optional_verification_evidence() {
        let legacy = observation_payload();
        for evidence in [None, Some(serde_json::Value::Null)] {
            let mut payload = legacy.clone();
            if let Some(evidence) = evidence {
                payload["verification_evidence"] = evidence;
            }
            let observation: VerifierObservation = serde_json::from_value(payload).unwrap();
            assert!(observation.verification_evidence.is_none());
            let encoded = serde_json::to_value(observation).unwrap();
            assert!(encoded.get("verification_evidence").is_none());
        }
        let mut payload = legacy;
        payload["source_postimages"] = serde_json::Value::Null;
        payload["verification_evidence"] = verification_evidence("remote-bound");
        let observation: VerifierObservation = serde_json::from_value(payload.clone()).unwrap();
        assert_eq!(observation.verification_evidence.as_ref(), payload.get("verification_evidence"));
        assert_eq!(serde_json::to_value(observation).unwrap(), payload);
    }

    #[test]
    fn remote_observation_accepts_legacy_missing_context() {
        let mut payload = observation_payload();
        let observation: VerifierObservation =
            serde_json::from_value(payload.clone()).unwrap();
        assert!(observation.source_postimages.is_none());
        payload["source_postimages"] = serde_json::Value::Null;
        assert_eq!(serde_json::to_value(observation).unwrap(), payload);
    }

    #[test]
    fn remote_observation_roundtrips_null_and_source_postimages() {
        for context in [serde_json::Value::Null, source_postimages_context()] {
            let mut payload = observation_payload();
            payload["source_postimages"] = context.clone();
            let observation: VerifierObservation =
                serde_json::from_value(payload.clone()).unwrap();
            if context.is_null() {
                assert!(observation.source_postimages.is_none());
            } else {
                assert_eq!(observation.source_postimages.as_ref(), Some(&context));
            }
            assert_eq!(serde_json::to_value(observation).unwrap(), payload);
        }
    }

    #[test]
    fn remote_observation_still_rejects_unknown_top_level_fields() {
        let mut payload = observation_payload();
        payload["unexpected"] = json!(true);
        assert!(serde_json::from_value::<VerifierObservation>(payload).is_err());
    }

    #[tokio::test]
    async fn remote_focused_verifier_forwards_context_only_on_known_success() {
        use std::sync::Arc;
        let root = tempfile::tempdir().unwrap();
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(&root.path().canonicalize().unwrap()).unwrap());
        for (call_id, context) in [
            ("remote-legacy", None),
            ("remote-null", Some(serde_json::Value::Null)),
            ("remote-postimages", Some(source_postimages_context())),
        ] {
            let ctx = ToolContext::new(root.path().canonicalize().unwrap())
                .with_call_id(call_id.into()).with_receipt_store(Some(Arc::clone(&store)));
            let mut payload = observation_payload();
            if let Some(context) = context {
                payload["source_postimages"] = context;
            }
            let observation: VerifierObservation =
                serde_json::from_value(payload).unwrap();
            let expected_context = observation.source_postimages.clone();
            let raw_stdout = observation.stdout.clone();
            let raw_stderr = observation.stderr.clone();
            let result = execute_remote_focused_verifier(5, &ctx, async { Ok(observation) }).await;
            assert!(result.success);
            assert_eq!(result.exit_code, 0);
            assert_eq!(result.stdout, raw_stdout);
            assert_eq!(result.stderr, raw_stderr);
            assert!(result.changes.is_empty());
            assert_eq!(result.output["executor"], "parent_focused_verifier");
            assert_eq!(result.output.get("source_postimages"), expected_context.as_ref());
            assert!(result.output.get("verification_evidence").is_none());
            assert_eq!(result.output["terminal_receipt"]["call_id"], call_id);
            assert_eq!(result.output["terminal_receipt"]["terminal_state"], "completed");
            assert_eq!(result.output["terminal_receipt"]["termination_proven"], true);
        }
    }

    #[tokio::test]
    async fn remote_focused_verifier_forwards_source_bound_evidence_on_known_success() {
        use std::sync::Arc;
        let root = tempfile::tempdir().unwrap();
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(&root.path().canonicalize().unwrap()).unwrap());
        let call_id = "remote-bound";
        let ctx = ToolContext::new(root.path().canonicalize().unwrap())
            .with_call_id(call_id.into()).with_receipt_store(Some(store));
        let evidence = verification_evidence(call_id);
        let mut payload = observation_payload();
        payload["verification_evidence"] = evidence.clone();
        let observation: VerifierObservation = serde_json::from_value(payload).unwrap();
        let raw_stdout = observation.stdout.clone();
        let raw_stderr = observation.stderr.clone();
        let result = execute_remote_focused_verifier(5, &ctx, async { Ok(observation) }).await;
        assert!(result.success);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, raw_stdout);
        assert_eq!(result.stderr, raw_stderr);
        assert!(result.changes.is_empty());
        assert_eq!(result.output["verification_evidence"], evidence);
        assert_eq!(result.output["terminal_receipt"]["call_id"], call_id);
        assert_eq!(result.output["terminal_receipt"]["terminal_state"], "completed");
        assert_eq!(result.output["terminal_receipt"]["termination_proven"], true);
    }

    #[tokio::test]
    async fn remote_focused_verifier_withholds_context_without_known_success() {
        use std::sync::Arc;
        for (call_id, success, exit_code, outcome, reaped, empty) in [
            ("remote-failed", false, 1, "known", true, true),
            ("remote-success-false", false, 0, "known", true, true),
            ("remote-nonzero", true, 1, "known", true, true),
            ("remote-unknown", true, 0, "unknown", true, true),
            ("remote-unreaped", true, 0, "known", false, true),
            ("remote-group-live", true, 0, "known", true, false),
        ] {
            let root = tempfile::tempdir().unwrap();
            let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(&root.path().canonicalize().unwrap()).unwrap());
            let ctx = ToolContext::new(root.path().canonicalize().unwrap())
                .with_call_id(call_id.into()).with_receipt_store(Some(store));
            let mut payload = observation_payload();
            payload["success"] = json!(success);
            payload["exit_code"] = json!(exit_code);
            payload["outcome"] = json!(outcome);
            payload["process_reaped"] = json!(reaped);
            payload["process_group_empty"] = json!(empty);
            payload["source_postimages"] = source_postimages_context();
            payload["verification_evidence"] = verification_evidence(call_id);
            let observation: VerifierObservation =
                serde_json::from_value(payload).unwrap();
            let result = execute_remote_focused_verifier(5, &ctx, async { Ok(observation) }).await;
            assert!(!result.success);
            assert!(result.output.get("source_postimages").is_none());
            assert!(result.output.get("verification_evidence").is_none());
        }
    }

    #[tokio::test]
    async fn remote_focused_verifier_channel_error_does_not_forward_context() {
        use std::sync::Arc;
        let root = tempfile::tempdir().unwrap();
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(&root.path().canonicalize().unwrap()).unwrap());
        let ctx = ToolContext::new(root.path().canonicalize().unwrap())
            .with_call_id("remote-error".into()).with_receipt_store(Some(store));
        let result = execute_remote_focused_verifier(5, &ctx, async {
            Err("parent channel unavailable".into())
        }).await;
        assert!(!result.success);
        assert!(result.output.get("source_postimages").is_none());
        assert!(result.output.get("verification_evidence").is_none());
    }

    #[tokio::test]
    async fn remote_focused_verifier_claim_precedes_send_and_uses_router_receipt() {
        use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};
        let root = tempfile::tempdir().unwrap();
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(&root.path().canonicalize().unwrap()).unwrap());
        let ctx = ToolContext::new(root.path().canonicalize().unwrap())
            .with_call_id("remote-verifier".into()).with_receipt_store(Some(store));
        let sent = AtomicUsize::new(0);
        let first = execute_remote_focused_verifier(5, &ctx, async {
            sent.fetch_add(1, Ordering::SeqCst);
            Ok(VerifierObservation { success:false, exit_code:1, stdout:"test failed".into(),
                stderr:String::new(), process_reaped:true,process_group_empty:true,outcome:"known".into(),
                source_postimages:None, verification_evidence:None })
        }).await;
        assert!(!first.success);
        assert_eq!(first.output["terminal_receipt"]["call_id"], "remote-verifier");
        assert_eq!(first.output["terminal_receipt"]["terminal_state"], "failed");
        assert_eq!(first.output["terminal_receipt"]["termination_proven"], true);
        let duplicate = execute_remote_focused_verifier(5, &ctx, async {
            sent.fetch_add(1, Ordering::SeqCst);
            Err("must not send".into())
        }).await;
        assert!(!duplicate.success);
        assert_eq!(sent.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn remote_focused_verifier_panic_cannot_invent_cleanup_for_null_pid() {
        use std::sync::Arc;
        let root = tempfile::tempdir().unwrap();
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(&root.path().canonicalize().unwrap()).unwrap());
        let ids = vec!["remote-panic".to_string()];
        execution::begin_command_run_batch(&store, "remote-batch", &ids).unwrap();
        execution::mark_command_run_batch_call_accepted(&store, "remote-batch", &ids[0]).unwrap();
        let ctx = ToolContext::new(root.path().canonicalize().unwrap())
            .with_call_id(ids[0].clone()).with_receipt_store(Some(Arc::clone(&store)));
        execution::claim_parent_verifier_execution(&ctx, 5).unwrap();
        let result = execution::terminalize_interrupted_command_run_claims(&store,"remote-batch", &ids).await;
        assert!(result.unwrap_err().contains("PARENT_VERIFIER_CLEANUP_UNPROVEN"));
        assert!(!store.list_names().unwrap().iter().any(|name| name.ends_with(".receipt.json")));
    }

    #[test]
    fn cancelled_verifier_does_not_spawn() {
        let root = tempfile::tempdir().unwrap();
        let ctx = ToolContext::new(root.path().canonicalize().unwrap())
            .with_call_id("cancelled-verifier".into());
        ctx.cancellation.cancel();
        let result = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(execute_focused_verifier(
                &["/bin/cp".into(), "unadmitted".into()],
                root.path(),
                root.path(),
                &[],
                1,
                &ctx,
            ));
        assert!(!result.success);
        assert!(
            result
                .stderr
                .contains("VERIFIER_CANCELLED_BEFORE_EXECUTION")
        );
        assert_eq!(
            result.output["terminal_receipt"]["terminal_state"],
            "not_started"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn inherited_full_core_fence_is_detected_before_verifier_execution() {
        if std::env::var_os("NOKIY_TEST_OUTER_FENCE").is_some() {
            assert!(inherited_seatbelt_active());
            assert!(!focused_verifier_sandbox_available());
            return;
        }
        if inherited_seatbelt_active() {
            return;
        }
        let result = std::process::Command::new("/usr/bin/sandbox-exec")
            .args([
                "-p",
                "(version 1) (allow default) (deny signal) (allow signal (target same-sandbox))",
            ])
            .arg(std::env::current_exe().unwrap())
            .args([
                "inherited_full_core_fence_is_detected_before_verifier_execution",
                "--nocapture",
            ])
            .env("NOKIY_TEST_OUTER_FENCE", "1")
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
    }

    #[test]
    fn policy_requires_disjoint_canonical_scopes() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        let workspace = root.join("workspace");
        let scratch = root.join("scratch");
        std::fs::create_dir(&workspace).unwrap();
        std::fs::create_dir(&scratch).unwrap();
        std::fs::write(workspace.join("allowed.txt"), "yes").unwrap();
        let executable = std::env::current_exe().unwrap().canonicalize().unwrap();
        let profile = policy(&executable, &workspace, &scratch, &["allowed.txt".into()]).unwrap();
        assert!(profile.contains("(deny network*)"));
        assert!(!profile.contains("(subpath \"/usr\")"));
        assert!(policy(&executable, &workspace, &workspace, &["allowed.txt".into()]).is_err());
        assert!(policy(&executable, &workspace, &scratch, &["../outside".into()]).is_err());
        assert!(policy(&executable, &workspace, &scratch, &["missing".into()]).is_err());
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(workspace.join("allowed.txt"), workspace.join("link"))
                .unwrap();
            assert!(policy(&executable, &workspace, &scratch, &["link".into()]).is_err());
        }
    }

    #[test]
    fn policy_grants_directory_only_ancestors_for_deep_separate_roots() {
        let workspace_tree = tempfile::tempdir().unwrap();
        let scratch_tree = tempfile::tempdir().unwrap();
        let workspace = workspace_tree.path().canonicalize().unwrap()
            .join("workspace-parent/nested/workspace");
        let scratch = scratch_tree.path().canonicalize().unwrap()
            .join("scratch-parent/nested/scratch");
        for path in [&workspace, &scratch] {
            std::fs::create_dir_all(path).unwrap();
        }
        let allowed = workspace.join("allowed.txt");
        let outside = workspace.parent().unwrap().join("outside.txt");
        std::fs::write(&allowed, "yes").unwrap();
        std::fs::write(&outside, "no").unwrap();
        let executable = std::env::current_exe().unwrap().canonicalize().unwrap();
        let profile = policy(&executable, &workspace, &scratch, &["allowed.txt".into()]).unwrap();
        let reads = profile.lines().find(|line| line.starts_with("(allow file-read* ")).unwrap();
        for directory in workspace.ancestors().chain(scratch.parent().unwrap().ancestors()) {
            let quoted = serde_json::to_string(directory.to_str().unwrap()).unwrap();
            assert!(reads.contains(&format!(
                "(require-all (literal {quoted}) (vnode-type DIRECTORY))"
            )), "missing directory traversal: {}", directory.display());
            assert!(!reads.contains(&rule("subpath", directory).unwrap()),
                "ancestor subtree admitted: {}", directory.display());
        }
        assert!(reads.contains(&rule("literal", &allowed).unwrap()));
        assert!(reads.contains(&rule("subpath", &scratch).unwrap()));
        for path in [workspace.join("private.txt"), outside.clone(),
            scratch.parent().unwrap().join("outside.txt")] {
            assert!(!reads.contains(&rule("literal", &path).unwrap()));
        }
        assert_eq!(
            profile.lines().find(|line| line.starts_with("(allow file-write* ")).unwrap(),
            format!("(allow file-write* {} (literal \"/dev/null\"))", rule("subpath", &scratch).unwrap())
        );
        assert!(profile.contains("(deny default)"));
        assert!(profile.contains("(deny network*)"));
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&outside, workspace.join("escape-file")).unwrap();
            std::os::unix::fs::symlink(workspace.parent().unwrap(), workspace.join("escape-dir")).unwrap();
            for scope in ["escape-file", "escape-dir/outside.txt", "escape-dir/**"] {
                assert!(policy(&executable, &workspace, &scratch, &[scope.into()]).is_err());
            }
        }
    }

    #[cfg(target_os = "macos")]
    #[tokio::test]
    #[ignore = "requires NOKIY_VERIFIER_TEST_PYTHON pointing to a canonical standalone CPython"]
    async fn real_policy_confines_reads_writes_network_and_child_exec() {
        let workspace_tree = tempfile::tempdir().unwrap();
        let scratch_tree = tempfile::tempdir().unwrap();
        let workspace_root = workspace_tree.path().canonicalize().unwrap();
        let scratch_root = scratch_tree.path().canonicalize().unwrap();
        let workspace = workspace_root.join("workspace-parent/nested/workspace");
        let scratch = scratch_root.join("scratch-parent/nested/scratch");
        let receipts = workspace_root.join("receipts");
        for path in [&workspace, &scratch, &receipts] {
            std::fs::create_dir_all(path).unwrap();
        }
        std::fs::write(workspace.join("allowed.txt"), "yes").unwrap();
        std::fs::write(workspace.join("private.txt"), "no").unwrap();
        let mut forbidden_files = Vec::new();
        for (scope, root) in [(&workspace, &workspace_root), (&scratch, &scratch_root)] {
            for directory in scope.parent().unwrap().ancestors().take_while(|path| path.starts_with(root)) {
                let outside = directory.join("outside.txt");
                std::fs::write(&outside, "no").unwrap();
                std::fs::create_dir(directory.join("sibling")).unwrap();
                std::fs::write(directory.join("sibling/private.txt"), "no").unwrap();
                forbidden_files.push(outside);
            }
            std::os::unix::fs::symlink(scope.parent().unwrap().join("outside.txt"), scope.join("escape-file")).unwrap();
            std::os::unix::fs::symlink(scope.parent().unwrap(), scope.join("escape-dir")).unwrap();
        }
        let script = workspace.join("verify.py");
        std::fs::write(
            &script,
            r#"import contextlib, errno, os, pathlib, socket, subprocess
w = pathlib.Path.cwd()
s = pathlib.Path(os.environ['TMPDIR'])
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
@contextlib.contextmanager
def directory(path):
    descriptor = os.open('/', directory_flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)
def denied_read(path):
    try: path.read_text()
    except PermissionError: pass
    else: raise AssertionError('ungranted read succeeded: ' + str(path))
def denied_write(path):
    try: path.write_text('bad')
    except PermissionError: pass
    else: raise AssertionError('ungranted write succeeded: ' + str(path))
assert (w/'allowed.txt').read_text() == 'yes'
(s/'ok.txt').write_text('yes')
with directory(w) as workspace_fd, directory(s) as scratch_fd:
    with os.fdopen(os.open('allowed.txt', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=workspace_fd)) as stream:
        assert stream.read() == 'yes'
    with os.fdopen(os.open('descriptor.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=scratch_fd), 'w') as stream:
        stream.write('yes')
    for descriptor in [workspace_fd, scratch_fd]:
        try: opened = os.open('../outside.txt', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        except PermissionError: pass
        else:
            os.close(opened)
            raise AssertionError('descriptor-relative sibling read succeeded')
denied_read(w/'private.txt')
denied_write(w/'bad.txt')
for scope in [w, s]:
    for ancestor in list(scope.parents)[:3]:
        denied_read(ancestor/'outside.txt')
        denied_read(ancestor/'sibling'/'private.txt')
        denied_write(ancestor/'bad.txt')
    for path in [scope/'escape-file', scope/'escape-dir'/'outside.txt']:
        denied_read(path)
        denied_write(path)
    try:
        with directory(scope/'escape-dir'): pass
    except OSError as error:
        assert error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM), error
    else: raise AssertionError('no-follow directory walk accepted escaping symlink')
try: socket.socket().connect(('127.0.0.1', 9))
except PermissionError: pass
else: raise AssertionError('network not denied by policy')
try: subprocess.run(['/usr/bin/true'], check=True)
except PermissionError: pass
else: raise AssertionError('ungranted executable succeeded')
print('VERIFIER_OS_POLICY_PASS')
"#,
        )
        .unwrap();
        let python =
            std::env::var("NOKIY_VERIFIER_TEST_PYTHON").expect("explicit test interpreter");
        let ctx = ToolContext::new(receipts).with_call_id("verifier-os-policy".to_string());
        let result = execute_focused_verifier(
            &[
                python,
                "-I".into(),
                "-B".into(),
                script.display().to_string(),
            ],
            &scratch,
            &workspace,
            &["verify.py".into(), "allowed.txt".into()],
            15,
            &ctx,
        )
        .await;
        assert!(result.success, "{:?}", result);
        assert_eq!(result.exit_code, 0);
        assert!(result.stdout.contains("VERIFIER_OS_POLICY_PASS"));
        assert_eq!(
            std::fs::read_to_string(scratch.join("ok.txt")).unwrap(),
            "yes"
        );
        assert_eq!(std::fs::read_to_string(scratch.join("descriptor.txt")).unwrap(), "yes");
        assert!(!workspace.join("bad.txt").exists());
        for path in forbidden_files {
            assert_eq!(std::fs::read_to_string(&path).unwrap(), "no");
            assert!(!path.with_file_name("bad.txt").exists());
        }
    }
}
