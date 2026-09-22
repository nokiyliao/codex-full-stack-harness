# Nokiy Direct DCF + J-Space ablation

Date: 2026-09-22. Status: isolated candidate implemented and four bounded
real-provider executions completed; no installed runtime or entrypoint change.

## Mission and acceptance

- MISSION: compare the same Nokiy Direct runtime with and without DCF + J-Space.
- FIRST_FALSE_PREDICATE at dispatch: both arms execute with independently checked answers.
- SHORTEST_VALID_ROUTE: two read-only cross-file tasks; CLEAN/FULL then FULL/CLEAN.
- EXPECTED_PREDICATE_DELTA: matched correctness, reported usage, elapsed time and cleanup evidence.
- ABANDON_IF: source drift, sandbox failure, provider failure or uncertain cleanup; no retries.

All four corrected trials completed, matched the frozen exact JSON answer keys,
and reported engine reaping and no live descendants. Post-run source, candidate
asset, installed image and configuration identities matched. The existing
quiescence verifier found no residual engine processes.

## Implementation boundary

`scripts/nokiy_ablation_benchmark.py` reuses the installed v3 caller's `_engine`,
runtime image verification and process supervisor. It does not install anything.
The Rust candidate adds a nondefault `nokiy-ablation-benchmark` feature with a
read-only admission guard. Ordinary builds retain the existing admission path.

Both arms use the same pinned candidate binaries, workspace, prompt per task,
Direct mode, native identity and external Seatbelt policy. They request
`gpt-6-astra`, `high`, `default`. Provider-observed model and tier are unavailable,
not independently verified. Only provider traffic and bounded source reads are
part of the workload; no broker or deployment operations occur.

CLEAN omits both `--task-context-capsule` and `--jspace-contract` and does not load
or validate those inputs in caller preflight. Common request metadata remains;
this is not a metadata-free runtime. FULL uses canonical DCF source-navigation
evidence compiled into a capsule and J-Space contract. The answer keys are not
included in the prompts or projections.

The experimental guard requires the explicit canonical read-only workspace,
existing command sandbox enablement, and permission restrictions enabled. It
also verifies that opening the two source files for writing is denied, without
creating, truncating or writing either file. Both arms use the same external
sandbox. Existing runtime receipts remain writable; source, installed release,
candidate checkout, caller checkout and Codex configuration are protected.

## Results

| Task | Arm | Correct | Wall seconds | Reported total tokens | Tool batches |
| --- | --- | --- | ---: | ---: | ---: |
| digest-versus-domain | CLEAN | yes | 42.094 | 61,572 | 1 |
| digest-versus-domain | FULL | yes | 36.090 | 67,480 | 1 |
| validation-versus-presentation | FULL | yes | 43.285 | 67,652 | 1 |
| validation-versus-presentation | CLEAN | yes | 45.819 | 108,631 | 2 |
| Total | CLEAN | 2/2 | 87.914 | 170,203 | 3 |
| Total | FULL | 2/2 | 79.375 | 135,132 | 2 |

FULL used 20.61% fewer reported total tokens and 9.71% less execution wall time
in this sample. Shared navigation took 0.429 seconds; the two contract/capsule
preparations took 0.839 and 0.602 seconds. Charging all 1.870 seconds to FULL gives
81.245 seconds, about 7.6% below CLEAN. Parent model preparation/review tokens
were not measured, so these are not complete end-to-end productivity costs.

Reported input tokens: CLEAN 169,659; FULL 134,630. Cached input tokens: CLEAN
93,696; FULL 55,040. Subtracting cached from input gives 75,963 versus 79,590,
respectively: FULL has about 4.8% more noncached input. Output tokens were 544
versus 502. Billing and monetary savings are UNKNOWN; total-token reduction
must not be presented as a verified cost reduction.

Every batch executed only the allowed source reads. All command exit codes were
zero. All trials first read the same two files; the second CLEAN task reread
`jspace.py` once. Returned stdout hashes for each source were identical across
arms, including the reread. The presented `jspace.py` output was truncated, so
this does not establish that models saw the whole file. It establishes equal
tool-output presentation between arms.

## Interpretation and limits

This is a successful combined ablation entry and a small positive observation,
not proof of broad coding productivity. The first task actually used more tokens
with FULL; the aggregate improvement is driven by CLEAN's extra tool round trip
on the second task. There are only two known-file read-only tasks and one sample
per arm, no editing or source-discovery tasks, no statistical confidence bound,
and uncontrolled provider caching. The tasks do not measure the independent
contribution of DCF versus J-Space or J-Space's safety effectiveness.

The candidate runtime is a debug build used identically in both arms. Absolute
times are not installed-release benchmarks. The result does not justify changing
production routing, removing controls, or claiming stable 20% savings.

## Verification and failure preservation

Focused tests: five Python driver tests, two feature-gated Rust guard tests and
two default-feature taskcore tests passed (nine total). Coverage includes exact
flag removal, unsafe mode rejection, real read-only sandbox behavior, rejection
without the sandbox, serializable supervisor stderr and unchanged normal gates.

The first v1 attempt failed before the provider call because the sandbox flag
was encoded as `1` while the initial experimental guard expected `true`.
The guard now uses the existing `command_run_sandbox_enabled()` parser. The
outer supervisor's bytes-to-JSON error was also fixed and regression-tested.
That failed identity was preserved, not resent. The corrected v2 protocol pinned
the changed source and candidate before its four executions.

Evidence roots:

- Corrected protocol, four terminal results, argv, tool logs and scores:
  `/Volumes/NOKIY-TB5/UTM/rebuildable_caches/nokiy-direct-ablation-20260922-v2`
- Preserved failed pre-provider attempt:
  `/Volumes/NOKIY-TB5/UTM/rebuildable_caches/nokiy-direct-ablation-20260922-v1`
- Rust source candidate:
  `/Volumes/NOKIY-TB5/UTM/compute_worktrees/tura-oneshot-simplify-20260921-v1`
- Unchanged installed runtime:
  `/Volumes/NOKIY-TB5/UTM/runtime_releases/codex-collaboration-harness/nokiy-20260922-v3`

Each `attempt.json` is exclusive-created to reject accidental reruns. Completed
trial identities must not be reused. No background benchmark is scheduled.
