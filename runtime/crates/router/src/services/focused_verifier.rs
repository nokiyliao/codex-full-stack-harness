use code_tools::runtime::tool::{CancellationToken, ToolContext};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::SystemTime;
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
const SCRATCH_REPORTS_SCHEMA: &str = "nokiy_verifier_scratch_reports_v1";
// The runtime cuts any single JSON value above 10,000 characters to a middle-truncated
// string; staying under it keeps the failing tail (tracebacks, assertion text) intact.
const SCRATCH_REPORTS_CONTEXT_CHARS: usize = 9_000;
const SCRATCH_REPORTS_MAX_FILES: usize = 2;
const SCRATCH_REPORTS_MAX_DEPTH: usize = 3;
const SCRATCH_REPORTS_MAX_ENTRIES: usize = 256;
// Selected reports up to this size are parsed whole; other candidates get bounded probes.
const SCRATCH_REPORT_FULL_READ_BYTES: u64 = 4 * 1024 * 1024;
const FAILURE_KEY_HINTS: [&str; 9] = [
    "fail",
    "error",
    "traceback",
    "assert",
    "exception",
    "stderr",
    "reason",
    "blocker",
    "diagnostic",
];
const FAILURE_TEXT_HINTS: [&str; 6] = [
    "Traceback",
    "AssertionError",
    "FAIL",
    "Error:",
    "Exception",
    "assert ",
];
const FAILURE_VALUE_CHARS: usize = 1_200;
const FAILURE_JSON_MAX_NODES: usize = 4_096;

struct ScratchCandidate {
    modified: SystemTime,
    relative: String,
    path: PathBuf,
    bytes: u64,
    dev: u64,
    ino: u64,
}

/// A failed verifier often writes its explanation (report JSON, traceback, junit XML) under
/// the host-allocated scratch root, which is outside the worker's read scopes. Return bounded
/// tails of regular files this run created or modified so the worker can repair the failure
/// instead of escalating. Raw stdout/stderr stay untouched; this is review context only.
fn failed_verifier_scratch_reports(scratch_root: &Path, started: SystemTime) -> Option<Value> {
    let root = scratch_root.canonicalize().ok()?;
    let mut candidates = Vec::new();
    let mut pending = vec![(root.clone(), 0usize)];
    let mut visited = 0usize;
    while let Some((directory, depth)) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(&directory) else {
            continue;
        };
        for entry in entries.flatten() {
            visited += 1;
            if visited > SCRATCH_REPORTS_MAX_ENTRIES {
                break;
            }
            let path = entry.path();
            let Ok(meta) = std::fs::symlink_metadata(&path) else {
                continue;
            };
            let kind = meta.file_type();
            if kind.is_dir() && !kind.is_symlink() {
                if depth + 1 < SCRATCH_REPORTS_MAX_DEPTH {
                    pending.push((path, depth + 1));
                }
                continue;
            }
            if !kind.is_file() || meta.nlink() != 1 || meta.len() == 0 {
                continue;
            }
            let Ok(modified) = meta.modified() else {
                continue;
            };
            let Ok(relative) = path.strip_prefix(&root) else {
                continue;
            };
            if modified < started {
                continue;
            }
            candidates.push(ScratchCandidate {
                modified,
                relative: relative.to_string_lossy().into_owned(),
                path,
                bytes: meta.len(),
                dev: meta.dev(),
                ino: meta.ino(),
            });
        }
    }
    if candidates.is_empty() {
        return None;
    }
    let mut budget = SCRATCH_REPORTS_CONTEXT_CHARS * 3 / 4;
    let candidate_count = candidates.len();
    let mut diagnostic_read_bytes = 0u64;
    // Probe every candidate's head and tail before filling the slots, so an older observed
    // failure beats newer passing logs without reading every report in full.
    let mut reports: Vec<_> = candidates
        .into_iter()
        .filter_map(|candidate| {
            let excerpt = read_scratch_excerpt(
                &root,
                &candidate,
                started,
                budget,
                0,
                &mut diagnostic_read_bytes,
            )?;
            Some((candidate, excerpt))
        })
        .collect();
    let priority = |(a, a_excerpt): &(ScratchCandidate, ScratchExcerpt),
                    (b, b_excerpt): &(ScratchCandidate, ScratchExcerpt)| {
        b_excerpt
            .failures
            .is_some()
            .cmp(&a_excerpt.failures.is_some())
            .then_with(|| b.modified.cmp(&a.modified))
            .then_with(|| a.relative.cmp(&b.relative))
    };
    reports.sort_by(priority);
    let mut incomplete_files = candidate_count
        - reports
            .iter()
            .filter(|(_, excerpt)| !excerpt.scan_incomplete)
            .count();
    // At most two full-read attempts, even if a guarded read rejects a selected file.
    // Cache the results so rendering retries never repeat diagnostic I/O.
    let mut full_reads = 0;
    let mut selected = Vec::new();
    for (candidate, excerpt) in reports {
        if selected.len() == SCRATCH_REPORTS_MAX_FILES {
            break;
        }
        let excerpt = if excerpt.scan_incomplete
            && candidate.bytes <= SCRATCH_REPORT_FULL_READ_BYTES
            && full_reads < SCRATCH_REPORTS_MAX_FILES
        {
            full_reads += 1;
            let Some(excerpt) = read_scratch_excerpt(
                &root,
                &candidate,
                started,
                budget,
                SCRATCH_REPORT_FULL_READ_BYTES,
                &mut diagnostic_read_bytes,
            ) else {
                continue;
            };
            if !excerpt.scan_incomplete {
                incomplete_files -= 1;
            }
            excerpt
        } else {
            excerpt
        };
        selected.push((candidate, excerpt));
    }
    let mut reports = selected;
    reports.sort_by(priority);
    for _ in 0..8 {
        let mut files = Vec::new();
        let mut remaining = budget;
        for (candidate, excerpt) in &reports {
            if files.len() == SCRATCH_REPORTS_MAX_FILES {
                break;
            }
            // The highest-priority file gets three quarters; later files share what is left.
            let share = if files.is_empty() && candidate_count > 1 {
                remaining * 3 / 4
            } else {
                remaining
            };
            if share == 0 {
                break;
            }
            let excerpt = excerpt.bounded(share);
            remaining = remaining.saturating_sub(excerpt.chars());
            let mut entry = json!({
                "path": candidate.relative, "bytes": candidate.bytes,
                "truncated_head": excerpt.truncated, "tail": excerpt.tail,
                "failure_scan_incomplete": excerpt.scan_incomplete,
            });
            if let Some(failures) = excerpt.failures {
                entry["failure_excerpt"] = json!(failures);
            }
            files.push(entry);
        }
        if files.is_empty() {
            return None;
        }
        let omitted = candidate_count - files.len();
        let value = json!({
            "schema_version": SCRATCH_REPORTS_SCHEMA,
            "scope": "files this failed verifier wrote under its scratch root; tail only; not execution authority",
            "files": files,
            "omitted_files": omitted,
            "failure_scan_incomplete_files": incomplete_files,
            "diagnostic_read_bytes": diagnostic_read_bytes,
        });
        let rendered = serde_json::to_string_pretty(&value)
            .map(|text| text.chars().count())
            .unwrap_or(usize::MAX);
        if rendered <= SCRATCH_REPORTS_CONTEXT_CHARS {
            return Some(value);
        }
        // Escaping (quotes, newlines) inflates the rendering; shrink proportionally and retry.
        budget = budget * SCRATCH_REPORTS_CONTEXT_CHARS / rendered.max(1) * 9 / 10;
    }
    None
}

struct ScratchExcerpt {
    failures: Option<String>,
    tail: String,
    truncated: bool,
    scan_incomplete: bool,
}

impl ScratchExcerpt {
    fn chars(&self) -> usize {
        self.tail.chars().count()
            + self
                .failures
                .as_ref()
                .map_or(0, |text| text.chars().count())
    }

    fn bounded(&self, max_chars: usize) -> Self {
        let failure_budget = max_chars * 2 / 3;
        let failures = self
            .failures
            .as_ref()
            .filter(|_| failure_budget >= 64)
            .map(|text| {
                if text.chars().count() <= failure_budget {
                    text.clone()
                } else {
                    last_chars(text, failure_budget - 3)
                }
            });
        let tail_budget =
            max_chars.saturating_sub(failures.as_ref().map_or(0, |text| text.chars().count()));
        let count = self.tail.chars().count();
        Self {
            failures,
            tail: self
                .tail
                .chars()
                .skip(count.saturating_sub(tail_budget))
                .collect(),
            truncated: self.truncated || count > tail_budget,
            scan_incomplete: self.scan_incomplete,
        }
    }
}

fn last_chars(text: &str, max_chars: usize) -> String {
    let count = text.chars().count();
    if count <= max_chars {
        return text.to_string();
    }
    let kept: String = text.chars().skip(count - max_chars).collect();
    format!("...{kept}")
}

fn failure_word(text: &str) -> bool {
    FAILURE_TEXT_HINTS.iter().any(|hint| text.contains(hint))
}

fn failure_key(key: &str) -> bool {
    let key = key.to_ascii_lowercase();
    FAILURE_KEY_HINTS.iter().any(|hint| key.contains(hint))
}

/// Leaves under a failure-named key, rendered as "path: value"; long strings keep their end,
/// where tracebacks put the assertion.
fn render_failure_value(value: &Value, path: &str, out: &mut Vec<String>, nodes: &mut usize) {
    *nodes += 1;
    if *nodes > FAILURE_JSON_MAX_NODES {
        return;
    }
    match value {
        Value::String(text) => {
            let trimmed = text.trim();
            if !trimmed.is_empty() && !matches!(trimmed, "PASS" | "OK" | "ok" | "passed") {
                out.push(format!(
                    "{path}: {}",
                    last_chars(trimmed, FAILURE_VALUE_CHARS)
                ));
            }
        }
        Value::Number(number) if number.as_f64() != Some(0.0) => {
            out.push(format!("{path}: {number}"))
        }
        Value::Bool(true) => out.push(format!("{path}: true")),
        Value::Array(items) => {
            for (index, item) in items.iter().take(32).enumerate() {
                render_failure_value(item, &format!("{path}[{index}]"), out, nodes);
            }
        }
        Value::Object(map) => {
            for (key, item) in map {
                render_failure_value(item, &format!("{path}.{key}"), out, nodes);
            }
        }
        _ => {}
    }
}

fn collect_json_failures(value: &Value, path: &str, out: &mut Vec<String>, nodes: &mut usize) {
    *nodes += 1;
    if *nodes > FAILURE_JSON_MAX_NODES {
        return;
    }
    match value {
        Value::Object(map) => {
            for (key, item) in map {
                let child = format!("{path}.{key}");
                if failure_key(key) {
                    render_failure_value(item, &child, out, nodes);
                } else {
                    collect_json_failures(item, &child, out, nodes);
                }
            }
        }
        Value::Array(items) => {
            for (index, item) in items.iter().take(256).enumerate() {
                collect_json_failures(item, &format!("{path}[{index}]"), out, nodes);
            }
        }
        Value::String(text) if failure_word(text) => {
            out.push(format!(
                "{path}: {}",
                last_chars(text.trim(), FAILURE_VALUE_CHARS)
            ));
        }
        _ => {}
    }
}

/// Failure-bearing lines of a report: JSON fields named like failures/errors/tracebacks, or
/// text lines carrying failure markers. The latest entries are kept when over budget.
fn failure_excerpt(text: &str, max_chars: usize) -> Option<String> {
    if max_chars < 64 {
        return None;
    }
    let trimmed = text.trim_start();
    let mut lines = Vec::new();
    if trimmed.starts_with('{') || trimmed.starts_with('[') {
        if let Ok(value) = serde_json::from_str::<Value>(text) {
            let mut nodes = 0;
            collect_json_failures(&value, "$", &mut lines, &mut nodes);
        }
    }
    if lines.is_empty() {
        lines = text
            .lines()
            .filter(|line| failure_word(line))
            .map(|line| last_chars(line.trim(), 400))
            .collect();
    }
    lines.dedup();
    if lines.is_empty() {
        return None;
    }
    let mut kept: Vec<String> = Vec::new();
    let mut used = 0usize;
    for line in lines.iter().rev() {
        let size = line.chars().count() + 1;
        if used + size > max_chars {
            break;
        }
        used += size;
        kept.push(line.clone());
    }
    if kept.is_empty() {
        return Some(last_chars(lines.last()?, max_chars.saturating_sub(4)));
    }
    kept.reverse();
    Some(kept.join("\n"))
}

fn read_scratch_excerpt(
    root: &Path,
    candidate: &ScratchCandidate,
    started: SystemTime,
    max_chars: usize,
    full_read_bytes: u64,
    diagnostic_read_bytes: &mut u64,
) -> Option<ScratchExcerpt> {
    let mut file = std::fs::File::open(&candidate.path).ok()?;
    let opened = file.metadata().ok()?;
    // Re-check the opened inode so a swapped symlink or hard link cannot redirect the read.
    if !opened.is_file()
        || opened.nlink() != 1
        || opened.dev() != candidate.dev
        || opened.ino() != candidate.ino
        || opened.modified().ok()? < started
        || !candidate.path.canonicalize().ok()?.starts_with(root)
    {
        return None;
    }
    // Probes split the existing byte window between head and tail. Only selected reports
    // may be read whole up to the full-read limit. UTF-8 needs at most four bytes per character.
    let window = if opened.len() <= full_read_bytes {
        opened.len()
    } else {
        (max_chars as u64).saturating_mul(4).min(opened.len())
    };
    let head_window = if window < opened.len() { window / 2 } else { 0 };
    let mut bytes = Vec::with_capacity(window as usize + 5);
    if head_window > 0 {
        let read = file.by_ref().take(head_window).read_to_end(&mut bytes);
        *diagnostic_read_bytes += bytes.len() as u64;
        read.ok()?;
        bytes.extend_from_slice(b"\n...\n");
    }
    let tail_window = window - head_window;
    file.seek(SeekFrom::Start(opened.len() - tail_window))
        .ok()?;
    let tail_start = bytes.len();
    let read = file.take(tail_window).read_to_end(&mut bytes);
    *diagnostic_read_bytes += (bytes.len() - tail_start) as u64;
    read.ok()?;
    if bytes.contains(&0) {
        return None;
    }
    let start = tail_start
        + bytes[tail_start..]
            .iter()
            .position(|byte| byte & 0xC0 != 0x80)
            .unwrap_or(bytes.len() - tail_start);
    let text = String::from_utf8_lossy(&bytes[start..]);
    let failures = if head_window == 0 {
        failure_excerpt(&text, max_chars * 2 / 3)
    } else {
        failure_excerpt(&String::from_utf8_lossy(&bytes), max_chars * 2 / 3)
    };
    let tail_budget =
        max_chars.saturating_sub(failures.as_ref().map_or(0, |text| text.chars().count()));
    let count = text.chars().count();
    let truncated = window < opened.len() || count > tail_budget;
    let tail = if count > tail_budget {
        text.chars().skip(count - tail_budget).collect()
    } else {
        text.into_owned()
    };
    Some(ScratchExcerpt {
        failures,
        tail,
        truncated,
        scan_incomplete: window < opened.len(),
    })
}

pub(super) fn prepare_batch(
    arguments: &Value,
    matcher: Option<&JSpaceMatcher>,
    allowed_commands: Option<&BTreeSet<String>>,
    call_ids: &[String],
) -> Result<Option<Vec<PreparedVerifier>>, String> {
    prepare_batch_with_parent_channel(
        arguments,
        matcher,
        allowed_commands,
        call_ids,
        crate::parent_verifier::available(),
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
            || [&invocation.command_type, &invocation.command]
                .into_iter()
                .flatten()
                .any(|name| name != "focused_verifier")
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
    if !parent_channel_available
        && !code_tools::shell_executor::focused_verifier_sandbox_available()
    {
        return Err("VERIFIER_READ_SCOPE_OS_POLICY_UNAVAILABLE".into());
    }
    matcher
        .revalidate_verifier_paths()
        .map_err(|error| error.to_string())?;
    Ok(Some(prepared))
}

fn validate_parent_observation(
    observation: code_tools::shell_executor::VerifierObservation,
    matcher: &JSpaceMatcher,
    item: &PreparedVerifier,
) -> Result<code_tools::shell_executor::VerifierObservation, String> {
    observation.validate_verifier_output_binding(
        matcher.authorization_semantic_sha256(),
        item.index,
        &tura_path::jspace::semantic_sha256(&json!(item.command)),
        &item.call_id,
    )?;
    Ok(observation)
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
        let started = SystemTime::now();
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
        let mut response = if let Err(error) = accepted {
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
                item.command.timeout_seconds,
                &ctx,
                async {
                    let observation = crate::parent_verifier::request(
                        matcher, &item.call_id, item.index, &cancellation,
                    ).await?;
                    validate_parent_observation(observation, matcher, &item)
                },
            )
            .await
        } else if item.command.python_import_roots.is_some() {
            // The direct executor cannot install explicit PYTHONPATH; never drop it.
            code_tools::shell_executor::focused_verifier_not_started(
                &ctx,
                PYTHON_IMPORT_ROOTS_PARENT_REQUIRED,
                item.command.timeout_seconds,
            )
        } else {
            code_tools::shell_executor::execute_focused_verifier(
                &item.command.argv,
                &item.command.scratch_root,
                matcher.repo_root(),
                item.command.cwd.as_deref(),
                &matcher.scope_projection().read_scopes,
                item.command.timeout_seconds,
                &ctx,
            )
            .await
        };
        // Match command_run's result envelope so the existing model feedback
        // and binding path sees test failures instead of silently dropping them.
        let success = response.success;
        if !success
            && let Some(reports) =
                failed_verifier_scratch_reports(&item.command.scratch_root, started)
        {
            match &mut response.output {
                Value::Object(fields) => {
                    fields.entry("scratch_reports").or_insert(reports);
                }
                other => *other = json!({"scratch_reports": reports}),
            }
        }
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

#[cfg(test)]
#[cfg(target_os = "macos")]
mod tests {
    use super::*;
    use crate::services::command_run::CommandRunService;
    use sha2::{Digest, Sha256};
    use std::fs;
    use tura_path::jspace::{authorization_semantic_sha256, semantic_sha256};

    fn digest(path: &Path) -> String {
        format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
    }

    #[test]
    fn failed_verifier_scratch_reports_surface_only_new_bounded_regular_files() {
        let temp = tempfile::tempdir().unwrap();
        let scratch = temp.path().join("verifier-2");
        let outside = temp.path().join("outside");
        fs::create_dir_all(&scratch).unwrap();
        fs::create_dir_all(&outside).unwrap();
        let stale = scratch.join("branch-b-old.json");
        fs::write(&stale, "FAIL: STALE_REPORT\n").unwrap();
        fs::OpenOptions::new()
            .write(true)
            .open(&stale)
            .unwrap()
            .set_modified(SystemTime::now() - std::time::Duration::from_secs(120))
            .unwrap();
        let started = SystemTime::now();
        assert!(failed_verifier_scratch_reports(&scratch, started).is_none());

        let marker = "AssertionError: 'UNKNOWN_OR_POSSIBLY_STARTED' != 'STARTED'";
        let report = format!(
            "{{\"suites\": \"{}\", \"stderr\": \"{marker}\"}}\n",
            "x".repeat(60_000)
        );
        fs::write(scratch.join("branch-b-new.json"), &report).unwrap();
        fs::write(scratch.join("binary.bin"), b"FAIL: BINARY_REPORT\0").unwrap();
        let secret = outside.join("secret.txt");
        fs::write(&secret, "FAIL: SECRET_OUTSIDE").unwrap();
        std::os::unix::fs::symlink(&secret, scratch.join("link.json")).unwrap();
        std::os::unix::fs::symlink(&outside, scratch.join("linked-directory")).unwrap();
        fs::hard_link(&secret, scratch.join("hard.json")).unwrap();
        fs::create_dir(scratch.join("nested")).unwrap();
        fs::write(
            scratch.join("nested/junit.xml"),
            "<failure message=\"NESTED_FAILURE\"/>\n",
        )
        .unwrap();

        let value = failed_verifier_scratch_reports(&scratch, started).expect("new reports");
        let rendered = serde_json::to_string_pretty(&value).unwrap();
        assert!(
            rendered.chars().count() <= SCRATCH_REPORTS_CONTEXT_CHARS,
            "{}",
            rendered.len()
        );
        assert!(
            rendered.contains("UNKNOWN_OR_POSSIBLY_STARTED"),
            "{rendered}"
        );
        assert!(rendered.contains("NESTED_FAILURE"), "{rendered}");
        assert!(!rendered.contains("SECRET_OUTSIDE"));
        assert!(!rendered.contains("STALE_REPORT"));
        assert!(!rendered.contains("BINARY_REPORT"));
        assert_eq!(value["schema_version"], SCRATCH_REPORTS_SCHEMA);
        let files = value["files"].as_array().unwrap();
        assert_eq!(files.len(), 2, "{rendered}");
        let main = files
            .iter()
            .find(|file| file["path"] == "branch-b-new.json")
            .unwrap();
        assert_eq!(main["truncated_head"], true);
        assert_eq!(main["bytes"], report.len() as u64);
        assert_eq!(
            value["omitted_files"], 1,
            "binary candidate is counted, never rendered"
        );
    }

    #[test]
    fn failed_verifier_scratch_reports_prioritize_failures_over_newer_passing_logs() {
        let temp = tempfile::tempdir().unwrap();
        let scratch = temp.path().join("verifier-2");
        fs::create_dir_all(&scratch).unwrap();
        let started = SystemTime::now() - std::time::Duration::from_secs(10);
        let diagnostic = "AssertionError: expected 2, got 1";
        let reports = [
            ("first.log", format!("FAIL: test_value\n{diagnostic}\n")),
            ("second.log", "Ran 6 tests\nOK\n".to_string()),
            ("summary.json", json!({"status": "FAIL"}).to_string()),
        ];
        for (index, (name, text)) in reports.iter().enumerate() {
            let path = scratch.join(name);
            fs::write(&path, text).unwrap();
            fs::OpenOptions::new()
                .write(true)
                .open(&path)
                .unwrap()
                .set_modified(started + std::time::Duration::from_secs(index as u64 + 1))
                .unwrap();
        }

        let value = failed_verifier_scratch_reports(&scratch, started).expect("failed reports");
        let rendered = serde_json::to_string_pretty(&value).unwrap();
        assert!(rendered.chars().count() <= SCRATCH_REPORTS_CONTEXT_CHARS);
        let files = value["files"].as_array().unwrap();
        assert_eq!(files.len(), SCRATCH_REPORTS_MAX_FILES);
        assert_eq!(files[0]["path"], "summary.json");
        assert_eq!(files[1]["path"], "first.log");
        assert!(
            files[1]["failure_excerpt"]
                .as_str()
                .unwrap()
                .contains(diagnostic),
            "{rendered}"
        );
        assert!(
            files[1]["tail"].as_str().unwrap().contains(diagnostic),
            "{rendered}"
        );
        assert_eq!(files[1]["truncated_head"], false);
        let summary: Value = serde_json::from_str(files[0]["tail"].as_str().unwrap()).unwrap();
        assert_eq!(summary["status"], "FAIL");
        assert_eq!(value["omitted_files"], 1);
        assert!(!rendered.contains("second.log"));
    }

    #[test]
    fn failed_verifier_scratch_reports_bound_aggregate_diagnostic_reads() {
        let temp = tempfile::tempdir().unwrap();
        let scratch = temp.path().join("verifier-2");
        fs::create_dir_all(&scratch).unwrap();
        let started = SystemTime::now() - std::time::Duration::from_secs(10);
        let candidate_count = SCRATCH_REPORTS_MAX_FILES + 1;
        let diagnostic = "AssertionError: expected 2, got 1";
        for index in 0..candidate_count {
            let marker = if index == 0 { diagnostic } else { "OK" };
            let report = format!(
                "{marker}\n{}\n",
                "x".repeat(SCRATCH_REPORT_FULL_READ_BYTES as usize - marker.len() - 2)
            );
            let path = scratch.join(format!("report-{index}.log"));
            fs::write(&path, report).unwrap();
            fs::OpenOptions::new()
                .write(true)
                .open(&path)
                .unwrap()
                .set_modified(started + std::time::Duration::from_secs(index as u64 + 1))
                .unwrap();
        }

        let value = failed_verifier_scratch_reports(&scratch, started).expect("reports");
        let probe_bytes = (SCRATCH_REPORTS_CONTEXT_CHARS * 3 / 4 * 4) as u64;
        let read_bound = candidate_count as u64 * probe_bytes
            + SCRATCH_REPORTS_MAX_FILES as u64 * SCRATCH_REPORT_FULL_READ_BYTES;
        // Counts actual bytes read, including probes of the omitted file, rather than output.
        assert_eq!(value["diagnostic_read_bytes"], read_bound);
        assert!(read_bound < candidate_count as u64 * SCRATCH_REPORT_FULL_READ_BYTES);
        let rendered = serde_json::to_string_pretty(&value).unwrap();
        assert!(rendered.chars().count() <= SCRATCH_REPORTS_CONTEXT_CHARS);
        let files = value["files"].as_array().unwrap();
        assert_eq!(files.len(), SCRATCH_REPORTS_MAX_FILES);
        assert_eq!(files[0]["path"], "report-0.log");
        assert_eq!(files[1]["path"], "report-2.log");
        assert!(
            files[0]["failure_excerpt"]
                .as_str()
                .unwrap()
                .contains(diagnostic)
        );
        assert!(
            files
                .iter()
                .all(|file| file["failure_scan_incomplete"] == false)
        );
        assert_eq!(value["failure_scan_incomplete_files"], 1);
        assert_eq!(value["omitted_files"], 1);
    }

    #[test]
    fn failed_verifier_scratch_reports_surface_head_failures_of_json_reports() {
        let temp = tempfile::tempdir().unwrap();
        let scratch = temp.path().join("verifier-2");
        fs::create_dir_all(&scratch).unwrap();
        let started = SystemTime::now();
        // I18 shape: the failing assertion sits in suites.*.failures near the head, while the
        // tail holds contexts and a long passing stdout.
        let report = json!({
            "status": "FAIL",
            "suites": {
                "B_objective": {"tests": 12, "success": false, "errors": [], "failures": [[
                    "test_prestart_contract.Case.test_identity",
                    "Traceback (most recent call last):\n  File \"case.py\", line 3, in test_identity\nAssertionError: 'MODEL_LAUNCH_FAILED' != 'IDENTITY_MISMATCH'"
                ]]},
                "B_preserved": {"tests": 114, "success": true, "errors": [], "failures": [],
                                "stdout": "x".repeat(40_000)}
            },
            "contexts": {"B_objective": {"cwd": "/tmp/workspace/task_b", "selector": "test_prestart_contract"}},
            "semantic": null, "wall_seconds": 9.5
        });
        fs::write(
            scratch.join("branch-b-head.json"),
            serde_json::to_string_pretty(&report).unwrap(),
        )
        .unwrap();
        let value = failed_verifier_scratch_reports(&scratch, started).expect("report");
        let rendered = serde_json::to_string_pretty(&value).unwrap();
        assert!(
            rendered.chars().count() <= SCRATCH_REPORTS_CONTEXT_CHARS,
            "{}",
            rendered.len()
        );
        let failures = value["files"][0]["failure_excerpt"]
            .as_str()
            .expect("failure excerpt");
        assert!(
            failures.contains("'MODEL_LAUNCH_FAILED' != 'IDENTITY_MISMATCH'"),
            "{failures}"
        );
        assert!(
            failures.contains("test_prestart_contract.Case.test_identity"),
            "{failures}"
        );
        assert!(failures.contains("$.status: FAIL"), "{failures}");
        assert!(
            !failures.contains("xxxxxxxxxx"),
            "passing stdout is not failure evidence: {failures}"
        );
        assert_eq!(value["files"][0]["truncated_head"], true);
    }

    #[test]
    fn failure_excerpt_keeps_latest_text_failures_within_budget() {
        let text = (0..200)
            .map(|i| format!("ok line {i}"))
            .collect::<Vec<_>>()
            .join("\n")
            + "\nFAIL: test_alpha (suite.Alpha)\nTraceback (most recent call last):\nAssertionError: 1 != 2\n";
        let excerpt = failure_excerpt(&text, 200).expect("text failures");
        assert!(excerpt.contains("AssertionError: 1 != 2"), "{excerpt}");
        assert!(excerpt.contains("FAIL: test_alpha"), "{excerpt}");
        assert!(!excerpt.contains("ok line"), "{excerpt}");
        assert!(failure_excerpt("all good\n", 200).is_none());
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

    #[tokio::test]
    async fn parent_output_binding_and_shell_transport_preserve_python_metadata() {
        let root = tempfile::tempdir().unwrap();
        let (contract, workspace) = fixture(root.path());
        let matcher = JSpaceMatcher::from_value(&workspace, &contract).unwrap();
        let item = PreparedVerifier {
            call_id: "parent-output".into(), index: 0, step: 1, id: None,
            command: matcher.verifier_commands()[0].clone(),
        };
        let reference = json!({
            "path": null, "sha256": null, "bytes": 0, "durable": false,
            "complete": false, "error": "VERIFIER_OUTPUT_PATH_REJECTED",
        });
        let metadata = json!({
            "schema_version": "nokiy_verifier_output_v1",
            "request_id": "request-v356", "request_sha256": "a".repeat(64),
            "authorization_semantic_sha256": matcher.authorization_semantic_sha256(),
            "verifier_index": item.index,
            "verifier_sha256": tura_path::jspace::semantic_sha256(&contract["verifier_commands"][0]),
            "call_id": item.call_id,
            "streams": {"stdout": reference, "stderr": reference},
        });
        let legacy = json!({
            "success": false, "exit_code": 1, "stdout": "raw failure", "stderr": "raw error",
            "process_reaped": true, "process_group_empty": true, "outcome": "known",
        });
        let observation = serde_json::from_value(legacy.clone()).unwrap();
        assert!(validate_parent_observation(observation, &matcher, &item).is_ok());
        let mut payload = legacy;
        payload["verifier_output"] = metadata.clone();
        for (field, value) in [
            ("authorization_semantic_sha256", json!("b".repeat(64))),
            ("verifier_index", json!(1)),
            ("verifier_sha256", json!("c".repeat(64))),
            ("call_id", json!("another-call")),
        ] {
            let mut conflicting = payload.clone();
            conflicting["verifier_output"][field] = value;
            let observation = serde_json::from_value(conflicting).unwrap();
            assert_eq!(validate_parent_observation(observation, &matcher, &item).unwrap_err(),
                "VERIFIER_OUTPUT_BINDING_MISMATCH", "{field}");
        }
        let observation = validate_parent_observation(
            serde_json::from_value(payload).unwrap(), &matcher, &item,
        ).unwrap();
        let store = Arc::new(ReceiptStore::open(root.path().canonicalize().unwrap()).unwrap());
        let ctx = ToolContext::new(workspace).with_call_id(item.call_id.clone())
            .with_receipt_store(Some(store));
        let response = code_tools::shell_executor::execute_remote_focused_verifier(
            5, &ctx, async { Ok(observation) },
        ).await;
        assert!(!response.success);
        let output = code_tools::shell_executor::shell_output_value(response);
        assert_eq!(output["verifier_output"], metadata);
        assert_eq!(output["exit_code"], 1);
        assert_eq!(output["stdout"], "raw failure");
        assert_eq!(output["stderr"], "raw error");
        assert!(output.get("verification_evidence").is_none());
        assert_eq!(output["terminal_receipt"]["call_id"], item.call_id);
        assert_eq!(output["terminal_receipt"]["terminal_state"], "failed");
        assert_eq!(output["terminal_receipt"]["termination_proven"], true);
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
        let old =
            prepare_batch_with_parent_channel(&args, Some(&legacy), Some(&allowed), &ids, true)
                .unwrap()
                .unwrap();
        assert!(old[0].command.python_import_roots.is_none());

        let executable = workspace.join("python3.13");
        fs::write(&executable, [0xfe, 0xed, 0xfa, 0xcf, 1, 2, 3, 4]).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
        contract["verifier_commands"][0]["argv"][0] = json!(executable);
        contract["verifier_commands"][0]["executable_sha256"] = json!(digest(&executable));
        contract["verifier_commands"][0]["python_import_roots"] = json!([workspace.join("src")]);
        contract["authorization_semantic_sha256"] =
            json!(authorization_semantic_sha256(&contract).unwrap());
        contract.as_object_mut().unwrap().remove("content_sha256");
        contract["content_sha256"] = json!(semantic_sha256(&contract));
        let matcher = JSpaceMatcher::from_value(&workspace, &contract).unwrap();
        assert_eq!(
            prepare_batch_with_parent_channel(&args, Some(&matcher), Some(&allowed), &ids, false)
                .unwrap_err(),
            PYTHON_IMPORT_ROOTS_PARENT_REQUIRED,
        );
        let prepared =
            prepare_batch_with_parent_channel(&args, Some(&matcher), Some(&allowed), &ids, true)
                .unwrap()
                .unwrap();
        assert_eq!(prepared.len(), 1);
        assert_eq!(prepared[0].index, 0);
        assert_eq!(prepared[0].call_id, ids[0]);
        assert_eq!(prepared[0].command, matcher.verifier_commands()[0]);
        assert_eq!(
            args["commands"][0]["command_line"],
            "{\"verifier_index\":0}"
        );
        for selection in [
            json!({"verifier_index":0,"python_import_roots":[workspace.join("src")]}),
            json!({"verifier_index":0,"argv":[executable]}),
        ] {
            let mut invalid = args.clone();
            invalid["commands"][0]["command_line"] = json!(selection.to_string());
            assert!(
                prepare_batch_with_parent_channel(
                    &invalid,
                    Some(&matcher),
                    Some(&allowed),
                    &ids,
                    true
                )
                .is_err()
            );
        }
        let mut invalid = args;
        invalid["commands"][0]["python_import_roots"] = json!([workspace.join("src")]);
        assert!(
            prepare_batch_with_parent_channel(&invalid, Some(&matcher), Some(&allowed), &ids, true)
                .is_err()
        );
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
        let normalized =
            code_tools::command_run::normalize_command_value_for_execution(raw, 0).unwrap();
        let mut args = json!({"execution_id":"run:tool:0","commands":[normalized]});
        let (_, ids) = code_tools::command_run::command_run_batch_identity(&args).unwrap();
        assert!(
            prepare_batch(&args, Some(&matcher), Some(&allowed), &ids)
                .unwrap()
                .is_some()
        );
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
