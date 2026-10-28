# Tura Integration Kit

> **Profile boundary:** This document describes the optional external-runtime
> adapter. The preferred Native Codex profile uses the packaged
> [`agents/tura.toml`](../src/codex_collaboration_harness/agents/tura.toml)
> directly and does not call `TuraAdapter`, start a Tura service, or depend on
> `components/tura-runtime.json`. See
> [`native-tura-role.md`](native-tura-role.md).

## Purpose

The external Tura runtime was the full-stack executor route that motivated this
public compatibility profile. The Python package does not embed a Tura server
or assume a domain-specific repository. Instead, it exposes the smallest
contract a third party needs to connect an external Tura deployment while
preserving the collaboration graph's identity, effect, and callback boundaries.

The integration lives in
[`codex_collaboration_harness.adapters.tura`](../src/codex_collaboration_harness/adapters/tura.py).
It is MIT-licensed adapter code. A separately versioned Tura runtime remains
under its own license and release identity.

## Contract Surface

An integration implements one method:

```python
from codex_collaboration_harness.adapters.tura import (
    TuraClient,
    TuraDispatchRequest,
    TuraTerminalEnvelope,
)


class MyTuraClient(TuraClient):
    def dispatch(self, request: TuraDispatchRequest) -> TuraTerminalEnvelope:
        # Send request through your authenticated Tura transport and wait for
        # the exact terminal identity. Do not synthesize missing effect state.
        ...
```

`TuraDispatchRequest` carries the generic collaboration contract only:

- mission, revision, mode, predicate, route, and executor identity;
- bounded scope and both expected and claimed CAS versions;
- expected predicate delta and abandon condition;
- parent-bounded local recovery budget;
- exact callback destination and lease identity;
- a deterministic `request_id` derived from those public fields.

The protocol version is exactly `tura-collaboration/v1` and participates in
the request identity. `encode_tura_dispatch_request()` emits the canonical JSON
shape. The packaged request schema and golden vector are:

- `protocol/tura_dispatch_request_v1.schema.json`
- `protocol/golden/tura_dispatch_request_v1.json`

It contains no filesystem path, shell command, provider secret, model prompt,
UTM object, account payload, or private runtime schema.

## Terminal Mapping

The client returns one `TuraTerminalEnvelope`:

- `RESULT` requires a settled or unsettled effect identity;
- `FAILURE` requires a typed failure code and evidence digest;
- `NONE` may be asserted only for an explicit failure envelope with no effect
  identity;
- request, packet, lease, and executor identities must all match.

JSON terminals must include the same exact protocol version. The only inbound
wire normalization boundary is `decode_tura_terminal_envelope()`; core records
never accept string substitutes for Enum or bool values. The packaged terminal
schema and result/failure vectors are:

- `protocol/tura_terminal_envelope_v1.schema.json`
- `protocol/golden/tura_result_v1.json`
- `protocol/golden/tura_failure_v1.json`

`TuraAdapter.dispatch()` maps the envelope to `ExecutionResult`,
`ExecutionFailure`, or `TuraTypedRejection`. An unsettled result remains
unsettled for the core reconciliation path. The adapter never retries a Tura
request and never guesses that an interrupted request had no effect.

`TuraAdapter.execute()` implements the core `Executor` protocol for settled
success. Typed adapter failures use the generic core `ExecutorFailureSignal`,
so the failure code, detail digest, and observed effect identity survive the
adapter-to-harness boundary. The host must reconcile that same failed attempt
before releasing its lease; a second execution is rejected before another Tura
request can be sent.

## Responsibilities Outside This Package

A production integration must provide:

1. authenticated and destination-bound transport;
2. durable task/runtime registration and lease fencing;
3. effect receipts and interruption reconciliation;
4. terminalization and process cleanup;
5. guided callback delivery, convergence proof, and acknowledgement;
6. an authoritative parent `MissionSnapshotReadback` with monotonic sequence;
7. provider credentials, sandboxing, quotas, and irreversible-effect policy.

The public adapter does not grant those capabilities. It makes the handoff
boundary explicit so a Tura implementation can be tested without changing the
generic mission model.

## Conformance

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_tura_adapter -v
```

The public fake-client suite covers settled success, unsettled effect
preservation, exact wire/golden identity, transport exception, typed terminal failure, terminal identity
mismatch, and adapter-to-core failure composition for both settled-effect and
proven-no-effect paths. It uses no network, credential, private corpus, or
installed runtime.

## Versioned Runtime Component

The review package binds a public Tura runtime fork through
[`components/tura-runtime.json`](../components/tura-runtime.json). The manifest
keeps these identities distinct:

- upstream Tura source and license;
- the modified runtime commit;
- the benchmarked candidate commit;
- any packaged release artifact;
- any installed or running deployment.

An adapter test or source commit is not proof of installed Tura behavior. A
real-runtime conformance report must name the exact component commit and remain
separate from this package's synthetic acceptance.

Run `make check-components` to resolve the published branch, four declared Git
trees, ancestry, `LICENSE`, and `MODIFICATIONS.md` from GitHub. This networked
check is intentionally not folded into deterministic offline `make check`.
