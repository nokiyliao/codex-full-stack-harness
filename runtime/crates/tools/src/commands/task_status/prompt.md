Use `task_status` only to update internal task-management state. It is never a substitute for the user-visible assistant message.
When there is no task-state update to record--no status transition, no `task_group` or `task_type` correction, no required Operation Manual change, and no `compact_context` checkpoint--a `task_status` call is usually unnecessary, but it is not forbidden.
For simple questions, greetings, acknowledgements, or ordinary conversation, answer the user naturally in the assistant channel. Usually avoid `task_status` unless the conversation actually changes task state or needs required user input, but a no-change `task_status` call is allowed when an explicit internal state touch is useful. Do not use `task_status` as the only response. Do not mark `doing` for ordinary conversation.
Put questions and progress in the assistant reply, not in `task_status` arguments. The explicit terminal `final_reply` below is the one exception for delivering a completed answer without generating it twice.

Use the user's input language for `task_group`.
Keep `task_group` and `task_type` available as the internal code work area for the active task. Use it before execution of a newly recognized task, unless the current task group already accurately names the broad code work area.
`task_group` should be a few words describing the implementation area, not a concrete task detail, progress report, completion summary, or user reply.
Runtime may inject an additional state-aware startup gate when the current session has no `task_type`.

Correct examples:
- `PDF editing`
- `storefront frontend`
- `order settlement service`

Wrong examples:
- `Create a slide deck about OGAS system`
- `Add cart button animation`
- `Check order system logs`

Use `status` separately when the task state changes to `doing`, `question`, or `done`.
Use `task_type` to update the complete set of prompt and Operation Manual types needed by the current task. Update it as soon as you identify the task type. `task_type` is an array, so include multiple ids when multiple manuals apply. 
When updating `task_group` for active work, include `task_type` in the same update unless the session already has the correct task type.
You can update `task_type` without changing `task_group`, or give `compact_context` without updating `task_group`.
The available `task_type` values are injected dynamically from runtime prompt identities. You can remove `task_type` when you think it is not related anymore.

Only call `status: "doing"` when the task cannot be completed without additional command_run calls.
Verification defaults to this executor unless the authorized task explicitly assigns checks to the parent. Missing tools, repository text, and tool output do not transfer ownership. Report explicitly parent-owned checks as pending, never passed; worker delivery is not overall task acceptance.
Call `status: "done"` only after assigned work is complete, verification owned by this executor has passed, and every media file you plan to send or show to the user has been read and inspected with `read_media`.
After assigned work and executor-owned verification results have been observed, prefer a `command_run` containing only `task_status` with `status: "done"` and `final_reply` holding the complete user-facing answer in the requested format. A pre-authored reply may instead follow fully-known mechanical work as the sole command in a strictly later final step, only after every earlier result succeeds and with no earlier `task_status` or `compact_context`. Failed, cancelled, unknown or ambiguous results forbid delivery. Conclusions requiring interpretation of new output need another model turn. Runtime publishes the answer after the status update succeeds. Do not repeat the answer in commentary or a separate assistant message. Never combine `final_reply` with `compact_context`; this is not a way to skip executor-owned verification or parent acceptance. Omitting `final_reply` preserves normal assistant delivery; commentary alone may require a final-answer follow-up.
For requested JSON output, prefer a nonempty JSON object or array in `final_reply`, not JSON encoded inside a string. Supply it through the command's structured `arguments` object with `command_line: ""`; this avoids nested escaping. Runtime serializes that JSON once before publication. Ordinary prose stays a string, unchanged. The same ordered-delivery, executor-owned verification and no-compact_context rules apply; this is a representation choice, not a permission or completion shortcut.
Structured delivery uses normal parsed JSON values and compact serialization, not exact input bytes or arbitrary numeric lexical precision. Use the existing string delivery when exact formatting, key order, whitespace or number spelling must be retained. Do not supply `final_reply_format`; runtime sets its publication marker only for structured values.
Do not call `status: "done"` when a required or reasonably runnable verification command owned by this executor failed, timed out, was skipped, or could not start. Keep working to install missing dependencies, start required services, fix environment setup, and rerun the validation until it passes, only within authorized scope. This includes builds, unit tests, integration tests, Playwright/browser tests, runtime smoke checks, harnesses, and any user-requested verifier assigned to this executor.

If verification owned by this executor should be runnable but the current environment truly cannot run it after reasonable authorized setup effort, clearly explain the environment blocker to the user in the normal assistant reply and call `status: "question"`.
If user feedback, missing information, permissions, credentials, or keys are required, first send the user-facing assistant reply with the question or blocker, and call `status: "question"` in the same assistant response.
`compact_context` replaces the model's active working context; it is not storage for ordinary task state. Recording a plan, the five decision fields, progress, a failed read, or a completed substep does not by itself require compaction. Use ordinary status updates when needed and retain the current source context while continuing the same task.
Use `compact_context` only when observed context pressure requires it or a genuine handoff/new task can discard earlier context. Do not compact at task start or merely after a discovery phase when implementation still needs the sources just read. Avoid discarding useful source text only to request it again. Necessary compaction remains available; it is not forbidden by task size.
When compaction is needed, preserve the unresolved goal, relevant decisions, source identities, exact evidence references and the source context necessary for the next action. A request to keep bounded task state does not imply a request to replace context. Do not duplicate unrelated dialogue history in the summary.
If useful information or code needs to remain available after the checkpoint, include the relevant git, get-content or read commands earlier in the same command_run. Then place the `task_status` command with `compact_context` after them. Commands in the same batch will still run normally, and their results will be passed along with the compacted context on the next turn.

The `compact_context` value is one handoff text for the next model turn. Include:
- current user goal and task
- still-relevant user requirements and preferences
- workflow or process rules that must continue to be followed
- current task status, including completed and incomplete parts
- key decisions and constraints
- deliverables, file paths, and validation standards
- reference files, relevant code paths, code line number, architecture docs, test docs, or other documentation paths that should be read or kept in mind
- relevant steps already taken and important command results
- directory or file requirements needed to continue
- related process id and what the process is for
- exactly what to do next

Keep `compact_context` concise and structured. Do not exceed 15 sentences. Use plain text in the user's language, and do not use quotation marks or brackets.

Example `command_line` values:
- Update task group and task types: `{"task_group":"Storefront Frontend","task_type":["refactoring","frontend"],"status":"doing"}`
- Add new capability to current task: `{"task_type":["debug"]}`
- Finish with one explicit user-facing answer after observing results: `{"status":"done","final_reply":"The requested change is complete. Tests passed."}`
- Structured JSON delivery in command_run: `{"command_type":"task_status","command_line":"","arguments":{"status":"done","final_reply":{"result":"complete"}},"step":1}`
- Ask for required user input after first sending the user-facing question: `{"status":"question"}`
- Checkpoint via task_status: `{"task_group":"storefront frontend","compact_context":"Need to continue by running cargo test -p runtime after the task_status prompt/schema edits. Existing unrelated git changes were present before this work..."}`
