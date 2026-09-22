# Embedded Single-Execution Nokiy Runtime

`nokiy-embedded-run` invokes one bounded Native worker through the existing
Router's `native-once` entry mode. One execution may contain multiple tool
calls. Only the outer Codex task decides mission acceptance or another
execution. This path does not start a Gateway, Nokiy Session DB, planner,
callback service, scheduler, or second Codex control plane.

## Topology

```text
Original Codex task / tool invocation
  -> explicit request JSON + verified compact context/J-Space
  -> nokiy-embedded-run
       -> exact runtime image: tura_router native-once
       -> Native worker -> exact Codex exec --ephemeral
            -> tura_command_graph -> Router CommandRunService -> tools
       -> stop tool admission and settle owned processes
       -> validate Native terminal bindings
       -> persist terminal.json, then return bounded JSON on stdout
  -> original Codex tool result
  -> Codex accepts, reports a blocker, or explicitly dispatches again
```

Codex retains UI, Goal, durable session and user-facing closure. The outer task
harness retains writer leases/CAS; execution IDs do not grant write authority.
The Router still owns its bounded command listener and J-Space checks. It does
not acquire cross-execution session ownership. The bridge never accesses
private Codex tables or imports a Nokiy session into them.

## Completion Callback

There is no separate asynchronous callback in this executor. Completion is the
return from the original `run` invocation: a validated result or typed failure
is persisted before stdout returns to the calling Codex tool. If the tool
harness yields a running process handle, the original task must wait for that
same handle; yielding is not completion.

`RESULT_AVAILABLE` means the execution contract passed, not that the parent's
mission is accepted. `mission_acceptance=parent_owned` and
`continuation_owner=codex` make this distinction explicit. The parent reads the
actual result/effect evidence and determines the next predicate.

An operator-direct task closes with the operator. If the caller itself is an
explicitly delegated Codex task, that task uses its already-bound parent and
callback identity through the native task transport. Nokiy does not infer a
Commander from historical metadata, send a second message, or retarget a
callback. Delivered or uncertain callback messages must not be blindly resent.

If the original call loses its response, `read-result` can recover the durable
terminal without rerunning the model or tools. It does not wake a task or prove
callback delivery. No terminal means an uncertain attempt, not permission to
retry under a new request ID.

## Required Request

New requests use `tura_embedded_request_v3` and bind:

- one `FROZEN_BYTE_EXACT` runtime-image path and SHA-256;
- one exact Codex executable path and SHA-256;
- canonical workspace and task-local artifact roots;
- fresh `task_context_capsule_v1` and J-Space identities;
- exact model, reasoning level, service tier and provider-network authorization;
- optional `model_provider`, included in the execution profile and binding;
- timeout, trajectory and result byte budgets;
- whether a successful authoritative tool event is required;
- the exact Native Codex thread and `native_codex_thread_only` persistence mode;
- `authority_effect=none` or `workspace`.

For new v3 requests, omitted `model` and `reasoning_effort` default to
`gpt-6-astra` and `high`. If neither `service_tier` nor the legacy
`model_acceleration` field is supplied, the service tier defaults to `default`.
Defaults are resolved into the canonical request before computing its identity.
An explicit profile takes precedence; existing request bytes and identities
are not rewritten.

Prefer an explicit profile when handing a request to another task:

```json
{
  "model": "gpt-6-astra",
  "reasoning_effort": "high",
  "service_tier": "default"
}
```

`service_tier` accepts `default`, `priority` and `ultrafast`. It takes precedence
over `model_acceleration`; without it, a previously explicit acceleration flag
retains its old meaning (`false` -> `default`, `true` -> `priority`). The frozen
legacy Rust benchmark profile is not a default for these v3 requests and is not
rewritten. `requested_service_tier` is distinct from `observed_service_tier`:
the latter remains unknown until provider-side evidence exists. Catalog
advertisement and argument forwarding do not prove the tier actually served.

Set `require_tool_call=true` for work that must read, inspect, edit, or verify
through a tool. The terminal then requires a successful Native CLI JSON
`item.completed` MCP event for `tura_command_graph`; model prose alone is not
evidence. Pure reasoning requests may set it to `false`.

The v3 `native_thread_id` must exactly equal the host-provided
`CODEX_THREAD_ID`. This value participates in the request digest, so an existing
terminal cannot be returned by `execute` under another Codex task. This check
also runs before cached terminal lookup. Version 1 and 2 requests remain
readable as historical artifacts, but new execution requires version 3.

`none` rejects non-empty J-Space write scopes. Stale context, runtime drift,
scope mismatch and unknown request fields fail before provider execution.
Existing workspace `.tura` effect receipts are neither rejected nor deleted.

```bash
nokiy-embedded-run preflight --request /absolute/path/request.json
nokiy-embedded-run run --request /absolute/path/request.json
```

The request identity is derived from canonical request bytes. A completed
identical request returns the existing terminal without launching Nokiy again.
An existing request directory without a terminal is an uncertain prior attempt
and is never retried automatically.

Completed evidence remains readable after execution context or authorization
expires:

```bash
nokiy-embedded-run read-result \
  --artifact-root /absolute/task-local/artifacts \
  --request-id tura_embedded_<sha256>
```

This explicit historical read path does not require a current caller binding,
inspect the runtime, context, J-Space or credentials, or start a process. Reading
another task's result is not accepting it as this task's execution.

## Tool Call Identity

Tool identity is scoped to this execution. Without an explicit tool
`execution_id`, the MCP JSON-RPC request ID identifies a call. Repeating the
same read or verifier with a new request ID is a new call, even when its
arguments are identical. Thus read -> edit -> read works within one execution.

An explicit tool `execution_id` can identify the same intended effect across
transport retries. Reusing that identity, even with another RPC ID or changed
arguments, reaches the same lower-level effect claim and is rejected rather
than repeated. Callers must reuse it for uncertain retransmissions, not
generate a fresh ID to bypass duplicate protection. New executions likewise
require parent-side effect readback before resubmission.

## Bounded Result

The terminal JSON is capped at 16 KiB. The inline model result is capped at
8 KiB; a larger last message is represented by a deterministic head/tail
preview plus an exact local artifact reference. Native request, terminal,
stderr and full result remain local and travel only as path, byte count and
SHA-256. Existing tool effect receipts remain in the workspace.

Provider execution never silently falls back to Native Codex. A runtime error,
timeout, output overflow, missing result, missing required tool event or
unproven process cleanup returns a typed blocker. `RESULT_AVAILABLE` proves the
declared execution contract and cleanup, not the truth of the model's prose.

## Native Identity and Cancellation

The child inherits the caller's `HOME` and `CODEX_HOME`. The bridge does not
copy/link credentials, require `auth.json`, bypass native rules, ignore user
configuration, or force an approval policy. Authentication and managed
requirements remain Native-owned. An explicitly selected provider is passed
through; otherwise Native resolves the provider from its existing configuration.
Requested provider values are recorded separately from observed provider
identity, which remains unknown unless actually observed.

Before model execution, the same Native binary reads its effective MCP catalog
using `mcp list --json`, with a five-second cap inside the execution deadline.
Only server names are retained; transport settings do not enter logs or prompts.
For this invocation, each inherited server is explicitly disabled and the bound
`tura_command_graph` is enabled. An empty MCP table alone is insufficient because
Native merges configuration tables. Invalid catalogs or an inherited graph-name
collision fail before the provider starts. Apps/plugin routes, native shell/exec
and agent delegation are also disabled. The Native sandbox remains read-only;
allowed effects use the Router's existing command/J-Space enforcement. The
user's configuration files are not edited. Concurrent configuration edits are
not frozen by this source-level runner and require a fresh invocation/readback.

The CLI handles SIGTERM/SIGINT while the Router runs, forwards termination to
its owned group, and waits for cleanup before returning a typed cancellation
terminal. Existing effects and receipts are preserved. Missing Native terminal
proof is still reported as unverified cleanup, even when observed processes
have exited; cancellation never becomes successful task completion.

Library calls from non-main threads leave process-wide signal handling to the
host. SIGKILL, power loss or failure before terminal persistence cannot promise
a returned callback. The parent must inspect the saved request/effects and
treat the missing terminal as uncertain instead of replaying automatically.

## Claim Boundary

Unit and fake-runtime tests prove the bridge contract, byte bounds, replay,
freshness and cleanup behavior. Compiled-tool tests with a fake provider prove
Router/worker/graph integration, not remote provider acceptance, model quality
or token savings. Source changes and candidate builds are not installed
adoption. A real provider smoke proves compatibility only for its exact
runtime/Codex/model identity; performance claims require a matched benchmark.
