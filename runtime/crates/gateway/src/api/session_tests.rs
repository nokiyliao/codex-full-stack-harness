use super::{
    SessionChangeRecord, SessionListParams, api_message_from_store, apply_single_change,
    config_model_override, filter_list_sessions, first_prompt_part_id, frontend_safe_reply_message,
    frontend_safe_value, prompt_command_run_shell, prompt_message_id, prompt_model_acceleration,
    prompt_model_variant, prompt_text, workspace_key,
};
use crate::contracts::{Session, SessionContextTokens, SessionStatus};
use crate::session::config::TuraSessionConfig;
use crate::session_db_client::SessionDbClient;
use crate::session_store;
use crate::test_support::SessionDbTestService;
use axum::{
    Json,
    extract::{Path, Query},
    http::HeaderMap,
};
use lifecycle::{
    PlanStatus, SessionCommand, SessionState, TASK_DISPATCH_CLAIM_SCHEMA_VERSION,
    TaskDispatchClaimV1, TaskPlan, TaskStep,
};
use router_contract::{
    AcknowledgeChildCallbackEffectIdentity, AcknowledgeChildCallbackOutcome,
    AcknowledgeChildCallbackResponse,
};
use std::fs;

async fn create_canonical_test_session(directory: String) -> Session {
    super::create_session_value(
        super::SessionDirectoryParams { directory: None },
        super::CreateSessionRequest {
            directory: Some(directory),
            session_type: Some("chat".to_string()),
            ..super::CreateSessionRequest::default()
        },
        None,
    )
    .await
    .expect("canonical test session should be created")
}

async fn create_child_callback_parent<F>(
    child_session_id: &str,
    outcome: AcknowledgeChildCallbackOutcome,
    task_plan: F,
) -> (Session, AcknowledgeChildCallbackResponse)
where
    F: FnOnce(&AcknowledgeChildCallbackResponse) -> TaskPlan,
{
    let directory = std::env::temp_dir()
        .join(format!(
            "tura-child-callback-parent-{}",
            uuid::Uuid::new_v4()
        ))
        .to_string_lossy()
        .to_string();
    let session = create_canonical_test_session(directory).await;
    let response = child_callback_ack_response(&session.id, child_session_id, outcome);
    session_store()
        .execute_canonical_session_command(
            &session.id,
            SessionCommand::ApplyTaskStatus {
                task_plan: task_plan(&response),
            },
        )
        .expect("callback parent task plan should be stored");
    session_store()
        .execute_canonical_session_command(
            &session.id,
            SessionCommand::ApplyRuntimeState {
                state: SessionState::Running,
            },
        )
        .expect("callback parent should start running without a runtime");
    (session, response)
}

fn child_callback_ack_response(
    parent_session_id: &str,
    child_session_id: &str,
    outcome: AcknowledgeChildCallbackOutcome,
) -> AcknowledgeChildCallbackResponse {
    AcknowledgeChildCallbackResponse {
        outcome,
        parent_session_id: parent_session_id.to_string(),
        parent_mission_revision_sha256: "a".repeat(64),
        commander_thread_id: "commander-thread".to_string(),
        child_session_id: child_session_id.to_string(),
        child_runtime_id: "child-runtime".to_string(),
        child_lease_id: "child-lease".to_string(),
        transaction_id: "child-transaction".to_string(),
        event_id: "child-event".to_string(),
        callback_payload_sha256: "b".repeat(64),
        effect_identity: AcknowledgeChildCallbackEffectIdentity::Exact {
            effect_id: "child-effect".to_string(),
        },
    }
}

fn child_callback_claim(
    task_id: &str,
    response: &AcknowledgeChildCallbackResponse,
) -> TaskDispatchClaimV1 {
    TaskDispatchClaimV1 {
        schema_version: TASK_DISPATCH_CLAIM_SCHEMA_VERSION.to_string(),
        mission_id: "callback-mission".to_string(),
        task_id: task_id.to_string(),
        child_session_id: response.child_session_id.clone(),
        child_runtime_id: response.child_runtime_id.clone(),
        child_lease_id: response.child_lease_id.clone(),
        child_transaction_id: response.transaction_id.clone(),
        semantic_dispatch_key: "c".repeat(64),
        authority_mission_revision_sha256: response.parent_mission_revision_sha256.clone(),
        delegated_input_sha256: "d".repeat(64),
        task_context_capsule_semantic_sha256: "e".repeat(64),
        parent_task_plan_sha256: "f".repeat(64),
        task_scheduling_contract_sha256: "1".repeat(64),
        scope_claim_sha256: "2".repeat(64),
    }
}

fn child_callback_task(
    task_id: &str,
    status: PlanStatus,
    response: &AcknowledgeChildCallbackResponse,
) -> TaskStep {
    TaskStep {
        task_id: task_id.to_string(),
        sub_session_id: response.child_session_id.clone(),
        status,
        dispatch_claim: Some(child_callback_claim(task_id, response)),
        task_summary: format!("Await {task_id}"),
        ..TaskStep::default()
    }
}

fn child_callback_parent_projection(session_id: &str) -> lifecycle::SessionProjection {
    session_store()
        .session_lifecycle_projection(session_id)
        .expect("callback parent lifecycle projection")
}

#[tokio::test]
async fn session_tests_acknowledged_child_callback_completes_exact_task_and_parent() {
    let _service = SessionDbTestService::start();
    let child_session_id = "child-callback-success";
    let (parent, response) = create_child_callback_parent(
        child_session_id,
        AcknowledgeChildCallbackOutcome::Acknowledged,
        |response| TaskPlan {
            plan_summary: "Callback parent".to_string(),
            detailed_tasks: vec![child_callback_task(
                "callback-task",
                PlanStatus::Doing,
                response,
            )],
        },
    )
    .await;

    let returned = super::converge_child_callback_ack_result(Ok(response.clone()))
        .expect("successful ACK should converge its parent");
    let projection = child_callback_parent_projection(&parent.id);

    assert_eq!(returned, response);
    assert_eq!(
        projection.task_plan.detailed_tasks[0].status,
        PlanStatus::Done
    );
    assert_eq!(projection.state, SessionState::Completed);
    assert!(projection.active_runtime_id.is_none());
    assert_eq!(
        session_store()
            .get_session(&parent.id)
            .map(|session| session.status),
        Some(SessionStatus::Idle)
    );
}

#[tokio::test]
async fn session_tests_acknowledged_child_callback_preserves_nonterminal_sibling() {
    let _service = SessionDbTestService::start();
    let child_session_id = "child-callback-with-sibling";
    let (parent, response) = create_child_callback_parent(
        child_session_id,
        AcknowledgeChildCallbackOutcome::Acknowledged,
        |response| TaskPlan {
            plan_summary: "Callback parent with sibling".to_string(),
            detailed_tasks: vec![
                child_callback_task("callback-task", PlanStatus::Doing, response),
                TaskStep {
                    task_id: "sibling-task".to_string(),
                    status: PlanStatus::Todo,
                    task_summary: "Continue parent work".to_string(),
                    ..TaskStep::default()
                },
            ],
        },
    )
    .await;

    super::converge_child_callback_ack_result(Ok(response))
        .expect("successful ACK should complete only its bound task");
    let projection = child_callback_parent_projection(&parent.id);

    assert_eq!(
        projection.task_plan.detailed_tasks[0].status,
        PlanStatus::Done
    );
    assert_eq!(
        projection.task_plan.detailed_tasks[1].status,
        PlanStatus::Todo
    );
    assert_eq!(projection.state, SessionState::Running);
    assert!(projection.active_runtime_id.is_none());
}

#[tokio::test]
async fn session_tests_acknowledged_child_callback_preserves_active_parent_runtime() {
    let _service = SessionDbTestService::start();
    let child_session_id = "child-callback-active-runtime";
    let (parent, response) = create_child_callback_parent(
        child_session_id,
        AcknowledgeChildCallbackOutcome::Acknowledged,
        |response| TaskPlan {
            plan_summary: "Callback parent with runtime".to_string(),
            detailed_tasks: vec![child_callback_task(
                "callback-task",
                PlanStatus::Doing,
                response,
            )],
        },
    )
    .await;
    session_store()
        .execute_canonical_session_command(
            &parent.id,
            SessionCommand::RuntimeStarted {
                runtime_id: "active-parent-runtime".to_string(),
            },
        )
        .expect("parent runtime should become active");

    super::converge_child_callback_ack_result(Ok(response))
        .expect("successful ACK should complete the task without closing an active runtime");
    let projection = child_callback_parent_projection(&parent.id);

    assert_eq!(
        projection.task_plan.detailed_tasks[0].status,
        PlanStatus::Done
    );
    assert_eq!(projection.state, SessionState::Running);
    assert_eq!(
        projection.active_runtime_id.as_deref(),
        Some("active-parent-runtime")
    );
}

#[tokio::test]
async fn session_tests_acknowledged_child_callback_missing_binding_fails_closed() {
    let _service = SessionDbTestService::start();
    let (parent, response) = create_child_callback_parent(
        "missing-child-binding",
        AcknowledgeChildCallbackOutcome::Acknowledged,
        |_| TaskPlan {
            plan_summary: "Unrelated parent work".to_string(),
            detailed_tasks: vec![TaskStep {
                task_id: "unrelated-task".to_string(),
                sub_session_id: "different-child".to_string(),
                status: PlanStatus::Doing,
                task_summary: "Unrelated work".to_string(),
                ..TaskStep::default()
            }],
        },
    )
    .await;
    let before = child_callback_parent_projection(&parent.id);

    let error = super::converge_child_callback_ack_result(Ok(response))
        .expect_err("missing child binding must fail closed");

    assert!(
        error
            .to_string()
            .contains("ACKNOWLEDGED_CHILD_CALLBACK_DISPATCH_CLAIM_MISSING")
    );
    assert_eq!(child_callback_parent_projection(&parent.id), before);
}

#[tokio::test]
async fn session_tests_acknowledged_child_callback_ambiguous_binding_fails_closed() {
    let _service = SessionDbTestService::start();
    let child_session_id = "ambiguous-child-binding";
    let (parent, response) = create_child_callback_parent(
        child_session_id,
        AcknowledgeChildCallbackOutcome::Acknowledged,
        |response| TaskPlan {
            plan_summary: "Ambiguous callback parent".to_string(),
            detailed_tasks: vec![
                child_callback_task("ambiguous-task-one", PlanStatus::Doing, response),
                child_callback_task("ambiguous-task-two", PlanStatus::Doing, response),
            ],
        },
    )
    .await;
    let before = child_callback_parent_projection(&parent.id);

    let error = super::converge_child_callback_ack_result(Ok(response))
        .expect_err("ambiguous child binding must fail closed");

    assert!(
        error
            .to_string()
            .contains("ACKNOWLEDGED_CHILD_CALLBACK_DISPATCH_CLAIM_AMBIGUOUS")
    );
    assert_eq!(child_callback_parent_projection(&parent.id), before);
}

#[tokio::test]
async fn session_tests_acknowledged_child_callback_mismatched_claim_fails_closed() {
    let _service = SessionDbTestService::start();
    let child_session_id = "mismatched-child-binding";
    let (parent, response) = create_child_callback_parent(
        child_session_id,
        AcknowledgeChildCallbackOutcome::Acknowledged,
        |response| {
            let mut task = child_callback_task("mismatched-task", PlanStatus::Doing, response);
            task.dispatch_claim
                .as_mut()
                .expect("claim fixture")
                .child_transaction_id = "different-transaction".to_string();
            TaskPlan {
                plan_summary: "Mismatched callback parent".to_string(),
                detailed_tasks: vec![task],
            }
        },
    )
    .await;
    let before = child_callback_parent_projection(&parent.id);

    let error = super::converge_child_callback_ack_result(Ok(response))
        .expect_err("mismatched durable claim must fail closed");

    assert!(
        error
            .to_string()
            .contains("ACKNOWLEDGED_CHILD_CALLBACK_DISPATCH_CLAIM_MISMATCH")
    );
    assert_eq!(child_callback_parent_projection(&parent.id), before);
}

#[test]
fn session_tests_child_callback_claim_identity_errors_are_conflicts() {
    for code in [
        "ACKNOWLEDGED_CHILD_CALLBACK_DISPATCH_CLAIM_MISSING",
        "ACKNOWLEDGED_CHILD_CALLBACK_DISPATCH_CLAIM_AMBIGUOUS",
        "ACKNOWLEDGED_CHILD_CALLBACK_DISPATCH_CLAIM_MISMATCH",
    ] {
        assert_eq!(
            super::child_callback_ack_error_status(code),
            axum::http::StatusCode::CONFLICT
        );
    }
}

#[tokio::test]
async fn session_tests_failed_child_callback_ack_does_not_mutate_parent() {
    let _service = SessionDbTestService::start();
    let (parent, _) = create_child_callback_parent(
        "child-with-failed-ack",
        AcknowledgeChildCallbackOutcome::Acknowledged,
        |response| TaskPlan {
            plan_summary: "Failed ACK parent".to_string(),
            detailed_tasks: vec![child_callback_task(
                "callback-task",
                PlanStatus::Doing,
                response,
            )],
        },
    )
    .await;
    let before = child_callback_parent_projection(&parent.id);

    let error = super::converge_child_callback_ack_result(Err(anyhow::anyhow!(
        "CHILD_CALLBACK_ACK_INTAKEN_CALLBACK_NOT_FOUND:test"
    )))
    .expect_err("failed Router ACK must not enter parent convergence");

    assert_eq!(
        error.to_string(),
        "CHILD_CALLBACK_ACK_INTAKEN_CALLBACK_NOT_FOUND:test"
    );
    assert_eq!(child_callback_parent_projection(&parent.id), before);
}

#[tokio::test]
async fn session_tests_already_acknowledged_replay_repairs_and_is_idempotent() {
    let _service = SessionDbTestService::start();
    let child_session_id = "child-callback-replay";
    let (parent, response) = create_child_callback_parent(
        child_session_id,
        AcknowledgeChildCallbackOutcome::AlreadyAcknowledged,
        |response| TaskPlan {
            plan_summary: "Replay callback parent".to_string(),
            detailed_tasks: vec![child_callback_task(
                "callback-task",
                PlanStatus::Doing,
                response,
            )],
        },
    )
    .await;

    super::converge_child_callback_ack_result(Ok(response.clone()))
        .expect("already-acknowledged replay should repair missing parent convergence");
    let after_repair = child_callback_parent_projection(&parent.id);
    super::converge_child_callback_ack_result(Ok(response))
        .expect("an exact acknowledged replay should be idempotent");

    assert_eq!(
        after_repair.task_plan.detailed_tasks[0].status,
        PlanStatus::Done
    );
    assert_eq!(after_repair.state, SessionState::Completed);
    assert_eq!(child_callback_parent_projection(&parent.id), after_repair);
}

#[tokio::test]
async fn session_tests_late_child_callback_ack_preserves_newer_task_status() {
    let _service = SessionDbTestService::start();

    for status in [
        PlanStatus::Todo,
        PlanStatus::WaitingUser,
        PlanStatus::Question,
    ] {
        let (parent, response) = create_child_callback_parent(
            &format!("late-ack-{status:?}"),
            AcknowledgeChildCallbackOutcome::Acknowledged,
            |response| TaskPlan {
                plan_summary: "Late callback parent".to_string(),
                detailed_tasks: vec![child_callback_task("callback-task", status, response)],
            },
        )
        .await;
        let before_cache = child_callback_parent_projection(&parent.id);
        let before_durable = SessionDbClient::discover()
            .expect("session DB client")
            .get_session(parent.id.clone())
            .expect("durable parent read")
            .expect("durable parent exists")
            .lifecycle_projection;

        let error = super::converge_child_callback_ack_result(Ok(response))
            .expect_err("late ACK must not override newer task state");

        assert!(
            error
                .to_string()
                .contains("ACKNOWLEDGED_CHILD_CALLBACK_TASK_STATUS_CONFLICT"),
            "unexpected error for {status:?}: {error}"
        );
        assert_eq!(child_callback_parent_projection(&parent.id), before_cache);
        assert_eq!(
            SessionDbClient::discover()
                .expect("session DB client")
                .get_session(parent.id)
                .expect("durable parent re-read")
                .expect("durable parent still exists")
                .lifecycle_projection,
            before_durable
        );
    }
}

#[tokio::test]
async fn session_tests_projection_cache_preserves_intervening_runtime_on_receipt_replay() {
    let _service = SessionDbTestService::start();
    let child_session_id = "child-callback-cache-replay";
    let (parent, response) = create_child_callback_parent(
        child_session_id,
        AcknowledgeChildCallbackOutcome::AlreadyAcknowledged,
        |response| TaskPlan {
            plan_summary: "Replay callback parent with sibling".to_string(),
            detailed_tasks: vec![
                child_callback_task("callback-task", PlanStatus::Doing, response),
                TaskStep {
                    task_id: "sibling-task".to_string(),
                    status: PlanStatus::Todo,
                    task_summary: "Continue parent work".to_string(),
                    ..TaskStep::default()
                },
            ],
        },
    )
    .await;

    super::converge_child_callback_ack_result(Ok(response.clone()))
        .expect("first ACK should converge its exact task");
    session_store()
        .execute_canonical_session_command(
            &parent.id,
            SessionCommand::RuntimeStarted {
                runtime_id: "intervening-parent-runtime".to_string(),
            },
        )
        .expect("intervening runtime should become canonical");
    let canonical_after_runtime = SessionDbClient::discover()
        .expect("session DB client")
        .get_session(parent.id.clone())
        .expect("durable parent read")
        .expect("durable parent exists")
        .lifecycle_projection;

    super::converge_child_callback_ack_result(Ok(response))
        .expect("exact receipt replay should retain current canonical projection");

    let cached = child_callback_parent_projection(&parent.id);
    let canonical_after_replay = SessionDbClient::discover()
        .expect("session DB client")
        .get_session(parent.id)
        .expect("durable parent re-read")
        .expect("durable parent still exists")
        .lifecycle_projection;
    assert_eq!(canonical_after_replay, canonical_after_runtime);
    assert_eq!(cached, canonical_after_replay);
    assert_eq!(
        cached.active_runtime_id.as_deref(),
        Some("intervening-parent-runtime")
    );
    assert_eq!(cached.task_plan.detailed_tasks[0].status, PlanStatus::Done);
    assert_eq!(cached.task_plan.detailed_tasks[1].status, PlanStatus::Todo);
}

#[tokio::test]
async fn session_list_does_not_mutate_running_lifecycle_when_router_is_unavailable() {
    let _service = SessionDbTestService::start();
    let directory = std::env::temp_dir()
        .join(format!(
            "tura-session-list-observer-{}",
            uuid::Uuid::new_v4()
        ))
        .to_string_lossy()
        .to_string();
    let session = create_canonical_test_session(directory.clone()).await;
    let runtime_id = "runtime-observer-test".to_string();

    session_store()
        .execute_canonical_session_command(
            &session.id,
            SessionCommand::RuntimeStarted {
                runtime_id: runtime_id.clone(),
            },
        )
        .expect("runtime should start");

    for _ in 0..2 {
        let listed = super::list_sessions_value(
            SessionListParams {
                directory: Some(directory.clone()),
                include_children: true,
                ..SessionListParams::default()
            },
            None,
        )
        .await;
        assert_eq!(
            listed
                .iter()
                .find(|item| item.id == session.id)
                .map(|item| item.status.clone()),
            Some(SessionStatus::Busy)
        );
        assert_eq!(
            session_store()
                .session_lifecycle_projection(&session.id)
                .expect("running lifecycle projection")
                .state,
            SessionState::Running
        );
    }

    session_store()
        .execute_canonical_session_command(
            &session.id,
            SessionCommand::RuntimeCompleted { runtime_id },
        )
        .expect("runtime completion should terminalize the running session");
    assert_eq!(
        session_store()
            .session_lifecycle_projection(&session.id)
            .expect("completed lifecycle projection")
            .state,
        SessionState::Completed
    );

    let _ = fs::remove_dir_all(directory);
}

#[test]
fn prompt_payload_keeps_frontend_message_and_part_ids() {
    let payload = serde_json::json!({
        "messageID": "msg_frontend_1",
        "parts": [
            { "id": "part_text_1", "type": "text", "text": "Read README.md" },
            { "id": "part_file_1", "type": "file", "url": "file:///README.md" }
        ]
    });

    assert_eq!(
        prompt_message_id(&payload).as_deref(),
        Some("msg_frontend_1")
    );
    assert_eq!(
        first_prompt_part_id(&payload).as_deref(),
        Some("part_text_1")
    );
    assert_eq!(prompt_text(&payload).as_deref(), Some("Read README.md"));
}

#[test]
fn prompt_payload_extracts_model_runtime_options() {
    let payload = serde_json::json!({
        "variant": "high",
        "model_acceleration_enabled": true,
    });

    assert_eq!(prompt_model_variant(&payload).as_deref(), Some("high"));
    assert_eq!(prompt_model_acceleration(&payload), Some(true));
}

#[test]
fn prompt_payload_extracts_documented_command_run_shell_surfaces() {
    let zsh = serde_json::json!({ "command_run_shell": "zsh" });
    let shel = serde_json::json!({ "command_run_shell": "shel" });
    let shell_command = serde_json::json!({ "commandRunShell": "shell_command" });
    let typo = serde_json::json!({ "command_run_shell": "zash" });

    assert_eq!(prompt_command_run_shell(&zsh).as_deref(), Some("zsh"));
    assert_eq!(
        prompt_command_run_shell(&shel).as_deref(),
        Some("shell_command")
    );
    assert_eq!(
        prompt_command_run_shell(&shell_command).as_deref(),
        Some("shell_command")
    );
    assert_eq!(prompt_command_run_shell(&typo), None);
}

#[test]
fn prompt_payload_treats_default_model_variant_as_unset() {
    let payload = serde_json::json!({
        "variant": " default ",
    });

    assert_eq!(prompt_model_variant(&payload), None);
}

#[test]
fn session_config_defaults_priority_routing_off() {
    assert_eq!(
        TuraSessionConfig::default().model_acceleration_enabled,
        Some(false)
    );
}

#[test]
fn session_config_model_override_keeps_tier_names_out_of_specific_model_display() {
    let config = TuraSessionConfig {
        model: Some("thinking".to_string()),
        active_provider: None,
        active_model: None,
        ..TuraSessionConfig::default()
    };

    assert_eq!(config_model_override(&config), None);
}

fn test_session(id: &str, directory: &str, parent_id: Option<&str>, updated_at: i64) -> Session {
    Session {
        id: id.to_string(),
        name: Some(id.to_string()),
        parent_id: parent_id.map(ToString::to_string),
        created_at: updated_at - 1,
        updated_at,
        last_user_message_at: None,
        task_start_at: Some(updated_at - 1),
        directory: Some(directory.to_string()),
        model: None,
        agent: None,
        session_type: Some("coding".to_string()),
        auto_session_name: true,
        kill_processes_on_start: false,
        validator_enabled: false,
        force_planning: false,
        model_variant: None,
        model_acceleration_enabled: false,
        disable_permission_restrictions: false,
        status: SessionStatus::Idle,
        message_count: 0,
        task_management: serde_json::json!({}),
        context_tokens: SessionContextTokens::default(),
        usage: Default::default(),
        plan_summary: None,
        session_display_name: None,
    }
}

#[test]
fn session_list_filters_requested_directory_and_roots() {
    let sessions = vec![
        test_session("root-a", r"C:\repo", None, 10),
        test_session("child-a", r"C:\repo", Some("root-a"), 11),
        test_session("root-b", r"C:\other", None, 12),
    ];
    let params = SessionListParams {
        roots: Some(true),
        ..SessionListParams::default()
    };

    let filtered = filter_list_sessions(sessions, &params, Some("C:/repo/"));

    assert_eq!(
        filtered
            .iter()
            .map(|session| session.id.as_str())
            .collect::<Vec<_>>(),
        vec!["root-a"]
    );
}

#[test]
fn session_list_hides_children_by_default() {
    let sessions = vec![
        test_session("root-a", r"C:\repo", None, 10),
        test_session("child-a", r"C:\repo", Some("root-a"), 11),
    ];

    let filtered = filter_list_sessions(sessions, &SessionListParams::default(), Some("C:/repo"));

    assert_eq!(
        filtered
            .iter()
            .map(|session| session.id.as_str())
            .collect::<Vec<_>>(),
        vec!["root-a"]
    );
}

#[test]
fn session_list_can_include_children_when_requested() {
    let sessions = vec![
        test_session("root-a", r"C:\repo", None, 10),
        test_session("child-a", r"C:\repo", Some("root-a"), 11),
    ];
    let params = SessionListParams {
        include_children: true,
        ..SessionListParams::default()
    };

    let filtered = filter_list_sessions(sessions, &params, Some("C:/repo"));

    assert_eq!(
        filtered
            .iter()
            .map(|session| session.id.as_str())
            .collect::<Vec<_>>(),
        vec!["root-a", "child-a"]
    );
}

#[tokio::test]
async fn session_list_orders_by_latest_user_message_not_runtime_update() {
    let _service = SessionDbTestService::start();
    let directory = std::env::temp_dir()
        .join(format!(
            "tura-session-user-message-order-{}",
            uuid::Uuid::new_v4()
        ))
        .to_string_lossy()
        .to_string();

    let assistant_updated_later = create_canonical_test_session(directory.clone()).await;
    let user_sent_later = create_canonical_test_session(directory.clone()).await;

    super::update_session_task_management_value(
        assistant_updated_later.id.clone(),
        super::UpdateSessionTaskManagementRequest {
            task_management: serde_json::json!({
                "task_summary": "Assistant updated later",
                "start_at": "2026-06-25T12:00:00Z"
            }),
        },
    )
    .expect("first task management patch should succeed");
    super::update_session_task_management_value(
        user_sent_later.id.clone(),
        super::UpdateSessionTaskManagementRequest {
            task_management: serde_json::json!({
                "task_summary": "User sent later",
                "start_at": "2026-06-25T10:00:00Z"
            }),
        },
    )
    .expect("second task management patch should succeed");

    session_store().add_message(
        &assistant_updated_later.id,
        crate::session::store::MessageRole::User,
        "older user prompt".to_string(),
    );
    std::thread::sleep(std::time::Duration::from_millis(2));
    let newer_user_message = session_store()
        .add_message(
            &user_sent_later.id,
            crate::session::store::MessageRole::User,
            "newer user prompt".to_string(),
        )
        .expect("newer user message should be stored");
    std::thread::sleep(std::time::Duration::from_millis(2));
    session_store().add_message(
        &assistant_updated_later.id,
        crate::session::store::MessageRole::Assistant,
        "later assistant reply".to_string(),
    );

    let Json(listed) = super::list_sessions(
        HeaderMap::new(),
        Query(SessionListParams {
            directory: Some(directory.clone()),
            include_children: true,
            ..SessionListParams::default()
        }),
    )
    .await;

    assert_eq!(
        listed.first().map(|session| session.id.as_str()),
        Some(user_sent_later.id.as_str())
    );
    assert_eq!(
        listed
            .first()
            .and_then(|session| session.last_user_message_at),
        Some(newer_user_message.updated_at)
    );

    let _ = fs::remove_dir_all(directory);
}

#[test]
fn workspace_key_normalizes_slashes_and_trailing_separator() {
    assert_eq!(workspace_key(r"C:\repo\"), "C:/repo");
    assert_eq!(workspace_key("C:/"), "C:/");
    assert_eq!(workspace_key("///"), "/");
}

#[tokio::test]
async fn session_status_includes_task_management_display_fields() {
    let _service = SessionDbTestService::start();
    let directory = std::env::temp_dir()
        .join(format!("tura-session-status-{}", uuid::Uuid::new_v4()))
        .to_string_lossy()
        .to_string();
    let session = create_canonical_test_session(directory).await;
    session_store()
        .update_session(
            &session.id,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            Some(serde_json::json!({
                "plan_summary": "Status Contract",
                "task_summary": "Status task",
                "status": "question"
            })),
        )
        .expect("session task management should update");

    let Json(statuses) = super::session_status().await;
    let status = statuses
        .get(&session.id)
        .expect("status map should include new session");

    assert_eq!(status["task_management"]["status"], "question");
    assert_eq!(status["plan_summary"], "Status Contract");
    assert_eq!(status["session_display_name"], "Status task");
}

#[tokio::test]
async fn create_session_accepts_task_management_and_serializes_session_fields() {
    let _service = SessionDbTestService::start();
    let directory = std::env::temp_dir()
        .join(format!("tura-create-session-plan-{}", uuid::Uuid::new_v4()))
        .to_string_lossy()
        .to_string();
    let session = super::create_session_value(
        super::SessionDirectoryParams { directory: None },
        super::CreateSessionRequest {
            directory: Some(directory.clone()),
            model: None,
            agent: None,
            session_type: Some("chat".to_string()),
            kill_processes_on_start: Some(false),
            validator_enabled: Some(false),
            force_planning: Some(false),
            model_variant: None,
            model_acceleration_enabled: Some(false),
            disable_permission_restrictions: Some(false),
            auto_session_name: None,
            task_management: Some(serde_json::json!({
                "plan_summary": "Create Route Plan",
                "task_summary": "Create route task"
            })),
        },
        None,
    )
    .await
    .expect("canonical session creation should succeed");

    assert_eq!(session.directory.as_deref(), Some(directory.as_str()));
    assert_eq!(session.plan_summary.as_deref(), Some("Create Route Plan"));
    assert_eq!(
        session.session_display_name.as_deref(),
        Some("Create route task")
    );
    assert_eq!(session.task_management["task_summary"], "Create route task");

    let value = serde_json::to_value(&session).expect("session should serialize");
    assert!(value["name"].as_str().is_some_and(|name| !name.is_empty()));
    assert!(value["task_management"].get("status").is_none());
    assert_eq!(value["task_management"]["start_condition"], "user_action");
    assert_eq!(value["plan_summary"], "Create Route Plan");
    assert_eq!(value["session_display_name"], "Create route task");
    assert_eq!(value["auto_session_name"], true);
    assert_eq!(value["context_tokens"]["input"], 0);
    assert!(value["context_tokens"]["limit"].as_u64().is_some());
    assert_eq!(value["usage"]["context_tokens"]["input"], 0);
    assert!(value["usage"]["context_tokens"]["limit"].as_u64().is_some());
    assert_eq!(
        value["last_user_message_at"].as_i64(),
        session.last_user_message_at
    );
    let object = value.as_object().expect("session JSON should be an object");
    assert_eq!(object.len(), 25);

    let Json(listed) = super::list_sessions(
        HeaderMap::new(),
        Query(SessionListParams {
            directory: Some(directory.clone()),
            include_children: true,
            ..SessionListParams::default()
        }),
    )
    .await;
    assert!(listed.iter().any(|item| item.id == session.id
        && item.task_management.get("status").is_none()
        && item.task_management["start_condition"] == "user_action"));

    let _ = fs::remove_dir_all(directory);
}

#[tokio::test]
async fn task_management_route_patches_session_and_returns_session_fields() {
    let _service = SessionDbTestService::start();
    let directory = std::env::temp_dir()
        .join(format!(
            "tura-task-management-route-{}",
            uuid::Uuid::new_v4()
        ))
        .to_string_lossy()
        .to_string();
    let session = create_canonical_test_session(directory.clone()).await;

    let updated = super::update_session_task_management_value(
        session.id.clone(),
        super::UpdateSessionTaskManagementRequest {
            task_management: serde_json::json!({
                "plan_summary": "Dedicated Patch Route",
                "task_summary": "Patch task",
                "status": "question",
                "start_at": "2026-05-25T08:30:00Z"
            }),
        },
    )
    .expect("task management patch should succeed");

    assert_eq!(
        updated.plan_summary.as_deref(),
        Some("Dedicated Patch Route")
    );
    assert_eq!(updated.session_display_name.as_deref(), Some("Patch task"));
    assert_eq!(updated.task_management["status"], "question");
    assert_eq!(updated.task_management["start_condition"], "scheduled_task");

    let value = serde_json::to_value(&updated).expect("session should serialize");
    assert_eq!(value["task_management"]["status"], "question");
    assert_eq!(
        value["task_management"]["start_condition"],
        "scheduled_task"
    );
    assert_eq!(value["plan_summary"], "Dedicated Patch Route");
    assert_eq!(value["session_display_name"], "Patch task");
    assert_eq!(value["auto_session_name"], true);
    assert_eq!(value["context_tokens"]["input"], 0);
    assert!(value["context_tokens"]["limit"].as_u64().is_some());
    assert_eq!(value["usage"]["context_tokens"]["input"], 0);
    assert!(value["usage"]["context_tokens"]["limit"].as_u64().is_some());
    assert_eq!(
        value["last_user_message_at"].as_i64(),
        updated.last_user_message_at
    );
    let object = value.as_object().expect("session JSON should be an object");
    assert_eq!(object.len(), 25);

    let Json(fetched) = super::get_session(Path(session.id)).await;
    assert_eq!(fetched.task_management["status"], "question");
    assert_eq!(fetched.task_management["start_condition"], "scheduled_task");

    let _ = fs::remove_dir_all(directory);
}

#[test]
fn frontend_safe_value_strips_tool_internal_fields_recursively() {
    let value = frontend_safe_value(Some(serde_json::json!({
        "input": {
            "reply_message": "done",
            "new_learning": "private",
            "nested": [{ "runtime_id": "runtime-1", "ok": true }]
        },
        "runtime_id": "runtime-2"
    })))
    .expect("value should remain present");

    let serialized = serde_json::to_string(&value).expect("value should serialize");
    assert!(!serialized.contains("new_learning"));
    assert!(!serialized.contains("runtime_id"));
    assert!(serialized.contains("reply_message"));
}

#[test]
fn runtime_tool_part_keeps_exact_input_output_payloads() {
    let message = crate::session::store::Message {
        id: "message-1".to_string(),
        session_id: "session-1".to_string(),
        role: crate::session::store::MessageRole::Assistant,
        parent_id: None,
        parts: vec![crate::session::store::MessagePart {
            id: "part-1".to_string(),
            part_type: "tool".to_string(),
            content: None,
            text: None,
            metadata: None,
            call_id: Some("runtime-1".to_string()),
            tool: Some("runtime".to_string()),
            state: Some(serde_json::json!({
                "status": "completed",
                "input": {
                    "messages": [{ "role": "user", "content": "ACTUAL_CONTEXT_MARKER" }],
                    "runtime_id": "request-runtime-id"
                },
                "output": {
                    "text": "FULL_PROVIDER_OUTPUT_MARKER",
                    "runtime_id": "response-runtime-id"
                }
            })),
        }],
        created_at: 1,
        updated_at: 2,
    };

    let value =
        serde_json::to_value(api_message_from_store(message)).expect("message should serialize");

    assert_eq!(
        value["parts"][0]["state"]["input"]["messages"][0]["content"],
        "ACTUAL_CONTEXT_MARKER"
    );
    assert_eq!(
        value["parts"][0]["state"]["input"]["runtime_id"],
        "request-runtime-id"
    );
    assert_eq!(
        value["parts"][0]["state"]["output"]["text"],
        "FULL_PROVIDER_OUTPUT_MARKER"
    );
    assert_eq!(
        value["parts"][0]["state"]["output"]["runtime_id"],
        "response-runtime-id"
    );
}

#[test]
fn frontend_safe_reply_message_extracts_reply_from_raw_tool_payload() {
    let text = serde_json::json!({
        "error": null,
        "input": {
            "reply_message": "final answer",
            "new_learning": "",
            "runtime_id": "runtime-1"
        }
    })
    .to_string();

    assert_eq!(frontend_safe_reply_message(&text), "final answer");
}

#[test]
fn frontend_safe_reply_message_hides_raw_tool_argument_payload() {
    let text = serde_json::json!({
            "requests": [{
                "path": "services/sd-text-to-image/main.py",
                "start_line": 1,
                "end_line": 250
            }],
            "step_summary": "Read the Stable Diffusion image service main.py to find the port it runs on."
        })
        .to_string();

    assert_eq!(frontend_safe_reply_message(&text), "");
}

#[test]
fn apply_single_change_reports_target_directory_context() {
    let temp = tempfile::tempdir().expect("tempdir");
    let blocking_parent = temp.path().join("blocked");
    std::fs::write(&blocking_parent, "file blocks child directory").expect("write blocking file");
    let target = blocking_parent.join("child.txt");
    let record = SessionChangeRecord {
        path: target.to_string_lossy().to_string(),
        before_exists: true,
        before_content: Some("before".to_string()),
        after_exists: true,
        after_content: None,
        reverted: false,
    };

    let error = apply_single_change(&record, true)
        .expect_err("blocked parent path should fail directory creation");

    let message = &error;
    assert!(
        message.contains("failed to create change target directory"),
        "error should describe the failed operation: {message}"
    );
    assert!(
        message.contains(&blocking_parent.to_string_lossy().to_string()),
        "error should include the target directory path: {message}"
    );
}
