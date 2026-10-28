# OpenAI Review Packet

## Purpose

This document is a reviewer map for the public repository. It is not an OpenAI
application, submission, endorsement request, or evidence that OpenAI has
reviewed the project.

## Review in 15 Minutes

1. Read the scope and non-goals in [`../README.md`](../README.md).
2. Inspect the closed graph in [`architecture.md`](architecture.md).
3. Compare implementation behavior with [`verification.md`](verification.md).
4. Run the public suite from a clean checkout:

   ```bash
   PYTHONPATH=src python3 -m unittest discover -s tests -v
   ```

   Or run the full repository review contract:

   ```bash
   make check
   ```

5. Review [`trust-boundaries.md`](trust-boundaries.md) and confirm external
   effects remain outside the package.
6. Inspect the preferred thin Native Tura role in
   [`native-tura-role.md`](native-tura-role.md), then inspect the optional
   external Tura integration contract and conformance tests in
   [`tura-integration.md`](tura-integration.md).
   Verify the packaged request/terminal schemas and golden vectors.
7. Inspect the exact public AGPL component identities and maturity split in
   [`full-stack-profile.md`](full-stack-profile.md).
8. Review [`provenance.md`](provenance.md) and scan the complete history for
   private or third-party material.
9. Treat [`internal-benchmark.md`](internal-benchmark.md) only as labeled,
   non-public motivation.

## Contribution Summary

The public contribution is an original, standard-library Python reference
implementation that makes a collaboration control loop explicit:

- parent-owned ordered exit predicates;
- deterministic first-false selection;
- bounded worker packets;
- lease/CAS ownership modeling;
- typed terminal receipt with exact task-lease release;
- same-attempt effect reconciliation without executor replay;
- step-level effect identities, typed blockers, parent-bounded recovery budget,
  and deterministic admission of model-generated recovery proposals;
- exact callback destination and request identity, with authoritative absence
  or convergence proof before recovery;
- callback, receipt, and continuation ACK identities after convergence;
- exact parent-owned `MissionSnapshotReadback`, including a complete truth
  vector and monotonic parent-state sequence;
- parent-authored route disposition and supersession evidence;
- return to parent mission verification;
- a packaged thin Native Tura role that delegates persistence, lifecycle,
  tools, effects, and callback ownership to Native Codex;
- a content-addressed Native execution profile and deterministic compiler for
  official `create_thread` arguments;
- one strict Native terminal marker/schema/parser for mechanical callback
  intake without a second callback service;
- a transport-neutral Tura adapter contract that third parties can implement
  without importing a domain-specific application schema.

It aims to make these semantics small enough to audit, adapt, and test without
requiring a private agent runtime.

## Claim-to-Evidence Table

| Claim | Public evidence | Limitation |
|---|---|---|
| The graph is closed in the reference state machine | Source plus deterministic end-to-end test | One process, in-memory only |
| First-false ownership is explicit | Mission/predicate types and negative tests | Host supplies predicate truth |
| Dispatch is bounded | Immutable task-packet fields and validation | No provider authorization |
| Ownership conflicts fail closed | Lease/CAS and terminal-release tests | Not a distributed lock |
| Unsettled effects do not blindly retry | Same-attempt reconciliation test | Real settlement authority belongs to an adapter |
| Callback requires convergence and stable retry identity | Destination/recovery/proof/ACK tests | No durable or authenticated network transport |
| Unknown callback delivery cannot blindly retry | Delivery-started/unsettled and absence/convergence reconciliation tests | Destination must supply authoritative readback |
| Model recovery cannot expand its authority | Blocker, step ledger, recovery gate, budget, policy escalation, and duplicate-fingerprint tests | Production sandbox and policy remain external |
| Task completion is not mission completion | Worker-claim versus parent-`MissionSnapshotReadback` tests | Integration must supply an authoritative current reader |
| Runtime dependency surface is small | Package metadata and imports | Development/release tooling still requires review |
| Native Tura is a portable Codex role | Packaged exact role bytes, target-bound dispatch compiler with inherited, preferred, and pinned model policies, strict terminal parser, full source/artifact byte parity, and Native ownership documentation | A real Codex deployment must still prove role discovery, task creation, and direct-parent callback |
| External Tura remains an optional implementation route | Public adapter, terminal envelope, fake client, and conformance tests | A real external deployment still supplies transport, durability, credentials, and effect authority |
| The modified runtime is inspectable | Public AGPL fork, exact component manifest, and networked ref/tree/ancestry check | Public source is not installed/running acceptance |
| Internal measurements motivated publication | Labeled aggregate summary | Underlying corpus is not public or reproducible here |

## Reviewer Questions

- Can every accepted transition be correlated to one mission revision, packet,
  lease/CAS claim, terminal receipt, and destination?
- Can stale or mismatched state reach acknowledgement?
- Can a worker or receipt silently complete the parent mission?
- Are duplicate terminalization and acknowledgement observable and rejected?
- Does any public claim exceed what the public fixtures prove?
- Does the repository contain material attributable to external/private systems?
- Is the in-memory limitation unmistakable to an integrator?

## Release Readiness Checklist

- [ ] Clean checkout passes the canonical Python 3.11+ suite.
- [ ] Exact test count and output are captured for the candidate commit.
- [ ] Package metadata, tag, and changelog agree on version and MIT license.
- [ ] CI and release actions are pinned by full commit SHA.
- [ ] Built wheel passes a fresh isolated install and includes protocol resources.
- [ ] Wheel/sdist checksums and GitHub provenance attestation are published.
- [ ] Source archive matches the reviewed commit.
- [ ] Full-history secret and private-data scan is clean.
- [ ] Third-party code and asset inventory is complete.
- [ ] Synthetic end-to-end run uses public bytes only.
- [ ] No benchmark corpus or private receipt is bundled.
- [ ] Security advisory channel is enabled.
- [ ] At least one independent clean-checkout reproduction is recorded.

This is a reviewer verification list, not mutable release state. Current CI,
tag, archive, and artifact evidence belongs to the exact GitHub release so this
source document does not create a self-referential claim about its own commit.

## Current Honest Disposition

The public v0.3.8 candidate is designed for source, artifact, Native dispatch,
callback-contract, and optional component-lineage review. Release status belongs
to the exact Git tag and attached artifacts, not this document. A passing suite
still does not establish installed Tura adoption, a live Codex integration,
production durability, or OpenAI acceptance.
