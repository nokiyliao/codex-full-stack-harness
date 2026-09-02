# Architecture

## Ownership boundary

Codex owns provider sessions, turns, tools, and the user-facing task lifecycle.
The plugin kernel owns only the collaboration state required to make those
operations durable: mission identity, task admission, leases, compare-and-swap,
effect receipts, recovery, callback delivery, and acknowledgement convergence.

The design deliberately avoids launching a second general-purpose agent runtime.

## Component boundaries

### Collaboration kernel

- Validates mission and execution identities.
- Admits one bounded owner for an execution.
- Records effect intent and terminal receipts.
- Prevents ambiguous retry and duplicate completion.
- Delivers child callbacks and converges parent acknowledgement.

### Deep Context Federation

- Reads code graphs and evidence artifacts.
- Produces bounded, advisory context projections.
- Does not grant execution authority or mutate protected state.

### Session Governor

- Preserves same-ID task continuity across compaction.
- Bounds retained context and local storage.
- Provides verified capsules as evidence, never as transferred authority.

### Codex adapter

- Maps native Codex task, tool, and lifecycle events to kernel contracts.
- Keeps model/provider selection explicit.
- Exposes typed failures without inventing successful completion.

## Non-goals

- Recreating the complete Tura CLI, TUI, provider catalog, or standalone runtime.
- Shipping private task corpora, account state, credentials, or protected effects.
- Claiming that deterministic tests alone prove installed or live behavior.
