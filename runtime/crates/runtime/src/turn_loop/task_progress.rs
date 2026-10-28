//! Task-progress helpers for the turn loop.

use chrono::Utc;

use crate::prompt_style::context_blocks;
use lifecycle::{SessionManagement, TaskStatus};
use lifecycle::{StartCondition, TaskStep};

const TASK_STATUS_COMMAND: &str = "task_status";

pub(crate) fn command_run_result_terminal_task_status(
    result: &serde_json::Value,
) -> Option<String> {
    command_run_result_items(result)
        .into_iter()
        .find_map(command_run_item_terminal_task_status)
}

fn command_run_item_terminal_task_status(item: &serde_json::Value) -> Option<String> {
    if command_result_type(item).as_deref() != Some(TASK_STATUS_COMMAND) {
        return None;
    }
    if item.get("success").and_then(serde_json::Value::as_bool) != Some(true) {
        return None;
    }
    item.get("output")
        .and_then(|output| {
            output
                .get(TASK_STATUS_COMMAND)
                .or_else(|| output.get("status"))
        })
        .and_then(|status| status.get("status"))
        .and_then(serde_json::Value::as_str)
        .filter(|status| matches!(*status, "doing" | "done" | "question"))
        .map(ToString::to_string)
}

pub(crate) fn command_run_result_is_single_task_status(
    result: &serde_json::Value,
    status: &str,
) -> bool {
    let items = command_run_result_items(result);
    let [item] = items.as_slice() else {
        return false;
    };
    command_run_item_terminal_task_status(item).as_deref() == Some(status)
}

pub(crate) fn command_run_result_final_reply(result: &serde_json::Value) -> Option<(&str, bool)> {
    let body = result.get("streamed_command_run_result").unwrap_or(result);
    if !terminal_delivery_envelope_ok(result) || !terminal_delivery_envelope_ok(body) {
        return None;
    }
    let raw = body.get("results")?.as_array()?;
    let items = match raw.as_slice() {
        [batch] if batch.get("mode").and_then(serde_json::Value::as_str) == Some("batch") => {
            if !terminal_delivery_envelope_ok(batch) {
                return None;
            }
            let nested = batch.get("results")?.as_array()?;
            if nested.len() != 1 {
                return None;
            }
            nested.as_slice()
        }
        rows => rows,
    };
    // Classify raw containers before flattening can discard their failure flags.
    if items.iter().any(|row| row.get("mode").is_some()) {
        return None;
    }
    let (item, prior) = items.split_last()?;
    if !terminal_delivery_envelope_ok(item) {
        return None;
    }
    if !prior.is_empty() {
        let mut guard = code_tools::command_run::CommandRunTerminalStatusGuard::default();
        let mut previous_step = 0;
        for row in prior {
            if !terminal_delivery_envelope_ok(row)
                || command_result_type(row).is_none_or(|kind| kind == TASK_STATUS_COMMAND)
            {
                return None;
            }
            let step = row.get("step").and_then(serde_json::Value::as_u64)?;
            if step == 0 || step < previous_step {
                return None;
            }
            previous_step = step;
            guard.observe_step(Some(step));
            guard.observe_result(row.get("success").and_then(serde_json::Value::as_bool));
        }
        if guard.done_error(item.get("step").and_then(serde_json::Value::as_u64), true).is_some() {
            return None;
        }
    }
    if !matches!(command_run_item_terminal_task_status(item).as_deref(), Some("done" | "question")) {
        return None;
    }
    let status = item.pointer("/output/task_status")?;
    if status.get("compact_context").is_some() {
        return None;
    }
    let reply = status.get("final_reply")?.as_str()?;
    let structured = match status.get("final_reply_format") {
        None => false,
        Some(value) if value.as_str() == Some("json") => true,
        Some(_) => return None,
    };
    if structured {
        let value = serde_json::from_str::<serde_json::Value>(reply).ok()?;
        if value.as_object().is_none_or(|object| object.is_empty())
            && value.as_array().is_none_or(|array| array.is_empty())
        {
            return None;
        }
    }
    Some((reply, structured))
}

fn terminal_delivery_envelope_ok(value: &serde_json::Value) -> bool {
    value.get("cancelled").is_none_or(|flag| flag.as_bool() == Some(false))
        && value.get("cancel_reason").is_none_or(serde_json::Value::is_null)
        && value.get("error").is_none_or(serde_json::Value::is_null)
        && value.get("failure").is_none_or(serde_json::Value::is_null)
        && value.get("early_finish_reason").is_none_or(serde_json::Value::is_null)
        && value.get("success").is_none_or(|flag| flag.as_bool() == Some(true))
}

pub(crate) fn command_run_result_has_command(result: &serde_json::Value) -> bool {
    command_run_result_items(result).into_iter().any(|item| {
        command_result_type(item).is_some_and(|command_type| command_type != TASK_STATUS_COMMAND)
    })
}

fn command_run_result_items(result: &serde_json::Value) -> Vec<&serde_json::Value> {
    let result = result.get("streamed_command_run_result").unwrap_or(result);
    let Some(results) = result.get("results").and_then(serde_json::Value::as_array) else {
        return Vec::new();
    };
    let mut items = Vec::new();
    for item in results {
        if item.get("mode").and_then(serde_json::Value::as_str) == Some("batch")
            && let Some(batch_results) = item.get("results").and_then(serde_json::Value::as_array)
        {
            items.extend(batch_results);
            continue;
        }
        items.push(item);
    }
    items
}

fn command_result_type(item: &serde_json::Value) -> Option<String> {
    item.get("command_type")
        .or_else(|| item.get("command"))
        .and_then(serde_json::Value::as_str)
        .map(|value| value.trim().to_ascii_lowercase().replace('-', "_"))
        .filter(|value| !value.is_empty())
}

pub(crate) fn active_task_user_message(session: &SessionManagement) -> Option<serde_json::Value> {
    task_user_message_by(session, task_is_executable)
}

#[cfg(test)]
pub(crate) fn active_todo_task_user_message(
    session: &SessionManagement,
) -> Option<serde_json::Value> {
    task_user_message_by(session, task_is_user_action_todo)
}

pub(crate) fn active_doing_task_user_message(
    session: &SessionManagement,
) -> Option<serde_json::Value> {
    task_user_message_by(session, task_is_doing)
}

fn task_user_message_by(
    session: &SessionManagement,
    predicate: fn(&TaskStep) -> bool,
) -> Option<serde_json::Value> {
    let (_index, task) = session
        .task_plan
        .detailed_tasks
        .iter()
        .enumerate()
        .find(|(_, task)| predicate(task))?;
    let current_task = context_blocks::current_task_text(&task.task_summary);
    Some(serde_json::json!({
        "role": "user",
        "content": context_blocks::current_objective_block(
            &session.current_objective,
            Some(current_task),
        )
    }))
}

pub(crate) fn record_task_focus_message(
    session: &mut SessionManagement,
    message: &serde_json::Value,
) {
    record_task_focus_message_for_terminal_done(session, message, false);
}

pub(crate) fn record_task_focus_message_for_terminal_done(
    session: &mut SessionManagement,
    message: &serde_json::Value,
    only_todo: bool,
) {
    let Some(task) = session.task_plan.detailed_tasks.iter().find(|task| {
        if only_todo {
            task_is_user_action_todo(task)
        } else {
            task_is_executable(task)
        }
    }) else {
        return;
    };
    let task_id = task.task_id.as_str();
    if session.session_log.iter().rev().any(|entry| {
        let value = entry.value();
        (value.get("type").and_then(|kind| kind.as_str()) == Some("task_focus"))
            .then(|| {
                value
                    .get("task_id")
                    .and_then(serde_json::Value::as_str)
                    .map(|seen| seen == task_id)
            })
            .flatten()
            .unwrap_or(false)
    }) {
        return;
    }
    let now = Utc::now();
    session.push_log(
        serde_json::json!({
            "type": "task_focus",
            "task_id": task.task_id,
            "step": task.step,
            "task_summary": task.task_summary,
            "deliverable": task.step_deliverable_description,
            "content": message.get("content").cloned().unwrap_or(serde_json::Value::Null),
            "timestamp": now.to_rfc3339(),
        })
        .to_string(),
        now,
    );
}

fn task_is_executable(task: &TaskStep) -> bool {
    task.status == TaskStatus::Doing
        || (task.status == TaskStatus::Todo && task.start_condition == StartCondition::UserAction)
}

fn task_is_doing(task: &TaskStep) -> bool {
    task.status == TaskStatus::Doing
}

fn task_is_user_action_todo(task: &TaskStep) -> bool {
    task.status == TaskStatus::Todo && task.start_condition == StartCondition::UserAction
}

#[cfg(test)]
mod tests {
    use crate::context::build_messages_from_session;
    use chrono::Utc;
    use lifecycle::{PlanStatus, SessionInput, SessionManagement, StartCondition, TaskStep};
    use serde_json::json;
    use std::path::PathBuf;

    fn ordered_reply() -> serde_json::Value {
        json!({"results":[
            {"command_type":"apply_patch", "step":1, "success":true, "output":{}},
            {"command_type":"source_read", "step":2, "success":true, "output":{}},
            {"command_type":"task_status", "step":3, "success":true,
             "output":{"task_status":{"status":"done", "final_reply":"Parent acceptance pending."}}}
        ]})
    }

    #[test]
    fn ordered_terminal_reply_accepts_successful_steps_and_streamed_envelope() {
        let input = ordered_reply();
        for result in [input.clone(), json!({"streamed_command_run_result":input.clone()})] {
            assert_eq!(super::command_run_result_final_reply(&result),
                       Some(("Parent acceptance pending.", false)));
        }
        let mut independent = input;
        independent["results"][1]["step"] = json!(1);
        assert!(super::command_run_result_final_reply(&independent).is_some());
    }

    #[test]
    fn ordered_terminal_reply_rejects_failed_unknown_unordered_and_nested_results() {
        let original = ordered_reply();
        for flag in [json!(false), json!(null), json!("true")] {
            let mut result = original.clone();
            result["results"][0]["success"] = flag;
            assert!(super::command_run_result_final_reply(&result).is_none());
        }
        for path in ["success", "step"] {
            let mut result = original.clone();
            result["results"][0].as_object_mut().unwrap().remove(path);
            assert!(super::command_run_result_final_reply(&result).is_none());
        }
        for index in 0..3 {
            for step in [json!(0), json!("1"), json!(-1), json!(null)] {
                let mut result = original.clone();
                result["results"][index]["step"] = step;
                assert!(super::command_run_result_final_reply(&result).is_none());
            }
        }
        for step in [1, 2] {
            let mut result = original.clone();
            result["results"][2]["step"] = json!(step);
            assert!(super::command_run_result_final_reply(&result).is_none());
        }
        let mut reversed = original.clone();
        reversed["results"][0]["step"] = json!(2);
        reversed["results"][1]["step"] = json!(1);
        assert!(super::command_run_result_final_reply(&reversed).is_none());
        for status in ["doing", "question", "done"] {
            let mut result = original.clone();
            result["results"][0] = json!({"command_type":"task_status", "step":1,
                "success":true, "output":{"task_status":{"status":status}}});
            assert!(super::command_run_result_final_reply(&result).is_none());
        }
        for field in ["error", "cancel_reason", "early_finish_reason", "failure"] {
            let mut result = original.clone();
            result["results"][0][field] = json!("uncertain");
            assert!(super::command_run_result_final_reply(&result).is_none());
        }
        let nested = json!({"results":[{"mode":"batch", "cancelled":true,
            "results":original["results"]}]});
        assert!(super::command_run_result_final_reply(&nested).is_none());
        let mut not_last = original;
        not_last["results"].as_array_mut().unwrap().swap(1, 2);
        assert!(super::command_run_result_final_reply(&not_last).is_none());
    }

    #[test]
    fn terminal_reply_never_ignores_outer_or_streamed_failure_flags() {
        for flag in [json!(true), json!(null), json!("false")] {
            for streamed in [false, true] {
                let mut result = if streamed {
                    json!({"streamed_command_run_result":ordered_reply()})
                } else { ordered_reply() };
                result["cancelled"] = flag.clone();
                assert!(super::command_run_result_final_reply(&result).is_none());
            }
        }
        for field in ["error", "cancel_reason", "early_finish_reason", "failure"] {
            let mut body = ordered_reply();
            body[field] = json!("uncertain");
            assert!(super::command_run_result_final_reply(&body).is_none());
            assert!(super::command_run_result_final_reply(
                &json!({"streamed_command_run_result":body})).is_none());
            let mut outer = json!({"streamed_command_run_result":ordered_reply()});
            outer[field] = json!("uncertain");
            assert!(super::command_run_result_final_reply(&outer).is_none());
        }
        let single = json!({"cancelled":true, "results":[ordered_reply()["results"][2]]});
        assert!(super::command_run_result_final_reply(&single).is_none());
        let done = ordered_reply()["results"][2].clone();
        for raw in [
            json!({"results":[{"mode":"batch", "cancelled":true, "results":[]}, done.clone()]}),
            json!({"results":[{"mode":"batch", "cancelled":true, "results":[done.clone()]}]}),
            json!({"results":[{"mode":"batch", "error":"unknown", "results":[]}, done.clone()]}),
            json!({"results":[{"mode":"batch", "results":[]}, done.clone()]}),
        ] {
            assert!(super::command_run_result_final_reply(&raw).is_none());
            assert!(super::command_run_result_final_reply(
                &json!({"streamed_command_run_result":raw})).is_none());
        }
        let legacy = json!({"results":[{"mode":"batch", "results":[done]}]});
        for result in [legacy.clone(), json!({"streamed_command_run_result":legacy.clone()})] {
            assert_eq!(super::command_run_result_final_reply(&result),
                Some(("Parent acceptance pending.", false)));
        }
        for (field, value) in [
            ("cancelled", json!(true)),
            ("success", json!(false)),
            ("error", json!("unknown")),
            ("failure", json!("unknown")),
            ("cancel_reason", json!("unknown")),
            ("early_finish_reason", json!("unknown")),
        ] {
            let mut result = legacy.clone();
            result["results"][0][field] = value;
            assert!(super::command_run_result_final_reply(&result).is_none());
            assert!(super::command_run_result_final_reply(
                &json!({"streamed_command_run_result":result})).is_none());
        }
    }

    #[test]
    fn task_status_detection_accepts_streamed_command_run_results() {
        let result = json!({
            "streamed_command_run_result": {
                "results": [{
                    "command_type": "task_status",
                    "success": true,
                    "output": {
                        "task_status": {
                            "status": "done",
                            "task_group": "finished"
                        }
                    }
                }]
            }
        });

        assert_eq!(
            super::command_run_result_terminal_task_status(&result).as_deref(),
            Some("done")
        );

        let question = json!({
            "results": [{
                "command_type": "task_status",
                "success": true,
                "output": {
                    "task_status": {
                        "status": "question",
                        "content": "Need API key."
                    }
                }
            }]
        });
        assert_eq!(
            super::command_run_result_terminal_task_status(&question).as_deref(),
            Some("question")
        );

        let doing = json!({
            "results": [{
                "command_type": "task_status",
                "success": true,
                "output": {
                    "task_status": {
                        "status": "doing"
                    }
                }
            }]
        });
        assert_eq!(
            super::command_run_result_terminal_task_status(&doing).as_deref(),
            Some("doing")
        );

        let unsuccessful = json!({
            "results": [{
                "command_type": "task_status",
                "success": false,
                "output": { "task_status": { "status": "done" } }
            }]
        });
        assert_eq!(
            super::command_run_result_terminal_task_status(&unsuccessful),
            None
        );

        let missing_success = json!({
            "results": [{
                "command_type": "task_status",
                "output": { "task_status": { "status": "done" } }
            }]
        });
        assert_eq!(
            super::command_run_result_terminal_task_status(&missing_success),
            None
        );
    }

    #[test]
    fn command_run_result_detects_non_task_status_command_before_terminal_status() {
        let result = json!({
            "streamed_command_run_result": {
                "results": [
                    { "command_type": "shell_command", "success": true, "output": "ok" },
                    {
                        "command_type": "task_status",
                        "success": true,
                        "output": { "task_status": { "status": "done" } }
                    }
                ]
            }
        });
        assert!(super::command_run_result_has_command(&result));

        let status_only = json!({
            "results": [{
                "command_type": "task_status",
                "success": true,
                "output": { "task_status": { "status": "done" } }
            }]
        });
        assert!(!super::command_run_result_has_command(&status_only));
        assert!(super::command_run_result_is_single_task_status(
            &status_only,
            "done"
        ));

        let batched_status_only = json!({
            "results": [{
                "mode": "batch",
                "results": [{
                    "command_type": "task_status",
                    "success": true,
                    "output": { "status": { "status": "done" } }
                }]
            }]
        });
        assert!(!super::command_run_result_has_command(&batched_status_only));
        assert!(super::command_run_result_is_single_task_status(
            &batched_status_only,
            "done"
        ));
    }

    fn session_with_tasks(tasks: Vec<TaskStep>) -> SessionManagement {
        let now = Utc::now();
        let mut session = SessionManagement::new(
            "sess-executable-task".to_string(),
            "task routing".to_string(),
            PathBuf::from("C:/workspace"),
            false,
            "coding".to_string(),
            SessionInput {
                user_input: "finish queued work".to_string(),
                file_input: vec![],
                agent: None,
                runtime_context: None,
                planning_mode_override: None,
            },
            "finish queued work".to_string(),
            now,
        );
        let mut task_plan = session.task_plan.clone();
        task_plan.detailed_tasks = tasks;
        session.replace_task_plan(task_plan, now);
        session
    }

    fn task(step: u64, status: PlanStatus, start_condition: StartCondition) -> TaskStep {
        TaskStep {
            task_id: format!("task-{step}"),
            step,
            task_summary: format!("Task {step}"),
            step_deliverable_description: format!("Deliverable {step}"),
            status,
            start_condition,
            ..TaskStep::default()
        }
    }

    #[test]
    fn terminal_task_status_continues_when_gateway_added_task_is_executable() {
        let session = session_with_tasks(vec![
            task(1, PlanStatus::Done, StartCondition::UserAction),
            task(2, PlanStatus::Todo, StartCondition::UserAction),
        ]);

        let message =
            super::active_todo_task_user_message(&session).expect("todo task is executable");
        assert!(
            message["content"]
                .as_str()
                .expect("message content")
                .contains("Task 2")
        );
    }

    #[test]
    fn terminal_task_status_done_only_continues_for_todo_user_action_task() {
        let session = session_with_tasks(vec![
            task(1, PlanStatus::Done, StartCondition::UserAction),
            task(2, PlanStatus::Doing, StartCondition::UserAction),
        ]);

        assert!(super::active_todo_task_user_message(&session).is_none());
    }

    #[test]
    fn terminal_task_status_done_focuses_nearest_todo_not_existing_doing() {
        let mut session = session_with_tasks(vec![
            task(1, PlanStatus::Done, StartCondition::UserAction),
            task(2, PlanStatus::Doing, StartCondition::UserAction),
            task(3, PlanStatus::Todo, StartCondition::UserAction),
        ]);
        session.current_objective = "Original user objective".to_string();

        let message =
            super::active_todo_task_user_message(&session).expect("todo task should be selected");
        super::record_task_focus_message_for_terminal_done(&mut session, &message, true);

        let content = message["content"].as_str().expect("message content");
        assert!(content.contains("[current objective]:\nOriginal user objective"));
        assert!(!content.contains("[current task]:"));
        assert!(content.ends_with("\n\nTask 3"));
        assert_eq!(session.current_objective, "Original user objective");
        let focus_event = session
            .session_log
            .iter()
            .filter_map(|entry| serde_json::from_str::<serde_json::Value>(entry).ok())
            .find(|value| {
                value.get("type").and_then(serde_json::Value::as_str) == Some("task_focus")
            })
            .expect("task focus should be recorded");
        assert_eq!(focus_event["task_id"], "task-3");
    }

    #[test]
    fn task_focus_is_audited_without_entering_model_context() {
        let mut session = session_with_tasks(vec![
            task(1, PlanStatus::Done, StartCondition::UserAction),
            task(2, PlanStatus::Todo, StartCondition::UserAction),
        ]);
        let message = super::active_task_user_message(&session).expect("todo task is executable");

        super::record_task_focus_message(&mut session, &message);
        super::record_task_focus_message(&mut session, &message);

        let focus_events = session
            .session_log
            .iter()
            .filter_map(|entry| serde_json::from_str::<serde_json::Value>(entry).ok())
            .filter(|value| {
                value.get("type").and_then(serde_json::Value::as_str) == Some("task_focus")
            })
            .collect::<Vec<_>>();
        assert_eq!(focus_events.len(), 1);
        assert_eq!(focus_events[0]["task_id"], "task-2");
        let context_messages = build_messages_from_session(&session);
        assert!(!context_messages.iter().any(|value| {
            value
                .get("content")
                .map(|content| content.to_string().contains("[current objective]"))
                .unwrap_or(false)
        }));
    }

    #[test]
    fn terminal_task_status_ends_when_only_scheduled_or_completed_tasks_remain() {
        let session = session_with_tasks(vec![
            task(1, PlanStatus::Done, StartCondition::UserAction),
            task(2, PlanStatus::Todo, StartCondition::ScheduledTask),
        ]);

        assert!(super::active_todo_task_user_message(&session).is_none());
        assert!(super::active_task_user_message(&session).is_none());
    }
}
