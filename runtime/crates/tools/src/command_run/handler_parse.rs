use serde_json::Value;

use super::CommandItem;

pub(super) fn parse_arguments_value(arguments: &Value) -> Result<Value, String> {
    let value = match arguments {
        Value::String(text) => parse_jsonish_value(text)
            .map_err(|err| format!("failed to parse command_run arguments: {err}"))?,
        other => other.clone(),
    };
    Ok(value.get("requests").cloned().unwrap_or(value))
}

pub(super) fn command_values(value: &Value) -> Vec<Value> {
    match value {
        Value::Array(items) => items.clone(),
        Value::Object(_) | Value::String(_) => vec![value.clone()],
        _ => Vec::new(),
    }
}

pub(super) fn parse_command_item(value: &Value) -> Result<CommandItem, String> {
    if let Some(text) = value.as_str() {
        return Ok(CommandItem {
            index: 0,
            command: text.to_string(),
            command_line: String::new(),
            inline_arguments: None,
            workdir: None,
            step: None,
            timeout_ms: None,
            stall_timeout_ms: None,
            binding_id: None,
        });
    }
    let Some(object) = value.as_object() else {
        return Err("failed to parse command_run command: expected object".to_string());
    };
    let command = string_field(
        object,
        &[
            "command_type",
            "commandType",
            "command",
            "cmd",
            "tool",
            "name",
            "tool_name",
            "toolName",
            "tool_package_name",
            "toolPackageName",
        ],
    )
    .or_else(|| {
        string_field(
            object,
            &[
                "command_line",
                "commandLine",
                "command_code",
                "commandCode",
                "input",
                "args",
                "code",
                "script",
                "payload",
            ],
        )
        .map(|_| crate::commands::active_shell_command_name().to_string())
    })
    .ok_or_else(|| {
        "failed to parse command_run command: missing field `command_type`".to_string()
    })?;
    let command_line = string_field(
        object,
        &[
            "command_line",
            "commandLine",
            "command_code",
            "commandCode",
            "input",
            "args",
            "code",
            "script",
            "payload",
        ],
    )
    .or_else(|| {
        // Some models name the command in `command_type` and put the argument
        // payload in `command` (e.g. task_status carrying a `{status,...}` JSON
        // blob). Recover that payload as the command_line so it is not dropped.
        if object.contains_key("command_type") || object.contains_key("commandType") {
            string_field(object, &["command", "cmd"])
                .filter(|payload| payload.trim() != command.trim())
        } else {
            None
        }
    })
    .unwrap_or_default();
    let inline_arguments = inline_command_arguments(object);
    let command = if shell_typed_source_read(&command, &command_line) {
        "source_read".to_string()
    } else {
        command
    };
    Ok(CommandItem {
        index: 0,
        command,
        command_line,
        inline_arguments,
        workdir: string_field(object, &["workdir", "cwd"]),
        step: u64_field(object, &["step"]),
        timeout_ms: u64_field(object, &["timeout_ms", "timeoutMs"]),
        stall_timeout_ms: u64_field(object, &["stall_timeout_ms", "stallTimeoutMs"]),
        binding_id: string_field(object, &["id", "command_id", "commandId", "result_id"]),
    })
}

/// Fields only an exact source_read request carries; a shell JSON invocation names `command`.
const SOURCE_READ_ONLY_FIELDS: &[&str] = &[
    "search_terms",
    "start_line",
    "end_line",
    "json_pointer",
    "children_offset",
    "context_lines",
    "line_numbers",
    "expected_sha256",
    "expected_sha256_ref",
];

/// Models sometimes label an exact source_read request as the shell, for example
/// `{"command_type":"zsh","command_line":"{\"path\":...,\"search_terms\":[...]}"}`. The shell
/// policy then refuses the JSON as unsafe syntax and the model pays another round. A shell-typed
/// JSON object with a string `path`, a source_read-only field and no `command`/`cmd` key can
/// only mean source_read, so it is retyped and meets source_read's own J-Space admission.
fn shell_typed_source_read(command: &str, command_line: &str) -> bool {
    if crate::commands::canonical_command(command) != crate::commands::active_shell_command_name() {
        return false;
    }
    let Ok(Value::Object(request)) = serde_json::from_str::<Value>(command_line.trim()) else {
        return false;
    };
    request.get("path").is_some_and(Value::is_string)
        && !request.contains_key("command")
        && !request.contains_key("cmd")
        && SOURCE_READ_ONLY_FIELDS.iter().any(|field| request.contains_key(*field))
}

pub(super) fn string_field(
    object: &serde_json::Map<String, Value>,
    names: &[&str],
) -> Option<String> {
    names.iter().find_map(|name| {
        object.get(*name).and_then(|value| match value {
            Value::String(text) if !text.trim().is_empty() => Some(text.to_string()),
            Value::Object(_) | Value::Array(_) => Some(value.to_string()),
            _ => None,
        })
    })
}

pub(super) fn u64_field(object: &serde_json::Map<String, Value>, names: &[&str]) -> Option<u64> {
    names.iter().find_map(|name| {
        object.get(*name).and_then(|value| {
            value
                .as_u64()
                .or_else(|| value.as_str().and_then(|text| text.parse::<u64>().ok()))
        })
    })
}

fn inline_command_arguments(object: &serde_json::Map<String, Value>) -> Option<Value> {
    for name in [
        "arguments",
        "argument",
        "parameters",
        "parameter",
        "params",
        "options",
        "input_json",
        "inputJson",
    ] {
        if let Some(value) = object.get(name) {
            return Some(value.clone());
        }
    }

    let mut arguments = object.clone();
    for name in [
        "command_type",
        "commandType",
        "command",
        "cmd",
        "tool",
        "name",
        "tool_name",
        "toolName",
        "tool_package_name",
        "toolPackageName",
        "command_line",
        "commandLine",
        "command_code",
        "commandCode",
        "input",
        "args",
        "code",
        "script",
        "payload",
        "workdir",
        "cwd",
        "step",
        "timeout_ms",
        "timeoutMs",
        "id",
        "command_id",
        "commandId",
        "result_id",
    ] {
        arguments.remove(name);
    }
    (!arguments.is_empty()).then_some(Value::Object(arguments))
}

fn parse_jsonish_value(text: &str) -> Result<Value, serde_json::Error> {
    let trimmed = text.trim();
    if let Some(unfenced) = strip_json_code_fence(trimmed)
        && let Ok(value) = serde_json::from_str(unfenced.trim())
    {
        return Ok(value);
    }
    serde_json::from_str(trimmed)
}

fn strip_json_code_fence(text: &str) -> Option<&str> {
    let stripped = text.strip_prefix("```")?;
    let newline = stripped.find('\n')?;
    let body = &stripped[newline + 1..];
    let end = body.rfind("```")?;
    Some(&body[..end])
}
