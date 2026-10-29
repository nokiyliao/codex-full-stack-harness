# Nokiy Full-Stack Harness

## Current Deployment: V473

V473 deploys Rust source `392130d3990b8a1c7917a7b0f6a414aef9ebff07` with unchanged caller source `48342f2` and Switchboard `5b16a5b`. OAuth access selection now reports the fixed, secret-free reason `NOKIY_OAUTH_ACCESS_UNAVAILABLE` instead of implying that an API key is required. Authentication preference, fallback, retries, models and permissions are unchanged. All 284 provider unit tests and the existing guarded candidate/live release checks passed. The original failed request was not replayed; its historical root cause remains unknown.

No performance gain is attributed to this diagnostic repair. The V472 and earlier benchmark observations below retain their original measured compositions. [Current manifest](releases/v473-oauth-selection-diagnostic.json) and [provenance](PROVENANCE.md) describe the deployed source; older deployment descriptions are historical.

## Current Deployment: Caller V472

As of 2026-10-29, the deployed caller is V472 (`0.3.472.dev0`), source `48342f2df96710be9e1ebcdbc6b8101bb521a9ac`. Switchboard `5b16a5b666b2c91f78399dbb40bbfeacafd7ced8` and Rust V464 `1304065c69b9c5c6a702a05754526395c9a41678` are unchanged.

V472 adds `exit_code`, `execution_proof` and `receipt_check` from already validated matched commands to context-bearing verifier receipts, matching existing receipts without context. This fixes missing evidence that prompted extra coordinator reads; it adds no success inference, live freshness, dependency coverage or acceptance authority. Models, prompts and permissions are unchanged; Rust was not rebuilt.

The current V472 workflow has now completed one accepted R19 confirmation pair: Efficient **750.859 seconds / $0.4778292** versus native Sol 6.1/max **940.850 seconds / $1.3143364**, including coordinator, workers and native children. Both passed 72 cases with zero skips, failures, errors or integrity errors. This is **20.19% less time / 63.64% lower API-equivalent estimate**, not a causal gain attributable to the V472 receipt-field change. The earlier V471 results below retain their original source identities and limits. See [benchmark boundaries](docs/benchmark-summary.md) and [current surface](docs/current-surface.md).

## Two Different Frozen Tasks

The measured caller V471 / Switchboard `5b16a5b` / Rust V464 workflow has one parent-accepted pair on each of two different real-engineering snapshots:

| Task | Efficient E2E / estimate | Native Sol 6.1/max E2E / estimate |
| --- | ---: | ---: |
| DCF2-v3 navigation and shared-cache repair | 590.022 s / $0.5206696 | 1,097.809 s / $1.5102848 |
| R19 resident-cache and ignored-directory closeout | 821.286 s / $0.5716726 | 1,196.011 s / $1.3063272 |

The R19 pair records **-31.33% time / -56.24% estimated cost**, with all recorded coordinators/workers/native children counted once. Both R19 gates pass the same 67 mandatory cases plus focused tests (68/74 total, no failures/errors/skips/integrity errors); bounded independent source review found no confirmed task defect. Both Efficient product hashes match the recorded worker batch and its PASSED/live_return combined gate. This supports observed transfer beyond the DCF task, not repeatability or universal gain: there is only one accepted pair per workload, not repeated trials or live-service benchmarking. Estimates retain the pricing/provider limitations below.

## DCF2-v3 Observation

On the stronger DCF2-v3 task, this single concurrent pair recorded **590.022 seconds / $0.5206696** for Efficient versus native Sol 6.1/max **1,097.809 seconds / $1.5102848**: **-46.25% time and -65.53% estimated cost**, calculated from unrounded totals. Both passed the shared 690-case oracle and their focused tests (697/706 recorded cases, each including ten skips). Parent product/test review accepted this bounded observation; all three Efficient product postimages match the recorded worker batch.

All recorded coordinators, workers and native children are included once: Efficient used 10 coordinator plus 15 worker calls; native used 46 coordinator plus 39 child calls. Costs use the disclosed source-pinned model weights, not invoices or provider-observed worker identity. This is not stable/universal gain, a same-model runtime acceleration claim, a DCF/J-Space ablation or an isolated test of the plan-only prompt. The earlier v2 quality holds below remain in force. See the [full qualified results](docs/benchmark-summary.md).

## Historical V2 Qualification

**The two latest DCF2-v2 pairs are not accepted equal-quality gains.** Later focused regressions found that the earlier Efficient implementation fails owner-selected override-cache retention; the follow-up implementation also misclassifies an unrelated symbol with the same short name as a full target FQN. Original timing, usage, source-integrity results and frozen-v2 PASS receipts remain unchanged, but do not establish those missing semantics.

The follow-up records 701.1 seconds / $0.5898 for Efficient versus 1,014.2 seconds / $1.5106 for native, including all recorded coordinators/workers/native children. Its planning stayed at six model calls (commands 10 to 12), so the deployed plan-only guidance has not demonstrated the intended round reduction. A versioned private v3 oracle now checks both defects: its positive reference passes 690 recorded cases including ten skips; it rejects the current Efficient product on both new cases and the earlier product on cache retention. No measured subject was repaired or overwritten. See the [qualified observations](docs/benchmark-summary.md).

## Historical V2 Observation (Later Held)

On the corrected DCF2-v2 task, Efficient observed **699.0 seconds / $0.5245** versus native Sol 6.1/max **1,248.6 seconds / $1.4840**: machine-acceptance time **-44.02%** and complete recorded API-equivalent cost **-64.66%**. Both kept the same session ID through all three turns and passed the strengthened 688-case frozen contract plus their own focused tests. Native freely chose two Sol children; their entire recorded cost is included. This is one concurrent pair, not a stable/universal or isolated-patch gain, a billing statement, a large-context stress test or a DCF/J-Space ablation.

The observed workflow now matches the deployed ownership contract: Efficient's apply phase used one batch and two coordinator model calls; workers delivered the product implementations and tests, not tests after parent implementation. Earlier negative results remain below and in the [benchmark summary](docs/benchmark-summary.md). Source composition is unchanged by this measurement.

## Earlier Same-ID Ownership Workflow

Switchboard `914f345` was hot-deployed with bounded planning/apply ownership guidance; caller V471 and Rust V464 were unchanged. Ultra/Efficient now explicitly carry planned branch ownership through navigation, implementation, focused tests and verification, instead of implementing the products first and delegating only tests. Read-only planning remains read-only, serial routing is unchanged, and ownership grants no authority. Canonical Node verification passed 1,074 tests. Deployed contract generation was checked without a provider call; improved model behavior or performance after this repair was not yet measured at that release.

The earlier v1 three-turn same-ID observation did **not** establish a cost win: recorded machine E2E was 11.91% lower but cost 18.06% higher. The coordinator cost was almost native's, with workers added late for tests. Both saved machine quality gates passed, but semantic review found a resolved-reference omission in Efficient and a retention CLI reporting gap in native, so that pair's equal-quality acceptance remains held. It was not repaired into the newer result. See [benchmark observations](docs/benchmark-summary.md).

## Previous Context-Count Diagnostics

Caller V471 (`560cc0e`) and Switchboard `e4192b7` were deployed for this repair; Rust V464 was unchanged. Oversized exact-file context now reports its actual and maximum unique file count, the scopes being counted, and a supported correction instead of generic `action/invalid_shape`. The canonical 32-file bound and all authority checks remain unchanged. An actual advertised-tool canary returned 37/32 before provider execution, without changing its fixture. Caller release verification passed 481 tests; the canonical Node candidate passed 1,073. Counts overlap and are not additive. This diagnostic repair has no newly measured performance claim.

A separately measured second DCF workload on the preceding V470/`0d94344` composition observed 49.46% lower CLI workflow time and 61.98% lower reported API-equivalent cost than native Sol 6.1/max, including all recorded coordinator, worker and native-child usage. Independent final audits were deliberately held behind a two-arm barrier; their completion timestamps are not standalone user-acceptance E2E. See [benchmark observations](docs/benchmark-summary.md) for timing, quality and accounting limits.

## Previous Bounded Read Routing

Switchboard `0d9434476edc90ce3e2c7d0dc9548437799b0a30` accepts filename read patterns such as `tests/test_resident*.py` by expanding them into deterministic exact-file reads. Directory grants and write authority are not enlarged; unsafe paths and symlinks remain rejected, and original request recovery is preserved. The guarded hot deployment passed, including actual source/admission/native-tool snapshot identity. Python verification ran 227 tests with two skips; the canonical Node candidate passed 1,073 tests. Counts overlap with focused checks.

The post-deployment R19 Efficient pair on `0d94344` observed CLI workflow time -47.8% and reported API-equivalent cost -36.3%. Ultra with its existing default Terra/xhigh workers observed -40.5% time and -76.0% cost against native Astra/xhigh; the explicit Sol/xhigh route saved 66.8% cost but was 6.9% slower. All recorded coordinators/workers/native children are included and fixed quality passed. These are separate single-pair composition observations, not stable/universal or isolated-patch gains. No defaults were changed. See [benchmark observations](docs/benchmark-summary.md).

## Previous Admitted Host Binding

Switchboard `bd9ade07e95fe5abdcad28a2760a02c23f5667e9` fixes source-only publication retaining the predecessor host digest. Exact native-host updates now refresh and verify that admitted identity; ordinary module updates preserve the host binding. Independent admission/source preimages also permit recovery when source bytes were already installed. The combined canonical candidate suite passed 1,073 tests with no failures or skips.

Both guarded hot deployments passed. Native App execution metadata now identifies the corrected snapshot, and the original read-only advertised-tool route returns its indexed budget refusal before provider execution instead of starting an inspection worker. Existing immutable snapshots and requests are not rewritten. See [current surface](docs/current-surface.md); the earlier negative observation below is retained, not relabeled. No new model performance pair or universal session-recovery claim is made.

## Earlier Native-Host Correctness Follow-Up

Switchboard `b856bc102833bae2b348b13be6842a5f4a4c47b7` includes the native-host diagnostic fix from `5ab348c8451e15868298c6d45ee8397e20e9f4e7` and the publisher succession correction. The parent exported five exact committed source/test files. Caller V470 and Rust V464 retain their existing identities; this publication creates no new caller/Rust version or rebuild.

Inline external UTF-8 evidence that exceeds the unchanged 65,536-byte total prompt budget now returns `NOKIY_NATIVE_INPUT_INVALID`, indexed `action.read_scopes[n]` and `external_evidence_prompt_budget_exceeded`, with exact byte-budget facts and an actionable correction before caller/provider execution on the corrected native-host path. No cap is raised, evidence truncated or dropped, authority expanded or store added. Valid small evidence, path/symlink/UTF-8 checks and verifier-dependency fences remain. The publisher newly permits only the named `src/nokiy-native-host.py` alongside its existing source targets, not arbitrary Python/config targets. It preserves complete historical predecessor pins while binding a fresh CURRENT executor to current same-byte source, allowing verified immutable publisher succession without losing provenance.

Both bounded native-parent deployments, `nokiy-publisher-native-host-support-20261029` then `nokiy-external-evidence-budget-diagnostics-20261029`, were verified, healthy and admitted with `hot_reload`, without an App restart. Inherited HOME/CODEX_HOME, clean canonical source `b856bc1`, models, efforts, topology, signed App and trading services were unchanged. The 16 focused source tests, pre-publisher 1,058-test source/candidate canonical Node suite and combined candidate canonical `npm test` result of 1,063/1,063 PASS (zero failures/skips) overlap and must not be summed. An earlier worker-only run had two official-catalog JSON parsing failures; the unchanged candidate later passed through the native parent. That failed worker has no promoted done/evidence_only proof.

The deployed native-host CLI negative canary passed: a 147,028-byte regular report required 147,562 prompt bytes against 65,536; it returned the indexed budget reason at `action.read_scopes[0]`, exit 2 and `provider_execution_started=false`. The earlier current-turn advertised `functions.nokiy_execute` canary instead adapted `unsafe_path` to `read_only_refusal_to_inspect` and started a read-only Sol worker with 38,378 reported tokens. That route did not prove zero execution. Its current binding identity/refresh and target-user-session recovery remain unverified; stale binding is not a proven cause, and universal tool/session recovery is not claimed.

The V470 locations worker received the complete requirement and validator and read admitted `query.py` size rules before editing. Its pagination miss was a semantic implementation error, not lost harness/DCF handoff context. The useful parent correction/review remains counted. No new model performance benchmark, speed/cost percentage or handoff-pipeline change accompanies these correctness fixes. [Current surface](docs/current-surface.md) records the supplied deployment/canary evidence references; historical measurements below keep their actual compositions.

## V470 Caller Reliability Follow-Up

V470 adds dependency-first publication guidance using existing ordered steps in the SAME command_run array. It does not require another model turn or add a lock, grant or runtime rebuild. The final production delta is one48-byte instruction (including newline); all earlier instruction text is preserved, and regression tests separately account for this explicit feature cost while retaining baseline budgets. Source, candidate and live each passed288 tests, with overlapping counts. In the later accepted DCF2 pair, the producer published its dependency before the importer within one provider call, and the prior missing-module race did not recur. This remains guidance, not enforced atomic publication or universal adherence.

The accepted V470 workflow measured Efficient477.2s/$0.36723 versus native Sol550.3s/$0.6576468: CLI time-13.2865% and frozen API-equivalent cost-44.1600%. Both workers reached done/EVIDENCE_VERIFIED with cleanup, but acceptance includes a real parent semantic correction after the joined688-test/10-skip gate. The coordinator changed long-FQN metadata handling and its regression test; final root and independent validation passed689/10, versus native688/10, with zero failures/errors/integrity failures. The original live-return source binding covers only the pre-correction state. All five Efficient coordinator calls and both workers are counted. One pair with uncontrolled cache warmth proves neither stability nor isolated patch causality; per-arm acceptance E2E remains unmeasured. [Benchmark observations](docs/benchmark-summary.md) retain the precise metrics and correction boundary.

V469 adds field-specific deployment/closeout path diagnostics without changing admission or execution rights. The installed native interface now identifies an invalid `artifact_root` instead of hiding it behind a generic input error; the negative canary remained pre-execution with zero control-plane mutations. Source, candidate and live focused suites each passed 108 tests (overlapping counts). A subsequent frozen DCF2 pair observed256.2s/$0.409027 versus native Sol433.5s/$0.7551816, with passing functional acceptance but an unresolved branch-verifier warning. This is CLI execution, not full acceptance E2E or causal/stable gain proof. See the benchmark summary; earlier V468 measurements retain their original composition.

Historical V470 source surface: **caller-only V470 `e0d039fd31aa22dd86ea321b2e4e96e5add36ae1` + unchanged V464 Rust `1304065c69b9c5c6a702a05754526395c9a41678` + Switchboard `bd9ade07e95fe5abdcad28a2760a02c23f5667e9`**, synchronized on 2026-10-29. The accepted V470 pair above used earlier Switchboard `116d1adb43f7c88aa449aaf963dd3ee9df9846f8`.

This repository publishes the source behind the verified local Nokiy execution pipeline. It supersedes the v157 snapshot. V464 keeps failed-patch feedback fast: an already-finished provider uses its normal metrics path, while a still-running provider is cancelled without waiting just for accounting. Cancelled usage remains UNKNOWN, never zero. The intermediate V463 forced-drain approach was rejected for delaying repair. V462 optional worker notes and single-branch-only Efficient native implementation remain. These changes do not expand permissions or change models.

V465 losslessly removes duplicate batch diff context while preserving the complete diff and nontext proof, with no new caps or truncation. A real-code-delta replay reduced encoded bytes from 58,078 to 44,285 (-23.75%); it was not a historical exact-wire replay, and provider token, cost and time savings remain UNMEASURED. Partial, mismatched or tiny cases retain independent context.

V466 preserves a known provider transport failure instead of hiding it behind missing runtime-terminal evidence. Batch delivery includes only normalized phase/category/cause labels; incomplete evidence, UNKNOWN usage, integrity rejection and parent acceptance remain unchanged. The original failed request was read back without replay. This fixes diagnostic propagation, not the underlying unexpected EOF or a measured performance bottleneck; 175 focused tests passed at each executed source/candidate/live stage, with equivalent staged evidence reused.

V468 includes complete safe UTF-8 verifier logs as optional untrusted context from the existing scratch-file hashing read. Text already visible in stdout/stderr is not repeated, and optional file text yields before existing previews, proof, results and diffs within unchanged budgets. Native source/candidate/live stages each passed 260 focused tests. The following real-task observation delivered the 1,041-byte log with a matching hash in the initial live gate, and its coordinator made no subsequent command call; this is activation evidence, not an isolated causal estimate.

A fresh V468 frozen R19 pair observed Efficient 245.8s/$0.2718504 versus native Sol 412.0s/$0.6797552: execution time -40.34% and historical API-equivalent cost about -60%. Totals include both coordinators, two Efficient Terra workers and the native Sol child; both fixed quality gates passed, root CLI counters reconciled and configuration controls matched. Efficient used three coordinator calls versus five in the preceding V466 observation, but it also batched its initial reads and added different optional tests, so the change is not attributed solely to log delivery. Uncached input was 8.44% higher than native. Setup and post-barrier independent audits are excluded from per-arm timing; this is one descriptive pair, not stable or isolated causal gain. [Benchmark observations](docs/benchmark-summary.md) retain the earlier V466 and negative results.

The earlier Switchboard native-catalog fallback repair was a verified hot deployment (`hot_reload`), backed by 1,056 passing source tests. Fresh complete native catalogs can recover an exec-less chain; the exact same-connection verified fallback remains usable when tool deltas omit tools. Unsupported execution, synthetic stale catalogs and unknown unbound chains remain non-dispatching; full-request helper behavior is retained. No App restart, permission or model changes were made. Recovery of the original affected target task remains unverified, and no latency/cost percentage is assigned to this repair.

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
| Caller, navigation, recovery and verifier adapters | [packages/nokiy](packages/nokiy) | `48342f2df96710be9e1ebcdbc6b8101bb521a9ac` | MIT |
| Modified Tura-derived execution engine | [runtime](runtime) | `1304065c69b9c5c6a702a05754526395c9a41678` | AGPL-3.0-or-later |
| Model topology, capability transport and hot deployment | [packages/switchboard](packages/switchboard) | `5b16a5b666b2c91f78399dbb40bbfeacafd7ced8` | Repository AGPL-3.0-or-later |

[Historical V470 source manifest](releases/v470-caller-controls.json) records that release's per-file hashes, executable bits and installed-source correspondence; `CURRENT.json` selects the current release manifest. [Publication boundaries](docs/current-surface.md) distinguish source, deployment and portability. Historical version documents are not current installation instructions.

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

The Rust names preserve their actual source lineage. The caller source package version is `0.3.309.dev0`; the current deployed release wheel is `0.3.472.dev0` (the historical V470 wheel was `0.3.470.dev0`). The existing V464 release rebuilt the actual four-binary payload with cargo --locked. V465 through V472 change only the caller: there is no Rust rebuild or new universal runtime version.

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

An earlier V464 runtime/89c7889 Switchboard composition completed a frozen R19 pair: Efficient253.9s/$0.2467354 versus nativeSol439.4s/$0.4943428, time-42.22% and cost-50.09%, both fixed quality gates PASS. Totals include the Efficient coordinator/two Terra workers and the native coordinator/one Sol child. Efficient had three coordinator calls and no post-batch parent source read, but its uncached input was8.27% higher. This is one earlier-composition execution result, not a measurement of current Switchboard b856bc1, isolated causality, statistical stability or long-session behavior.

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
