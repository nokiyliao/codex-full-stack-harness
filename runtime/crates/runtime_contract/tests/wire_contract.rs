use runtime_contract::{
    CallContext, DEFAULT_MAXIMUM_PARALLEL_RUNTIME_WORKERS, DEFAULT_MAXIMUM_RUNTIME_LLM_TURNS,
    MAXIMUM_PARALLEL_RUNTIME_WORKER_OPTIONS, MAXIMUM_RUNTIME_LLM_TURN_OPTIONS,
    NATIVE_CODEX_EXECUTION_BINDING_SCHEMA_VERSION, NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION,
    NativeCodexExecutionBinding, NativeCodexTaskDelta, RunAgentRequest, RuntimeWorkerResponse,
    TASK_CONTEXT_CAPSULE_SCHEMA_VERSION, TaskContextCapsule, WORKER_KIND_CALL,
    WORKER_KIND_HEALTH_CHECK, WorkerEnvelope, maximum_parallel_runtime_workers,
    maximum_runtime_llm_turns,
};
use serde_json::json;

#[test]
fn worker_envelopes_preserve_the_existing_wire_shape() {
    assert_eq!(
        serde_json::to_value(WorkerEnvelope::health_check()).expect("health envelope"),
        json!({ "kind": WORKER_KIND_HEALTH_CHECK, "payload": {} })
    );
    let call = WorkerEnvelope::call(CallContext {
        request_id: "request-1".to_string(),
        method: "POST".to_string(),
        path: "/runtime_worker/session-1".to_string(),
        input: json!({ "session_id": "session-1", "prompt": "hello" }),
    });
    assert_eq!(call.kind, WORKER_KIND_CALL);
    assert_eq!(call.payload["input"]["request_id"], "request-1");
    assert_eq!(call.payload["input"]["input"]["prompt"], "hello");
}

#[test]
fn task_context_capsule_validates_digest_and_jspace_binding() {
    let jspace_digest = "a".repeat(64);
    let mut value = json!({
        "schema_version": TASK_CONTEXT_CAPSULE_SCHEMA_VERSION,
        "mission": {
            "mission_id": "mission-1",
            "task_id": "task-1",
            "mode": "GOVERNANCE",
            "current_predicate": "source.compiles",
            "objective": "Compile source"
        },
        "context_summary": "Use only the bound source and focused tests.",
        "dcf_generation": {"generation_id": "generation-1"},
        "surface": {"repo_root": "/workspace", "matched_surface_ids": ["surface-1"]},
        "authority": {"forbidden_effects": ["live_runtime"]},
        "evidence_refs": [{"id": "receipt-1", "kind": "receipt", "sha256": "b".repeat(64)}],
        "focused_verifiers": [{"command": "cargo test -p runtime_contract"}],
        "jspace_semantic_sha256": jspace_digest,
    });
    let digest = super_semantic_sha256(&value);
    value["semantic_sha256"] = json!(digest);

    let capsule = TaskContextCapsule::from_value(value).expect("valid capsule");
    capsule
        .bind_jspace(Some(&json!({"semantic_sha256": "a".repeat(64)})))
        .expect("matching J-Space digest");
    capsule
        .bind_jspace(Some(&json!({
            "authorization_semantic_sha256": "a".repeat(64),
            "content_sha256": "d".repeat(64)
        })))
        .expect("v2 binds authorization rather than provenance content");
    assert!(
        capsule
            .bind_jspace(Some(&json!({"semantic_sha256": "c".repeat(64)})))
            .unwrap_err()
            .contains("TASK_CONTEXT_JSPACE_BINDING_MISMATCH")
    );
}

#[test]
fn native_codex_task_delta_is_revision_bound_and_rejects_host_context_aliases() {
    let mut value = json!({
        "schema_version": NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION,
        "mission_id": "mission-1",
        "mission_revision_sha256": "c".repeat(64),
        "task_id": "task-1",
        "current_predicate": "source.compiles",
        "instruction": "Compile the exact bounded source and return the terminal result."
    });
    value["semantic_sha256"] = json!(super_semantic_sha256(&value));
    NativeCodexTaskDelta::from_value(value).expect("valid task delta");

    let mut aliased_history = json!({
        "schema_version": NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION,
        "mission_id": "mission-1",
        "mission_revision_sha256": "c".repeat(64),
        "task_id": "task-1",
        "current_predicate": "source.compiles",
        "instruction": "<skills_instructions>full host bootstrap</skills_instructions>"
    });
    aliased_history["semantic_sha256"] = json!(super_semantic_sha256(&aliased_history));
    assert_eq!(
        NativeCodexTaskDelta::from_value(aliased_history).unwrap_err(),
        "NATIVE_CODEX_TASK_DELTA_RESERVED_CONTEXT_MARKER"
    );
}

#[test]
fn native_codex_execution_binding_requires_authoritative_task_evidence() {
    let instruction = "Return the bounded durable result.";
    let mission_revision_sha256 = "c".repeat(64);
    let mut delta_value = json!({
        "schema_version": NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION,
        "mission_id": "mission-native-binding",
        "mission_revision_sha256": mission_revision_sha256,
        "task_id": "task-native-binding",
        "current_predicate": "P1_BOUNDED_CONTEXT_AND_TOOL_OWNERSHIP",
        "instruction": instruction,
    });
    delta_value["semantic_sha256"] = json!(super_semantic_sha256(&delta_value));
    let delta = NativeCodexTaskDelta::from_value(delta_value).expect("valid task delta");

    let mut binding_value = json!({
        "schema_version": NATIVE_CODEX_EXECUTION_BINDING_SCHEMA_VERSION,
        "execution_profile_sha256": "d".repeat(64),
        "task_delta": delta,
        "codex_executable": "/Applications/ChatGPT.app/Contents/Resources/codex",
        "codex_executable_sha256": "e".repeat(64),
        "command_graph_executable": "/tmp/tura_command_graph",
        "command_graph_executable_sha256": "f".repeat(64),
        "command_graph_allowed_commands": ["zsh"],
        "sandbox": "read_only",
        "timeout_ms": 300000,
    });
    binding_value["semantic_sha256"] = json!(super_semantic_sha256(&binding_value));
    let binding =
        NativeCodexExecutionBinding::from_value(binding_value).expect("valid execution binding");
    let mut noncanonical_binding =
        serde_json::to_value(&binding).expect("serialize execution binding");
    noncanonical_binding["command_graph_allowed_commands"] = json!(["zsh", "zsh"]);
    noncanonical_binding
        .as_object_mut()
        .expect("binding object")
        .remove("semantic_sha256");
    noncanonical_binding["semantic_sha256"] = json!(super_semantic_sha256(&noncanonical_binding));
    assert_eq!(
        NativeCodexExecutionBinding::from_value(noncanonical_binding).unwrap_err(),
        "NATIVE_CODEX_EXECUTION_BINDING_ALLOWLIST_NOT_CANONICAL"
    );
    let mut writable_binding = serde_json::to_value(&binding).expect("serialize execution binding");
    writable_binding["sandbox"] = json!("workspace_write");
    writable_binding
        .as_object_mut()
        .expect("binding object")
        .remove("semantic_sha256");
    writable_binding["semantic_sha256"] = json!(super_semantic_sha256(&writable_binding));
    assert_eq!(
        NativeCodexExecutionBinding::from_value(writable_binding).unwrap_err(),
        "NATIVE_CODEX_EXECUTION_BINDING_REQUIRES_READ_ONLY_SANDBOX"
    );

    let mut capsule_value = json!({
        "schema_version": TASK_CONTEXT_CAPSULE_SCHEMA_VERSION,
        "mission": {
            "mission_id": "mission-native-binding",
            "task_id": "task-native-binding",
            "mode": "DELIVERY",
            "current_predicate": "P1_BOUNDED_CONTEXT_AND_TOOL_OWNERSHIP",
            "objective": "Run the exact durable Native Codex task"
        },
        "context_summary": "Use only the task-bound Native Codex execution binding.",
        "dcf_generation": {"generation_id": "generation-native-binding"},
        "surface": {"repo_root": "/workspace"},
        "authority": {"authority_effect": "none"},
        "evidence_refs": [{
            "id": "native-codex-execution-binding",
            "kind": "execution_binding",
            "sha256": binding.semantic_sha256
        }],
        "focused_verifiers": [],
        "jspace_semantic_sha256": "a".repeat(64),
    });
    capsule_value["semantic_sha256"] = json!(super_semantic_sha256(&capsule_value));
    let capsule = TaskContextCapsule::from_value(capsule_value).expect("valid capsule");

    binding
        .bind_authoritative_task(
            &capsule,
            "task-native-binding",
            &mission_revision_sha256,
            instruction,
        )
        .expect("durable packet identities bind");
    assert_eq!(
        binding
            .bind_authoritative_task(
                &capsule,
                "task-native-binding",
                &mission_revision_sha256,
                "a different prompt",
            )
            .unwrap_err(),
        "NATIVE_CODEX_EXECUTION_BINDING_TASK_IDENTITY_MISMATCH"
    );

    let mut unbound_value = serde_json::to_value(&capsule).expect("serialize capsule");
    unbound_value["evidence_refs"] = json!([]);
    unbound_value
        .as_object_mut()
        .expect("capsule object")
        .remove("semantic_sha256");
    unbound_value["semantic_sha256"] = json!(super_semantic_sha256(&unbound_value));
    let unbound =
        TaskContextCapsule::from_value(unbound_value).expect("valid capsule without binding ref");
    assert_eq!(
        binding
            .bind_authoritative_task(
                &unbound,
                "task-native-binding",
                &mission_revision_sha256,
                instruction,
            )
            .unwrap_err(),
        "NATIVE_CODEX_EXECUTION_BINDING_NOT_IN_TASK_EVIDENCE"
    );
}

fn super_semantic_sha256(value: &serde_json::Value) -> String {
    use sha2::{Digest, Sha256};

    fn canonical(value: &serde_json::Value) -> String {
        match value {
            serde_json::Value::Null => "null".to_string(),
            serde_json::Value::Bool(value) => value.to_string(),
            serde_json::Value::Number(value) => value.to_string(),
            serde_json::Value::String(value) => serde_json::to_string(value).unwrap(),
            serde_json::Value::Array(values) => format!(
                "[{}]",
                values.iter().map(canonical).collect::<Vec<_>>().join(",")
            ),
            serde_json::Value::Object(values) => {
                let mut keys = values.keys().collect::<Vec<_>>();
                keys.sort();
                format!(
                    "{{{}}}",
                    keys.into_iter()
                        .map(|key| format!(
                            "{}:{}",
                            serde_json::to_string(key).unwrap(),
                            canonical(&values[key])
                        ))
                        .collect::<Vec<_>>()
                        .join(",")
                )
            }
        }
    }

    format!("{:x}", Sha256::digest(canonical(value).as_bytes()))
}

#[test]
fn run_agent_request_is_strict_and_defaults_optional_worker_inputs() {
    let request: RunAgentRequest = serde_json::from_value(json!({
        "runtime_id": "runtime-1",
        "lease_id": "lease-1",
        "session_id": "session-1",
        "prompt": "hello",
        "jspace_contract": {
            "schema_version": "jspace_contract_v1",
            "semantic_sha256": "jspace-digest"
        }
    }))
    .expect("run-agent request");
    assert_eq!(request.runtime_id, "runtime-1");
    assert_eq!(request.lease_id, "lease-1");
    assert_eq!(request.session_id.as_deref(), Some("session-1"));
    assert_eq!(request.prompt.as_deref(), Some("hello"));
    assert_eq!(
        request.jspace_contract.as_ref().and_then(|contract| {
            contract
                .get("semantic_sha256")
                .and_then(serde_json::Value::as_str)
        }),
        Some("jspace-digest")
    );
    assert!(!request.no_op_manual);
    assert!(!request.return_log);
    assert_eq!(request.maximum_parallel_runtime_workers, None);
    assert!(request.worker_env.is_empty());
    assert!(
        serde_json::from_value::<RunAgentRequest>(json!({
            "runtime_id": "runtime-1",
            "turn_id": "legacy"
        }))
        .is_err()
    );
}

#[test]
fn runtime_setting_catalogs_keep_supported_defaults_and_reject_other_values() {
    assert_eq!(
        MAXIMUM_PARALLEL_RUNTIME_WORKER_OPTIONS,
        [6, 12, 24, 48, 128]
    );
    assert_eq!(
        MAXIMUM_RUNTIME_LLM_TURN_OPTIONS,
        [64, 128, 256, 1_080, 2_560]
    );
    assert_eq!(
        maximum_parallel_runtime_workers(None),
        DEFAULT_MAXIMUM_PARALLEL_RUNTIME_WORKERS
    );
    assert_eq!(maximum_parallel_runtime_workers(Some(48)), 48);
    assert_eq!(
        maximum_parallel_runtime_workers(Some(7)),
        DEFAULT_MAXIMUM_PARALLEL_RUNTIME_WORKERS
    );
    assert_eq!(
        maximum_runtime_llm_turns(None),
        DEFAULT_MAXIMUM_RUNTIME_LLM_TURNS
    );
    assert_eq!(maximum_runtime_llm_turns(Some(1_080)), 1_080);
    assert_eq!(
        maximum_runtime_llm_turns(Some(1)),
        DEFAULT_MAXIMUM_RUNTIME_LLM_TURNS
    );
}
#[test]
fn runtime_worker_response_rejects_unknown_fields_and_uses_typed_state() {
    let response: RuntimeWorkerResponse = serde_json::from_value(json!({
        "ok": true,
        "session_id": "session-1",
        "session_state": "completed",
        "message_count": 3,
        "turn_started_at_ms": 42,
        "final_text": "done",
        "session_log": []
    }))
    .expect("runtime response");
    assert_eq!(
        response.session_state,
        Some(lifecycle::SessionState::Completed)
    );
    assert!(
        serde_json::from_value::<RuntimeWorkerResponse>(json!({
            "ok": true,
            "legacy_status": "done"
        }))
        .is_err()
    );
}
