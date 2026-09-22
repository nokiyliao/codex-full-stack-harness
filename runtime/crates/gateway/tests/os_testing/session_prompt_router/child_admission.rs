use super::helpers::*;
use axum::body::{Body, to_bytes};
use axum::http::{Method, Request, StatusCode};
use tower::ServiceExt;

fn admission_request() -> Value {
    json!({
        "parent_session_id": "parent-1",
        "parent_mission_revision_sha256": "a".repeat(64),
        "commander_thread_id": "commander-thread-1",
        "child_session_id": "child-1",
        "child_runtime_id": "runtime-1",
        "child_transaction_id": "callback-1",
        "child_lease_id": "lease-1",
        "callback_request_id": "callback-1",
        "effect_id": "runtime-1.message",
        "callback_delivery_route": "trusted_tura_direct_thread_writer",
        "delegated_input_sha256": "b".repeat(64),
        "session_directory": "/tmp/child-1",
        "session_name": "delegated child",
        "created_at_ms": 1_788_000_000_000_i64,
        "execution_payload": {"prompt": "perform delegated work"}
    })
}

async fn post_admission(parent_id: &str, payload: &Value) -> Result<(StatusCode, Value)> {
    let request = Request::builder()
        .method(Method::POST)
        .uri(format!("/session/{parent_id}/children"))
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(payload)?))?;
    let response = gateway::web::build_router().oneshot(request).await?;
    let status = response.status();
    let body = to_bytes(response.into_body(), 1024 * 1024).await?;
    Ok((status, serde_json::from_slice(&body)?))
}

async fn commander_request(
    method: Method,
    path: &str,
    payload: Option<&Value>,
) -> Result<(StatusCode, Value)> {
    let mut request = Request::builder().method(method).uri(path);
    let body = if let Some(payload) = payload {
        request = request.header("content-type", "application/json");
        Body::from(serde_json::to_vec(payload)?)
    } else {
        Body::empty()
    };
    let response = gateway::web::build_router()
        .oneshot(request.body(body)?)
        .await?;
    let status = response.status();
    let body = to_bytes(response.into_body(), 1024 * 1024).await?;
    Ok((status, serde_json::from_slice(&body)?))
}

fn task_packet() -> Value {
    json!({
        "schema_version": "tura_commander_task_packet_v1",
        "parent_session_id": "parent-1",
        "parent_mission_revision_sha256": "a".repeat(64),
        "commander_thread_id": "commander-thread-1",
        "task_id": "task-1",
        "session_directory": "/tmp/workspace/child",
        "session_name": "delegated child",
        "created_at_ms": 1_788_000_000_000_i64,
        "prompt": "perform delegated work",
        "model": "official_codex_app_server/gpt-5.6-sol",
        "agent": "balanced",
        "maximum_parallel_runtime_workers": 4,
        "task_context_capsule": {"schema_version": "task_context_capsule_v1"},
        "jspace_contract": {"schema_version": "jspace_contract_v2"}
    })
}

fn compile_response() -> Value {
    json!({
        "schema_version": "tura_commander_task_packet_compile_result_v1",
        "protocol_version": "tura_commander_dispatch_protocol_v1",
        "task_packet_schema_version": "tura_commander_task_packet_v1",
        "compile_identity_sha256": "1".repeat(64),
        "semantic_dispatch_key": "2".repeat(64),
        "parent_task_plan_sha256": "3".repeat(64),
        "task_scheduling_contract_sha256": "4".repeat(64),
        "scope_claim_sha256": "5".repeat(64),
        "task_context_capsule_semantic_sha256": "6".repeat(64),
        "jspace_authorization_semantic_sha256": "7".repeat(64),
        "delegated_input_sha256": "8".repeat(64),
        "parent_session_id": "parent-1",
        "parent_mission_revision_sha256": "a".repeat(64),
        "commander_thread_id": "commander-thread-1",
        "task_id": "task-1",
        "child_session_id": format!("child-{}", "1".repeat(64)),
        "child_runtime_id": format!("runtime-{}", "1".repeat(64)),
        "child_transaction_id": format!("transaction-{}", "1".repeat(64)),
        "child_lease_id": format!("lease-{}", "1".repeat(64)),
        "callback_request_id": format!("transaction-{}", "1".repeat(64)),
        "effect_id": format!("runtime-{}.message", "1".repeat(64)),
        "callback_delivery_route": "trusted_tura_direct_thread_writer",
        "authority_effect": "none",
        "mutation_counts": {"parent_claim": 0, "child": 0, "runtime": 0, "callback": 0}
    })
}

fn callback_ack_request() -> Value {
    json!({
        "parent_session_id": "parent-1",
        "parent_mission_revision_sha256": "a".repeat(64),
        "commander_thread_id": "commander-thread-1",
        "child_session_id": "child-1",
        "child_runtime_id": "runtime-1",
        "child_lease_id": "lease-1",
        "transaction_id": "callback-1",
        "event_id": "event-1",
        "callback_payload_sha256": "b".repeat(64),
        "effect_identity": {"kind": "exact", "effect_id": "runtime-1.message"}
    })
}

async fn post_callback_ack(
    parent_id: &str,
    child_id: &str,
    payload: &Value,
) -> Result<(StatusCode, Value)> {
    let request = Request::builder()
        .method(Method::POST)
        .uri(format!(
            "/session/{parent_id}/children/{child_id}/callback/ack"
        ))
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(payload)?))?;
    let response = gateway::web::build_router().oneshot(request).await?;
    let status = response.status();
    let body = to_bytes(response.into_body(), 1024 * 1024).await?;
    Ok((status, serde_json::from_slice(&body)?))
}

#[tokio::test]
async fn public_gateway_child_route_forwards_exact_contract_and_replay() -> Result<()> {
    let _guard = ENV_LOCK.lock().await;
    let root = tempfile::tempdir().context("temp root")?;
    let home = root.path().join("home");
    std::fs::create_dir_all(&home)?;
    let source_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let _env = EnvGuard::new(&home, &source_root);
    let response = json!({
        "outcome": "admitted",
        "parent_session_id": "parent-1",
        "child_session_id": "child-1",
        "child_runtime_id": "runtime-1",
        "child_transaction_id": "callback-1",
        "callback_request_id": "callback-1",
        "effect_id": "runtime-1.message",
        "callback_delivery_route": "trusted_tura_direct_thread_writer"
    });
    let mut replay_response = response.clone();
    replay_response["outcome"] = Value::String("already_admitted".to_string());
    let router = FakeRouter::start(
        &home,
        vec![
            RouterReply::Payload(response.clone()),
            RouterReply::Payload(replay_response.clone()),
        ],
    )?;
    let payload = admission_request();

    let (status, first_body) = post_admission("parent-1", &payload).await?;
    assert_eq!(status, StatusCode::OK, "{first_body}");
    assert_eq!(first_body, response);
    let first = router.next_request(Duration::from_secs(10))?;
    assert_eq!(
        first["method"],
        router_contract::METHOD_REGISTER_CHILD_SESSION
    );
    assert_eq!(first["payload"], payload);

    let (status, second_body) = post_admission("parent-1", &payload).await?;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(second_body, replay_response);
    let second = router.next_request(Duration::from_secs(10))?;
    assert_eq!(second["method"], first["method"]);
    assert_eq!(second["payload"], first["payload"]);

    let (status, _) = post_admission("different-parent", &payload).await?;
    assert_eq!(status, StatusCode::CONFLICT);
    let mut missing_effect = payload;
    missing_effect["effect_id"] = Value::String(String::new());
    let (status, _) = post_admission("parent-1", &missing_effect).await?;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let mut missing_commander_thread = admission_request();
    missing_commander_thread
        .as_object_mut()
        .expect("admission object")
        .remove("commander_thread_id");
    let (status, body) = post_admission("parent-1", &missing_commander_thread).await?;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");

    let mut blank_commander_thread = admission_request();
    blank_commander_thread["commander_thread_id"] = Value::String("   ".to_string());
    let (status, body) = post_admission("parent-1", &blank_commander_thread).await?;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");

    let mut missing_route = admission_request();
    missing_route
        .as_object_mut()
        .expect("admission object")
        .remove("callback_delivery_route");
    let (status, body) = post_admission("parent-1", &missing_route).await?;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");

    let mut unknown_route = admission_request();
    unknown_route["callback_delivery_route"] = Value::String("codex_owned_adapter".to_string());
    let (status, body) = post_admission("parent-1", &unknown_route).await?;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");

    drop(router);
    Ok(())
}

#[tokio::test]
async fn commander_task_packet_routes_capability_compile_dispatch_and_zero_effect_error()
-> Result<()> {
    let _guard = ENV_LOCK.lock().await;
    let root = tempfile::tempdir().context("temp root")?;
    let home = root.path().join("home");
    std::fs::create_dir_all(&home)?;
    let source_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let _env = EnvGuard::new(&home, &source_root);
    let capabilities = json!({
        "schema_version": "tura_commander_task_packet_capabilities_v1",
        "protocol_version": "tura_commander_dispatch_protocol_v1",
        "task_packet_schema_versions": ["tura_commander_task_packet_v1"],
        "callback_delivery_route": "trusted_tura_direct_thread_writer",
        "compile_only": true,
        "idempotent_replay": true
    });
    let compilation = compile_response();
    let dispatch = json!({
        "schema_version": "tura_commander_task_packet_dispatch_response_v1",
        "compilation": compilation.clone(),
        "admission": {
            "outcome": "admitted",
            "parent_session_id": "parent-1",
            "child_session_id": format!("child-{}", "1".repeat(64)),
            "child_runtime_id": format!("runtime-{}", "1".repeat(64)),
            "child_transaction_id": format!("transaction-{}", "1".repeat(64)),
            "callback_request_id": format!("transaction-{}", "1".repeat(64)),
            "effect_id": format!("runtime-{}.message", "1".repeat(64)),
            "callback_delivery_route": "trusted_tura_direct_thread_writer"
        },
        "duplicate_effect_count": 0,
        "commander_mission_verification_required": false
    });
    let mut replay_dispatch = dispatch.clone();
    replay_dispatch["admission"]["outcome"] = Value::String("already_admitted".to_string());
    let router = FakeRouter::start(
        &home,
        vec![
            RouterReply::Payload(capabilities.clone()),
            RouterReply::Payload(compilation.clone()),
            RouterReply::Payload(dispatch.clone()),
            RouterReply::Payload(replay_dispatch.clone()),
            RouterReply::RawLine(
                json!({
                    "request_id": "task-packet-pre-admission",
                    "ok": false,
                    "payload": null,
                    "error": "TASK_PACKET_PRE_ADMISSION:TASK_PACKET_MISSION_REVISION_MISMATCH"
                })
                .to_string(),
            ),
            RouterReply::RawLine(
                json!({
                    "request_id": "task-packet-post-claim",
                    "ok": false,
                    "payload": null,
                    "error": "TASK_PACKET_DISPATCH_FAILED:INJECTED_POST_CLAIM_FAILURE"
                })
                .to_string(),
            ),
        ],
    )?;
    let packet = task_packet();

    let (status, body) = commander_request(Method::GET, "/commander/capabilities", None).await?;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body, capabilities);
    let request = router.next_request(Duration::from_secs(10))?;
    assert_eq!(
        request["method"],
        router_contract::METHOD_COMMANDER_TASK_PACKET_CAPABILITIES
    );

    let (status, body) = commander_request(
        Method::POST,
        "/commander/task-packet/compile",
        Some(&packet),
    )
    .await?;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body, compilation);
    let request = router.next_request(Duration::from_secs(10))?;
    assert_eq!(
        request["method"],
        router_contract::METHOD_COMPILE_COMMANDER_TASK_PACKET
    );
    assert_eq!(request["payload"], packet);

    let (status, body) = commander_request(
        Method::POST,
        "/commander/task-packet/dispatch",
        Some(&packet),
    )
    .await?;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body, dispatch);
    let request = router.next_request(Duration::from_secs(10))?;
    assert_eq!(
        request["method"],
        router_contract::METHOD_DISPATCH_COMMANDER_TASK_PACKET
    );
    assert_eq!(request["payload"], packet);

    let (status, body) = commander_request(
        Method::POST,
        "/commander/task-packet/dispatch",
        Some(&packet),
    )
    .await?;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body, replay_dispatch);
    assert_eq!(body["admission"]["outcome"], "already_admitted");
    assert_eq!(body["duplicate_effect_count"], 0);
    let replay = router.next_request(Duration::from_secs(10))?;
    assert_eq!(replay["method"], request["method"]);
    assert_eq!(replay["payload"], request["payload"]);

    let (status, body) = commander_request(
        Method::POST,
        "/commander/task-packet/dispatch",
        Some(&packet),
    )
    .await?;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["phase"], "pre_admission");
    assert_eq!(body["authority_effect"], "none");
    assert_eq!(body["auto_retry_allowed"], false);
    assert_eq!(
        body["mutation_counts"],
        json!({"parent_claim": 0, "child": 0, "runtime": 0, "callback": 0})
    );
    let request = router.next_request(Duration::from_secs(10))?;
    assert_eq!(
        request["method"],
        router_contract::METHOD_DISPATCH_COMMANDER_TASK_PACKET
    );

    let (status, body) = commander_request(
        Method::POST,
        "/commander/task-packet/dispatch",
        Some(&packet),
    )
    .await?;
    assert_eq!(status, StatusCode::BAD_GATEWAY, "{body}");
    assert_eq!(body["phase"], "post_compile_dispatch");
    assert_eq!(body["authority_effect"], "unsettled");
    assert_eq!(body["auto_retry_allowed"], false);
    assert_eq!(body["commander_mission_verification_required"], true);
    assert_eq!(body["mutation_counts"], Value::Null);
    let request = router.next_request(Duration::from_secs(10))?;
    assert_eq!(
        request["method"],
        router_contract::METHOD_DISPATCH_COMMANDER_TASK_PACKET
    );

    drop(router);
    Ok(())
}

#[tokio::test]
async fn public_gateway_child_callback_ack_forwards_exact_contract_and_paths() -> Result<()> {
    let _guard = ENV_LOCK.lock().await;
    let root = tempfile::tempdir().context("temp root")?;
    let home = root.path().join("home");
    std::fs::create_dir_all(&home)?;
    let source_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let _env = EnvGuard::new(&home, &source_root);
    let response = json!({
        "outcome": "acknowledged",
        "parent_session_id": "parent-1",
        "parent_mission_revision_sha256": "a".repeat(64),
        "commander_thread_id": "commander-thread-1",
        "child_session_id": "child-1",
        "child_runtime_id": "runtime-1",
        "child_lease_id": "lease-1",
        "transaction_id": "callback-1",
        "event_id": "event-1",
        "callback_payload_sha256": "b".repeat(64),
        "effect_identity": {"kind": "exact", "effect_id": "runtime-1.message"}
    });
    let mut replay_response = response.clone();
    replay_response["outcome"] = Value::String("already_acknowledged".to_string());
    let router = FakeRouter::start(
        &home,
        vec![
            RouterReply::Payload(response.clone()),
            RouterReply::Payload(replay_response.clone()),
            RouterReply::RawLine(
                json!({
                    "request_id": "unsettled-callback-ack",
                    "ok": false,
                    "payload": null,
                    "error": "CONTINUATION_DELIVERY_UNSETTLED_NO_BLIND_RETRY:callback-1"
                })
                .to_string(),
            ),
        ],
    )?;
    let payload = callback_ack_request();

    let (status, first_body) = post_callback_ack("parent-1", "child-1", &payload).await?;
    assert_eq!(status, StatusCode::OK, "{first_body}");
    assert_eq!(first_body, response);
    let first = router.next_request(Duration::from_secs(10))?;
    assert_eq!(
        first["method"],
        router_contract::METHOD_ACKNOWLEDGE_CHILD_CALLBACK
    );
    assert_eq!(first["payload"], payload);

    let (status, second_body) = post_callback_ack("parent-1", "child-1", &payload).await?;
    assert_eq!(status, StatusCode::OK, "{second_body}");
    assert_eq!(second_body, replay_response);
    let second = router.next_request(Duration::from_secs(10))?;
    assert_eq!(second["method"], first["method"]);
    assert_eq!(second["payload"], first["payload"]);

    let (status, unsettled_body) = post_callback_ack("parent-1", "child-1", &payload).await?;
    assert_eq!(status, StatusCode::CONFLICT, "{unsettled_body}");
    assert_eq!(unsettled_body["error"], "child_callback_ack_failed");
    assert_eq!(
        unsettled_body["message"],
        "CONTINUATION_DELIVERY_UNSETTLED_NO_BLIND_RETRY:callback-1"
    );
    let unsettled = router.next_request(Duration::from_secs(10))?;
    assert_eq!(unsettled["method"], first["method"]);
    assert_eq!(unsettled["payload"], first["payload"]);

    assert_eq!(
        post_callback_ack("other-parent", "child-1", &payload)
            .await?
            .0,
        StatusCode::CONFLICT
    );
    assert_eq!(
        post_callback_ack("parent-1", "other-child", &payload)
            .await?
            .0,
        StatusCode::CONFLICT
    );
    drop(router);
    Ok(())
}
