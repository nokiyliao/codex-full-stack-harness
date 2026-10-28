# Nokiy Full-Stack Harness

Current source surface: **caller-only V465 + unchanged V464 Rust + Switchboard native-catalog/continuation/observation repairs: caller bf6463b + Rust 1304065 + switchboard 35e4fe4**, synchronized on 2026-10-29.

This repository publishes the source behind the verified local Nokiy execution pipeline. It supersedes the v157 snapshot. V464 keeps failed-patch feedback fast: an already-finished provider uses its normal metrics path, while a still-running provider is cancelled without waiting just for accounting. Cancelled usage remains UNKNOWN, never zero. The intermediate V463 forced-drain approach was rejected for delaying repair. V462 optional worker notes and single-branch-only Efficient native implementation remain. These changes do not expand permissions or change models.

V465 losslessly removes duplicate batch diff context while preserving the complete diff and nontext proof, with no new caps or truncation. A real-code-delta replay reduced encoded bytes from 58,078 to 44,285 (-23.75%); it was not a historical exact-wire replay, and provider token, cost and time savings remain UNMEASURED. Partial, mismatched or tiny cases retain independent context.

Current Switchboard native-catalog fallback repair is a verified hot deployment (`hot_reload`), backed by 1,056 passing source tests. Fresh complete native catalogs can recover an exec-less chain; the exact same-connection verified fallback remains usable when tool deltas omit tools. Unsupported execution, synthetic stale catalogs and unknown unbound chains remain non-dispatching; full-request helper behavior is retained. No App restart, permission or model changes were made. Recovery of the original affected target task remains unverified, and no latency/cost percentage is assigned to this repair.

Earlier response.incomplete continuation retains verified same-chain capability without claiming successful delivery. Efficient's long-observation guidance preserves actual explicit tool arguments; publisher prepare creates its declared 0700 native receipt directory. Trading behavior is unchanged.

```text
Native Codex thread / Goal / UI
  -> switchboard model topology and native capability projection
  -> Nokiy caller: scoped context, DCF navigation and J-Space preparation
  -> modified Tura-derived Rust worker execution and tools
  -> bound receipts, combined verification, parent acceptance and recovery
```

| Component | Published directory | Source identity | License |
| --- | --- | --- | --- |
| Caller, navigation, recovery and verifier adapters | [packages/nokiy](packages/nokiy) | `bf6463bfb2538d9dde675f13f44185bf6650d2d8` | MIT |
| Modified Tura-derived execution engine | [runtime](runtime) | `1304065c69b9c5c6a702a05754526395c9a41678` | AGPL-3.0-or-later |
| Model topology, capability transport and hot deployment | [packages/switchboard](packages/switchboard) | `35e4fe45a0e6170e539bc567def3451e93c69ca3` | Repository AGPL-3.0-or-later |

[Current source manifest](releases/v465-caller-controls.json) records per-file hashes, executable bits and installed-source correspondence. [Publication boundaries](docs/current-surface.md) distinguish source, deployment and portability. Historical version documents are not current installation instructions.

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

The Rust names preserve their actual source lineage. The caller source package version is `0.3.309.dev0`; the deployed release wheel is `0.3.465.dev0`. The existing V464 release rebuilt the actual four-binary payload with cargo --locked. V465 changes only the caller: there is no Rust rebuild or new universal runtime version.

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

An earlier V464 runtime/89c7889 Switchboard composition completed a frozen R19 pair: Efficient253.9s/$0.2467354 versus nativeSol439.4s/$0.4943428, time-42.22% and cost-50.09%, both fixed quality gates PASS. Totals include the Efficient coordinator/two Terra workers and the native coordinator/one Sol child. Efficient had three coordinator calls and no post-batch parent source read, but its uncached input was8.27% higher. This is one earlier-composition execution result, not a measurement of current 35e4fe4, isolated causality, statistical stability or long-session behavior.

The complete R19 pair was measured at earlier Switchboard 372c9b2, before the native-catalog repair: Efficient 240.9s/$0.2512/489,262 total tokens versus native Sol 390.1s/$0.5439/1,399,271 tokens, time -38.25% and API-equivalent cost -53.82%. Both quality gates passed (67 versus 73 tests) with matched controls and root CLI usage reconciliation. Totals include both coordinators, Efficient's two Workers and the correlated native child. Complete reported usage uses fixed historical 2026-10-22 API-equivalent rates, not subscription billing or complete provider billing. This one concurrent frozen repair pair is not evidence that the later catalog repair caused the gain or that same-ID long-workflow gains are stable.

The newly accepted second same-ID task used Switchboard35e4fe4 + V464 caller/Rust, BEFORE V465 dedup: Efficient 327.835s/$0.5753314/1,109,878 tokens versus native 726.842s/$0.64037/1,860,793 tokens (-54.90% App turn duration, -10.16% API-equivalent cost, -40.35% tokens). Both quality checks ran 690 tests with ten skips and no failures, errors or integrity failures. Totals include Efficient's three coordinator calls and 17 calls across two Terra workers; native used 11 coordinator calls and no children. Code/config/runtime hashes matched, but cache warmth was uncontrolled, worker wire effort remains UNKNOWN and App CLI counter reconciliation is unavailable. External evaluation followed both arms, so full contiguous E2E wall is unobserved; this is not stable or causal gain proof.

Efficient's first request had 138,811 input tokens, zero cached tokens and a $0.280182 estimate (48.70% of its total); native's first request had 44,928 cached tokens. The cause is UNKNOWN, with no basis to blame the pipeline or deem all miss costs avoidable. No warmup, context-window or model change was made; V465 did not affect this request. The original test chats were formally archived; original affected live-target recovery remains unverified.

The complete retained 2026-10-28 same-ID planning/implementation/acceptance sample was mixed: Efficient versus native Sol6.1/max cost $0.63624948/$0.5501194 (+15.66%), while summed active-turn durations were629.473s/766.505s (-17.88%). Both quality counts were69. These are correlated provider-telemetry totals without independent CLI usage reconciliation. End-of-run native-binary identity drift affected the control, and filesystem replacement timestamps were after the measured phases. Active-turn sums exclude inter-turn gaps; this is descriptive evidence, not strict unchanged-executable causal proof or a joint cost/time gain.

On2026-10-29, one sequential fresh-CLI Efficient before/after observation-policy pair used Sol6.1/max, zero workers and69tests each, with both CLI usage totals reconciled. Coordinator calls fell6to3, waits3to0, tokens213834to106463 (-50.21%) and API-equivalent cost$0.1049896to$0.0908988 (-13.42%); wall time was68.62877to66.89733s (-2.52%). Removed cached input has a lower API weight, so token savings need not equal cost savings. This acceptance-only pair is not Efficient-versus-native or full repair/multiturn stability evidence, and the small time difference does not establish a stable speedup.

The current manifest records caller-source correspondence, the existing 602 Rust build-source inputs and current Switchboard source, except explicitly excluded operator-only diagnostics and compiled caches. Recorded binary hashes are provenance, not a byte-reproducible-build claim. Clean-machine installation and cross-platform service operation have not been established by this publication.

## Related Projects

- [Collaboration contracts](https://github.com/nokiyliao/codex-collaboration-harness)
- [Deep Context Federation](https://github.com/nokiyliao/deep-context-federation)
- [Session Governor](https://github.com/nokiyliao/codex-session-governor)

Their published revisions and local deployments are separate. This update does not silently claim companion repositories were synchronized as well.

## License and Provenance

The integration repository is AGPL-3.0-or-later. The caller retains MIT and third-party notices; modified Tura-derived code retains its upstream authorship and AGPL obligations. See [PROVENANCE.md](PROVENANCE.md).
