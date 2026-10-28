# Nokiy Full-Stack Harness

Current source surface: **V463 release: caller ffafb43 + rebuilt Rust a240866 + switchboard fa0b537**, synchronized on 2026-10-28.

This repository publishes the source behind the verified local Nokiy execution pipeline. It supersedes the v157 snapshot. V463 fixes local loss of provider usage after a failed streamed patch: retain the failure and later-command fence, but let the existing provider deadline/completion path collect usage instead of aborting early. V462 optional public worker notes and single-branch-only Efficient native implementation remain, alongside earlier context and typed admission repairs. These changes do not expand permissions or change models.

```text
Native Codex thread / Goal / UI
  -> switchboard model topology and native capability projection
  -> Nokiy caller: scoped context, DCF navigation and J-Space preparation
  -> modified Tura-derived Rust worker execution and tools
  -> bound receipts, combined verification, parent acceptance and recovery
```

| Component | Published directory | Source identity | License |
| --- | --- | --- | --- |
| Caller, navigation, recovery and verifier adapters | [packages/nokiy](packages/nokiy) | `ffafb43816ea188b080a7978894ca98711625e9b` | MIT |
| Modified Tura-derived execution engine | [runtime](runtime) | `a24086644e0df9328475a973f913a8c09f8dad24` | AGPL-3.0-or-later |
| Model topology, capability transport and hot deployment | [packages/switchboard](packages/switchboard) | `fa0b537ada314cdc17274e27d3956533bb4f3668` | Repository AGPL-3.0-or-later |

[Current source manifest](releases/v463.json) records per-file hashes, executable bits and installed-source correspondence. [Publication boundaries](docs/current-surface.md) distinguish source, deployment and portability. Historical version documents are not current installation instructions.

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

The Rust names preserve their actual source lineage. The caller source package version remains `0.3.21.dev0`; the installed release wheel was stamped `0.3.463.dev0` by its existing release process. V463 rebuilt the actual four-binary payload with cargo --locked; it is not a version-only relabeling.

Switchboard source requires Node.js 22.15+ and Python 3.11+ for its host/tests:

```sh
cd packages/switchboard
npm ci
npm test
```

Do not start or admit a service by copying another machine's configuration. Installation identities, authentication, provider credentials, local ports and protected project contracts are site-specific and intentionally excluded. Managed-workspace DCF contracts remain external integrations; this publication does not expose private workspace maps or policy data.

## Measured Results and Limits

[Benchmark summary](docs/benchmark-summary.md) includes positive and negative controls. The V458 DCF2 pair observed -35.13% time/-71.15% API-equivalent cost versus native Astra/xhigh; earlier default R19/DCF gains and the slower high/Sol arm remain visible. All coordinator and execution-child usage is included. Each is one descriptive pair, not subscription charges, stable universal speedups or causal proof of an individual patch. Initial read-scope choices also differed between the two DCF2 pairs.

Subsequent Efficient/Sol pairs include both gains and regressions. V461 activated definition context but did not reduce coordinator calls. The first V462 pair observed433.0s/$0.3827504 versus332.4s/$0.7042340: cost-45.64%, time+30.26%, with zero workers. The later single-branch routing correction was exercised in a fresh matched pair:303.3s/$0.2662306 versus451.7s/$0.5099076, time-32.85% and cost-47.79%, with two workers and both public notes delivered. Both workers still issued done after a separate verifier runtime, so note delivery does not prove elimination of a terminal model round. The earlier failure remains in the report; source/model/provider variation prevents isolated causal or stable-gain claims.

That V462 Efficient surface also completed the second frozen DCF engineering task in230.0s/$0.2542672 versus nativeSol519.9s/$0.4178072 (-55.76% time/-39.13% cost), with two workers, three coordinator calls and no post-batch parent source reread. Both quality checks passed. The coordinator did not call DCF navigation, and neither worker merged its final verifier with done; these observations do not isolate gains from either mechanism. Each workload has only one pair under that V462 policy, not stable universal evidence. No new runtime release was made for those measurements.

A subsequent V462 Ultra/Astra-xhigh DCF2 pair completed in332.3s versus nativeAstra-xhigh538.7s with quality692/705PASS. One failed patch lost its provider usage locally; Ultra's complete cost is UNKNOWN, not the known subtotal$0.8686608. That failure motivated V463's red/green delayed-completion regression and verified deployment, not a fabricated cost backfill or rerun for better numbers. No V463 model-performance gain is claimed. Frozen snapshot pairs do not establish sustained native-session continuation/compaction gains.

The source export matches all 48 non-cache files in the installed caller package, all 602 recorded Rust build-source inputs, and the current switchboard commit except explicitly excluded operator-only diagnostics and compiled caches. Recorded binary hashes are provenance, not a byte-reproducible-build claim. Clean-machine installation and cross-platform service operation have not been established by this publication.

## Related Projects

- [Collaboration contracts](https://github.com/nokiyliao/codex-collaboration-harness)
- [Deep Context Federation](https://github.com/nokiyliao/deep-context-federation)
- [Session Governor](https://github.com/nokiyliao/codex-session-governor)

Their published revisions and local deployments are separate. This update does not silently claim companion repositories were synchronized as well.

## License and Provenance

The integration repository is AGPL-3.0-or-later. The caller retains MIT and third-party notices; modified Tura-derived code retains its upstream authorship and AGPL obligations. See [PROVENANCE.md](PROVENANCE.md).
