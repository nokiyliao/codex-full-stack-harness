pub const COMMAND_NAME: &str = "apply_patch";
pub const PROMPT: &str = include_str!("prompt.md");
pub const POLICY: &str = include_str!("policy.toml");
pub const SCHEMA: &str = include_str!("schema.json");

use super::CommandResponse;
use crate::runtime::file_locks::Access;
use crate::runtime::tool::{
    FunctionToolOutput, ToolCall, ToolContext, ToolError, ToolHandler, ToolPayload,
};
use crate::shell_executor;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

mod postimage;
mod failure_context;

#[derive(Clone, Debug)]
struct PatchChange {
    kind: String,
    path: String,
    move_path: Option<String>,
    hunks: Vec<Vec<String>>,
    lines: Vec<String>,
}

#[derive(Clone, Debug)]
struct PatchError {
    kind: &'static str,
    message: String,
    failed_change: Option<Value>,
}

pub struct ApplyPatchHandler;

#[async_trait::async_trait]
impl ToolHandler for ApplyPatchHandler {
    fn tool_name(&self) -> &str {
        "apply_patch"
    }

    async fn is_mutating(&self, _call: &ToolCall, _ctx: &ToolContext) -> bool {
        true
    }

    async fn access(&self, call: &ToolCall, ctx: &ToolContext) -> Access {
        let patch_text = patch_text_from_payload(&call.payload);
        access(&patch_text, &ctx.session_dir)
    }

    async fn handle(
        &self,
        call: ToolCall,
        ctx: ToolContext,
    ) -> Result<FunctionToolOutput, ToolError> {
        let patch_text = patch_text_from_payload(&call.payload);
        let response = shell_executor::run_in_process_command_with_terminal_receipt(
            &ctx,
            "in_process_apply_patch",
            || execute_with_readback(&patch_text, &ctx.session_dir, ctx.patch_readback_paths()),
        );
        let success = response.success;
        Ok(FunctionToolOutput::from_value(
            shell_executor::json_like_output(
                response.exit_code,
                response.stdout,
                response.stderr,
                response.output,
                response.changes,
            ),
            Some(success),
        ))
    }
}

fn patch_text_from_payload(payload: &ToolPayload) -> String {
    let raw = match payload {
        ToolPayload::Freeform { input } => input.clone(),
        ToolPayload::Function { arguments } => {
            if let Some(patch) = extract_apply_patch_body_from_value(arguments) {
                patch
            } else {
                arguments
                    .get("patch")
                    .or_else(|| arguments.get("command"))
                    .or_else(|| arguments.get("command_line"))
                    .or_else(|| arguments.get("input"))
                    .or_else(|| arguments.get("payload"))
                    .and_then(Value::as_str)
                    .map(ToString::to_string)
                    .unwrap_or_else(|| arguments.as_str().unwrap_or_default().to_string())
            }
        }
    };
    normalize_apply_patch_text(&raw)
}

fn normalize_apply_patch_text(text: &str) -> String {
    extract_apply_patch_body_from_json_wrapper(text)
        .or_else(|| extract_apply_patch_body(text))
        .or_else(|| normalize_apply_patch_body_without_begin(text))
        .unwrap_or_else(|| text.to_string())
}

fn extract_apply_patch_body_from_json_wrapper(text: &str) -> Option<String> {
    let value: Value = serde_json::from_str(text.trim()).ok()?;
    extract_apply_patch_body_from_value(&value)
}

fn extract_apply_patch_body_from_value(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => extract_apply_patch_body(text),
        Value::Object(object) => [
            "patch",
            "command",
            "command_line",
            "input",
            "payload",
            "content",
        ]
        .iter()
        .find_map(|key| {
            object
                .get(*key)
                .and_then(extract_apply_patch_body_from_value)
        })
        .or_else(|| {
            object
                .values()
                .find_map(extract_apply_patch_body_from_value)
        }),
        Value::Array(items) => items.iter().find_map(extract_apply_patch_body_from_value),
        _ => None,
    }
}

pub(crate) fn patch_marker_range(text: &str, marker: &str) -> Option<std::ops::Range<usize>> {
    let mut start = 0;
    for physical in text.split_inclusive('\n') {
        let line = physical
            .strip_suffix("\r\n")
            .or_else(|| physical.strip_suffix('\n'))
            .unwrap_or(physical);
        if line == marker {
            return Some(start..start + line.len());
        }
        start += physical.len();
    }
    None
}

fn extract_apply_patch_body(text: &str) -> Option<String> {
    if let Some(begin) = patch_marker_range(text, "*** Begin Patch") {
        let patch = &text[begin.start..];
        let end = patch_marker_range(patch, "*** End Patch")?;
        return Some(patch[..end.end].to_string());
    }
    normalize_apply_patch_body_without_begin(text)
}

fn normalize_apply_patch_body_without_begin(text: &str) -> Option<String> {
    let body = strip_apply_patch_command_line(text.trim_start());
    if !starts_with_patch_hunk(body) {
        return None;
    }
    let end_marker = "*** End Patch";
    let body = if let Some(end) = patch_marker_range(body, end_marker) {
        body[..end.end].to_string()
    } else {
        let separator = if body.ends_with('\n') { "" } else { "\n" };
        format!("{body}{separator}{end_marker}")
    };
    Some(format!("*** Begin Patch\n{body}"))
}

fn strip_apply_patch_command_line(text: &str) -> &str {
    for prefix in [
        "apply_patch <<'PATCH'",
        "apply_patch <<\"PATCH\"",
        "apply_patch",
    ] {
        if let Some(rest) = text.strip_prefix(prefix) {
            return rest.trim_start_matches(['\r', '\n', ' ', '\t']);
        }
    }
    text
}

fn starts_with_patch_hunk(text: &str) -> bool {
    matches!(
        text.trim_start(),
        body if body.starts_with("*** Add File: ")
            || body.starts_with("*** Delete File: ")
            || body.starts_with("*** Update File: ")
    )
}

#[cfg(test)]
mod physical_marker_tests {
    use super::*;

    const BODY: &str = concat!(
        "*** Update File: x\n@@\n",
        "-let s = \"*** End Patch\";\n",
        "+let s = \"\\n*** Begin Patch\\n*** End Patch\";\n",
        " *** End Patch\n+*** End Patch\n-*** End Patch\n+tail\n",
    );

    fn check(input: &str, expected: &str) {
        assert_eq!(extract_apply_patch_body(input).as_deref(), Some(expected));
        let command = crate::command_run::normalize_command_value_for_execution(
            serde_json::json!({"command_type": "apply_patch", "command_line": input, "step": 1}),
            0,
        )
        .unwrap();
        assert_eq!(command["command_line"].as_str(), Some(expected));
    }

    #[test]
    fn physical_lines() {
        for nl in ["\n", "\r\n"] {
            let body = BODY.replace('\n', nl);
            for marker in ["*** Begin Patch", "*** End Patch"] {
                assert!(patch_marker_range(&body, marker).is_none());
            }
            let prefix = format!("\u{e9}{nl}{body}");
            let text = format!("{prefix}*** End Patch{nl}*** End Patch");
            assert_eq!(
                patch_marker_range(&text, "*** End Patch"),
                Some(prefix.len()..prefix.len() + "*** End Patch".len())
            );
            let patch = format!("*** Begin Patch{nl}{body}*** End Patch");
            check(
                &format!("apply_patch <<'PATCH'{nl}{patch}{nl}PATCH{nl}*** End Patch"),
                &patch,
            );
            assert_eq!(
                normalize_apply_patch_text(&serde_json::json!({"patch": &patch}).to_string()),
                patch
            );
            check(
                &format!("apply_patch\n{body}*** End Patch\nPATCH"),
                &format!("*** Begin Patch\n{body}*** End Patch"),
            );
        }
        let expected = format!("*** Begin Patch\n{BODY}*** End Patch");
        check(BODY, &expected);
        check(BODY.trim_end(), &expected);
    }

    #[test]
    fn incomplete_or_impostor_boundaries() {
        for suffix in [" suffix", " ", "\r"] {
            let patch = format!("*** Begin Patch\n{BODY}*** End Patch{suffix}");
            assert!(extract_apply_patch_body(&patch).is_none());
        }
        assert!(extract_apply_patch_body(&format!("*** Begin Patch\n{BODY}")).is_none());
        assert!(extract_apply_patch_body("*** Begin Patch suffix\n*** End Patch").is_none());
        assert!(parse_patch("*** Begin Patch\n*** Add File: x\n+x\n*** End Patch suffix").is_err());
    }

    // 2026-10-22 i8: a Luna worker sent five id-only apply_patch entries as labels next to the
    // real patches; each was reported as a malformed patch failure.
    #[test]
    fn empty_entry_changes_nothing_and_says_so() {
        let dir = tempfile::tempdir().unwrap();
        for text in ["", "  \n"] {
            let response = execute(text, dir.path());
            assert!(response.success);
            assert!(response.changes.is_empty());
            assert_eq!(response.output["no_effect"], serde_json::json!(true));
            assert!(response.output["message"].as_str().unwrap().contains("nothing changed"));
        }
        assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
        assert!(!execute("*** Begin Patch\n*** Update File: missing.txt\n@@\n-a\n+b\n*** End Patch", dir.path()).success);
    }
}

pub fn execute(patch_text: &str, session_dir: &Path) -> CommandResponse {
    execute_with_readback(patch_text, session_dir, &[])
}

fn execute_with_readback(patch_text: &str, session_dir: &Path, read_paths: &[PathBuf]) -> CommandResponse {
    if patch_text.trim().is_empty() {
        // Models label patches with id-only entries next to the real ones. Reporting those as
        // failed patches made workers resend edits that had already applied; say plainly that
        // the entry changed nothing so a genuinely omitted patch is still visible.
        return CommandResponse {
            success: true,
            exit_code: 0,
            stdout: String::new(),
            stderr: String::new(),
            output: json!({
                "no_effect": true,
                "message": "This apply_patch entry has no command_line, so nothing changed. Entries are commands, not labels: to edit files, put the complete patch from *** Begin Patch to *** End Patch in command_line.",
            }),
            changes: Vec::new(),
        };
    }
    match parse_patch(patch_text) {
        Ok(changes) => {
            let mut applied_changes = Vec::new();
            let mut failed_changes = Vec::new();
            for change in &changes {
                match apply_change(change, session_dir, read_paths) {
                    Ok(applied_change) => applied_changes.push(applied_change),
                    Err(err) => {
                        let mut failure = json!({
                            "error_type": err.kind,
                            "message": err.message,
                            "guidance": apply_patch_failure_guidance(err.kind, !applied_changes.is_empty()),
                        });
                        if let Some(failed_change) = err.failed_change {
                            failure["failed_change"] = failed_change;
                        }
                        failed_changes.push(failure);
                    }
                }
            }
            if !failed_changes.is_empty() {
                for change in &mut applied_changes {
                    if let Some(change) = change.as_object_mut() {
                        change.remove("source_delta");
                    }
                }
                let first_failure = failed_changes.first();
                let first_kind = first_failure
                    .and_then(|failure| failure["error_type"].as_str())
                    .unwrap_or("PatchFailed");
                let first_message = first_failure
                    .and_then(|failure| failure["message"].as_str())
                    .unwrap_or("apply_patch failed");
                let message = if failed_changes.len() == 1 {
                    first_message.to_string()
                } else {
                    format!(
                        "apply_patch failed for {} of {} changes; first failure: {}",
                        failed_changes.len(),
                        changes.len(),
                        first_message
                    )
                };
                let output = json!({
                    "error_type": if failed_changes.len() == 1 {
                        first_kind
                    } else {
                        "MultiplePatchFailures"
                    },
                    "failed_changes": failed_changes,
                });
                return CommandResponse {
                    success: false,
                    exit_code: 1,
                    stdout: String::new(),
                    stderr: message,
                    output,
                    changes: applied_changes,
                };
            }
            postimage::bound_batch(&mut applied_changes);
            CommandResponse {
                success: true,
                exit_code: 0,
                stdout: "Success. Updated files.".to_string(),
                stderr: String::new(),
                output: json!({}),
                changes: applied_changes,
            }
        }
        Err(err) => CommandResponse {
            success: false,
            exit_code: 1,
            stdout: String::new(),
            stderr: err.clone(),
            output: json!({
                "error_type": "ParseError",
                "message": err,
            }),
            changes: Vec::new(),
        },
    }
}

pub fn access(patch_text: &str, session_dir: &Path) -> Access {
    match parse_patch(patch_text) {
        Ok(changes) => Access {
            write_paths: changes
                .iter()
                .flat_map(|change| {
                    let mut keys = Vec::new();
                    if let Some(key) = lock_key(session_dir, &change.path) {
                        keys.push(key);
                    }
                    if let Some(move_path) = change.move_path.as_deref()
                        && let Some(key) = lock_key(session_dir, move_path)
                    {
                        keys.push(key);
                    }
                    keys
                })
                .collect(),
            ..Access::default()
        },
        Err(_) => Access {
            workspace_write: true,
            ..Access::default()
        },
    }
}

pub(crate) fn validate_paths_within_session_dir(
    patch_text: &str,
    session_dir: &Path,
) -> Result<(), String> {
    let changes = parse_patch(patch_text)?;
    for change in changes {
        validate_patch_path_within_session_dir(session_dir, &change.path)?;
        if let Some(move_path) = change.move_path {
            validate_patch_path_within_session_dir(session_dir, &move_path)?;
        }
    }
    Ok(())
}

/// Return the parsed patch paths for Router-owned J-Space admission.
///
/// The normal apply-patch parser remains the source of truth; this helper only
/// exposes its already-parsed path and operation classification to the local
/// contract matcher before mutation begins.
pub fn jspace_changes(patch_text: &str) -> Result<Vec<(String, String, Option<String>)>, String> {
    let normalized = normalize_apply_patch_text(patch_text);
    parse_patch(&normalized).map(|changes| {
        changes
            .into_iter()
            .map(|change| (change.kind, change.path, change.move_path))
            .collect()
    })
}

fn validate_patch_path_within_session_dir(session_dir: &Path, raw: &str) -> Result<(), String> {
    let root = session_dir.canonicalize().map_err(|err| {
        format!(
            "failed to resolve patch workspace {}: {err}",
            session_dir.display()
        )
    })?;
    let path = normalize_path_lexically(&safe_path(session_dir, raw)?);
    if path_is_within_root(&path, &root) {
        return Ok(());
    }
    Err(format!(
        "apply_patch path is outside workspace: {}",
        patch_path(raw).display()
    ))
}

fn path_is_within_root(path: &Path, root: &Path) -> bool {
    if path.strip_prefix(root).is_ok() {
        return true;
    }
    let path = comparable_path_string(path);
    let root = comparable_path_string(root);
    path == root || path.starts_with(&(root + "/"))
}

fn normalize_path_lexically(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::Prefix(prefix) => normalized.push(prefix.as_os_str()),
            std::path::Component::RootDir => normalized.push(component.as_os_str()),
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                normalized.pop();
            }
            std::path::Component::Normal(part) => normalized.push(part),
        }
    }
    normalized
}

fn parse_patch(patch_text: &str) -> Result<Vec<PatchChange>, String> {
    let mut changes = Vec::new();
    let mut current: Option<PatchChange> = None;
    let mut hunk: Option<Vec<String>> = None;
    let mut started = false;
    let mut ended = false;

    for (line_index, line) in patch_text.lines().enumerate() {
        let line_number = line_index + 1;
        if !started {
            if line.trim().is_empty() {
                continue;
            }
            if line == "*** Begin Patch" {
                started = true;
                continue;
            }
            return Err(format!(
                "invalid patch: expected *** Begin Patch at line {line_number}"
            ));
        }
        if let Some(path) = line.strip_prefix("*** Update File: ") {
            finish_change(&mut changes, &mut current, &mut hunk);
            current = Some(PatchChange {
                kind: "update".to_string(),
                path: path.to_string(),
                move_path: None,
                hunks: Vec::new(),
                lines: Vec::new(),
            });
        } else if let Some(path) = line.strip_prefix("*** Add File: ") {
            finish_change(&mut changes, &mut current, &mut hunk);
            current = Some(PatchChange {
                kind: "add".to_string(),
                path: path.to_string(),
                move_path: None,
                hunks: Vec::new(),
                lines: Vec::new(),
            });
        } else if let Some(path) = line.strip_prefix("*** Delete File: ") {
            finish_change(&mut changes, &mut current, &mut hunk);
            current = Some(PatchChange {
                kind: "delete".to_string(),
                path: path.to_string(),
                move_path: None,
                hunks: Vec::new(),
                lines: Vec::new(),
            });
        } else if let Some(path) = line.strip_prefix("*** Move to: ") {
            let Some(change) = current.as_mut() else {
                return Err("move target without file".to_string());
            };
            if change.kind != "update" {
                return Err("move target is only valid for update file changes".to_string());
            }
            change.move_path = Some(path.to_string());
        } else if line.starts_with("@@") {
            let Some(change) = current.as_ref() else {
                return Err("hunk without file".to_string());
            };
            if change.kind != "update" {
                return Err("hunk is only valid for update file changes".to_string());
            }
            if let Some(hunk_lines) = hunk.take() {
                let Some(change) = current.as_mut() else {
                    return Err("hunk without file".to_string());
                };
                change.hunks.push(hunk_lines);
            }
            hunk = Some(Vec::new());
        } else if line == "*** End Patch" {
            finish_change(&mut changes, &mut current, &mut hunk);
            ended = true;
            break;
        } else if line == "*** End of File" {
            continue;
        } else if let Some(change) = current.as_mut() {
            if change.kind == "add" && line.starts_with('+') {
                change.lines.push(line[1..].to_string());
            } else if let Some(hunk_lines) = hunk.as_mut() {
                if matches!(line.as_bytes().first(), Some(b' ' | b'+' | b'-')) {
                    hunk_lines.push(line.to_string());
                } else if line.trim().is_empty() {
                    hunk_lines.push(format!(" {line}"));
                } else {
                    return Err(format!(
                        "invalid patch line {line_number}: hunk lines must start with space, +, or -"
                    ));
                }
            } else if line.trim().is_empty() {
                continue;
            } else {
                return Err(format!(
                    "invalid patch line {line_number}: content must be inside a hunk"
                ));
            }
        } else if line.trim().is_empty() {
            continue;
        } else {
            return Err(format!(
                "invalid patch line {line_number}: expected file operation"
            ));
        }
    }
    if !started {
        return Err(if patch_text.trim().is_empty() {
            "invalid patch: command_line is empty; put the complete patch text, from *** Begin Patch to *** End Patch, in command_line".to_string()
        } else {
            "invalid patch: missing *** Begin Patch".to_string()
        });
    }
    if !ended {
        return Err("invalid patch: missing *** End Patch".to_string());
    }
    if changes.is_empty() {
        return Err("no file changes found in patch".to_string());
    }
    validate_changes(&changes)?;
    Ok(changes)
}

fn finish_change(
    changes: &mut Vec<PatchChange>,
    current: &mut Option<PatchChange>,
    hunk: &mut Option<Vec<String>>,
) {
    if let Some(hunk_lines) = hunk.take()
        && let Some(change) = current.as_mut()
    {
        change.hunks.push(hunk_lines);
    }
    if let Some(change) = current.take() {
        changes.push(change);
    }
}

fn validate_changes(changes: &[PatchChange]) -> Result<(), String> {
    for change in changes {
        if change.path.trim().is_empty() {
            return Err("invalid patch: file path must not be empty".to_string());
        }
        match change.kind.as_str() {
            "add" => {
                if change.move_path.is_some() {
                    return Err("invalid patch: add file cannot have move target".to_string());
                }
                if !change.hunks.is_empty() {
                    return Err("invalid patch: add file cannot contain hunks".to_string());
                }
            }
            "delete" => {
                if change.move_path.is_some() {
                    return Err("invalid patch: delete file cannot have move target".to_string());
                }
                if !change.hunks.is_empty() || !change.lines.is_empty() {
                    return Err("invalid patch: delete file cannot contain content".to_string());
                }
            }
            "update" => {
                if change.hunks.iter().any(Vec::is_empty) {
                    return Err(format!(
                        "invalid patch: update file {} contains an empty hunk",
                        change.path
                    ));
                }
            }
            other => return Err(format!("unsupported patch change kind: {other}")),
        }
    }
    Ok(())
}

fn apply_change(change: &PatchChange, session_dir: &Path, read_paths: &[PathBuf]) -> Result<Value, PatchError> {
    let path = safe_path(session_dir, &change.path).map_err(PatchError::path)?;
    let mut source_delta = None;
    // Bind identities to the operation's exact read/write buffers, never a later
    // filesystem reread. Publish them only after the entire change succeeds.
    let (preimage_sha256, postimage_sha256) = match change.kind.as_str() {
        "delete" => {
            if !path.exists() {
                return Err(PatchError::missing_file("DeleteFileNotFound", change));
            }
            let original = std::fs::read(&path).map_err(PatchError::io)?;
            std::fs::remove_file(path).map_err(PatchError::io)?;
            (Some(format!("{:x}", Sha256::digest(&original))), None)
        }
        "add" => {
            if path.exists() {
                return Err(PatchError::file_exists(change));
            }
            let mut updated = change.lines.join("\n");
            if !updated.is_empty() && !updated.ends_with('\n') {
                updated.push('\n');
            }
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).map_err(PatchError::io)?;
            }
            std::fs::write(path, updated.as_bytes()).map_err(PatchError::io)?;
            (None, Some(format!("{:x}", Sha256::digest(updated.as_bytes()))))
        }
        "update" => {
            if !path.exists() {
                return Err(PatchError::missing_file("UpdateFileNotFound", change));
            }
            let original = std::fs::read_to_string(&path).map_err(PatchError::io)?;
            let (updated, delta) = apply_hunks_with_delta(&original, &change.hunks,
                change.move_path.is_none() && read_paths.contains(&path))
                .map_err(|message| PatchError::context_mismatch(message, change))?;
            let destination = match change.move_path.as_deref() {
                Some(move_path) => safe_path(session_dir, move_path).map_err(PatchError::path)?,
                None => path.clone(),
            };
            if let Some(parent) = destination.parent() {
                std::fs::create_dir_all(parent).map_err(PatchError::io)?;
            }
            std::fs::write(&destination, updated.as_bytes()).map_err(PatchError::io)?;
            if destination != path && path.exists() {
                std::fs::remove_file(path).map_err(PatchError::io)?;
            }
            source_delta = delta;
            (
                Some(format!("{:x}", Sha256::digest(original.as_bytes()))),
                Some(format!("{:x}", Sha256::digest(updated.as_bytes()))),
            )
        }
        _ => {
            return Err(PatchError {
                kind: "ParseError",
                message: format!("unsupported patch change kind: {}", change.kind),
                failed_change: Some(patch_change_value(change)),
            });
        }
    };
    let mut applied_change = patch_change_value(change);
    applied_change["preimage_sha256"] = json!(preimage_sha256);
    applied_change["postimage_sha256"] = json!(postimage_sha256);
    if let Some(delta) = source_delta { applied_change["source_delta"] = delta; }
    Ok(applied_change)
}

fn apply_hunks_with_delta(original: &str, hunks: &[Vec<String>], readback: bool) -> Result<(String, Option<Value>), String> {
    let mut lines = original
        .lines()
        .map(ToString::to_string)
        .collect::<Vec<_>>();
    let line_ending = dominant_line_ending(original);
    let original_had_final_newline = original.ends_with('\n') || original.ends_with('\r');
    let mut replacements = Vec::new();
    let mut search_start = 0;

    for hunk in hunks {
        let old = hunk
            .iter()
            .filter(|line| line.starts_with(' ') || line.starts_with('-'))
            .map(|line| &line[1..])
            .map(ToString::to_string)
            .collect::<Vec<_>>();
        let new = hunk
            .iter()
            .filter(|line| line.starts_with(' ') || line.starts_with('+'))
            .map(|line| &line[1..])
            .map(ToString::to_string)
            .collect::<Vec<_>>();
        if old.is_empty() {
            replacements.push((lines.len(), lines.len(), new));
            search_start = lines.len();
        } else if let Some(start) = seek_sequence(&lines, &old, search_start) {
            let end = start + old.len();
            let new = replacement_lines_for_hunk(hunk, &lines[start..end]);
            replacements.push((start, end, new));
            search_start = end;
        } else {
            let mut message = format!(
                "patch context not found: {}",
                old.join("\n").chars().take(120).collect::<String>()
            );
            if readback {
                if let Some((start_line, text)) = old.first()
                    .and_then(|expected| failure_context::excerpt(original, expected))
                {
                    let evidence = json!({"kind": "diagnostic_candidate_not_patch_match",
                        "preimage_sha256": format!("{:x}", Sha256::digest(original.as_bytes())),
                        "start_line": start_line, "text": text});
                    message.push_str(&format!("\nRead-only preimage context: {evidence}"));
                }
            }
            return Err(message);
        }
    }

    for (start, end, new) in replacements.iter().rev() {
        lines.splice(*start..*end, new.iter().cloned());
    }
    let updated = join_lines(&lines, line_ending, original_had_final_newline);
    let delta = readback.then(|| postimage::from_applied_buffers(original, &updated, &replacements)).flatten();
    Ok((updated, delta))
}

fn replacement_lines_for_hunk(hunk: &[String], actual_old_lines: &[String]) -> Vec<String> {
    let mut actual_index = 0;
    let mut replacement = Vec::new();
    for line in hunk {
        if let Some(text) = line.strip_prefix(' ') {
            replacement.push(
                actual_old_lines
                    .get(actual_index)
                    .cloned()
                    .unwrap_or_else(|| text.to_string()),
            );
            actual_index += 1;
        } else if let Some(text) = line.strip_prefix('-') {
            let _ = text;
            actual_index += 1;
        } else if let Some(text) = line.strip_prefix('+') {
            replacement.push(text.to_string());
        }
    }
    replacement
}

fn safe_path(root: &Path, raw: &str) -> Result<PathBuf, String> {
    let root = root
        .canonicalize()
        .map_err(|err| format!("failed to resolve patch root {}: {err}", root.display()))?;
    let raw_path = patch_path(raw);
    let path = if raw_path.is_absolute() {
        raw_path
    } else {
        root.join(raw_path)
    };
    Ok(path.canonicalize().unwrap_or(path))
}

fn lock_key(root: &Path, raw: &str) -> Option<String> {
    let path = safe_path(root, raw).ok()?;
    let root = root.canonicalize().ok()?;
    if let Ok(relative) = path.strip_prefix(&root) {
        return Some(relative.to_string_lossy().replace('\\', "/"));
    }
    let path = comparable_path_string(&path);
    let root = comparable_path_string(&root);
    path.strip_prefix(&(root + "/"))
        .map(|suffix| suffix.to_string())
        .or_else(|| Some(format!("absolute:{path}")))
}

fn patch_path(raw: &str) -> PathBuf {
    let trimmed = raw.trim();
    #[cfg(windows)]
    {
        let normalized = trimmed.replace('\\', "/");
        let bytes = normalized.as_bytes();
        if normalized.len() > 3
            && bytes[0] == b'/'
            && bytes[1].is_ascii_alphabetic()
            && bytes[2] == b'/'
        {
            let drive = (bytes[1] as char).to_ascii_uppercase();
            return PathBuf::from(format!("{drive}:\\{}", normalized[3..].replace('/', "\\")));
        }
    }
    PathBuf::from(trimmed)
}

fn comparable_path_string(path: &Path) -> String {
    let mut text = path.to_string_lossy().replace('\\', "/");
    if let Some(stripped) = text.strip_prefix("//?/") {
        text = stripped.to_string();
    }
    #[cfg(windows)]
    {
        text = text.to_ascii_lowercase();
    }
    text.trim_end_matches('/').to_string()
}

fn patch_change_value(change: &PatchChange) -> Value {
    match change.kind.as_str() {
        "add" => json!({"kind": change.kind, "path": change.path, "lines": change.lines}),
        _ => json!({
            "kind": change.kind,
            "path": change.path,
            "move_path": change.move_path,
            "hunks": change.hunks
        }),
    }
}

impl PatchError {
    fn context_mismatch(message: String, change: &PatchChange) -> Self {
        Self {
            kind: "ContextMismatch",
            message,
            failed_change: Some(patch_change_value(change)),
        }
    }

    fn io(err: std::io::Error) -> Self {
        let kind = if err.kind() == std::io::ErrorKind::PermissionDenied {
            "PermissionDenied"
        } else {
            "IoError"
        };
        Self {
            kind,
            message: err.to_string(),
            failed_change: None,
        }
    }

    fn missing_file(kind: &'static str, change: &PatchChange) -> Self {
        Self {
            kind,
            message: format!("{}: {}", kind, change.path),
            failed_change: Some(patch_change_value(change)),
        }
    }

    fn file_exists(change: &PatchChange) -> Self {
        Self {
            kind: "AddFileExists",
            message: format!("AddFileExists: {}", change.path),
            failed_change: Some(patch_change_value(change)),
        }
    }

    fn path(message: String) -> Self {
        Self {
            kind: "IoError",
            message,
            failed_change: None,
        }
    }
}

fn apply_patch_failure_guidance(kind: &str, partial: bool) -> &'static str {
    match (kind, partial) {
        ("ContextMismatch", true) => {
            "apply_patch failed because expected context was not found after earlier changes were applied. Use any attached preimage context to correct smaller hunks; read the exact missing range only if context is insufficient or the file may have changed. The excerpt is diagnostic, not a patch match or authority to replay the whole batch. Subsequent commands run against a partially changed tree."
        }
        ("ContextMismatch", false) => {
            "apply_patch failed because expected context was not found. Use any attached preimage context to correct a smaller hunk; read the exact missing range only if context is absent, insufficient, or the file may have changed. The excerpt is diagnostic, not a patch match or authority to replay the whole batch."
        }
        (_, true) => {
            "apply_patch failed after earlier changes were applied; inspect changes before retrying."
        }
        _ => "apply_patch failed; inspect error_type and message before retrying.",
    }
}

fn dominant_line_ending(text: &str) -> &'static str {
    if text.contains("\r\n") { "\r\n" } else { "\n" }
}

fn join_lines(lines: &[String], line_ending: &str, final_newline: bool) -> String {
    let mut text = lines.join(line_ending);
    if final_newline && !text.is_empty() {
        text.push_str(line_ending);
    }
    text
}

fn seek_sequence(lines: &[String], pattern: &[String], start: usize) -> Option<usize> {
    if pattern.is_empty() {
        return Some(start.min(lines.len()));
    }
    if pattern.len() > lines.len() || start > lines.len().saturating_sub(pattern.len()) {
        return None;
    }
    let max_start = lines.len() - pattern.len();
    for index in start..=max_start {
        let candidate = &lines[index..index + pattern.len()];
        if sequence_matches(candidate, pattern, MatchMode::Exact)
            || sequence_matches(candidate, pattern, MatchMode::TrimEnd)
            || sequence_matches(candidate, pattern, MatchMode::Trim)
            || sequence_matches(candidate, pattern, MatchMode::Normalized)
        {
            return Some(index);
        }
    }
    None
}

#[derive(Clone, Copy)]
enum MatchMode {
    Exact,
    TrimEnd,
    Trim,
    Normalized,
}

fn sequence_matches(candidate: &[String], pattern: &[String], mode: MatchMode) -> bool {
    candidate
        .iter()
        .zip(pattern)
        .all(|(left, right)| match mode {
            MatchMode::Exact => left == right,
            MatchMode::TrimEnd => left.trim_end() == right.trim_end(),
            MatchMode::Trim => left.trim() == right.trim(),
            MatchMode::Normalized => normalize_match_text(left) == normalize_match_text(right),
        })
}

fn normalize_match_text(text: &str) -> String {
    text.trim()
        .chars()
        .map(|ch| match ch {
            '\u{00a0}' | '\u{2007}' | '\u{202f}' => ' ',
            '\u{2010}' | '\u{2011}' | '\u{2012}' | '\u{2013}' | '\u{2014}' | '\u{2212}' => '-',
            '\u{2018}' | '\u{2019}' | '\u{201a}' | '\u{201b}' => '\'',
            '\u{201c}' | '\u{201d}' | '\u{201e}' | '\u{201f}' => '"',
            other => other,
        })
        .collect()
}

#[cfg(test)]
mod tests;
