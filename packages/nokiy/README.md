# Codex Collaboration Harness

[![CI](https://github.com/nokiyliao/codex-collaboration-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/nokiyliao/codex-collaboration-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](pyproject.toml)

`codex-collaboration-harness` is a small, domain-neutral Python reference
implementation for coordinating delegated agent work without transferring the
parent mission to a worker.

The harness models one closed collaboration cycle:

```text
mission -> first false predicate -> bounded task packet -> lease/CAS claim
        -> step/effect ledger -> execution result -> terminal receipt
        -> destination-bound callback with delivery reconciliation
        -> convergence proof -> mission verification or route selection
```

It is intentionally an in-memory reference, not an agent runtime, workflow
service, sandbox, durable queue, or authorization system. Integrators provide
the actual worker, persistence, effect, and callback adapters.

The preferred Codex deployment profile packages Tura as the explicitly invoked
[`$tura-kernel` Skill](src/codex_collaboration_harness/skills/tura-kernel/SKILL.md).
The parent creates a first-class Native Codex task; Native Codex remains the sole
owner of task persistence, tools, provider turns, and restart recovery, while
the Skill contributes bounded execution policy and returns one official
`send_message_to_thread` callback. The older
[`agents/tura.toml`](src/codex_collaboration_harness/agents/tura.toml) remains a
compatibility resource rather than the operational dispatch baseline. See
[`docs/native-tura-role.md`](docs/native-tura-role.md) for the topology and
drift-safe installation procedure.

Ordinary Native Tura dispatch can inherit the current Native Codex model and
reasoning setting, or request a `preferred` pair for its first turn without
binding later turns to it. Explicit benchmark or reproduction profiles may pin
both values; `thinking="max"` is their default and highest admitted Tura effort.
The Skill does not advertise `ultra` as a Tura tier.

The package also retains an optional, transport-neutral
[`TuraAdapter`](src/codex_collaboration_harness/adapters/tura.py). It turns the
generic task/lease contract into a bounded Tura request and maps a third-party
Tura terminal envelope back into the core result/failure model. No private
endpoint, credential, UTM shape, or Tura runtime source is required by the
Python package.

For review of the historical external-runtime profile, the repository binds the
[public AGPL runtime fork](https://github.com/nokiyliao/tura)
through [`components/tura-runtime.json`](components/tura-runtime.json). The
manifest deliberately separates the public source ref, the benchmarked
candidate, and any installed/running claim. See
[`docs/full-stack-profile.md`](docs/full-stack-profile.md).

## Why It Exists

Agent collaboration often treats a completed child task as proof that the
parent goal is complete. That shortcut loses mission ownership, makes retries
ambiguous, and can duplicate effects. This project makes the missing control
edges explicit and testable:

- the parent owns an ordered list of measurable exit predicates;
- only the first false predicate is eligible for work;
- a task packet carries a bounded route and abandon condition;
- a lease and compare-and-swap version protect the declared scope;
- a worker result is converted into a typed terminal receipt that releases the
  exact task lease;
- an unsettled effect can be reconciled on the same attempt without executing
  the worker again;
- typed blockers can receive model-generated recovery proposals, but a
  deterministic gate enforces the parent packet's scope, authority, effect,
  destination, predicate, verification plan, and recovery budget;
- continuation targets an exact destination and request identity; a delivery
  that may have committed cannot retry until an authoritative absence or
  convergence proof reconciles that same request;
- callback, receipt, and continuation ACK identities are created only after
  convergence;
- every mission verification consumes a complete parent snapshot with a
  monotonic state sequence, so older truth cannot overwrite newer truth;
- route disposition and supersession remain parent-authored evidence rather
  than worker authority;
- control returns to parent mission verification or route selection.

## Quick Start

Requirements: Python 3.11 or newer. Runtime code uses only the Python standard
library.

```bash
git clone https://github.com/nokiyliao/codex-collaboration-harness.git
cd codex-collaboration-harness
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

The test suite is the canonical executable contract. It includes a deterministic
in-memory end-to-end cycle and negative or recovery cases for stale mission/CAS
state, duplicate dispatch, overlapping leases, pre-execution rejection,
unsettled effects, callback mismatch/reconciliation, stale parent snapshots,
bounded recovery, route disposition, missing convergence, and replay. It
uses no network, credentials, private corpus, or external agent service.

Run only the complete public cycle with:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_harness.HappyPathTests.test_complete_cycle -v
```

Until a release is installed, the source-tree equivalent is:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

There is no runtime daemon or lifecycle service in the reference package. The
only CLI, `tura-taskpacket`, installs or verifies the packaged Skill and renders
an immutable Native Tura task capsule. Its `--format dispatch` view is a compact
ready-to-send Native task with task-local evidence already projected; it does
not create sessions, dispatch workers, or write callbacks. A target-bound
capsule can be compiled into exact official `create_thread` arguments with:

```bash
tura-taskpacket prepare-dispatch --task-name /root/example_task
```

The resulting dispatch-plan v4 JSON always binds the selection policy, target,
Skill contract, prompt digest, callback identity, and parent thread. A
`preferred` profile supplies initial model and reasoning arguments while
allowing later official Native turns to override them. A pinned profile binds
the exact pair for reproducibility; an inherited profile omits both from
`create_thread` so Native Codex selects them. It also
reports exact UTF-8 byte counts for the dispatch, projected task context, and
J-Space policy plus the inline evidence-reference count. The Commander still
performs the official task creation. A reviewed Skill update can be adopted
atomically with `tura-taskpacket install-skill --replace`; without `--replace`,
any target drift continues to fail closed.

Audit an existing packet root without migrating or dispatching anything with:

```bash
tura-taskpacket inspect-packets
```

The inventory separates current target-bound packets from readable historical
packets and exact rejected members. Historical digest-only v1 filenames remain
readable, but they still fail `prepare-dispatch` because they have no execution
profile. Add `--summary` when only the counts, total, root, and full inventory
digest are needed; this avoids carrying the per-packet rows into a callback turn.

Capsules whose scopes are all explicitly prefixed with `read:` receive the
`NATIVE_TURA_READ_ONLY_FAST_PATH_V1` and
`NATIVE_TURA_FAST_PATH_EXECUTION_V3` markers. The inline fast-path contract is
self-contained, so those tasks do not spend a Native tool continuation reading
the Skill file. They obtain
`CODEX_THREAD_ID` in the same first batch as every task read, proceed directly
to the single terminal callback, and emit only a fixed delivery acknowledgement
after tool success. The compiler embeds the exact two-line canonical terminal
template so the worker cannot drift to a historical marker while source/test
discovery is disabled. The fast-path command contract also avoids zsh
special-parameter collisions and requires hidden-root-aware file enumeration.
The terminal marker plus canonical JSON is limited to 65,536 UTF-8 bytes and 32
evidence items; large logs and artifacts travel as immutable references and
digests rather than inline callback content.

Start with the public objects exported by `codex_collaboration_harness`; see
[`docs/verification.md`](docs/verification.md) for the exact verification
contract and [`docs/architecture.md`](docs/architecture.md) for the lifecycle.

The primary API is `CollaborationHarness`. Its explicit stages include `plan`,
`claim`, `execute`, effect reconciliation, continuation/ACK, and
`verify_mission`; `run` composes one complete settled cycle. The worker's
`predicate_satisfied` field is advisory. Only an exact parent-owned
`MissionSnapshotReadback` may change mission truth. `Executor` and
`ContinuationTransport` are adapter protocols. All public evidence records are
immutable dataclasses, and `InMemoryStore` is a deterministic reference store
rather than production persistence.

For a repository-level review, including provenance, public-data hygiene,
tests, the synthetic demo, and import smoke check, run:

```bash
make check
```

For the complete release predicate, including removal of reproducible packaging
state, deterministic sdist metadata, two byte-identical builds,
source/wheel/sdist parity, isolated
installation, CLI smoke test, and checksums, run:

```bash
make release-check
```

Networked verification of the public Tura component ref, exact Git trees,
ancestry, license, and modification notice is deliberately separate:

```bash
make check-components
```

The packaged protocol schemas and cross-language golden vectors live under
[`src/codex_collaboration_harness/protocol`](src/codex_collaboration_harness/protocol).
The external-runtime request and terminal envelopes are bound to
`protocol_version=tura-collaboration/v1`. The preferred Native profile also
packages schemas for the task projection, execution profile, and exact
`[TURA_NATIVE_TERMINAL_V1]` callback.

## What Is Verified

The public suite verifies the state-machine and ownership invariants implemented
by this repository. A passing run means the in-memory reference behaved as
specified for those fixtures. It does **not** prove:

- durable or distributed lease safety;
- cryptographic receipt authenticity;
- exactly-once delivery across process or network failures;
- correct authorization for real tools or irreversible effects;
- isolation of an external worker;
- compatibility with every Codex or third-party runtime;
- the results of the non-public internal benchmark described in this repo.

Read [`docs/trust-boundaries.md`](docs/trust-boundaries.md) before adapting the
reference to production effects.

## Review Map

| Reviewer question | Public evidence |
|---|---|
| What is the collaboration graph? | [`docs/architecture.md`](docs/architecture.md) |
| How is Tura installed as a thin Native Codex role? | [`docs/native-tura-role.md`](docs/native-tura-role.md) |
| What constitutes the reviewable full stack? | [`docs/full-stack-profile.md`](docs/full-stack-profile.md) |
| Which boundaries are enforced here? | [`docs/trust-boundaries.md`](docs/trust-boundaries.md) |
| What can I reproduce? | [`docs/verification.md`](docs/verification.md) |
| What is not claimed? | [`docs/limitations.md`](docs/limitations.md) |
| How can I connect a Tura runtime? | [`docs/tura-integration.md`](docs/tura-integration.md) |
| Where did the implementation come from? | [`docs/provenance.md`](docs/provenance.md) |
| What do the internal measurements mean? | [`docs/internal-benchmark.md`](docs/internal-benchmark.md) |
| What should an OpenAI reviewer inspect? | [`docs/openai-review-packet.md`](docs/openai-review-packet.md) |
| How are changes and reports handled? | [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), [`GOVERNANCE.md`](GOVERNANCE.md) |

## Project Status

This is an early reference implementation. The API may change before a stable
release. Source acceptance, a packaged candidate, installed adoption, and a
real runtime integration are separate states and must not be inferred from one
another.

## Provenance and Affiliation

The code in this repository is an original, from-scratch, domain-neutral
reference implementation. It is informed by prior work with collaboration
runtimes and evidence systems, but it does not contain private task corpora,
raw conversations, local session identities, protected runtime receipts, or
source copied from those systems. See [`docs/provenance.md`](docs/provenance.md).

This is an independent project. It is not an official OpenAI product and is not
endorsed by OpenAI. "Codex" identifies the intended collaboration context; no
OpenAI source code is included.

## License

MIT. See [`LICENSE`](LICENSE).
