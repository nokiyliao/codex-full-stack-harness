# Full-Stack Review Profile

## Preferred Native Control Plane And Independent Nokiy Runtime

The terminal topology keeps Native Codex as the sole UI, conversation, Goal,
task-lifecycle and authority owner. An already-admitted dependent command graph
or explicitly selected one-shot full runtime may execute outside that control
plane:

```text
parent mission / ordered predicates
  -> Native Codex task persistence and lifecycle
  -> DCF/J-Space bounded context and effect contract
  -> explicit Native batch OR Nokiy command graph OR embedded full Nokiy turn
  -> Native Codex result intake
  -> strict native direct-parent callback
  -> parent mission verification
```

The ordinary Native and command-graph paths use no Nokiy Gateway, Router,
Session DB, provider lifecycle, daemon, callback queue, or second App Server.
The explicit embedded full-runtime path starts those Nokiy runtime components
only inside one call, with a task-local ephemeral database and no persistent
Gateway. It never owns the Codex task or conversation. The retired Skill was
only a selector and contract surface, not the performance source. No implicit
model-selected route remains.

## Explicit Embedded Full Runtime

`nokiy-embedded-run` is the full-runtime surface. It verifies a frozen runtime
image and the current Codex executable by SHA-256, verifies fresh compact
context and J-Space scope, and starts one unique localhost Gateway. Nokiy owns
the model/context/tool loop for that call. Native Codex receives only a bounded
terminal result and local evidence identities.

The current v3 contract binds the call to the invoking `CODEX_THREAD_ID` and
uses `persistence_mode=native_codex_thread_only`. Nokiy's Session DB exists only
as per-call scratch and is removed after closeout; it is never a second durable
conversation store. This is shared persistence through the public Native turn,
not direct access to Codex's private database.

This preserves the architectural gain that the retired Skill could not
provide, without maintaining a second global Session DB or Gateway. Per-call
state is removed only after process closeout is proven; uncertain state is
retained with a typed blocker. There is no Native fallback and no direct access
to the Native Codex private session database. See
[`embedded-nokiy-runtime.md`](embedded-nokiy-runtime.md).

## Native Dispatch Prefix Locality

The compact `render_dispatch()` output places a fixed no-Skill execution-surface
marker and fast-path guidance first, followed by the execution profile and verified
J-Space policy. Task identities, fresh evidence, callback templates and the
mission follow. Only presentation order changes: no padding, prewarming,
omitted bindings or cached authorization decisions are introduced. The full
fallback `render_task()` and canonical capsule identities remain unchanged.

Equal profiles and policies therefore keep an equal text prefix across new
dispatches. Changed scope, policy, model preference or evidence is still
rendered from the newly verified capsule. J-Space v1 includes generation in its
semantic identity; v2 can preserve authorization identity across a generation
change. Neither identity may be stripped merely to lengthen a common prefix.

This improves caller-side prefix stability, not proof of provider cache hits.
Native Codex still constructs the final model request; this adapter does not
control its tool serialization, cache lookup boundaries, routing or retention.
It does not rewrite existing conversations or replace their compaction flow.
Use complete cumulative input/cached/output usage and equal-workload timing
for a live comparison, not prompt byte counts or cache-hit ratio alone.

## Optional Same-Conversation Graph Tool

`nokiy-graph-tool` exposes the separately licensed Rust `command_run` engine as
a preferred task-bound direct CLI or an explicit local stdio MCP tool. It reuses the repository's
DCF freshness verifier and binds its compact context to the J-Space contract.
Codex still owns the only conversation, task, model and Goal. No Desktop binary
or private state is modified. Installation and actual host discovery require
their own readback; a standalone stdio client does not prove host registration.

The default remains Native tools/Code Mode. The optional profile is for fully
determined multi-step work, not automatic routing of every turn through Nokiy.
The direct `prepare` entry reuses the same DCF compiler and `prepare_graph`
validation as the Native MCP tool, without starting either MCP or the engine.
It reads one bounded specification from stdin and returns the exact verified
execution envelope with the Native caller's task identity. Exit 0 proves only
preparation; commands and task artifacts are not created. The caller retains
that envelope for execution/replay instead of recompiling a new expiry.
For already compiled candidates, `nokiy-graph-tool auto` selects the existing
direct graph or returns a non-executing `native_required` recommendation (exit
3). It validates host/context/freshness before choosing, retains existing graph
call history on the graph path, and never falls back after graph failure.
The Native owner still authorizes and executes a recommended Native call;
selection is not a cross-route lease, automatic Native dispatch, or an effect
receipt. Simple reads need not prepare a candidate or invoke this selector.
The Skill selector is retired. `nokiy-graph-tool` may be called only through an
explicit command-graph request; it is not selected implicitly and is not a full
Nokiy model runtime. Missing native tool discovery is not permission to create a
second dispatch service or reuse a historical model benchmark.
The Python adapter stays MIT; the Rust implementation remains AGPL-3.0-or-later.
Source-backed integration tests do not establish model/token savings. Compare
the same task/context/model against native batching and isolated full Nokiy before
claiming a performance gain.

Install the optional `[graph]` extra for this profile. It includes the official
MCP SDK and `filelock`, which the configured DCF repository's storage module
imports. An import that passes in the development environment is not evidence
that the separate installed environment has the same dependencies.

`scripts/graph_readonly_smoke.py --installed-entry <installed-nokiy-graph-tool>`
verifies the installed CLI and default configuration from the current Native
task. It does not inject a task ID or a source `PYTHONPATH` into that CLI. It
checks a real dependent source-read graph, then cached replay in a second
invocation. It defaults to `--entry-mode execute`; `--entry-mode invoke` verifies
the retained MCP route. Neither entry is direct Native MCP
registration or proof that every eligible model turn automatically selects it.
`--entry-mode auto` also verifies the distinct no-execution Native recommendation,
actual selected graph execution, and same-identity cached replay.

The direct entry invokes the independent bounded runtime while removing the MCP
client/server startup, not its
DCF verification, sandbox, supervisor, effect journal, or finalization. Both
shell modes require the real `CODEX_THREAD_ID` environment and reject a conflicting
`--caller-task-id` before opening DCF or executing commands. The separate `serve`
mode remains for an explicitly bound host. Same-workload local measurements
support this lower-cost graph entry, but still favor Native batching for fixed
commands. A current-task eight-step comparison measured about 54.54 s for eight
model/tool turns, 20.24 ms for one Native batch, 845.61 ms for a cold Nokiy graph,
and 652.76 ms for cached replay. That proves where the independent runtime helps;
it does not establish universal token or model-quality gains.

The graph closeout profile seals newly completed calls into one verified gzip
bundle per call and retires only the exact private duplicate artifacts. One
small journal guard remains so an older engine cannot repeat the call after
rollback; a missing archive also stays fail-closed. Its
tests cover lossless reconstruction, cached replay, interrupted staging/link/
unlink, changed preimages, foreign links, unknown evidence and legacy journals.
This is producer-side finalization, not a global cleanup service. The per-call
supervisor runs inside the same restrictive macOS sandbox as the engine and
commands. Its kernel-enforced signal boundary includes descendants that change
process group or session, but excludes unrelated and independently sandboxed
processes. Engine completion is only provisional until process cleanup has
finished; a separate, bounded finalizer then publishes the successful bundle
without executing the graph again.

Execution results pin the original engine-result digest before adapter metadata.
Every outward projection enforces the 65,536-byte application JSON limit after
metadata, including CLI newline and the MCP SDK's indented text representation.
This is not a bound on the whole MCP frame, which contains protocol fields and
may contain both text and structured copies. A diagnostic too large to project
returns `GRAPH_RESPONSE_BUDGET_EXCEEDED` without allowing fallback or replay;
the recorded engine outcome and its bounded provenance remain distinguishable
from successful result intake.

Engine death, caller death, cancellation, deadline and output overflow have
focused real-process tests. When the caller survives and cleanup is proved, it
can record an interrupted journal and exact admitted target observations.
Partial or unknown effects remain unproven and cannot be blindly replayed.
Caller death can clean processes without updating that journal. Supervisor
SIGKILL or machine failure is not covered by a claim of automatic recovery;
the surviving journal remains conservative recovery evidence. Whole-task
retention and a fair Native/graph/full-runtime comparison remain separate work.

## Task Retirement Preparation

`read-result --call-id EXACT_CALL_ID --request-digest EXACT_REQUEST_SHA256`
returns verified completed-call evidence even after execution expiry. It reads
only the existing sealed pair under pinned directory descriptors, with bounded
decompression, complete terminal validation and an exact before/after snapshot.
It neither executes a replay nor acquires the task writer lock. Unrelated running
calls and their locks remain untouched. Missing, changed, corrupt or unresolved
exact-call evidence is still rejected. Both `read-result` and `plan-retirement`
need only the Native caller and artifact root, not engine or DCF configuration.
The latter still takes its existing owner lock because it snapshots the whole
task for a possible later retirement; individual result intake does not.

The `plan-retirement` entry introduced in `0.3.17.dev0` prepares an in-memory
lossless snapshot of the current Native caller's sealed graph artifacts. It is
not a task-retirement command. After the matching package is installed and an
authorized artifact read is available, use:

```sh
nokiy-graph-tool plan-retirement
```

The entry binds `CODEX_THREAD_ID` before reading artifacts, takes the existing
Rust task-directory lock without waiting, and never reads DCF, starts a model,
or consumes stdin. It rejects unknown/partial artifacts, unpaired archives and
rollback guards, foreign identities, unsafe links, unsettled node receipts,
unproven process closeout, changed snapshots and bounded-input violations.
Directory enumeration and aggregate decompression are bounded, not just the
size of the final response.

`graph_retention.prepare_task_retirement` returns the snapshot bytes and a
report. The CLI emits only the report; neither path persists a new artifact or
mutates the original files. All original guard/archive bytes and directory/file
modes are retained in the snapshot and independently decoded for comparison.
Preparation reports `prepared_not_admitted`, zero reclaimed bytes and
`GRAPH_TASK_TERMINAL_AUTHORITY_UNVERIFIED`. Successful CLI exit means the plan
was computed, not that the Native task completed or cleanup was authorized.

Isolated tests prove that a regular-file tombstone at the former task-directory
path makes both the current and rollback Rust engines fail before execution.
That fixture is NOT an atomic adoption implementation. An eventual retirement
still needs authoritative task/Goal/lease/reference closeout, a race-free
existing-owner transaction, persistent snapshot readback, rollback/replay proof
and exact reclamation. No active task may be renamed or deleted on the strength
of this preparation alone. There is no apply, restore, unlock, sweeper or daemon
entry in this profile. Packing already compressed call bundles can increase
logical bytes for small tasks. Compare source bytes with snapshot bytes and the
additional retirement/replay marker cost; do not infer storage savings from
successful reconstruction or a smaller projected file count.

## Optional External-Runtime Review Profile

The public project also preserves an external-runtime profile for portability,
historical provenance, and review of the modified Rust implementation:

```text
parent mission / ordered predicates
  -> MIT collaboration core
  -> MIT Nokiy adapter contract
  -> public AGPL Nokiy runtime fork
  -> terminal envelope / effect reconciliation
  -> destination-bound continuation and ACK
  -> parent-owned MissionSnapshotReadback
```

This is an optional review package, not a dependency of the preferred Native
Codex deployment. The split keeps the generic protocol reusable while making
the earlier runtime engineering inspectable to third parties.

## Components

| Component | Role | License | Public evidence |
|---|---|---|---|
| Collaboration core | Mission, first-false routing, lease/CAS, terminal receipt, continuation, ACK, parent readback | MIT | `src/codex_collaboration_harness/core.py`, synthetic tests |
| Nokiy integration kit | Stable request/envelope mapping and fail-closed adapter behavior | MIT | `src/codex_collaboration_harness/adapters/nokiy.py`, conformance tests |
| Native task capsule | Bounded task projection; Native Codex retains persistence, lifecycle, tools, and callback transport | MIT | Execution-profile and terminal schemas, capsule and distribution tests |
| Modified Nokiy runtime | Optional external/legacy profile for Gateway, Router, runtime/session lifecycle, commands, receipts, recovery, callback and provider execution | AGPL-3.0-or-later | Public fork and exact `components/nokiy-runtime.json` identities |
| Internal benchmark projection | Motivation and bounded aggregate engineering observations | Evidence only | Sanitized JSON, integrity manifest, explicit non-causal limitations |

No UTM repository, task corpus, account state, broker surface, raw conversation,
or local runtime database is needed to inspect or run the public synthetic suite.

## Runtime Contribution

The public Nokiy fork contains 95 maintainer-authored commits after its exact
upstream base at the modified-source parent recorded in the component manifest.
The changes cover collaboration ownership, command receipts, interruption
recovery, terminal callbacks, ACK/convergence, child dispatch, provider routing,
health truth, bounded startup recovery, and fault tests. The fork's
`MODIFICATIONS.md` is the authoritative reviewer map.

## Identity and Maturity Split

The component manifest prevents four different facts from collapsing into one:

1. **Public source ref:** the source and modification notice are fetchable.
2. **Benchmarked candidate:** one exact ancestor produced the labeled internal
   engineering aggregate.
3. **Later collaboration source:** additional callback and lifecycle changes are
   visible but do not inherit the benchmark result.
4. **Installed/running state:** not claimed by this public package.

A reviewer can therefore inspect the real implementation without treating a
source commit, test, benchmark, binary, and deployment as interchangeable.

## Reproduction Paths

### Generic synthetic path

```bash
make check
```

This path is offline, standard-library-only at runtime, and exercises the
complete generic collaboration cycle plus Nokiy adapter conformance.

### Runtime source path

```bash
git clone --branch codex/collaboration-runtime-public-v0.1.0 \
  https://github.com/nokiyliao/tura.git
```

Follow the Nokiy repository's platform build instructions. The full Rust/runtime
build is intentionally not executed by the Python package's CI. A future public
runtime conformance workflow must bind its binary and run receipt to the exact
component commit before claiming installed behavior.

## Non-Claims

The profile does not assert that OpenAI reviewed or endorsed the project, that
the internal benchmark generalizes to other workloads, that the latest Nokiy
source is installed, or that the in-memory Python core provides distributed
durability. Those remain separate evidence predicates.
