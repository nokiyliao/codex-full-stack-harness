# Claims and Limitations

## Claims Supported by Public Bytes

This repository claims only that it provides:

- a domain-neutral Python reference model of a closed parent/worker
  collaboration cycle;
- explicit types for mission, predicate, route, task packet, ownership,
  step/effect evidence, blocker/recovery proposal, terminal receipt,
  destination delivery reconciliation, convergence, and verification;
- deterministic in-memory behavior for the public fixtures;
- fail-closed checks represented by the public test suite;
- documentation of boundaries an integration must implement outside the core.

These claims are reviewable from the repository and reproducible with the
canonical test command.

## Explicit Non-Claims

This repository does not claim:

- to be an official OpenAI or Codex component;
- to contain or replace the Codex runtime;
- production-ready orchestration, scheduling, persistence, or networking;
- exactly-once effects across crashes or distributed systems;
- security certification, formal verification, or cryptographic auditability;
- built-in authorization for any external tool or effect;
- that a worker result is trustworthy without an authoritative readback;
- compatibility with an undocumented or future provider API;
- public reproduction of the internal benchmark context;
- that the public reference implementation caused the internal results;
- universal gains in accuracy, token use, latency, or cost;
- redistribution clearance for any private corpus or runtime evidence;
- installed or live adoption merely because source and tests pass.

## Known Gaps

The reference does not yet supply:

- a durable database or distributed fencing implementation;
- crash-safe/outbox delivery and replay recovery;
- durable or signed receipt storage (individual reference records are
  content-addressed in memory);
- provider-specific continuation adapters;
- worker sandboxing or credential isolation;
- a public multi-process fault-injection suite;
- exhaustive tests for every possible invalid record combination or adapter
  exception;
- installed/running package identity closure;
- an independent third-party reproduction.

These are not silently delegated to the in-memory store. An integration must
implement and verify the subset required by its actual effects.

## Evidence States

Use precise language when discussing this project:

| State | Meaning |
|---|---|
| Source verified | A specific source tree passed the stated checks |
| Candidate verified | A built artifact is bound to that source and passed checks |
| Published | An artifact is present at a named registry/release identity |
| Installed verified | The installed artifact identity was read back |
| Runtime verified | The running process and behavior were read back |

One state never implies a later state.
