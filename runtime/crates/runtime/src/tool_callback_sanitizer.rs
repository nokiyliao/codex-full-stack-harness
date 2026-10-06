use serde_json::Value;

const TOOL_CALLBACK_OUTPUT_MAX_BYTES: usize = 10_000;
// Match the range-page producer in tools::commands::source_read, including
// JSON string escaping and the receipt-inclusive output envelope.
const SOURCE_READ_RANGE_MAX_TEXT_BYTES: usize = 12_288;
const SOURCE_READ_RANGE_MAX_RESULT_BYTES: usize = 16_384;
const MEDIA_CHANNEL_REASON: &str = "media payload is sent through the provider media channel";
pub(crate) const COMMAND_EVENTS_OMISSION_REASON: &str =
    "command event payloads are represented by the canonical results channel";
const SOURCE_POSTIMAGE_MAX_FILES: usize = 4;
const SOURCE_POSTIMAGE_MAX_LINES: u64 = 600;
const SOURCE_POSTIMAGE_MAX_JSON_BYTES: usize = 32_768;
const SOURCE_POSTIMAGE_MAX_LOCATORS: usize = 64;
const SOURCE_POSTIMAGE_MAX_LOCATOR_NAME_BYTES: usize = 512;
pub(crate) const SOURCE_POSTIMAGES_NOTICE: &str = concat!(
    "Only listed definitions/bindings are complete. Same-file syntactic support ",
    "is not dependency resolution or complete-file proof. Context only: semantically ",
    "review against the task; not verifier facts, acceptance or a new grant. ",
    "Missing evidence requires ordinary granted source_read."
);
pub(crate) const SOURCE_POSTIMAGE_DELTA_NOTICE: &str = concat!(
    "All textual changes between each pinned preimage and fresh postimage; hunks ",
    "include up to three surrounding physical context lines at each boundary, ",
    "not complete definitions, dependencies or files. ",
    "after_text is postimage source at after_start_line; before_text is old source. ",
    "For each listed path, previously supplied excerpts matching preimage_sha256 ",
    "can be combined with these hunks for postimage_sha256: text outside hunks is ",
    "unchanged, but line offsets may shift. Unseen surrounding semantics are not ",
    "supplied. Context only: semantically ",
    "review against the task; not verifier facts, acceptance or a new grant. ",
    "Missing or newer evidence requires ordinary granted source_read."
);
pub(crate) const SOURCE_POSTIMAGE_DELTA_V2_NOTICE_SUFFIX: &str =
    " AST locators identify qualified units in the pinned postimage; they do not supply complete source text or prove dependencies.";

pub(crate) fn sanitize_tool_callback_output(value: &Value) -> Value {
    sanitize_callback(value)
}

pub(crate) fn sanitize_tool_callback_result(value: &Value) -> Value {
    sanitize_callback(value)
}

fn sanitize_callback(value: &Value) -> Value {
    let Some(map) = value.as_object() else {
        return sanitize_value(value, None);
    };
    match map.get("tool_name") {
        Some(name) if name.as_str() == Some("command_run") && command_run_tool_identity(value) => {
            let commands = value
                .get("input")
                .and_then(|input| input.get("commands"))
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            Value::Object(
                map.iter().map(|(field, item)| {
                    let sanitized = if field == "output" {
                        sanitize_command_run_output(item, Some(commands))
                    } else {
                        sanitize_object_field(map, field, item)
                    };
                    (field.clone(), sanitized)
                }).collect(),
            )
        }
        Some(_) => sanitize_value(value, None),
        // Streaming sanitizes individual results and completed event records
        // before the command_run envelope exists. Do not infer these shapes in
        // generic recursion: only their canonical output site is exempt.
        None if map.contains_key("command_type") && map.contains_key("output") => {
            sanitize_command_result(value, None)
        }
        None if is_streamed_command_event(value) => Value::Object(
            map.iter().map(|(field, item)| {
                let sanitized = if field == "result" {
                    // A completed result record may omit command_type/command.
                    // Check supplied wrapper bindings below; only an actual
                    // command (or a typed wrapper) supplies command context.
                    let input = value.get("command")
                        .or_else(|| value.get("command_type").map(|_| value));
                    sanitize_command_result(item, input)
                } else {
                    sanitize_object_field(map, field, item)
                };
                (field.clone(), sanitized)
            }).collect(),
        ),
        None => sanitize_command_run_output(value, None),
    }
}

pub(crate) fn command_run_tool_identity(value: &Value) -> bool {
    ["tool_name", "tool"].iter().all(|key| {
        value.get(*key).is_none_or(|name| name.as_str() == Some("command_run"))
    })
}

fn command_identities_agree(left: &Value, right: &Value) -> bool {
    command_run_tool_identity(left) && command_run_tool_identity(right)
        && left.get("command_type") == right.get("command_type")
        && command_bindings_agree(left, right)
}

fn command_bindings_agree(left: &Value, right: &Value) -> bool {
    ["command_id", "command_run_id", "provider_tool_call_id", "command_index", "step"]
        .iter().all(|key| match (left.get(*key).filter(|v| !v.is_null()), right.get(*key).filter(|v| !v.is_null())) {
            (Some(left), Some(right)) => left == right,
            _ => true,
        })
}

fn is_streamed_command_event(value: &Value) -> bool {
    command_run_tool_identity(value) && value.get("input").is_none()
        && value.get("status").and_then(Value::as_str) == Some("completed")
        && value.get("runtime_id").and_then(Value::as_str).is_some_and(|id| !id.is_empty())
        && value.get("command_index").and_then(Value::as_u64).is_some()
        && value.get("result_index").is_none_or(|index| index.as_u64().is_some())
        && (value.get("result_index").is_some() || value.get("command").is_some_and(Value::is_object))
        && value.get("result").is_some_and(Value::is_object)
        && value.get("command_type").is_none_or(|kind| Some(kind) == value["result"].get("command_type"))
        && command_bindings_agree(value, &value["result"])
        && value.get("success").is_none_or(|success| Some(success) == value["result"].get("success"))
        && value.get("error").is_none_or(Value::is_null)
}

// Only the results channel and its one observed streamed wrapper are typed.
// Arbitrary nesting, event copies and wrong-tool payloads remain generic.
pub(crate) fn command_run_results_channel(value: &Value) -> Option<&Value> {
    if !command_run_tool_identity(value) || value.get("input").is_some() { return None; }
    let channel = if value.get("results").is_some() {
        value
    } else {
        if value.get("commands").is_some() { return None; }
        value.get("streamed_command_run_result")?
    };
    (command_run_tool_identity(channel) && channel.get("input").is_none()
        && channel.get("results").is_some_and(Value::is_array))
        .then_some(channel)
}

pub(crate) fn command_run_context_commands<'a>(channel: &'a Value, input: Option<&'a [Value]>) -> Option<&'a [Value]> {
    let local = channel.get("commands")
        .map(|commands| commands.as_array().map(Vec::as_slice).unwrap_or(&[]));
    match (input, local) {
        (Some(input), Some(local)) if input.len() != local.len()
            || input.iter().zip(local).any(|(left, right)| {
                !command_identities_agree(left, right)
                    || (left.get("command_type").and_then(Value::as_str) == Some("focused_verifier")
                        && (!focused_verifier_identity(left) || !focused_verifier_identity(right)))
            }) => Some(&[]),
        _ => input.or(local),
    }
}

fn sanitize_command_run_output(value: &Value, commands: Option<&[Value]>) -> Value {
    let Some(channel) = command_run_results_channel(value) else {
        return sanitize_value(value, None);
    };
    if value.get("results").is_none() {
        let map = value.as_object().unwrap();
        return Value::Object(map.iter().map(|(field, item)| {
            let sanitized = if field == "streamed_command_run_result" {
                sanitize_command_run_output(channel, commands)
            } else {
                sanitize_object_field(map, field, item)
            };
            (field.clone(), sanitized)
        }).collect());
    }
    let map = channel.as_object().unwrap();
    let commands = command_run_context_commands(channel, commands);
    let mut index = 0;
    Value::Object(map.iter().map(|(field, item)| {
        let sanitized = if field == "results" && let Some(results) = item.as_array() {
            Value::Array(sanitize_command_results(results, commands, &mut index))
        } else {
            sanitize_object_field(map, field, item)
        };
        (field.clone(), sanitized)
    }).collect())
}

fn sanitize_command_results(
    results: &[Value],
    commands: Option<&[Value]>,
    index: &mut usize,
) -> Vec<Value> {
    results.iter().map(|result| {
        let Some(map) = result.as_object() else {
            *index += 1;
            return sanitize_value(result, None);
        };
        let batch = result.get("mode").and_then(Value::as_str) == Some("batch")
            && result.get("results").and_then(Value::as_array).is_some();
        if !batch {
            let input = commands.map(|items| items.get(*index).unwrap_or(&Value::Null));
            *index += 1;
            return sanitize_command_result(result, input);
        }
        let commands = if command_run_tool_identity(result) { commands } else { Some(&[][..]) };
        Value::Object(map.iter().map(|(field, item)| {
            let sanitized = if field == "results" {
                Value::Array(sanitize_command_results(item.as_array().unwrap(), commands, index))
            } else {
                sanitize_object_field(map, field, item)
            };
            (field.clone(), sanitized)
        }).collect())
    }).collect()
}

fn sanitize_command_result(result: &Value, input: Option<&Value>) -> Value {
    let Some(map) = result.as_object() else { return sanitize_value(result, None); };
    let preserve_source_page = result.pointer("/output/stdout").and_then(Value::as_str)
        .is_some_and(|stdout| stdout.len() > TOOL_CALLBACK_OUTPUT_MAX_BYTES
            && bounded_source_read_page(result, input).is_some());
    Value::Object(map.iter().map(|(field, item)| {
        let sanitized = if field == "output" && let Some(output) = item.as_object() {
            Value::Object(output.iter().map(|(key, value)| {
                let sanitized = if key == "stdout" && preserve_source_page {
                    value.clone()
                } else if key == "source_postimages" {
                    bounded_source_postimages_for_context(value, Some(result), input)
                } else if key == "verification_evidence"
                    && focused_verifier_identity(result)
                    && input.is_none_or(|input| focused_verifier_identity(input) && command_identities_agree(result, input))
                    && output.get("executor").and_then(Value::as_str) == Some("parent_focused_verifier")
                    && TerminalVerifierProof::parse_bounded(value).is_some_and(|proof|
                        result.pointer("/output/terminal_receipt/call_id").and_then(Value::as_str) == Some(proof.call_id.as_str()))
                {
                    // Preserve the typed proof, not arbitrary nested postimages.
                    // The tracker still owns grant/source/outcome validation.
                    value.clone()
                } else {
                    sanitize_object_field(output, key, value)
                };
                (key.clone(), sanitized)
            }).collect())
        } else {
            sanitize_object_field(map, field, item)
        };
        (field.clone(), sanitized)
    }).collect())
}

fn source_read_command_identity(value: &Value) -> bool {
    value.get("command_type").and_then(Value::as_str) == Some("source_read")
        && command_run_tool_identity(value)
        && value.get("step").and_then(Value::as_u64).is_some_and(|step| step > 0)
        && value.get("command_index").is_none_or(|index| index.as_u64().is_some())
        && ["command_id", "command_run_id", "provider_tool_call_id"].iter().all(|key|
            value.get(*key).filter(|id| !id.is_null()).is_none_or(|id|
                id.as_str().is_some_and(|id| !id.trim().is_empty() && id.len() <= 256)))
        && ["command", "input"].iter().all(|key| value.get(*key).is_none_or(|command| {
            (*key == "command" && command.as_str() == Some("source_read"))
                || (source_read_command_identity(command) && command_identities_agree(value, command))
        }))
}

// Only canonical, successful range pages can exceed the generic text budget.
// These lexical checks preserve producer pagination; they prove neither source
// freshness nor authority. Search pages have a smaller budget and need no bypass.
pub(crate) fn bounded_source_read_page(result: &Value, input: Option<&Value>) -> Option<()> {
    if !source_read_command_identity(result)
        || input.is_some_and(|input| !source_read_command_identity(input) || !command_identities_agree(result, input))
        || result.get("success").and_then(Value::as_bool) != Some(true)
        || result.get("error").is_some_and(|error| !error.is_null())
    {
        return None;
    }
    let output = result.get("output")?;
    let map = output.as_object()?;
    let required = [
        "exit_code", "stdout", "stderr", "path", "start_line", "end_line",
        "requested_end_line", "truncated", "truncation_reason", "next_line",
        "at_eof", "total_lines", "file_bytes", "source_sha256", "ends_with_newline",
    ];
    if !required.iter().all(|key| map.contains_key(*key))
        || !map.keys().all(|key| required.contains(&key.as_str())
            || matches!(key.as_str(), "line_numbers" | "terminal_receipt" | "terminal_receipt_path" | "changes"))
        || output["exit_code"].as_i64() != Some(0)
        || output["stderr"].as_str() != Some("")
        || output.get("changes").is_some_and(|changes| !changes.as_array().is_some_and(Vec::is_empty))
        || output.get("terminal_receipt_path").is_some_and(|path| !path.as_str().is_some_and(|path| !path.is_empty()))
    {
        return None;
    }
    let stdout = output["stdout"].as_str()?;
    if stdout.len() > SOURCE_READ_RANGE_MAX_TEXT_BYTES
        || !json_within_byte_budget(&output["stdout"], SOURCE_READ_RANGE_MAX_TEXT_BYTES)
        || !json_within_byte_budget(output, SOURCE_READ_RANGE_MAX_RESULT_BYTES)
    {
        return None;
    }
    let path = output["path"].as_str()?;
    if path.is_empty() || path.len() > 512 || path.contains(['\\', '\0'])
        || path.split('/').any(|part| matches!(part, "" | "." | ".."))
        || !std::path::Path::new(path).components().all(|part| matches!(part, std::path::Component::Normal(_)))
    {
        return None;
    }
    source_digest(&output["source_sha256"])?;
    let (start, end) = source_range(&output["start_line"], &output["end_line"])?;
    let requested = output["requested_end_line"].as_u64()?;
    let total = output["total_lines"].as_u64()?;
    let file_bytes = output["file_bytes"].as_u64()?;
    let truncated = output["truncated"].as_bool()?;
    let numbered = match output.get("line_numbers") {
        None => false,
        Some(value) if value.as_bool() == Some(true) => true,
        _ => return None,
    };
    if end > requested || end > total || end - start >= 200 || total > file_bytes || file_bytes > 1_048_576 {
        return None;
    }
    let mut lines = 0;
    for line in stdout.split_inclusive('\n') {
        let number = start.checked_add(lines)?;
        if numbered && !line.split_once(": ").is_some_and(|(label, _)| label == number.to_string()) {
            return None;
        }
        lines += 1;
    }
    let pagination = if end < total {
        output["next_line"].as_u64() == end.checked_add(1)
            && output["at_eof"].as_bool() == Some(false) && stdout.ends_with('\n')
    } else {
        output["next_line"].is_null() && output["at_eof"].as_bool() == Some(true)
    };
    let truncation = match output["truncation_reason"].as_str() {
        None => output["truncation_reason"].is_null() && !truncated && end == requested,
        Some("end_of_file") => truncated && end == total && end < requested,
        Some("line_limit") => truncated && end < total && end < requested && lines == 200,
        Some("response_limit") => truncated && end < total && end < requested && lines < 200,
        _ => false,
    };
    if lines != end - start + 1 || !pagination || !truncation
        || output["ends_with_newline"].as_bool() != Some(stdout.ends_with('\n'))
    {
        return None;
    }
    if let Some(receipt) = output.get("terminal_receipt") {
        receipt.as_object()?;
        if receipt["schema_version"] != "tura_command_terminal_receipt_v1"
            || receipt["termination_origin"] != "in_process_source_read"
            || receipt["outcome"] != "known" || receipt["terminal_state"] != "completed"
            || receipt["exit_code"].as_i64() != Some(0)
            || !["process_reaped", "process_group_empty", "termination_proven"].iter().all(|key| receipt[*key] == true)
            || receipt["reconcile_required"] != false || receipt["authority_effect"] != "none"
            || receipt["staging_authority"] != "none"
            || !matches!(receipt["failure_class"].as_str(), Some("none" | "workload"))
            || !receipt["call_id"].as_str().is_some_and(|id| !id.is_empty())
            || receipt.get("error").is_some_and(|error| !error.is_null())
            || ["timed_out", "cancelled"].iter().any(|key| receipt.get(*key).is_some_and(|value| value.as_bool() != Some(false)))
            || receipt.get("unresolved_effects").is_some_and(|effects| !effects.is_null() && !effects.as_array().is_some_and(Vec::is_empty))
        {
            return None;
        }
    }
    Some(())
}

fn json_within_byte_budget(value: &Value, budget: usize) -> bool {
    struct BudgetWriter { remaining: usize }
    impl std::io::Write for BudgetWriter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.remaining = self.remaining.checked_sub(bytes.len())
                .ok_or_else(|| std::io::Error::other("source page JSON budget exceeded"))?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
    }
    serde_json::to_writer(BudgetWriter { remaining: budget }, value).is_ok()
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct TerminalSourcePostimage {
    pub(crate) sha256: String,
    pub(crate) bytes: u64,
    pub(crate) mode: u32,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct TerminalVerifierProof {
    pub(crate) schema_version: String,
    pub(crate) authorization_semantic_sha256: String,
    pub(crate) verifier_index: usize,
    pub(crate) verifier_sha256: String,
    pub(crate) call_id: String,
    pub(crate) source_postimages: std::collections::BTreeMap<String, TerminalSourcePostimage>,
}

impl TerminalVerifierProof {
    pub(crate) const MAX_SOURCE_TARGETS: usize = 128;

    pub(crate) fn parse_bounded(value: &Value) -> Option<Self> {
        exact_object(value, &[
            "schema_version", "authorization_semantic_sha256", "verifier_index",
            "verifier_sha256", "call_id", "source_postimages",
        ])?;
        if value["schema_version"] != "nokiy_focused_verifier_evidence_v1"
            || value["call_id"].as_str().is_none_or(|id| id.trim().is_empty() || id.len() > 256)
        {
            return None;
        }
        source_digest(&value["authorization_semantic_sha256"])?;
        source_digest(&value["verifier_sha256"])?;
        let images = value["source_postimages"].as_object()?;
        if images.len() > Self::MAX_SOURCE_TARGETS || !images.iter().all(|(path, image)|
            !path.is_empty() && path.len() <= 256
                && exact_object(image, &["sha256", "bytes", "mode"]).is_some()
                && source_digest(&image["sha256"]).is_some())
        {
            return None;
        }
        // Bound every allocating field before deserializing. This schema is
        // shared with the tracker; preservation alone authenticates nothing.
        let proof: Self = serde::Deserialize::deserialize(value).ok()?;
        proof.source_postimages.values().all(|image|
            image.mode <= 0o177777 && image.bytes <= i64::MAX as u64).then_some(proof)
    }
}

// One guard for callback records and provider/cache projections. These are
// observations only: lexical bindings are checked, never filesystem identity,
// current J-Space authority, source freshness, verifier truth or acceptance.
pub(crate) fn bounded_source_postimages_for_context(
    projection: &Value,
    result: Option<&Value>,
    input_command: Option<&Value>,
) -> Value {
    if result.is_some_and(|result| successful_focused_verifier(result, input_command))
        && valid_source_postimages(projection).is_some()
    {
        return projection.clone();
    }
    serde_json::json!({
        "omitted_from_context": true,
        "reason": "Source postimages unavailable, invalid or over budget; use ordinary granted source_read. Context only, not verifier facts, acceptance or a grant."
    })
}

fn focused_verifier_identity(value: &Value) -> bool {
    value.get("command_type").and_then(Value::as_str) == Some("focused_verifier")
        && command_run_tool_identity(value)
        && ["command", "input"].iter().all(|key| value.get(*key).is_none_or(|command| {
            (*key == "command" && command.as_str() == Some("focused_verifier"))
                || (focused_verifier_identity(command) && command_identities_agree(value, command))
        }))
}

fn successful_focused_verifier(result: &Value, input: Option<&Value>) -> bool {
    if !focused_verifier_identity(result)
        || input.is_some_and(|input| !focused_verifier_identity(input) || !command_identities_agree(result, input))
        || result.get("success").and_then(Value::as_bool) != Some(true)
        || result.get("error").is_some_and(|error| !error.is_null())
    {
        return false;
    }
    let Some(output) = result.get("output").and_then(Value::as_object) else {
        return false;
    };
    if output.get("exit_code").and_then(Value::as_i64) != Some(0) {
        return false;
    }
    if output.get("executor").and_then(Value::as_str) == Some("parent_focused_verifier")
        && !output.contains_key("terminal_receipt")
    {
        return false;
    }
    // The compact, current wire packet contains result.success and exit_code.
    // Producer known_success also checks the raw process facts; if those facts
    // are supplied here, none may contradict that success. Do not invent them.
    successful_verifier_facts(output) && output.get("terminal_receipt").is_none_or(|receipt| {
        if output.get("executor").and_then(Value::as_str) == Some("parent_focused_verifier") {
            successful_parent_verifier_receipt(result, output, receipt)
        } else {
            receipt.as_object().is_some_and(successful_verifier_facts)
        }
    })
}

fn successful_verifier_facts(facts: &serde_json::Map<String, Value>) -> bool {
    ["success", "process_reaped", "process_group_empty", "termination_proven"]
        .iter()
        .all(|key| facts.get(*key).is_none_or(|value| value.as_bool() == Some(true)))
        && [("outcome", "known"), ("terminal_state", "completed"), ("failure_class", "none")]
            .iter()
            .all(|(key, expected)| facts.get(*key).is_none_or(|value| value.as_str() == Some(*expected)))
        && facts.get("exit_code").is_none_or(|value| value.as_i64() == Some(0))
        && facts.get("reconcile_required").is_none_or(|value| value.as_bool() == Some(false))
        && facts.get("error").is_none_or(Value::is_null)
}

fn successful_parent_verifier_receipt(
    result: &Value,
    output: &serde_json::Map<String, Value>,
    receipt: &Value,
) -> bool {
    // The current parent verifier uses "workload" even for known exit-zero success.
    // Admit only its complete observed receipt, never a partial or failed receipt.
    if result.get("error") != Some(&Value::Null)
        || output.get("executor").and_then(Value::as_str) != Some("parent_focused_verifier")
    {
        return false;
    }
    let Some(receipt) = exact_object(receipt, &[
        "schema_version", "exit_code", "outcome", "terminal_state",
        "process_reaped", "process_group_empty", "termination_proven",
        "reconcile_required", "termination_origin", "failure_class",
        "authoritative_publication", "authority_effect", "auto_retry_allowed",
        "call_id", "pid", "replay_semantics", "retry_safe", "staging_authority",
        "stall_timeout_ms", "wall_time_ms", "wall_timeout_ms",
    ]) else {
        return false;
    };
    [
        ("schema_version", "tura_command_terminal_receipt_v1"),
        ("outcome", "known"),
        ("terminal_state", "completed"),
        ("termination_origin", "parent_verifier"),
        ("failure_class", "workload"),
        ("authoritative_publication", "unproven"),
        ("authority_effect", "none"),
        ("staging_authority", "none"),
        ("replay_semantics", "diagnosed_replay_only_after_no_authoritative_publication_or_idempotent_cas_proof"),
    ].iter().all(|(key, expected)| receipt.get(*key).and_then(Value::as_str) == Some(*expected))
        && ["process_reaped", "process_group_empty", "termination_proven"]
            .iter()
            .all(|key| receipt.get(*key).and_then(Value::as_bool) == Some(true))
        && receipt.get("exit_code").and_then(Value::as_i64) == Some(0)
        && receipt.get("reconcile_required").and_then(Value::as_bool) == Some(false)
        && ["auto_retry_allowed", "retry_safe"].iter()
            .all(|key| receipt.get(*key).and_then(Value::as_bool) == Some(false))
        && ["pid", "stall_timeout_ms"].iter()
            .all(|key| receipt.get(*key) == Some(&Value::Null))
        && receipt.get("call_id").and_then(Value::as_str).is_some_and(|id| !id.is_empty())
        && receipt.get("wall_time_ms").and_then(Value::as_u64).is_some()
        && receipt.get("wall_timeout_ms").and_then(Value::as_u64).is_some_and(|ms| ms > 0)
}

fn exact_object<'a>(value: &'a Value, keys: &[&str]) -> Option<&'a serde_json::Map<String, Value>> {
    let map = value.as_object()?;
    (map.len() == keys.len() && keys.iter().all(|key| map.contains_key(*key))).then_some(map)
}

fn source_digest(value: &Value) -> Option<&str> {
    let digest = value.as_str()?;
    (digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit())).then_some(digest)
}

fn source_path(value: &Value) -> Option<&str> {
    let path = value.as_str()?;
    (!path.is_empty() && path.ends_with(".py")
        && !path.chars().any(|ch| ch < ' ' || ch == '\u{7f}' || matches!(ch, '\\' | ':' | '*' | '?' | '[' | ']'))
        && path.split('/').all(|part| !matches!(part, "" | "." | "..")))
        .then_some(path)
}

fn source_range(start: &Value, end: &Value) -> Option<(u64, u64)> {
    let (start, end) = (start.as_u64()?, end.as_u64()?);
    (start > 0 && end >= start).then_some((start, end))
}

fn add_source_lines(total: &mut u64, count: u64) -> Option<()> {
    *total = total.checked_add(count)?;
    (*total <= SOURCE_POSTIMAGE_MAX_LINES).then_some(())
}

fn physical_source_lines(text: &str) -> u64 {
    // io.StringIO(source, newline="").readlines(): CRLF, LF and lone CR,
    // without translating text or treating Unicode separators as newlines.
    let mut lines = 0;
    let mut previous_cr = false;
    for byte in text.bytes() {
        if byte == b'\r' || (byte == b'\n' && !previous_cr) {
            lines += 1;
        }
        previous_cr = byte == b'\r';
    }
    if text.as_bytes().last().is_some_and(|byte| !matches!(byte, b'\r' | b'\n')) {
        lines += 1;
    }
    lines
}

fn valid_delta_navigation_locators(value: &Value, total: &mut usize) -> Option<()> {
    let locators = value.as_array()?;
    *total = total.checked_add(locators.len())?;
    if *total > SOURCE_POSTIMAGE_MAX_LOCATORS {
        return None;
    }
    let mut names = std::collections::HashSet::new();
    let mut previous = None;
    for locator in locators {
        let locator = exact_object(locator, &["qualified_name", "kind", "line", "start_line", "end_line"])?;
        let name = locator["qualified_name"].as_str()?;
        let (start, end) = source_range(&locator["start_line"], &locator["end_line"])?;
        let line = locator["line"].as_u64()?;
        if name.len() > SOURCE_POSTIMAGE_MAX_LOCATOR_NAME_BYTES
            || name.split('.').any(str::is_empty)
            || name.chars().any(|ch| ch < ' ' || ch == '\u{7f}')
            || !names.insert(name)
            || !matches!(locator["kind"].as_str()?, "function" | "async_function" | "class" | "binding")
            || line < start || line > end
        {
            return None;
        }
        let key = (start, end, name);
        if previous.is_some_and(|previous| previous >= key) {
            return None;
        }
        previous = Some(key);
    }
    Some(())
}

fn valid_source_postimages(projection: &Value) -> Option<()> {
    let root = exact_object(projection, &["schema_version", "kind", "jspace_semantic_sha256", "notice", "files"])?;
    let (delta, navigation) = match (root["schema_version"].as_str()?, root["kind"].as_str()?) {
        ("nokiy_source_postimages_v1", "verifier_postimage_context") => (false, false),
        ("nokiy_source_postimage_delta_v1", "verifier_postimage_delta") => (true, false),
        ("nokiy_source_postimage_delta_v2", "verifier_postimage_delta") => (true, true),
        _ => return None,
    };
    source_digest(&root["jspace_semantic_sha256"])?;
    let notice = root["notice"].as_str()?;
    let notice = if navigation { notice.strip_suffix(SOURCE_POSTIMAGE_DELTA_V2_NOTICE_SUFFIX)? } else { notice };
    if notice != if delta { SOURCE_POSTIMAGE_DELTA_NOTICE } else { SOURCE_POSTIMAGES_NOTICE } {
        return None;
    }
    let files = root["files"].as_array()?;
    if files.is_empty() || files.len() > SOURCE_POSTIMAGE_MAX_FILES {
        return None;
    }
    let mut paths = std::collections::HashSet::new();
    let mut lines = 0;
    let mut locator_count = 0;
    for file in files {
        let keys: &[&str] = if navigation {
            &["path", "preimage_sha256", "postimage_sha256", "coverage", "hunks", "locators"]
        } else if delta {
            &["path", "preimage_sha256", "postimage_sha256", "coverage", "hunks"]
        } else {
            &["path", "preimage_sha256", "postimage_sha256", "locators", "spans"]
        };
        let file = exact_object(file, keys)?;
        if !paths.insert(source_path(&file["path"])?)
            || source_digest(&file["preimage_sha256"])?
                .eq_ignore_ascii_case(source_digest(&file["postimage_sha256"])?)
        {
            return None;
        }
        if delta {
            if file["coverage"].as_str()? != "all_text_changes" {
                return None;
            }
            let hunks = file["hunks"].as_array()?;
            if hunks.is_empty() {
                return None;
            }
            let (mut before_end, mut after_end) = (1_u64, 1_u64);
            for hunk in hunks {
                let hunk = exact_object(hunk, &["before_start_line", "before_line_count", "after_start_line", "after_line_count", "before_text", "after_text"])?;
                let before_start = hunk["before_start_line"].as_u64()?;
                let after_start = hunk["after_start_line"].as_u64()?;
                let before_count = hunk["before_line_count"].as_u64()?;
                let after_count = hunk["after_line_count"].as_u64()?;
                if before_start.checked_sub(before_end)? != after_start.checked_sub(after_end)?
                    || (before_count == 0 && after_count == 0)
                    || physical_source_lines(hunk["before_text"].as_str()?) != before_count
                    || physical_source_lines(hunk["after_text"].as_str()?) != after_count
                    || hunk["before_text"] == hunk["after_text"]
                {
                    return None;
                }
                add_source_lines(&mut lines, before_count.checked_add(after_count)?)?;
                before_end = before_start.checked_add(before_count)?;
                after_end = after_start.checked_add(after_count)?;
            }
            if navigation {
                // Navigation is not source/dependency coverage: units may lie
                // outside hunks and their ranges do not consume source lines.
                valid_delta_navigation_locators(&file["locators"], &mut locator_count)?;
            }
        } else {
            let spans = file["spans"].as_array()?;
            let locators = file["locators"].as_array()?;
            if spans.is_empty() || locators.is_empty() {
                return None;
            }
            let mut previous_end = 0;
            let mut ranges = Vec::new();
            for span in spans {
                let span = exact_object(span, &["start_line", "end_line", "text"])?;
                let (start, end) = source_range(&span["start_line"], &span["end_line"])?;
                let count = end.checked_sub(start)?.checked_add(1)?;
                if start <= previous_end || physical_source_lines(span["text"].as_str()?) != count {
                    return None;
                }
                add_source_lines(&mut lines, count)?;
                ranges.push((start, end));
                previous_end = end;
            }
            for locator in locators {
                let locator = exact_object(locator, &["qualified_name", "kind", "line", "start_line", "end_line", "complete"])?;
                let (start, end) = source_range(&locator["start_line"], &locator["end_line"])?;
                let line = locator["line"].as_u64()?;
                if locator["qualified_name"].as_str()?.split('.').any(str::is_empty)
                    || !matches!(locator["kind"].as_str()?, "function" | "async_function" | "class" | "binding")
                    || !locator["complete"].as_bool()?
                    || line < start || line > end
                    || !ranges.iter().any(|(span_start, span_end)| *span_start <= start && end <= *span_end)
                {
                    return None;
                }
            }
        }
    }
    // Python json.dumps(sort_keys=True, ensure_ascii=True), with the default
    // ", " / ": " separators, not serde's shorter UTF-8/compact serialization.
    producer_json_bytes(projection)?;
    Some(())
}

fn producer_json_bytes(value: &Value) -> Option<usize> {
    fn add(total: &mut usize, count: usize) -> Option<()> {
        *total = total.checked_add(count)?;
        (*total <= SOURCE_POSTIMAGE_MAX_JSON_BYTES).then_some(())
    }
    fn string_bytes(text: &str) -> Option<usize> {
        let mut total = 2;
        for ch in text.chars() {
            let count = match ch {
                '"' | '\\' | '\u{8}' | '\u{c}' | '\n' | '\r' | '\t' => 2,
                ' '..='~' => 1,
                '\u{0}'..='\u{ffff}' => 6,
                _ => 12, // Python's escaped UTF-16 surrogate pair.
            };
            add(&mut total, count)?;
        }
        Some(total)
    }
    let mut total = match value {
        Value::Null => 4,
        Value::Bool(true) => 4,
        Value::Bool(false) => 5,
        Value::Number(number) if number.is_i64() || number.is_u64() => number.to_string().len(),
        Value::String(text) => return string_bytes(text),
        Value::Array(items) => {
            let mut total = 2;
            for (index, item) in items.iter().enumerate() {
                if index > 0 { add(&mut total, 2)?; }
                add(&mut total, producer_json_bytes(item)?)?;
            }
            total
        }
        Value::Object(map) => {
            let mut total = 2;
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 { add(&mut total, 2)?; }
                add(&mut total, string_bytes(key)?)?;
                add(&mut total, 2)?;
                add(&mut total, producer_json_bytes(item)?)?;
            }
            total
        }
        _ => return None,
    };
    add(&mut total, 0)?;
    Some(total)
}

fn sanitize_value(value: &Value, key: Option<&str>) -> Value {
    match value {
        Value::String(text) if is_media_payload_string(key, text) => {
            redact_media_payload_string(key)
        }
        Value::String(text) if should_truncate_string(key, text) => {
            Value::String(truncate_middle(text, TOOL_CALLBACK_OUTPUT_MAX_BYTES))
        }
        Value::String(_) | Value::Null | Value::Bool(_) | Value::Number(_) => value.clone(),
        Value::Array(items) => {
            Value::Array(items.iter().map(|item| sanitize_value(item, key)).collect())
        }
        Value::Object(map) => sanitize_object(map),
    }
}

fn sanitize_object(map: &serde_json::Map<String, Value>) -> Value {
    Value::Object(map.iter().map(|(field, item)| {
        (field.clone(), sanitize_object_field(map, field, item))
    }).collect())
}

fn sanitize_object_field(map: &serde_json::Map<String, Value>, field: &str, item: &Value) -> Value {
    match field {
        "source_postimages" => bounded_source_postimages_for_context(item, None, None),
        "visual_previews" => media_channel_summary(map, field, "visual_preview_count", item),
        "audio_previews" => media_channel_summary(map, field, "audio_preview_count", item),
        "file_attachments" => media_channel_summary(map, field, "file_attachment_count", item),
        "command_events" if map.get("results").is_some() => serde_json::json!({
            "omitted_from_record": true,
            "count": array_len_value(item),
            "reason": COMMAND_EVENTS_OMISSION_REASON,
        }),
        _ => sanitize_value(item, Some(field)),
    }
}

fn media_channel_summary(
    map: &serde_json::Map<String, Value>,
    field: &str,
    count_field: &str,
    value: &Value,
) -> Value {
    serde_json::json!({
        "omitted_from_record": true,
        "media_channel": field,
        "count": map
            .get(count_field)
            .cloned()
            .unwrap_or_else(|| array_len_value(value)),
        "reason": MEDIA_CHANNEL_REASON,
    })
}

fn array_len_value(value: &Value) -> Value {
    value
        .as_array()
        .map(|items| Value::Number(items.len().into()))
        .or_else(|| {
            (value.get("omitted_from_record").and_then(Value::as_bool) == Some(true))
                .then(|| value.get("count").and_then(Value::as_u64).map(Value::from))
                .flatten()
        })
        .unwrap_or(Value::Null)
}

fn should_truncate_string(key: Option<&str>, text: &str) -> bool {
    if text.len() <= TOOL_CALLBACK_OUTPUT_MAX_BYTES {
        return false;
    }
    !matches!(
        key,
        Some(
            "command"
                | "command_line"
                | "input"
                | "provider"
                | "runtime_id"
                | "session_id"
                | "id"
                | "call_id"
                | "tool"
                | "tool_name"
        )
    )
}

fn is_media_payload_string(key: Option<&str>, text: &str) -> bool {
    if matches!(key, Some("data_base64")) && !text.is_empty() {
        return true;
    }
    if matches!(
        key,
        Some("file_data" | "image_url" | "url" | "audio_url" | "data")
    ) {
        return contains_base64_data_url(text);
    }
    false
}

fn contains_base64_data_url(text: &str) -> bool {
    text.contains("data:") && text.contains(";base64,")
}

fn redact_media_payload_string(key: Option<&str>) -> Value {
    let label = match key {
        Some("data_base64") => "[redacted base64 media payload]",
        Some("file_data") => "[redacted media file data URL]",
        Some("audio_url") => "[redacted audio media data URL]",
        Some("image_url" | "url") => "[redacted image media data URL]",
        Some("data") => "[redacted media data URL]",
        _ => "[redacted media payload]",
    };
    Value::String(label.to_string())
}

fn truncate_middle(content: &str, max_bytes: usize) -> String {
    if content.len() <= max_bytes {
        return content.to_string();
    }
    let total_lines = content.lines().count();
    let prefix = format!("Total output lines: {total_lines}\n\n");
    // Removed character count cannot have more digits than the byte length.
    // Reserve the header AND marker, then round retained slices inward to UTF-8
    // boundaries. The result is below the trigger without trusting any marker.
    let marker_budget = format!("...{} characters truncated...", content.len()).len();
    let keep_bytes = max_bytes.saturating_sub(prefix.len() + marker_budget);
    let mut head_end = keep_bytes / 2;
    while !content.is_char_boundary(head_end) {
        head_end -= 1;
    }
    let mut tail_start = content.len() - (keep_bytes - keep_bytes / 2);
    while !content.is_char_boundary(tail_start) {
        tail_start += 1;
    }
    let removed = content[head_end..tail_start].chars().count();
    let mut projected = format!(
        "{prefix}{}...{removed} characters truncated...{}",
        &content[..head_end], &content[tail_start..]
    );
    // Also keep the helper bounded for budgets smaller than the marker itself.
    let mut end = max_bytes.min(projected.len());
    while !projected.is_char_boundary(end) {
        end -= 1;
    }
    projected.truncate(end);
    projected
}

#[cfg(test)]
pub(crate) mod tests {
    use super::{
        SOURCE_POSTIMAGE_MAX_JSON_BYTES, SOURCE_READ_RANGE_MAX_RESULT_BYTES,
        SOURCE_READ_RANGE_MAX_TEXT_BYTES, TOOL_CALLBACK_OUTPUT_MAX_BYTES,
        json_within_byte_budget, producer_json_bytes, sanitize_tool_callback_output,
        sanitize_tool_callback_result, truncate_middle,
    };
    use serde_json::json;

    pub(crate) fn source_read_result() -> serde_json::Value {
        let stdout = (69..=248).map(|line| format!("{line}: {}{}\n",
            "x".repeat(50), if line == 69 { "abcdef" } else { "" })).collect::<String>();
        json!({
            "command_type": "source_read", "command_index": 0, "step": 1,
            "success": true, "error": null,
            "output": {
                "exit_code": 0, "stdout": stdout, "stderr": "",
                "path": "tests/test_graph_process.py", "source_sha256": "a".repeat(64),
                "start_line": 69, "end_line": 248, "requested_end_line": 248,
                "next_line": 249, "at_eof": false, "total_lines": 300,
                "file_bytes": 23_106, "line_numbers": true, "ends_with_newline": true,
                "truncated": false, "truncation_reason": null,
                "terminal_receipt_path": "session/receipts/source-page.json",
                "terminal_receipt": {
                    "schema_version": "tura_command_terminal_receipt_v1", "call_id": "source-page",
                    "outcome": "known", "terminal_state": "completed", "exit_code": 0,
                    "termination_origin": "in_process_source_read", "failure_class": "none",
                    "process_reaped": true, "process_group_empty": true, "termination_proven": true,
                    "reconcile_required": false, "authority_effect": "none", "staging_authority": "none",
                    "authoritative_publication": "unproven"
                }
            }
        })
    }

    #[test]
    fn oversized_text_projection_is_a_utf8_byte_bounded_fixed_point() {
        for text in [
            "x".repeat(10_001), "line\n".repeat(4_000),
            "é".repeat(6_000), "🦀".repeat(3_000),
            format!("α{}🦀", "x".repeat(20_000)),
            format!("Total output lines: 1\n\n{}...1 characters truncated...", "x".repeat(10_055)),
        ] {
            let value = json!({"output": {"stdout": text}});
            let projected = sanitize_tool_callback_result(&value);
            let stdout = projected["output"]["stdout"].as_str().unwrap();
            assert!(stdout.len() <= TOOL_CALLBACK_OUTPUT_MAX_BYTES);
            assert_ne!(projected, value, "even a supplied marker must be bounded");
            assert!(stdout.contains("characters truncated"));
            for _ in 0..4 {
                assert_eq!(sanitize_tool_callback_result(&projected), projected);
                assert_eq!(sanitize_tool_callback_output(&projected), projected);
            }
        }
        for budget in 0..128 {
            let projected = truncate_middle(&"🦀".repeat(100), budget);
            assert!(projected.len() <= budget);
            assert_eq!(truncate_middle(&projected, budget), projected);
        }
    }

    #[test]
    fn complete_source_range_pages_keep_text_and_pagination_above_generic_budget() {
        let first = source_read_result();
        assert_eq!(first["output"]["stdout"].as_str().unwrap().len(), 10_055);
        let mut unicode = first.clone();
        unicode["output"]["stdout"] = json!((69..=248).map(|line| format!("{line}: {}{}\n",
            "é".repeat(25), if line == 69 { "abcdef" } else { "" })).collect::<String>());
        let mut plain = first.clone();
        let _ = plain["output"].as_object_mut().unwrap().remove("line_numbers");
        plain["output"]["stdout"] = json!(format!("{}\n", "x".repeat(60)).repeat(180));
        let mut byte_limited = first.clone();
        byte_limited["output"]["requested_end_line"] = json!(300);
        byte_limited["output"]["truncated"] = json!(true);
        byte_limited["output"]["truncation_reason"] = json!("response_limit");
        let mut line_limited = byte_limited.clone();
        line_limited["output"]["start_line"] = json!(49);
        line_limited["output"]["stdout"] = json!((49..=248).map(|line| format!("{line}: {}{}\n",
            "x".repeat(50), if line == 69 { "abcdef" } else { "" })).collect::<String>());
        line_limited["output"]["truncation_reason"] = json!("line_limit");
        let mut resumed = first.clone();
        resumed["output"]["start_line"] = first["output"]["next_line"].clone();
        resumed["output"]["end_line"] = json!(300);
        resumed["output"]["requested_end_line"] = json!(300);
        resumed["output"]["next_line"] = serde_json::Value::Null;
        resumed["output"]["at_eof"] = json!(true);
        resumed["output"]["stdout"] = json!((249..=300).map(|line|
            format!("{line}: {}\n", "x".repeat(200))).collect::<String>());
        let mut without_newline = resumed.clone();
        without_newline["output"]["stdout"] = json!(resumed["output"]["stdout"].as_str().unwrap().trim_end_matches('\n'));
        without_newline["output"]["ends_with_newline"] = json!(false);
        without_newline["output"]["file_bytes"] = json!(23_105);
        let mut eof = resumed.clone();
        eof["output"]["requested_end_line"] = json!(400);
        eof["output"]["truncated"] = json!(true);
        eof["output"]["truncation_reason"] = json!("end_of_file");
        for result in [first, unicode, plain, byte_limited, line_limited, resumed, without_newline, eof] {
            assert!(result["output"]["stdout"].as_str().unwrap().len() > TOOL_CALLBACK_OUTPUT_MAX_BYTES);
            assert!(json_within_byte_budget(&result["output"]["stdout"], SOURCE_READ_RANGE_MAX_TEXT_BYTES));
            assert!(json_within_byte_budget(&result["output"], SOURCE_READ_RANGE_MAX_RESULT_BYTES));
            for _ in 0..4 {
                assert_eq!(sanitize_tool_callback_result(&result), result);
                assert_eq!(sanitize_tool_callback_output(&result), result);
            }
        }
    }

    #[test]
    fn source_page_streamed_records_and_canonical_batches_project_identically() {
        let result = source_read_result();
        let command = json!({"command_type": "source_read", "step": 1, "command_index": 0});
        let event = crate::provider_flow::streamed_command_run::streamed_command_result_record(
            "completed", "runtime", 0, &result, chrono::Utc::now());
        let recorded = sanitize_tool_callback_result(&event);
        assert_eq!(recorded, event);
        assert_eq!(sanitize_tool_callback_result(&recorded), recorded);
        for batch in [
            json!({"results": [result]}),
            json!({"streamed_command_run_result": {"results": [result]}}),
            json!({"tool_name": "command_run", "input": {"commands": [command]},
                "output": {"commands": [command], "results": [result], "command_events": [event]}}),
        ] {
            let projected = sanitize_tool_callback_output(&batch);
            let channel = projected.get("output").unwrap_or(&projected);
            let channel = channel.get("streamed_command_run_result").unwrap_or(channel);
            assert_eq!(channel["results"][0], recorded["result"]);
            assert_eq!(sanitize_tool_callback_output(&projected), projected);
        }
        for untyped in [
            result["output"].clone(), json!({"advisory": result}),
            json!({"tool_name": "other_tool", "output": {"results": [result]}}),
        ] {
            let projected = sanitize_tool_callback_output(&untyped);
            assert!(projected.to_string().contains("characters truncated"));
            assert_eq!(sanitize_tool_callback_output(&projected), projected);
        }
    }

    fn assert_source_page_rejected(result: &serde_json::Value) {
        let projected = sanitize_tool_callback_result(result);
        assert!(projected["output"]["stdout"].as_str().unwrap().len() <= TOOL_CALLBACK_OUTPUT_MAX_BYTES);
        assert_ne!(projected["output"]["stdout"], result["output"]["stdout"]);
        assert_eq!(sanitize_tool_callback_result(&projected), projected);
    }

    #[test]
    fn source_page_bypass_rejects_wrong_identities_shapes_and_budgets() {
        let result = source_read_result();
        for (pointer, replacement) in [
            ("/command_type", json!("shell_command")), ("/success", json!(false)),
            ("/step", json!(0)), ("/command_index", json!("0")),
            ("/error", json!("unsettled")), ("/output/exit_code", json!(1)),
            ("/output/stderr", json!("unexpected")), ("/output/path", json!("../file.rs")),
            ("/output/source_sha256", json!("g".repeat(64))), ("/output/start_line", json!("69")),
            ("/output/end_line", json!(249)), ("/output/requested_end_line", json!(247)),
            ("/output/next_line", json!(250)), ("/output/at_eof", json!(true)),
            ("/output/ends_with_newline", json!(false)), ("/output/line_numbers", json!(false)),
            ("/output/truncated", json!(true)), ("/output/truncation_reason", json!("response_limit")),
            ("/output/file_bytes", json!(1_048_577)),
            ("/output/terminal_receipt/termination_origin", json!("in_process_shell")),
            ("/output/terminal_receipt/reconcile_required", json!(true)),
        ] {
            let mut invalid = result.clone();
            *invalid.pointer_mut(pointer).unwrap() = replacement;
            assert_source_page_rejected(&invalid);
        }
        for key in ["tool", "tool_name"] {
            let mut wrong_tool = result.clone();
            wrong_tool[key] = json!("other_tool");
            assert_source_page_rejected(&wrong_tool);
        }
        for key in ["command", "input"] {
            let mut contradictory = result.clone();
            contradictory[key] = json!({"command_type": "shell_command", "step": 1});
            assert_source_page_rejected(&contradictory);
        }
        let mut missing = result.clone();
        let _ = missing["output"].as_object_mut().unwrap().remove("source_sha256");
        assert_source_page_rejected(&missing);
        let mut extra = result.clone();
        extra["output"]["arbitrary_payload"] = json!("not a producer field");
        assert_source_page_rejected(&extra);
        let mut mislabeled = result.clone();
        mislabeled["output"]["stdout"] = json!(result["output"]["stdout"].as_str().unwrap().replacen("69: ", "68: ", 1));
        assert_source_page_rejected(&mislabeled);
        let mut raw_overflow = result.clone();
        raw_overflow["output"]["stdout"] = json!((69..=248).map(|line|
            format!("{line}: {}\n", "x".repeat(70))).collect::<String>());
        assert!(raw_overflow["output"]["stdout"].as_str().unwrap().len() > SOURCE_READ_RANGE_MAX_TEXT_BYTES);
        assert_source_page_rejected(&raw_overflow);
        let mut escaped_overflow = result.clone();
        escaped_overflow["output"]["stdout"] = json!(result["output"]["stdout"].as_str().unwrap().replace('x', "\\"));
        assert!(escaped_overflow["output"]["stdout"].as_str().unwrap().len() <= SOURCE_READ_RANGE_MAX_TEXT_BYTES);
        assert!(!json_within_byte_budget(&escaped_overflow["output"]["stdout"], SOURCE_READ_RANGE_MAX_TEXT_BYTES));
        assert_source_page_rejected(&escaped_overflow);
        let mut envelope_overflow = result.clone();
        envelope_overflow["output"]["terminal_receipt_path"] = json!("p".repeat(7_000));
        assert!(!json_within_byte_budget(&envelope_overflow["output"], SOURCE_READ_RANGE_MAX_RESULT_BYTES));
        assert_source_page_rejected(&envelope_overflow);
        for (field, replacement) in [
            ("command_type", json!("shell_command")), ("step", json!(2)),
            ("command_index", json!(1)), ("tool_name", json!("other_tool")),
        ] {
            let mut command = json!({"command_type": "source_read", "step": 1, "command_index": 0});
            command[field] = replacement;
            let batch = json!({"tool_name": "command_run", "input": {"commands": [command]},
                "output": {"results": [result]}});
            let projected = sanitize_tool_callback_output(&batch);
            let rejected = &projected["output"]["results"][0];
            assert!(rejected["output"]["stdout"].as_str().unwrap().len() <= TOOL_CALLBACK_OUTPUT_MAX_BYTES);
            assert_ne!(rejected["output"]["stdout"], result["output"]["stdout"]);
            assert_eq!(sanitize_tool_callback_output(&projected), projected);
        }
    }

    #[test]
    fn source_page_json_budget_matches_compact_serde_utf8_and_escaping() {
        for value in [json!(null), json!(1.25), json!([true, false, 0]),
            json!({"é🦀": "\"\\\u{1}\n\r\t"})] {
            let bytes = serde_json::to_vec(&value).unwrap().len();
            assert!(json_within_byte_budget(&value, bytes));
            assert!(!json_within_byte_budget(&value, bytes - 1));
            assert!(json_within_byte_budget(&value, bytes + 1));
        }
    }

    #[test]
    fn source_postimage_json_budget_matches_python_ascii_and_default_separators() {
        assert_eq!(producer_json_bytes(&json!("\"\\\u{8}\u{c}\n\r\t\u{1}\u{7f}é🦀")), Some(46));
        assert_eq!(producer_json_bytes(&json!([0, true, null, "é"])), Some(25));
        assert_eq!(producer_json_bytes(&json!({"é": [0, true, null, "🦀"]})), Some(43));
        assert_eq!(
            producer_json_bytes(&json!("x".repeat(SOURCE_POSTIMAGE_MAX_JSON_BYTES - 2))),
            Some(SOURCE_POSTIMAGE_MAX_JSON_BYTES),
        );
        assert_eq!(producer_json_bytes(&json!("x".repeat(SOURCE_POSTIMAGE_MAX_JSON_BYTES - 1))), None);
        let escaped_overflow = json!("🦀".repeat(3_000));
        assert!(serde_json::to_string(&escaped_overflow).unwrap().len() < SOURCE_POSTIMAGE_MAX_JSON_BYTES);
        assert_eq!(producer_json_bytes(&escaped_overflow), None);
    }

    #[test]
    fn sanitizer_truncates_large_output_strings_but_keeps_command_line() {
        let large = "line\n".repeat(4_000);
        let value = json!({
            "command_line": large,
            "output": large,
        });

        let sanitized = sanitize_tool_callback_output(&value);

        assert_eq!(sanitized["command_line"], value["command_line"]);
        let output = sanitized["output"].as_str().expect("output string");
        assert!(output.len() < large.len(), "{output}");
        assert!(output.contains("characters truncated"), "{output}");
    }

    #[test]
    fn sanitizer_redacts_media_payloads_to_single_channel_summaries() {
        let image_url = format!("data:image/jpeg;base64,{}", "A".repeat(20_000));
        let audio_url = format!("data:audio/mpeg;base64,{}", "B".repeat(20_000));
        let file_data = format!("data:application/pdf;base64,{}", "C".repeat(20_000));
        let data_base64 = "D".repeat(20_000);
        let value = json!({
            "output": "ordinary\n".repeat(4_000),
            "media_results": [{
                "visual_previews": [{
                    "type": "image_url",
                    "image_url": { "url": image_url }
                }],
                "audio_previews": [{
                    "type": "audio_url",
                    "audio_url": { "url": audio_url }
                }],
                "file_attachments": [{
                    "mime_type": "application/pdf",
                    "data_base64": data_base64
                }]
            }],
            "input_file": {
                "file_data": file_data
            }
        });

        let sanitized = sanitize_tool_callback_output(&value);

        assert_eq!(
            sanitized["media_results"][0]["visual_previews"]["media_channel"],
            "visual_previews"
        );
        assert_eq!(
            sanitized["media_results"][0]["audio_previews"]["media_channel"],
            "audio_previews"
        );
        assert_eq!(
            sanitized["media_results"][0]["file_attachments"]["media_channel"],
            "file_attachments"
        );
        assert_eq!(
            sanitized["input_file"]["file_data"],
            "[redacted media file data URL]"
        );
        let serialized = serde_json::to_string(&sanitized).expect("sanitized json");
        assert!(!serialized.contains("data:image/jpeg;base64"));
        assert!(!serialized.contains("data:audio/mpeg;base64"));
        assert!(!serialized.contains("data:application/pdf;base64"));
        assert!(!serialized.contains(&"D".repeat(1_000)));
        assert!(
            sanitized["output"]
                .as_str()
                .is_some_and(|output| output.contains("characters truncated"))
        );
    }

    #[test]
    fn sanitizer_counts_command_events_only_once_when_results_are_present() {
        let value = json!({
            "results": [{
                "step": 1,
                "command_type": "read_media",
                "output": {
                    "visual_previews": [{
                        "type": "image_url",
                        "image_url": { "url": "data:image/png;base64,AAA" }
                    }]
                }
            }],
            "command_events": [{
                "result": {
                    "output": {
                        "visual_previews": [{
                            "type": "image_url",
                            "image_url": { "url": "data:image/png;base64,AAA" }
                        }]
                    }
                }
            }]
        });

        let sanitized = sanitize_tool_callback_output(&value);

        assert_eq!(sanitized["command_events"]["omitted_from_record"], true);
        assert_eq!(sanitized["command_events"]["count"], 1);
        assert_eq!(sanitize_tool_callback_output(&sanitized), sanitized);
        let serialized = serde_json::to_string(&sanitized).expect("sanitized json");
        assert!(!serialized.contains("data:image/png;base64"));
    }

    fn verifier_proof_result() -> serde_json::Value {
        json!({
            "command_type": "focused_verifier", "step": 1, "success": true, "error": null,
            "output": {
                "executor": "parent_focused_verifier", "exit_code": 0,
                "terminal_receipt": {"call_id": "proof-call"},
                "verification_evidence": {
                    "schema_version": "nokiy_focused_verifier_evidence_v1",
                    "authorization_semantic_sha256": "a".repeat(64), "verifier_index": 0,
                    "verifier_sha256": "b".repeat(64), "call_id": "proof-call",
                    "source_postimages": {"src/example.rs": {"sha256": "c".repeat(64), "bytes": 28, "mode": 420}}
                }
            }
        })
    }

    #[test]
    fn sanitizer_preserves_verifier_proof_only_at_typed_canonical_sites() {
        let result = verifier_proof_result();
        let proof = &result["output"]["verification_evidence"];
        assert_eq!(sanitize_tool_callback_output(&result), result);
        let batch = json!({"results": [result]});
        assert_eq!(sanitize_tool_callback_output(&batch), batch);
        let completed = json!({
            "status": "completed", "runtime_id": "runtime", "command_index": 0,
            "result_index": 0, "success": true, "result": result
        });
        assert_eq!(sanitize_tool_callback_output(&completed), completed);
        assert_eq!(sanitize_tool_callback_output(&sanitize_tool_callback_output(&batch)), batch);
        let advisory = sanitize_tool_callback_output(&json!({"advisory": result}));
        assert_eq!(advisory["advisory"]["output"]["verification_evidence"]["source_postimages"]["omitted_from_context"], true);
        assert_eq!(sanitize_tool_callback_output(proof)["source_postimages"]["omitted_from_context"], true);
        let mut wrong_tool = result.clone();
        wrong_tool["command_type"] = json!("source_read");
        assert_eq!(sanitize_tool_callback_output(&wrong_tool)["output"]["verification_evidence"]["source_postimages"]["omitted_from_context"], true);
    }

    #[test]
    fn sanitizer_preserves_native_streamed_verifier_proof_bindings() {
        let command = json!({
            "command_type": "focused_verifier", "command_line": "{\"verifier_index\":0}", "step": 1,
            "command_id": "proof-command", "command_run_id": "proof-run",
            "provider_tool_call_id": "proof-provider", "command_index": 0
        });
        let mut result = verifier_proof_result();
        for key in ["command_id", "command_run_id", "provider_tool_call_id", "command_index"] {
            result[key] = command[key].clone();
        }
        result["command"] = command.clone();
        let now = chrono::Utc::now();
        let completed = crate::provider_flow::streamed_command_run::streamed_command_result_record(
            "completed", "runtime", 0, &result, now,
        );
        let command_event = crate::provider_flow::streamed_command_run::streamed_command_event_record(
            "completed", "runtime", "proof-provider", 0, &command, Some(&result), now,
        );
        for event in [&completed, &command_event] {
            assert_eq!(sanitize_tool_callback_output(event), *event);
            assert_eq!(sanitize_tool_callback_output(&sanitize_tool_callback_output(event)), *event);
        }
        for (key, value) in [
            ("command_type", json!("source_read")), ("step", json!(2)),
            ("command_id", json!("other-command")), ("command_run_id", json!("other-run")),
            ("provider_tool_call_id", json!("other-provider")), ("command_index", json!(1)),
            ("tool", json!("source_read")), ("input", json!({"command_type": "source_read"})),
        ] {
            let mut conflicting = completed.clone();
            conflicting[key] = value;
            let sanitized = sanitize_tool_callback_output(&conflicting);
            assert_eq!(sanitized["result"]["output"]["verification_evidence"]["source_postimages"]["omitted_from_context"], true, "{key}");
        }
    }

    #[test]
    fn sanitizer_does_not_exempt_malformed_or_unbounded_verifier_proofs() {
        for scenario in 0..7 {
            let mut result = verifier_proof_result();
            let proof = &mut result["output"]["verification_evidence"];
            match scenario {
                0 => proof["unexpected"] = json!(true),
                1 => proof["call_id"] = json!("other-call"),
                2 => proof["call_id"] = json!("x".repeat(257)),
                3 => proof["source_postimages"]["src/example.rs"]["mode"] = json!(0o200000),
                4 => proof["source_postimages"]["src/example.rs"]["bytes"] = json!(u64::MAX),
                5 => proof["source_postimages"]["src/example.rs"]["sha256"] = json!("invalid"),
                _ => proof["source_postimages"] = serde_json::Value::Object(
                    (0..=super::TerminalVerifierProof::MAX_SOURCE_TARGETS).map(|index|
                        (format!("src/{index}.rs"), json!({"sha256": "c".repeat(64), "bytes": 28, "mode": 420}))).collect()),
            }
            let sanitized = sanitize_tool_callback_output(&result);
            assert_eq!(sanitized["output"]["verification_evidence"]["source_postimages"]["omitted_from_context"], true, "scenario {scenario}");
            assert!(sanitized["output"].get("verification_evidence").is_some(), "malformed proofs must remain visible to the tracker");
        }
    }
}
