# Trust Boundaries

## Boundary Map

| Boundary | Trusted input | What this package checks | Required production control |
|---|---|---|---|
| Mission admission | Host-created mission and predicate order | Structure, order, revision, first-false selection | Authenticate operator and durable mission store |
| Dispatch | Parent-created route and packet | Packet binds mission, predicate, route, delta, abandon condition, scope | Policy/authorization for selected worker and tools |
| Ownership | In-memory store state | Lease identity, scope conflict, CAS version | Atomic durable/distributed transaction and fencing |
| Worker | Typed `ExecutionResult` | Result is correlated to the accepted packet/claim; unsettled identity is retained | Sandbox, credentials, quotas, effect containment |
| Terminal receipt | In-process state | One accepted terminal transition per packet releases the exact task lease | Append-only or signed evidence when needed |
| Continuation | Exact `Destination`, resume proof, and convergence proof | One stable continuation is retried; proofs match destination and receipt before three ACK identities are bound | Durable outbox, authenticated transport, and replay protection |
| Mission verification | Exact parent-owned `MissionSnapshotReadback` | Binds mission revision, monotonic parent-state sequence, and complete predicate vector; task completion is insufficient | Authoritative current-state readers |
| Model-assisted recovery | `BlockerReport` + `RecoveryProposal` | Deterministic admission enforces original scope, authority, effect, destination, predicate, verification plan, and budget | Durable recovery execution and sandbox policy |

## Assumptions

- The host creates honest mission predicates and supplies their authoritative
  truth values.
- Identifiers are unique within the store lifetime.
- One Python process owns the reference store.
- The external adapter does not treat a lease as effect authorization.
- A convergence proof is created only after the destination has verified the
  exact terminal result.

Violating these assumptions can make an integration unsafe even when all public
tests pass.

## Fail-Closed Cases

The reference is expected to reject or leave unacknowledged:

- a packet for anything other than the mission's first false predicate;
- stale mission or CAS state;
- conflicting live scope ownership;
- a result that does not match the active packet/lease;
- re-execution after an effect is recorded as unsettled;
- a second terminal result for an already terminal packet;
- a callback to a different destination;
- an ACK without matching convergence proof;
- a callback retry without exact authoritative delivery-absence proof;
- a policy/authority blocker self-labelled as safe local recovery;
- a stale parent snapshot overwriting a newer truth vector;
- a duplicate ACK;
- mission completion inferred only from worker prose or task terminal state.
- mission completion inferred from a worker `predicate_satisfied` claim without
  an exact parent readback.

These are implemented rejection rules. See
[`verification.md`](verification.md) for the narrower set currently exercised
by dedicated public fixtures.

## Effects and Irreversibility

The harness has no built-in authority to send messages, modify repositories,
deploy software, trade, charge accounts, or perform any other real-world
effect. A production adapter must independently enforce:

1. **CAS ownership:** the actor still owns the exact current preimage and scope.
2. **Effect authority:** the operator or policy explicitly authorizes this
   action, target, and validity period.
3. **Irreversible-effect safety:** rollback, containment, or explicit final
   approval exists where reversal is not reliable.

The existence of a route, task packet, lease, test result, or receipt does not
satisfy those controls by itself.

## Receipt Semantics

Reference receipts are typed Python values. They provide correlation and state
machine evidence inside one process; they are not cryptographically authentic,
tamper evident, durable, or independently timestamped.

Reference continuation recovery is likewise in-memory. A production adapter
needs a durable outbox so a process crash cannot lose the stable continuation
identity after the task lease has been released.

An integration that needs audit-grade receipts should bind at least the packet,
mission revision, lease/fencing token, input digest, result digest, destination,
and acknowledgement to an append-only or signed store. That is an adapter
requirement, not a hidden guarantee of this package.

## Private Data

Examples and tests must use synthetic values. Do not place prompts, raw
conversations, credentials, local account paths, session identifiers, private
task definitions, or protected receipts in issues, fixtures, or debug output.
