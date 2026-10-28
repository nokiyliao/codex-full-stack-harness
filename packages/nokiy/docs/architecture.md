# Architecture

## One Mission, One Owner

The harness keeps the parent mission authoritative. A worker may change an
observation about one predicate; it cannot replace the mission, reorder its
predicates, or declare the parent complete.

```mermaid
flowchart LR
    O[Operator or host] --> M[Mission and ordered predicates]
    M --> F[First false predicate]
    F --> R[Shortest valid route]
    R --> P[Bounded TaskPacket]
    P --> L[Lease and CAS claim]
    L --> W[External worker adapter]
    W --> S[Step attempts and typed blocker]
    S --> Q[Deterministic recovery admission]
    Q --> E[ExecutionResult]
    E --> T[TerminalReceipt]
    T --> C[Destination-bound continuation]
    C --> G[ConvergenceProof]
    G --> A[Callback, receipt, and continuation ACK identities]
    A --> B[Parent-owned MissionSnapshotReadback]
    B --> V[MissionVerification]
    V -->|all predicates true| D[Mission complete]
    V -->|predicate still false| F
    V -->|route abandoned| R

    X[External persistence and effects] -. integration boundary .-> L
    X -. integration boundary .-> W
    X -. integration boundary .-> C
```

Dashed edges are deliberately outside this package. The reference store and
worker are in-process test doubles for their contracts.

## Preferred Native Codex Deployment

The packaged Native Tura role removes the external lifecycle boundary from a
Codex deployment:

```text
operator / parent mission
  -> Native Codex task runtime
       owns persistence, child lifecycle, tools, effects and callback
  -> Tura agent role
       contributes first-false routing and bounded execution policy only
  -> Native Codex direct-parent callback
  -> parent mission verification or route selection
```

The role is selected explicitly for a child task; ordinary Codex roles do not
inherit it. It does not start a Gateway, Router, Session DB, provider runtime,
daemon, queue, MCP relay, or second callback transport. This is a single-owner
architecture: Tura changes executor behavior without acquiring storage,
lifecycle, or thread ownership.

The external worker graph above remains the portable reference contract and an
optional compatibility profile. It is not a runtime dependency of the Native
Codex role.

## Decision Record

Every delegated packet preserves five fields:

| Field | Meaning |
|---|---|
| `mission` | The parent objective and ordered exit predicates |
| `first_false_predicate` | The only predicate eligible for the current action |
| `shortest_valid_route` | The selected bounded way to change that predicate |
| `expected_predicate_delta` | The measurable state change expected from success |
| `abandon_if` | The condition that ends this route instead of creating a recursive mission |

The exact Python field names are defined by the public dataclasses. The
five-part decision record is a control contract, not a prompt-writing style.

## Lifecycle

1. **Select.** Read the mission in declared order and select its first false
   predicate. A mission with no false predicate is already complete and cannot
   dispatch more work.
2. **Route.** Choose one bounded route. The reference accepts an explicit route;
   it does not rank arbitrary real-world tools or grant their authority.
3. **Packetize.** Create a task packet bound to the mission revision, predicate,
   route, expected delta, abandon condition, declared scope, and parent recovery
   budget.
4. **Claim.** Acquire the scope in the reference store and record the CAS
   version. A stale version or overlapping live claim fails closed.
5. **Execute.** Pass the packet to an adapter. The adapter returns a typed
   result; it never edits the mission directly.
6. **Recover or terminalize.** A typed diagnostic/plan blocker may receive a
   model-generated proposal, but only a deterministic gate can admit work
   inside the existing packet's scope, authority, effects, destination,
   predicate, verification plan, and budget. Each effect-bearing step retains
   an exact identity. Then reconcile result and ownership into one terminal receipt,
   then release the exact task lease. An unsettled effect retains its recorded
   attempt and lease until `EffectReconciliation` settles that same effect;
   reconciliation must not call the executor a second time.
7. **Continue.** Prepare one destination-bound continuation and deterministic
   turn request from the terminal receipt. Once delivery starts, any uncertain
   outcome remains `DELIVERY_UNSETTLED`; retry is forbidden until an exact
   authoritative absence proof or convergence proof reconciles that request.
   Human-readable prose is not a routing identity.
8. **Prove convergence.** Confirm that the destination observed the exact
   terminal result. Without this proof, acknowledgement is forbidden.
9. **Acknowledge.** Atomically bind callback, receipt, and continuation ACK
   identities. The task lease was already released at terminalization. A replay
   cannot generate a new acknowledgement or effect.
10. **Verify mission.** Obtain a complete parent-owned
    `MissionSnapshotReadback` with a monotonic parent-state sequence, then
    re-read every ordered predicate from that same snapshot.
    Complete only when all are true; otherwise select the current first false
    predicate or another valid route. A worker result is advisory here.

## Data Ownership

| Object | Owner | What it may establish |
|---|---|---|
| `Mission` and `ExitPredicate` | Parent host | Objective, order, current truth, revision |
| `Route` | Parent decision layer | Selected bounded path and declared scope |
| `TaskPacket` | Parent dispatch layer | Immutable child contract |
| `Lease` | Store adapter | Temporary ownership and CAS precondition |
| `ExecutionResult` | Worker adapter | Claimed outcome of one attempt |
| `StepAttempt` and `BlockerReport` | Worker/store boundary | Exact sub-effect evidence and a typed obstruction, never new authority |
| `RecoveryProposal` | Model solver | Candidate method only; confidence is not authority |
| `RecoveryAdmission` | Store policy | Whether one proposal remains inside the original packet envelope |
| `EffectReconciliation` | Effect/recovery adapter | Settlement of the already-recorded attempt without re-execution |
| `TerminalReceipt` | Harness/store boundary | Accepted terminal state and release of the exact task lease |
| `Destination` | Parent continuation layer | Exact callback target |
| `Continuation` | Harness/store boundary | Stable callback/outbox identity independent of the task lease |
| `ContinuationDeliveryAbsenceProof` | Destination authority adapter | Exact proof that the same turn request was not committed |
| `ResumeProof` and `ConvergenceProof` | Destination adapter | Evidence that the exact destination resumed and observed the result |
| `Acknowledgement` | Harness/store boundary | Callback, receipt, and continuation ACK identities after convergence |
| `MissionSnapshotReadback` | Parent host | Complete authoritative truth vector and monotonic parent-state sequence |
| `MissionVerification` | Parent host | Next predicate or actual mission completion |

No child-owned object grants permission to mutate external systems.

## Success Definition

Task success and mission success are different predicates:

- **task terminal:** one packet has an accepted terminal receipt and its task
  lease is released;
- **continuation converged:** the exact destination has the exact terminal
  result and can be acknowledged;
- **mission complete:** after convergence, all ordered exit predicates read
  true in parent-owned state.

The harness closes the graph only when these distinctions remain observable.
