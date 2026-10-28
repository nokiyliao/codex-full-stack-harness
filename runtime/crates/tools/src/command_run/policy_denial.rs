//! Router-owned witness that J-Space refused one command before dispatch.
//!
//! A non-attempt: not success, retry permission or task-quality proof. Only the router publishes
//! it, before batch admission, into the process-owned receipt store under a name derived from the
//! command's call identity. A callback, assistant text or diagnostic that looks the same is not
//! proof; authentication reads the immutable witness from the store.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

pub const POLICY_DENIAL_SCHEMA: &str = "nokiy_jspace_policy_denial_v1";
const MAX_ID_BYTES: usize = 512;
const MAX_TEXT_BYTES: usize = 8192;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct JSpacePolicyDenial {
    schema_version: String,
    owner: String,
    session_id: String,
    runtime_id: String,
    execution_id: String,
    call_id: String,
    authorization_semantic_sha256: String,
    command_type: String,
    command_line_sha256: String,
    step: u64,
    jspace_error_code: String,
    operation: String,
    target: String,
    error_message: String,
    effect_state: String,
    process_started: bool,
    mutation_count: u64,
    authority_effect: String,
}

/// Store name of the witness for one command call.
pub fn policy_denial_receipt_name(session_id: &str, runtime_id: &str, call_id: &str) -> String {
    let identity =
        serde_json::to_vec(&(session_id, runtime_id, call_id)).expect("string tuple serialization");
    format!("jspace-policy-denial-{:x}.json", Sha256::digest(identity))
}

fn command_line_sha256(line: &str) -> String {
    format!("{:x}", Sha256::digest(line.as_bytes()))
}

fn command_step(command: &Value) -> Option<u64> {
    match command.get("step") {
        None | Some(Value::Null) => Some(1),
        Some(value) => value.as_u64().filter(|step| *step > 0),
    }
}

fn bounded(value: &str, max: usize) -> bool {
    !value.trim().is_empty() && value.len() <= max
}

impl JSpacePolicyDenial {
    #[expect(
        clippy::too_many_arguments,
        reason = "the denial record mirrors its serialized fields"
    )]
    pub fn new(
        session_id: &str,
        runtime_id: &str,
        execution_id: &str,
        call_id: &str,
        authorization: &str,
        command: &Value,
        code: &str,
        operation: &str,
        target: &str,
        error_message: &str,
    ) -> Option<Self> {
        let command_type = command["command_type"].as_str()?;
        let line = command["command_line"].as_str()?;
        let step = command_step(command)?;
        if ![session_id, runtime_id, execution_id, call_id]
            .iter()
            .all(|id| bounded(id, MAX_ID_BYTES))
            || authorization.len() != 64
            || !authorization.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
            || !code.starts_with("JSPACE_")
            || !bounded(code, 128)
            || operation.len() > 128
            || target.len() > MAX_TEXT_BYTES
            || !bounded(error_message, MAX_TEXT_BYTES)
            || !bounded(command_type, 64)
        {
            return None;
        }
        Some(Self {
            schema_version: POLICY_DENIAL_SCHEMA.to_owned(),
            owner: "router".to_owned(),
            session_id: session_id.to_owned(),
            runtime_id: runtime_id.to_owned(),
            execution_id: execution_id.to_owned(),
            call_id: call_id.to_owned(),
            authorization_semantic_sha256: authorization.to_owned(),
            command_type: command_type.to_owned(),
            command_line_sha256: command_line_sha256(line),
            step,
            jspace_error_code: code.to_owned(),
            operation: operation.to_owned(),
            target: target.to_owned(),
            error_message: error_message.to_owned(),
            effect_state: "not_started".to_owned(),
            process_started: false,
            mutation_count: 0,
            authority_effect: "none".to_owned(),
        })
    }

    pub fn receipt_name(&self) -> String {
        policy_denial_receipt_name(&self.session_id, &self.runtime_id, &self.call_id)
    }

    pub fn publish(&self, store: &tura_path::command_receipts::ReceiptStore) -> std::io::Result<()> {
        let bytes = serde_json::to_vec(self).map_err(std::io::Error::other)?;
        store.publish_new(&self.receipt_name(), &bytes)
    }

    /// The stored witness for this call describes exactly this command and the denial its
    /// result shows. An absent, foreign or different witness authenticates nothing.
    pub fn authenticate(
        store: &tura_path::command_receipts::ReceiptStore,
        session_id: &str,
        runtime_id: &str,
        call_id: &str,
        authorization: Option<&str>,
        command: &Value,
        result: &Value,
    ) -> bool {
        let Some(stored) = store
            .read(&policy_denial_receipt_name(session_id, runtime_id, call_id))
            .ok()
            .filter(|bytes| bytes.len() <= 4 * MAX_TEXT_BYTES)
            .and_then(|bytes| serde_json::from_slice::<Self>(&bytes).ok())
        else {
            return false;
        };
        stored.schema_version == POLICY_DENIAL_SCHEMA
            && stored.owner == "router"
            && stored.session_id == session_id
            && stored.runtime_id == runtime_id
            && stored.call_id == call_id
            && authorization.is_none_or(|authorization| stored.authorization_semantic_sha256 == authorization)
            && command["command_type"].as_str() == Some(stored.command_type.as_str())
            && command["command_line"]
                .as_str()
                .is_some_and(|line| command_line_sha256(line) == stored.command_line_sha256)
            && command_step(command) == Some(stored.step)
            && result["step"].as_u64() == Some(stored.step)
            && result["jspace_error_code"].as_str() == Some(stored.jspace_error_code.as_str())
            && result["operation"].as_str() == Some(stored.operation.as_str())
            && result["target"].as_str() == Some(stored.target.as_str())
            && result["error"].as_str() == Some(stored.error_message.as_str())
            && stored.effect_state == "not_started"
            && !stored.process_started
            && stored.mutation_count == 0
            && stored.authority_effect == "none"
    }
}
