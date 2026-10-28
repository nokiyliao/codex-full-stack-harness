# Nokiy Full-Stack Harness

Current source surface: **V464 runtime + Ultra/Efficient control correction: caller ffafb43 + rebuilt Rust 1304065 + switchboard 89c7889**, synchronized on 2026-10-28.

This repository publishes the source behind the verified local Nokiy execution pipeline. It supersedes the v157 snapshot. V464 keeps failed-patch feedback fast: an already-finished provider uses its normal metrics path, while a still-running provider is cancelled without waiting just for accounting. Cancelled usage remains UNKNOWN, never zero. The intermediate V463 forced-drain approach was rejected for delaying repair. V462 optional worker notes and single-branch-only Efficient native implementation remain. These changes do not expand permissions or change models.

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
| Modified Tura-derived execution engine | [runtime](runtime) | `1304065c69b9c5c6a702a05754526395c9a41678` | AGPL-3.0-or-later |
| Model topology, capability transport and hot deployment | [packages/switchboard](packages/switchboard) | `89c7889dc9c6d8b53e142ae2270b09dc266efa61` | Repository AGPL-3.0-or-later |

[Current source manifest](releases/v464-controls.json) records per-file hashes, executable bits and installed-source correspondence. [Publication boundaries](docs/current-surface.md) distinguish source, deployment and portability. Historical version documents are not current installation instructions.

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

The Rust names preserve their actual source lineage. The caller source package version remains `0.3.21.dev0`; the installed release wheel was stamped `0.3.464.dev0` by its existing release process. V464 rebuilt the actual four-binary payload with cargo --locked; it is not a version-only relabeling.

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

A subsequent V462 Ultra/Astra-xhigh DCF2 pair completed in332.3s versus nativeAstra-xhigh538.7s with quality692/705PASS. One failed-patch call was cancelled without provider usage; Ultra's complete cost is UNKNOWN, not the known subtotal$0.8686608. V463's attempt to wait for usage was deployed but rejected after a fail-fast regression test. V464 preserves fast cancellation and only keeps metrics already available at completion; it cannot backfill that historical call. No causal performance gain from these failure-accounting changes is claimed. Frozen snapshot pairs do not establish sustained native-session continuation/compaction gains.

The current V464 runtime/89c7889 switchboard also completed a fresh frozen R19 pair: Efficient253.9s/$0.2467354 versus nativeSol439.4s/$0.4943428, time-42.22% and cost-50.09%, both fixed quality gates PASS. Totals include the Efficient coordinator/two Terra workers and the native coordinator/one Sol child. Efficient had three coordinator calls and no post-batch parent source read, but its uncached input was8.27% higher. This confirms one current-composition execution result, not isolated causality, statistical stability or long-session behavior.

The source export matches all 48 non-cache files in the installed caller package, all 602 recorded Rust build-source inputs, and the current switchboard commit except explicitly excluded operator-only diagnostics and compiled caches. Recorded binary hashes are provenance, not a byte-reproducible-build claim. Clean-machine installation and cross-platform service operation have not been established by this publication.

## Related Projects

- [Collaboration contracts](https://github.com/nokiyliao/codex-collaboration-harness)
- [Deep Context Federation](https://github.com/nokiyliao/deep-context-federation)
- [Session Governor](https://github.com/nokiyliao/codex-session-governor)

Their published revisions and local deployments are separate. This update does not silently claim companion repositories were synchronized as well.

## License and Provenance

The integration repository is AGPL-3.0-or-later. The caller retains MIT and third-party notices; modified Tura-derived code retains its upstream authorship and AGPL obligations. See [PROVENANCE.md](PROVENANCE.md).
