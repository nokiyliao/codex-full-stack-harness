use runtime::router_command_run::execute_command_run_value_with_jspace;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::path::PathBuf;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

const TOOL_NAME: &str = "tura_command_graph";
const PROTOCOL_VERSION: &str = "2025-06-18";

struct CommandGraphContext {
    session_id: String,
    task_id: String,
    execution_id: String,
    lease_id: String,
    workspace: PathBuf,
    allowed_commands: BTreeSet<String>,
    jspace_contract: Value,
}

impl CommandGraphContext {
    fn from_env() -> Result<Self, String> {
        let session_id = required_env("TURA_NATIVE_SESSION_ID")?;
        let task_id = required_env("TURA_NATIVE_TASK_ID")?;
        let execution_id = required_env("TURA_NATIVE_EXECUTION_ID")?;
        let lease_id = required_env("TURA_NATIVE_LEASE_ID")?;
        let workspace = PathBuf::from(required_env("TURA_NATIVE_WORKSPACE")?);
        if !workspace.is_absolute() || !workspace.is_dir() {
            return Err("TURA_COMMAND_GRAPH_WORKSPACE_INVALID".to_string());
        }
        let allowed_commands: BTreeSet<String> =
            serde_json::from_str(&required_env("TURA_NATIVE_ALLOWED_COMMANDS_JSON")?)
                .map_err(|error| format!("TURA_COMMAND_GRAPH_ALLOWLIST_INVALID:{error}"))?;
        if allowed_commands.is_empty()
            || allowed_commands
                .iter()
                .any(|command| command.trim().is_empty())
        {
            return Err("TURA_COMMAND_GRAPH_ALLOWLIST_EMPTY".to_string());
        }
        let jspace_contract: Value =
            serde_json::from_str(&required_env("TURA_NATIVE_JSPACE_CONTRACT_JSON")?)
                .map_err(|error| format!("TURA_COMMAND_GRAPH_JSPACE_INVALID:{error}"))?;
        if !jspace_contract.is_object() {
            return Err("TURA_COMMAND_GRAPH_JSPACE_OBJECT_REQUIRED".to_string());
        }
        Ok(Self {
            session_id,
            task_id,
            execution_id,
            lease_id,
            workspace,
            allowed_commands,
            jspace_contract,
        })
    }

    fn bind_arguments(&self, value: Value, rpc_id: &Value) -> Result<Value, String> {
        let mut arguments = value
            .as_object()
            .cloned()
            .ok_or_else(|| "TURA_COMMAND_GRAPH_ARGUMENTS_OBJECT_REQUIRED".to_string())?;
        // Content equality is not call identity: the same read or verifier
        // must be callable again after a mutation within this execution.
        let identity = match arguments.remove("execution_id") {
            Some(Value::String(id)) if !id.trim().is_empty() => json!({"caller": id}),
            None | Some(Value::Null) => match rpc_id {
                Value::String(_) | Value::Number(_) => json!({"rpc": rpc_id}),
                _ => return Err("TURA_COMMAND_GRAPH_CALL_ID_INVALID".to_string()),
            },
            _ => return Err("TURA_COMMAND_GRAPH_CALL_ID_INVALID".to_string()),
        };
        let semantic = canonical_json(&identity);
        let digest = format!("{:x}", Sha256::digest(semantic.as_bytes()));
        arguments.insert(
            "execution_id".to_string(),
            Value::String(format!(
                "native-graph:{}:{}",
                self.execution_id,
                &digest[..24]
            )),
        );
        Ok(Value::Object(arguments))
    }
}

#[tokio::main]
async fn main() {
    if let Err(error) = serve().await {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

async fn serve() -> Result<(), String> {
    let context = CommandGraphContext::from_env()?;
    let stdin = tokio::io::stdin();
    let mut lines = BufReader::new(stdin).lines();
    let mut stdout = tokio::io::stdout();
    while let Some(line) = lines
        .next_line()
        .await
        .map_err(|error| format!("TURA_COMMAND_GRAPH_STDIN_FAILED:{error}"))?
    {
        let request: Value = match serde_json::from_str(&line) {
            Ok(request) => request,
            Err(error) => {
                write_response(
                    &mut stdout,
                    json_rpc_error(Value::Null, -32700, format!("invalid JSON: {error}")),
                )
                .await?;
                continue;
            }
        };
        let Some(id) = request.get("id").cloned() else {
            continue;
        };
        let method = request
            .get("method")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let response = match method {
            "initialize" => json_rpc_result(
                id,
                json!({
                    "protocolVersion": request
                        .pointer("/params/protocolVersion")
                        .and_then(Value::as_str)
                        .unwrap_or(PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": false}},
                    "serverInfo": {"name": TOOL_NAME, "version": env!("CARGO_PKG_VERSION")}
                }),
            ),
            "ping" => json_rpc_result(id, json!({})),
            "tools/list" => json_rpc_result(id, tools_list()?),
            "tools/call" => match call_tool(&context, request.get("params"), &id).await {
                Ok(result) => json_rpc_result(id, result),
                Err(error) => json_rpc_result(
                    id,
                    json!({
                        "content": [{"type": "text", "text": error}],
                        "isError": true
                    }),
                ),
            },
            _ => json_rpc_error(id, -32601, format!("unsupported method {method}")),
        };
        write_response(&mut stdout, response).await?;
    }
    Ok(())
}

async fn call_tool(context: &CommandGraphContext, params: Option<&Value>, rpc_id: &Value) -> Result<Value, String> {
    let params = params.ok_or_else(|| "TURA_COMMAND_GRAPH_PARAMS_REQUIRED".to_string())?;
    if params.get("name").and_then(Value::as_str) != Some(TOOL_NAME) {
        return Err("TURA_COMMAND_GRAPH_TOOL_NAME_INVALID".to_string());
    }
    let arguments = context.bind_arguments(
        params
            .get("arguments")
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new())),
        rpc_id,
    )?;
    let result = execute_command_run_value_with_jspace(
        arguments,
        context.workspace.clone(),
        Some(&context.session_id),
        Some(&context.execution_id),
        Some(context.allowed_commands.clone()),
        Some(context.jspace_contract.clone()),
    )
    .await?;
    let is_error = command_run_result_is_error(&result)?;
    let text = serde_json::to_string(&result)
        .map_err(|error| format!("TURA_COMMAND_GRAPH_RESULT_ENCODE_FAILED:{error}"))?;
    Ok(json!({
        "content": [{"type": "text", "text": text}],
        "structuredContent": result,
        "isError": is_error,
        "_meta": {
            "task_id": context.task_id,
            "session_id": context.session_id,
            "execution_id": context.execution_id,
            "lease_id": context.lease_id
        }
    }))
}

fn command_run_result_is_error(result: &Value) -> Result<bool, String> {
    let results = result
        .get("results")
        .and_then(Value::as_array)
        .filter(|results| !results.is_empty())
        .ok_or_else(|| "TURA_COMMAND_GRAPH_RESULT_SHAPE_INVALID".to_string())?;
    let mut is_error = false;
    for item in results {
        let success = item
            .get("success")
            .and_then(Value::as_bool)
            .ok_or_else(|| "TURA_COMMAND_GRAPH_RESULT_SUCCESS_MISSING".to_string())?;
        is_error |= !success;
    }
    Ok(is_error)
}

fn tools_list() -> Result<Value, String> {
    let mut schema: Value =
        serde_json::from_str(include_str!("../../../tools/src/command_run/schema.json"))
            .map_err(|error| format!("TURA_COMMAND_GRAPH_SCHEMA_INVALID:{error}"))?;
    schema["input_schema"]["properties"]["execution_id"]["description"] = json!(
        "Optional stable identity for one tool call within this task execution. Reuse it only for a retransmission of the same intended effect. Use a new identity, or omit it, for a new read/test after an edit. Omitted identity uses the MCP request ID. Do not automatically retry an uncertain effect with a new identity."
    );
    Ok(json!({
        "tools": [{
            "name": TOOL_NAME,
            "description": schema["description"],
            "inputSchema": schema["input_schema"],
            "annotations": {
                "readOnlyHint": false,
                "destructiveHint": false,
                "idempotentHint": false,
                "openWorldHint": false
            }
        }]
    }))
}

async fn write_response(stdout: &mut tokio::io::Stdout, response: Value) -> Result<(), String> {
    let mut bytes = serde_json::to_vec(&response)
        .map_err(|error| format!("TURA_COMMAND_GRAPH_RESPONSE_ENCODE_FAILED:{error}"))?;
    bytes.push(b'\n');
    stdout
        .write_all(&bytes)
        .await
        .map_err(|error| format!("TURA_COMMAND_GRAPH_STDOUT_FAILED:{error}"))?;
    stdout
        .flush()
        .await
        .map_err(|error| format!("TURA_COMMAND_GRAPH_FLUSH_FAILED:{error}"))
}

fn json_rpc_result(id: Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

fn json_rpc_error(id: Value, code: i64, message: String) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message}
    })
}

fn required_env(name: &str) -> Result<String, String> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| format!("TURA_COMMAND_GRAPH_ENV_MISSING:{name}"))
}

fn canonical_json(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => serde_json::to_string(value).unwrap_or_default(),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(canonical_json)
                .collect::<Vec<_>>()
                .join(",")
        ),
        Value::Object(values) => {
            let mut keys = values.keys().collect::<Vec<_>>();
            keys.sort();
            format!(
                "{{{}}}",
                keys.into_iter()
                    .map(|key| format!(
                        "{}:{}",
                        serde_json::to_string(key).unwrap_or_default(),
                        canonical_json(&values[key])
                    ))
                    .collect::<Vec<_>>()
                    .join(",")
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn context() -> CommandGraphContext {
        CommandGraphContext {
            session_id: "session".into(), task_id: "task".into(),
            execution_id: "execution".into(), lease_id: "lease".into(),
            workspace: PathBuf::from("/tmp"), allowed_commands: BTreeSet::new(),
            jspace_contract: json!({}),
        }
    }

    #[test]
    fn repeated_content_is_a_new_call_but_retransmission_keeps_identity() {
        let c = context();
        let args = json!({"commands":[{"command_type":"shell_command","command_line":"cat input.txt"}]});
        let first = c.bind_arguments(args.clone(), &json!(1)).unwrap();
        assert_eq!(first, c.bind_arguments(args.clone(), &json!(1)).unwrap());
        assert_ne!(first["execution_id"], c.bind_arguments(args, &json!(2)).unwrap()["execution_id"]);
    }

    #[test]
    fn explicit_call_identity_survives_transport_retry_and_payload_changes() {
        let c = context();
        let args = json!({"execution_id":"effect-1","commands":[]});
        let first = c.bind_arguments(args.clone(), &json!(1)).unwrap();
        assert_eq!(first, c.bind_arguments(args, &json!(2)).unwrap());
        let changed = c.bind_arguments(json!({"execution_id":"effect-1","commands":[{}]}), &json!(3)).unwrap();
        assert_eq!(first["execution_id"], changed["execution_id"]);
        assert!(c.bind_arguments(json!({"execution_id":false}), &json!(1)).is_err());
        assert!(c.bind_arguments(json!({}), &Value::Null).is_err());
    }

    #[test]
    fn tool_schema_does_not_advertise_fresh_effect_calls_as_idempotent() {
        let tools = tools_list().unwrap();
        assert_eq!(tools["tools"][0]["annotations"]["idempotentHint"], false);
        let description = tools["tools"][0]["inputSchema"]["properties"]["execution_id"]["description"]
            .as_str().unwrap();
        assert!(description.contains("retransmission"));
        assert!(description.contains("new read/test after an edit"));
    }
}
