# Codex Collaboration Harness Specification

Status: Draft v0.1
License: MIT
Scope: Domain-neutral reference protocol

## 1. Purpose

This specification defines an evidence-bound collaboration loop for coding
agents. One Commander owns the mission. Executors receive bounded contracts,
produce durable terminal receipts, and return results to the exact Commander
continuation. Task completion is an observation; only the Commander can decide
whether the parent mission is complete.

The protocol is model-, provider-, repository-, and business-domain neutral.
It does not prescribe a project tracker, browser controller, cloud service,
trading system, or private application schema.

## 2. Goals

1. Preserve one authoritative mission and ordered exit predicates.
2. Work only on the first false predicate.
3. Bind every execution to a bounded task contract and ownership claim.
4. Separate execution success, durable terminalization, callback delivery,
   convergence, and acknowledgement.
5. Make retries deterministic and reject ambiguous effects.
6. Return every child outcome to mission verification or route selection.
7. Permit provider and evidence adapters without giving them mission authority.

## 3. Non-Goals

- A general-purpose workflow engine.
- A distributed scheduler or message broker.
- A replacement for a coding model or its sandbox.
- A policy for live, financial, deployment, or other protected effects.
- A requirement to copy raw conversation history between workers.
- A guarantee that every failed task is retryable.

## 4. Roles

### 4.1 Operator

Defines the mission, terminal outcome, and any effect authority.

### 4.2 Commander

Owns mission state, ordered predicates, route selection, task dispatch, and
terminal acceptance. There is exactly one Commander per mission revision.

### 4.3 Executor

Implements one bounded task contract. An Executor cannot redefine the parent
mission, grant itself effects, or mark the parent mission complete.

### 4.4 Continuation Transport

Delivers a terminal packet to the exact destination Commander identity and
returns a convergence proof. Transport success alone is not acknowledgement.

### 4.5 Evidence Adapter

Projects immutable or read-only evidence for inspection. Evidence adapters do
not own leases, effects, task completion, or mission state.

## 5. Core Records

### 5.1 Mission

A Mission contains:

- `mission_id`
- `revision`
- ordered `exit_predicates`
- `mode`
- candidate routes
- exact callback destination

The first predicate whose value is false is the only current predicate.

### 5.2 Task Contract

A Task Contract contains:

- parent mission identity and revision
- current predicate
- shortest valid route
- expected predicate delta
- abandon condition
- bounded read/write/effect scope
- executor/provider selection
- deterministic contract digest

Missing or ambiguous contract fields fail before execution.

### 5.3 Lease Claim

A Lease Claim binds:

- task contract digest
- declared scope
- compare-and-swap preimage revision
- terminal-receipt release condition

Overlapping claims fail closed. Non-overlapping claims may proceed concurrently.

### 5.4 Execution and Effect Records

An Execution Result or Failure binds one attempt to:

- canonical effect identity
- task packet and lease identity
- executor identity or typed failure
- settled, unsettled, or proven-none state
- result digest or typed ambiguity
- reconciliation proof when the attempt did not settle synchronously

An accepted or possibly accepted effect without a reconciled result is
`UNSETTLED`. It must not be blindly retried.

### 5.5 Terminal Receipt

A Terminal Receipt binds:

- task, mission revision, executor, and lease identity
- terminal status
- result or typed blocker digest
- effect reconciliation summary

Terminalization releases the exact lease. It does not acknowledge callback
delivery or complete the parent mission.

### 5.6 Continuation

A Continuation binds:

- exact destination Commander identity
- terminal receipt identity, which transitively binds the task and mission
- deterministic `turn_request_id`
- state: `PREPARED`, `DELIVERY_STARTED`, `DELIVERY_UNSETTLED`, or
  `ACKNOWLEDGED`

### 5.7 Mission Snapshot Readback

A Mission Snapshot Readback is parent-owned evidence containing the exact
mission revision, a monotonic parent-state sequence, the complete ordered
predicate truth vector, and evidence digest. Older sequences and conflicting
content at the same sequence fail closed. A worker's `predicate_satisfied`
value remains advisory and cannot complete the mission.

### 5.8 Blocker and Recovery Records

`StepAttempt` records each operation, precondition, tool, result, effect class,
effect identity, and effect state. `BlockerReport` classifies an obstruction
without terminalizing the task. A recovery step also binds its parent step, the
exact `RecoveryAdmission(ADMITTED)`, and one action from that admission's
proposal; its operation digest and effect class must match the admitted action.
A model may produce a `RecoveryProposal`, but only an admitted proposal
authorizes a local method change. Admission
requires a diagnostic or plan blocker, exact packet/lease binding, unchanged
predicate/destination/authority, subset scope, reconciled prior effects, a
verification plan, no new protected effect, and available parent packet budget.

## 6. State Machines

### 6.1 Task

```text
PLANNED
  -> CLAIMED
  -> EXECUTING
EXECUTING
  -> BLOCKED_FOR_LOCAL_RECOVERY -> EXECUTING
  -> TERMINAL
  -> CALLBACK_DELIVERY_STARTED
  -> ACKNOWLEDGED
```

Any pre-execution rejection terminalizes as a typed blocker without inventing
an execution result. A begun attempt that fails or returns an invalid identity
remains unresolved and keeps its lease until explicit failure reconciliation
proves `NONE` or `SETTLED`; it then terminalizes without a second execution.
An executor return that is not an exact `ExecutionResult` becomes a
harness-origin execution failure rather than leaking a runtime exception. No
terminal path may release a packet lease while a persisted step effect remains
`UNSETTLED`.

### 6.2 Continuation

```text
PREPARED
  -> DELIVERY_STARTED
  -> DELIVERY_UNSETTLED | ACKNOWLEDGED
DELIVERY_UNSETTLED
  -> PREPARED | ACKNOWLEDGED
```

- `PREPARED`: terminal payload and target identity are durably bound.
- `DELIVERY_STARTED`: the destination mutation may begin under the exact
  `turn_request_id`.
- `DELIVERY_UNSETTLED`: delivery may have committed and cannot be retried.
  An authoritative absence proof reopens the same request as `PREPARED`; an
  exact convergence proof moves it directly to `ACKNOWLEDGED`.
- `ACKNOWLEDGED`: the Commander accepted the observation and persisted its next
  mission revision or terminal decision.

Replaying an acknowledged continuation is an empty operation. A payload or
target identity mismatch is a conflict, never a new delivery.

### 6.3 Mission

```text
ROUTE_SELECTION
  -> TASK_DISPATCH
  -> MISSION_VERIFICATION
  -> COMPLETE | ROUTE_SELECTION
```

The mission becomes complete only when every ordered exit predicate is true and
the terminal readback matches the current mission revision.

## 7. Reference Protocol

1. Read the current Mission and calculate its first false predicate.
2. Select the shortest legal route that respects authority and ownership.
3. Compile a deterministic Task Contract.
4. Claim the exact scope through lease/CAS.
5. Execute once under the bounded contract.
6. Reconcile all accepted or possibly accepted effects.
7. Persist one Terminal Receipt and release the lease.
8. Prepare a destination-bound Continuation.
9. Deliver it to the exact Commander continuation.
10. Verify convergence from the destination's durable state.
11. Commit and acknowledge the continuation exactly once.
12. Obtain an exact parent-owned Mission Readback and re-evaluate the Mission.

## 8. Required Invariants

1. **Single mission owner**: only the Commander readback can change mission truth.
2. **First-false discipline**: child acceptance criteria cannot replace the
   current mission predicate.
3. **Bounded authority**: tool availability is not effect authority.
4. **Identity closure**: mission, contract, task, executor, receipt, callback,
   and destination identities are explicitly bound.
5. **No blind retry**: unsettled effects block replay.
6. **Exactly-once acknowledgement**: identical acknowledged replay is empty;
   changed identity is conflict.
7. **Terminal cleanup**: every admitted attempt has an explicit reconciliation
   path to terminal state and lease release; unknown effects are never guessed.
8. **Evidence is not authority**: projections and reports cannot dispatch or
   complete work.
9. **No hidden-history requirement**: a worker receives a bounded contract and
   references, not an implicit full transcript.
10. **Provider isolation**: one provider failure does not silently substitute,
    retry, or corrupt another provider route.

## 9. Typed Failures

Implementations must expose typed failures at least for:

- mission revision conflict
- task contract mismatch
- lease/CAS conflict
- executor or result identity conflict
- unsettled effect
- terminal receipt mismatch
- callback target mismatch
- continuation payload mismatch
- convergence proof missing
- duplicate acknowledgement conflict

Pre-execution failures are terminal observations. A begun failure with unknown
effect state is an unresolved observation until explicit reconciliation. No
typed failure creates a new mission.

## 10. Verification Contract

A conforming reference implementation must provide deterministic tests for:

1. complete happy-path cycle;
2. first-false predicate selection;
3. overlapping lease rejection;
4. pre-execution typed terminalization;
5. unsettled-effect no-retry behavior;
6. callback target mismatch;
7. identical replay as no-op;
8. changed replay identity as conflict;
9. exact acknowledgement count;
10. parent-readback-only mission verification;
11. same-attempt failure reconciliation;
12. concurrent overlapping-claim exclusion.

The public example must run without credentials, network access, private
repositories, or external services.

## 11. Adapter Boundary

Production integrations may implement:

- issue/project tracker adapters;
- Codex App Server continuation transports;
- local or remote executor providers;
- immutable evidence stores;
- dashboards and operator projections.

Adapters must preserve the records, state transitions, and invariants above.
They must not introduce a second mission owner or infer authority from visible
text, task names, or future model output.
