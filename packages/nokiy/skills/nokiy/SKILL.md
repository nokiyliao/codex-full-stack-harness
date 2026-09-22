---
name: nokiy
description: Execute a bounded task through the existing installed Nokiy full-stack caller when the user invokes $nokiy or requests Nokiy execution; not a replacement for ordinary Codex coding work.
---

# Nokiy

Use the installed caller, not a new orchestrator. The original Codex retains mission ownership, CAS/lease, permissions, final review, parent acceptance, and user-facing closure. Never recursively invoke $nokiy inside Nokiy.

Example: "Use $nokiy to inspect the market-time contract and report findings without changing files."

## Execute

For an explicitly authorized deployment, use the **deterministic deployment lane** in [invocation details](references/invocation.md#authorized-deployment), skipping the source-task preparation steps below. Do not ask a model to rewrite approved commands or reject deployment solely because source-task authority is limited to `none`/`workspace`. Without actual parent deployment authorization, remain read-only; this skill does not issue grants.

1. Confirm the actual target workspace, bounded permissions, parent ownership and provider-network authorization. Inspect its applicable instructions. If the workspace has DCF, use its canonical compiler and an exact discovered surface. Otherwise prepare explicit local source context plus J-Space; do not create another DCF or pretend the workspace is UTM.
2. Follow [invocation details](references/invocation.md) to resolve the installed entry, verify runtime-image identity, obtain the current native Codex identity, and query the task's exact surface. Preserve `HOME`, `CODEX_HOME`, and the current `CODEX_THREAD_ID`; never borrow an identity.
3. Reuse the target project's existing task artifact/storage policy (UTM uses its TB5 resolver). Author a fresh draft and bounded action from this task, not an old acceptance request. For discovery, declare task-authorized `read_directories` with `read` and `command` operations; the parent need not know filenames or search terms first. DCF requests also declare corresponding `read_scopes: ["<directory>/**"]`. Exact write targets remain separate. Always use `prepare`, then run its prepared request. Preparation chooses DCF/J-Space or explicitly labeled local context/J-Space and performs preflight without starting a model; execution revalidates source bindings. Local preparation needs no project Python installation.
4. If execution returns a process/session handle, wait on that same handle until terminal. Inspect actual result artifacts and relevant test evidence. `RESULT_AVAILABLE` plus `cleanup_pass` establishes execution completion, not parent acceptance.

## Boundaries and recovery

No bypass/CLEAN fallback, silent fresh-ID retry, Commander reparenting, Gateway/service/queue, private Codex database access, credential copying, self-issued deployment/broker grants, or changes to ordinary Codex routing. Nokiy may carry an existing explicit deployment authorization; it never creates that authority or overrides the target installer's CAS/lease/live boundaries.

An attempted request without a terminal result is uncertain. Recover using its original request ID and reconcile effects; never automatically resend. Report failures and unresolved evidence to the parent rather than claiming acceptance or freshness.

Missing DCF is not a global tool failure. Local preparation supports exact source reads/edits and explicitly scoped directory discovery, not arbitrary shell authority. Deployment uses its separately admitted exact plan, never a local-mode downgrade or a broader model tool grant. Other unsupported operations remain with parent native tools under their own authorization, explicitly reported as native execution, not Nokiy success. DCF compiler/freshness errors, unknown DCF surfaces, missing interpreters and nested DCF subdirectories never silently downgrade to local mode.
