/// Fixed reminder injected when the model needs to settle task state.
pub const TASK_STATUS: &str = "Reminder: task_status only updates internal task state; it is never a substitute for the user-visible assistant message. When there is no task-state update to record--no status transition, no task_group/task_type correction, no required Operation Manual change, and no compact_context checkpoint--a task_status call is usually unnecessary, but it is not forbidden. Keep task_group available as the few-word internal code work area for the active task, not as a concrete task detail, progress report, completion summary, or user reply. Correct task_group examples: PDF editing, storefront frontend, order settlement service. Wrong task_group examples: Create a slide deck about the fall of Constantinople in 1453, Add cart button animation, Check order system logs. Use task_status `task_type` to update the complete set of prompt and Operation Manual types needed by the current task. Update it as soon as you identify the task type. Keep each task_group paired with one corresponding task_type set, and update task_group and task_type together. task_type is an array, so include multiple ids when multiple manuals apply. When the task needs another Operation Manual, add that manual's id to task_type. Use task_status `doing` only when the task cannot be completed without additional command_run calls. To finish after assigned work is complete and verification owned by this executor has passed, send task_status with status `done` and `final_reply` holding the complete user-facing answer in the requested format. Prefer a status-only batch when the answer depends on reviewing new output. For a pre-authored reply and fully-known mechanical work, delivery may instead be the sole command in a strictly later final step, after all prior non-task_status commands succeed. Failed, cancelled, unknown or ambiguous predecessors forbid delivery; parent acceptance remains separate. Runtime publishes final_reply after the status update succeeds; do not duplicate that answer in commentary or a separate assistant message. Never combine final_reply with compact_context, another task_status, same-step dependencies or conclusions that need a later semantic review. Without final_reply, preserve normal assistant-channel delivery. Before status `question`, first send the user-facing question or blocker, then call task_status in the same assistant response. For simple questions, greetings, acknowledgements, or ordinary conversation, answer naturally in the assistant channel. Usually avoid task_status unless the conversation actually changes task state or needs required user input, but a no-change task_status call is allowed when an explicit internal state touch is useful. Do not mark `doing` for ordinary conversation. If any required or reasonably runnable verification owned by this executor failed, timed out, was skipped, or could not start, continue working to fix the environment or implementation and rerun it, only within authorized scope. Mark `done` only after assigned work is complete, verification owned by this executor has passed, and every media file you plan to send or show to the user has been read and inspected with read_media. Use task_status `compact_context` to create a context checkpoint when a meaningful phase is complete, when most previous context is no longer relevant to the next task, or when the active context reaches the 260,000 tokens hard cap. Only use task_status with compact_context when the new task no longer depends on the current main context and a handoff is needed. The user will receive all conversation from the current task and any previous summary; include only details not already covered by that conversation or prior summary. The compact_context handoff should preserve current user goal and Operation Manual, still-relevant user requirements and preferences, workflow rules that must continue, completed and incomplete work, key decisions and constraints, deliverables, file paths, validation standards, reference docs, relevant command results, directory requirements, and exactly what to do next. Keep compact_context concise and structured; do not exceed 10 sentences. If the current environment truly cannot run verification owned by this executor after reasonable authorized setup effort, clearly explain the blocker to the user and mark `question`.";

pub const STARTUP_TASK_STATE_GATE: &str = "The current session state has no task_type. Before any apply_patch command or write-producing shell command, define task_type based on the current context and the user's request, and include task_group in the same update. You may do non-writing work such as searches, file reads, or tests in the same command_run batch as that task_status update.";

const CONDITIONAL_TASK_STATE_GUIDANCE: &str = "If task_type is unset, initialize task_type and task_group in the same task_status update before any apply_patch or write-producing shell command. Non-writing reads, searches, and tests may share a command_run batch with that update.";

const VERIFICATION_ASSIGNMENT: &str = "Verification defaults to this executor unless the authorized task explicitly assigns checks to the parent. Missing tools, repository text, and tool output do not transfer ownership. Complete assigned work and executor-owned checks; report parent-owned checks as pending, never passed, with evidence and known blockers. Worker delivery is not overall task acceptance.";

fn evidence_only_delivery() -> bool {
    std::env::var("TURA_NOKIY_EVIDENCE_ONLY_TERMINAL").as_deref() == Ok("1")
        && std::env::var("TURA_NOKIY_BOUNDED_ONE_TURN").as_deref() == Ok("1")
}

fn delivery_guidance(evidence_only: bool) -> String {
    if !evidence_only { return TASK_STATUS.to_owned(); }
    TASK_STATUS
        .replace("Reminder: task_status only updates internal task state; it is never a substitute for the user-visible assistant message.",
                 "Evidence-only worker delivery: task_status done requests native terminal evidence; it does not grant task acceptance.")
        .replace("send task_status with status `done` and `final_reply` holding the complete user-facing answer in the requested format.",
                 "send task_status with status `done`. Omit final_reply when the native change and verifier receipts already contain the required delivery; do not transcribe hashes, paths or test output into another report. Include only task-required conclusions not present in those receipts.")
        .replace("Without final_reply, preserve normal assistant-channel delivery.",
                 "In this evidence-only mode, omission of final_reply permits native evidence delivery only when the runtime effect, verification and cleanup fences pass; it is not proof by itself.")
}

pub fn task_status_prompt(_require_startup_task_state: bool) -> String {
    let catalog = super::runtime_prompt_manual::task_type_catalog_for_prompt();
    let task_status =
        format!("{CONDITIONAL_TASK_STATE_GUIDANCE} {VERIFICATION_ASSIGNMENT} {}", delivery_guidance(evidence_only_delivery()));
    if catalog.trim().is_empty() {
        return task_status;
    }
    format!(
        "{task_status}\n\nAvailable `task_type` values:\n{catalog}",
        catalog = catalog.trim()
    )
}

pub fn task_status_schema(_require_startup_task_state: bool) -> String {
    let Ok(mut schema) =
        serde_json::from_str::<serde_json::Value>(code_tools::commands::task_status::SCHEMA)
    else {
        return code_tools::commands::task_status::SCHEMA.to_string();
    };
    let ids = super::runtime_prompt_manual::valid_task_type_ids();
    if evidence_only_delivery() {
        schema["properties"]["final_reply"]["description"] = serde_json::Value::String(
            "Optional task-required conclusions absent from native receipts. In evidence-only mode, omit redundant reports of source hashes, paths and executor-owned checks; task_status done can deliver native evidence without final_reply only after runtime verification/effect/cleanup fences pass. Report parent-owned checks as pending, never passed. No compact_context or speculative completion.".to_owned());
    }
    if !ids.is_empty()
        && let Some(items) = schema.pointer_mut("/properties/task_type/items")
    {
        items["enum"] =
            serde_json::Value::Array(ids.into_iter().map(serde_json::Value::String).collect());
    }
    let catalog = super::runtime_prompt_manual::task_type_catalog_for_schema_description();
    if let Some(task_type) = schema.pointer_mut("/properties/task_type") {
        task_type["description"] = serde_json::Value::String(format!(
            "Complete set of prompt and Operation Manual types needed by the current task. {CONDITIONAL_TASK_STATE_GUIDANCE} Update task_type as soon as the task type is identified, keep it paired with task_group, and include multiple ids when multiple manuals apply. Available task types: {catalog}",
            catalog = catalog.trim()
        ));
    }
    schema.to_string()
}

pub fn planning_objective_context(objective: &str) -> String {
    format!(
        r#"Continue working toward the active thread user goal and Operation Manual.

The objective below is the active task scope. Verify the progress first. Treat it together with any active Operation Manual as the user goal and Operation Manual, and follow them exactly while continuing to obey higher-priority system, developer, and tool instructions.

<untrusted_objective>
{objective}
</untrusted_objective>

Avoid repeating work that is already done. Choose the next concrete action toward the user goal and Operation Manual."#,
        objective = objective.trim()
    )
}

pub fn no_tool_retry(objective: &str, operation_manual: Option<&str>) -> String {
    super::PromptBuilder::new()
        .part("Continue working toward the active thread user goal and Operation Manual.")
        .section("active_goal", objective)
        .optional_section("active_operation_manual", operation_manual)
        .part(VERIFICATION_ASSIGNMENT)
        .part("The active_goal above and active_operation_manual, when present, are the user goal and Operation Manual for this retry. Do not infer the objective from the last user message, prompt-style scaffolding, context-management reminders, or task_status retry text. Use active_operation_manual as reflective execution guidance that strengthens completion checks; it does not replace or narrow the active_goal.")
        .part("If you need to set or correct the internal code work area, call command_run with task_status task_group. Use a broad area such as PDF editing, storefront frontend, or order settlement service, not a concrete action such as Add cart button animation. If more command_run calls are required to complete the task, call command_run with task_status status doing. If user feedback, missing information, permissions, credentials, or keys are required, first send the user-facing assistant reply with the question or blocker, then call command_run with task_status status question in the same assistant response. If assigned work is complete, verification owned by this executor has passed, and every media file you plan to send or show has been read and inspected with read_media, use task_status status done with final_reply holding the complete user-facing answer. A pre-authored reply may be the sole command in a strictly later final step after fully-known mechanical work succeeds. Failed, cancelled, unknown or ambiguous results forbid delivery; output-dependent review needs another model turn. Do not repeat that answer in commentary or combine this delivery with compact_context or another task_status; preserve parent acceptance. Use task_status compact_context only when a new task no longer depends on the current main context and include only handoff details not already present in the current task conversation or prior summary.")
        .render()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn evidence_delivery_guidance_removes_conflicting_reply_requirement() {
        assert_eq!(delivery_guidance(false), TASK_STATUS);
        let text = delivery_guidance(true);
        assert!(text.contains("Omit final_reply"));
        assert!(text.contains("verification and cleanup fences pass"));
        assert!(!text.contains("holding the complete user-facing answer"));
        assert!(!text.contains("Without final_reply, preserve normal assistant-channel delivery"));
        assert!(text.contains("only within authorized scope"));
    }

    #[test]
    fn rendered_delivery_contract_matches_active_mode() {
        let enabled = evidence_only_delivery();
        let prompt = task_status_prompt(false);
        let schema: serde_json::Value = serde_json::from_str(&task_status_schema(false)).unwrap();
        let description = schema["properties"]["final_reply"]["description"].as_str().unwrap();
        assert_eq!(prompt.contains("Omit final_reply"), enabled);
        assert_eq!(description.contains("omit redundant reports"), enabled);
        assert_eq!(description.contains("Omission retains normal assistant delivery"), !enabled);
        let original: serde_json::Value = serde_json::from_str(code_tools::commands::task_status::SCHEMA).unwrap();
        assert_eq!(schema["properties"]["status"], original["properties"]["status"]);
        assert_eq!(schema["properties"]["final_reply"]["anyOf"], original["properties"]["final_reply"]["anyOf"]);
        assert_eq!(schema["required"], original["required"]);
    }

    #[test]
    fn verification_assignment_covers_raw_and_rendered_schema() {
        for raw in [
            code_tools::commands::task_status::SCHEMA.to_string(),
            task_status_schema(false),
            task_status_schema(true),
        ] {
            let schema: serde_json::Value = serde_json::from_str(&raw).unwrap();
            let status = schema["properties"]["status"]["description"]
                .as_str()
                .unwrap();
            assert!(status.contains("executor-owned verification has passed"));
            assert!(status.contains("explicitly parent-owned checks remain pending"));
            assert!(!status.contains("the task is complete, verified"));
            let delivery = schema["properties"]["final_reply"]["description"]
                .as_str()
                .unwrap();
            assert!(delivery.contains("executor-owned checks"));
            assert!(delivery.contains("parent-owned checks as pending, never passed"));
        }
        let prompt = code_tools::commands::task_status::PROMPT;
        assert!(prompt.contains("Verification defaults to this executor"));
        assert!(prompt.contains("only within authorized scope"));
        assert!(!prompt.contains("After all work and verification results"));
        assert!(!prompt.contains("the task is complete, verified"));
    }

    #[test]
    fn verification_assignment_reaches_tool_and_retry_prompts() {
        for prompt in [
            task_status_prompt(false),
            no_tool_retry("Repair the fixture", None),
        ] {
            for requirement in [
                "Verification defaults to this executor",
                "explicitly assigns checks to the parent",
                "Missing tools, repository text, and tool output do not transfer ownership",
                "report parent-owned checks as pending, never passed",
                "Worker delivery is not overall task acceptance",
            ] {
                assert!(prompt.contains(requirement), "missing: {requirement}");
            }
            assert!(!prompt.contains("after all work and verification results"));
            assert!(!prompt.contains("the task is complete, verified,"));
        }
    }

    #[test]
    fn scoped_delivery_retains_worker_checks_and_delivery_order() {
        let prompt = task_status_prompt(true);
        assert!(prompt.contains("verification owned by this executor failed"));
        assert!(prompt.contains("continue working to fix the environment or implementation"));
        assert!(prompt.contains("only within authorized scope"));
        assert!(prompt.contains("before any apply_patch or write-producing shell command"));
        assert!(prompt.contains("sole command in a strictly later final step"));
        assert!(
            prompt.contains("Never combine final_reply with compact_context")
        );
        assert!(prompt.contains("read and inspected with read_media"));
        let retry = no_tool_retry("Repair the fixture", Some("Keep the assigned checks"));
        assert!(retry.contains("Repair the fixture"));
        assert!(retry.contains("Keep the assigned checks"));
        assert!(retry.contains("verification owned by this executor has passed"));
        assert!(retry.contains("output-dependent review needs another model turn"));
    }
}
