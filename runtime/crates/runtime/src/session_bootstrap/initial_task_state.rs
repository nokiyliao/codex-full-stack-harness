//! Optional parent-bound startup state; never replay it over a resumed session.

use chrono::{DateTime, Utc};
use lifecycle::{SessionInput, SessionManagement};
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashSet;
use std::path::PathBuf;

use crate::prompt_style::runtime_prompt_manual::{
    append_missing_runtime_prompt_manuals, normalize_task_type_ids, valid_task_type_ids,
};

use super::persisted::load_persisted_gateway_session;
use super::{
    bootstrap_orchestration_session, create_session_with_topic, initial_messages_for_session,
};

const INITIAL_TASK_STATE_ENV: &str = "TURA_NOKIY_INITIAL_TASK_STATE";
const SCHEMA_VERSION: &str = "nokiy_initial_task_state_v1";
const MAX_ENVELOPE_BYTES: usize = 4096;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct InitialTaskState {
    schema_version: String,
    request_sha256: String,
    session_id: String,
    task_group: String,
    task_type: Vec<String>,
}

impl InitialTaskState {
    pub(crate) fn from_env() -> Result<Option<Self>, String> {
        match std::env::var(INITIAL_TASK_STATE_ENV) {
            Ok(raw) => Self::parse(
                Some(&raw),
                std::env::var("TURA_NOKIY_BOUNDED_ONE_TURN").as_deref() == Ok("1"),
            ),
            Err(std::env::VarError::NotPresent) => Ok(None),
            Err(std::env::VarError::NotUnicode(_)) => {
                Err(format!("{INITIAL_TASK_STATE_ENV} must be UTF-8"))
            }
        }
    }

    fn parse(raw: Option<&str>, bounded_one_turn: bool) -> Result<Option<Self>, String> {
        let Some(raw) = raw else {
            return Ok(None);
        };
        if raw.len() > MAX_ENVELOPE_BYTES {
            return Err(format!("{INITIAL_TASK_STATE_ENV} exceeds 4096 bytes"));
        }
        if !bounded_one_turn {
            return Err(format!(
                "{INITIAL_TASK_STATE_ENV} requires TURA_NOKIY_BOUNDED_ONE_TURN=1"
            ));
        }
        let mut state: Self = serde_json::from_str(raw)
            .map_err(|err| format!("invalid {INITIAL_TASK_STATE_ENV}: {err}"))?;
        if state.schema_version != SCHEMA_VERSION {
            return Err(format!(
                "invalid {INITIAL_TASK_STATE_ENV} schema_version: expected {SCHEMA_VERSION}"
            ));
        }
        if state.request_sha256.len() != 64
            || !state
                .request_sha256
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        {
            return Err(format!(
                "{INITIAL_TASK_STATE_ENV} request_sha256 must be 64 lowercase hex characters"
            ));
        }
        if state.session_id != format!("full-{}", state.request_sha256) {
            return Err(format!(
                "{INITIAL_TASK_STATE_ENV} session_id binding is invalid"
            ));
        }
        state.task_group = checked_text(&state.task_group, 256, "task_group")?;
        if !(1..=8).contains(&state.task_type.len()) {
            return Err(format!("{INITIAL_TASK_STATE_ENV} task_type requires 1..8 ids"));
        }
        let valid_ids = valid_task_type_ids();
        let mut seen = HashSet::new();
        for id in &mut state.task_type {
            *id = checked_text(id, 128, "task_type id")?;
            if !seen.insert(id.clone()) {
                return Err(format!(
                    "{INITIAL_TASK_STATE_ENV} task_type ids must be distinct"
                ));
            }
            if !valid_ids.contains(id) {
                return Err(format!(
                    "{INITIAL_TASK_STATE_ENV} has unknown task_type {id:?}"
                ));
            }
        }
        Ok(Some(state))
    }

    fn initialize(
        &self,
        session: &mut SessionManagement,
        first_execution: bool,
    ) -> Result<bool, String> {
        // Validate even for resumes and fallbacks; never trust the envelope's own id alone.
        if session.session_id != self.session_id {
            return Err(format!(
                "{INITIAL_TASK_STATE_ENV} does not match the actual session_id"
            ));
        }
        if !session
            .jspace_contract
            .as_ref()
            .is_some_and(Value::is_object)
        {
            return Err(format!(
                "{INITIAL_TASK_STATE_ENV} requires a session J-Space contract"
            ));
        }
        if !first_execution
            || session.session_current_turn != 0
            || !session.session_log.is_empty()
            || !session.task_type.is_empty()
            || !session.session_capabilities.is_empty()
            || session.session_log_retention.omitted_entries != 0
            || session.session_log_retention.last_compaction.is_some()
            || !session.task_plan.plan_summary.is_empty()
            || !session.task_plan.detailed_tasks.is_empty()
        {
            return Ok(false);
        }

        if session.auto_session_name {
            session.session_name = self.task_group.clone();
        }
        let mut task_plan = session.task_plan.clone();
        task_plan.plan_summary = self.task_group.clone();
        session.replace_task_plan(task_plan, Utc::now());
        session.replace_task_type(normalize_task_type_ids(
            self.task_type.iter().map(String::as_str),
        ));
        Ok(true)
    }
}

fn checked_text(value: &str, max_bytes: usize, field: &str) -> Result<String, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.len() > max_bytes || value.chars().any(char::is_control) {
        return Err(format!(
            "{INITIAL_TASK_STATE_ENV} {field} must be nonempty, at most {max_bytes} UTF-8 bytes, and contain no control characters"
        ));
    }
    Ok(trimmed.to_string())
}

fn is_first_runtime_execution(session: &SessionManagement, runtime_id: Option<&str>) -> bool {
    let Some(runtime_id) = runtime_id.filter(|id| !id.trim().is_empty()) else {
        return false;
    };
    // These are canonical lifecycle fields, not inferred from turn/log counters.
    // A later runtime, an inactive checkpoint, or a foreign runtime is a resume.
    session.state == lifecycle::SessionState::Running
        && !session.cancelled
        && session.active_runtime_id.as_deref() == Some(runtime_id)
        && matches!(session.runtime_ids.as_slice(), [first] if first == runtime_id)
}

pub(crate) fn bootstrap_session_for_initial_task_state(
    input: SessionInput,
    session_directory: Option<PathBuf>,
    gateway_session_id: Option<String>,
    now: DateTime<Utc>,
    has_initial_task_state: bool,
    initial_runtime_id: Option<&str>,
) -> Result<(SessionManagement, bool), String> {
    if !has_initial_task_state {
        // Preserve the original bootstrap path when the optional environment is absent.
        return bootstrap_orchestration_session(input, session_directory, gateway_session_id, now)
            .map(|session| (session, false));
    }

    // Keep the existing bootstrap operations/order. CLI precreation is not a
    // resume when the canonical lifecycle identifies our sole active runtime.
    if let Some(directory) = session_directory.as_ref() {
        crate::workspace_git::ensure_workspace_git_repo(directory)?;
    }
    if let Some(session_id) = gateway_session_id {
        if let Some(directory) = session_directory.as_ref()
            && let Some(mut persisted) = load_persisted_gateway_session(directory, &session_id)?
        {
            // Inspect the authoritative projection before preparing the user turn.
            let first_execution = is_first_runtime_execution(&persisted, initial_runtime_id);
            persisted.prepare_for_new_user_turn(input, now);
            if let Some(directory) = session_directory {
                persisted.session_directory = directory;
            }
            persisted.rebind_session_id(session_id);
            return Ok((persisted, first_execution));
        }

        let mut session = create_session_with_topic(input, session_directory)?;
        session.rebind_session_id(session_id);
        return Ok((session, true));
    }

    create_session_with_topic(input, session_directory).map(|session| (session, true))
}

pub(crate) fn initial_messages_with_task_state(
    session: &mut SessionManagement,
    initial_task_state: Option<&InitialTaskState>,
    first_execution: bool,
    is_fallback: bool,
) -> Result<Vec<Value>, String> {
    // Called only after agent policy selection, and before initial message insertion.
    let initialized = match initial_task_state {
        Some(state) => state.initialize(session, first_execution && !is_fallback)?,
        None => false,
    };
    let mut messages = initial_messages_for_session(session)?;
    if initialized {
        // Use the existing manual policy/normalization and update both representations.
        append_missing_runtime_prompt_manuals(session, Some(&mut messages))?;
    }
    Ok(messages)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manas::tool_catalog::startup_task_state_required;
    use crate::prompt_style::runtime_prompt_manual::RUNTIME_PROMPT_MANUAL_RECORD_TYPE;
    use serde_json::json;
    use std::collections::BTreeSet;

    fn envelope() -> Value {
        let hash = "a".repeat(64);
        json!({
            "schema_version": SCHEMA_VERSION,
            "request_sha256": hash,
            "session_id": format!("full-{hash}"),
            "task_group": "storefront frontend",
            "task_type": ["frontend"],
        })
    }

    fn parse(value: &Value) -> Result<InitialTaskState, String> {
        InitialTaskState::parse(Some(&value.to_string()), true)?
            .ok_or_else(|| "expected initial state".to_string())
    }

    fn session() -> SessionManagement {
        let mut session = SessionManagement::new(
            format!("full-{}", "a".repeat(64)),
            "untitled".to_string(),
            PathBuf::from("C:/workspace"),
            false,
            Vec::<String>::new(),
            SessionInput {
                user_input: "implement storefront".to_string(),
                file_input: vec![],
                agent: None,
                runtime_context: None,
                planning_mode_override: None,
            },
            "implement storefront".to_string(),
            Utc::now(),
        );
        session.goal_mode = false;
        session.reflection_enabled = false;
        session.op_manual_enabled = true;
        session.no_op_manual = false;
        session.jspace_contract = Some(json!({
            "schema_version": "jspace_contract_v2",
            "allowed_operations": ["read"],
            "read_scopes": ["admitted.txt"],
        }));
        session
    }

    fn manuals(session: &SessionManagement) -> Vec<Value> {
        session
            .session_log
            .iter()
            .filter_map(|entry| serde_json::from_str::<Value>(entry).ok())
            .filter(|record| record["type"] == RUNTIME_PROMPT_MANUAL_RECORD_TYPE)
            .collect()
    }

    fn startup_gate(session: &SessionManagement) -> bool {
        startup_task_state_required(session, &BTreeSet::from(["task_status".to_string()]))
    }

    fn first_runtime_session() -> SessionManagement {
        let mut session = session();
        let mut aggregate = lifecycle::SessionAggregate::new(session.session_id.clone());
        aggregate
            .execute(lifecycle::SessionCommand::RuntimeStarted {
                runtime_id: "runtime-first".to_string(),
            })
            .expect("start canonical initial runtime");
        session.replace_lifecycle_projection(aggregate.query(lifecycle::SessionQuery::Lifecycle));
        session
    }

    #[test]
    fn initializes_first_message_vector_and_persisted_parent_manuals() {
        let mut value = envelope();
        value["task_group"] = json!(" storefront frontend ");
        value["task_type"] = json!([" frontend "]);
        let state = parse(&value).expect("valid trimmed state");
        let mut session = session();
        assert!(startup_gate(&session));

        let messages = initial_messages_with_task_state(&mut session, Some(&state), true, false)
            .expect("first messages");
        assert_eq!(session.session_name, "storefront frontend");
        assert_eq!(session.task_plan.plan_summary, "storefront frontend");
        assert!(session.task_plan.detailed_tasks.is_empty());
        assert_eq!(session.task_type, normalize_task_type_ids(["frontend"]));
        assert!(!startup_gate(&session));

        let records = manuals(&session);
        assert_eq!(records.len(), session.task_type.len());
        assert_eq!(
            records
                .iter()
                .map(|record| record["task_type"].as_str().expect("id"))
                .collect::<Vec<_>>(),
            vec!["visual", "frontend"]
        );
        let user_position = messages
            .iter()
            .position(|message| message["role"] == "user")
            .expect("initial user message");
        for record in &records {
            let positions = messages
                .iter()
                .enumerate()
                .filter_map(|(position, message)| {
                    (message.get("role") == record.get("role")
                        && message.get("content") == record.get("content"))
                    .then_some(position)
                })
                .collect::<Vec<_>>();
            assert_eq!(positions.len(), 1);
            assert!(positions[0] > user_position);
        }
        let persisted: SessionManagement = serde_json::from_value(
            serde_json::to_value(&session).expect("serialize initial checkpoint"),
        )
        .expect("restore initial checkpoint");
        assert_eq!(persisted.task_type, session.task_type);
        assert_eq!(
            persisted.task_plan.plan_summary,
            session.task_plan.plan_summary
        );
        assert_eq!(manuals(&persisted), records);
    }

    #[test]
    fn precreated_first_runtime_initializes_before_messages_but_not_on_fallback() {
        let state = parse(&envelope()).expect("valid state");
        for fallback in [false, true] {
            let mut session = first_runtime_session();
            let jspace = session.jspace_contract.clone();
            let first_execution = is_first_runtime_execution(&session, Some("runtime-first"));
            assert!(first_execution);
            assert!(startup_gate(&session));
            let messages = initial_messages_with_task_state(
                &mut session,
                Some(&state),
                first_execution,
                fallback,
            )
            .expect("precreated initial messages");
            if fallback {
                assert!(session.task_type.is_empty());
                assert!(session.task_plan.plan_summary.is_empty());
                assert!(manuals(&session).is_empty());
                assert!(startup_gate(&session));
            } else {
                assert_eq!(session.task_plan.plan_summary, "storefront frontend");
                assert_eq!(session.task_type, normalize_task_type_ids(["frontend"]));
                assert!(!startup_gate(&session));
                let records = manuals(&session);
                assert_eq!(records.len(), 2);
                for record in records {
                    assert!(messages.iter().any(|message| {
                        message.get("role") == record.get("role")
                            && message.get("content") == record.get("content")
                    }));
                }
            }
            assert_eq!(session.runtime_ids, vec!["runtime-first".to_string()]);
            assert_eq!(session.active_runtime_id.as_deref(), Some("runtime-first"));
            assert_eq!(session.jspace_contract, jspace);
            assert!(!session.disable_permission_restrictions);
        }
    }

    #[test]
    fn empty_precreated_checkpoints_require_the_current_initial_runtime_identity() {
        use lifecycle::SessionState::{Cancelled, Completed, Created, Failed, Paused, Running};
        let state = parse(&envelope()).expect("valid state");
        let first = "runtime-first";
        for (current, history, active, lifecycle_state, cancelled) in [
            (Some(first), vec![], None, Created, false),
            (None, vec![first], Some(first), Running, false),
            (Some("foreign"), vec![first], Some(first), Running, false),
            (Some(""), vec![first], Some(first), Running, false),
            (Some(first), vec![first], None, Running, false),
            (Some(first), vec![first], Some("foreign"), Running, false),
            (Some(first), vec!["foreign"], Some(first), Running, false),
            (Some(first), vec!["prior", first], Some(first), Running, false),
            (Some(first), vec![first], Some(first), Created, false),
            (Some(first), vec![first], Some(first), Completed, false),
            (Some(first), vec![first], Some(first), Failed, false),
            (Some(first), vec![first], Some(first), Paused, false),
            (Some(first), vec![first], Some(first), Cancelled, false),
            (Some(first), vec![first], Some(first), Running, true),
        ] {
            let mut session = session();
            let mut projection = session.lifecycle_projection();
            projection.runtime_ids = history.into_iter().map(str::to_string).collect();
            projection.active_runtime_id = active.map(str::to_string);
            projection.state = lifecycle_state;
            projection.cancelled = cancelled;
            session.replace_lifecycle_projection(projection);
            assert_eq!(session.session_current_turn, 0);
            assert!(session.session_log.is_empty());
            let before = session.clone();
            let first_execution = is_first_runtime_execution(&session, current);
            assert!(!first_execution, "current={current:?}; session={session:?}");
            assert!(!state.initialize(&mut session, first_execution).expect("safe resume"));
            assert_eq!(session, before);
            assert!(startup_gate(&session));
        }
    }

    #[test]
    fn absent_state_preserves_original_messages_and_startup_fence() {
        assert!(
            InitialTaskState::parse(None, false)
                .expect("absent state")
                .is_none()
        );
        let mut session = session();
        let mut control = session.clone();
        let expected = initial_messages_for_session(&mut control).expect("original messages");
        let actual = initial_messages_with_task_state(&mut session, None, true, false)
            .expect("unchanged messages");
        assert_eq!(actual, expected);
        assert_eq!(session.session_name, control.session_name);
        assert_eq!(session.task_plan, control.task_plan);
        assert!(session.task_type.is_empty());
        assert!(manuals(&session).is_empty());
        assert!(startup_gate(&session));
    }

    #[test]
    fn rejects_malformed_unknown_null_missing_and_duplicate_fields() {
        for raw in ["", "null", "[]", "{", "true", "{}"] {
            assert!(InitialTaskState::parse(Some(raw), true).is_err(), "{raw}");
        }
        let mut unknown = envelope();
        unknown["extra"] = json!(true);
        assert!(parse(&unknown).is_err());
        for field in [
            "schema_version",
            "request_sha256",
            "session_id",
            "task_group",
            "task_type",
        ] {
            let mut null = envelope();
            null[field] = Value::Null;
            assert!(parse(&null).is_err(), "null {field}");
            let mut missing = envelope();
            missing.as_object_mut().expect("object").remove(field);
            assert!(parse(&missing).is_err(), "missing {field}");
        }
        let duplicate = envelope().to_string().replacen(
            '{',
            "{\"schema_version\":\"nokiy_initial_task_state_v1\",",
            1,
        );
        assert!(InitialTaskState::parse(Some(&duplicate), true).is_err());
    }

    #[test]
    fn rejects_wrong_schema_hash_and_self_binding() {
        let mut value = envelope();
        value["schema_version"] = json!("nokiy_initial_task_state_v2");
        assert!(parse(&value).unwrap_err().contains(SCHEMA_VERSION));
        for hash in ["a".repeat(63), "a".repeat(65), "A".repeat(64), "g".repeat(64)] {
            let mut value = envelope();
            value["request_sha256"] = json!(hash);
            value["session_id"] = json!(format!("full-{hash}"));
            assert!(parse(&value).is_err());
        }
        let mut value = envelope();
        value["session_id"] = json!(format!("full-{}", "b".repeat(64)));
        assert!(parse(&value).is_err());
    }

    #[test]
    fn enforces_utf8_envelope_group_and_type_bounds() {
        let raw = envelope().to_string();
        let at_limit = format!("{raw}{}", " ".repeat(MAX_ENVELOPE_BYTES - raw.len()));
        assert!(InitialTaskState::parse(Some(&at_limit), true).is_ok());
        assert!(InitialTaskState::parse(Some(&format!("{at_limit} ")), true).is_err());

        let mut value = envelope();
        value["task_group"] = json!("é".repeat(128));
        assert!(parse(&value).is_ok());
        for group in [
            "é".repeat(129),
            String::new(),
            "   ".to_string(),
            "group\n".to_string(),
            "\u{7f}group".to_string(),
        ] {
            value["task_group"] = json!(group);
            assert!(parse(&value).is_err());
        }
        for ids in [
            json!([]),
            json!(vec!["frontend"; 9]),
            json!(["frontend", " frontend "]),
            json!([""]),
            json!(["frontend\t"]),
            json!([null]),
            json!([42]),
            json!(["not_a_current_manual"]),
            json!(["é".repeat(65)]),
        ] {
            let mut value = envelope();
            value["task_type"] = ids;
            assert!(parse(&value).is_err());
        }
        assert!(checked_text(&"x".repeat(128), 128, "task_type id").is_ok());
        assert!(checked_text(&"x".repeat(129), 128, "task_type id").is_err());
        let mut value = envelope();
        value["task_type"] = json!(
            valid_task_type_ids()
                .into_iter()
                .take(8)
                .collect::<Vec<_>>()
        );
        assert!(parse(&value).is_ok());
    }

    #[test]
    fn requires_bounded_mode_and_session_jspace_without_mutation() {
        assert!(InitialTaskState::parse(Some(&envelope().to_string()), false).is_err());
        let state = parse(&envelope()).expect("valid state");
        for contract in [None, Some(Value::Null), Some(json!([])), Some(json!("foreign"))] {
            for fresh in [true, false] {
                let mut session = session();
                session.jspace_contract = contract.clone();
                let before = session.clone();
                assert!(state.initialize(&mut session, fresh).is_err());
                assert_eq!(session, before);
            }
        }
    }

    #[test]
    fn rejects_foreign_actual_sessions_even_on_resume_or_fallback_before_messages() {
        let state = parse(&envelope()).expect("valid state");
        for (fresh, fallback) in [(true, false), (false, false), (true, true)] {
            let mut session = session();
            session.rebind_session_id(format!("full-{}", "b".repeat(64)));
            let before = session.clone();
            assert!(
                initial_messages_with_task_state(&mut session, Some(&state), fresh, fallback)
                    .is_err()
            );
            assert_eq!(session, before);
        }
    }

    #[test]
    fn resumed_empty_initial_checkpoint_is_not_fresh_and_fallback_never_initializes() {
        let state = parse(&envelope()).expect("valid state");
        let mut resumed = session();
        let before = resumed.clone();
        assert!(
            !state
                .initialize(&mut resumed, false)
                .expect("validate resume")
        );
        assert_eq!(resumed, before);

        let mut fallback = session();
        initial_messages_with_task_state(&mut fallback, Some(&state), true, true)
            .expect("validate fallback");
        assert_eq!(fallback.session_name, "untitled");
        assert!(fallback.task_type.is_empty());
        assert!(manuals(&fallback).is_empty());
        assert!(startup_gate(&fallback));
    }

    #[test]
    fn resumes_and_repeated_initialization_preserve_evolved_state_and_manuals() {
        let state = parse(&envelope()).expect("valid state");
        let mut session = session();
        initial_messages_with_task_state(&mut session, Some(&state), true, false)
            .expect("first messages");
        session.session_name = "evolved group".to_string();
        session.replace_task_type(normalize_task_type_ids(["refactoring"]));
        let mut plan = session.task_plan.clone();
        plan.plan_summary = "evolved group".to_string();
        session.replace_task_plan(plan, Utc::now());
        append_missing_runtime_prompt_manuals(&mut session, None).expect("evolved manual");
        assert_eq!(session.session_current_turn, 0);
        for (fresh, fallback) in [(false, false), (true, true), (true, false)] {
            let before = session.clone();
            initial_messages_with_task_state(&mut session, Some(&state), fresh, fallback)
                .expect("preserve evolved session");
            assert_eq!(session, before);
        }
    }

    #[test]
    fn turn_zero_alone_cannot_overwrite_existing_or_compacted_state() {
        let state = parse(&envelope()).expect("valid state");
        for kind in 0..6 {
            let mut session = session();
            match kind {
                0 => session.session_current_turn = 1,
                1 => session.push_log(
                    json!({"role": "user", "content": "history"}).to_string(),
                    Utc::now(),
                ),
                2 => {
                    session.replace_task_type(vec!["refactoring".to_string()]);
                }
                3 => session.session_capabilities.push("task_status".to_string()),
                4 => session.session_log_retention.omitted_entries = 1,
                _ => {
                    let mut plan = session.task_plan.clone();
                    plan.plan_summary = "existing group".to_string();
                    session.replace_task_plan(plan, Utc::now());
                }
            }
            let before = session.clone();
            assert!(
                !state
                    .initialize(&mut session, true)
                    .expect("validate evolved state")
            );
            assert_eq!(session, before);
        }
    }

    #[test]
    fn retains_selected_manual_planning_reflection_and_permission_policies() {
        let state = parse(&envelope()).expect("valid state");
        for (goal, reflection, manual, no_manual) in [
            (false, false, false, false),
            (false, false, false, true),
            (false, false, true, false),
            (true, false, false, true),
            (false, true, false, true),
        ] {
            let mut session = session();
            session.goal_mode = goal;
            session.reflection_enabled = reflection;
            session.op_manual_enabled = manual;
            session.no_op_manual = no_manual;
            session.planning_enabled = true;
            let jspace = session.jspace_contract.clone();
            initial_messages_with_task_state(&mut session, Some(&state), true, false)
                .expect("respect selected policies");
            assert_eq!(!manuals(&session).is_empty(), goal || reflection || manual);
            assert_eq!(session.goal_mode, goal);
            assert_eq!(session.reflection_enabled, reflection);
            assert_eq!(session.op_manual_enabled, manual);
            assert_eq!(session.no_op_manual, no_manual);
            assert!(session.planning_enabled);
            assert!(!session.disable_permission_restrictions);
            assert_eq!(session.jspace_contract, jspace);
        }
    }

    #[test]
    fn retains_manual_session_name_while_initializing_task_group() {
        let state = parse(&envelope()).expect("valid state");
        let mut session = session();
        session.auto_session_name = false;
        assert!(state.initialize(&mut session, true).expect("initial state"));
        assert_eq!(session.session_name, "untitled");
        assert_eq!(session.task_plan.plan_summary, "storefront frontend");
        assert!(!startup_gate(&session));
    }
}
