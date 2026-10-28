# Nokiy Full-Stack Harness

Current source surface: **V457 release: unchanged V456 caller source + rebuilt Rust 5154083 + switchboard e5f757d**, synchronized on 2026-10-28.

This repository publishes the source behind the verified local Nokiy execution pipeline. It supersedes the v157 snapshot. V457 rebuilds the Rust payload to distinguish admitted missing source files from permission denials; it does not expand read scopes or change models.

```text
Native Codex thread / Goal / UI
  -> switchboard model topology and native capability projection
  -> Nokiy caller: scoped context, DCF navigation and J-Space preparation
  -> modified Tura-derived Rust worker execution and tools
  -> bound receipts, combined verification, parent acceptance and recovery
```

| Component | Published directory | Source identity | License |
| --- | --- | --- | --- |
| Caller, navigation, recovery and verifier adapters | [packages/nokiy](packages/nokiy) | `1101c58096f42e6299002a9f1fceba2f021c7d0d` | MIT |
| Modified Tura-derived execution engine | [runtime](runtime) | `5154083b3a2cee92dc3264920f0df480f0fda389` | AGPL-3.0-or-later |
| Model topology, capability transport and hot deployment | [packages/switchboard](packages/switchboard) | `e5f757d8cdd810bbe625f9660129b6979d054ae2` | Repository AGPL-3.0-or-later |

[Current source manifest](releases/v457.json) records per-file hashes, executable bits and installed-source correspondence. [Publication boundaries](docs/current-surface.md) distinguish source, deployment and portability. Historical version documents are not current installation instructions.

## What Is Included

- Current coordinator/worker routing and bounded parallel execution.
- DCF navigation extraction and resolution, local context, J-Space preparation and preserved license notices.
- Original-request recovery, complete reported usage accounting and explicit parent acceptance.
- Bound verifier-output pointers, settled-failure handling and same-batch verifier input identities.
- Missing-exec coordinator fallback with advertised-tool diagnostics.
- Supervised hot reload, immutable host generations and exact cross-release dispatch-result binding.
- Cross-generation observation of original native handles, without replaying commands or granting execution or permissions.
- Compact final-acceptance defaults for intent, ordinary and shared/delta batch members, using their existing read grants.
- Typed intake validation rejections return correctable diagnostics without starting a worker; they are not worker success or permission grants.
- Compact batches retain bounded transport while applying expanded request limits per worker and final verifier, without changing admission or concurrency controls.

Native Codex retains session, Goal, UI and permission ownership. No retired Tura Skill, private App database writer or second general messaging authority is activated by publishing this source.

## Verify Source

Python 3.11+:

```sh
python3 scripts/verify_release.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

These commands verify the publication manifest and its verifier, without model calls or service installation. They do not prove runtime installation, provider access or binary reproducibility.

## Build Components

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./packages/nokiy
cd runtime
cargo build --locked --release --bin tura_router --bin tura_runtime --bin tura_session_db --bin tura_exec
```

The Rust names preserve their actual source lineage. The caller source package version remains `0.3.21.dev0`; the installed release wheel was stamped `0.3.456.dev0` by its existing release process. No version-only rebuild is claimed.

Switchboard source requires Node.js 22.15+ and Python 3.11+ for its host/tests:

```sh
cd packages/switchboard
npm ci
npm test
```

Do not start or admit a service by copying another machine's configuration. Installation identities, authentication, provider credentials, local ports and protected project contracts are site-specific and intentionally excluded. Managed-workspace DCF contracts remain external integrations; this publication does not expose private workspace maps or policy data.

## Measured Results and Limits

[Benchmark summary](docs/benchmark-summary.md) includes positive and negative controls. Default Ultra with Terra workers observed -15.17% time/-75.92% API-equivalent cost on R19 and -19.44%/-71.10% on a second DCF task versus native Astra/xhigh. The high/Sol worker R19 arm was slower (+42.46%) and remains visible. All coordinator and execution-child usage is included. Each is one descriptive pair, measured before V457; these are not subscription charges, stable universal speedups or causal proof of the new missing-file fix.

The source export matches all 48 non-cache files in the installed caller package, all 602 recorded Rust build-source inputs, and the current switchboard commit except explicitly excluded operator-only diagnostics and compiled caches. Recorded binary hashes are provenance, not a byte-reproducible-build claim. Clean-machine installation and cross-platform service operation have not been established by this publication.

## Related Projects

- [Collaboration contracts](https://github.com/nokiyliao/codex-collaboration-harness)
- [Deep Context Federation](https://github.com/nokiyliao/deep-context-federation)
- [Session Governor](https://github.com/nokiyliao/codex-session-governor)

Their published revisions and local deployments are separate. This update does not silently claim companion repositories were synchronized as well.

## License and Provenance

The integration repository is AGPL-3.0-or-later. The caller retains MIT and third-party notices; modified Tura-derived code retains its upstream authorship and AGPL obligations. See [PROVENANCE.md](PROVENANCE.md).
