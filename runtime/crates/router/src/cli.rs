use serde::{Deserialize, Serialize};

use crate::app::build_state;
use crate::daemon::{serve_socket, serve_stdio};
use crate::runtime_dispatch::dispatch_run_agent;
use crate::runtime_utils::tokio_runtime;
use router_contract::ExecuteCommandRequest;
use runtime_contract::RunAgentRequest;
use tura_router::registry::Registry;
use tura_router::registry::agent::UpsertAgentRequest;
use tura_router::registry::persona::UpsertPersonaRequest;

pub(crate) fn run_router_command(command: &str) -> anyhow::Result<()> {
    match command {
        "native-once" => tokio_runtime()?.block_on(crate::native_once::run()),
        "serve" => tokio_runtime()?.block_on(serve_stdio()),
        "serve-socket" => tokio_runtime()?.block_on(serve_socket()),
        "run-agent" => tokio_runtime()?.block_on(run_agent_cli()),
        "compile-task-packet" => tokio_runtime()?.block_on(compile_task_packet_cli()),
        "registry-agents-list" => registry_agents_list_cli(),
        "registry-agent-get" => registry_agent_get_cli(),
        "registry-agent-create" => registry_agent_upsert_cli(None),
        "registry-agent-update" => {
            let agent_id = std::env::args()
                .nth(2)
                .ok_or_else(|| anyhow::anyhow!("agent id is required"))?;
            registry_agent_upsert_cli(Some(agent_id))
        }
        "registry-agent-delete" => registry_agent_delete_cli(),
        "registry-personas-list" => registry_personas_list_cli(),
        "registry-persona-get" => registry_persona_get_cli(),
        "registry-persona-create" => registry_persona_upsert_cli(None),
        "registry-persona-update" => {
            let persona_id = std::env::args()
                .nth(2)
                .ok_or_else(|| anyhow::anyhow!("persona id is required"))?;
            registry_persona_upsert_cli(Some(persona_id))
        }
        "registry-persona-delete" => registry_persona_delete_cli(),
        "registry-commands-list" => registry_commands_list_cli(),
        "registry-command-execute" => registry_command_execute_cli(),
        _ => Err(anyhow::anyhow!("unknown router command: {command}")),
    }
}

/// CLI subcommand `run-agent`: reads a `RunAgentRequest` JSON from stdin,
/// dispatches a runtime worker, and writes the result JSON to stdout.
async fn run_agent_cli() -> anyhow::Result<()> {
    let raw = read_stdin()?;
    let req: RunAgentRequest = serde_json::from_str(raw.trim())
        .map_err(|error| anyhow::anyhow!("invalid run-agent request json: {error}"))?;
    if let Some(error) = native_codex_cli_admission_error(&req) {
        println!("{}", error);
        return Ok(());
    }
    let state = build_state();
    let (_status, body) = dispatch_run_agent(&state, req, "router-cli-run-agent".to_string()).await;
    println!("{}", serde_json::to_string(&body)?);
    Ok(())
}

/// CLI subcommand `compile-task-packet`: reads one Commander task packet from
/// stdin and runs the same zero-mutation admission compiler used by dispatch.
async fn compile_task_packet_cli() -> anyhow::Result<()> {
    let raw = read_stdin()?;
    let packet: serde_json::Value = serde_json::from_str(raw.trim())
        .map_err(|error| anyhow::anyhow!("TASK_PACKET_WIRE_INVALID:{error}"))?;
    let state = build_state();
    let result = state
        .execution
        .compile_commander_task_packet_request(&state, packet)
        .await?;
    print_json(&result)
}

fn native_codex_cli_admission_error(req: &RunAgentRequest) -> Option<serde_json::Value> {
    req.native_codex_execution.as_ref().map(|_| {
        serde_json::json!({
            "ok": false,
            "code": "NATIVE_CODEX_COMMANDER_ADMISSION_REQUIRED",
            "error": "Native Codex execution is admitted only through the durable Commander task-packet path"
        })
    })
}

fn read_stdin() -> anyhow::Result<String> {
    use std::io::Read;

    let mut raw = String::new();
    std::io::stdin().read_to_string(&mut raw)?;
    Ok(raw)
}

fn print_json<T: Serialize>(value: &T) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string(value)?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn direct_run_agent_rejects_self_attested_native_codex_execution() {
        let request: RunAgentRequest = serde_json::from_value(serde_json::json!({
            "runtime_id": "runtime-direct-native",
            "lease_id": "lease-direct-native",
            "native_codex_execution": {
                "schema_version": "tura_native_codex_execution_binding_v1",
                "execution_profile_sha256": "a".repeat(64),
                "task_delta": {
                    "schema_version": "tura_native_codex_task_delta_v1",
                    "mission_id": "mission-direct-native",
                    "mission_revision_sha256": "b".repeat(64),
                    "task_id": "task-direct-native",
                    "current_predicate": "P1_BOUNDED_CONTEXT_AND_TOOL_OWNERSHIP",
                    "instruction": "Run the durable task.",
                    "semantic_sha256": "c".repeat(64)
                },
                "codex_executable": "/usr/bin/true",
                "codex_executable_sha256": "d".repeat(64),
                "command_graph_executable": "/usr/bin/true",
                "command_graph_executable_sha256": "e".repeat(64),
                "command_graph_allowed_commands": ["zsh"],
                "sandbox": "read_only",
                "timeout_ms": 300000,
                "semantic_sha256": "f".repeat(64)
            }
        }))
        .expect("parse direct Native Codex request");

        let error = native_codex_cli_admission_error(&request)
            .expect("direct Native Codex request must be rejected");
        assert_eq!(error["code"], "NATIVE_CODEX_COMMANDER_ADMISSION_REQUIRED");
        assert_eq!(error["ok"], false);
    }
}

fn registry_agents_list_cli() -> anyhow::Result<()> {
    print_json(&Registry::from_static().agents.list_catalog())
}

fn registry_agent_get_cli() -> anyhow::Result<()> {
    let agent_id = std::env::args()
        .nth(2)
        .ok_or_else(|| anyhow::anyhow!("agent id is required"))?;
    let registry = Registry::from_static();
    match registry.agents.get_stored(&agent_id) {
        Some(agent) => print_json(&agent),
        None => Err(anyhow::anyhow!("agent not found: {agent_id}")),
    }
}

fn registry_agent_upsert_cli(agent_id: Option<String>) -> anyhow::Result<()> {
    let raw = read_stdin()?;
    let payload: UpsertAgentRequest = serde_json::from_str(raw.trim())
        .map_err(|error| anyhow::anyhow!("invalid agent payload json: {error}"))?;
    let registry = Registry::from_static();
    let agent = registry
        .agents
        .upsert(agent_id, payload)
        .map_err(|error| anyhow::anyhow!("failed to upsert registry agent: {error}"))?;
    print_json(&agent)
}

fn registry_agent_delete_cli() -> anyhow::Result<()> {
    let agent_id = std::env::args()
        .nth(2)
        .ok_or_else(|| anyhow::anyhow!("agent id is required"))?;
    let registry = Registry::from_static();
    let deleted = registry
        .agents
        .delete(&agent_id)
        .map_err(|error| anyhow::anyhow!("failed to delete registry agent {agent_id}: {error}"))?;
    print_json(&deleted)
}

fn registry_personas_list_cli() -> anyhow::Result<()> {
    print_json(&Registry::from_static().personas.list())
}

fn registry_persona_get_cli() -> anyhow::Result<()> {
    let persona_id = std::env::args()
        .nth(2)
        .ok_or_else(|| anyhow::anyhow!("persona id is required"))?;
    let registry = Registry::from_static();
    match registry.personas.get(&persona_id) {
        Some(persona) => print_json(&persona),
        None => Err(anyhow::anyhow!("persona not found: {persona_id}")),
    }
}

fn registry_persona_upsert_cli(persona_id: Option<String>) -> anyhow::Result<()> {
    let raw = read_stdin()?;
    let payload: UpsertPersonaRequest = serde_json::from_str(raw.trim())
        .map_err(|error| anyhow::anyhow!("invalid persona payload json: {error}"))?;
    let registry = Registry::from_static();
    let persona = registry
        .personas
        .upsert(persona_id, payload)
        .map_err(|error| anyhow::anyhow!("failed to upsert registry persona: {error}"))?;
    print_json(&persona)
}

fn registry_persona_delete_cli() -> anyhow::Result<()> {
    let persona_id = std::env::args()
        .nth(2)
        .ok_or_else(|| anyhow::anyhow!("persona id is required"))?;
    let registry = Registry::from_static();
    let deleted = registry.personas.delete(&persona_id).map_err(|error| {
        anyhow::anyhow!("failed to delete registry persona {persona_id}: {error}")
    })?;
    print_json(&deleted)
}

#[derive(Debug, Deserialize)]
struct RegistryDirectoryPayload {
    #[serde(default)]
    directory: Option<String>,
}

fn registry_commands_list_cli() -> anyhow::Result<()> {
    let raw = read_stdin()?;
    let payload = if raw.trim().is_empty() {
        RegistryDirectoryPayload { directory: None }
    } else {
        serde_json::from_str::<RegistryDirectoryPayload>(raw.trim())
            .map_err(|error| anyhow::anyhow!("invalid command list payload json: {error}"))?
    };
    let registry = Registry::from_static();
    print_json(&registry.commands.list(payload.directory.as_deref()))
}

#[derive(Debug, Deserialize)]
struct RegistryCommandExecutePayload {
    #[serde(default)]
    directory: Option<String>,
    command: String,
    #[serde(default)]
    args: Option<Vec<String>>,
}

fn registry_command_execute_cli() -> anyhow::Result<()> {
    let raw = read_stdin()?;
    let payload: RegistryCommandExecutePayload = serde_json::from_str(raw.trim())
        .map_err(|error| anyhow::anyhow!("invalid command execute payload json: {error}"))?;
    let registry = Registry::from_static();
    let response = registry.commands.execute(ExecuteCommandRequest {
        directory: payload.directory,
        command: payload.command,
        args: payload.args,
    });
    print_json(&response)
}
