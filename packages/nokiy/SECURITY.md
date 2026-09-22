# Security Policy

## Supported Versions

Security fixes are applied to the latest released minor version and the default
branch. Pre-release versions may receive breaking fixes.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Default branch | Best effort |
| Older releases | No |

## Reporting a Vulnerability

Please use the repository's private security-advisory channel. Do not open a
public issue for a suspected vulnerability, unpublished exploit, credential,
private task payload, raw conversation, or runtime receipt.

Include, where possible:

- affected version or commit;
- the violated trust-boundary or invariant;
- a minimal synthetic reproduction;
- expected and observed behavior;
- likely impact and whether real effects were involved;
- any suggested containment.

Maintainers aim to acknowledge a complete report within five business days.
Response and disclosure timing depends on severity, reproducibility, and the
availability of a safe fix. A reporter will be credited when requested and when
doing so does not increase risk.

If no private advisory channel is available, contact a maintainer privately
through the contact method on their repository profile and share only enough
information to establish a secure reporting path.

## Security Model

This package enforces state-machine invariants in one process. It does not
provide:

- authentication, authorization, or secret management;
- a worker sandbox;
- cryptographic signing or an append-only receipt log;
- durable/distributed locking;
- network transport security;
- authorization for tools, brokers, deployments, or real-world effects.

The in-memory lease/CAS mechanism is a reference contract. Production adapters
must supply atomic persistence, identity binding, replay protection,
authorization, and rollback or containment appropriate to their effects.

Passing the public tests is not a security certification. Review
[`docs/trust-boundaries.md`](docs/trust-boundaries.md) before integrating an
external executor.

## In-Scope Security Defects

- bypassing first-false-predicate selection;
- accepting a result for the wrong packet, route, lease, or CAS version;
- acknowledging a continuation without destination-bound convergence;
- allowing duplicate terminalization or duplicate acknowledgement;
- retaining or releasing the wrong task lease at terminalization;
- re-executing an unsettled effect instead of reconciling the recorded attempt;
- changing continuation identity during callback recovery;
- silently treating a worker result as parent mission completion;
- leaking secrets or private evidence through examples, tests, or diagnostics.

## Out of Scope

Findings that exist only because an integrator treats the reference in-memory
store as a distributed lock, or grants an executor authority outside the
adapter contract, are integration issues rather than package vulnerabilities.
They are still useful reports when the documentation could prevent the misuse.
