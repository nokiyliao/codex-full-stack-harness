# Full-Stack Review Profile

## Preferred Native Stack

The lowest-cost deployment uses Native Codex as the only runtime owner:

```text
parent mission / ordered predicates
  -> Native Codex task persistence and lifecycle
  -> packaged thin Tura Skill + target-bound dispatch policy
  -> native tools and effects
  -> strict native direct-parent callback
  -> parent mission verification
```

This path uses no Tura Gateway, Router, Session DB, provider lifecycle, daemon,
MCP relay, or second callback transport. The Python collaboration core remains
an executable reference for the contract rather than a second production state
machine.

## Optional External-Runtime Review Profile

The public project also preserves an external-runtime profile for portability,
historical provenance, and review of the modified Rust implementation:

```text
parent mission / ordered predicates
  -> MIT collaboration core
  -> MIT Tura adapter contract
  -> public AGPL Tura runtime fork
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
| Tura integration kit | Stable request/envelope mapping and fail-closed adapter behavior | MIT | `src/codex_collaboration_harness/adapters/tura.py`, conformance tests |
| Native Tura role | Bounded execution policy; Native Codex retains persistence, lifecycle, tools, and callback transport | MIT | `skills/tura-kernel`, execution-profile and terminal schemas, resource and distribution tests |
| Modified Tura runtime | Optional external/legacy profile for Gateway, Router, runtime/session lifecycle, commands, receipts, recovery, callback and provider execution | AGPL-3.0-or-later | Public fork and exact `components/tura-runtime.json` identities |
| Internal benchmark projection | Motivation and bounded aggregate engineering observations | Evidence only | Sanitized JSON, integrity manifest, explicit non-causal limitations |

No UTM repository, task corpus, account state, broker surface, raw conversation,
or local runtime database is needed to inspect or run the public synthetic suite.

## Runtime Contribution

The public Tura fork contains 95 maintainer-authored commits after its exact
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
complete generic collaboration cycle plus Tura adapter conformance.

### Runtime source path

```bash
git clone --branch codex/collaboration-runtime-public-v0.1.0 \
  https://github.com/nokiyliao/tura.git
```

Follow the Tura repository's platform build instructions. The full Rust/runtime
build is intentionally not executed by the Python package's CI. A future public
runtime conformance workflow must bind its binary and run receipt to the exact
component commit before claiming installed behavior.

## Non-Claims

The profile does not assert that OpenAI reviewed or endorsed the project, that
the internal benchmark generalizes to other workloads, that the latest Tura
source is installed, or that the in-memory Python core provides distributed
durability. Those remain separate evidence predicates.
