# Contributing

Thank you for helping make collaboration control easier to inspect and test.

## Development Setup

Use Python 3.11 or newer:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

The source-tree command is also supported:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Contribution Boundary

Keep the core domain-neutral and deterministic. A contribution should not add a
provider SDK, network service, database, credential path, private corpus, or
runtime-specific control plane merely to demonstrate the state machine.

Changes should preserve these ownership rules:

1. The parent mission owns its ordered exit predicates.
2. A worker receives only the bounded task packet.
3. A lease/CAS claim does not grant external effect authority.
4. Terminal receipt is evidence, not mission completion.
5. Callback acknowledgement requires exact destination convergence.
6. Every result returns to mission verification or route selection.

## Pull Requests

Please keep each pull request focused and include:

- the behavior or invariant being changed;
- a synthetic test that fails before the change when practical;
- documentation changes for public API, claims, or trust boundaries;
- the verification command and result;
- a statement that no private corpus, raw conversation, secret, local path, or
  protected runtime receipt was introduced;
- attribution for any third-party material.

Do not describe a source patch or passing unit test as installed/runtime
adoption. Do not add benchmark claims without public fixtures and reproducible
receipts, or without labeling non-public evidence as non-public.

## Design Proposals

Open an issue before a change that adds durable persistence, distributed
coordination, new receipt semantics, a provider integration, or a compatibility
commitment. The proposal should state the trust boundary, failure semantics,
and how duplicate effects remain impossible or typed as unresolved.

## Style

- Prefer standard-library types and explicit state transitions.
- Keep error states typed and actionable.
- Use synthetic identifiers and fixtures.
- Avoid hidden retries and implicit authority transfer.
- Keep examples executable from public bytes only.

## Security and Conduct

Report vulnerabilities according to [`SECURITY.md`](SECURITY.md). Participation
is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
