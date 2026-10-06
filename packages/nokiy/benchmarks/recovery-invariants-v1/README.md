# Paired long-task fixture: generic recovery invariants

This fixture replays a real public-source repair, not a synthetic one-question
probe. Both arms start from Git commit
`8c7411de47e8ec2456619b7251427d7ce8062d9f` without `.git`. The held-out
quality test is the exact `tests/test_harness.py` blob from the historical fix
commit `f906c7eab5cc1772cbe946574d599fd5631eee16`. The source repository's
working tree is not copied, so its current dirty changes are irrelevant.

This task is no longer a fresh held-out benchmark for Nokiy. The existing
`nokiy-long-task-pair-20260924-v1` protocol used the same task SHA-256
`47ac9e8b529fcf5ed4c06ca49245f2806f6270901f8de8b74f2c0873337b6335`
and held-out test SHA-256
`301daced893d29f881fc202fd794a2610e6deef2acd46e670273c94e383b79e7`.
Use this fixture for offline scorer regression only; a new output directory
does not restore blinding or make another paid pair a causal comparison.

## Offline preparation

From this repository, choose one explicit model and effort for both arms:

```sh
python3 scripts/paired_recovery_fixture.py prepare \
  --output /absolute/new/pair-directory \
  --model gpt-6-astra --effort high
```

This creates `native/` and `nokiy/` workspaces, each with the same source and
`BENCHMARK_TASK.md`, plus an operator-side `protocol.json`. It makes no model
call. Preserve the printed `protocol_sha256` outside both arm workspaces. The
verifier is **not** copied into either workspace during preparation.
Keep each executor's read scope to its own workspace and its write scope to
`src/codex_collaboration_harness/core.py`; exclude the original repository,
other arm, protocol, and historical Git objects. Do not dispatch retired Tura
entrypoints. If that read isolation cannot be enforced on both surfaces,
record an isolation failure instead of treating the result as a clean pair.

Before any paid run, freshly verify the installed Nokiy executable, version,
runtime-image bytes, selected model/effort, and the Native Codex effective
settings. Run each arm at most once, with the same explicit model and effort,
the exact copied task prompt, a 20-minute wall limit per arm, and no broad
effect authority. Stop on uncertain completion, sandbox failure, or source
drift; never retry the same attempt identity. The protocol states these limits
but this offline script does **not** enforce provider cost or runtime limits.

## Offline quality scoring

Only after both arm attempts have ended:

```sh
python3 scripts/paired_recovery_fixture.py score \
  --protocol /absolute/new/pair-directory/protocol.json \
  --protocol-sha256 <sha256 printed by prepare> \
  --native-reviewed-core-sha256 <reviewed native core sha256> \
  --nokiy-reviewed-core-sha256 <reviewed nokiy core sha256>
```

Before scoring, an operator must inspect each arm's `core.py` changes for
unexpected imports, verifier manipulation, or effects outside the task, then
record that exact file's SHA-256. A missing or mismatched reviewed digest
rejects the arm before importing candidate code. The scorer cannot authenticate
who performed the review; supplying an unreviewed digest defeats this gate.

The scorer fetches the pinned original and held-out test blobs from local Git
objects, verifies their SHA-256, and runs both suites against each arm's source
with Python's isolated import mode inside a Darwin Seatbelt process. It fails
closed if that OS sandbox is unavailable. The verifier process receives a
minimal environment without host credentials, cannot use the network or fork,
cannot read file contents outside its arm and Python/system runtime roots, and can write only
to a disposable scratch directory inside that arm (plus `/dev/null` and stdio).
It also rejects changes to tracked files outside `core.py`, any unexpected
files or directories, missing files, symlinks, or a changed prompt.
The historical unfixed source fails the held-out suite; the historical fix
passes it. Scoring is functional and bounded, with no provider, network,
broker, deployment, or UTM activity.
The verifier is independent of the two arm executions, but it was authored
with the historical repair; this is not a blinded third-party review. Candidate
code still executes in the test interpreter, so this is not adversarial-proof
oracle authentication. A source review and test-count check reduce obvious
forgery, but do not establish that arbitrary malicious code cannot fake a pass.

The quality verdict is not an efficiency claim. Preserve actual start/end,
installed runtime identity, effective configuration, provider-reported usage
and independent terminal receipts separately. Feed those exact artifacts to
`scripts/paired_long_task_eval.py` only when its required inputs exist; do not
fill missing usage or promote its `EVIDENCE_INCOMPLETE` result from this score.
One pair has no causal or general productivity claim.
