# One-shot Tura execution for UTM

Current source tranche: `WEB_TURA_ONESHOT_SIMPLIFY_20260921_V1`.
A bounded execution may contain multiple provider/tool rounds. It does not own
cross-execution continuation, task planning, mission acceptance or redispatch.

## Canonical request path

```text
Outer Codex + existing task harness
  -> existing embedded_tura caller: exact request/image/context binding
  -> tura_router native-once --worker <exact Native binary> --worker-sha256 <SHA>
  -> tura_native_codex_worker: one v3 request / one terminal
  -> Native Codex execution (read-only; native machine-effect tools disabled)
  -> tura_command_graph (the only MCP tool)
  -> existing Router CommandRunService / J-Space / command receipts / locks
  -> existing machine-touching handlers
  -> confirmed execution cleanup + Native terminal
  -> outer Codex decides acceptance or an explicit next request
```

`native-once` is an entry mode of the existing Router binary, not a second daemon
or a new persistent control plane. Its task-local command IPC accepts only the
bound session, execution, workspace, command set and complete J-Space body.
It never constructs Gateway AppState, a Session DB, MANO/MANAS, a child-session
planner, callbacks, a session recovery scan or a continuation dispatcher.
Generic interactive Tura consumers keep their existing entrypoints unchanged.

## Identity and ownership

The caller verifies the selected Codex executable, runtime artifacts, capsule
and J-Space bytes, current Native caller identity and existing freshness/authority
constraints. The same verified capsule/J-Space bytes are used to build the request.
The Router cross-binds the full request digest, root, task, delta and capsule.
The model, reasoning effort and service tier are explicit v3 profile fields and
are included in the execution-profile identity. No model fallback occurs.
The balanced prompt bytes remain bound to the worker's compiled prompt.

The v3 request's `lease_id` names this disposable execution lifetime. It is not a
replacement for the outer task harness's writer lease, a CAS ownership proof or
an authorization grant. The existing outer harness still owns admission and
source-write governance. DCF is evidence/navigation, not an execution authority.
J-Space exact commands/targets and the existing tool effect boundary are retained.

Legacy raw-worker v2 requests retain their old fixed profile. The canonical Python
caller dispatches v3 only. Historical caller v1/v2 requests can be decoded/read
back, but cannot be silently re-executed using the new route. Old images are not
modified to support the new entry mode: use a matching new image and caller.

## State, retry and continuation

A completed caller request is read back from its terminal instead of rerunning.
An existing request directory without a terminal is an uncertain prior attempt:
there is no blind retry. Within an execution, the existing command receipt layer
refuses duplicate effect dispatch; it does not promise identical RPC replies.
A failed execution can already have effects. Its evidence and files survive.

The parent must inspect the current workspace, changed-file identities, tool/test
evidence and the failed route before preparing the next request. A new request
has a new execution identity; unchanged mission/task identity is permitted.
Cross-execution effects are not automatically exactly-once. The parent must not
reissue an already applied non-idempotent effect merely with a new request ID.

The caller no longer rejects an existing `.tura` directory and never recursively
deletes it. Existing `.tura/run` effect receipts are retained. No new persistent
Tura session database is created by this route. The temporary Native CODEX_HOME
is discarded; only an auth link is passed and credentials are not read by the
bridge. Original Native databases and sessions are not edited.

## Lifetime and output

Native prompt delivery, stdout/stderr consumption and child wait share one
execution deadline. Raw event bytes are hashed incrementally. Diagnostics retain
bounded prefixes while draining to EOF. The caller bounds its final projection
and references complete result/request/terminal artifacts.

Router stops command admission before cancelling and awaiting command tasks,
terminates its owned worker process group and confirms the child is reaped and
the owned group is gone. The group existence probe uses signal zero, not a global
process listing. Observation errors and EPERM cannot masquerade as no process.
Unproven cleanup is an error, never successful completion. Abrupt host/owner loss
is not claimed recoverable without independent evidence.

A completed provider turn is not mission acceptance. Terminals expose
`mission_acceptance=parent_owned` and `continuation_owner=codex`. Provider token
counters are preserved, and missing usage stays unknown. MCP transport completion
is distinct from a tool's `isError` result. No provider-quality or throughput
improvement percentage is inferred from these structural changes.

## Focused verification

Use an external, storage-admitted Cargo target directory and the existing offline
Cargo cache. Build the three entry binaries:

```sh
cargo build --locked --offline -j 4 -p runtime --bin tura_native_codex_worker --bin tura_command_graph -p router --bin tura_router
cargo test --locked --offline -j 4 -p runtime --features business-tests --test native_codex_runner_contract -- --test-threads=1
cargo test --locked --offline -j 4 -p runtime --lib native_codex_runner::tests
cargo test --locked --offline -j 4 -p router --bin tura_router services::command_run::tests -- --test-threads=1
cargo test --locked --offline -j 4 -p tura_path --lib
cargo test --locked --offline -j 4 -p runtime_contract --lib
```

In the existing `codex-collaboration-harness` source, set `PYTHONPATH=src:tests`,
`TURA_ONESHOT_TEST_BIN=<candidate binary directory>` and
`TURA_ONESHOT_TEST_SOURCE=<this source root>`, then run:

```sh
python -B -m unittest -v test_embedded_tura test_embedded_oneshot
```

This tranche passed 117 distinct scoped tests: 22 Python tests (including six
actual compiled Router/Native/MCP/tool process tests with a local fake provider),
15 Native worker/graph integration tests, nine runner unit tests, 16 Router effect
and cleanup tests, 45 path/J-Space tests and ten runtime-contract tests. All listed
runs had zero failures/skips; unrelated filtered tests are not claimed executed.
Source/build verification is not installed caller/image activation or real
provider acceptance. Prior deployment receipts and its unresolved lease remain
historical evidence and are not rewritten by this task.
