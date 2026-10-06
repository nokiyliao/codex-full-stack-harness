use std::collections::BTreeSet;
use std::path::PathBuf;

use crate::prompt_style::task_status;
use crate::state_machine::agent_management::AgentManagement;
use lifecycle::SessionManagement;

use super::constants::{
    COMMAND_RUN_TOOL, DISABLE_EXECUTE_TOOLS_TOOL_ENV, DISABLE_PLANNING_TOOL_ENV, PROJECT_ROOT_ENV,
    RELEASE_ROOT_ENV, TASK_STATUS_COMMAND,
};

pub(super) fn load_agent_capabilities_with_commands(
    agent: &AgentManagement,
    session: &SessionManagement,
    allowed_commands: &BTreeSet<String>,
) -> Result<Vec<serde_json::Value>, String> {
    load_agent_capabilities_for_task_state(
        agent,
        allowed_commands,
        startup_task_state_required(session, allowed_commands),
        session.jspace_contract.as_ref(),
    )
}

pub(crate) fn startup_task_state_required(
    session: &SessionManagement,
    allowed_commands: &BTreeSet<String>,
) -> bool {
    session.task_type.is_empty() && allowed_commands.contains(TASK_STATUS_COMMAND)
}

fn load_agent_capabilities_for_task_state(
    agent: &AgentManagement,
    allowed_commands: &BTreeSet<String>,
    require_startup_task_state: bool,
    jspace_contract: Option<&serde_json::Value>,
) -> Result<Vec<serde_json::Value>, String> {
    let Some(command_run_directory) = command_run_capability_directory(agent)? else {
        return Ok(Vec::new());
    };
    let interface_path = command_run_directory
        .join(COMMAND_RUN_TOOL)
        .join("schema.json");
    if !interface_path.exists() {
        return Ok(Vec::new());
    }

    let content = std::fs::read_to_string(&interface_path)
        .map_err(|e| format!("failed to read tool interface: {e}"))?;
    let interface = serde_json::from_str::<serde_json::Value>(&content)
        .map_err(|e| format!("failed to parse tool interface: {e}"))?;

    Ok(vec![
        tool_interface_to_provider_schema_with_commands_and_jspace(
            interface,
            Some(allowed_commands),
            require_startup_task_state,
            jspace_contract,
        ),
    ])
}

pub(crate) fn filter_tools_for_turn(
    tools: Vec<serde_json::Value>,
    _is_final_turn: bool,
    _force_no_tools: bool,
) -> Result<Vec<serde_json::Value>, String> {
    Ok(keep_command_run_only(tools))
}

pub(super) fn keep_command_run_only(tools: Vec<serde_json::Value>) -> Vec<serde_json::Value> {
    tools
        .into_iter()
        .filter(|tool| tool_schema_name(tool) == Some(COMMAND_RUN_TOOL))
        .collect()
}

pub(super) fn tool_schema_name(tool: &serde_json::Value) -> Option<&str> {
    tool.get("function")
        .and_then(|function| function.get("name"))
        .and_then(|name| name.as_str())
}

pub(crate) fn env_flag(name: &str) -> bool {
    std::env::var(name)
        .ok()
        .map(|value| {
            let value = value.trim().to_ascii_lowercase();
            matches!(value.as_str(), "1" | "true" | "yes" | "on")
        })
        .unwrap_or(false)
}

pub(super) fn planning_tool_disabled() -> bool {
    env_flag(DISABLE_PLANNING_TOOL_ENV) || env_flag(DISABLE_EXECUTE_TOOLS_TOOL_ENV)
}

pub(crate) fn planning_child_depth() -> usize {
    std::env::var("TURA_PLANNING_DEPTH")
        .or_else(|_| std::env::var("TURA_EXECUTE_TOOLS_DEPTH"))
        .ok()
        .and_then(|value| value.trim().parse::<usize>().ok())
        .unwrap_or(0)
}

pub(crate) fn project_directory_with_tools() -> Result<PathBuf, String> {
    if let Ok(root) = std::env::var(PROJECT_ROOT_ENV) {
        let root = PathBuf::from(root);
        if root
            .join("crates")
            .join("tools")
            .join("src")
            .join("command_run")
            .join("schema.json")
            .exists()
        {
            return Ok(root);
        }
    }

    if let Ok(root) = std::env::var(RELEASE_ROOT_ENV) {
        let root = PathBuf::from(root);
        if root
            .join("crates")
            .join("tools")
            .join("src")
            .join("command_run")
            .join("schema.json")
            .exists()
        {
            return Ok(root);
        }
    }

    if let Some(root) = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(std::path::Path::to_path_buf))
        && root
            .join("crates")
            .join("tools")
            .join("src")
            .join("command_run")
            .join("schema.json")
            .exists()
    {
        return Ok(root);
    }

    let current = std::env::current_dir()
        .map_err(|err| format!("failed to resolve project directory: {err}"))?;
    for candidate in current.ancestors() {
        if candidate
            .join("crates")
            .join("tools")
            .join("src")
            .join("command_run")
            .join("schema.json")
            .exists()
        {
            return Ok(candidate.to_path_buf());
        }
    }
    Ok(current)
}

fn command_run_capability_directory(agent: &AgentManagement) -> Result<Option<PathBuf>, String> {
    if agent.agent_capabilities.is_empty() && code_tools::registry::forced_command_ids().is_empty()
    {
        return Ok(None);
    }

    let fallback_directory = project_directory_with_tools()?
        .join("crates")
        .join("tools")
        .join("src");
    Ok(command_run_capability_directory_with_fallback(
        agent,
        fallback_directory,
    ))
}

fn command_run_capability_directory_with_fallback(
    agent: &AgentManagement,
    fallback_directory: PathBuf,
) -> Option<PathBuf> {
    let has_command_run_schema = |directory: &PathBuf| {
        directory
            .join(COMMAND_RUN_TOOL)
            .join("schema.json")
            .is_file()
    };

    if let Some(capability) = agent
        .agent_capabilities
        .iter()
        .find(|capability| capability.capability_name == COMMAND_RUN_TOOL)
        .filter(|capability| has_command_run_schema(&capability.capability_directory))
    {
        return Some(capability.capability_directory.clone());
    }

    if let Some(capability) = agent
        .agent_capabilities
        .first()
        .filter(|capability| has_command_run_schema(&capability.capability_directory))
    {
        return Some(capability.capability_directory.clone());
    }

    Some(fallback_directory)
}

#[cfg(test)]
pub(super) fn tool_interface_to_provider_schema(interface: serde_json::Value) -> serde_json::Value {
    tool_interface_to_provider_schema_with_commands_and_jspace(interface, None, false, None)
}

pub(crate) fn command_run_commands_for_agent(agent: &AgentManagement) -> BTreeSet<String> {
    let mut commands = agent
        .agent_capabilities
        .iter()
        .filter_map(|capability| {
            let name = code_tools::commands::canonical_command(&capability.capability_name);
            (name != COMMAND_RUN_TOOL).then_some(name)
        })
        .collect::<BTreeSet<_>>();

    commands.extend(code_tools::registry::forced_command_ids());

    if commands.is_empty() && !agent.agent_capabilities.is_empty() {
        commands = default_command_run_commands();
    }
    commands
}

pub(crate) fn extend_command_run_commands_with_capabilities<I, S>(
    commands: &mut BTreeSet<String>,
    capabilities: I,
) where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    for capability in capabilities {
        let name = code_tools::commands::canonical_command(capability.as_ref());
        if name != COMMAND_RUN_TOOL {
            commands.insert(name);
        }
    }
}

pub(crate) fn authorize_source_read_command(
    commands: &mut BTreeSet<String>,
    session: &SessionManagement,
) {
    commands.remove("source_read");
    commands.remove("focused_verifier");
    if let Some(contract) = session.jspace_contract.as_ref()
        && let Ok(matcher) =
            tura_path::jspace::JSpaceMatcher::from_value(&session.session_directory, contract)
    {
        if matcher.source_read_enabled() {
            commands.insert("source_read".to_string());
        }
        if !matcher.verifier_commands().is_empty() {
            commands.insert("focused_verifier".to_string());
        }
    }
}

pub(crate) fn provider_command_run_commands_for_jspace(
    execution_commands: &BTreeSet<String>,
    jspace_contract: Option<&serde_json::Value>,
) -> BTreeSet<String> {
    let Some(contract) = jspace_contract else {
        return execution_commands.clone();
    };
    let Some(allowed_values) = contract
        .get("allowed_operations")
        .and_then(serde_json::Value::as_array)
    else {
        return execution_commands.clone();
    };
    if allowed_values.iter().any(|value| !value.is_string()) {
        return execution_commands.clone();
    }
    let allowed = allowed_values
        .iter()
        .filter_map(serde_json::Value::as_str)
        .collect::<BTreeSet<_>>();
    let denied = match contract.get("denied_operations") {
        None => BTreeSet::new(),
        Some(value) => {
            let Some(values) = value.as_array() else {
                return execution_commands.clone();
            };
            if values.iter().any(|value| !value.is_string()) {
                return execution_commands.clone();
            }
            values
                .iter()
                .filter_map(serde_json::Value::as_str)
                .collect::<BTreeSet<_>>()
        }
    };

    let has_command_templates = contract
        .get("command_templates")
        .and_then(serde_json::Value::as_array)
        .is_some_and(|commands| !commands.is_empty());
    let has_read_commands = contract
        .get("read_commands")
        .is_some_and(serde_json::Value::is_object);
    let has_write_scopes = contract
        .get("write_scopes")
        .and_then(serde_json::Value::as_array)
        .is_some_and(|scopes| !scopes.is_empty());

    let mut visible = execution_commands.clone();
    let shell_is_admitted = allowed.contains("command")
        && !denied.contains("command")
        && (has_command_templates || has_read_commands);
    if !shell_is_admitted {
        visible.remove(active_shell_command_name());
    }

    let patch_is_admitted = has_write_scopes
        && ["create", "modify"]
            .into_iter()
            .any(|operation| allowed.contains(operation) && !denied.contains(operation));
    if !patch_is_admitted {
        visible.remove("apply_patch");
    }

    if !allowed.contains("network") || denied.contains("network") {
        visible.remove("web_discover");
    }

    if contract
        .get("source_read")
        .and_then(serde_json::Value::as_bool)
        != Some(true)
    {
        visible.remove("source_read");
    }

    if contract
        .get("verifier_commands")
        .and_then(serde_json::Value::as_array)
        .is_none_or(|commands| commands.is_empty())
    {
        visible.remove("focused_verifier");
    }
    visible
}

fn default_command_run_commands() -> BTreeSet<String> {
    [
        "apply_patch",
        active_shell_command_name(),
        "web_discover",
        "task_status",
    ]
    .into_iter()
    .map(str::to_string)
    .collect::<BTreeSet<_>>()
}

#[cfg(test)]
fn tool_interface_to_provider_schema_with_commands(
    interface: serde_json::Value,
    allowed_commands: Option<&BTreeSet<String>>,
    require_startup_task_state: bool,
) -> serde_json::Value {
    tool_interface_to_provider_schema_with_commands_and_jspace(
        interface,
        allowed_commands,
        require_startup_task_state,
        None,
    )
}

fn tool_interface_to_provider_schema_with_commands_and_jspace(
    interface: serde_json::Value,
    allowed_commands: Option<&BTreeSet<String>>,
    require_startup_task_state: bool,
    jspace_contract: Option<&serde_json::Value>,
) -> serde_json::Value {
    let name = interface
        .get("name")
        .and_then(|value| value.as_str())
        .unwrap_or("unknown_tool");
    let mut description = interface
        .get("description")
        .and_then(|value| value.as_str())
        .unwrap_or("")
        .to_string();
    if name == COMMAND_RUN_TOOL {
        description = command_run_description_for_active_shell(
            &description,
            allowed_commands,
            require_startup_task_state,
            jspace_contract,
        );
    }
    let mut input_schema = sanitize_provider_schema(
        interface
            .get("input_schema")
            .cloned()
            .unwrap_or_else(|| serde_json::json!({ "type": "object" })),
    );
    if name == COMMAND_RUN_TOOL
        && let Some(commands) = allowed_commands
    {
        input_schema = restrict_command_run_schema(input_schema, commands);
    }
    let mut parameters =
        if input_schema.get("type").and_then(|value| value.as_str()) == Some("array") {
            serde_json::json!({
                "type": "object",
                "required": ["requests"],
                "properties": {
                    "requests": input_schema
                }
            })
        } else {
            input_schema
        };
    let strict = env_flag("TURA_COMMAND_RUN_STRICT_JSON");
    if strict {
        parameters = strict_provider_schema(parameters);
    }
    parameters = strip_tura_schema_extensions(parameters);

    serde_json::json!({
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
            "strict": strict
        }
    })
}

fn restrict_command_run_schema(
    mut schema: serde_json::Value,
    commands: &BTreeSet<String>,
) -> serde_json::Value {
    let active = active_shell_command_name();
    let command_names = command_list_for_description(commands, active)
        .into_iter()
        .map(serde_json::Value::String)
        .collect::<Vec<_>>();
    if let Some(command_type) = schema
        .pointer_mut("/properties/commands/items/properties/command_type")
        .and_then(serde_json::Value::as_object_mut)
    {
        command_type.insert("enum".to_string(), serde_json::Value::Array(command_names));
    }
    schema
}

fn strip_tura_schema_extensions(mut value: serde_json::Value) -> serde_json::Value {
    match &mut value {
        serde_json::Value::Object(object) => {
            object.remove("x-tura-optional");
            for child in object.values_mut() {
                *child = strip_tura_schema_extensions(std::mem::take(child));
            }
        }
        serde_json::Value::Array(items) => {
            for child in items {
                *child = strip_tura_schema_extensions(std::mem::take(child));
            }
        }
        _ => {}
    }
    value
}

fn strict_provider_schema(mut value: serde_json::Value) -> serde_json::Value {
    match &mut value {
        serde_json::Value::Object(object) => {
            if object.get("type").and_then(serde_json::Value::as_str) == Some("object") {
                object
                    .entry("additionalProperties".to_string())
                    .or_insert(serde_json::Value::Bool(false));
                if let Some(properties) = object
                    .get("properties")
                    .and_then(serde_json::Value::as_object)
                {
                    let mut required = object
                        .get("required")
                        .and_then(serde_json::Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    for key in properties.keys() {
                        let key_value = serde_json::Value::String(key.clone());
                        if !required.contains(&key_value) {
                            required.push(key_value);
                        }
                    }
                    object.insert("required".to_string(), serde_json::Value::Array(required));
                }
            }
            for child in object.values_mut() {
                *child = strict_provider_schema(std::mem::take(child));
            }
        }
        serde_json::Value::Array(items) => {
            for child in items {
                *child = strict_provider_schema(std::mem::take(child));
            }
        }
        _ => {}
    }
    value
}

fn active_shell_command_name() -> &'static str {
    match std::env::var("TURA_COMMAND_RUN_SHELL")
        .ok()
        .map(|value| value.trim().to_ascii_lowercase())
        .as_deref()
    {
        Some("bash") => "bash",
        Some("zsh") => "zsh",
        Some("shell") | Some("shell_command") | Some("shll") | Some("shall") => "shell_command",
        _ if cfg!(windows) => "shell_command",
        _ if cfg!(target_os = "macos") => "zsh",
        _ => "bash",
    }
}

fn command_run_description_for_active_shell(
    original: &str,
    allowed_commands: Option<&BTreeSet<String>>,
    require_startup_task_state: bool,
    jspace_contract: Option<&serde_json::Value>,
) -> String {
    let active = active_shell_command_name();
    let default_commands;
    let allowed_commands = match allowed_commands {
        Some(commands) => commands,
        None => {
            default_commands = default_command_run_commands();
            &default_commands
        }
    };
    let prefix = original
        .split("\nAvailable command details")
        .next()
        .unwrap_or(original)
        .split("\nCommand line formats:\n")
        .next()
        .unwrap_or(original)
        .replace("Available commands: apply_patch, bash, shell_command.", "")
        .trim_end()
        .to_string();
    let jspace_shell_guidance = jspace_provider_shell_guidance(jspace_contract);
    let command_lines = command_list_for_description(allowed_commands, active)
        .into_iter()
        .filter_map(|command| {
            if command == active
                && let Some(guidance) = jspace_shell_guidance.as_deref()
            {
                return Some(format!(
                    "- {active}: Follow the J-Space shell boundary exactly. {guidance} Submit the admitted command as `command_line`; do not probe substitute utilities."
                ));
            }
            command_run_command_format_line(&command, require_startup_task_state)
        })
        .collect::<Vec<_>>();
    let jspace_guidance = jspace_shell_guidance
        .as_deref()
        .map(|guidance| format!("\nJ-Space shell boundary:\n{guidance}"))
        .unwrap_or_default();
    format!(
        "{prefix} Available commands: {}.{jspace_guidance}\nCommand run patterns:\n{}\nCommand line formats:\n{}",
        command_list_for_description(allowed_commands, active).join(", "),
        command_run_usage_patterns(allowed_commands),
        command_lines.join("\n"),
    )
}

fn jspace_provider_shell_guidance(contract: Option<&serde_json::Value>) -> Option<String> {
    let contract = contract?;
    let mut sections = Vec::new();

    if let Some(value) = contract.get("command_templates") {
        let templates = value.as_array()?;
        let mut exact_argv = Vec::new();
        for template in templates {
            let argv = template.get("argv")?.as_array()?;
            if argv.is_empty() || argv.iter().any(|item| !item.is_string()) {
                return None;
            }
            exact_argv.push(serde_json::Value::Array(argv.clone()).to_string());
        }
        exact_argv.sort();
        exact_argv.dedup();
        if !exact_argv.is_empty() {
            sections.push(format!(
                "Exact admitted argv: {}. Use these exact argv; do not substitute unlisted shell utilities.",
                exact_argv.join("; ")
            ));
        }
    }

    if let Some(value) = contract.get("read_commands") {
        let read_commands = value.as_object()?;
        let mut utilities = Vec::new();
        let mut roots = Vec::new();
        for (name, spec) in read_commands {
            if name == "roots" {
                let values = spec.as_array()?;
                if values.iter().any(|item| !item.is_string()) {
                    return None;
                }
                roots.extend(
                    values
                        .iter()
                        .filter_map(serde_json::Value::as_str)
                        .map(str::to_string),
                );
            } else {
                if !spec.is_object() {
                    return None;
                }
                utilities.push(name.clone());
            }
        }
        utilities.sort();
        utilities.dedup();
        roots.sort();
        roots.dedup();
        if !utilities.is_empty() {
            sections.push(format!(
                "Admitted read utilities: {}.",
                utilities.join(", ")
            ));
        }
        if !roots.is_empty() {
            sections.push(format!("Admitted read roots: {}.", roots.join(", ")));
        }
    }

    (!sections.is_empty()).then(|| sections.join(" "))
}

pub(crate) fn command_run_command_format_line(
    command_id: &str,
    require_startup_task_state: bool,
) -> Option<String> {
    let command_id = code_tools::commands::canonical_command(command_id);
    let active = active_shell_command_name();
    match command_id.as_str() {
        "apply_patch" => Some(format!(
            "- apply_patch: {}",
            current_apply_patch_command_format()
        )),
        command if command == active => {
            let shell_prompt = command_prompt(active);
            Some(format!(
                "- {active}: {}",
                current_shell_command_format(&shell_prompt)
            ))
        }
        "read_media" | "generate_media" | "web_discover" => Some(format!(
            "- {command_id}: {} Schema: {}",
            compact_prompt(&command_prompt(&command_id)),
            compact_schema(&command_schema(&command_id)),
        )),
        "source_read" => Some(format!(
            "- source_read: Read only an exact J-Space-granted workspace file. Use DCF locators first; for an unknown exact branch, search the relevant file using JSON `path`, `search_terms` (1..16 nonempty literal OR terms, <=2048 bytes total), optional `context_lines` (0..5), and optional `start_line` (default 1). For ranges provide `path`, `start_line`, `end_line` and optional `line_numbers`: true. For JSON projection instead provide named `path`, `json_pointer` (RFC 6901 string; empty selects the whole JSON), and no line/search options. Returns compact selected JSON with projection metadata and the source file SHA, not line coverage; invalid JSON, malformed/missing pointers and oversized selections fail without partial output. All modes accept `expected_sha256`; range/search resume using `next_line` as `start_line` with that hash. {} Read only needed ranges or JSON fields, batch independent file requests in the existing command_run, don't reread file starts; cite printed physical line labels only for line reads, stripping labels from verbatim quotes. No shell or workdir override.",
            code_tools::commands::source_read::output_limits_description(),
        )),
        "focused_verifier" => Some(
            "- focused_verifier: Run a preauthorized public test in a verifier-only step group. command_line must contain only JSON {\"verifier_index\":0}, selecting a zero-based grant. No argv, cwd/workdir or timeout overrides. Task-specific whole-array restrictions prevail. Only when the task permits a mixed response, fully-known J-Space-admitted apply_patch commands may run at earlier positive steps in the same command_run response, with focused_verifier in its own verifier-only strictly later positive step group after all patches it verifies. No same-step dependent verification, speculative edits, relaxed admission or automatic done. Reverify after relevant mutations or verifier failures, interpret required fresh evidence before completion, and avoid unconditional duplicate retests.".to_string(),
        ),
        "task_status" => {
            let task_status_schema = task_status::task_status_schema(require_startup_task_state);
            Some(format!(
                "- task_status: {} Schema: {}",
                task_status::task_status_prompt(require_startup_task_state),
                compact_schema(&task_status_schema),
            ))
        }
        "planning" => Some(format!(
            "- planning: {} Schema: {}",
            compact_prompt(&command_prompt("planning")),
            compact_schema(code_tools::commands::planning::SCHEMA),
        )),
        _ if code_tools::registry::forced_command_directory(&command_id).is_some() => {
            Some(format!(
                "- {command_id}: {} Schema: {}",
                compact_prompt(&command_prompt(&command_id)),
                compact_schema(&command_schema(&command_id)),
            ))
        }
        _ => None,
    }
}

fn command_prompt(command_id: &str) -> String {
    read_command_file(command_id, "prompt.md")
        .or_else(|| builtin_command_prompt(command_id).map(str::to_string))
        .unwrap_or_default()
}

fn builtin_command_prompt(command_id: &str) -> Option<&'static str> {
    Some(match command_id {
        "apply_patch" => code_tools::commands::apply_patch::PROMPT,
        "bash" => code_tools::commands::bash::PROMPT,
        "planning" => code_tools::commands::planning::PROMPT,
        "shell_command" => code_tools::commands::shell_command::PROMPT,
        "task_status" => code_tools::commands::task_status::PROMPT,
        "zsh" => code_tools::commands::zsh::PROMPT,
        _ => return None,
    })
}

fn command_schema(command_id: &str) -> String {
    read_command_file(command_id, "schema.json").unwrap_or_else(|| "{}".to_string())
}

fn read_command_file(command_id: &str, file_name: &str) -> Option<String> {
    if let Some(directory) = code_tools::registry::forced_command_directory(command_id)
        && let Ok(content) = std::fs::read_to_string(directory.join(file_name))
    {
        return Some(content);
    }
    let root = project_directory_with_tools().ok()?;
    [
        root.join("crates")
            .join("tools")
            .join("src")
            .join("commands")
            .join(command_id)
            .join(file_name),
        root.join("commands").join(command_id).join(file_name),
    ]
    .into_iter()
    .find_map(|path| std::fs::read_to_string(path).ok())
}

fn command_list_for_description(commands: &BTreeSet<String>, active_shell: &str) -> Vec<String> {
    let order = [
        "apply_patch",
        active_shell,
        "generate_media",
        "read_media",
        "web_discover",
        "source_read",
        "focused_verifier",
        "task_status",
        "planning",
    ];
    let mut ordered = order
        .into_iter()
        .filter(|name| commands.contains(*name))
        .map(str::to_string)
        .collect::<Vec<_>>();
    let remaining = commands
        .iter()
        .filter(|name| !ordered.contains(name))
        .cloned()
        .collect::<Vec<_>>();
    ordered.extend(remaining);
    ordered
}

fn command_run_usage_patterns(allowed_commands: &BTreeSet<String>) -> String {
    let output_bindings_enabled = code_tools::registry::forced_command_ids()
        .into_iter()
        .any(|command_id| allowed_commands.contains(&command_id));
    command_run_usage_patterns_for(allowed_commands, output_bindings_enabled)
}

fn command_run_usage_patterns_for(
    allowed_commands: &BTreeSet<String>,
    output_bindings_enabled: bool,
) -> String {
    let mut patterns = vec![
        "- Current call schema is mandatory: call `command_run` with a non-empty `commands` array only. Every command object must include `command_type`, `command_line`, and `step`. Historical replay may show `arguments: {}` as a bookkeeping placeholder; never copy that placeholder into a new call.",
        "- Batch related discovery, targeted searches, file reads, edits, and already-known validation in as few calls as practical. Read relevant code before probes. Independent commands with no output dependency share one step; dependent commands use later steps.",
    ];
    patterns.push(if output_bindings_enabled {
        "- Use steps as dependency groups, not command indexes. Commands in the same step must have no output dependency on each other and may run together; later steps may consume earlier-step JSON output through placeholders."
    } else {
        "- Use steps as dependency groups, not command indexes. Commands in the same step must have no output dependency on each other and may run together; commands that depend on earlier output must use later unique ordered steps whose inputs are already known before the batch is created."
    });
    if output_bindings_enabled {
        patterns.push("- Cross-step output variable: wrap a generic binding path as `#@#${<command id or command_type>.<JSON path>}#@#$` inside a later step's input. Give repeated commands unique `id` values. Only previous-step outputs are visible; unresolved, same-step, and future-step references fail before dispatch.");
        patterns.push("- Example output binding: if step 1 command id `create_file` returns `{\"filename\":\"draft.txt\"}`, a later step may use `#@#${create_file.filename}#@#$` in its input.");
    }
    patterns.extend([
        "- After discovery produces enough facts, edit coherently and run already-known focused validation in later steps. Inspect failures and change the next command; never retry an unchanged failed command.",
        "- Avoid embedding long generated source code or complex quoting directly in shell command lines; for complex logic, invoke a script/interpreter from the active shell rather than encoding the logic in shell syntax.",
    ]);
    if allowed_commands.contains("task_status") {
        patterns.push("- Context compaction: after a meaningful phase completes, or when context is near the active context limit and feels crowded, put the handoff summary in `task_status.compact_context` after the work it summarizes.");
    }
    if allowed_commands.contains("task_status")
        && (allowed_commands.contains("focused_verifier") || allowed_commands.contains("source_read"))
    {
        if allowed_commands.contains("focused_verifier") {
            patterns.push(if allowed_commands.contains("apply_patch") {
                "- Terminal batching economy: when task instructions permit and all inputs, acceptance conditions, effects, checks and readbacks are fully known with no result-dependent interpretation, prefer admitted apply_patch -> focused_verifier -> task_status done in one command_run response; focused_verifier -> done without edits is also useful."
            } else {
                "- Terminal batching economy: when task instructions permit and all inputs, acceptance conditions, effects, checks and readbacks are fully known with no result-dependent interpretation, prefer focused_verifier -> task_status done in one command_run response."
            });
            patterns.push("- Put focused_verifier in its own verifier-only strictly later positive step after any mutations it verifies. task_status done must be the sole command in a strictly later final positive step. Required effects, checks and readbacks must pass before done executes, not before proposing; failed, timed-out or unknown prior results fence done until resolved with required fresh verification. Fully-known fail -> repair -> pass recovery may remain in the same request.");
        }
        if allowed_commands.contains("source_read") {
            patterns.push("- Terminal batching economy: mechanical final source_read readbacks with already-known inputs may also share a batch with task_status done alone at a strictly later final step, after required verification has already passed.");
        }
        patterns.push("- Keep a separate model round for interpretation, missing evidence, unknown outcomes, or failure correction requiring reasoning. Runtime fences failed/unknown prior results and ambiguous streamed ordering; command success alone is not task completion and never waives task_status completion or Operation Manual rules. No blind replay or fence bypass.");
    }
    if allowed_commands.contains("read_media") || allowed_commands.contains("generate_media") {
        patterns.push("- For media work, collect or generate first, then verify with `read_media` or focused reads in a later step.");
    } else if allowed_commands.contains("web_discover") {
        patterns.push("- For web discovery, collect references first, then verify resulting repo evidence with focused reads or probes.");
    }
    patterns.join("\n")
}

fn current_apply_patch_command_format() -> String {
    let grammar = "start: begin_patch hunk+ end_patch\nbegin_patch: \"*** Begin Patch\" LF\nend_patch: \"*** End Patch\" LF?\n\nhunk: add_hunk | delete_hunk | update_hunk\nadd_hunk: \"*** Add File: \" filename LF add_line+\ndelete_hunk: \"*** Delete File: \" filename LF\nupdate_hunk: \"*** Update File: \" filename LF change_move? change?\n\nfilename: /(.+)/\nadd_line: \"+\" /(.*)/ LF -> line\n\nchange_move: \"*** Move to: \" filename LF\nchange: (change_context | change_line)+ eof_line?\nchange_context: (\"@@\" | \"@@ \" /(.+)/) LF\nchange_line: (\"+\" | \"-\" | \" \") /(.*)/ LF\neof_line: \"*** End of File\" LF\n\n%import common.LF\n";
    let prompt = compact_prompt(&command_prompt("apply_patch"));
    format!(
        "{prompt} Use one patch for coordinated multi-file source edits after reads. Patches validate context and fail on mismatch. Raw freeform body. Format type `grammar`, syntax `lark`. Definition: {grammar}"
    )
}

fn current_shell_command_format(shell_prompt: &str) -> String {
    let guidance = format!(
        "Use for tests, builds, scripts, package tools, and host-shell behavior. Default timeout is 5 minutes; finite long-running commands may set timeout_ms up to 4 hours. stall_timeout_ms is an independent no-output progress watchdog and must be used only when the workload emits progress. Put verification after edits in a later step only when that verification command is already known. Delete commands are allowed only when every delete target is a literal path inside the workspace; variable targets such as `$file.FullName` may be blocked. {} {}",
        compact_prompt(shell_prompt),
        long_running_service_guidance(),
    );
    format!(
        "{guidance} `command_line` accepts plain shell text or an escaped JSON object string with required `command` and optional `workdir`, `timeout_ms`, and `stall_timeout_ms`."
    )
}

fn long_running_service_guidance() -> &'static str {
    "Finite builders, tests, rehashes, and backtests must stay attached to command_run until a terminal receipt exists. Persistent services are different: use an existing managed lifecycle surface with bounded readiness checks and cleanup, never an untracked background process as a timeout workaround."
}

fn compact_prompt(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn compact_schema(text: &str) -> String {
    serde_json::from_str::<serde_json::Value>(text)
        .map(|value| value.to_string())
        .unwrap_or_else(|_| compact_prompt(text))
}

fn sanitize_provider_schema(mut value: serde_json::Value) -> serde_json::Value {
    match &mut value {
        serde_json::Value::Object(object) => {
            for child in object.values_mut() {
                *child = sanitize_provider_schema(std::mem::take(child));
            }
        }
        serde_json::Value::Array(items) => {
            for child in items {
                *child = sanitize_provider_schema(std::mem::take(child));
            }
        }
        _ => {}
    }
    value
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manas::constants::PLANNING_TOOL;
    use crate::state_machine::agent_management::{AgentCapabilityItem, ValidatorConfig};
    use chrono::Utc;
    use lifecycle::{ProviderConfig, SessionInput, ToolChoice};
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn tool(name: &str) -> serde_json::Value {
        serde_json::json!({
            "type": "function",
            "function": {
                "name": name,
                "description": "",
                "parameters": { "type": "object" }
            }
        })
    }

    fn names(tools: Vec<serde_json::Value>) -> Vec<String> {
        tools
            .iter()
            .filter_map(|tool| tool_schema_name(tool).map(str::to_string))
            .collect()
    }

    fn command_run_agent_with_capabilities(capabilities: &[&str]) -> AgentManagement {
        let mut agent = AgentManagement::new(
            "agent-1".to_string(),
            "thoughtful".to_string(),
            std::path::PathBuf::from("agents/src/thoughtful"),
            None,
            true,
            true,
            false,
            false,
            ProviderConfig {
                tura_llm_name: "thinking".to_string(),
                default_model_tier: None,
                current_model: None,
                stream: true,
                temperature: 0.2,
                max_tokens: 0,
                tool_choice: ToolChoice::Auto,
                time_out_ms: 120_000,
            },
            ValidatorConfig {
                need_validator: false,
                validator_name: None,
            },
        );
        for capability_name in capabilities {
            agent.add_capability(AgentCapabilityItem {
                capability_name: (*capability_name).to_string(),
                capability_directory: std::path::PathBuf::from("crates/tools/src"),
            });
        }
        agent
    }

    #[test]
    fn missing_workspace_tool_schema_falls_back_to_tura_tool_root() {
        let stale_workspace = tempfile::tempdir().expect("stale workspace");
        let tura_root = tempfile::tempdir().expect("Tura root");
        let fallback_directory = tura_root.path().join("crates/tools/src");
        std::fs::create_dir_all(fallback_directory.join("command_run"))
            .expect("create fallback command_run directory");
        std::fs::write(fallback_directory.join("command_run/schema.json"), "{}")
            .expect("write fallback command_run schema");

        let mut agent = command_run_agent_with_capabilities(&["shells"]);
        agent.agent_capabilities[0].capability_directory =
            stale_workspace.path().join("crates/tools/src");

        assert_eq!(
            command_run_capability_directory_with_fallback(&agent, fallback_directory.clone()),
            Some(fallback_directory)
        );
    }

    fn command_run_interface() -> serde_json::Value {
        serde_json::json!({
            "name": COMMAND_RUN_TOOL,
            "description": "Run tools as a pure batch+step command runner. Use assistant content only for concise reasoning, progress, and conclusions. Available commands: apply_patch, bash, shell_command.\nCommand line formats:\n- apply_patch: patch\n- bash: bash details\n- shell_command: shell details",
            "input_schema": {
                "type": "object",
                "required": ["commands"],
                "properties": {
                    "commands": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["command_type", "command_line"],
                            "properties": {
                                "command_type": { "type": "string" },
                                "command_line": { "type": "string" },
                                "step": { "type": "integer", "x-tura-optional": true }
                            }
                        }
                    }
                }
            }
        })
    }

    fn session_with_task_type(task_type: Vec<String>) -> SessionManagement {
        SessionManagement::new(
            "session-test".to_string(),
            "Test session".to_string(),
            std::path::PathBuf::from("C:/workspace"),
            false,
            task_type,
            SessionInput {
                user_input: "test task".to_string(),
                file_input: vec![],
                agent: None,
                runtime_context: None,
                planning_mode_override: None,
            },
            "test goal".to_string(),
            Utc::now(),
        )
    }

    fn command_type_enum(schema: &serde_json::Value) -> Vec<String> {
        schema["function"]["parameters"]["properties"]["commands"]["items"]["properties"]
            ["command_type"]["enum"]
            .as_array()
            .expect("command_type enum should be injected")
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .expect("command_type enum value should be a string")
                    .to_string()
            })
            .collect()
    }

    fn assert_command_type_enum(schema: &serde_json::Value, expected: &[&str]) {
        assert_eq!(
            command_type_enum(schema),
            expected
                .iter()
                .map(|value| value.to_string())
                .collect::<Vec<_>>()
        );
    }

    fn command_batch_cardinality_is_valid(
        commands_schema: &serde_json::Value,
        command_count: usize,
    ) -> bool {
        let minimum = commands_schema["minItems"]
            .as_u64()
            .expect("commands minItems should be an unsigned integer")
            as usize;
        let maximum = commands_schema["maxItems"]
            .as_u64()
            .expect("commands maxItems should be an unsigned integer")
            as usize;
        command_count >= minimum && command_count <= maximum
    }

    #[test]
    fn apply_patch_only_provider_schema_accepts_bounded_command_batches() {
        let interface = serde_json::from_str::<serde_json::Value>(include_str!(
            "../../../tools/src/command_run/schema.json"
        ))
        .expect("command_run schema should parse");
        let allowed_commands = BTreeSet::from(["apply_patch".to_string()]);
        let schema = tool_interface_to_provider_schema_with_commands(
            interface,
            Some(&allowed_commands),
            false,
        );
        let commands_schema = &schema["function"]["parameters"]["properties"]["commands"];
        let description = commands_schema["description"]
            .as_str()
            .expect("commands description should be a string");

        assert_eq!(commands_schema["minItems"], 1);
        assert_eq!(commands_schema["maxItems"], 20);
        assert_command_type_enum(&schema, &["apply_patch"]);
        assert!(description.contains("prefer 5 or more commands"));
        assert!(description.contains("1-2 commands are acceptable"));
        assert_eq!(
            commands_schema["items"]["required"],
            serde_json::json!(["command_type", "command_line"])
        );
        assert!(command_batch_cardinality_is_valid(commands_schema, 1));
        assert!(!command_batch_cardinality_is_valid(commands_schema, 0));
        assert!(command_batch_cardinality_is_valid(commands_schema, 5));
        assert!(!command_batch_cardinality_is_valid(commands_schema, 21));
    }

    #[test]
    fn command_run_provider_schema_exposes_top_level_and_per_command_timeout_ms() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        let interface = serde_json::from_str::<serde_json::Value>(include_str!(
            "../../../tools/src/command_run/schema.json"
        ))
        .expect("command_run schema should parse");
        let allowed_commands = BTreeSet::from([active_shell_command_name().to_string()]);
        let schema = tool_interface_to_provider_schema_with_commands(
            interface,
            Some(&allowed_commands),
            false,
        );
        let parameters = &schema["function"]["parameters"];

        assert_eq!(
            parameters["properties"]["timeout_ms"]["type"],
            serde_json::json!(["number", "null"]),
            "top-level timeout_ms must be accepted by the provider schema: {schema}"
        );
        assert_eq!(
            parameters["properties"]["commands"]["items"]["properties"]["timeout_ms"]["type"],
            serde_json::json!(["number", "null"]),
            "per-command timeout_ms must be accepted by the provider schema: {schema}"
        );
        assert_eq!(
            parameters["properties"]["stall_timeout_ms"]["type"],
            serde_json::json!(["number", "null"]),
            "top-level stall_timeout_ms must be accepted by the provider schema: {schema}"
        );
        assert_eq!(
            parameters["properties"]["commands"]["items"]["properties"]["stall_timeout_ms"]["type"],
            serde_json::json!(["number", "null"]),
            "per-command stall_timeout_ms must be accepted by the provider schema: {schema}"
        );
    }

    #[test]
    fn command_run_prompt_without_cli_capability_matches_main_step_guidance() {
        let description = command_run_usage_patterns_for(&default_command_run_commands(), false);

        assert!(description.contains("commands that depend on earlier output must use later unique ordered steps whose inputs are already known before the batch is created."));
        assert!(!description.contains("Cross-step output variable"));
        assert!(!description.contains("Example output binding"));
        assert!(!description.contains("#@#${"));
    }

    #[test]
    fn command_run_prompt_with_cli_capability_explains_output_bindings() {
        let description = command_run_usage_patterns_for(&default_command_run_commands(), true);

        assert!(
            description
                .contains("later steps may consume earlier-step JSON output through placeholders.")
        );
        assert!(description.contains("Cross-step output variable"));
        assert!(description.contains("Example output binding"));
        assert!(description.contains("#@#${create_file.filename}#@#$"));
    }

    #[test]
    fn terminal_batching_economy_respects_allowed_commands_in_both_binding_modes() {
        let command_names = ["apply_patch", "focused_verifier", "source_read", "task_status"];
        for output_bindings_enabled in [false, true] {
            for mask in 0..(1 << command_names.len()) {
                let commands = command_names
                    .iter()
                    .enumerate()
                    .filter_map(|(bit, command)| {
                        (mask & (1 << bit) != 0).then(|| (*command).to_string())
                    })
                    .collect::<BTreeSet<_>>();
                let description =
                    command_run_usage_patterns_for(&commands, output_bindings_enabled);
                let verifier_done =
                    commands.contains("focused_verifier") && commands.contains("task_status");
                let readback_done =
                    commands.contains("source_read") && commands.contains("task_status");
                assert_eq!(
                    description.contains(
                        "prefer admitted apply_patch -> focused_verifier -> task_status done"
                    ),
                    verifier_done && commands.contains("apply_patch")
                );
                assert_eq!(
                    description.contains("prefer focused_verifier -> task_status done"),
                    verifier_done && !commands.contains("apply_patch")
                );
                assert_eq!(
                    description.contains(
                        "mechanical final source_read readbacks with already-known inputs"
                    ),
                    readback_done
                );
                assert_eq!(
                    description.contains("Terminal batching economy"),
                    verifier_done || readback_done
                );
                assert_eq!(
                    description.contains("Cross-step output variable"),
                    output_bindings_enabled
                );
                assert_eq!(
                    description.contains("separate model round"),
                    verifier_done || readback_done
                );
                assert!(!description.contains("only mechanical final source_read"));
                if !commands.contains("apply_patch") {
                    assert!(!description.contains("apply_patch"));
                }
                if readback_done {
                    assert!(description.contains("after required verification has already passed"));
                }
            }
        }
    }

    #[test]
    fn known_terminal_batching_requires_success_and_fresh_recovery() {
        for output_bindings_enabled in [false, true] {
            for with_edits in [false, true] {
                let mut commands =
                    BTreeSet::from(["focused_verifier".to_string(), "task_status".to_string()]);
                if with_edits {
                    commands.insert("apply_patch".to_string());
                }
                let description =
                    command_run_usage_patterns_for(&commands, output_bindings_enabled);
                for required in [
                    "when task instructions permit",
                    "all inputs, acceptance conditions, effects, checks and readbacks are fully known",
                    "no result-dependent interpretation",
                    "verifier-only strictly later positive step",
                    "sole command in a strictly later final positive step",
                    "must pass before done executes, not before proposing",
                    "failed, timed-out or unknown prior results fence done",
                    "resolved with required fresh verification",
                    "Fully-known fail -> repair -> pass recovery may remain in the same request",
                    "separate model round for interpretation, missing evidence, unknown outcomes, or failure correction requiring reasoning",
                    "ambiguous streamed ordering",
                    "command success alone is not task completion",
                    "never waives task_status completion or Operation Manual rules",
                    "No blind replay or fence bypass",
                ] {
                    assert!(
                        description.contains(required),
                        "missing guidance: {required}"
                    );
                }
            }
        }
    }

    #[test]
    fn source_read_provider_schema_advertises_mode_limits_and_navigation_rules() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let commands = BTreeSet::from(["source_read".to_string()]);
        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&commands),
            false,
        );
        let description = schema["function"]["description"]
            .as_str()
            .expect("command guidance");
        assert!(description.contains(&code_tools::commands::source_read::output_limits_description()));
        for expected in [
            "Line modes: At most 200 complete lines in both modes.",
            "Range reads: 12288 text bytes, 16384 receipt-inclusive serialized result bytes.",
            "Search reads: 6144 text bytes, 8192 receipt-inclusive serialized result bytes.",
            "JSON projection reads: 12288 text bytes, 16384 receipt-inclusive serialized result bytes.",
            "Oversized JSON projections fail without partial output.",
            "Read only an exact J-Space-granted workspace file.",
            "Use DCF locators first",
            "`search_terms` (1..16 nonempty literal OR terms, <=2048 bytes total)",
            "optional `context_lines` (0..5), and optional `start_line` (default 1)",
            "For ranges provide `path`, `start_line`, `end_line` and optional `line_numbers`: true.",
            "For JSON projection instead provide named `path`, `json_pointer` (RFC 6901 string; empty selects the whole JSON), and no line/search options.",
            "Returns compact selected JSON with projection metadata and the source file SHA, not line coverage",
            "invalid JSON, malformed/missing pointers and oversized selections fail without partial output.",
            "All modes accept `expected_sha256`; range/search resume using `next_line` as `start_line` with that hash.",
            "Read only needed ranges or JSON fields, batch independent file requests in the existing command_run, don't reread file starts",
            "cite printed physical line labels only for line reads, stripping labels from verbatim quotes",
            "No shell or workdir override.",
        ] {
            assert!(description.contains(expected), "missing source_read guidance: {expected}");
        }
    }

    #[test]
    fn command_run_schema_injects_task_status_command_and_dynamic_schema() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let commands = default_command_run_commands();
        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&commands),
            false,
        );

        assert_command_type_enum(
            &schema,
            &[
                "apply_patch",
                active_shell_command_name(),
                "web_discover",
                "task_status",
            ],
        );

        let task_status_schema =
            serde_json::from_str::<serde_json::Value>(&task_status::task_status_schema(false))
                .expect("task_status schema should parse");
        assert_eq!(
            task_status_schema["properties"]["status"]["enum"],
            serde_json::json!(["doing", "question", "done"])
        );
        assert_eq!(
            task_status_schema["properties"]["task_type"]["items"]["enum"],
            serde_json::Value::Array(
                crate::prompt_style::runtime_prompt_manual::valid_task_type_ids()
                    .into_iter()
                    .map(serde_json::Value::String)
                    .collect()
            )
        );
    }

    #[test]
    fn provider_tools_are_byte_identical_across_task_type_initialization() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let agent =
            command_run_agent_with_capabilities(&["command_run", "apply_patch", "task_status"]);
        let commands = command_run_commands_for_agent(&agent);
        assert!(commands.contains("task_status"));
        let before = load_agent_capabilities_for_task_state(&agent, &commands, true, None)
            .expect("startup provider tools");
        let after = load_agent_capabilities_for_task_state(&agent, &commands, false, None)
            .expect("initialized provider tools");
        assert!(!before.is_empty());
        assert_eq!(
            serde_json::to_vec(&before).expect("serialize startup tools"),
            serde_json::to_vec(&after).expect("serialize initialized tools")
        );

        let description = before[0]["function"]["description"]
            .as_str()
            .expect("provider tool description");
        assert!(description.contains("If task_type is unset"));
        assert!(description.contains("before any apply_patch or write-producing shell command"));
        assert!(
            description
                .contains("Non-writing reads, searches, and tests may share a command_run batch")
        );
        assert!(description.contains("Available `task_type` values:"));
        assert!(description.contains("Available task types:"));
        for id in crate::prompt_style::runtime_prompt_manual::valid_task_type_ids() {
            assert!(description.contains(id.as_str()), "missing task type {id}");
        }
    }

    #[test]
    fn planning_command_extends_task_status_command_schema() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let mut commands = default_command_run_commands();
        commands.insert("planning".to_string());
        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&commands),
            false,
        );
        assert!(command_type_enum(&schema).contains(&"task_status".to_string()));
        assert!(command_type_enum(&schema).contains(&"planning".to_string()));
    }

    #[test]
    fn default_non_final_turn_keeps_only_command_run() {
        let filtered = filter_tools_for_turn(
            vec![
                tool(COMMAND_RUN_TOOL),
                tool(PLANNING_TOOL),
                tool("web_search"),
            ],
            false,
            false,
        )
        .expect("filter should succeed");

        assert_eq!(names(filtered), vec![COMMAND_RUN_TOOL]);
    }

    #[test]
    fn planning_mode_still_keeps_only_command_run() {
        let filtered = filter_tools_for_turn(
            vec![tool(COMMAND_RUN_TOOL), tool(PLANNING_TOOL)],
            false,
            false,
        )
        .expect("filter should succeed");

        assert_eq!(names(filtered), vec![COMMAND_RUN_TOOL]);
    }

    #[test]
    fn final_turn_keeps_command_run_schema_for_prompt_cache() {
        let filtered = filter_tools_for_turn(
            vec![
                tool(COMMAND_RUN_TOOL),
                tool(PLANNING_TOOL),
                tool("web_search"),
            ],
            true,
            true,
        )
        .expect("filter should succeed");

        assert_eq!(names(filtered), vec![COMMAND_RUN_TOOL]);
    }

    #[test]
    fn planning_capability_adds_command_for_configured_agent_capabilities() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        let agent = command_run_agent_with_capabilities(&[
            "command_run",
            "apply_patch",
            "shells",
            "task_status",
            "planning",
        ]);

        let commands = command_run_commands_for_agent(&agent);

        assert!(commands.contains("planning"));
        assert!(commands.contains("task_status"));
        assert!(commands.contains(active_shell_command_name()));
        assert!(!commands.contains("shells"));
    }

    #[test]
    fn startup_task_state_is_required_only_when_agent_can_set_it() {
        let empty_session = session_with_task_type(Vec::new());
        let initialized_session = session_with_task_type(vec!["debug".to_string()]);
        let restricted = command_run_agent_with_capabilities(&["apply_patch"]);
        let capable = command_run_agent_with_capabilities(&["apply_patch", "task_status"]);

        assert!(!startup_task_state_required(
            &empty_session,
            &command_run_commands_for_agent(&restricted)
        ));
        assert!(startup_task_state_required(
            &empty_session,
            &command_run_commands_for_agent(&capable)
        ));
        assert!(!startup_task_state_required(
            &initialized_session,
            &command_run_commands_for_agent(&capable)
        ));
    }

    #[test]
    fn forced_capability_is_added_to_agent_schema_with_its_prompt_and_schema() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        let previous = std::env::var_os(code_tools::registry::FORCED_CAPABILITY_DIRECTORIES_ENV);
        let temp = tempfile::tempdir().expect("temporary capability directory");
        let directory = temp.path().join("mcp-command");
        std::fs::create_dir_all(&directory).expect("create capability directory");
        std::fs::write(
            directory.join("command.toml"),
            r#"id = "mcp_workspace"
core = false
execution = "one_shot"
supports_macro_command = true
mutating = true
[runtime]
binary = "tura-command-mcp"
[limits]
default_timeout_ms = 1000
max_timeout_ms = 2000
"#,
        )
        .expect("write manifest");
        std::fs::write(
            directory.join("prompt.md"),
            "Use the task-local MCP server.",
        )
        .expect("write prompt");
        std::fs::write(
            directory.join("schema.json"),
            r#"{"name":"mcp_workspace","input_schema":{"type":"object","required":["name"]}}"#,
        )
        .expect("write schema");
        let encoded = serde_json::to_string(&vec![directory]).expect("encode capability paths");
        // SAFETY: environment mutation is serialized by ENV_LOCK in this module.
        #[allow(unsafe_code, reason = "test environment mutation is serialized")]
        unsafe {
            std::env::set_var(
                code_tools::registry::FORCED_CAPABILITY_DIRECTORIES_ENV,
                encoded,
            )
        };

        let agent = command_run_agent_with_capabilities(&["command_run", "apply_patch"]);
        let commands = command_run_commands_for_agent(&agent);
        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&commands),
            false,
        );
        let description = schema["function"]["description"]
            .as_str()
            .expect("command_run description");

        assert!(commands.contains("mcp_workspace"));
        assert!(command_type_enum(&schema).contains(&"mcp_workspace".to_string()));
        assert!(
            description.contains("Use the task-local MCP server."),
            "{description}"
        );
        assert!(
            description.contains(r#""required":["name"]"#),
            "{description}"
        );
        assert!(
            description.contains("Cross-step output variable"),
            "{description}"
        );
        assert!(
            description.contains("Example output binding"),
            "{description}"
        );
        assert!(
            description.contains("#@#${create_file.filename}#@#$"),
            "{description}"
        );

        // SAFETY: environment mutation is serialized by ENV_LOCK in this module.
        #[allow(unsafe_code, reason = "test environment mutation is serialized")]
        unsafe {
            if let Some(previous) = previous {
                std::env::set_var(
                    code_tools::registry::FORCED_CAPABILITY_DIRECTORIES_ENV,
                    previous,
                );
            } else {
                std::env::remove_var(code_tools::registry::FORCED_CAPABILITY_DIRECTORIES_ENV);
            }
        }
    }

    #[test]
    fn empty_agent_capabilities_do_not_enable_default_command_run_commands() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_SHELL", "shell_command")
        };
        let agent = command_run_agent_with_capabilities(&[]);

        let commands = command_run_commands_for_agent(&agent);

        assert!(
            commands.is_empty(),
            "an agent with no capabilities must not receive default command_run commands"
        );
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_SHELL")
        };
    }

    #[test]
    fn empty_agent_capabilities_do_not_load_command_run_provider_tool() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_SHELL", "shell_command")
        };
        let agent = command_run_agent_with_capabilities(&[]);
        let session = session_with_task_type(vec!["debug".to_string()]);
        let commands = command_run_commands_for_agent(&agent);

        let tools = load_agent_capabilities_with_commands(&agent, &session, &commands)
            .expect("tool loading should succeed");

        assert!(
            tools.is_empty(),
            "an agent with no capabilities must not receive the command_run provider tool"
        );
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_SHELL")
        };
    }

    #[test]
    fn runtime_prompt_capabilities_extend_command_run_schema() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_SHELL", "shell_command")
        };
        let mut commands = command_run_commands_for_agent(&command_run_agent_with_capabilities(&[
            "command_run",
            "apply_patch",
            "shells",
            "web_discover",
            "task_status",
        ]));
        extend_command_run_commands_with_capabilities(
            &mut commands,
            ["read_media", "generate_media"],
        );

        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&commands),
            false,
        );

        assert_command_type_enum(
            &schema,
            &[
                "apply_patch",
                "shell_command",
                "generate_media",
                "read_media",
                "web_discover",
                "task_status",
            ],
        );
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_SHELL")
        };
    }

    #[test]
    fn jspace_provider_surface_hides_unadmitted_workspace_effects() {
        let shell = active_shell_command_name().to_string();
        let execution_commands = BTreeSet::from([
            "apply_patch".to_string(),
            shell.clone(),
            "task_status".to_string(),
            "web_discover".to_string(),
        ]);
        let contract = serde_json::json!({
            "allowed_operations": ["read", "command"],
            "denied_operations": ["delete", "network"],
            "write_scopes": [],
            "command_templates": [{"argv": ["cat", "src/lib.rs"]}],
        });

        let visible =
            provider_command_run_commands_for_jspace(&execution_commands, Some(&contract));

        assert_eq!(visible, BTreeSet::from([shell, "task_status".to_string()]));
        assert!(execution_commands.contains("apply_patch"));
        assert!(execution_commands.contains("web_discover"));
    }

    #[test]
    fn jspace_provider_surface_keeps_authorized_patch_and_discovery_shell() {
        let shell = active_shell_command_name().to_string();
        let execution_commands = BTreeSet::from([
            "apply_patch".to_string(),
            shell.clone(),
            "task_status".to_string(),
            "web_discover".to_string(),
        ]);
        let contract = serde_json::json!({
            "allowed_operations": ["read", "command", "modify"],
            "denied_operations": ["delete", "network"],
            "write_scopes": ["src/lib.rs"],
            "command_templates": [],
            "read_commands": {
                "roots": ["src"],
                "rg": {"path": "/usr/bin/rg", "sha256": "a"},
                "cat": {"path": "/bin/cat", "sha256": "b"}
            }
        });

        let visible =
            provider_command_run_commands_for_jspace(&execution_commands, Some(&contract));

        assert_eq!(
            visible,
            BTreeSet::from(["apply_patch".to_string(), shell, "task_status".to_string(),])
        );
    }

    #[test]
    fn jspace_provider_surface_keeps_internal_status_with_granted_source_read() {
        let shell = active_shell_command_name().to_string();
        let execution_commands = BTreeSet::from([
            "apply_patch".to_string(),
            shell,
            "planning".to_string(),
            "task_status".to_string(),
            "source_read".to_string(),
        ]);
        let contract = serde_json::json!({
            "allowed_operations": ["read", "command"],
            "denied_operations": ["create", "modify", "delete", "network"],
            "read_scopes": ["src/main.rs"],
            "write_scopes": [],
            "command_templates": [],
            "source_read": true,
        });
        let visible =
            provider_command_run_commands_for_jspace(&execution_commands, Some(&contract));
        assert_eq!(
            visible,
            BTreeSet::from([
                "planning".to_string(),
                "source_read".to_string(),
                "task_status".to_string(),
            ])
        );
        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&visible),
            false,
        );
        assert_command_type_enum(&schema, &["source_read", "task_status", "planning"]);
        assert!(
            schema["function"]["description"]
                .as_str()
                .expect("description")
                .contains("exact J-Space-granted workspace file")
        );
        assert!(
            schema["function"]["description"]
                .as_str()
                .expect("description")
                .contains("search_terms")
        );

        let mut without_grant = contract;
        without_grant
            .as_object_mut()
            .expect("contract")
            .remove("source_read");
        let hidden =
            provider_command_run_commands_for_jspace(&execution_commands, Some(&without_grant));
        assert!(!hidden.contains("source_read"));
    }

    #[test]
    fn focused_verifier_format_describes_ordered_step_groups_and_task_limits() {
        let guide = command_run_command_format_line("focused_verifier", false)
            .expect("focused_verifier guide");
        for expected in [
            "preauthorized public test in a verifier-only step group",
            "command_line must contain only JSON {\"verifier_index\":0}",
            "No argv, cwd/workdir or timeout overrides",
            "Task-specific whole-array restrictions prevail",
            "Only when the task permits a mixed response",
            "fully-known J-Space-admitted apply_patch commands",
            "earlier positive steps in the same command_run response",
            "own verifier-only strictly later positive step group after all patches it verifies",
            "No same-step dependent verification, speculative edits, relaxed admission or automatic done",
            "Reverify after relevant mutations or verifier failures",
            "interpret required fresh evidence before completion",
            "avoid unconditional duplicate retests",
        ] {
            assert!(
                guide.contains(expected),
                "missing focused_verifier guidance: {expected}"
            );
        }
        assert!(!guide.contains("verifier-only batch"));
    }

    #[test]
    fn jspace_provider_surface_shows_focused_verifier_only_with_typed_grant() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        let interface = serde_json::from_str::<serde_json::Value>(include_str!(
            "../../../tools/src/command_run/schema.json"
        ))
        .expect("command_run schema should parse");
        let commands = BTreeSet::from([
            "apply_patch".to_string(),
            "focused_verifier".to_string(),
            "task_status".to_string(),
        ]);
        let mut contract = serde_json::json!({
            "allowed_operations": ["read", "command", "modify"],
            "denied_operations": ["create", "delete", "network"],
            "read_scopes": ["src/main.rs"],
            "write_scopes": ["src/main.rs"],
            "verifier_commands": [{}]
        });
        let visible = provider_command_run_commands_for_jspace(&commands, Some(&contract));
        let schema = tool_interface_to_provider_schema_with_commands_and_jspace(
            interface.clone(),
            Some(&visible),
            false,
            Some(&contract),
        );
        assert_command_type_enum(&schema, &["apply_patch", "focused_verifier", "task_status"]);
        let description = schema["function"]["description"]
            .as_str()
            .expect("provider description");
        let guide = command_run_command_format_line("focused_verifier", false)
            .expect("focused_verifier guide");
        assert_eq!(
            description
                .lines()
                .find(|line| line.starts_with("- focused_verifier:")),
            Some(guide.as_str())
        );
        assert!(description.contains("- apply_patch:"));
        assert!(!description.contains("verifier-only batch"));
        contract
            .as_object_mut()
            .expect("contract")
            .remove("verifier_commands");
        let hidden = provider_command_run_commands_for_jspace(&commands, Some(&contract));
        assert!(!hidden.contains("focused_verifier"));
        let schema = tool_interface_to_provider_schema_with_commands_and_jspace(
            interface,
            Some(&hidden),
            false,
            Some(&contract),
        );
        assert_command_type_enum(&schema, &["apply_patch", "task_status"]);
        let description = schema["function"]["description"]
            .as_str()
            .expect("provider description without verifier grant");
        assert!(!description.contains("- focused_verifier:"));
        assert!(!description.contains("verifier_index"));
    }

    #[tokio::test]
    async fn source_read_only_schema_commands_match_jspace_and_dispatch() {
        let workspace = tempfile::tempdir().expect("workspace");
        let workspace_root = workspace
            .path()
            .canonicalize()
            .expect("canonical workspace");
        std::fs::create_dir(workspace_root.join("src")).expect("source directory");
        let source = workspace_root.join("src/main.rs");
        std::fs::write(&source, "alpha\nbeta\n").expect("source file");
        let json_source = workspace_root.join("src/capsule.json");
        let json_content = serde_json::json!({
            "answer": {"ok": true},
            "padding": "x".repeat(13 * 1024)
        }).to_string();
        assert!(json_content.len() > 12 * 1024);
        assert_eq!(json_content.lines().count(), 1);
        std::fs::write(&json_source, &json_content).expect("single-line JSON fixture");
        std::fs::write(workspace_root.join("src/denied.json"), r#"{"answer":{"ok":true}}"#)
            .expect("ungranted JSON fixture");
        let mut contract = serde_json::json!({
            "schema_version": "jspace_contract_v2",
            "repo_root": workspace_root,
            "dcf_generation": {
                "repo_root": workspace_root,
                "generation_id": "source-read-schema-dispatch",
                "required_domain_bindings": {
                    "surface-map": {
                        "required_domains": ["surface"],
                        "source_fingerprints": {"surface": "surface-a"}
                    }
                }
            },
            "provenance": {"matched_surface_ids": ["surface"]},
            "matched_surface_ids": ["surface"],
            "read_scopes": ["src/main.rs", "src/capsule.json"],
            "write_scopes": [],
            "allowed_operations": ["read", "command"],
            "denied_operations": ["create", "modify", "delete", "network"],
            "command_templates": [],
            "focused_verifiers": [],
            "declared_targets": [],
            "expansion": {
                "mode": "exact_target_only",
                "error_code": "JSPACE_EXPANSION_REQUIRED",
                "mutation_on_expansion": false
            },
            "source_read": true
        });
        contract["authorization_semantic_sha256"] = serde_json::json!(
            tura_path::jspace::authorization_semantic_sha256(&contract)
                .expect("authorization digest")
        );
        contract["content_sha256"] =
            serde_json::json!(tura_path::jspace::semantic_sha256(&contract));
        let matcher = tura_path::jspace::JSpaceMatcher::from_value(&workspace_root, &contract)
            .expect("source-read-only contract");

        let execution_commands = BTreeSet::from([
            "apply_patch".to_string(),
            active_shell_command_name().to_string(),
            "planning".to_string(),
            "source_read".to_string(),
            "task_status".to_string(),
        ]);
        let visible =
            provider_command_run_commands_for_jspace(&execution_commands, Some(&contract));
        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&visible),
            false,
        );
        assert_command_type_enum(&schema, &["source_read", "task_status", "planning"]);
        matcher.check_source_read(&source).expect("exact read");
        matcher.check_source_read(&json_source).expect("exact JSON read");
        let denied_request = code_tools::commands::source_read::parse_command_line(
            r#"{"path":"src/denied.json","json_pointer":"/answer"}"#,
        ).expect("valid denied JSON projection request");
        let denied_target = code_tools::commands::source_read::target_path(
            &workspace_root, &denied_request,
        ).expect("existing denied JSON target");
        matcher.check_source_read(&denied_target)
            .expect_err("JSON projection must not expand exact read scopes");
        matcher
            .check_command("task_status", "done")
            .expect("internal status");
        matcher
            .check_command("planning", "[]")
            .expect("internal planning");
        assert_eq!(
            matcher
                .check_command("shell_command", "cat src/main.rs")
                .expect_err("shell without template")
                .code(),
            "JSPACE_COMMAND_DENIED"
        );
        assert_eq!(
            matcher
                .check_command("apply_patch", "")
                .expect_err("patch without target")
                .code(),
            "JSPACE_COMMAND_DENIED"
        );

        let command_run_result = code_tools::command_run::execute_async_value_with_source_read_admission(
            serde_json::json!({
                "execution_id": "schema-dispatch-source-read",
                "commands": [
                    {
                        "command_type": "source_read",
                        "command_line": serde_json::json!({
                            "path": "src/main.rs", "start_line": 1, "end_line": 2
                        }).to_string(),
                        "step": 1
                    },
                    {"command_type": "planning", "command_line": "[{\"task_summary\":\"read\"},{\"task_summary\":\"report\"}]", "step": 2},
                    {"command_type": "task_status", "command_line": "done", "step": 3}
                ]
            }),
            workspace_root.clone(),
            Some(visible.clone()),
            None,
            false,
            code_tools::runtime::tool::CancellationToken::new(),
            Some(std::sync::Arc::new(
                std::fs::File::open(&workspace_root).expect("workspace directory descriptor")
            )),
            None,
        )
        .await;
        let results = command_run_result["results"]
            .as_array()
            .expect("dispatch results");
        assert_eq!(results.len(), 3, "{command_run_result}");
        assert!(
            results.iter().all(|result| result["success"] == true),
            "{command_run_result}"
        );
        assert_eq!(results[0]["output"]["stdout"], "alpha\nbeta\n");
        assert_eq!(
            results[1]["output"]["steps"].as_array().map(Vec::len),
            Some(2)
        );
        assert_eq!(results[2]["output"]["task_status"]["status"], "done");
        assert_eq!(
            std::fs::read_to_string(source).expect("source unchanged"),
            "alpha\nbeta\n"
        );

        let json_dispatch_result = code_tools::command_run::execute_async_value_with_source_read_admission(
            serde_json::json!({
                "execution_id": "schema-dispatch-json-projection",
                "commands": [
                    {
                        "command_type": "source_read",
                        "command_line": serde_json::json!({
                            "path": "src/capsule.json", "json_pointer": "/answer"
                        }).to_string(),
                        "step": 1
                    },
                    {
                        "command_type": "source_read",
                        "command_line": serde_json::json!({
                            "path": "src/capsule.json",
                            "search_terms": ["not-present-in-fixture"]
                        }).to_string(),
                        "step": 1
                    }
                ]
            }),
            workspace_root.clone(),
            Some(visible),
            None,
            false,
            code_tools::runtime::tool::CancellationToken::new(),
            Some(std::sync::Arc::new(
                std::fs::File::open(&workspace_root).expect("workspace directory descriptor")
            )),
            None,
        ).await;
        let json_results = json_dispatch_result["results"].as_array().expect("JSON dispatch results");
        assert_eq!(json_results.len(), 2, "{json_dispatch_result}");
        assert!(json_results.iter().all(|result| result["success"] == true), "{json_dispatch_result}");
        let projection = &json_results[0]["output"];
        let search = &json_results[1]["output"];
        assert_eq!(projection["stdout"], r#"{"ok":true}"#);
        assert_eq!(projection["mode"], "json_projection");
        assert_eq!(projection["path"], "src/capsule.json");
        assert_eq!(projection["json_pointer"], "/answer");
        assert_eq!(projection["file_bytes"], json_content.len());
        assert_eq!(projection["truncated"], false);
        assert_eq!(search["stdout"], "");
        assert_eq!(search["search_matches"], serde_json::json!([]));
        // The empty search hashes the same complete source without emitting its oversized line.
        let source_sha = search["source_sha256"].as_str().expect("source SHA");
        assert_eq!(source_sha.len(), 64);
        assert!(source_sha.bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert_eq!(projection["source_sha256"], source_sha);
        for key in [
            "start_line", "end_line", "requested_end_line", "next_line", "total_lines",
            "line_numbers", "at_eof", "ends_with_newline", "search_matches", "context_lines",
        ] {
            assert!(projection.get(key).is_none(), "no fabricated line metadata: {key}");
        }
        assert_eq!(projection["terminal_receipt"]["terminal_state"], "completed");
        assert_eq!(projection["terminal_receipt"]["exit_code"], 0);
        let receipt_path = projection["terminal_receipt_path"].as_str().expect("JSON receipt path");
        let durable: serde_json::Value = serde_json::from_slice(
            &std::fs::read(receipt_path).expect("durable JSON receipt"),
        ).expect("JSON terminal receipt");
        assert_eq!(durable, projection["terminal_receipt"]);
        assert_eq!(std::fs::read_to_string(json_source).expect("JSON source unchanged"), json_content);
    }

    #[test]
    fn malformed_jspace_provider_surface_preserves_existing_commands() {
        let execution_commands = BTreeSet::from([
            "apply_patch".to_string(),
            active_shell_command_name().to_string(),
            "task_status".to_string(),
            "web_discover".to_string(),
        ]);
        let contract = serde_json::json!({"allowed_operations": "read"});

        assert_eq!(
            provider_command_run_commands_for_jspace(&execution_commands, Some(&contract)),
            execution_commands
        );
    }

    #[test]
    fn jspace_provider_description_exposes_exact_command_templates_in_stable_order() {
        let shell = active_shell_command_name().to_string();
        let allowed_commands = BTreeSet::from([shell]);
        let contract = serde_json::json!({
            "allowed_operations": ["read", "command"],
            "command_templates": [
                {"argv": ["cat", "scripts/ops/dcf/task_context.py"]},
                {"argv": ["cat", "scripts/ops/dcf/jspace.py"]}
            ],
            "write_scopes": []
        });

        let schema = tool_interface_to_provider_schema_with_commands_and_jspace(
            command_run_interface(),
            Some(&allowed_commands),
            false,
            Some(&contract),
        );
        let description = schema["function"]["description"].as_str().unwrap();
        let jspace_pos = description
            .find(r#"["cat","scripts/ops/dcf/jspace.py"]"#)
            .unwrap();
        let task_context_pos = description
            .find(r#"["cat","scripts/ops/dcf/task_context.py"]"#)
            .unwrap();

        assert!(description.contains("J-Space shell boundary:"));
        assert!(
            description
                .contains("Use these exact argv; do not substitute unlisted shell utilities.")
        );
        assert!(jspace_pos < task_context_pos);
        assert!(!description.contains("Use for tests, builds, scripts, package tools"));
        assert!(!description.contains("sed -n"));
        assert!(!description.contains("tail -n"));
    }

    #[test]
    fn jspace_provider_description_exposes_admitted_read_utilities_and_roots() {
        let shell = active_shell_command_name().to_string();
        let allowed_commands = BTreeSet::from([shell]);
        let contract = serde_json::json!({
            "allowed_operations": ["read", "command"],
            "command_templates": [],
            "read_commands": {
                "roots": ["src", "tests"],
                "rg": {"path": "/usr/bin/rg", "sha256": "a"},
                "cat": {"path": "/bin/cat", "sha256": "b"}
            }
        });

        let schema = tool_interface_to_provider_schema_with_commands_and_jspace(
            command_run_interface(),
            Some(&allowed_commands),
            false,
            Some(&contract),
        );
        let description = schema["function"]["description"].as_str().unwrap();

        assert!(description.contains("Admitted read utilities: cat, rg."));
        assert!(description.contains("Admitted read roots: src, tests."));
    }

    #[test]
    fn malformed_jspace_shell_projection_falls_back_to_generic_shell_guidance() {
        let shell = active_shell_command_name().to_string();
        let allowed_commands = BTreeSet::from([shell]);
        let contract = serde_json::json!({
            "allowed_operations": ["read", "command"],
            "command_templates": [{"argv": "cat src/lib.rs"}]
        });

        let schema = tool_interface_to_provider_schema_with_commands_and_jspace(
            command_run_interface(),
            Some(&allowed_commands),
            false,
            Some(&contract),
        );
        let description = schema["function"]["description"].as_str().unwrap();

        assert!(!description.contains("J-Space shell boundary:"));
        assert!(description.contains("Use for tests, builds, scripts, package tools"));
    }

    #[test]
    fn provider_schema_preserves_additional_properties_recursively() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_DISABLE_STRICT_JSON", "1")
        };

        let schema = tool_interface_to_provider_schema(serde_json::json!({
            "name": "example",
            "description": "example",
            "input_schema": {
                "type": "object",
                "additionalProperties": false,
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": false,
                            "properties": {
                                "name": { "type": "string" }
                            }
                        }
                    }
                }
            }
        }));

        assert!(schema.to_string().contains("additionalProperties"));
        assert_eq!(schema["function"]["strict"], false);

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_DISABLE_STRICT_JSON")
        };
    }

    #[test]
    fn strict_json_env_requires_all_provider_object_fields() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_STRICT_JSON", "1")
        };

        let schema = tool_interface_to_provider_schema(command_run_interface());
        let parameters = &schema["function"]["parameters"];

        assert_eq!(schema["function"]["strict"], true);
        assert_eq!(parameters["required"], serde_json::json!(["commands"]));
        assert_eq!(
            parameters["properties"]["commands"]["items"]["required"],
            serde_json::json!(["command_type", "command_line", "step"])
        );

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_STRICT_JSON")
        };
    }

    #[test]
    fn real_command_run_provider_schema_is_openai_strict_compatible() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_STRICT_JSON", "1")
        };

        let interface = serde_json::from_str::<serde_json::Value>(include_str!(
            "../../../tools/src/command_run/schema.json"
        ))
        .expect("command_run schema should parse");
        let schema = tool_interface_to_provider_schema(interface);
        let parameters = &schema["function"]["parameters"];
        let command_required = parameters["properties"]["commands"]["items"]["required"]
            .as_array()
            .expect("commands item required should be an array");

        assert_eq!(schema["function"]["strict"], true);
        assert_eq!(
            parameters["required"],
            serde_json::json!(["commands", "execution_id", "stall_timeout_ms", "timeout_ms"])
        );
        assert_eq!(
            parameters["properties"]["commands"]["items"]["required"],
            serde_json::json!([
                "command_type",
                "command_line",
                "id",
                "stall_timeout_ms",
                "step",
                "timeout_ms"
            ])
        );
        assert!(parameters["properties"].get("sandbox").is_none());
        assert!(parameters["properties"].get("task_status").is_none());
        assert!(command_required.contains(&serde_json::json!("command_type")));
        assert!(command_required.contains(&serde_json::json!("step")));
        assert!(command_required.contains(&serde_json::json!("id")));
        assert_eq!(
            parameters["properties"]["commands"]["items"]["properties"]["id"]["type"],
            serde_json::json!(["string", "null"])
        );
        assert_eq!(
            parameters["properties"]["timeout_ms"]["type"],
            serde_json::json!(["number", "null"])
        );
        assert_eq!(
            parameters["properties"]["stall_timeout_ms"]["type"],
            serde_json::json!(["number", "null"])
        );
        assert_eq!(
            parameters["properties"]["execution_id"]["type"],
            serde_json::json!(["string", "null"])
        );
        assert_eq!(
            parameters["properties"]["commands"]["items"]["properties"]["timeout_ms"]["type"],
            serde_json::json!(["number", "null"])
        );
        assert_eq!(
            parameters["properties"]["commands"]["items"]["properties"]["stall_timeout_ms"]["type"],
            serde_json::json!(["number", "null"])
        );
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_STRICT_JSON")
        };
    }

    #[test]
    fn command_run_provider_schema_exposes_only_shell_command_surface() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_SHELL", "shell_command")
        };

        let commands = default_command_run_commands();
        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&commands),
            false,
        );
        assert_command_type_enum(
            &schema,
            &[
                "apply_patch",
                "shell_command",
                "web_discover",
                "task_status",
            ],
        );
        assert_eq!(
            schema["function"]["parameters"]["properties"]["commands"]["items"]["properties"]["command_line"]
                ["type"],
            "string"
        );
        assert_eq!(
            schema["function"]["parameters"]["properties"]["commands"]["items"]["properties"]["step"]
                ["type"],
            "integer"
        );

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_SHELL")
        };
    }

    #[test]
    fn command_run_provider_schema_exposes_only_bash_surface() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_SHELL", "bash")
        };

        let commands = default_command_run_commands();
        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&commands),
            false,
        );
        assert_command_type_enum(
            &schema,
            &["apply_patch", "bash", "web_discover", "task_status"],
        );

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_SHELL")
        };
    }

    #[test]
    fn command_run_provider_schema_exposes_only_zsh_surface() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_SHELL", "zsh")
        };

        let commands = default_command_run_commands();
        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&commands),
            false,
        );
        assert_command_type_enum(
            &schema,
            &["apply_patch", "zsh", "web_discover", "task_status"],
        );

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_SHELL")
        };
    }

    #[test]
    fn command_run_provider_schema_injects_planning_only_when_enabled() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_SHELL", "shell_command")
        };
        let mut commands = default_command_run_commands();
        commands.insert("planning".to_string());

        let schema = tool_interface_to_provider_schema_with_commands(
            command_run_interface(),
            Some(&commands),
            false,
        );

        assert_command_type_enum(
            &schema,
            &[
                "apply_patch",
                "shell_command",
                "web_discover",
                "task_status",
                "planning",
            ],
        );

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_SHELL")
        };
    }

    #[test]
    fn command_run_capability_loading_uses_agent_commands_for_schema() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|err| err.into_inner());
        let run_id = format!(
            "tura-command-run-schema-test-{}",
            Utc::now().timestamp_nanos_opt().unwrap_or_default()
        );
        let root = std::env::temp_dir().join(run_id);
        let command_run_dir = root.join("command_run");
        std::fs::create_dir_all(&command_run_dir).expect("command_run dir should be created");
        std::fs::write(
            command_run_dir.join("schema.json"),
            command_run_interface().to_string(),
        )
        .expect("command_run schema should be written");

        let mut agent = AgentManagement::new(
            "agent".to_string(),
            "general".to_string(),
            root.clone(),
            None,
            true,
            false,
            false,
            false,
            ProviderConfig {
                tura_llm_name: "test".to_string(),
                default_model_tier: None,
                current_model: None,
                stream: false,
                temperature: 0.0,
                max_tokens: 0,
                tool_choice: ToolChoice::Auto,
                time_out_ms: 1000,
            },
            ValidatorConfig {
                need_validator: false,
                validator_name: None,
            },
        );
        agent.add_capability(AgentCapabilityItem {
            capability_name: COMMAND_RUN_TOOL.to_string(),
            capability_directory: root.clone(),
        });
        for capability_name in ["apply_patch", "shells", "task_status", "planning"] {
            agent.add_capability(AgentCapabilityItem {
                capability_name: capability_name.to_string(),
                capability_directory: root.clone(),
            });
        }

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_COMMAND_RUN_SHELL", "shell_command")
        };
        let session = session_with_task_type(vec!["debug".to_string()]);
        let allowed_commands = command_run_commands_for_agent(&agent);
        let tools = load_agent_capabilities_with_commands(&agent, &session, &allowed_commands)
            .expect("tool loading should succeed");
        let command_run = tools.first().expect("command_run tool should load");
        assert_command_type_enum(
            command_run,
            &["apply_patch", "shell_command", "task_status", "planning"],
        );

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_COMMAND_RUN_SHELL")
        };
        let _ = std::fs::remove_dir_all(root);
    }
}
