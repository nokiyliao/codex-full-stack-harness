---
name: nokiy
description: Execute a bounded task through the existing installed Nokiy full-stack caller when the user invokes $nokiy or ＠nokiy (fullwidth U+FF20), or requests Nokiy execution; not a replacement for ordinary Codex coding work.
---

# Nokiy

Treat `＠nokiy` (U+FF20 FULLWIDTH COMMERCIAL AT followed by `nokiy`) as an explicit invocation equivalent to `$nokiy`. This alias does not change the execution mode or permissions; Nokiy Ultra remains opt-in. The former Nokiy Direct name remains a compatibility alias; its internal workflow ID is still `nokiy-direct`.

Use the installed caller, not a new orchestrator. The original Codex retains mission ownership, CAS/lease, permissions, final review, parent acceptance, and user-facing closure. Never recursively invoke $nokiy inside Nokiy.

Example: "Use $nokiy to inspect the market-time contract and report findings without changing files."

## Nokiy Ultra dispatch

When the operator selects Nokiy Ultra (formerly Nokiy Direct), keep the current native Codex parent as
coordinator and use this installed caller for bounded model work. Do not create
intermediate Codex subagents, including review agents or recursive reviewers.
Bind each Sol worker request explicitly to `gpt-6.1-sol` / `max`; preserve an explicit Luna or Astra picker selection and verify any required
parent model/effort contract before dispatch rather than silently switching it.
The parent performs integration and deterministic verification, does not duplicate
the worker's assignment, and recovers the original terminal without another model
call. This is an opt-in dispatch contract, not a change to ordinary Codex routing.

With verified caller V61+ and its matching native adapter, Ultra batches use the
current `agents.max_concurrent_threads_per_session` from the caller's
`CODEX_HOME/config.toml` (legacy `agents.max_threads` is supported). The coordinator
chooses the smallest useful count within that capacity; the installed configuration
currently allows five workers, not a mandatory five. With the coordinator-concurrency
caller, bind that choice as `max_parallel_workers` in the SHA-bound batch plan.
Select 1 for dependent coordination, 2 or more only for independently scoped work
whose expected latency benefit exceeds dispatch and integration cost. A batch may
contain more members than its selected parallelism, but never more than host capacity.
Omitting the field preserves existing all-member parallel execution. No extra user
prompt or fixed worker count is required; the coordinator owns the selection.
The Nokiy fallback is five
when neither setting exists; it is not a claim about Codex's native default.
All members must be independently scoped, and every read/write pair is checked
before execution. Wait for the current batch to terminate before starting another;
this is a per-batch limit, not a shared global reservation of native agent slots.
Keep the original plan path and SHA for recovery; lowering capacity must not prevent
reading completed or uncertain results. Ordinary Nokiy remains serial.

Ultra uses the default service tier. The Fast workflow has been retired;
do not route its former name to a priority request or silently substitute another mode.

## Execute

For an explicitly authorized deployment, use the **deterministic deployment lane** in [invocation details](references/invocation.md#authorized-deployment), skipping the source-task preparation steps below. Do not ask a model to rewrite approved commands or reject deployment solely because source-task authority is limited to `none`/`workspace`. Without actual parent deployment authorization, remain read-only; this skill does not issue grants.

1. Confirm the actual target workspace, bounded permissions, parent ownership and provider-network authorization. Inspect its applicable instructions. If the workspace has DCF, use an exact known surface ID or, only when the current installed caller supports it, opt in to `surface_targets` for bounded path lookup; `surface-map --target` accepts a surface ID, not a file path. For a known exact symbol, prefer the installed caller's `navigation_targets` preparation path instead of a separate source-navigation call; it resolves a current locator before J-Space compilation. A locator is context, not a read/write grant. Otherwise prepare explicit local source context plus J-Space; do not create another DCF or pretend the workspace is UTM.
   Prefer batching known independent preflight reads in one available native orchestration call; keep fresh permission/owner/CAS checks at effect boundaries.
2. Follow [invocation details](references/invocation.md) to resolve the installed entry, verify runtime-image identity, obtain the current native Codex identity, and query the task's exact surface. Preserve `HOME`, `CODEX_HOME`, and the current `CODEX_THREAD_ID`; never borrow an identity.
3. Reuse the target project's existing task artifact/storage policy (UTM uses its TB5 resolver). Author a fresh draft and bounded action from this task, not an old acceptance request. For known DCF symbols, add up to four exact `symbol:...` values to `navigation_targets`; keep authorized read/write scopes explicit and independent of those locators. For registered paths when the installed caller supports the option, use `surface_targets` with 1..4 distinct exact physical workspace-relative regular files; omit `--surface-id` only if exactly one common surface is expected. An explicit ID must match every path. Resolution is diagnostic only, never a read/write/operation grant; unresolved, ambiguous and unverified evidence do not permit guesswork. With caller V38+, omit `include_source_excerpt` to automatically supply a bounded, SHA-verified definition plus same-file syntactic support for a DCF read-only question with one exact symbol/file, `authority_effect: "none"`, and explicit `require_tool_call: false`. Existing `source_read` tools stay available for missing evidence. `include_source_excerpt: false` disables this optimization; `true` retains the older explicit body-only behavior. Capacity overflow retains the normal read path, never truncated evidence. With verified caller V40+, exact edit requests also auto-preload 1..4 Python symbols when `authority_effect: "workspace"`, `require_tool_call: true`, `source_read: true`, and each selected file has exact read and write grants. Operations must be read/modify with optional command; complete definitions and shared support use one deduplicated budget. Preimages are checked before execution and edits get fresh postimage readback. Explicit `true` retains read-only semantics; omit the flag for automatic edit selection. Other shapes retain ordinary reads. On older callers the explicit body-only option remains available. Excerpts are context, never grants. Preparation rejects stale, partial or ambiguous navigation rather than broadening access. Do not paste raw DCF responses into the prompt. Use `read_directories` only when paths remain unknown. Directory discovery needs task-authorized `read` and `command` operations, and DCF requests also declare matching `read_scopes: ["<directory>/**"]`. For exact-file source reads on a verified runtime and canonical compiler that support `source_read`, prefer `source_read: true` with `read` and `command`, exact regular-file `read_scopes`, and no `cat` template. Read bounded line ranges and continue from `next_line`, retaining `expected_sha256` for follow-up pages. DCF mode must obtain `source_read: true` from the real DCF compiler and retain its required-domain freshness checks; never inject the grant into a compiled contract or downgrade to local mode. `read_scopes` alone expose no read tool: the action must select `source_read` or exact typed `cat` templates. For a few small known files that fit one bounded batch, or when the verified runtime lacks `source_read`, an exact multi-file `cat` template remains useful. Never switch paths after denied, stale, or uncertain execution and never broaden scopes merely to batch. Exact write targets remain separate. Always use `prepare`. If the sole requested answer is an exact symbol `path:line`, and preparation returns one current complete locator with no required file-content check, effect or explicit model-execution requirement, report that locator from preparation and stop without `run`; label this `PREPARED_NOT_EXECUTED`, not a model result. Otherwise run the prepared request. Preparation chooses DCF/J-Space or explicitly labeled local context/J-Space and performs preflight without starting a model; execution revalidates source bindings. Local preparation needs no project Python installation.
   Dispatch through the genuine installed Nokiy tool; never guess nested APIs.
   On a verified supporting caller/runtime, include already-known, task-authorized deterministic tests as `action.verifier_commands` using the existing [in-loop public verification contract](references/invocation.md#in-loop-public-verification). When the task permits mixed responses and all dependencies, effects, checks and readbacks are fully known, prefer ordered `apply_patch` → `focused_verifier` → `task_status done` in one `command_run`, without a model handoff between known steps. Keep verifier-only step groups strictly later than dependent patches, and `done` alone at a strictly later final step, conditional on all required verification and readbacks succeeding. Missing evidence, test failures or uncertain effects require normal reasoning; never batch unknown work or expand permissions.
4. If execution returns a process/session handle, wait on that same handle until terminal. With native `functions.exec`/`tools.write_stdin`, keep repeated waits in one orchestration call instead of re-entering the coordinator on unchanged output; resume any yielded original cell with `functions.wait` only (see [native terminal-owned wait](references/invocation.md#native-terminal-owned-wait)). With the verified V51+ caller, `run` and original-ID `read-result` automatically attach `result_inspection`: use this deterministic projection instead of repeating its artifact hashing, command-event lookup and process probes. It preserves the raw terminal and never executes another model call. Require `status: EVIDENCE_VERIFIED`, the expected request/thread, the exact required command and integer `exit_code: 0` with `receipt_check: matched` and `execution_proof: known_success`; still inspect task-specific test results and edit postimages. `pagination.next_offset` means displayed commands are incomplete: retrieve only the required continuation using the installed `result_inspection` module, original artifact root/request ID and current thread, not a fresh execution. Unknown, failed or missing evidence is not success. Older callers without this projection require verifying `command_evidence_artifact` and the exact indexed `core.jsonl` event or per-command receipt. An outer `runtime_exit_code`, `completed`, aggregate count or hash alone is insufficient. `RESULT_AVAILABLE` plus `cleanup_pass` establishes execution completion, not parent acceptance; raw Python `read_terminal` remains unprojected recovery data.
   Reuse verified `result_inspection`, avoiding extra terminal-key dumps or reopening historical receipts.
   Parent retains semantic acceptance, fresh effect authority and necessary postimage/quality checks. Reuse matched successful verifier receipts for the exact unchanged source/test/config state rather than repeat the same test; relevant changes, failures or unknown/mismatched evidence require fresh verification.
5. Finish the parent-owned delivery in this invocation, not in a future cleanup task. For a task with an outer repository lease, bind that exact lease and the existing project finish command in the delivery plan's `task_closeout` (see invocation details). After parent acceptance and required commit/deployment verification, the deterministic lane automatically runs closeout and verifies both the matching completion receipt and absence of the active lease. A worker terminal never releases the parent's lease. Do not claim overall completion from `VERIFIED` deployment alone when closeout is missing or blocked; report `CLOSEOUT_BLOCKED` and the exact remaining predicate. For source-only delivery without a deployment plan, invoke the project's existing finish command in the same parent turn and perform the same readback. Never delete a lock, silently park failed work, or change the baseline to finish.
   Where supported, prefer batching known task verification, required postimage/quality readback, compact evidence publication and required closeout in one parent acceptance call, in dependency order; not a call-count cap, permission grant or new executor.

With verified caller V112+, automatic evidence uses the `priority` command stream.
Continue its cursor with `--command-stream priority`; omitting this option means
the original chronological history, not the priority page. See invocation details.

## Capability-gap continuation

With a verified matching terminal-evidence caller/runtime pair (V127+), select
`action.terminal_delivery: "evidence_only"` for bounded execution work whose
deliverable is tool evidence or artifacts that the parent will inspect, rather
than a worker-authored answer. Do not select it for reviews, questions, or work
requiring a narrative conclusion. The default remains `assistant_reply`.
Skipping the final summary requires a persisted, same-request terminal marker;
inspect `requested_terminal_delivery`, `observed_terminal_delivery`, and
`terminal_evidence` on original-ID recovery. Missing evidence retains the normal
reply path; it never proves completion or parent acceptance.

When the current caller exposes `capability_gap`, treat it as a **model-reported diagnostic**, never a permission grant, execution proof, safe replay or mission success. See [the handoff format and procedure](references/invocation.md#capability-gap-diagnostic-caller-only). This caller-only feature does not expand runtime permissions or imply an installed/deployed runtime supports it.

1. Recover the original request ID and inspect its terminal, effects and cleanup first. Reconcile uncertain effects; never resend a consumed action to recover a handoff.
2. Compare the exact missing path/operation/tool and remaining work against the original human mission, explicit prohibitions and fresh owner/CAS/lease evidence. If this is only an omitted capability **within that authorization**, the parent freshly prepares only the unfinished bounded action without asking again; no worker self-grant or replay of completed effects.
3. A true prohibition, new protected scope, owner conflict or unresolved effect stops continuation. A genuinely unsupported tool may be used by parent native tools only under the same original authorization; report it as **native execution**, not Nokiy success.
4. A read-only patch proposal must contain an actual usable unified diff from sufficient admitted reads, or precise missing read evidence. Review fresh preimages and run required verification before parent acceptance; prose or a placeholder is not a patch.

The earlier “never switch paths after denied, stale, or uncertain execution” rule still binds the worker. Fresh parent re-admission after effect reconciliation and owner checks is a new bounded action, not a worker bypass, scope broadening or an automatic retry.

## Boundaries and recovery

No bypass/CLEAN fallback, silent fresh-ID retry, Commander reparenting, Gateway/service/queue, private Codex database access, credential copying, self-issued deployment/broker grants, or changes to ordinary Codex routing. Nokiy may carry an existing explicit deployment authorization; it never creates that authority or overrides the target installer's CAS/lease/live boundaries.

An attempted request without a terminal result is uncertain. Recover using its original request ID and reconcile effects; never automatically resend. Report failures and unresolved evidence to the parent rather than claiming acceptance or freshness.

Missing DCF is not a global tool failure. Local preparation supports exact source reads/edits and explicitly scoped directory discovery, not arbitrary shell authority. Deployment uses its separately admitted exact plan, never a local-mode downgrade or a broader model tool grant. Other unsupported operations remain with parent native tools under their own authorization, explicitly reported as native execution, not Nokiy success. DCF compiler/freshness errors, unknown DCF surfaces, missing interpreters and nested DCF subdirectories never silently downgrade to local mode.
