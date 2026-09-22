# nokiy: Codex-owned execution core

Status: the full-stack caller is installed at `nokiy-20260922-v5`, reusing the
unchanged `nokiy-20260922-v3` runtime image. The installed CLI compiled DCF and
J-Space, completed a real Direct tool read, returned a recoverable terminal and
reaped its processes. Evidence: `outputs/nokiy-fullstack-default-20260922-v1/installed.json`.
Ordinary Codex routing is unchanged. The prior v3 caller is the rollback target.

## Naming and ownership

The local execution core is named **nokiy**.
Codex owns the task, Goal, context, permissions, continuation and final acceptance;
nokiy manages an individual execution and returns its result. It does not
create a second mission owner or a permanent task scheduler.

New preflight and terminal results identify `executor_name: nokiy` and the
`direct` / `balanced` profile, without an `execution_backend` branding field.
The naming change is not dependency removal or a performance claim. Installed
deployment has separate byte-bound acceptance. Required upstream license notices and
internal compatibility identifiers remain intact.

Owned modules and CLI entry points use nokiy; new caller errors use `NOKIY_*`.
Existing request/schema identifiers, callback markers, recovery paths and
upstream binary/environment names remain compatibility identifiers. They are
not the product name. Request hashes and
completed terminal records are not rewritten for branding; a naming change
must not turn a prior execution into a new request or trigger a replay.

## Request contract

The existing `nokiy-embedded-run` request accepts an optional
`execution_profile` of `direct` or `balanced`. For new work use the existing
CLI's `prepare` entry below: it defaults to Direct and compiles fresh DCF and
J-Space together. The low-level decoder preserves old request identities;
omission there retains historical native-once behavior, not the new-task default.
No automatic dispatch or ordinary Codex route change is enabled.

### Default new-task invocation

```bash
nokiy-embedded-run prepare \
  --request /absolute/task/draft.json \
  --action /absolute/task/action.json \
  --surface-id <verified-dcf-surface> \
  --output-dir /absolute/task/new-preparation
nokiy-embedded-run run --request /absolute/task/new-preparation/request.json
```

The draft uses the existing request fields (including pinned runtime image,
Codex executable, workspace, artifact root, prompt, budgets and explicit
provider-network/effect authority), but omits `context_capsule` and
`jspace_contract`. Model/effort/tier default to Astra/high/default. Explicit
settings are preserved. `native_thread_id` defaults to the current host-provided
thread and cannot differ from it. `action.json` supplies the current mission,
bounded context summary, operations, read/write scopes and exact command
templates. It is not permission to exceed the parent's lease or sandbox.

`prepare` invokes the workspace's `.venv/bin/python scripts/ops/dcf.py jspace
compile`, consumes the canonical inline result without rewriting its semantics,
and runs the existing full-core preflight. It publishes `request.json` last and
starts no model. Missing DCF, stale/mismatched context, invalid scope or image
drift blocks preparation; there is no CLEAN fallback. `run` revalidates before
execution. The output directory must be new; a submitted request must not be
recompiled during transport recovery. Use `read-result` for completed requests.

For canonical v2 contracts with action-scoped freshness, preflight and execution
invoke the existing DCF required-domain fingerprint verifier using the workspace
interpreter. An old generation timestamp alone does not make unchanged required
domains stale. Changed fingerprints, mismatched bindings or an unavailable
verifier fail closed. Legacy contexts without that token retain the age limit;
future-dated generations are rejected in both cases. No timestamp is rewritten
and no global index refresh or wider age allowance is used as a workaround.

Both capsule and J-Space are required in the installed full-core path. The
read-only ablation bypass is a separate nondefault candidate build and is not
part of this installed workflow. Current-task tool-registry sharing remains
outside the meaning of "full stack" here.

Codex owns the task, Goal, authority, acceptance and explicit next execution.
The caller reuses context/J-Space validation and completed-terminal readback.
An existing request directory without a terminal is uncertain and cannot rerun.

## Execution implementation

The full-core image must inventory every file and bind the CLI, runtime,
Router, Session DB, provider configuration and both agent profiles by hash.
The caller preserves HOME/CODEX_HOME. Provider credentials are not copied.
Only the execution-local backend state path is new; native private databases are
not opened or edited by the caller.

The full core uses the existing scoped CLI entry and explicit loopback Router.
The caller starts the existing services for this execution, stops its child
handles, and reuses `graph_process.supervise` for a kernel-fenced check that
detached descendants have also terminated. Its extra Seatbelt profile is a
signal fence, not a substitute for filesystem sandboxing or J-Space authority.
Tool writes still require the full core's existing sandbox and J-Space checks.

Completed output must bind the requested model, effort, service tier, agent,
workspace and session. This is CLI configuration evidence, not independent
provider attestation. Usage stays unknown when no provider counter is returned.

## Sharing with Codex

The operator asked whether services can be shared with Codex. Task/Goal/context,
authority and result ownership should be shared, not duplicated. Codex native
private SQLite files are not a supported replacement for the backend Session DB
protocol. A public tool adapter or existing public execution API must be used
where available; no private Codex-state writes or new permanent controller.

The deployed path uses execution-local services, owned and reaped by the caller;
they do not become a second cross-task owner or permanent controller. This is
distinct from reusing every tool of the current Native Codex turn. Native2 is
the Web-model-to-Codex bridge, not a dependency of this local execution path.
Do not discard Direct/Balanced capabilities merely to remove these processes,
or claim native-once has the measured benefits of the full core.

Current-task tool-registry sharing remains an unimplemented optional integration.
The earlier exploration did not establish Native2 as a suitable local adapter.
If this integration is pursued, it requires its own admitted interface and real
tool, authority and cancellation acceptance; current tests do not prove it.

## Verification boundary

Actual candidate Direct and Balanced executions now pass a bounded source edit,
three unchanged fixture tests, eight independent cases per profile, tool-result
return and process cleanup. A real ten-second deadline run leaves source
unchanged and reports no live local descendants; remote provider cancellation
and billing remain unknown. These are functional probes, not a new benchmark.
Source-bound records live under the Rust candidate's
`output/taskcore-explicit-provider-20260922/` directory.

The initial tool observer counted `task_status` bookkeeping as command execution.
nokiy now excludes it and rejects explicit failures nested inside a completed
wrapper. Offline readback of the preserved actual trajectories gives Direct
8 execution observations / 4 successful, and Balanced 9 / 5, without changing
their text, usage, request identity or immutable terminals. Historical terminals
retain their original counters; they are not silently repaired or replayed.
The naming fields remain output metadata only.

Codex's public `command/exec` API can execute without creating a new thread:
https://learn.chatgpt.com/docs/app-server
That does not by itself expose the current task's complete tool registry or
authority. On this host, native `app-server daemon version` cannot connect to
`~/.codex/app-server-control/app-server-control.sock` (ENOENT). The advertised
Native2 bridge requires a current admitted turn token, which is unavailable to
this caller. No daemon or substitute identity was created to evade that limit.

That interface limitation does not block the independently installed local
executor. It must not be worked around with another task's token or a new daemon.

## Historical v2 acceptance

Release root:
`/Volumes/NOKIY-TB5/UTM/runtime_releases/codex-collaboration-harness/nokiy-20260922-v2`

- Installed package bytes match the wheel; all 22 runtime assets match the image.
- Actual installed Direct tool-read acceptance is in the release's
  `acceptance/accepted.json`.
- Actual installed Balanced edit/test acceptance is in
  `outputs/nokiy-balanced-installed-20260922-v1/acceptance.json`: three unchanged
  fixture tests and eight independent parent cases passed, with process cleanup
  and unchanged cached-result readback.
- `tests/test_full_core.py` runs against the installed package: 16 tests passed,
  including both profiles under parent SIGTERM and SIGINT, in-flight duplicate
  rejection without interrupting the owner, detached descendant cleanup, and
  failure-after-edit preservation without replay. These lifecycle cases use
  synthetic engine executables, not remote provider cancellation.
- Exact aliases, unchanged ordinary Codex configuration and preserved prior
  image were freshly checked in the lifecycle acceptance turn.

Return/callback semantics are the original invocation's terminal result, persisted
before returning. No independent sender retries a callback or starts another
execution. Codex performs independent acceptance and explicitly decides what is
next. If the caller itself dies before a terminal is durable, an existing request
directory is uncertain and cannot be blindly retried.

The current audit is
`outputs/nokiy-lifecycle-acceptance-20260922-v1/ACCEPTANCE.md`.
Historical audits remain unchanged. There is no new claim of comparative model
gain, provider-observed identity, remote billing cancellation, every possible tool
policy, automatic dispatch, or clean-room independence.
# Flexible workspace context

`nokiy-embedded-run prepare` uses canonical DCF when the workspace declares it;
`--surface-id` remains mandatory there. A project without DCF can omit that flag
and provide a parent-authored mission, context summary and exact file scopes.
This emits `local_workspace_jspace`, using the same capsule/J-Space wire schemas,
installed execution core, sandbox, thread binding, cancellation and result recovery.
The existing wire field `dcf_generation` explicitly records `dcf_available: false`
and `context_mode: local_workspace_jspace`; it contains no invented generation ID
or discovered surface. Local source hashes/modes and absent create targets are
checked at prepare and execution admission, plus the existing age bound.

Local mode currently supports bounded regular-file read/create/modify tools and
typed `pwd`/`cat` commands. It does not grant arbitrary shell or deletion authority.
Parent-owned native validation remains available independently. Parent scopes and
CAS/leases are still required; a context hash is not a write lease. DCF markers in
the workspace or ancestors prevent bypass via a subdirectory. Missing DCF Python,
stale domains, unknown surfaces and compiler failures stop only the affected call;
they never downgrade silently or disable the whole native harness.
# Explicit Deployment Actions

The existing caller also exposes `check-deployment`, `deploy`, and `read-deployment-result`.
This is a separate deterministic execution lane for exact parent-authorized preflight/apply/verify commands,
not another model runtime and not a wider `authority_effect` for the source-task worker.
It reuses the existing per-call process supervisor. The target installer retains authority, locks,
CAS, rollback and service lifecycle responsibilities. The caller binds approved inputs, expiry and
current task identity, blocks uncertain replay, and requires bound version/health verification.
See `skills/nokiy/references/invocation.md#authorized-deployment` for the wire shape and acceptance semantics.
