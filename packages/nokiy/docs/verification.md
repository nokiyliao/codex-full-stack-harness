# Verification Contract

## Canonical Command

From an installed editable checkout:

```bash
python3 -m unittest discover -s tests -v
```

Without installing the package:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The supported baseline is Python 3.11 or newer. Runtime code is standard-library
only. Verification must exit zero with no failing tests. Test count and exact
names are reported by the command and should be included in release evidence;
this document does not freeze a count that could silently become stale.

To run only the deterministic complete cycle:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_harness.HappyPathTests.test_complete_cycle -v
```

For the repository-level review contract, run:

```bash
make check
```

`make check` runs the review-readiness scan, evidence-manifest integrity check,
full unit suite, synthetic demo, and import smoke test. Test counts belong to
the exact run receipt and are intentionally not frozen in this document.
Packaging is a separate predicate: `make release-check` removes reproducible
`dist/`, `build/`, and `*.egg-info` state, normalizes sdist container metadata,
builds both artifacts twice under one `SOURCE_DATE_EPOCH`, verifies identical
digests and source parity, installs the
wheel in an isolated environment, and writes checksums.

## Public End-to-End Scenario

The deterministic suite exercises this complete in-memory path:

```text
parent mission with ordered predicates
  -> select first false predicate
  -> construct bounded packet
  -> acquire scoped lease/CAS claim
  -> accept typed worker result
  -> create one terminal receipt
  -> target exact parent destination
  -> verify exact convergence
  -> acknowledge once
  -> obtain exact parent-owned MissionSnapshotReadback
  -> re-evaluate parent mission
```

All identifiers and payloads are synthetic. The run requires no network,
provider account, external agent, secret, private evidence, or installed local
runtime.

## Invariant Matrix

The suite count is discovered from the current source rather than frozen in
documentation. Its direct evidence is:

| Test area | Direct observation |
|---|---|
| Complete cycle | First false predicate and lowest-ranked route are selected; one settled effect, terminal receipt, exact destination, three ACK identities, and terminal `MissionVerification` are produced deterministically |
| Predicate order | An earlier false predicate wins even when a later route has a numerically better rank |
| Overlapping ownership | A second mission cannot claim an overlapping active scope |
| Pre-execution rejection | A typed blocked terminal receipt is written and the exact lease is released without an executor call |
| Unsettled reconciliation | One attempt/effect is retained, executor retry is blocked, and settlement terminalizes the same identity with an executor call count of one |
| Step-effect terminal guard | An unresolved step effect cannot create a receipt or release its lease; terminalization succeeds only after exact step reconciliation |
| Executor result boundary | A non-`ExecutionResult` return becomes a harness-origin typed failure with its lease retained for explicit reconciliation |
| Callback target | A wrong proof destination creates no ACK or mission verification; the terminal task lease remains released |
| Callback recovery | Delivery start becomes `DELIVERY_UNSETTLED` on uncertain outcome; retry requires an exact authoritative absence proof, while committed delivery requires its exact convergence proof |
| Identical replay | Replay after acknowledgement returns empty effect, continuation, and acknowledgement tuples |
| Changed replay | Reusing an acknowledged packet with a changed identity is a typed conflict |
| ACK cardinality | Callback, receipt, and continuation ACK identities are emitted once under one acknowledgement |
| Mission return | A terminal child outcome returns to parent `MissionVerification` and selects completion or route selection there |
| Parent readback authority | A true worker claim cannot complete a false parent predicate; a true parent readback can complete it despite a false worker claim |
| Readback identity | A complete ordered snapshot is required; stale parent sequences and conflicting content at the same sequence are rejected |
| Recovery authority | Only diagnostic/plan blockers with reconciled effects, subset scope, unchanged parent fields, verification plan, safe effect class, and available parent budget are admitted |
| Recovery execution binding | A recovery step references the exact admitted proposal action and matches its operation digest and effect class |
| Recovery replay | Repeating the same blocker/state/action fingerprint is a no-op rather than a second effect |
| Packet identity | A tampered content-addressed packet cannot acquire a lease |
| Failure authority | Executor and harness failure origins remain distinct through terminal receipt reconciliation |
| Step effects | Unsettled step identity must reconcile exactly before local recovery is considered |
| Route disposition | Parent-disposed routes are excluded and deterministic fallback selection occurs |
| Mission supersession | Old ACK evidence can be classified without mutating newer mission truth |
| Stale mission | A superseded mission revision is rejected before lease or execution |
| Duplicate dispatch | A second claim for the same packet is rejected |
| Stale CAS | A scope snapshot captured before a prior terminalization is rejected |
| Revision-drift closeout | A claimed packet whose mission revision changes before execution terminalizes the exact prior lease without running the executor |
| Failure reconciliation | Executor exceptions and invalid result identities require explicit `NONE` or `SETTLED` proof before terminalization; the executor is not rerun |
| Concurrent claim | Two synchronized threads competing for one scope produce exactly one lease and one typed conflict |
| Missing convergence | An incomplete destination proof creates no ACK |
| Scanner exclusions | Repo-local virtual-environment variants are excluded as build state |
| Packaging metadata exclusion | Generated `*.egg-info` metadata is excluded while authored `src/` remains scanned |
| Scanner inclusion | Public source remains inside the review-readiness scan boundary |
| Tura adapter success | A bounded task/lease maps deterministically to a Tura request and a settled terminal envelope maps to the core result |
| Tura wire contract | Packaged request/terminal schemas and golden vectors bind the exact protocol version and request digest |
| Tura unsettled effect | The exact effect identity remains unsettled for core reconciliation rather than being retried |
| Tura transport/terminal failure | Transport exceptions and explicit terminal failures become deterministic typed failures |
| Tura identity closure | Packet, lease, request, and executor drift is rejected before becoming a core result |
| Tura/core failure composition | Execution failures and typed rejections preserve the exact failure code, detail digest, and observed effect across the adapter-to-core boundary; a second execution is blocked until explicit `NONE` or `SETTLED` reconciliation |
| Native Tura task bootstrap | A canonical Native task name resolves the unique highest immutable capsule revision; TaskPacket, callback, revision, content digest, file mode, link count, duplicate-key, tamper, traversal, stale-revision, and same-revision conflict checks fail closed before execution. Historical digest-only v1 filenames are readable but remain unprofiled and non-dispatchable. |
| Native packet inventory | Read-only inspection classifies every revision as `CURRENT_PROFILED`, `LEGACY_READABLE`, or `REJECTED` and binds the complete deterministic result to one digest without migration. |
| Native execution profile | `inherit`, mutable initial `preferred`, and reproducible `pinned` policies compile with the official task target, Skill-contract digest, prompt digest, exact UTF-8 size metrics, and callback identity into one deterministic `create_thread` plan; unsupported pinned `ultra` is rejected rather than downgraded. |
| Native callback intake | Only the exact marker plus canonical terminal JSON is accepted; callback, parent, and task identity mismatches fail before mission intake, callbacks over 65,536 UTF-8 bytes fail, and evidence is capped at 32 items. |
| Skill adoption | Identical installation is a no-op, drift is rejected by default, and explicit replacement records the preimage and verifies the new target atomically |
| Artifact/source parity | Wheel and sdist package member sets and bytes exactly equal the source package, and the installed wheel exposes the same Skill and protocol resources |

The implementation has additional structural validation, and future paths may
need new dedicated fixtures. Public evidence is limited to the exact tests and
source bound to the reviewed run.

## Reproduction Record

A reviewer or release operator should record:

```text
source commit:
python implementation/version:
command:
tests run:
failures/errors/skips:
exit code:
```

Do not label the result "installed runtime verified" unless the installed
artifact identity and the actual adapter behavior were separately read back.

## What Passing Establishes

- the checked source tree imports and runs on the tested interpreter;
- the public in-memory fixtures satisfy the implemented transition contract;
- the tested negative paths fail closed.

## What Passing Does Not Establish

- package publication or installed adoption;
- production durability, distributed concurrency, or crash recovery;
- external worker correctness or isolation;
- authenticated callback delivery;
- permission for external effects;
- benchmark performance or universal productivity improvement.

## Release Artifact Verification

`make release-check` removes stale reproducible packaging output, runs the
complete source contract, builds exactly one wheel and one sdist twice, and
requires both builds to have identical artifact names and SHA-256 values. Every
packaged source member must also appear byte-for-byte in both artifact formats.
It then installs the wheel in a fresh isolated virtual environment with no index
or dependencies, checks installed metadata/imports and the Native CLI outside
the repository, runs `pip check`, and writes `dist/SHA256SUMS`. The tag-triggered release
workflow additionally requires an annotated tag matching `pyproject.toml` and
emits GitHub build provenance for those checksums.

`make check-components` is a separate networked predicate that verifies the
optional public Tura branch, exact commit trees, ancestry, license, and
modification notice. It runs in a scheduled/manual component-conformance
workflow rather than gating Native CI or release. Its success establishes source
lineage only, never installed/runtime acceptance.
