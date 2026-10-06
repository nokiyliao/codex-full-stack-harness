# Recovery and terminalization invariants

Repair the generic in-memory collaboration harness in this frozen source tree.
This is a coding task, not a request to run an agent, broker, deployment, or
external service. Work only in `src/codex_collaboration_harness/core.py`.

The current implementation has three related gaps:

1. A recovery step must identify its parent step, the exact admitted recovery,
   and one action in that admission's proposal. Its operation digest and effect
   class must match that action. Ordinary non-recovery step identities must
   remain unchanged.
2. An executor return that is not an exact `ExecutionResult` must become a
   typed harness-origin execution failure. Preserve the active lease and the
   existing explicit reconciliation path; do not leak an untyped exception or
   silently terminalize.
3. No terminal path may release a packet lease or write a terminal receipt
   while any persisted step effect for that packet is `UNSETTLED`. Reject before
   partially recording the final effect. Once the exact step effect is
   reconciled, normal terminalization must still work.

Keep public contracts and unrelated behavior intact. Use the existing local
tests as needed. Do not read other checkouts, Git history, answer keys, or
network resources. Do not edit tests, specifications, packaging, or metadata.
