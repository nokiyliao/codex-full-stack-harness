use std::sync::atomic::Ordering;

use lifecycle::RuntimeState;
use lifecycle::SessionInput;
use lifecycle::SessionState;
use runtime::mano;
use serde_json::Value;
use session_log_contract::{
    ActivateRuntimeLeaseRequest, GetSessionRequest, RegisterRuntimeRequest, ReplayRuntimeRequest,
    RuntimeLeaseOutcome, RuntimeRegistrationOutcome, SessionLogCommand, SessionLogResponse,
};

#[path = "../support/session_db_support.rs"]
mod session_db_support;

#[path = "../support/typed_session.rs"]
mod typed_session;

#[path = "helpers/coding_agent_mock.rs"]
mod helpers;
use helpers::*;

struct TerminalEvidenceRun {
    calls: usize,
    records: Vec<Value>,
    outcomes: Vec<Value>,
    usage: Vec<Value>,
    summary_usage: Value,
}

// The caller holds the existing ENV_LOCK via SessionDbTestService for the
// entire paired run, including actual router execution and persisted replay.
fn run_terminal_evidence_fixture(
    session_id: &str,
    scenario: TerminalEvidenceScenario,
    evidence_only: bool,
    bounded: bool,
) -> TerminalEvidenceRun {
    use session_log_contract::ReadExecutionEvidenceRequest;
    let workspace = create_rust_workspace();
    let provider = MockProvider::start_terminal_evidence(scenario);
    let llm_config = write_llm_config(&workspace, provider.addr);
    let router_addr = mock_command_run_router_addr();
    let home = std::env::var("TURA_HOME").expect("existing fixture session home");
    let _env = EnvGuard::set(&[
        ("TURA_DB_ROOT", home.as_str()),
        ("TURA_PROVIDER_CONFIG", llm_config.to_string_lossy().as_ref()),
        ("OPENAI_API_KEY", "test-key"),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_RUNTIME_AUTO_GIT_COMMIT", "0"),
        ("TURA_MANAS_MAX_TURNS", "6"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_NOKIY_EVIDENCE_ONLY_TERMINAL", if evidence_only { "1" } else { "0" }),
        ("TURA_NOKIY_BOUNDED_ONE_TURN", if bounded { "1" } else { "0" }),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        ("TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS", MOCK_MULTI_COMMAND_STREAM_TIMEOUT_MS),
        ("TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS", MOCK_MULTI_COMMAND_STREAM_TIMEOUT_MS),
    ]);
    typed_session::create_via_service(typed_session::root_create_request(session_id,
        workspace.to_string_lossy().as_ref(), "Terminal evidence fixture", 1))
        .expect("create persisted fixture session before leased execution");
    if matches!(scenario, TerminalEvidenceScenario::ForeignMarker) {
        use session_log_contract::{PersistSessionDeltaRequest, SessionContextRecord, SessionDeltaEntry};
        let previous = match session_log_contract::client::call_service(
            &SessionLogCommand::GetSession(GetSessionRequest { session_id:session_id.to_string() }),
        ).expect("canonical seeded session") {
            SessionLogResponse::Session { session:Some(snapshot) } => snapshot.into_management().expect("management"),
            other => panic!("unexpected seeded session: {other:?}"),
        };
        let delta = PersistSessionDeltaRequest {
            session_id:session_id.to_string(), management_sequence:0, retained_from_sequence:0,
            management_delta:lifecycle::SessionManagement::persistence_delta(Some(&previous), &previous),
            entries:vec![SessionDeltaEntry { context:SessionContextRecord { sequence:0,
                raw_record:serde_json::json!({"type":"nokiy.terminal_evidence",
                    "schema_version":"nokiy_terminal_evidence_v1", "session_id":"foreign-session",
                    "runtime_id":"foreign-runtime", "terminal_status":"done", "delivery_mode":"evidence_only",
                    "parent_acceptance_required":true, "final_summary_turn_executed":false}).to_string(),
            }, projection:None }],
        };
        match session_log_contract::client::call_service(
            &SessionLogCommand::PersistSessionDelta(Box::new(delta)),
        ).expect("persist foreign marker fixture") {
            SessionLogResponse::SessionDeltaPersisted { next_sequence, .. } => assert_eq!(next_sequence, 1),
            other => panic!("unexpected fixture persistence: {other:?}"),
        }
    }
    let initial_runtime_id = format!("runtime-{session_id}");
    let initial_lease_id = format!("lease-{session_id}");
    assert!(matches!(
        session_log_contract::client::call_service(&SessionLogCommand::RegisterRuntime(
            RegisterRuntimeRequest {
                runtime_id: initial_runtime_id.clone(), session_id: session_id.to_string(),
                fallback_from_id: None, lifecycle: None,
            }
        )).expect("initial fixture runtime registration"),
        SessionLogResponse::RuntimeRegistered {
            result: RuntimeRegistrationOutcome::Registered { .. }
        }
    ));
    assert!(matches!(
        session_log_contract::client::call_service(&SessionLogCommand::ActivateRuntimeLease(
            ActivateRuntimeLeaseRequest {
                runtime_id: initial_runtime_id.clone(), lease_id: initial_lease_id.clone(),
            }
        )).expect("initial fixture runtime lease activation"),
        SessionLogResponse::RuntimeLeaseActivated {
            result: RuntimeLeaseOutcome::Activated
        }
    ));
    let result = mano::process_from_gateway_session_with_lease_in_directory(
        session_id.to_string(),
        initial_runtime_id,
        initial_lease_id,
        SessionInput {
            user_input: if matches!(scenario, TerminalEvidenceScenario::Ordered(_)) {
                format!("Execute the predetermined offline fixture patch and exact source predicate check. Only after success, explicitly mark the worker done; failed or uncertain results require model review. All artifacts and checks are planned and no successful-result interpretation is needed. Deliver {} with parent acceptance still required.",
                    if evidence_only { "evidence_only" } else { "assistant_reply" })
            } else {
                "Execute the offline fixture patch and verification, then report the fixture terminal task status.".to_string()
            },
            file_input: vec![], agent: Some("direct".to_string()),
            runtime_context: None, planning_mode_override: None,
        },
        workspace.clone(),
    ).expect("complete coding-agent fixture loop");
    let blocked = evidence_only && bounded && matches!(scenario,
        TerminalEvidenceScenario::FailedCommand | TerminalEvidenceScenario::UnknownOutcome
            | TerminalEvidenceScenario::UnresolvedEffect);
    // The provider/tool turn completed even when task acceptance is blocked.
    let expected_state = SessionState::Completed;
    assert!(result.final_error.is_none(), "{:?}", result.final_error);
    if blocked {
        assert!(result.session.session_log.iter().any(|entry| {
            let record = entry.value();
            record["type"] == "nokiy.terminal_evidence" && record["session_id"] == session_id
                && record["terminal_status"] == "blocked" && record["parent_acceptance_required"] == true
        }), "local completion must retain its unaccepted blocked outcome");
    }
    assert_eq!(result.session.state, expected_state);
    assert!(std::fs::read_to_string(workspace.join("src/lib.rs"))
        .expect("actual fixture patch").contains("processed verified {input}"));
    let calls = provider.requests.lock().expect("mock provider calls").len();

    let snapshot = match session_log_contract::client::call_service(
        &SessionLogCommand::GetSession(GetSessionRequest { session_id:session_id.to_string() }),
    ).expect("durable terminal snapshot") {
        SessionLogResponse::Session { session:Some(snapshot) } => snapshot,
        other => panic!("unexpected terminal snapshot: {other:?}"),
    };
    assert_eq!(snapshot.lifecycle_projection.state, expected_state);
    assert_eq!(snapshot.lifecycle_projection.state, result.session.state, "durable and local runtime states must agree");
    assert_eq!(snapshot.lifecycle_projection.active_runtime_id, None, "runtime cleanup must finish");
    assert_eq!(snapshot.lifecycle_projection.runtime_ids.len(), calls);
    let runtime_ids = snapshot.lifecycle_projection.runtime_ids.clone();
    let checkpoint = snapshot.into_management().expect("canonical management");
    assert_eq!(checkpoint.state, result.session.state, "the checkpoint describes runtime completion, not business acceptance");

    let request = ReadExecutionEvidenceRequest {
        session_id:session_id.to_string(), snapshot:None, from_sequence:0, max_records:2,
        max_bytes:session_log_contract::EXECUTION_EVIDENCE_PAGE_BYTES, include_summary:true,
    };
    let head = match session_log_contract::client::call_service(
        &SessionLogCommand::ReadExecutionEvidence(request.clone()),
    ).expect("complete execution evidence head") {
        SessionLogResponse::ExecutionEvidence { evidence } => {
            evidence.validate(&request).expect("bound durable evidence"); Some(evidence)
        }
        SessionLogResponse::Error { error }
            if matches!(scenario, TerminalEvidenceScenario::ForeignMarker) => {
            assert_eq!(error, "execution evidence record 0 has wrong session identity");
            None
        }
        other => panic!("unexpected execution evidence: {other:?}"),
    };
    let summary_usage = head.as_ref().map(|head|
        head.summary.as_ref().expect("retained usage summary").usage.clone()
    ).unwrap_or_else(|| serde_json::json!({
        "evidence_read_error":"execution evidence record 0 has wrong session identity"
    }));
    let mut records = Vec::new();
    if let Some(head) = head.as_ref() {
    session_log_contract::visit_execution_evidence(&head.snapshot, |request| {
        match session_log_contract::client::call_service(&SessionLogCommand::ReadExecutionEvidence(request))
            .map_err(|error| error.to_string())? {
            SessionLogResponse::ExecutionEvidence { evidence } => Ok(evidence),
            other => Err(format!("unexpected evidence page: {other:?}")),
        }
    }, |record| {
        assert_eq!(record.sequence, records.len() as u64);
        records.push(serde_json::from_str::<Value>(&record.raw_record).expect("raw persisted record"));
        Ok(())
    }).expect("full persisted loop replay, not a predicate-only test");
    assert_eq!(records.len() as u64, head.snapshot.next_sequence);
    } else {
        // Foreign evidence must be refused, not relabeled as a valid replay.
        // Live records only establish that this invalid-input run kept its
        // ordinary provider behavior; no durable delivery is claimed for it.
        records.extend(result.session.session_log.iter().map(|entry| entry.value().clone()));
    }
    let usage: Vec<_> = records.iter().filter(|record| record["type"] == "runtime_usage")
        .map(|record| serde_json::json!({
            "input_tokens":record["usage"]["input_tokens"],
            "output_tokens":record["usage"]["output_tokens"],
            "total_tokens":record["usage"]["total_tokens"],
        })).collect();
    assert_eq!(usage.len(), calls, "no actual provider usage may disappear");
    if head.is_some() {
        assert_eq!(summary_usage["coverage"]["known_runtime_count"], calls as u64);
        assert_eq!(summary_usage["coverage"]["status"], "complete");
    }

    let mut outcomes = Vec::new();
    for record in records.iter().filter(|record| record["type"] == "tool_result") {
        assert_eq!(record["tool_name"], "command_run");
        for (index, command) in record["output"]["results"].as_array().expect("command evidence").iter().enumerate() {
            // Compare semantic outcomes, not generated IDs, timestamps, or
            // elapsed times. The commands and provider-reported token usage are
            // deterministic; this fixture does not claim general latency gain.
            outcomes.push(serde_json::json!({
                "input_command_type":record["input"]["commands"][index]["command_type"],
                "input_command_line":record["input"]["commands"][index]["command_line"],
                "command_type":command["command_type"], "success":command["success"],
                "error":command["error"], "exit_code":command["output"]["exit_code"],
                "stdout":command["output"]["stdout"], "stderr":command["output"]["stderr"],
                "terminal_status":command.pointer("/output/task_status/status"),
            }));
        }
    }
    for marker in records.iter().filter(|record| record["type"] == "nokiy.terminal_evidence"
        && record["session_id"] == session_id) {
        let runtime_id = marker["runtime_id"].as_str().expect("actual runtime identity");
        assert!(runtime_ids.iter().any(|id| id == runtime_id));
        let runtime = match session_log_contract::client::call_service(
            &SessionLogCommand::ReplayRuntime(ReplayRuntimeRequest { runtime_id:runtime_id.to_string() }),
        ).expect("sealed runtime replay") {
            SessionLogResponse::RuntimeReplayed { runtime:Some(replay) } => replay.aggregate,
            other => panic!("unexpected runtime replay: {other:?}"),
        };
        assert_eq!(runtime.state, RuntimeState::Finished);
        assert_eq!(runtime.session_id, session_id);
        assert_eq!(marker, &serde_json::json!({
            "type":"nokiy.terminal_evidence", "schema_version":"nokiy_terminal_evidence_v1",
            "session_id":session_id, "runtime_id":runtime_id,
            "terminal_status":if blocked { "blocked" } else { "done" },
            "delivery_mode":"evidence_only", "parent_acceptance_required":true,
            "final_summary_turn_executed":false,
        }));
    }
    TerminalEvidenceRun { calls, records, outcomes, usage, summary_usage }
}

fn ordered_evidence_work_record(run: &TerminalEvidenceRun) -> &Value {
    run.records.iter().find(|record| record["type"] == "tool_result"
        && record["input"]["commands"].as_array().is_some_and(|commands|
            commands.iter().any(|command| command["command_type"] == "apply_patch")))
        .expect("persisted patch/check batch")
}

fn assert_ordered_handoff_requires_model_review(run: &TerminalEvidenceRun) {
    assert_eq!(run.calls, 3, "the rejected handoff must return results to the model");
    assert!(!run.records.iter().any(|record| record["type"] == "nokiy.terminal_evidence"));
    assert!(run.records.iter().any(|record| record["role"] == "assistant"
        && record["content"] == TERMINAL_EVIDENCE_FINAL_REPLY));
}

#[test]
fn ordered_evidence_handoff_complete_loop_saves_completion_only_provider_round() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let separate = run_terminal_evidence_fixture("ordered-separate-done",
        TerminalEvidenceScenario::Ordered(OrderedEvidenceScenario::SeparateDone), true, true);
    let ordered = run_terminal_evidence_fixture("ordered-same-response-done",
        TerminalEvidenceScenario::Ordered(OrderedEvidenceScenario::Done), true, true);
    assert_eq!(separate.calls, 3, "start, patch/check, completion-only task_status");
    assert_eq!(ordered.calls, 2, "start, then success-gated patch/check/done");
    assert_eq!(separate.calls - ordered.calls, 1);
    assert_eq!(separate.outcomes, ordered.outcomes, "no command or check is omitted");
    assert_eq!(ordered.outcomes.len(), 4);
    assert!(ordered.outcomes.iter().all(|outcome| outcome["success"] == true));
    assert_eq!(&separate.usage[..ordered.calls], ordered.usage.as_slice());
    assert_eq!(separate.summary_usage["total_tokens"],
        ordered.summary_usage["total_tokens"].as_u64().expect("tokens") + 2);
    let work = ordered_evidence_work_record(&ordered);
    let results = work["output"]["results"].as_array().expect("actual command results");
    assert_eq!(results.len(), 3);
    for (result, (kind, step)) in results.iter().zip([
        ("apply_patch", 1),
        (code_tools::commands::active_shell_command_name(), 2),
        ("task_status", 3),
    ]) {
        assert_eq!(result["command_type"], kind);
        assert_eq!(result["step"], step);
        assert_eq!(result["success"], true);
    }
    assert_eq!(results[2]["output"]["task_status"]["status"], "done");
    for run in [&separate, &ordered] {
        let markers: Vec<_> = run.records.iter()
            .filter(|record| record["type"] == "nokiy.terminal_evidence").collect();
        assert_eq!(markers.len(), 1);
        assert_eq!(markers[0]["terminal_status"], "done");
        assert_eq!(markers[0]["parent_acceptance_required"], true);
        assert_eq!(markers[0]["final_summary_turn_executed"], false);
        assert!(!run.records.iter().any(|record| record["role"] == "assistant"
            && record["content"] == TERMINAL_EVIDENCE_FINAL_REPLY));
    }
    let marker = ordered.records.iter()
        .find(|record| record["type"] == "nokiy.terminal_evidence").unwrap();
    assert_eq!(marker["runtime_id"], work["runtime_id"], "same-request durable handoff");
}

#[test]
fn ordered_evidence_handoff_preserves_default_assistant_reply() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let scenario = TerminalEvidenceScenario::Ordered(OrderedEvidenceScenario::Done);
    let baseline = run_terminal_evidence_fixture("ordered-default-reply", scenario, false, true);
    let evidence = run_terminal_evidence_fixture("ordered-explicit-evidence", scenario, true, true);
    let unbounded = run_terminal_evidence_fixture("ordered-not-bounded", scenario, true, false);
    assert_eq!(evidence.calls, 2);
    for run in [&baseline, &unbounded] {
        assert_eq!(run.calls, 3, "ordinary delivery retains the final assistant turn");
        assert_eq!(run.outcomes, evidence.outcomes);
        assert!(!run.records.iter().any(|record| record["type"] == "nokiy.terminal_evidence"));
        assert!(run.records.iter().any(|record| record["role"] == "assistant"
            && record["content"] == TERMINAL_EVIDENCE_FINAL_REPLY));
    }
    assert!(!evidence.records.iter().any(|record| record["role"] == "assistant"
        && record["content"] == TERMINAL_EVIDENCE_FINAL_REPLY));
}

#[test]
fn ordered_evidence_handoff_failed_or_timed_out_check_returns_to_model() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    for (index, scenario) in [OrderedEvidenceScenario::FailedCheck,
        OrderedEvidenceScenario::TimedOutCheck].into_iter().enumerate() {
        let run = run_terminal_evidence_fixture(&format!("ordered-failed-check-{index}"),
            TerminalEvidenceScenario::Ordered(scenario), true, true);
        assert_ordered_handoff_requires_model_review(&run);
        let work = ordered_evidence_work_record(&run);
        let results = work["output"]["results"].as_array().expect("actual command results");
        assert_eq!(results.len(), 3);
        assert_eq!(results[0]["success"], true, "the real patch remains applied");
        assert_eq!(results[1]["success"], false, "failed check evidence is retained");
        if matches!(scenario, OrderedEvidenceScenario::FailedCheck) {
            assert_eq!(results[1]["output"]["exit_code"], 7);
        } else {
            assert_eq!(work["input"]["commands"][1]["timeout_ms"], 100);
        }
        assert_eq!(results[2]["success"], false);
        assert!(results[2]["output"].is_null(), "rejected done cannot update the task FSM");
        assert!(results[2]["error"].as_str().is_some_and(|error|
            error.starts_with("TERMINAL_STATUS_PRIOR_RESULT")));
        assert!(!run.outcomes.iter().any(|outcome| outcome["terminal_status"] == "done"));
    }
}

#[test]
fn ordered_evidence_handoff_uncertain_predecessor_cannot_seal_done() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    for (index, scenario) in [OrderedEvidenceScenario::UnknownOutcome,
        OrderedEvidenceScenario::UnresolvedEffect].into_iter().enumerate() {
        let run = run_terminal_evidence_fixture(&format!("ordered-uncertain-check-{index}"),
            TerminalEvidenceScenario::Ordered(scenario), true, true);
        assert_ordered_handoff_requires_model_review(&run);
        let work = ordered_evidence_work_record(&run);
        let verify = &work["output"]["results"][1];
        // The existing router injects transport uncertainty AFTER execution.
        // Even a successful wrapper and explicit done cannot seal delivery.
        assert_eq!(verify["success"], true);
        assert_eq!(verify["output"]["exit_code"], 0);
        if matches!(scenario, OrderedEvidenceScenario::UnknownOutcome) {
            assert_eq!(verify["output"]["outcome"], "unknown");
        } else {
            assert_eq!(verify["output"]["terminal_receipt"]["reconcile_required"], true);
        }
    }
}

#[test]
fn ordered_evidence_handoff_wrong_order_returns_to_model() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    for (index, scenario) in [OrderedEvidenceScenario::SameStep,
        OrderedEvidenceScenario::DoneBeforeCheck, OrderedEvidenceScenario::ReversedSteps,
    ].into_iter().enumerate() {
        let run = run_terminal_evidence_fixture(&format!("ordered-wrong-order-{index}"),
            TerminalEvidenceScenario::Ordered(scenario), true, true);
        assert_ordered_handoff_requires_model_review(&run);
        let work = ordered_evidence_work_record(&run);
        let results = work["output"]["results"].as_array().expect("actual command results");
        assert_eq!(results.len(), 3);
        let done = results.iter().find(|result| result["command_type"] == "task_status").unwrap();
        assert_eq!(done["success"], false);
        assert!(done["output"].is_null());
        assert!(done["error"].as_str().is_some_and(|error|
            error.starts_with("TERMINAL_STATUS_BATCH_ORDER")));
        assert!(results.iter().filter(|result| result["command_type"] != "task_status")
            .all(|result| result["success"] == true), "nonterminal work is not auto-cancelled");
    }
}

#[test]
fn evidence_only_terminal_complete_loop_saves_exactly_one_provider_call() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let baseline = run_terminal_evidence_fixture("terminal-assistant-default", TerminalEvidenceScenario::Done, false, true);
    let evidence = run_terminal_evidence_fixture("terminal-evidence-opt-in", TerminalEvidenceScenario::Done, true, true);
    assert_eq!(baseline.calls, 4, "three real tool turns plus final assistant");
    assert_eq!(evidence.calls, 3, "only the redundant final provider turn is omitted");
    assert_eq!(baseline.calls - evidence.calls, 1);
    assert_eq!(baseline.outcomes, evidence.outcomes, "identical successful tool outcomes");
    for run in [&baseline, &evidence] {
        assert!(run.records.iter().any(|record| record["role"] == "assistant"
            && record["content"] == TERMINAL_EVIDENCE_PROGRESS_REPLY), "raw earlier assistant history is retained");
    }
    assert_eq!(evidence.outcomes.len(), 4, "task type, patch, verification, actual done");
    assert!(evidence.outcomes.iter().all(|outcome| outcome["success"] == true));
    assert_eq!(&baseline.usage[..evidence.calls], evidence.usage.as_slice());
    assert_eq!(baseline.summary_usage["total_tokens"], evidence.summary_usage["total_tokens"].as_u64().expect("tokens") + 2);
    assert!(!baseline.records.iter().any(|record| record["type"] == "nokiy.terminal_evidence"));
    assert_eq!(evidence.records.iter().filter(|record| record["type"] == "nokiy.terminal_evidence").count(), 1);
    assert!(baseline.records.iter().any(|record| record["role"] == "assistant" && record["content"] == TERMINAL_EVIDENCE_FINAL_REPLY));
    assert!(!evidence.records.iter().any(|record| record["role"] == "assistant" && record["content"] == TERMINAL_EVIDENCE_FINAL_REPLY));
}

#[test]
fn evidence_only_blocked_complete_loop_skips_only_recap_and_preserves_first_blocker() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    for (index, scenario) in [TerminalEvidenceScenario::FailedCommand,
        TerminalEvidenceScenario::UnknownOutcome, TerminalEvidenceScenario::UnresolvedEffect,
    ].into_iter().enumerate() {
        let baseline = run_terminal_evidence_fixture(&format!("terminal-blocked-default-{index}"), scenario, false, true);
        let evidence = run_terminal_evidence_fixture(&format!("terminal-blocked-opt-in-{index}"), scenario, true, true);
        assert_eq!(baseline.calls, 4, "default still gets its assistant reply");
        assert_eq!(evidence.calls, 3, "only the redundant provider recap is skipped");
        assert_eq!(baseline.outcomes, evidence.outcomes, "no failed command is erased or retried");
        assert_eq!(&baseline.usage[..evidence.calls], evidence.usage.as_slice());
        assert_eq!(baseline.summary_usage["total_tokens"], evidence.summary_usage["total_tokens"].as_u64().expect("tokens") + 2);
        assert!(!baseline.records.iter().any(|record| record["type"] == "nokiy.terminal_evidence"));
        assert_eq!(evidence.records.iter().filter(|record| record["type"] == "nokiy.terminal_evidence"
            && record["terminal_status"] == "blocked").count(), 1);
        assert!(!evidence.records.iter().any(|record| record["type"] == "nokiy.terminal_evidence"
            && record["terminal_status"] == "done"), "completed delivery must not upgrade the blocked task");
        assert!(baseline.records.iter().any(|record| record["role"] == "assistant" && record["content"] == TERMINAL_EVIDENCE_FINAL_REPLY));
        assert!(!evidence.records.iter().any(|record| record["role"] == "assistant" && record["content"] == TERMINAL_EVIDENCE_FINAL_REPLY));
        assert!(evidence.records.iter().any(|record| record["role"] == "assistant" && record["content"] == TERMINAL_EVIDENCE_PROGRESS_REPLY));
        match scenario {
            TerminalEvidenceScenario::FailedCommand => assert!(evidence.outcomes.iter().any(|outcome| outcome["success"] == false && outcome["exit_code"] == 7)),
            TerminalEvidenceScenario::UnknownOutcome => assert!(evidence.records.iter().any(|record| record["output"]["results"].as_array().is_some_and(|results| results.iter().any(|result| result["output"]["outcome"] == "unknown")))),
            TerminalEvidenceScenario::UnresolvedEffect => assert!(evidence.records.iter().any(|record| record["output"]["results"].as_array().is_some_and(|results| results.iter().any(|result| result["output"]["terminal_receipt"]["reconcile_required"] == true)))),
            _ => unreachable!(),
        }
    }
}

#[test]
fn evidence_only_terminal_requires_both_flags_and_preserves_negative_complete_loops() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let unbounded = run_terminal_evidence_fixture("terminal-evidence-not-bounded", TerminalEvidenceScenario::Done, true, false);
    assert_eq!(unbounded.calls, 4);
    assert!(!unbounded.records.iter().any(|record| record["type"] == "nokiy.terminal_evidence"));
    for (index, scenario) in [
        TerminalEvidenceScenario::Question, TerminalEvidenceScenario::MissingTerminalStatus,
        TerminalEvidenceScenario::UnsafeTerminalBatch,
        TerminalEvidenceScenario::PendingCompact, TerminalEvidenceScenario::AutoCompact,
        TerminalEvidenceScenario::ForeignMarker,
    ].into_iter().enumerate() {
        let baseline = run_terminal_evidence_fixture(&format!("terminal-negative-default-{index}"), scenario, false, true);
        let evidence = run_terminal_evidence_fixture(&format!("terminal-negative-opt-in-{index}"), scenario, true, true);
        assert_eq!(baseline.calls, evidence.calls, "negative {scenario:?} must retain existing provider behavior");
        assert_eq!(baseline.calls, 4);
        assert_eq!(baseline.outcomes, evidence.outcomes);
        assert_eq!(baseline.usage, evidence.usage);
        assert!(!evidence.records.iter().any(|record| record["type"] == "nokiy.terminal_evidence"
            && record["session_id"] != "foreign-session"));
        assert!(evidence.records.iter().any(|record| record["role"] == "assistant" && record["content"] == TERMINAL_EVIDENCE_FINAL_REPLY));
        match scenario {
            TerminalEvidenceScenario::Question => assert!(evidence.outcomes.iter().any(|outcome| outcome["terminal_status"] == "question")),
            TerminalEvidenceScenario::MissingTerminalStatus => assert!(!evidence.outcomes.iter().any(|outcome| outcome["terminal_status"] == "done")),
            TerminalEvidenceScenario::UnsafeTerminalBatch => assert!(evidence.records.iter().any(|record| record["output"]["results"].as_array().is_some_and(|results| results.iter().any(|result| result["output"]["outcome"] == "unknown")))),
            TerminalEvidenceScenario::PendingCompact | TerminalEvidenceScenario::AutoCompact => assert!(evidence.records.iter().any(|record| record["type"] == "context_compaction")),
            TerminalEvidenceScenario::ForeignMarker => {
                assert_eq!(evidence.summary_usage["evidence_read_error"],
                    "execution evidence record 0 has wrong session identity");
                assert_eq!(baseline.summary_usage, evidence.summary_usage);
                assert_eq!(evidence.records.iter().filter(|record|
                record["type"] == "nokiy.terminal_evidence" && record["session_id"] == "foreign-session"
                && record["runtime_id"] == "foreign-runtime").count(), 1, "foreign input is preserved, never promoted");
            }
            TerminalEvidenceScenario::Done | TerminalEvidenceScenario::FailedCommand
                | TerminalEvidenceScenario::UnknownOutcome | TerminalEvidenceScenario::UnresolvedEffect
                | TerminalEvidenceScenario::Ordered(_) => unreachable!(),
        }
    }
}

#[test]
fn coding_agent_can_call_command_run_tool_e2e() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let provider = MockProvider::start_command_run();
    let llm_config = write_llm_config(&workspace, provider.addr);
    let router_addr = mock_command_run_router_addr();
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("OPENAI_API_KEY", "test-key"),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_MANAS_MAX_TURNS", "5"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_in_directory(
        "e2e-run-command-tool".to_string(),
        SessionInput {
            user_input: "Run pwd with command_run, then patch src/lib.rs with command_run apply_patch, verify it with shell_command, and finish with normal assistant text."
                .to_string(),
            file_input: vec![],
            agent: Some("direct".to_string()),
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace.clone(),
    )
    .expect("coding agent should complete the command_run e2e flow");

    assert_eq!(result.agents.len(), 1);
    assert_eq!(result.agents[0].agent_name, "direct");
    assert_eq!(
        result.session.state,
        SessionState::Completed,
        "final_error={:?}; session log: {:#?}",
        result.final_error,
        result.session.session_log
    );

    let tool_results = tool_results(&result.session.session_log);
    assert_tool_success(&tool_results, "command_run");
    assert!(
        !tool_results
            .iter()
            .any(|result| result.get("tool_name").and_then(Value::as_str)
                == Some("send_message_to_user"))
    );
    assert!(
        result
            .session
            .session_log
            .iter()
            .map(|entry| entry.value())
            .any(|entry| entry.get("role").and_then(Value::as_str) == Some("assistant"))
    );

    let run_output = tool_results
        .iter()
        .find(|result| result.get("tool_name").and_then(Value::as_str) == Some("command_run"))
        .and_then(|result| result.get("output"))
        .cloned()
        .unwrap_or(Value::Null);
    let first_command_output = run_output
        .pointer("/results/0/output")
        .expect("first command_run result should expose structured output");
    assert_eq!(first_command_output["exit_code"].as_i64(), Some(0));
    assert!(
        first_command_output["stdout"]
            .as_str()
            .is_some_and(|stdout| !stdout.trim().is_empty())
    );
    assert_eq!(first_command_output["stderr"].as_str(), Some(""));
    assert!(run_output.pointer("/results/0/exit_code").is_none());
    assert!(run_output.pointer("/results/0/display_command").is_none());

    let patched_content = std::fs::read_to_string(workspace.join("src/lib.rs"))
        .expect("patched file should be readable");
    assert!(
        patched_content.contains("processed verified {input}"),
        "patched file should contain verified output; tool_results={tool_results:#?}"
    );

    let requests = provider
        .requests
        .lock()
        .expect("mock provider requests lock");
    let first_tools = requests
        .iter()
        .find(|request| request.get("tools").and_then(Value::as_array).is_some())
        .and_then(|request| request.get("tools"))
        .and_then(Value::as_array)
        .expect("at least one provider request should include tools");
    let first_tool_names = first_tools
        .iter()
        .filter_map(|tool| {
            tool.pointer("/function/name")
                .or_else(|| tool.get("name"))
                .and_then(Value::as_str)
        })
        .collect::<Vec<_>>();
    assert!(first_tool_names.contains(&"command_run"));
}

#[test]
fn codex_apply_patch_only_agent_executes_before_task_type_exists() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let provider = MockProvider::start_codex_apply_patch_only();
    let llm_config = write_codex_llm_config(&workspace, provider.addr);
    let endpoint = format!("http://{}", provider.addr);
    let router_addr = mock_command_run_router_addr();
    let agent_spec = serde_json::json!({
        "agent_name": "apply-patch-only",
        "config": {
            "agent_name": "apply-patch-only",
            "agent_directory": "agents/src/direct",
            "parent_agent_id": null,
            "report_to_user": true,
            "default_config": false,
            "reflection": false,
            "op_manual": false,
            "self_reflection": false,
            "provider": {
                "tura_llm_name": "fast",
                "default_model_tier": "fast",
                "current_model": "openai/mock-codex-stream",
                "stream": true,
                "temperature": 0.0,
                "max_tokens": 0,
                "tool_choice": "Auto",
                "time_out_ms": 30000
            },
            "agent_prompt": [],
            "agent_capabilities": [{
                "capability_name": "apply_patch",
                "capability_directory": "crates/tools/src"
            }],
            "validator": {
                "need_validator": false,
                "validator_name": null
            }
        }
    })
    .to_string();
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("TURA_ROUTER_AGENT_SPEC", agent_spec.as_str()),
        ("OPENAI_LOGIN", "oauth"),
        ("OPENAI_API_KEY", "test-key"),
        ("OPENAI_TOKEN_EXPIRES", MOCK_OPENAI_TOKEN_EXPIRES),
        ("OPENAI_CODEX_ENDPOINT", endpoint.as_str()),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-codex-stream"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_MANAS_MAX_TURNS", "2"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_in_directory(
        "e2e-codex-apply-patch-only".to_string(),
        SessionInput {
            user_input: "Create apply-only-created.txt using your only available command, then report completion."
                .to_string(),
            file_input: vec![],
            agent: Some("apply-patch-only".to_string()),
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace.clone(),
    )
    .expect("apply_patch-only Codex agent should complete");

    assert_eq!(result.agents[0].agent_name, "apply-patch-only");
    assert_eq!(
        result.session.state,
        SessionState::Completed,
        "final_error={:?}; session log: {:#?}",
        result.final_error,
        result.session.session_log
    );
    assert!(result.session.task_type.is_empty());
    assert_eq!(
        std::fs::read_to_string(workspace.join("apply-only-created.txt"))
            .expect("apply-only target should exist"),
        "created by apply-only agent\n"
    );

    let requests = provider
        .requests
        .lock()
        .expect("mock provider requests lock");
    assert_eq!(
        requests.len(),
        2,
        "one tool turn and one terminal assistant turn are required: {requests:#?}"
    );
    let commands_schema = requests[0]
        .pointer("/tools/0/parameters/properties/commands")
        .expect("first Codex request should expose command_run commands schema");
    assert_eq!(commands_schema["minItems"], 1);
    assert_eq!(commands_schema["maxItems"], 20);
    assert_eq!(
        commands_schema["items"]["properties"]["command_type"]["enum"],
        serde_json::json!(["apply_patch"])
    );
    let function_output = requests[1]
        .get("input")
        .and_then(Value::as_array)
        .and_then(|input| {
            input.iter().find(|item| {
                item.get("type").and_then(Value::as_str) == Some("function_call_output")
            })
        })
        .expect("second Codex request should replay command_run output");
    assert_eq!(function_output["call_id"], "call_stream_apply_patch_only");
    let provider_output = function_output["output"]
        .as_str()
        .and_then(|output| serde_json::from_str::<Value>(output).ok())
        .expect("function_call_output should contain structured command results");
    assert_eq!(provider_output["results"].as_array().map(Vec::len), Some(1));
    assert_eq!(provider_output["results"][0]["success"], true);
    drop(requests);

    let tool_results = tool_results(&result.session.session_log);
    let output = tool_results
        .iter()
        .find(|entry| entry["tool_name"] == "command_run")
        .and_then(|entry| entry.get("output"))
        .expect("command_run output should be persisted");
    assert_eq!(output["commands"].as_array().map(Vec::len), Some(1));
    assert_eq!(output["results"].as_array().map(Vec::len), Some(1));
    assert_eq!(output["results"][0]["success"], true);
    let assistant_records = result
        .session
        .session_log
        .iter()
        .map(|entry| entry.value())
        .filter(|entry| entry.get("role").and_then(Value::as_str) == Some("assistant"))
        .collect::<Vec<_>>();
    assert_eq!(assistant_records.len(), 1);
    assert_eq!(
        assistant_records[0].get("content").and_then(Value::as_str),
        Some("Apply-only repair completed.")
    );
}

#[test]
fn coding_agent_executes_command_run_command_before_stream_finishes() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let provider = MockProvider::start_codex_streaming_probe(workspace.clone());
    let llm_config = write_codex_llm_config(&workspace, provider.addr);
    let endpoint = format!("http://{}", provider.addr);
    let router_addr = mock_command_run_router_addr();
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("OPENAI_LOGIN", "oauth"),
        ("OPENAI_API_KEY", "test-key"),
        ("OPENAI_TOKEN_EXPIRES", MOCK_OPENAI_TOKEN_EXPIRES),
        ("OPENAI_CODEX_ENDPOINT", endpoint.as_str()),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-codex-stream"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_MANAS_MAX_TURNS", "2"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_MULTI_COMMAND_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_MULTI_COMMAND_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_in_directory(
        "e2e-stream-command-before-message-done".to_string(),
        SessionInput {
            user_input: "Use command_run in this code file workspace to create streamed-first.txt, then create streamed-second.txt."
                .to_string(),
            file_input: vec![],
            agent: Some("direct".to_string()),
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace.clone(),
    )
    .expect("coding agent should complete the streaming command_run e2e flow");

    assert!(
        provider
            .first_command_observed_before_response_finished
            .load(Ordering::SeqCst),
        "first streamed command did not execute before the provider finished sending the response; requests={:#?}; first_exists={}; second_exists={}",
        provider
            .requests
            .lock()
            .expect("mock provider requests lock"),
        workspace.join("streamed-first.txt").exists(),
        workspace.join("streamed-second.txt").exists()
    );
    assert_eq!(
        result.session.state,
        SessionState::Completed,
        "final_error={:?}; session log: {:#?}",
        result.final_error,
        result.session.session_log
    );
    assert!(
        workspace.join("streamed-first.txt").exists(),
        "first streamed command should create streamed-first.txt"
    );
    assert!(
        workspace.join("streamed-second.txt").exists(),
        "second streamed command should create streamed-second.txt"
    );
}

#[test]
fn streamed_single_task_status_is_backfilled_when_final_response_lacks_tool_call() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let provider = MockProvider::start_codex_streaming_single_task_status_missing_final_tool_call();
    let llm_config = write_codex_llm_config(&workspace, provider.addr);
    let endpoint = format!("http://{}", provider.addr);
    let router_addr = mock_command_run_router_addr();
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("OPENAI_LOGIN", "oauth"),
        ("OPENAI_API_KEY", "test-key"),
        ("OPENAI_TOKEN_EXPIRES", MOCK_OPENAI_TOKEN_EXPIRES),
        ("OPENAI_CODEX_ENDPOINT", endpoint.as_str()),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-codex-stream"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_MANAS_MAX_TURNS", "3"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_in_directory(
        "e2e-stream-single-task-status-backfill".to_string(),
        SessionInput {
            user_input: "Create a GUI avatar effect and continue after task status setup."
                .to_string(),
            file_input: vec![],
            agent: Some("direct".to_string()),
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace,
    )
    .expect("streamed task_status-only first turn should continue to a follow-up provider turn");

    assert_eq!(
        result.session.state,
        SessionState::Completed,
        "final_error={:?}; session log: {:#?}",
        result.final_error,
        result.session.session_log
    );
    assert_eq!(
        result.session.task_type,
        vec!["visual".to_string(), "frontend".to_string()]
    );
    let requests = provider
        .requests
        .lock()
        .expect("mock provider requests lock");
    assert!(
        requests.len() >= 2,
        "streamed task_status-only first turn should not be lost before backfill; requests={requests:#?}"
    );
    let second_request = requests
        .get(1)
        .expect("second provider request should exist");
    let serialized = second_request.to_string();
    assert!(
        serialized.contains("function_call_output"),
        "second provider request should replay streamed command_run output: {second_request:#?}"
    );
    assert!(
        serialized.contains("call_stream_task_status_only"),
        "second provider request should bind output to the streamed provider call id: {second_request:#?}"
    );
    assert!(
        serialized.contains("GUI avatar effect") && serialized.contains("task_status"),
        "second provider request should include the streamed task_status result: {second_request:#?}"
    );
    assert!(
        serialized.contains("Frontend Operation Manual")
            && serialized.contains("Visual Operation Manual"),
        "second provider request should include manuals activated by streamed task_type: {second_request:#?}"
    );
}

#[test]
fn non_planning_agent_visible_reply_with_task_status_doing_is_backfilled_on_followup_turn() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let provider = MockProvider::start_task_status_doing_with_visible_reply();
    let llm_config = write_llm_config(&workspace, provider.addr);
    let router_addr = mock_command_run_router_addr();
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("OPENAI_API_KEY", "test-key"),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_MANAS_MAX_TURNS", "4"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_in_directory(
        "e2e-nonplanning-doing-visible-reply".to_string(),
        SessionInput {
            user_input: "Answer directly, mark the task status, and stop.".to_string(),
            file_input: vec![],
            agent: Some("direct".to_string()),
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace,
    )
    .expect("non-planning task_status doing session should backfill before completion");

    assert_eq!(
        result.session.state,
        SessionState::Completed,
        "final_error={:?}; session log: {:#?}",
        result.final_error,
        result.session.session_log
    );
    let requests = provider
        .requests
        .lock()
        .expect("mock provider requests lock");
    assert!(
        requests.len() >= 2,
        "single doing task_status must be backfilled before the runtime loop can end; requests={requests:#?}"
    );
    let second_request = requests
        .get(1)
        .expect("second provider request should exist");
    let serialized = second_request.to_string();
    assert!(
        serialized.contains("call_task_status_doing") && serialized.contains("task_status"),
        "second provider request should replay the doing task_status output: {second_request:#?}"
    );
    assert!(
        result
            .session
            .session_log
            .iter()
            .map(|entry| entry.value())
            .any(
                |entry| entry.get("role").and_then(Value::as_str) == Some("assistant")
                    && entry
                        .get("content")
                        .and_then(Value::as_str)
                        .is_some_and(|content| content.contains("Done."))
            )
    );
    assert_eq!(
        requests.len(),
        2,
        "runtime should do exactly one follow-up LLM turn to recover from stale task_status doing"
    );
}

#[test]
fn task_status_only_first_turn_is_backfilled_with_manuals_on_followup_turn() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let provider = MockProvider::start_task_status_only_then_final();
    let llm_config = write_llm_config(&workspace, provider.addr);
    let router_addr = mock_command_run_router_addr();
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("OPENAI_API_KEY", "test-key"),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_MANAS_MAX_TURNS", "4"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_in_directory(
        "e2e-task-status-only-backfill".to_string(),
        SessionInput {
            user_input: "Create a GUI avatar effect and continue after task status setup."
                .to_string(),
            file_input: vec![],
            agent: Some("direct".to_string()),
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace,
    )
    .expect("task_status-only first turn should continue to a follow-up provider turn");

    assert_eq!(
        result.session.state,
        SessionState::Completed,
        "final_error={:?}; session log: {:#?}",
        result.final_error,
        result.session.session_log
    );
    assert_eq!(
        result.session.task_type,
        vec!["visual".to_string(), "frontend".to_string()]
    );
    let requests = provider
        .requests
        .lock()
        .expect("mock provider requests lock");
    assert!(
        requests.len() >= 2,
        "task_status-only first turn should not end the runtime loop before backfill; requests={requests:#?}"
    );
    let second_request = requests
        .get(1)
        .expect("second provider request should exist");
    let serialized = second_request.to_string();
    assert!(
        serialized.contains("function_call_output"),
        "second provider request should replay command_run function output: {second_request:#?}"
    );
    assert!(
        serialized.contains("call_task_status_only"),
        "second provider request should bind the tool output to the original provider call id: {second_request:#?}"
    );
    assert!(
        serialized.contains("GUI avatar effect") && serialized.contains("task_status"),
        "second provider request should include the task_status result content: {second_request:#?}"
    );
    assert!(
        serialized.contains("Frontend Operation Manual")
            && serialized.contains("Visual Operation Manual"),
        "second provider request should include manuals activated by task_type: {second_request:#?}"
    );
}

#[test]
fn no_tool_visible_reply_auto_compacts_and_completes_with_one_provider_request() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let workspace_text = workspace.to_string_lossy().to_string();
    let session_id = "e2e-no-tool-post-reply-auto-compaction";
    let initial_runtime_id = "runtime-no-tool-post-reply-auto-compaction";
    let initial_lease_id = "lease-no-tool-post-reply-auto-compaction";
    typed_session::create_via_service(typed_session::root_create_request(
        session_id,
        &workspace_text,
        "No-tool post-reply automatic compaction",
        1,
    ))
    .expect("compaction session should be created before leased execution");
    assert!(matches!(
        session_log_contract::client::call_service(&SessionLogCommand::RegisterRuntime(
            RegisterRuntimeRequest {
                runtime_id: initial_runtime_id.to_string(),
                session_id: session_id.to_string(),
                fallback_from_id: None,
                lifecycle: None,
            }
        ))
        .expect("initial runtime registration"),
        SessionLogResponse::RuntimeRegistered {
            result: RuntimeRegistrationOutcome::Registered { .. }
        }
    ));
    assert!(matches!(
        session_log_contract::client::call_service(&SessionLogCommand::ActivateRuntimeLease(
            ActivateRuntimeLeaseRequest {
                runtime_id: initial_runtime_id.to_string(),
                lease_id: initial_lease_id.to_string(),
            }
        ))
        .expect("initial runtime lease activation"),
        SessionLogResponse::RuntimeLeaseActivated {
            result: RuntimeLeaseOutcome::Activated
        }
    ));
    let provider = MockProvider::start_no_tool_visible_reply_with_synthetic_compaction_usage();
    let llm_config = write_llm_config(&workspace, provider.addr);
    let router_addr = mock_command_run_router_addr();
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("OPENAI_API_KEY", "test-key"),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        // Leave room for a second turn so the turn limit cannot mask the regression.
        ("TURA_MANAS_MAX_TURNS", "4"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_with_lease_in_directory(
        session_id.to_string(),
        initial_runtime_id.to_string(),
        initial_lease_id.to_string(),
        SessionInput {
            user_input: "Complete the offline fixture with a short visible reply and no tool calls."
                .to_string(),
            file_input: vec![],
            agent: Some("direct".to_string()),
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace,
    )
    .expect("no-tool post-reply compaction should complete");
    let requests = provider
        .requests
        .lock()
        .expect("mock provider requests lock");
    assert_eq!(
        requests.len(),
        1,
        "successful no-tool post-reply compaction must not request another runtime; final_error={:?}; requests={requests:#?}",
        result.final_error
    );
    drop(requests);
    assert_eq!(
        result.session.state,
        SessionState::Completed,
        "final_error={:?}; session log: {:#?}",
        result.final_error,
        result.session.session_log
    );
    assert!(result.final_error.is_none());
    let log = &result.session.session_log;
    assert!(tool_results(log).is_empty(), "the reply must have no tools");
    let compactions = log
        .iter()
        .map(|entry| entry.value())
        .filter(|entry| entry.get("type").and_then(Value::as_str) == Some("context_compaction"))
        .collect::<Vec<_>>();
    assert_eq!(compactions.len(), 1, "automatic compaction must occur once");
    let compact_text = compactions[0]
        .get("content")
        .and_then(Value::as_str)
        .expect("compaction checkpoint must contain text");
    assert!(
        compact_text.contains(MOCK_COMPACTION_REPLY),
        "compaction must retain the visible completion reply; checkpoint={compact_text}"
    );
    assert!(compact_text.contains("Automatic context checkpoint:"));
    assert!(compact_text.contains(&format!(
        "provider input was about {MOCK_COMPACTION_INPUT_TOKENS} tokens"
    )));
    // Synthetic fixture input, not measured usage or cost: cross the actual active limit.
    let limit = result.session.context_tokens.limit;
    assert!(
        limit > 0 && MOCK_COMPACTION_INPUT_TOKENS > limit,
        "synthetic fixture input must exceed the active context limit; limit={limit}"
    );

    let snapshot = match session_log_contract::client::call_service(
        &SessionLogCommand::GetSession(GetSessionRequest {
            session_id: result.session.session_id.clone(),
        }),
    )
    .expect("compacted session checkpoint should be queryable")
    {
        SessionLogResponse::Session {
            session: Some(snapshot),
        } => snapshot,
        response => panic!("unexpected compacted session response: {response:?}"),
    };
    assert_eq!(snapshot.lifecycle_projection.state, SessionState::Completed);
    assert_eq!(snapshot.lifecycle_projection.active_runtime_id, None);
    assert_eq!(snapshot.lifecycle_projection.runtime_ids.len(), 1);
    let runtime_id = snapshot.lifecycle_projection.runtime_ids[0].clone();
    let checkpoint = snapshot
        .into_management()
        .expect("compacted session management checkpoint should decode");
    assert_eq!(checkpoint.state, SessionState::Completed);
    assert_eq!(checkpoint.context_tokens.limit, limit);
    assert_eq!(
        checkpoint.context_tokens.input,
        result.session.context_tokens.input,
        "terminal checkpoint must retain the post-compaction context state"
    );
    let runtime = match session_log_contract::client::call_service(
        &SessionLogCommand::ReplayRuntime(ReplayRuntimeRequest { runtime_id }),
    )
    .expect("completion runtime should replay")
    {
        SessionLogResponse::RuntimeReplayed {
            runtime: Some(replay),
        } => replay.aggregate,
        response => panic!("unexpected completion runtime replay response: {response:?}"),
    };
    assert_eq!(runtime.state, RuntimeState::Finished);
    assert_eq!(runtime.text, MOCK_COMPACTION_REPLY);
}

#[test]
fn single_done_task_status_with_long_visible_reply_completes_without_backfill_turn() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let provider = MockProvider::start_task_status_done_with_long_visible_reply();
    let llm_config = write_llm_config(&workspace, provider.addr);
    let router_addr = mock_command_run_router_addr();
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("OPENAI_API_KEY", "test-key"),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_MANAS_MAX_TURNS", "4"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_in_directory(
        "e2e-single-done-task-status-long-reply".to_string(),
        SessionInput {
            user_input: "Finish with a long assistant response and a done task status.".to_string(),
            file_input: vec![],
            agent: Some("direct".to_string()),
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace,
    )
    .expect("single done task_status with a long visible reply should complete");

    assert_eq!(result.session.state, SessionState::Completed);
    assert_eq!(
        result
            .session
            .task_plan
            .detailed_tasks
            .first()
            .map(|task| task.status),
        Some(lifecycle::PlanStatus::Done),
        "done task_status should still update task state; log={:#?}",
        result.session.session_log
    );
    assert!(
        result
            .session
            .session_log
            .iter()
            .map(|entry| entry.value())
            .any(
                |entry| entry.get("role").and_then(Value::as_str) == Some("assistant")
                    && entry
                        .get("content")
                        .and_then(Value::as_str)
                        .is_some_and(|content| content.len() > 200)
            )
    );
    assert_eq!(
        provider
            .requests
            .lock()
            .expect("mock provider requests lock")
            .len(),
        1,
        "a completion summary above 200 bytes must not trigger another provider runtime"
    );
}

#[test]
fn resumed_execution_evidence_keeps_prior_commands_files_usage_without_reinjecting_prompt_history() {
    use session_log_contract::{ReadExecutionEvidenceRequest, PersistSessionDeltaRequest, SessionContextRecord, SessionDeltaEntry};
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let session_id = "e2e-resumed-execution-evidence";
    typed_session::create_via_service(typed_session::root_create_request(session_id,
        workspace.to_string_lossy().as_ref(), "Resumed execution evidence", 1)).expect("create fixture session");
    // Synthetic historical execution facts, not measured usage or cost. Two
    // persisted retention advances make the old evidence inaccessible to prompts.
    let mut records = vec![
        serde_json::json!({"type":"tool_result", "tool_name":"command_run", "output":{"results":[{
            "command_type":"shell_command", "success":true, "output":{"stdout":"EARLY_EXECUTION_SENTINEL_COMMAND"}}]}}).to_string(),
        serde_json::json!({"type":"tool_result", "tool_name":"command_run", "output":{"results":[{
            "command_type":"apply_patch", "success":true, "changes":[{"path":"EARLY_EXECUTION_SENTINEL.rs","kind":"update"}]}]}}).to_string(),
        serde_json::json!({"type":"runtime_usage", "runtime_id":"fixture-prior",
            "usage":{"input_tokens":7,"output_tokens":2,"total_tokens":9}}).to_string(),
        serde_json::json!({"type":"runtime_provider_observation", "runtime_id":"fixture-prior",
            "provider_observation":{"schema_version":"provider_observation_v1", "source":"provider_response",
                "response_id":"fixture-prior-response", "model":"fixture-prior-model", "service_tier":"priority"}}).to_string(),
    ];
    records.extend((4..130).map(|index| serde_json::json!({"role":"assistant","content":format!("Historical fixture {index}")}).to_string()));
    let mut start = 0;
    for (management_sequence, retained, entries) in [
        (0, 0, records),
        (1, 100, vec![serde_json::json!({"type":"context_compaction","role":"system","content":"First fixture checkpoint"}).to_string()]),
        (2, 130, vec![serde_json::json!({"type":"context_compaction","role":"system","content":"Second fixture checkpoint"}).to_string()]),
    ] {
        let previous = match session_log_contract::client::call_service(&SessionLogCommand::GetSession(GetSessionRequest { session_id:session_id.to_string() })).expect("get canonical fixture") {
            SessionLogResponse::Session { session:Some(snapshot) } => snapshot.into_management().expect("management"),
            response => panic!("unexpected session response: {response:?}"),
        };
        let next = previous.persistence_view(retained);
        let count = entries.len() as u64;
        let delta = PersistSessionDeltaRequest { session_id:session_id.to_string(), management_sequence,
            management_delta:lifecycle::SessionManagement::persistence_delta(Some(&previous), &next), retained_from_sequence:retained,
            entries:entries.into_iter().enumerate().map(|(offset, raw_record)| SessionDeltaEntry {
                context:SessionContextRecord { sequence:start + offset as u64, raw_record }, projection:None }).collect() };
        for _ in 0..2 { // Retry the exact public write; no duplicated execution facts.
            match session_log_contract::client::call_service(&SessionLogCommand::PersistSessionDelta(Box::new(delta.clone()))).expect("persist/retry fixture") {
                SessionLogResponse::SessionDeltaPersisted { next_sequence, .. } => assert_eq!(next_sequence, start + count),
                response => panic!("unexpected persistence response: {response:?}"),
            }
        }
        start += count;
    }
    let provider = MockProvider::start_no_tool_visible_reply_with_synthetic_compaction_usage();
    let llm_config = write_llm_config(&workspace, provider.addr);
    let router_addr = mock_command_run_router_addr();
    let _env = EnvGuard::set(&[
        ("TURA_PROVIDER_CONFIG", llm_config.to_string_lossy().as_ref()), ("OPENAI_API_KEY", "test-key"),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"), ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"), ("TURA_MANAS_MAX_TURNS", "4"), ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        ("TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS", MOCK_PROVIDER_STREAM_TIMEOUT_MS),
        ("TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS", MOCK_PROVIDER_STREAM_TIMEOUT_MS),
    ]);
    let result = mano::process_from_gateway_session_in_directory(session_id.to_string(), SessionInput {
        user_input:"Complete the resumed offline fixture with no tool calls.".to_string(), file_input:vec![],
        agent:Some("direct".to_string()), runtime_context:None, planning_mode_override:None,
    }, workspace).expect("resume with retained model context");
    assert_eq!(result.session.state, SessionState::Completed, "{:?}", result.final_error);
    assert!(result.session.session_log_retention.omitted_entries >= 130);
    assert!(result.session.session_log.iter().all(|entry| !entry.raw().contains("EARLY_EXECUTION_SENTINEL")));
    let requests = provider.requests.lock().expect("fixture provider requests");
    assert_eq!(requests.len(), 1);
    assert!(!requests[0].to_string().contains("EARLY_EXECUTION_SENTINEL"), "execution history must not increase model context");
    drop(requests);
    let request = ReadExecutionEvidenceRequest { session_id:session_id.to_string(), snapshot:None, from_sequence:0,
        max_records:2, max_bytes:session_log_contract::EXECUTION_EVIDENCE_PAGE_BYTES, include_summary:true };
    let head = match session_log_contract::client::call_service(&SessionLogCommand::ReadExecutionEvidence(request.clone())).expect("public evidence head") {
        SessionLogResponse::ExecutionEvidence { evidence } => { evidence.validate(&request).expect("public evidence identity"); evidence },
        response => panic!("unexpected evidence response: {response:?}"),
    };
    assert_eq!(head.snapshot.next_sequence, result.session.session_log_retention.omitted_entries + result.session.session_log.len() as u64);
    assert!(head.records[0].raw_record.contains("EARLY_EXECUTION_SENTINEL_COMMAND"));
    assert!(head.records[1].raw_record.contains("EARLY_EXECUTION_SENTINEL.rs"));
    let summary = head.summary.as_ref().expect("historical usage");
    assert_eq!(summary.usage["input_tokens"], MOCK_COMPACTION_INPUT_TOKENS + 7);
    assert_eq!(summary.usage["coverage"]["known_runtime_count"], 2);
    assert_eq!(summary.usage["coverage"]["status"], "complete");
    let mut seen = 0;
    let mut early_records = 0;
    session_log_contract::visit_execution_evidence(&head.snapshot, |request| {
        match session_log_contract::client::call_service(&SessionLogCommand::ReadExecutionEvidence(request)).map_err(|error| error.to_string())? {
            SessionLogResponse::ExecutionEvidence { evidence } => Ok(evidence),
            response => Err(format!("unexpected evidence page: {response:?}")),
        }
    }, |record| {
        assert_eq!(record.sequence, seen);
        seen += 1;
        early_records += u64::from(record.raw_record.contains("EARLY_EXECUTION_SENTINEL"));
        Ok(())
    }).expect("multi-page resumed evidence traversal");
    assert_eq!(seen, head.snapshot.next_sequence);
    assert_eq!(early_records, 2);
}

#[test]
fn initial_task_state_first_provider_manual_preserves_tool_schemas() {
    fn contains_full_manual(value: &Value, manual: &str) -> bool {
        match value {
            Value::String(text) => text.contains(manual),
            Value::Array(values) => values.iter().any(|value| contains_full_manual(value, manual)),
            Value::Object(values) => values.values().any(|value| contains_full_manual(value, manual)),
            _ => false,
        }
    }

    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let debug_manual = runtime::prompt_style::runtime_prompt_manual::available_manuals()
        .into_iter()
        .find(|manual| manual.id == "debug")
        .expect("debug operation manual")
        .prompt
        .trim()
        .to_string();
    assert!(!debug_manual.is_empty());
    let contract = serde_json::json!({
        "schema_version": "jspace_contract_v2",
        "allowed_operations": ["read", "command"],
        "read_scopes": ["src/lib.rs"],
    });
    let mut tool_schemas = Vec::new();

    for initial_state_present in [false, true] {
        let request_sha256 = if initial_state_present { "a" } else { "b" }.repeat(64);
        let session_id = format!("full-{request_sha256}");
        let runtime_id = format!("runtime-{session_id}");
        let lease_id = format!("lease-{session_id}");
        let initial_state = serde_json::json!({
            "schema_version": "nokiy_initial_task_state_v1",
            "request_sha256": request_sha256,
            "session_id": session_id,
            "task_group": "runtime integration tests",
            "task_type": ["debug"],
        }).to_string();
        let workspace = create_rust_workspace();
        let provider = MockProvider::start_task_status_done_with_short_visible_reply();
        let llm_config = write_llm_config(&workspace, provider.addr);
        let router_addr = mock_command_run_router_addr();
        let home = std::env::var("TURA_HOME").expect("fixture session home");
        let _env = EnvGuard::set(&[
            ("TURA_DB_ROOT", home.as_str()),
            ("TURA_PROVIDER_CONFIG", llm_config.to_string_lossy().as_ref()),
            ("OPENAI_API_KEY", "test-key"),
            ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"),
            ("TURA_ROUTER_ADDR", router_addr.as_str()),
            ("TURA_GATEWAY_CALLBACKS", "0"),
            ("TURA_RUNTIME_AUTO_GIT_COMMIT", "0"),
            ("TURA_MANAS_MAX_TURNS", "4"),
            ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
            ("TURA_NOKIY_BOUNDED_ONE_TURN", "1"),
            ("TURA_NOKIY_EVIDENCE_ONLY_TERMINAL", "1"),
            ("TURA_NOKIY_INITIAL_TASK_STATE", initial_state.as_str()),
            ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
            ("TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS", MOCK_PROVIDER_STREAM_TIMEOUT_MS),
            ("TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS", MOCK_PROVIDER_STREAM_TIMEOUT_MS),
        ]);
        if !initial_state_present {
            // EnvGuard restores the original value; SessionDbTestService holds ENV_LOCK.
            // SAFETY: the lock serializes all fixture environment mutations.
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::remove_var("TURA_NOKIY_INITIAL_TASK_STATE")
            };
        }

        typed_session::create_via_service(typed_session::root_create_request(
            &session_id,
            workspace.to_string_lossy().as_ref(),
            "Initial task state fixture",
            1,
        )).expect("precreate canonical fixture session");
        assert!(matches!(
            session_log_contract::client::call_service(&SessionLogCommand::RegisterRuntime(
                RegisterRuntimeRequest {
                    runtime_id: runtime_id.clone(),
                    session_id: session_id.clone(),
                    fallback_from_id: None,
                    lifecycle: None,
                }
            )).expect("register initial fixture runtime"),
            SessionLogResponse::RuntimeRegistered {
                result: RuntimeRegistrationOutcome::Registered { .. }
            }
        ));
        assert!(matches!(
            session_log_contract::client::call_service(&SessionLogCommand::ActivateRuntimeLease(
                ActivateRuntimeLeaseRequest {
                    runtime_id: runtime_id.clone(),
                    lease_id: lease_id.clone(),
                }
            )).expect("activate initial fixture runtime lease"),
            SessionLogResponse::RuntimeLeaseActivated {
                result: RuntimeLeaseOutcome::Activated
            }
        ));
        let result = mano::orchestrate_for_session_with_lease_and_lifecycle_and_jspace_in_directory(
            SessionInput {
                user_input: "Finish with a short assistant response and a done task status."
                    .to_string(),
                file_input: vec![],
                agent: Some("direct".to_string()),
                runtime_context: None,
                planning_mode_override: None,
            },
            session_id,
            runtime_id,
            lease_id,
            workspace,
            None,
            Some(contract.clone()),
            None,
        ).expect("complete precreated J-Space fixture execution");
        assert!(result.final_error.is_none(), "{:?}", result.final_error);

        // The mock's done response changes task_type; only the first wire request proves startup.
        let first_request = provider.requests.lock().expect("captured provider requests")
            .first().cloned().expect("first actual provider request");
        let provider_input = first_request.get("input")
            .or_else(|| first_request.get("messages"))
            .expect("provider input/messages");
        assert_eq!(
            contains_full_manual(provider_input, &debug_manual),
            initial_state_present,
            "full debug manual presence in the first request must match initial state"
        );
        let tools = first_request.get("tools").and_then(Value::as_array)
            .expect("serialized provider tools");
        assert!(!tools.is_empty(), "tool equality must compare actual schemas");
        tool_schemas.push(tools.clone());
    }

    assert_eq!(
        tool_schemas[0], tool_schemas[1],
        "optional initial state must preserve provider tool schemas and cache behavior"
    );
}

#[test]
fn single_done_task_status_with_short_visible_reply_completes_without_backfill_turn() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let provider = MockProvider::start_task_status_done_with_short_visible_reply();
    let llm_config = write_llm_config(&workspace, provider.addr);
    let router_addr = mock_command_run_router_addr();
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("OPENAI_API_KEY", "test-key"),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"),
        ("TURA_ROUTER_ADDR", router_addr.as_str()),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_MANAS_MAX_TURNS", "4"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_in_directory(
        "e2e-single-done-task-status-short-reply".to_string(),
        SessionInput {
            user_input: "Finish with a short assistant response and a done task status."
                .to_string(),
            file_input: vec![],
            agent: Some("direct".to_string()),
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace,
    )
    .expect(
        "single done task_status with a short visible reply should complete without a backfill turn",
    );

    assert_eq!(result.session.state, SessionState::Completed);
    let requests = provider
        .requests
        .lock()
        .expect("mock provider requests lock");
    assert_eq!(
        requests.len(),
        1,
        "a nonblank short done reply must complete without another provider turn; requests={requests:#?}"
    );
    assert_eq!(
        result
            .session
            .task_plan
            .detailed_tasks
            .first()
            .map(|task| task.status),
        Some(lifecycle::PlanStatus::Done),
        "done task_status should still update task state; log={:#?}",
        result.session.session_log
    );
    assert!(
        result
            .session
            .session_log
            .iter()
            .map(|entry| entry.value())
            .any(|entry| {
                entry.get("role").and_then(Value::as_str) == Some("assistant")
                    && entry.get("content").and_then(Value::as_str)
                        == Some("Done. Short visible reply.")
            }),
        "the original short assistant reply should be retained; log={:#?}",
        result.session.session_log
    );
    let results = tool_results(&result.session.session_log);
    assert_tool_success(&results, "command_run");
    assert!(
        results.iter().any(|entry| {
            entry["tool_name"] == "command_run"
                && entry["provider_metadata"]["call_id"] == "call_task_status_done_short"
        }),
        "command_run result should retain the original provider call id; results={results:#?}"
    );
}

#[test]
fn coding_agent_provider_retry_exhaustion_preserves_provider_error() {
    let _session_db = session_db_support::SessionDbTestService::start(&ENV_LOCK);
    let workspace = create_rust_workspace();
    let workspace_text = workspace.to_string_lossy().to_string();
    let session_id = "e2e-provider-retry-exhausted";
    let initial_runtime_id = "runtime-provider-retry-initial";
    let initial_lease_id = "lease-provider-retry-initial";
    typed_session::create_via_service(typed_session::root_create_request(
        session_id,
        &workspace_text,
        "Provider retry exhaustion",
        1,
    ))
    .expect("retry session should be created before leased execution");
    assert!(matches!(
        session_log_contract::client::call_service(&SessionLogCommand::RegisterRuntime(
            RegisterRuntimeRequest {
                runtime_id: initial_runtime_id.to_string(),
                session_id: session_id.to_string(),
                fallback_from_id: None,
                lifecycle: None,
            }
        ))
        .expect("initial runtime registration"),
        SessionLogResponse::RuntimeRegistered {
            result: RuntimeRegistrationOutcome::Registered { .. }
        }
    ));
    assert!(matches!(
        session_log_contract::client::call_service(&SessionLogCommand::ActivateRuntimeLease(
            ActivateRuntimeLeaseRequest {
                runtime_id: initial_runtime_id.to_string(),
                lease_id: initial_lease_id.to_string(),
            }
        ))
        .expect("initial runtime lease activation"),
        SessionLogResponse::RuntimeLeaseActivated {
            result: RuntimeLeaseOutcome::Activated
        }
    ));
    let provider = MockProvider::start_rate_limit();
    let llm_config = write_llm_config(&workspace, provider.addr);
    let _env = EnvGuard::set(&[
        (
            "TURA_PROVIDER_CONFIG",
            llm_config.to_string_lossy().as_ref(),
        ),
        ("OPENAI_API_KEY", "test-key"),
        ("TURA_SESSION_MODEL_OVERRIDE", "openai/mock-coder"),
        ("TURA_GATEWAY_CALLBACKS", "0"),
        ("TURA_MANAS_MAX_TURNS", "6"),
        ("TURA_NO_TOOL_RETRY_LIMIT", "0"),
        ("TURA_PROVIDER_RETRY_BACKOFF_MS", "0,0,0"),
        ("TURA_PROVIDER_TOTAL_TIMEOUT_MS", MOCK_PROVIDER_TIMEOUT_MS),
        (
            "TURA_PROVIDER_FIRST_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
        (
            "TURA_PROVIDER_IDLE_OUTPUT_TIMEOUT_MS",
            MOCK_PROVIDER_STREAM_TIMEOUT_MS,
        ),
    ]);

    let result = mano::process_from_gateway_session_with_lease_in_directory(
        session_id.to_string(),
        initial_runtime_id.to_string(),
        initial_lease_id.to_string(),
        SessionInput {
            user_input: "Trigger a provider rate limit and report the real error.".to_string(),
            file_input: vec![],
            agent: None,
            runtime_context: None,
            planning_mode_override: None,
        },
        workspace,
    )
    .expect("provider failures should be captured in the session result");

    assert_eq!(result.session.state, SessionState::Failed);
    let final_error = result
        .final_error
        .as_deref()
        .expect("final provider error should be preserved");
    assert!(
        final_error.contains("rate_limit_exceeded"),
        "provider error should survive retries; got {final_error}"
    );
    assert!(
        final_error.contains("Provider runtime failed after 3 retries"),
        "retry exhaustion context should be visible; got {final_error}"
    );
    let request_count = provider
        .requests
        .lock()
        .expect("mock provider requests lock")
        .len();
    assert_eq!(
        request_count, 4,
        "initial provider call plus three retries should be attempted"
    );

    let lifecycle_projection = match session_log_contract::client::call_service(
        &SessionLogCommand::GetSession(GetSessionRequest {
            session_id: result.session.session_id.clone(),
        }),
    )
    .expect("retry session should be queryable")
    {
        SessionLogResponse::Session {
            session: Some(snapshot),
        } => snapshot.lifecycle_projection,
        response => panic!("unexpected retry session response: {response:?}"),
    };
    assert_eq!(lifecycle_projection.state, SessionState::Failed);
    assert_eq!(lifecycle_projection.active_runtime_id, None);
    let runtime_ids = lifecycle_projection.runtime_ids;
    assert_eq!(runtime_ids.len(), 4, "each provider retry needs a runtime");

    let runtimes = runtime_ids
        .iter()
        .map(|runtime_id| {
            match session_log_contract::client::call_service(&SessionLogCommand::ReplayRuntime(
                ReplayRuntimeRequest {
                    runtime_id: runtime_id.clone(),
                },
            ))
            .expect("retry runtime should replay")
            {
                SessionLogResponse::RuntimeReplayed {
                    runtime: Some(replay),
                } => replay.aggregate,
                response => panic!("unexpected runtime replay response: {response:?}"),
            }
        })
        .collect::<Vec<_>>();
    assert!(
        runtimes
            .iter()
            .all(|runtime| runtime.state == RuntimeState::Failed)
    );
    assert_eq!(runtimes[0].fallback_from_id, None);
    for (index, runtime) in runtimes.iter().enumerate().skip(1) {
        assert_eq!(
            runtime.fallback_from_id.as_ref(),
            Some(&runtimes[index - 1].runtime_id),
            "retry runtime must reference the immediately failed invocation"
        );
    }
}
