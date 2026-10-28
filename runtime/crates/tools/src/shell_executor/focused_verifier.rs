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
    /// Parent-reported diagnostic references, not source evidence or execution authority.
    #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "deserialize_verifier_output")]
    pub verifier_output: Option<VerifierOutput>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(try_from = "String")]
enum VerifierOutputSchema {
    #[serde(rename = "nokiy_verifier_output_v1")]
    V1,
}

impl TryFrom<String> for VerifierOutputSchema {
    type Error = &'static str;

    fn try_from(schema: String) -> Result<Self, Self::Error> {
        match schema.as_str() {
            "nokiy_verifier_output_v1" => Ok(Self::V1),
            _ => Err("VERIFIER_OUTPUT_SCHEMA_INVALID"),
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifierOutput {
    schema_version: VerifierOutputSchema,
    request_id: String,
    request_sha256: String,
    authorization_semantic_sha256: String,
    verifier_index: usize,
    verifier_sha256: String,
    call_id: String,
    streams: VerifierOutputStreams,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct VerifierOutputStreams {
    stdout: VerifierOutputReference,
    stderr: VerifierOutputReference,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct VerifierOutputReference {
    #[serde(deserialize_with = "required_nullable")]
    path: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    sha256: Option<String>,
    bytes: u64,
    durable: bool,
    complete: bool,
    #[serde(deserialize_with = "required_nullable")]
    error: Option<String>,
}

fn required_nullable<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

fn verifier_output_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

impl VerifierOutputReference {
    fn valid(&self) -> bool {
        // Check the wire claims only. No path is opened or promoted to source proof.
        let reference_valid = if self.durable {
            self.path.as_deref().is_some_and(|path| {
                Path::new(path).is_absolute()
                    && !path.contains('\0')
                    && !path.split('/').skip(1).any(|part| matches!(part, "" | "." | ".."))
            }) && self.sha256.as_deref().is_some_and(verifier_output_sha256)
        } else {
            self.path.is_none() && self.sha256.is_none() && self.bytes == 0
        };
        reference_valid
            && self.complete == (self.durable && self.error.is_none())
            && (self.complete || self.error.as_deref().is_some_and(|error| !error.is_empty()))
    }
}

impl VerifierOutput {
    fn validate(&self) -> Result<(), String> {
        if self.request_id.is_empty() || self.call_id.is_empty()
            || !verifier_output_sha256(&self.request_sha256)
            || !verifier_output_sha256(&self.authorization_semantic_sha256)
            || !verifier_output_sha256(&self.verifier_sha256)
            || !self.streams.stdout.valid() || !self.streams.stderr.valid()
        {
            return Err("VERIFIER_OUTPUT_INVALID".into());
        }
        Ok(())
    }
}

fn deserialize_verifier_output<'de, D>(deserializer: D) -> Result<Option<VerifierOutput>, D::Error>
where D: serde::Deserializer<'de> {
    let output = Option::<VerifierOutput>::deserialize(deserializer)?;
    if let Some(output) = &output {
        output.validate().map_err(serde::de::Error::custom)?;
    }
    Ok(output)
}

impl VerifierObservation {
    /// Bind diagnostic claims to the selected grant without changing raw workload facts.
    pub fn validate_verifier_output_binding(
        &self, authorization: &str, index: usize, verifier_sha256: &str, call_id: &str,
    ) -> Result<(), String> {
        self.validate_verifier_output(Some(call_id))?;
        if let Some(output) = &self.verifier_output
            && (output.authorization_semantic_sha256 != authorization
                || output.verifier_index != index || output.verifier_sha256 != verifier_sha256)
        {
            return Err("VERIFIER_OUTPUT_BINDING_MISMATCH".into());
        }
        Ok(())
    }

    fn validate_verifier_output(&self, call_id: Option<&str>) -> Result<(), String> {
        let Some(output) = &self.verifier_output else { return Ok(()); };
        output.validate()?;
        if Some(output.call_id.as_str()) != call_id {
            return Err("VERIFIER_OUTPUT_BINDING_MISMATCH".into());
        }
        if let Some(evidence) = &self.verification_evidence {
            if evidence.get("authorization_semantic_sha256").and_then(|value| value.as_str())
                    != Some(output.authorization_semantic_sha256.as_str())
                || evidence.get("verifier_index").and_then(|value| value.as_u64())
                    != Some(output.verifier_index as u64)
                || evidence.get("verifier_sha256").and_then(|value| value.as_str())
                    != Some(output.verifier_sha256.as_str())
                || evidence.get("call_id").and_then(|value| value.as_str()) != call_id
            {
                return Err("VERIFIER_OUTPUT_BINDING_MISMATCH".into());
            }
        }
        Ok(())
    }
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
    let observed = request.await.and_then(|value| {
        value.validate_verifier_output(ctx.current_call_id())?;
        Ok(value)
    });
    let (mut response, reaped, empty, known) = match observed {
        Ok(value) => {
            let known = value.outcome == "known" && value.process_reaped && value.process_group_empty;
            let success = value.success && value.exit_code == 0 && known;
            let mut output = json!({"executor":"parent_focused_verifier"});
            // Failed and unsettled workloads also need their diagnostic references.
            if let Some(verifier_output) = value.verifier_output {
                output["verifier_output"] = json!(verifier_output);
            }
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

/// One parent-executed read-only shell line (J-Space `parent_shell`). Claimed before the request
/// crosses the parent channel; the receipt is written here from the parent's observation with
/// origin "parent_shell", so completion can tell it from an in-engine shell. Lost or unsettled
/// replies stay unknown.
pub async fn execute_remote_parent_shell<F>(
    timeout_seconds: u64, ctx: &ToolContext, request: F,
) -> CommandResponse
where F: std::future::Future<Output = Result<VerifierObservation, String>> {
    let started = Instant::now();
    if ctx.cancellation.is_cancelled() {
        return focused_verifier_not_started(ctx, "PARENT_SHELL_CANCELLED_BEFORE_EXECUTION", timeout_seconds);
    }
    let admitted = ctx.bound_receipt_store().map_err(|error| error.to_string())
        .and_then(|store| execution::reconcile_command_execution_claims(&store, ctx.current_call_id()))
        .and_then(|_| execution::claim_parent_verifier_execution(ctx, timeout_seconds));
    if let Err(error) = admitted {
        return response::failed_async_response(&format!("PARENT_SHELL_CLAIM_FAILED:{error}"), -1);
    }
    let (mut response, reaped, empty, known) = match request.await {
        Ok(value) => {
            let known = value.outcome == "known" && value.process_reaped && value.process_group_empty;
            let wall = started.elapsed().as_secs_f32();
            let mut text = format!(
                "Exit code: {}\nWall time: {wall:.1} seconds\nOutput:\n{}", value.exit_code, value.stdout
            );
            if !value.stderr.is_empty() {
                text.push_str("\nStderr:\n");
                text.push_str(&value.stderr);
            }
            (CommandResponse {
                success: known && value.exit_code == 0,
                exit_code: value.exit_code, stdout: value.stdout, stderr: value.stderr,
                output: serde_json::Value::String(text), changes: Vec::new(),
            }, value.process_reaped, value.process_group_empty, known)
        }
        Err(error) => (response::failed_async_response(&error, -1), false, false, false),
    };
    let (terminal, failure_class, outcome) = if !known {
        ("terminated", "parent_channel_unsettled", "unknown")
    } else if response.exit_code == 0 {
        ("completed", "none", "known")
    } else {
        ("failed", "workload_exit_nonzero", "known")
    };
    if let Err(error) = execution::attach_durable_terminal_receipt(
        &mut response, ctx, None, started, timeout_seconds, None, terminal, failure_class,
        "parent_shell", outcome, reaped, empty,
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
pub(super) fn inherited_seatbelt_active() -> bool {
    #[link(name = "sandbox")]
    unsafe extern "C" {
        fn sandbox_check(pid: i32, operation: *const std::ffi::c_char, flags: i32, ...) -> i32;
    }
    // A null operation queries membership. An unknown result is not permission
    // to attempt nested sandbox_init, which Darwin rejects for inherited scopes.
    unsafe { sandbox_check(std::process::id() as i32, std::ptr::null(), 0) != 0 }
}

#[cfg(not(target_os = "macos"))]
pub(super) fn inherited_seatbelt_active() -> bool {
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

/// Credential stores a test never needs. Reads are otherwise global, so these are the only data
/// a verifier cannot surface to the model through its output. Entries overlapping the workspace
/// or scratch root are skipped so a verifier never loses access to its own task.
const CREDENTIAL_SUBPATHS: [&str; 6] = [".ssh", ".gnupg", ".aws", ".config/gh", ".docker", "Library/Keychains"];
const CREDENTIAL_FILES: [&str; 2] = [".netrc", ".codex/auth.json"];

fn credential_read_denials(home: Option<&Path>, workspace: &Path, scratch: &Path) -> Result<Vec<String>, String> {
    let Some(home) = home.filter(|home| home.is_absolute()) else {
        return Ok(Vec::new());
    };
    let entries = CREDENTIAL_SUBPATHS.iter().map(|name| ("subpath", home.join(name)))
        .chain(CREDENTIAL_FILES.iter().map(|name| ("literal", home.join(name))));
    let mut denials = Vec::new();
    for (kind, path) in entries {
        let overlaps = [workspace, scratch].iter().any(|root| root.starts_with(&path) || path.starts_with(root));
        if !overlaps {
            denials.push(rule(kind, &path)?);
        }
    }
    Ok(denials)
}

/// TCP ports that were already listening when the verifier policy was built: the switchboard,
/// broker gateways, dashboards. lsof lists this user's listeners from any parent process, while
/// netstat's protocol table comes back empty under some parents (Homebrew/uv Python, as for the
/// caller's parent verifier) yet also shows other users' listeners, so the union is used. An
/// unparsable listing or an empty union is None, which keeps the deny-all network policy.
fn listening_tcp_ports() -> Option<Vec<u16>> {
    let mut ports = std::collections::BTreeSet::new();
    if let Ok(output) = std::process::Command::new("/usr/sbin/lsof")
        .args(["-nP", "-iTCP", "-sTCP:LISTEN", "-Fn"]).output()
    {
        ports.extend(parse_lsof_ports(&String::from_utf8_lossy(&output.stdout))?);
    }
    if let Ok(output) = std::process::Command::new("/usr/sbin/netstat").args(["-anv", "-p", "tcp"]).output()
        && output.status.success()
    {
        ports.extend(parse_listening_ports(&String::from_utf8_lossy(&output.stdout))?);
    }
    (!ports.is_empty()).then(|| ports.into_iter().collect())
}

fn parse_lsof_ports(text: &str) -> Option<Vec<u16>> {
    let mut ports = std::collections::BTreeSet::new();
    for name in text.lines().filter_map(|line| line.strip_prefix('n')) {
        ports.insert(name.rsplit(':').next()?.parse::<u16>().ok()?);
    }
    Some(ports.into_iter().collect())
}

fn parse_listening_ports(text: &str) -> Option<Vec<u16>> {
    let mut ports = std::collections::BTreeSet::new();
    for line in text.lines() {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() >= 6 && fields[0].starts_with("tcp") && fields[5] == "LISTEN" {
            // A listener the parser cannot read could be a sensitive service: fail closed.
            ports.insert(fields[3].rsplit('.').next()?.parse::<u16>().ok()?);
        }
    }
    Some(ports.into_iter().collect())
}

fn network_rules(listening: Option<&[u16]>) -> String {
    let Some(ports) = listening else {
        return "(deny network*)\n".into();
    };
    // Loopback for servers the tests start themselves; every service that was already listening
    // stays unreachable, and nothing leaves the host.
    let mut rules = String::from(
        "(deny network*)\n(allow network-bind (local ip \"localhost:*\"))\n\
         (allow network-inbound (local ip \"localhost:*\"))\n(allow network-outbound (remote ip \"localhost:*\"))\n",
    );
    if !ports.is_empty() {
        let denied: Vec<String> = ports.iter().map(|port| format!("(remote ip \"localhost:{port}\")")).collect();
        // Suppress only the OS violation report for this deny. Native byte-exchange tests
        // require denied syscalls and zero accepts; verifier errors and receipts remain unchanged.
        rules.push_str(&format!("(deny network-outbound {} (with no-report))\n", denied.join(" ")));
    }
    rules
}

/// 2026-10-22 (Nokiy V312): a focused verifier is how a worker runs the tests it writes, and tests
/// load interpreters, site-packages, fixtures and receipts from wherever their task keeps them. As in
/// the inspect fence, reads are therefore global except credential stores, while writes stay
/// confined to the scratch root (plus /dev/null) and every child process inherits this profile.
/// V313: tests may serve and reach their own loopback ports (I-11: 13 of 127 existing tests bind
/// 127.0.0.1); services already listening and all non-loopback traffic stay denied.
/// `read_scopes` keeps its shape check; it no longer confines reads.
pub fn focused_verifier_policy(
    executable: &Path,
    workspace: &Path,
    scratch: &Path,
    read_scopes: &[String],
) -> Result<String, String> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    policy_with(executable, workspace, scratch, read_scopes, home.as_deref(), listening_tcp_ports().as_deref())
}

fn policy_with(
    executable: &Path,
    workspace: &Path,
    scratch: &Path,
    read_scopes: &[String],
    home: Option<&Path>,
    listening: Option<&[u16]>,
) -> Result<String, String> {
    canonical(executable, false)?;
    canonical(workspace, true)?;
    canonical(scratch, true)?;
    if scratch.starts_with(workspace) || workspace.starts_with(scratch) || read_scopes.is_empty() {
        return Err("VERIFIER_SCOPE_OVERLAP_OR_EMPTY".into());
    }
    for scope in read_scopes {
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
    }
    let denials = credential_read_denials(home, workspace, scratch)?;
    let denied = if denials.is_empty() {
        String::new()
    } else {
        format!("(deny file-read* {})\n", denials.join(" "))
    };
    Ok(format!(
        "(version 1)\n(deny default)\n(allow process-exec)\n(allow process-fork)\n\
         (allow signal (target same-sandbox))\n(allow sysctl-read)\n(allow file-read*)\n{}\
         (allow file-write* {} (literal \"/dev/null\"))\n{}",
        denied,
        rule("subpath", scratch)?,
        network_rules(listening),
    ))
}

const VERIFIER_SYSTEM_PATH: &str = "/usr/bin:/bin";

fn focused_verifier_child_path(
    argv: &[String], workspace: &Path, scratch: &Path,
) -> Result<std::ffi::OsString, String> {
    let system_path = || std::ffi::OsString::from(VERIFIER_SYSTEM_PATH);
    let Some(program) = tura_path::jspace::direct_node_verifier_program(argv) else {
        return Ok(system_path());
    };
    // The router already revalidated this exact grant, including its verifier_only pin and SHA.
    // Recognition here derives child search context; it supplies no execution or read grant.
    if program.starts_with(workspace) || program.starts_with(scratch)
        || program.components().any(|part| part.as_os_str() == ".git")
    {
        return Ok(system_path());
    }
    let executable = canonical(Path::new(&argv[0]), false)?;
    if executable.starts_with(scratch) {
        return Ok(system_path());
    }
    canonical(program, false)?;
    use std::io::Read;
    let mut header = Vec::new();
    std::fs::File::open(program).and_then(|file| file.take(256).read_to_end(&mut header))
        .map_err(|_| "VERIFIER_NODE_PROGRAM_UNAVAILABLE".to_string())?;
    if header.split(|byte| *byte == b'\n').next() != Some(b"#!/usr/bin/env node".as_slice()) {
        return Ok(system_path());
    }
    let directory = executable.parent().ok_or("VERIFIER_CHILD_PATH_INVALID")?;
    // join_paths rejects a colon in the admitted directory instead of adding another PATH entry.
    std::env::join_paths([directory, Path::new("/usr/bin"), Path::new("/bin")])
        .map_err(|_| "VERIFIER_CHILD_PATH_INVALID".to_string())
}

fn focused_verifier_environment(
    command: &mut tokio::process::Command,
    argv: &[String], workspace: &Path, scratch: &Path,
) -> Result<(), String> {
    let child_path = focused_verifier_child_path(argv, workspace, scratch)?;
    command.env_clear();
    // Preserve identity without exposing provider credentials or loader hooks.
    for key in ["HOME", "CODEX_HOME"] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    command
        .env("PATH", child_path)
        .env("TMPDIR", scratch)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONNOUSERSITE", "1")
        .env("LANG", "C.UTF-8");
    Ok(())
}

pub async fn execute_focused_verifier(
    argv: &[String],
    scratch_root: &Path,
    workspace: &Path,
    cwd: Option<&Path>,
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
        // V318: a granted cwd only moves the starting directory; the profile is unchanged.
        .current_dir(cwd.unwrap_or(workspace));
    if let Err(error) = focused_verifier_environment(&mut command, argv, workspace, scratch_root) {
        return deny(&error);
    }
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

    fn verifier_output(call_id: &str, complete: bool) -> serde_json::Value {
        let mut output = json!({
            "schema_version": "nokiy_verifier_output_v1",
            "request_id": "request-v356", "request_sha256": "a".repeat(64),
            "authorization_semantic_sha256": "c".repeat(64),
            "verifier_index": 0, "verifier_sha256": "d".repeat(64), "call_id": call_id,
            "streams": {
                "stdout": {
                    "path": format!("/parent-diagnostics/verifier-{}.stdout.bin", "e".repeat(64)),
                    "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                    "bytes": 3, "durable": true, "complete": true, "error": null,
                },
                "stderr": {
                    "path": format!("/parent-diagnostics/verifier-{}.stderr.bin", "e".repeat(64)),
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "bytes": 0, "durable": true, "complete": true, "error": null,
                },
            },
        });
        if !complete {
            output["streams"]["stdout"]["complete"] = json!(false);
            output["streams"]["stdout"]["error"] = json!("VERIFIER_OUTPUT_CAPTURE_INCOMPLETE");
            output["streams"]["stderr"] = json!({
                "path": null, "sha256": null, "bytes": 0, "durable": false,
                "complete": false, "error": "VERIFIER_OUTPUT_CREATE_FAILED",
            });
        }
        output
    }

    #[test]
    fn remote_observation_decodes_python_output_and_legacy_payload() {
        for output in [None, Some(serde_json::Value::Null),
            Some(verifier_output("remote-output", true)), Some(verifier_output("remote-output", false))]
        {
            let mut payload = observation_payload();
            if let Some(output) = output {
                payload["verifier_output"] = output;
            }
            let observation: VerifierObservation = serde_json::from_value(payload.clone()).unwrap();
            let expected = payload.get("verifier_output").filter(|value| !value.is_null());
            let encoded = serde_json::to_value(observation).unwrap();
            assert_eq!(encoded.get("verifier_output"), expected);
        }
    }

    #[test]
    fn remote_observation_rejects_malformed_python_output() {
        for (pointer, value) in [
            ("/schema_version", json!("nokiy_verifier_output_v2")),
            ("/schema_version", json!({"nokiy_verifier_output_v1": null})),
            ("/request_sha256", json!("not-a-sha256")),
            ("/call_id", json!("")),
            ("/streams/stdout/bytes", json!(-1)),
            ("/streams/stdout/complete", json!("true")),
            ("/streams/stdout/sha256", json!(null)),
            ("/streams/stdout/path", json!("relative.bin")),
            ("/streams/stdout/durable", json!(false)),
            ("/streams/stdout/error", json!("VERIFIER_OUTPUT_WRITE_FAILED")),
            ("/streams", json!({"stdout": {}, "stderr": {}, "extra": {}})),
        ] {
            let mut output = verifier_output("remote-output", true);
            *output.pointer_mut(pointer).unwrap() = value;
            let mut payload = observation_payload();
            payload["verifier_output"] = output;
            assert!(serde_json::from_value::<VerifierObservation>(payload).is_err(), "{pointer}");
        }
        let mut payload = observation_payload();
        payload["verifier_output"] = verifier_output("remote-output", false);
        payload["verifier_output"]["streams"]["stderr"].as_object_mut().unwrap().remove("path");
        assert!(serde_json::from_value::<VerifierObservation>(payload).is_err());
        let mut payload = observation_payload();
        payload["verifier_output"] = verifier_output("remote-output", true);
        payload["verifier_output"]["streams"]["stdout"]["unexpected"] = json!(true);
        assert!(serde_json::from_value::<VerifierObservation>(payload).is_err());
    }

    #[tokio::test]
    async fn remote_focused_verifier_retains_complete_and_incomplete_output_in_shell_result() {
        use std::sync::Arc;
        let root = tempfile::tempdir().unwrap();
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(root.path().canonicalize().unwrap()).unwrap());
        for (call_id, complete) in [("output-complete", true), ("output-incomplete", false)] {
            let ctx = ToolContext::new(root.path().canonicalize().unwrap())
                .with_call_id(call_id.into()).with_receipt_store(Some(Arc::clone(&store)));
            let mut payload = observation_payload();
            let output = verifier_output(call_id, complete);
            let evidence = verification_evidence(call_id);
            payload["verifier_output"] = output.clone();
            payload["verification_evidence"] = evidence.clone();
            let observation: VerifierObservation = serde_json::from_value(payload.clone()).unwrap();
            let result = execute_remote_focused_verifier(5, &ctx, async { Ok(observation) }).await;
            assert!(result.success);
            assert_eq!(result.exit_code, 0);
            assert_eq!(result.stdout, payload["stdout"].as_str().unwrap());
            assert_eq!(result.stderr, payload["stderr"].as_str().unwrap());
            assert!(result.changes.is_empty());
            assert_eq!(result.output["verifier_output"], output);
            let result = response::shell_output_value(result);
            assert_eq!(result["verifier_output"], output);
            assert_eq!(result["verification_evidence"], evidence);
            assert_eq!(result["terminal_receipt"]["call_id"], call_id);
            assert_eq!(result["terminal_receipt"]["terminal_state"], "completed");
            assert_eq!(result["terminal_receipt"]["termination_proven"], true);
        }
    }

    #[tokio::test]
    async fn remote_focused_verifier_rejects_output_call_and_evidence_conflicts() {
        use std::sync::Arc;
        let root = tempfile::tempdir().unwrap();
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(root.path().canonicalize().unwrap()).unwrap());
        for call_id in ["output-wrong-call", "output-wrong-evidence"] {
            let ctx = ToolContext::new(root.path().canonicalize().unwrap())
                .with_call_id(call_id.into()).with_receipt_store(Some(Arc::clone(&store)));
            let mut payload = observation_payload();
            payload["verifier_output"] = verifier_output(call_id, true);
            if call_id == "output-wrong-call" {
                payload["verifier_output"]["call_id"] = json!("another-call");
            } else {
                payload["verification_evidence"] = verification_evidence(call_id);
                payload["verification_evidence"]["verifier_sha256"] = json!("f".repeat(64));
            }
            let observation: VerifierObservation = serde_json::from_value(payload).unwrap();
            let result = execute_remote_focused_verifier(5, &ctx, async { Ok(observation) }).await;
            assert!(!result.success);
            assert!(result.stderr.contains("VERIFIER_OUTPUT_BINDING_MISMATCH"));
            assert!(result.output.get("verifier_output").is_none());
            assert!(result.output.get("verification_evidence").is_none());
            assert_ne!(result.output["terminal_receipt"]["terminal_state"], "completed");
        }
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
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(root.path().canonicalize().unwrap()).unwrap());
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
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(root.path().canonicalize().unwrap()).unwrap());
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
            let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(root.path().canonicalize().unwrap()).unwrap());
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
            let diagnostic_output = verifier_output(call_id, false);
            payload["verifier_output"] = diagnostic_output.clone();
            let observation: VerifierObservation =
                serde_json::from_value(payload).unwrap();
            let result = execute_remote_focused_verifier(5, &ctx, async { Ok(observation) }).await;
            assert!(!result.success);
            assert!(result.output.get("source_postimages").is_none());
            assert!(result.output.get("verification_evidence").is_none());
            assert_eq!(result.exit_code, exit_code);
            assert_eq!(response::shell_output_value(result)["verifier_output"], diagnostic_output);
        }
    }

    #[tokio::test]
    async fn remote_focused_verifier_channel_error_does_not_forward_context() {
        use std::sync::Arc;
        let root = tempfile::tempdir().unwrap();
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(root.path().canonicalize().unwrap()).unwrap());
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
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(root.path().canonicalize().unwrap()).unwrap());
        let ctx = ToolContext::new(root.path().canonicalize().unwrap())
            .with_call_id("remote-verifier".into()).with_receipt_store(Some(store));
        let sent = AtomicUsize::new(0);
        let first = execute_remote_focused_verifier(5, &ctx, async {
            sent.fetch_add(1, Ordering::SeqCst);
            Ok(VerifierObservation { success:false, exit_code:1, stdout:"test failed".into(),
                stderr:String::new(), process_reaped:true,process_group_empty:true,outcome:"known".into(),
                source_postimages:None, verification_evidence:None, verifier_output:None })
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
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(root.path().canonicalize().unwrap()).unwrap());
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
                None,
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
    fn direct_node_child_path_is_isolated_from_ambient_environment() {
        use std::ffi::{OsStr, OsString};
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        let workspace = root.join("workspace");
        let scratch = root.join("scratch");
        let bin = root.join("runtime/bin");
        for directory in [&workspace, &scratch, &bin] { std::fs::create_dir_all(directory).unwrap(); }
        let node = bin.join("node");
        let program = root.join("npm-cli.js");
        std::fs::write(&node, b"pinned node").unwrap();
        std::fs::write(&program, b"#!/usr/bin/env node\nconsole.log('test');\n").unwrap();
        let argv = vec![node.to_str().unwrap().into(), program.to_str().unwrap().into(), "test".into()];
        let mut command = tokio::process::Command::new("/usr/bin/sandbox-exec");
        command.env("PATH", "/ambient/bin").env("NODE_OPTIONS", "--require=/ambient/hook.js")
            .env("NPM_CONFIG_PREFIX", "/ambient/npm").env("DYLD_INSERT_LIBRARIES", "/ambient/loader");
        focused_verifier_environment(&mut command, &argv, &workspace, &scratch).unwrap();
        let env: std::collections::BTreeMap<OsString, Option<OsString>> = command.as_std().get_envs()
            .map(|(key, value)| (key.to_os_string(), value.map(OsStr::to_os_string))).collect();
        let expected = std::env::join_paths([bin.as_path(), Path::new("/usr/bin"), Path::new("/bin")]).unwrap();
        assert_eq!(env.get(OsStr::new("PATH")).unwrap().as_deref(), Some(expected.as_os_str()));
        assert_eq!(env.get(OsStr::new("TMPDIR")).unwrap().as_deref(), Some(scratch.as_os_str()));
        assert_eq!(env.get(OsStr::new("LANG")).unwrap().as_deref(), Some(OsStr::new("C.UTF-8")));
        assert_eq!(env.get(OsStr::new("PYTHONDONTWRITEBYTECODE")).unwrap().as_deref(), Some(OsStr::new("1")));
        assert_eq!(env.get(OsStr::new("PYTHONNOUSERSITE")).unwrap().as_deref(), Some(OsStr::new("1")));
        assert!(env.keys().all(|key| ["HOME", "CODEX_HOME", "PATH", "TMPDIR", "LANG",
            "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"].iter().any(|allowed| key == OsStr::new(allowed))));
    }

    #[test]
    fn child_path_stays_system_only_outside_exact_external_node_program() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        let workspace = root.join("workspace");
        let scratch = root.join("scratch");
        std::fs::create_dir(&workspace).unwrap();
        std::fs::create_dir(&scratch).unwrap();
        let node = root.join("node");
        let program = root.join("npm-cli.js");
        std::fs::write(&node, b"pinned node").unwrap();
        std::fs::write(&program, b"#!/usr/bin/env node\n").unwrap();
        let argv = vec![node.to_str().unwrap().to_string(), program.to_str().unwrap().to_string()];
        let system_path = std::ffi::OsString::from(VERIFIER_SYSTEM_PATH);
        let mut flags = argv.clone();
        flags.insert(1, "--no-warnings".into());
        let mut duplicate = argv.clone();
        duplicate.push(argv[1].clone());
        for other in [vec!["/usr/bin/python3".into(), argv[1].clone()], flags, duplicate] {
            assert_eq!(focused_verifier_child_path(&other, &workspace, &scratch).unwrap(), system_path);
        }
        let credentials = root.join(".ssh");
        std::fs::create_dir(&credentials).unwrap();
        for entry in [workspace.join("test.js"), scratch.join("test.js"),
            credentials.join("entry.js"), root.join("entry.txt")] {
            std::fs::copy(&program, &entry).unwrap();
            let other = vec![argv[0].clone(), entry.to_str().unwrap().into()];
            assert_eq!(focused_verifier_child_path(&other, &workspace, &scratch).unwrap(), system_path);
        }
        for directory in [&credentials, &scratch] {
            let executable = directory.join("node");
            std::fs::copy(&node, &executable).unwrap();
            let other = vec![executable.to_str().unwrap().into(), argv[1].clone()];
            assert_eq!(focused_verifier_child_path(&other, &workspace, &scratch).unwrap(), system_path);
        }
        std::fs::write(&program, b"#!/usr/bin/env -S node\n").unwrap();
        assert_eq!(focused_verifier_child_path(&argv, &workspace, &scratch).unwrap(), system_path);
        std::fs::write(&program, b"#!/usr/bin/env node\n").unwrap();
        let colon_bin = root.join("runtime:injected");
        std::fs::create_dir(&colon_bin).unwrap();
        let colon_node = colon_bin.join("node");
        std::fs::copy(&node, &colon_node).unwrap();
        let other = vec![colon_node.to_str().unwrap().into(), argv[1].clone()];
        assert_eq!(focused_verifier_child_path(&other, &workspace, &scratch).unwrap_err(), "VERIFIER_CHILD_PATH_INVALID");
        std::os::unix::fs::symlink(&root, root.join("linked")).unwrap();
        let other = vec![root.join("linked/node").to_str().unwrap().into(), argv[1].clone()];
        assert!(focused_verifier_child_path(&other, &workspace, &scratch).is_err());
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
        assert!(profile.contains("(deny default)"));
        assert!(profile.lines().any(|line| line == "(allow file-read*)"));
        assert_eq!(
            profile.lines().find(|line| line.starts_with("(allow file-write* ")).unwrap(),
            format!("(allow file-write* {} (literal \"/dev/null\"))", rule("subpath", &scratch).unwrap())
        );
        assert!(policy(&executable, &workspace, &workspace, &["allowed.txt".into()]).is_err());
        assert!(policy(&executable, &workspace, &scratch, &[]).is_err());
        assert!(policy(&executable, &workspace, &scratch, &["../outside".into()]).is_err());
        assert!(policy(&executable, &workspace, &scratch, &["/absolute".into()]).is_err());
        assert!(policy(&executable, &workspace, &scratch, &["glob/*.py".into()]).is_err());
        // Scopes keep their shape check but no longer confine reads, so a scope the worker has
        // not created yet or a link inside the workspace does not block the verifier.
        policy(&executable, &workspace, &scratch, &["missing".into()]).unwrap();
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(workspace.join("allowed.txt"), workspace.join("link"))
                .unwrap();
            policy(&executable, &workspace, &scratch, &["link".into()]).unwrap();
            std::os::unix::fs::symlink(&workspace, root.join("workspace-link")).unwrap();
            assert!(policy(&executable, &root.join("workspace-link"), &scratch, &["allowed.txt".into()]).is_err());
        }
    }

    #[test]
    fn credential_denials_cover_known_stores_but_never_the_task_roots() {
        let home = Path::new("/Users/example");
        let workspace = Path::new("/Volumes/work/repo");
        let scratch = Path::new("/Volumes/work/scratch");
        let denials = credential_read_denials(Some(home), workspace, scratch).unwrap();
        for (kind, name) in [("subpath", ".ssh"), ("subpath", ".gnupg"), ("subpath", ".aws"),
            ("subpath", ".config/gh"), ("subpath", ".docker"), ("subpath", "Library/Keychains"),
            ("literal", ".netrc"), ("literal", ".codex/auth.json")] {
            assert!(denials.contains(&rule(kind, &home.join(name)).unwrap()), "{name}");
        }
        assert_eq!(denials.len(), 8);
        // A task rooted inside (or above) a credential store keeps access to itself.
        let inside = credential_read_denials(Some(home), &home.join(".docker/project"), scratch).unwrap();
        assert!(!inside.contains(&rule("subpath", &home.join(".docker")).unwrap()));
        assert_eq!(inside.len(), 7);
        let above = credential_read_denials(Some(home), workspace, &home.join("Library")).unwrap();
        assert!(!above.contains(&rule("subpath", &home.join("Library/Keychains")).unwrap()));
        assert!(credential_read_denials(None, workspace, scratch).unwrap().is_empty());
        assert!(credential_read_denials(Some(Path::new("relative")), workspace, scratch).unwrap().is_empty());
    }

    #[test]
    fn listening_ports_parse_every_tcp_listener_and_fail_closed_on_unreadable_ones() {
        let sample = "Active Internet connections (including servers)\n\
            Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)\n\
            tcp4       0      0  127.0.0.1.43130        *.*                    LISTEN   0 0 262144 262144\n\
            tcp46      0      0  *.4002                 *.*                    LISTEN   0 0 131072 131072\n\
            tcp6       0      0  ::1.8080               *.*                    LISTEN   0 0 131072 131072\n\
            tcp4       0      0  127.0.0.1.53304        127.0.0.1.43130        ESTABLISHED 0 0\n\
            udp4       0      0  *.5353                 *.*                             0 0\n";
        assert_eq!(parse_listening_ports(sample), Some(vec![4002, 8080, 43130]));
        assert_eq!(parse_listening_ports("tcp4 0 0 127.0.0.1.http *.* LISTEN\n"), None);
        assert_eq!(parse_listening_ports(""), Some(vec![]));
        let lsof = "p501\nf7\nn127.0.0.1:43130\nf9\nn*:4002\np777\nf3\nn[::1]:8080\n";
        assert_eq!(parse_lsof_ports(lsof), Some(vec![4002, 8080, 43130]));
        assert_eq!(parse_lsof_ports("p1\nf2\nnlocalhost:http\n"), None);
        assert_eq!(parse_lsof_ports(""), Some(vec![]));
    }

    #[test]
    fn network_rules_open_loopback_only_around_existing_listeners() {
        assert_eq!(network_rules(None), "(deny network*)\n");
        let open = network_rules(Some(&[]));
        assert!(open.starts_with("(deny network*)\n"));
        for rule in ["(allow network-bind (local ip \"localhost:*\"))", "(allow network-inbound (local ip \"localhost:*\"))",
            "(allow network-outbound (remote ip \"localhost:*\"))"] {
            assert!(open.contains(rule), "{rule}");
        }
        assert!(!open.contains("(deny network-outbound"));
        let guarded = network_rules(Some(&[4002, 43130]));
        assert!(guarded.ends_with(
            "(deny network-outbound (remote ip \"localhost:4002\") (remote ip \"localhost:43130\") (with no-report))\n"));
        assert_eq!(guarded.matches("(with no-report)").count(), 1);
        assert!(guarded.starts_with(&open));
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        let (workspace, scratch) = (root.join("workspace"), root.join("scratch"));
        std::fs::create_dir(&workspace).unwrap();
        std::fs::create_dir(&scratch).unwrap();
        let executable = std::env::current_exe().unwrap().canonicalize().unwrap();
        let profile = policy_with(&executable, &workspace, &scratch, &["a.py".into()], None, Some(&[4002])).unwrap();
        // Keep the existing-port deny after the loopback grants; only its OS report is suppressed.
        assert!(profile.find("(allow network-outbound").unwrap() < profile.find("(deny network-outbound").unwrap());
        assert!(policy_with(&executable, &workspace, &scratch, &["a.py".into()], None, None).unwrap()
            .ends_with("(deny network*)\n"));
    }

    #[cfg(target_os = "macos")]
    #[tokio::test]
    #[ignore = "requires NOKIY_VERIFIER_TEST_PYTHON pointing to a canonical standalone CPython"]
    async fn existing_loopback_services_deny_bytes_but_new_servers_work() {
        use std::io::Read;
        let listeners = [
            std::net::TcpListener::bind("127.0.0.1:0").unwrap(),
            std::net::TcpListener::bind("[::1]:0").unwrap(),
        ];
        let targets = listeners.iter().map(|listener| {
            listener.set_nonblocking(true).unwrap();
            let address = listener.local_addr().unwrap();
            json!([if address.is_ipv4() { "AF_INET" } else { "AF_INET6" },
                   address.ip().to_string(), address.port()])
        }).collect::<Vec<_>>();
        let tree = tempfile::tempdir().unwrap();
        let root = tree.path().canonicalize().unwrap();
        let (workspace, scratch, receipts) = (root.join("workspace"), root.join("scratch"), root.join("receipts"));
        for path in [&workspace, &scratch, &receipts] {
            std::fs::create_dir(path).unwrap();
        }
        let script = workspace.join("verify.py");
        std::fs::write(&script, format!(r#"import socket, threading
for family_name, host, port in {targets}:
    family = getattr(socket, family_name)
    with socket.socket(family) as protected:
        protected.settimeout(1)
        try:
            protected.connect((host, port))
            protected.sendall(b'protected-canary')
        except PermissionError as error:
            assert error.errno in (1, 13)
            print('PROTECTED_DENIED', flush=True)
        else:
            raise AssertionError('existing service accepted bytes')
    with socket.socket(family) as own:
        own.settimeout(1)
        own.bind((host, 0))
        own.listen()
        def serve():
            connection, _ = own.accept()
            with connection:
                connection.sendall(b'own-ack')
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            with socket.socket(family) as client:
                client.settimeout(1)
                client.connect(own.getsockname())
                with client.makefile('rb') as reply:
                    assert reply.read(7) == b'own-ack'
                print('OWN_ACK', flush=True)
        finally:
            thread.join(2)
        assert not thread.is_alive()
"#, targets = serde_json::to_string(&targets).unwrap())).unwrap();
        let python = std::env::var("NOKIY_VERIFIER_TEST_PYTHON").expect("explicit test interpreter");
        // Keep the same IPv4/IPv6 listeners across a fixed number of complete sandbox runs.
        for trial in 0..4 {
            let ctx = ToolContext::new(receipts.clone()).with_call_id(format!("listener-bytes-{trial}"));
            let result = execute_focused_verifier(
                &[python.clone(), "-I".into(), "-B".into(), script.display().to_string()],
                &scratch, &workspace, None, &["verify.py".into()], 10, &ctx,
            ).await;
            for listener in &listeners {
                match listener.accept() {
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(error) => panic!("listener unavailable: {error}"),
                    Ok((mut stream, _)) => {
                        stream.set_read_timeout(Some(std::time::Duration::from_secs(1))).unwrap();
                        let mut bytes = [0; 64];
                        let count = stream.read(&mut bytes).unwrap_or(0);
                        panic!("protected listener accepted {} bytes: {:?}", count, &bytes[..count]);
                    }
                }
            }
            assert!(result.success, "{result:?}");
            assert_eq!(result.stdout.matches("PROTECTED_DENIED").count(), 2);
            assert_eq!(result.stdout.matches("OWN_ACK").count(), 2);
        }
    }

    #[cfg(target_os = "macos")]
    #[tokio::test]
    #[ignore = "requires NOKIY_VERIFIER_TEST_PYTHON pointing to a canonical standalone CPython"]
    async fn granted_cwd_moves_only_the_starting_directory() {
        // V318: tests start where their acceptance harness runs them; writes stay in scratch.
        let tree = tempfile::tempdir().unwrap();
        let root = tree.path().canonicalize().unwrap();
        let (workspace, elsewhere) = (root.join("task_a"), root.join("task_b"));
        let (scratch, receipts) = (root.join("scratch"), root.join("receipts"));
        for path in [&workspace, &elsewhere, &scratch, &receipts] {
            std::fs::create_dir_all(path).unwrap();
        }
        let script = workspace.join("verify.py");
        std::fs::write(
            &script,
            "import os, pathlib\nassert pathlib.Path.cwd().name == 'task_b', os.getcwd()\n\
             try:\n    pathlib.Path('bad.txt').write_text('x')\nexcept PermissionError:\n    print('CWD_PASS')\n",
        )
        .unwrap();
        let python =
            std::env::var("NOKIY_VERIFIER_TEST_PYTHON").expect("explicit test interpreter");
        let ctx = ToolContext::new(receipts).with_call_id("verifier-cwd".to_string());
        let result = execute_focused_verifier(
            &[python, "-I".into(), "-B".into(), script.display().to_string()],
            &scratch,
            &workspace,
            Some(&elsewhere),
            &["verify.py".into()],
            15,
            &ctx,
        )
        .await;
        assert!(result.success, "{:?}", result);
        assert!(result.stdout.contains("CWD_PASS"), "{:?}", result);
        assert!(!elsewhere.join("bad.txt").exists());
    }

    #[cfg(target_os = "macos")]
    #[tokio::test]
    #[ignore = "requires NOKIY_VERIFIER_TEST_PYTHON pointing to a canonical standalone CPython"]
    async fn real_policy_reads_globally_but_confines_writes_network_and_credentials() {
        // A service already listening before the verifier starts must stay unreachable.
        let existing = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let existing_port = existing.local_addr().unwrap().port();
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
        let mut outside_files = Vec::new();
        for (scope, root) in [(&workspace, &workspace_root), (&scratch, &scratch_root)] {
            for directory in scope.parent().unwrap().ancestors().take_while(|path| path.starts_with(root)) {
                let outside = directory.join("outside.txt");
                std::fs::write(&outside, "no").unwrap();
                std::fs::create_dir(directory.join("sibling")).unwrap();
                std::fs::write(directory.join("sibling/private.txt"), "no").unwrap();
                outside_files.push(outside);
            }
            std::os::unix::fs::symlink(scope.parent().unwrap().join("outside.txt"), scope.join("escape-file")).unwrap();
            std::os::unix::fs::symlink(scope.parent().unwrap(), scope.join("escape-dir")).unwrap();
        }
        let script = workspace.join("verify.py");
        std::fs::write(
            &script,
            r#"import os, pathlib, socket, subprocess, threading
w = pathlib.Path.cwd()
s = pathlib.Path(os.environ['TMPDIR'])
def denied_write(path):
    try: path.write_text('bad')
    except PermissionError: pass
    else: raise AssertionError('ungranted write succeeded: ' + str(path))
assert (w/'allowed.txt').read_text() == 'yes'
assert (w/'private.txt').read_text() == 'no'
(s/'ok.txt').write_text('yes')
denied_write(w/'bad.txt')
for scope in [w, s]:
    for ancestor in list(scope.parents)[:3]:
        assert (ancestor/'sibling'/'private.txt').read_text() == 'no'
        denied_write(ancestor/'bad.txt')
    assert (scope/'escape-file').read_text() == 'no'
    denied_write(scope/'escape-file')
    denied_write(scope/'escape-dir'/'outside.txt')
keys = pathlib.Path(os.environ['HOME'])/'.ssh'
try: os.listdir(keys)
except PermissionError: pass
except FileNotFoundError: assert not EXPECT_KEYS, 'credential store hidden as missing'
else: raise AssertionError('credential store readable')
own = socket.socket(); own.bind(('127.0.0.1', 0)); own.listen(1)
threading.Thread(target=lambda: own.accept()[0].sendall(b'ok'), daemon=True).start()
with socket.create_connection(own.getsockname(), timeout=5) as client:
    assert client.recv(2) == b'ok', 'own loopback server unreachable'
for target in [('127.0.0.1', EXISTING_PORT), ('192.0.2.1', 9)]:
    try: socket.create_connection(target, timeout=5).close()
    except PermissionError: pass
    else: raise AssertionError('network not denied by policy: ' + repr(target))
subprocess.run(['/usr/bin/true'], check=True)
child = subprocess.run(['/usr/bin/touch', str(w/'child.txt')], capture_output=True)
assert child.returncode != 0 and not (w/'child.txt').exists(), 'child escaped the write fence'
print('VERIFIER_OS_POLICY_PASS')
"#
            .replace("EXPECT_KEYS", if std::env::var_os("HOME").is_some_and(|home|
                Path::new(&home).join(".ssh").is_dir()) { "True" } else { "False" })
            .replace("EXISTING_PORT", &existing_port.to_string()),
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
            None,
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
        assert!(!workspace.join("bad.txt").exists());
        assert!(!workspace.join("child.txt").exists());
        for path in outside_files {
            assert_eq!(std::fs::read_to_string(&path).unwrap(), "no");
            assert!(!path.with_file_name("bad.txt").exists());
        }
    }
}
