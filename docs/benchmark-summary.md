# Benchmark Observations

## Plan-Only Follow-up: Quality Held

The deployed source is caller V471 / Switchboard `5b16a5b` / Rust V464. No task, native binary or requested profile was changed for the concurrent v2 follow-up; before/after controls match within the pair. Native Sol 6.1/max again chose two children freely.

| Metric | Efficient | Concurrent Native |
| --- | ---: | ---: |
| Machine-acceptance wall time | 701.121 s | 1,014.218 s |
| Complete recorded API-equivalent cost | $0.5897856 | $1.5106064 |
| Coordinator calls | 14 | 41 |
| Worker/native-child calls | 12 | 42 |
| Original v2 gate cases, including ten skips | 691 | 708 |

Arithmetic is 30.87% less time and 60.96% lower estimated cost, **not an accepted equal-quality gain**. All recorded participants are counted once; cumulative three-turn totals are reconciled as deltas and overlapping worker durations are not summed into wall time. Native-child usage is complete for the whole run, but its exact apply/accept allocation is unavailable.

Planning did not meet the intended round-reduction hypothesis: Efficient stayed at six model calls, commands increased 10 to 12, and completed root-command output fell from 131,413 to 115,608 bytes. Planning cost/time changed from $0.2360292 / 271.620 s to $0.2227592 / 182.306 s, while complete Efficient cost rose 12.44% and time rose 0.30%. These cross-run changes do not isolate policy effects from model/cache variance; no stable benefit is claimed for the new guidance.

Semantic review and offline v3 regressions found two missing requirements in current Efficient: a different fully qualified symbol sharing the final name is incorrectly classified as the target, and a valid owner does not prune its selected override pool. The earlier Efficient product fails the second case, so its formerly accepted 44.02% time / 64.66% cost observation is also held for equal-quality claims. A staged reviewed Native reference passes all 690 v3 cases including ten skips; product-only counterexample clones fail the exact new assertions without integrity errors. Original v2 subjects and receipts remain unchanged. CLI/test changes after the follow-up's worker batch mean final artifacts do not all match its post-batch postimages; the inspected receipts do not identify the later editor. The later independent gate, not the earlier batch gate, covers the final v2 bytes.

Cost arithmetic matches source-pinned weights per million uncached/cached/output tokens: Sol $2/$0.10/$10 and requested Terra $1/$0.05/$5. A separately retained dated price table is unavailable, and worker exports do not establish provider-observed Terra identity or effort. These are reproducible estimates under those weights, not current-price, billing, hidden-attempt or subscription-use claims. Reasoning is included in output rather than billed twice.

The v3 fixture is private engineering evidence, not a portable public benchmark distribution. The existing paired runner now accepts its explicit, hash-pinned `dcf2-v3` selector; prior modes and executor semantics are retained and the offline unit module passed. No additional paid pair has yet used it. No measured code was repaired to manufacture a pass; the ambiguous outside-Git override concern is explicitly deferred.

## Recorded Corrected V2 Pair (Later Held)

Composition: caller V471, Switchboard `914f345`, Rust V464. The separately versioned DCF2-v2 task clarifies already intended resolved-edge targets and CLI cache reporting, with four frozen regression cases. The staged reference passes 688 tests/ten skips; both retained v1 product candidates fail exactly two cases, without source-integrity failure. Branch closure passes independently with only owned product changes: locations 90 cases/one skip, cache 141/zero skips. Original exclusions are unchanged. CLI reporting is checked by the CLI-owned locations branch, not made a dependency of runtime-only cache verification.

Both arms use the same three phase prompts and task. Efficient requests Sol 6.1/max and Terra 6.1/xhigh workers. Native requests Sol 6.1/max and independently chooses two native Sol children, each observed at max on the request wire; their 39 calls are fully included. Neither side was forced to be serial. All three phases retain each arm's exact ID; cumulative native usage is reconciled as deltas, not added repeatedly. Before/after source/runtime controls match.

| Recorded metric | Efficient | Native Sol 6.1/max |
| --- | ---: | ---: |
| Machine-acceptance E2E seconds | 698.992787 | 1,248.648999 |
| Active CLI seconds, all three turns | 648.794768 | 1,197.850259 |
| Independent quality seconds | 50.084876 | 50.625021 |
| Root API-equivalent USD | 0.3961600 | 0.7867332 |
| Worker/native-child API-equivalent USD | 0.1283676 | 0.6973084 |
| Complete recorded USD | 0.5245276 | 1.4840416 |
| Root calls | 11 | 48 |
| Worker/native-child calls | 11 | 39 |
| Total input tokens | 1,157,813 | 6,056,263 |
| Cached input tokens | 994,432 | 5,874,176 |
| Uncached input tokens | 163,381 | 182,087 |
| Output tokens | 22,669 | 53,245 |
| Reasoning tokens, included in output | 9,125 | 25,180 |

The observed change is **-44.0201% machine time / -64.6555% recorded cost**. All observed root, worker and native-child records are priced and reconciled; missing usage count is zero for those records, not proof of complete hidden provider-attempt billing. Prices remain the frozen 2026-10-22 API-equivalent weights, not subscription invoices.

Both independent final gates pass: Efficient 696/native 713 total cases, each with ten skips, no failures/errors/integrity failures and at least the common 688 immutable cases. Additional tests are not treated as a quality score. Parent reviewed both complete product diffs and matched all three current Efficient products to recorded member postimages, with no parent product repair. Efficient's apply phase has two root model calls and one batch: workers deliver both implementation branches and focused tests. The old late test-only handoff is absent in this observation. Native uses 33 root model calls in its apply phase, in addition to its children; no claim is made that every native call was avoidable.

This is one concurrent pair with possible validator/resource contention, uncontrolled cache warmth, and human/parent final acceptance outside the machine timer. It is not a large-context/compaction test, a DCF/J-Space or model/effort ablation, proof of shared coordinator/worker provider cache, or an isolated effect of the ownership text. V2 task clarification, stronger oracles, neutral prompt continuation and native decomposition differ from v1. The prior negative pair remains negative and held; it is not replaced or retroactively accepted. No new runtime deployment accompanied this measurement.

The remaining measured planning component is six Efficient calls, 271.619528 seconds and $0.2360292 (about 45% of Efficient cost). This identifies an investigation target, not a predicted saving or license to remove necessary dependency reads. Two main sessions were formally archived. Two native child turns are completed; archival persistence for those children remains a local API limitation and does not invalidate source/usage evidence.

## Three-Turn Same-ID Cost Regression

The same frozen two-branch DCF task ran as read-only planning, implementation and acceptance on one exact newly created native session per arm. Composition was caller V471, Rust V464 and Switchboard `e4192b7`. Efficient used Sol 6.1/max plus two Terra 6.1/xhigh workers; native used Sol 6.1/max and no children. Controls matched. CLI resume retained exact identities/configuration; public cumulative usage was reconciled into phase deltas, not summed repeatedly. Each arm proceeded independently through its own quality gate with no cross-arm phase or quality barrier.

| Recorded metric | Efficient | Native Sol 6.1/max |
| --- | ---: | ---: |
| Machine-acceptance E2E seconds | 909.713025 | 1,032.728111 |
| Active CLI seconds, all three turns | 854.271302 | 982.700637 |
| Independent quality seconds | 55.295879 | 49.910782 |
| Root API-equivalent USD | 0.7249744 | 0.7291424 |
| Worker API-equivalent USD | 0.1358212 | 0 |
| Total API-equivalent USD | 0.8607956 | 0.7291424 |
| Recorded calls, root plus workers | 22 + 12 | 25 + 0 |
| Input tokens | 2,570,032 | 2,181,332 |
| Cached input tokens | 2,364,288 | 2,081,664 |
| Uncached input tokens | 205,744 | 99,668 |
| Output tokens | 34,870 | 32,164 |

Recorded machine time was 11.91% lower, but total cost was 18.06% higher. Worker cost $0.1358212 was offset by only $0.0041680 root savings, leaving the exact $0.1316532 premium. Root implemented both product branches before dispatching two test-only workers 557.038 seconds after initial CLI start. Both assignments said product implementations were already written and returned diffs were tests only. Workers provided useful tests; their entire cost is not avoidable waste. The planning turn accounted for $0.3553844 (41.29% of Efficient cost) and 244.963773 seconds. These locate affected work, not a forecast saving from removing required checks.

The saved unchanged machine validator passed Efficient 709 cases and native 711, each with ten skips and zero failures/errors/integrity failures. **Parent semantic acceptance is held for both outputs**, not upgraded by those green checks. A direct frozen-output witness confirmed Efficient drops a graph `MAY_RESOLVE_TO` target not present in syntactic payload targets, producing direction `other` instead of required `in`. Static review found native retention CLI discards the runtime's shared/local cache label; the runtime API itself supplies it. Original outputs and passing suite observations are retained, not repaired or retrospectively repriced. The two sessions were formally archived after completion.

Thus these numbers are descriptive accounting/timing, not a demonstrated equal-quality speedup. Both roots and all recorded workers are counted once; missing usage is zero only for these observed records. Prices are frozen 2026-10-22 API equivalents, not subscription billing or complete hidden provider-attempt coverage. Validators may contend with the other arm's CLI, parent/human acceptance is outside this machine timer, and a single pair proves no stable or isolated gain.

Follow-up source `914f345` hot-deploys generic cross-turn ownership guidance, and the existing runner retains compact phase observations and identical neutral ownership prompts. Offline/source gates pass, but **post-repair model performance is unmeasured**. The earlier single-turn gains below do not contradict this regression: they measure different workflow shapes. No Rust/runtime rewrite or new execution service is attributed to this result.

## Timed Second DCF Workload

One matched pair used caller V470, Rust V464 and Switchboard `0d94344`, before the V471 diagnostic repair. Efficient used Sol 6.1/max with Terra 6.1/xhigh workers; the native control used Sol 6.1/max, including its recorded native child. Source and runtime controls matched before and after the pair. Both frozen independent quality gates passed: Efficient 686 tests and native 718, each with 10 skips and no failures, errors or integrity failures. Counts differ because of branch-added tests; passing the unchanged acceptance contract, not equal counts, is the quality condition.

| Metric | Efficient | Native Sol 6.1/max |
| --- | ---: | ---: |
| CLI workflow seconds | 377.147312 | 746.301213 |
| Reported API-equivalent USD | 0.3777580 | 0.9934568 |
| Input tokens | 915,735 | 3,416,660 |
| Cached input tokens | 774,016 | 3,283,328 |
| Uncached input tokens | 141,719 | 133,332 |
| Output tokens | 15,928 | 39,846 |
| Reasoning tokens, included in output | 5,076 | 20,760 |
| Recorded model calls | 22 | 51 |

Observed CLI workflow time was 49.4645% lower and reported cost 61.9754% lower. Efficient includes all ten root and twelve worker calls; native includes 27 root and 24 child calls. Failed preparation and coordinator diagnostic work remain charged. All recorded usage reconciled; unknown provider attempts and actual subscription billing remain unknown. Prices are the frozen 2026-10-22 API-equivalent weights, not invoices. Uncached input increased despite the overall cost reduction.

Timing instrumentation exposes the deliberate benchmark barrier: independent quality waits for both model CLI arms to finish to avoid overlapping validation load. Efficient waited 369.341232 seconds after its CLI exit before its 46.720131-second quality audit; native waited 0.190656 seconds before its 48.553610-second audit. Dispatch-to-audit completion was 793.207243 versus 795.041092 seconds. The faster arm's barrier wait is not runtime latency. These observations do not measure each arm's uninterrupted standalone acceptance E2E, parent semantic acceptance duration, stable gain or isolated patch causality. Task, validator, model controls and barrier were not changed for a better result.

The initial glob expansion produced 37 unique exact context files against the canonical maximum of 32, but the old caller only returned generic `action/invalid_shape`. That forced several coordinator source investigations before narrowing the read set. V471 fixes this diagnostic and is separately deployed/verified; this pre-repair pair does not quantify V471's incremental savings. Historical negative and earlier single-pair observations remain below.

## Earlier Ultra Worker Routes

Two additional matched R19 pairs use the unchanged `0d94344` composition, Astra/xhigh coordinators and native Astra/xhigh controls. The explicit high picker selects Sol 6.1/xhigh workers; the already-deployed default medium picker selects Terra 6.1/xhigh. No model, effort, routing or runtime setting changed to run these cases. Each row has its own contemporaneous native control, including its recorded Astra child.

| Ultra worker route | Ultra seconds / USD | Native seconds / USD | Time change | Cost change |
| --- | ---: | ---: | ---: | ---: |
| High: Sol 6.1/xhigh | 375.366500 / 0.944942 | 351.133785 / 2.849460 | +6.9% | -66.8% |
| Default medium: Terra 6.1/xhigh | 229.714605 / 0.6975272 | 385.988216 / 2.907558 | -40.5% | -76.0% |

Both Ultra coordinators used three calls with verified xhigh request-wire settings. All four arms passed fixed quality/source-integrity gates: high Ultra/native 69/72 tests, default Ultra/native 67/73, with zero failures/errors/skips. Optional additions differ. Both pairs have complete reconciled reported root/worker/native-child usage, verified worker done/cleanup, joined source-bound live-return gates and no post-gate parent commands. Initial failed worker checks remain included. The slower high/Sol result is a tradeoff, not joint gain.

The longest worker was 215.0 seconds in the high/Sol observation versus 97.1 seconds in the default/Terra observation. This identifies worker execution as a material critical-path contributor, not an isolated model-throughput explanation. Prompts, optional work, provider load and cache warmth vary; this is not a controlled worker-model ablation or proof that Terra is preferable for every task. Existing routes remain unchanged. Costs use the same frozen 2026-10-22 API-equivalent rates, not invoices/subscription billing. CLI timings exclude setup and independent after-barrier audits; hidden provider attempts and internal effort remain unknown. Cross-task and sustained same-ID gains remain unproven.

## Post-Deployment V470 R19 Pair

The current `0d9434476edc90ce3e2c7d0dc9548437799b0a30` composition completed a new matched R19 pair with unchanged caller V470 and Rust V464. Both arms passed the fixed quality gate and source integrity: Efficient 67 tests, native 73, zero failures/errors/skips. Complete recorded usage reconciles; there are no missing usage records or unknown models.

| Metric | Efficient | Native Sol 6.1/max |
| --- | ---: | ---: |
| CLI workflow seconds | 250.756823 | 479.972637 |
| Reported API-equivalent USD | 0.2520500 | 0.3956116 |
| Input plus output tokens | 489,656 | 905,505 |
| Uncached input tokens | 116,472 | 69,145 |
| Coordinator calls | 3 | 13 |
| Worker calls | 12 | 0 |

Observed CLI time fell 47.8% and complete reported cost fell 36.3%. Efficient used two Terra 6.1/xhigh workers; native autonomously used no child this time. Both workers have exact-request verified done/cleanup evidence and a joined source-bound live-return gate. There were no coordinator preparation refusals or post-gate parent commands. Failed worker verifiers and native shell lookups/repairs remain included, not removed to improve totals.

The current run used exact/discovery read scopes, so it did not directly exercise filename-glob expansion. Three coordinator calls versus four in the preceding observation is measured, but cannot isolate patch causality. Cache warmth, provider load and autonomous decomposition vary, and the native baseline differs across runs. This is a qualified single pair, not a stable/universal gain. Frozen 2026-10-22 API-equivalent pricing is not invoice/subscription billing; hidden provider attempts and internal effort remain unknown. CLI timing excludes setup and the independent after-barrier audit. No further source deployment accompanied this measurement.

## Earlier V470 R19 Pair On The Repaired Binding

One accepted frozen R19 pair used caller V470, Rust V464 and Switchboard `bd9ade07e95fe5abdcad28a2760a02c23f5667e9`. Both arms passed the unchanged fixed quality gate with no integrity failures; Efficient passed 67 tests and native passed 73, including different optional additions. Controls matched. All recorded coordinator, worker and native-child usage is included and reconciled.

| Metric | Efficient | Native Sol 6.1/max |
| --- | ---: | ---: |
| CLI workflow seconds | 321.277002 | 397.608706 |
| Reported API-equivalent USD | 0.3094348 | 0.5399996 |
| Input plus output tokens | 499,409 | 1,508,951 |
| Uncached input tokens | 142,871 | 105,276 |
| Model calls | 15 | 30 |

Observed CLI time fell 19.2% and estimated cost fell 42.7%, despite higher uncached input. Efficient used two Terra 6.1/xhigh workers; native used one Sol 6.1/max child. A pre-provider filename-glob rejection and the 33.85-second coordinator correction interval remain included, as do each worker's initial failing tests exposing missing requested behavior. No failed work or useful verification was removed from accounting.

This is a single pair with uncontrolled provider load/cache warmth, using frozen 2026-10-22 API-equivalent prices rather than invoices/subscription billing. CLI workflow timing excludes setup and the independent after-barrier audit; whole paired-job wall time is not per-arm uninterrupted acceptance E2E. Hidden provider attempts and internal provider effort remain unknown. The later `0d94344` bounded read-glob fix has correctness/deployment evidence, not a new performance measurement; no stable or isolated patch gain is claimed.

## Previous Binding Repair: No New Performance Benchmark

Switchboard `bd9ade07e95fe5abdcad28a2760a02c23f5667e9` corrects source-only publication leaving stale admitted host metadata. The 1,073-test canonical candidate suite and both guarded hot deployments passed; native App evidence confirms the corrected snapshot, and the original-shaped read-only advertised-tool canary now returns the indexed budget refusal before provider execution. The earlier inspection-worker launch remains a negative historical observation, not a successful zero-provider test. This is verified correctness, not a new model performance pair; the accepted V470 cost/time observations below retain Switchboard `116d1ad` and their original scope.

## Earlier Correctness Deployment: No New Performance Benchmark

Current Switchboard source is `b856bc102833bae2b348b13be6842a5f4a4c47b7`, following native-host fix `5ab348c8451e15868298c6d45ee8397e20e9f4e7`. Caller remains V470 `e0d039fd31aa22dd86ea321b2e4e96e5add36ae1` and Rust remains V464 `1304065c69b9c5c6a702a05754526395c9a41678`; neither is rebuilt or versioned anew. The native-host change returns indexed, actionable byte-budget diagnostics for oversized inline external UTF-8 evidence before caller/provider execution on the corrected path, retaining the 65,536-byte total prompt cap, valid-small-evidence and path/symlink/UTF-8/verifier-dependency fences. It neither drops/truncates evidence nor expands authority or adds a store. The publisher adds only the named `src/nokiy-native-host.py` to existing source targets and separates complete historical predecessor pins from fresh same-byte CURRENT executor bindings, preserving mismatch/stale/missing-pin rejection and immutable publisher provenance.

Both bounded native-parent deployments (`nokiy-publisher-native-host-support-20261029`, then `nokiy-external-evidence-budget-diagnostics-20261029`) were verified, healthy and admitted via `hot_reload`, without an App restart or changes to inherited HOME/CODEX_HOME, clean canonical source `b856bc1`, models/efforts/topology, signed App or trading services. Source focused 16 PASS, pre-publisher source/candidate canonical Node 1,058 PASS and combined candidate canonical `npm test` 1,063/1,063 PASS with zero failures/skips are overlapping correctness counts, not additive or model performance measurements. A worker-only run's two official-catalog JSON parsing failures remain historical; the unchanged candidate later passed through the native parent, without promoting the failed worker to done/evidence_only.

The deployed native-host CLI negative canary rejected a 147,028-byte regular report requiring 147,562 prompt bytes against 65,536: `NOKIY_NATIVE_INPUT_INVALID`, `action.read_scopes[0]`, `external_evidence_prompt_budget_exceeded`, exit 2 and `provider_execution_started=false`. Supplied deployment verification SHA-256 is `f1bdb6db33cd3daf47aa9c59c202defa65b611e04fc233fb097353dfb2d92fb3`; native CLI canary SHA-256 is `6a51d6de16fdcedc41cccefeee899139d2899cbbd903970e3d155e44f21b2517`. [Current surface](current-surface.md) gives their relative evidence references and byte-budget facts.

The earlier current-turn advertised `functions.nokiy_execute` canary remains a negative result: generic `unsafe_path` became `read_only_refusal_to_inspect` and a read-only Sol worker actually started (38,378 reported tokens). This did not prove zero execution. Current advertised-tool binding identity/refresh, stale binding as a cause and target-user-session recovery are unproven; neither universal recovery nor all-call pre-model blocking is asserted. These diagnostic observations are not a fresh performance pair or speed/cost claim.

The V470 locations worker received the complete requirement and validator and admitted/read `query.py` size rules before editing. Its pagination miss was semantic implementation error, not loss of harness/DCF handoff context; no speculative handoff-pipeline change or removal of useful parent review follows. The accepted pair below remains at OLD Switchboard `116d1adb43f7c88aa449aaf963dd3ee9df9846f8`: 477.2 versus 550.3 seconds and $0.36723 versus $0.6576468, including the full measured coordinator/worker/native-child usage and parent repair. It is not a benchmark of latest `b856bc1`. All historical positive, negative and incomplete records retain their tested composition and limits; these correctness fixes have no new model performance benchmark or attributed speed/cost percentage.

## V470 DCF2 Pair Accepted With Parent Semantic Correction

The accepted composition was caller V470 `e0d039fd31aa22dd86ea321b2e4e96e5add36ae1` + unchanged V464 Rust `1304065c69b9c5c6a702a05754526395c9a41678` + Switchboard `116d1adb43f7c88aa449aaf963dd3ee9df9846f8`. CONTROL.json reported matched_controls=true, issues=[] and before==after. Both root CLI counters reported REPORTED_TOTALS_MATCH. Complete retained reported coordinator/worker/native-child usage has zero missing usage records and zero unknown models; this does not establish coverage of hidden provider attempts.

| Metric | Efficient | Native Sol 6.1 |
| --- | ---: | ---: |
| CLI execution seconds | 477.1968822921626 | 550.3145598750561 |
| Reported API-equivalent USD | 0.36722999999999995 | 0.6576468 |
| Total tokens (input + output) | 680,285 | 1,383,237 |
| Input / cached input / output | 662,282 / 525,952 / 18,003 | 1,358,314 / 1,214,848 / 24,923 |
| Uncached input | 136,330 | 143,466 |
| Overall input cache hit | 79.415113% | 89.437936% |
| Coordinator calls | 5 | 16 |
| Worker / native-child calls | 12 / 0 | 0 / 8 |
| Worker / native-child count | 2 / 0 | 0 / 1 |
| Final quality tests run / skipped | 689 / 10 | 688 / 10 |
| Failures / errors / integrity failures | 0 / 0 / 0 | 0 / 0 / 0 |

Both coordinators were observed as Sol6.1 with max on the request wire; provider-internal effort remains unknown. Efficient used two observed Terra6.1 workers with xhigh configured; native used one Sol child. Efficient cost includes coordinator $0.22917479999999998 plus workers $0.1380552; native includes root $0.4622528 plus child $0.195394. All five Efficient coordinator calls, including the parent correction and final checking, remain counted. Prices are frozen2026-10-22 API-equivalent estimates, not invoices or subscription consumption. Engineering chat/review is excluded by operator scope. Retained reported usage is complete, but hidden attempts, internal provider effort and complete provider billing remain unknown.

Observed producer adherence: core.jsonl events23/24, commands0/1, published projection.py at positive step1 before importing cli.py at positive step2 within the SAME command_run/provider call. No extra model round was needed for that publication ordering. Both workers reached terminal done/EVIDENCE_VERIFIED with cleanup; all35 command records had zero failures. The V469 missing-module race did not recur. This is one observed use of dependency-first guidance, not universal adherence or host-enforced atomicity; the earlier failed branch remains historical and is not promoted.

The acceptance boundary includes a real parent semantic correction AFTER the joined live-return gate passed688 tests/10skips. Native coordinator capture/events.jsonl line13, item_6 COMPLETED, modified BOTH scripts/ops/dcf/cli.py and tests/ops/test_dcf_locations_extension.py for long-FQN metadata within final public page budgets. This changes semantics and adds regression coverage; it was not redundant checking of an unchanged gate. Root and independent final validation then passed689/10. The native arm finished688/10, with zero failures/errors/integrity failures on both final arms. The original live-return source binding covers only the state BEFORE this edit; it does not freshly bind the final state. Workers independently completed their assigned work, but final correctness was not independently delivered by workers alone. The accepted result is the complete WORKFLOW WITH parent semantic correction, including its full coordinator cost/time and meaningful review.

Versus native in this pair, CLI time was13.2865% lower and reported API-equivalent cost44.1600% lower. Both arms were slower than V469. Efficient coordinator request spans increased118.449s to289.450s and calls3 to5; cache-branch verification changed from0.824s failed to39.910s passed. These are different observed workflows with uncontrolled cache warmth, not an isolated V470 patch-effect estimate. Request spans include provider, network, queue and overlap; they are neither pure reasoning time nor additive wall-time attribution.

Per-arm times cover CLI execution with internal delegation/checks. Independent final audits are separate; uninterrupted per-arm user-acceptance E2E remains unmeasured. The whole two-arm runner took597.8256695410237s, which is NOT either arm's acceptance E2E. One pair establishes neither statistical stability nor isolated patch causality, long-session gains or original live Ultra recovery. Earlier observations and their negative results remain below. This is a documentation/manifest update of an accepted measurement, not a new runtime, release or benchmark replay.

Evidence references: `current-v470-efficient-dcf2-pair-20261029/efficient/metrics.json`, SHA-256 `e4fcf23507a9b2b940755f1ceb75d2ac9f7849b17dc252d06263f48c2f206652`; `current-v470-efficient-dcf2-pair-20261029/native/metrics.json`, SHA-256 `107a6d4fc4471b8621edb3db9b3484476ac1c6882ac38a48152cf9864104fbcf`. The parent supplied these metrics and accepted control/lifecycle/correction evidence; the external review is not needed for this publication.

## V469 DCF2 Pair With A Branch Verification Warning

| Metric | Efficient | Native Sol 6.1 |
| --- | ---: | ---: |
| CLI execution wall | 256.2s | 433.5s |
| Reported API-equivalent cost | $0.409027 | $0.7551816 |
| Total tokens | 689,787 | 2,156,990 |
| Uncached input | 203,994 | 152,659 |
| Overall input cache hit | 69.83% | 92.84% |
| Coordinator input cache hit | 28.98% | 93.58% |
| Coordinator calls | 3 | 24 |
| Worker / native-child calls | 15 / 0 | 0 / 14 |
| Quality tests run / skipped | 691 / 10 | 703 / 10 |

Current V469/eb61be3, Switchboard116d1ad and V464 runtime observed CLI time -40.90%, cost -45.84% and tokens -68.02%, while uncached input increased33.63%. Both arms passed the same frozen acceptance/integrity rules with zero failures/errors; optional added tests differ. Controls matched and root CLI usage reconciled exactly. Complete retained reported usage includes the Sol6.1/max coordinator plus two Terra6.1/xhigh workers, and native Sol6.1/max plus its one Sol child. Internal provider effort remains unobserved. Fixed2026-10-22 API-equivalent pricing is neither invoices nor subscription consumption; engineering-parent usage is excluded by the operator's measurement scope.

Important lifecycle warning: the cache worker reported a missing sibling-owned projection module during its assigned verifier. Its terminal evidence remains incomplete and the raw batch remains BLOCKED/COMMAND_FAILURE. The joined live-return gate later passed691 tests and independent validation also passed; this permits functional snapshot acceptance, not promotion of that worker to verified/done. Cost includes the reported failed-check activity. Cross-branch verification timing/dependency remains an optimization gap, not a proven provider-speed cause.

Timing is CLI execution with internal checks, not complete user acceptance. Both independent quality audits ran after the model barrier; the whole two-arm benchmark took484.280s, but individual uninterrupted acceptance E2E was not measured. One concurrent pair with uncontrolled cache warmth proves neither stable long-session gains nor isolated V469 patch causality. Earlier V468/R19 and negative observations below are retained.


These are descriptive engineering measurements, not universal speedups, subscription billing or isolated causal estimates. Complete reported coordinator, worker and native-child usage is included; unreported provider attempts are not asserted to be covered.

## Earlier V468 Frozen R19 Pair

The composition was caller V468 sourceb336f5b + unchanged V464 Rust1304065 + Switchboard35e4fe4, using the same native CLI0.165.0-alpha.1.2 and frozen task/validator/manifest. Both coordinators were observed as Sol6.1 with max on the request wire. Efficient used two reported Terra6.1 workers (xhigh configured; provider-internal effort unobserved). Native used one Sol child, also counted. Before/after configuration controls matched, both root CLI usage counters reconciled and both fixed required quality gates passed.

| Metric | Efficient | Native Sol |
| --- | ---: | ---: |
| CLI execution seconds | 245.8 | 412.0 |
| Reported API-equivalent USD | 0.2718504 | 0.6797552 |
| Reported total tokens | 450825 | 2182748 |
| Uncached input | 127885 | 117936 |
| Coordinator calls | 3 | 23 |
| Worker calls | 11 | 0 |
| Native-child calls | 0 | 14 |
| Quality tests | 67 | 74 |
| Failures / errors / skips | 0 / 0 / 0 | 0 / 0 / 0 |

The observed execution-time difference was -40.34% and API-equivalent cost about -60%. Uncached input was 8.44% higher, demonstrating why lower overall cost does not require every token category to fall. Efficient includes coordinator $0.1568348 plus workers $0.1150156; native includes coordinator $0.4099276 plus child $0.2698276. Frozen2026-10-22 API-equivalent weights are not invoices or subscription billing. Complete retained reported usage is not proof covering hidden provider attempts.

Live delivery included a complete 1,041-byte quality.log with matching SHA and unchanged untrusted-file authority. The coordinator issued no further command after the PASSED live-return gate, compared with later log and validator-selection reads in V466. Both worker results were EVIDENCE_VERIFIED. The coordinator also combined its initial intake differently, and optional added tests differed (the fixed required suite still passed on both). Thus three versus five coordinator rounds and the cross-observation timing/cost change cannot be attributed solely to optional log delivery.

Per-arm times cover native CLI execution with internal delegation/checks, not setup or independent benchmark quality audits run after the model barrier. Full contiguous user-acceptance E2E is unmeasured. Publication checks overlapped model waits without changing frozen execution controls. Cache warmth was not controlled. This is one qualified full-route observation, not statistical stability, a DCF/J-Space ablation or isolated V468 causal proof. Original live Ultra recovery and the underlying EOF origin remain unverified.

## Prior V466 Frozen R19 Pair

The measured composition was caller V466 source188e80b + unchanged V464 Rust1304065 + Switchboard35e4fe4, using native CLI0.165.0-alpha.1.2. Both coordinators used observed Sol6.1 with max on the request wire. Efficient selected two Terra6.1 workers (xhigh configured; internal provider effort unobserved); native used one Sol child, also counted. The task bytes, frozen validator and manifest were identical. Code/configuration controls matched before/after, and both root CLI token counters reconciled to retained provider totals.

| Metric | Efficient | Native Sol | Difference |
| --- | ---: | ---: | ---: |
| CLI execution seconds | 278.8 | 397.3 | -29.83% |
| Reported API-equivalent USD | 0.308291 | 0.6258504 | -50.74% |
| Reported total tokens | 498402 | 1690990 | -70.53% |
| Uncached input | 145581 | 122351 | +18.99% |
| Coordinator calls | 5 | 21 | |
| Worker calls | 10 | 0 | |
| Native-child calls | 0 | 8 | |
| Quality tests | 70 | 74 | |
| Failures / errors / skips | 0 / 0 / 0 | 0 / 0 / 0 | |

The fixed required quality gate passed on both arms; optional added tests explain different totals. Efficient cost includes coordinator $0.180634 plus workers $0.127657. Native includes coordinator $0.4249388 plus child $0.2009116. These are complete retained reported costs under frozen2026-10-22 API-equivalent weights, not invoices, subscription billing or proof covering every hidden provider attempt. The engineering parent is excluded by operator scope, not the measured coordinator.

Per-arm timing covers native CLI execution including its internal delegation and checks. Setup and post-barrier independent quality audits are excluded, so full contiguous user-acceptance E2E is unmeasured. Source-publication checks ran during model waits without changing benchmark inputs or deployment. Cache warmth was not controlled. This one pair shows a qualified joint gain, not statistical stability or an isolated causal effect of V465 diff dedup or V466 diagnostics. The original live Ultra task recovery and unexpected-EOF origin remain unverified.

## Current Caller-Only V468

At V468, caller source b336f5b retained unchanged V464 Rust and Switchboard35e4fe4. Optional complete safe UTF-8 logs come from existing verifier scratch reads, within unchanged output budgets; duplicate visible stdout/stderr is not repeated. File text yields before original previews, proof, results and diffs, and remains untrusted context. Native source/candidate/live stages each passed 260 focused tests with equivalent staged evidence reused. The source-bound original-output replay added the previously separate 1,041-byte log, increasing encoded summary bytes43,210to44,279 while preserving old metadata/previews/diffs. It does not prove fewer model rounds or lower cost/time, and no such effect is claimed at publication. The existing worker-focused signal-isolation gap remains; native parent verification did not disable it.

## Prior Caller-Only V466

The V466 caller source was 188e80b, with unchanged V464 Rust and Switchboard35e4fe4. V466 preserves a known provider transport failure in batch output rather than masking it with missing terminal proof. Normalized phase/category/cause labels are diagnostic only: incomplete evidence, UNKNOWN usage, integrity rejection and parent acceptance remain unchanged. Source/candidate/live stages each passed 175 focused tests with matching staged evidence reused. A read-only recovery of the original failed request confirmed the deployed change without replay. No model speedup, root-cause fix for the unexpected EOF, or original live-target recovery is inferred.

## Caller-Only V465 Deployment

The V465 composition was caller `bf6463b` + unchanged V464 Rust (`1304065`) + Switchboard `35e4fe4`; V465 is not a universal runtime version. Lossless batch diff context dedup preserves the complete diff and nontext proof; partial, mismatched or tiny cases retain independent context. There are no new caps or truncation, and permissions/model/effort configuration is unchanged.

A real-code-delta replay reduced encoded bytes from 58,078 to 44,285 (-23.75%). This was not historical exact-wire replay; provider token, cost and time savings remain UNMEASURED. A post-cutover native tool read succeeded. Deployment/test evidence and byte reduction are distinct from the model measurements below, all of which predate this caller change.

## Second Same-ID Task Before V465

The newly accepted second same-ID task used Switchboard35e4fe4 + V464 caller/Rust, BEFORE V465 dedup. Code/config/runtime hashes matched; cache warmth was NOT controlled.

| Metric | Efficient | Native | Difference |
| --- | ---: | ---: | ---: |
| App turn seconds | 327.835 | 726.842 | -54.90% |
| API-equivalent USD | 0.5753314 | 0.64037 | -10.16% |
| Total tokens | 1109878 | 1860793 | -40.35% |
| Uncached input | 255390 | 122593 | |
| Coordinator calls | 3 | 11 | |
| Worker calls | 17 | 0 | |
| Workers | 2 | 0 | |
| Quality tests | 690 | 690 | |
| Skipped tests | 10 | 10 | |
| Failures / errors / integrity failures | 0 / 0 / 0 | 0 / 0 / 0 | |

Efficient totals include coordinator $0.4065676 and workers $0.1687638; native used no native children. Observed worker overlap was 31.186904s, with two peak workers. Both coordinator wire efforts were max. Terra workers were observed, with xhigh requested and terminal-reported; actual worker wire effort remains UNKNOWN.

App CLI counter reconciliation is UNAVAILABLE. Complete retained telemetry is not complete provider billing; estimates use fixed historical 2026-10-22 API-equivalent prices, not invoices or subscription billing. Timing covers App turn duration only: external evaluators ran after both arms, so full contiguous E2E wall is unobserved. Engineering investigations are not benchmark arms. The original test chats were formally archived; original affected live-target recovery remains unverified. This one task does not prove stable long-session or causal gains and is not a V465 performance measurement.

### Uncontrolled First Cold Request

Efficient's first request had 138,811 input tokens, zero cached tokens and a $0.280182 estimate, 48.70% of its total. Native's first request had 44,928 cached tokens. Cache warmth was uncontrolled and the cause remains UNKNOWN: do not blame the pipeline or assume all cache-miss costs were avoidable. No warmup, context-window or model change was made. V465 did not affect this first request; its encoded-byte reduction is separate evidence.

## Complete R19 Pair At Switchboard 372c9b2

One concurrent frozen R19 repair pair used earlier Switchboard `372c9b26b65b3ce9319877651bf63a1696232b24`, before the native-catalog fallback repair. Caller/Rust remained V464. Controls matched and both fixed quality gates passed, with no failures, errors or skips.

| Metric | Efficient | Native Sol | Difference |
| --- | ---: | ---: | ---: |
| Workflow wall seconds | 240.9 | 390.1 | -38.25% |
| API-equivalent USD | 0.2512 | 0.5439 | -53.82% |
| Total input plus output tokens | 489262 | 1399271 | |
| Uncached input | 122435 | 112787 | |
| Reported calls | 15 | 26 | |
| Workers / correlated native children | 2 / 0 | 0 / 1 | |
| Quality | PASS, 67 tests | PASS, 73 tests | |

All totals include both coordinators, Efficient's two Workers and the correlated native child. Root CLI counters reconciled and reported usage coverage was complete. Costs use fixed historical 2026-10-22 API-equivalent rates, not subscription billing or invoices; retained reported usage is not complete provider billing. Engineering investigations are not benchmark arms.

This one pair observed joint time/cost gains. It is not evidence that the later native-catalog repair caused the gain, and it does not establish stable same-ID long-workflow gains. The earlier negative same-ID cost result, observation-only policy comparison and incomplete cancelled usage remain below.

The earlier Switchboard `35e4fe45a0e6170e539bc567def3451e93c69ca3` was a verified hot deployment (`hot_reload`) with 1,056 passing source tests, no App restart and no permission/model changes; Rust remained V464 and the caller was V468. Original affected target-task recovery remains unverified, and no latency/cost percentage is assigned to this protocol repair.

Fixed-quality Efficient engineering pair, 2026-10-28, integration source d53f7e4:

| Metric | Efficient | Native control |
| --- | ---: | ---: |
| Coordinator | Sol 6.1/max | Sol 6.1/max |
| Workers | Two Terra 6.1/xhigh | One native Sol child |
| End-to-end seconds | 319.7 | 360.3 |
| API-equivalent USD | 0.2526554 | 0.5840620 |
| Total tokens | 490,239 | 1,784,779 |
| Uncached input | 125,625 | 120,942 |
| Fixed quality and source integrity | PASS | PASS |

Observed time -11.27%, cost -56.74%, total tokens -72.53%; uncached input +3.87%. The final test counts included permitted additions (67/71), not a reduction of frozen acceptance. Efficient's coordinator had 3 calls costing $0.1332712, plus two workers with 14 calls costing $0.1193842. Native's 21 parent and 11 child calls are included. This one pair does not establish stable or causal gains.

Source/configuration and log-continuity guards passed; root coordinator usage reconciled exactly to each CLI's reported input/cached/output totals. Prices use frozen standard short-context API weights read on 2026-10-22, not current billing or subscription charges. Efficient coordinator cached-input share was 64.59%; its 142.812s of observed upstream request spans are not pure reasoning time or an additive wall-time decomposition.

The preceding pair observed 279.3s/$0.2746386 versus 357.1s/$0.5229860 (time -21.79%, cost -47.50%). Keep both samples; their variation is not an isolated effect of a particular patch.

The preceding coordinator retained serial validators. A separate no-model native-fence check ran the same 42 JUnit IDs in 64.8545s serial and 17.2444s with four test processes; that local speed reduction did not establish automatic workflow adoption and must not be credited for the matched pair's timing.

Negative control: three earlier Efficient-versus-native-Terra pairs averaged 323.6s/$0.374 versus 180.9s/$0.219. Efficient was 78.9% slower and 70.7% more expensive on those controls. Baseline model and workload shape matter.

## Subsequent Efficient Acceptance

The pairs below retain the frozen real R19 task, Sol6.1/max coordinator/native baseline and Terra6.1/xhigh worker profile. All fixed quality/integrity checks, configuration continuity, complete reported usage and root CLI reconciliation passed; every executed worker and native child is counted. The V462 Efficient arm chose no workers.

| Surface | Efficient / native seconds | Efficient / native API-equivalent USD | Efficient / native total tokens | Time delta | Cost delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| V459 host, V458 caller | 283.1 / 382.8 | 0.3191698 / 0.6064524 | 496556 / 1360695 | -26.04% | -47.37% |
| V460 parent-context candidate | 315.8 / 555.1 | 0.2425276 / 0.6306024 | 445097 / 1812498 | -43.11% | -61.54% |
| V461 activated definition context | 330.9 / 469.2 | 0.2838406 / 0.4855232 | 519732 / 1084500 | -29.48% | -41.54% |
| V462 same-turn note candidate; zero workers | 433.0 / 332.4 | 0.3827504 / 0.7042340 | 895244 / 1485197 | +30.26% | -45.64% |
| V462 revised branch policy; two workers | 303.3 / 451.7 | 0.2662306 / 0.5099076 | 556159 / 1276485 | -32.85% | -47.79% |

The Efficient arm itself became32.7s slower (+11.55%) between these pairs, while its estimate fell24.01% and tokens fell10.36%. Native also slowed172.3s. The better relative time ratio is not proof that V460 accelerated the candidate. Coordinator calls remained4 to4: an extra post-batch source read disappeared, but a pre-dispatch manifest/directory read replaced it. Root cached-input share rose51.99% to72.57%, and root estimate fell$0.2146052 to$0.1502276; that is not proof of fewer model round trips. Test totals67/71 and68/75 include permitted additions, not changed fixed quality rules.

Crucially, actual V460 receipts had no new source-context envelopes: explicit null snapshots for planned absent test files disabled upstream context capture. Thus no observed pair gain is attributed to the context-delivery code. V461 repaired the omission and delivered9,300/6,597-byte definition envelopes in the actual pair, but coordinator calls rose4to5 and its acceptance tail rose60.159to81.525s. Efficient itself slowed15.1s and cost rose17.03% against V460. Necessary unseen tests and controller/CAS source were not automatically classified as redundant reads. Stable or isolated gains remain unproven. Frozen2026-10-22 API weights are not current billing or subscription charges.

V462 quality/integrity passed71/73 tests with complete usage and exact root CLI reconciliation. Efficient made13 root calls, no workers or native children; native made16 root plus11 child calls. Efficient input879448/cached807424/uncached72024/output15796 versus native1465379/1276160/189219/19818. The worker tool was advertised, with no preparation denial or native-fallback event. Its coordinator implemented both independent branches, so the optional same-turn worker note was never generated and its performance effect remains untested. These observations are not an excuse to omit the slower end-to-end result.

The subsequent source/deployment correction restricts Efficient's native merge exception to one coherent branch, removing a contradictory tiny-task exception. One justified revised-policy pair bound the changed policy and route hashes in the same existing before/after controls, keeping task, model/effort, quality and prices unchanged. Both fixed quality/integrity checks passed67/72 tests, complete reported usage and exact CLI reconciliation. Efficient made4root calls and two Terra workers6calls each; native made18root and7child calls. Efficient input547127/cached427904/uncached119223/output9032 versus native1257143/1156736/100407/19342. Uncached input is18.74% higher despite47.79% lower cost; model mix, output and all calls matter more than one cache ratio.

Both public notes actually reached the coordinator's batch result unchanged:270/361bytes of public text (3445bytes including bound note metadata), complete MODEL_REPORTED with parent acceptance still required. However both done-only runtime calls followed a separately passing final verifier; final_summary_turn_executed=false is not proof of zero additional terminal model rounds. The worker command envelopes overlapped34.948s, not a full-lifecycle/provider-span estimate. Root dispatch at82.098s, batch return229.814s and final303.243s include one958-byte post-batch source probe and a73.429s acceptance tail. That remaining probe is not automatically redundant.

Against the earlier zero-worker candidate itself, revised-policy Efficient is29.95% faster and30.44% cheaper, but native also changed332.4to451.7s. Against V461 Efficient330.9s, the new candidate is27.6s faster. These are descriptive observations, not isolated policy/note causal effects or stable universal gains. The prior negative pair is retained, not retried unchanged for a prettier ratio. All coordinator contract drafting, discovery, implementation and delivery costs remain in the totals regardless of their labels.

## Earlier V462 Efficient On The Second Task

One fresh DCF2 engineering pair used the same V462 caller/fa0b537 policy/V457 runtime, Sol6.1/max coordinator and native baseline, Terra6.1/xhigh worker profile, frozen task/quality rules and price basis. Policy/route hashes were included in the unchanged before/after controls; controls matched with no issues and both root CLI usage totals reconciled exactly.

| Metric | Efficient | Native Sol |
| --- | ---: | ---: |
| End-to-end seconds | 230.0 | 519.9 |
| Full API-equivalent USD | 0.2542672 | 0.4178072 |
| Total tokens | 466514 | 946662 |
| Uncached input | 111373 | 64436 |
| Root / worker model calls | 3 / 11 | 14 / 0 |
| Fixed quality and source integrity | PASS | PASS |

Observed time-55.76%, estimate-39.13%. Efficient's two workers cost$0.1085864 combined, coordinator$0.1456808 (57.29% of total); all are included. Uncached input increased72.85%, despite lower complete cost; output10565 versus20274 and model mix matter. Quality totals686/709 include permitted additions, and each arm retains the same ten allowed accepted-reference skips. These are not whole-repository test totals or newly relaxed exclusions.

The parent prepared at10.692s, dispatched at80.038s, received its original batch at195.130s and produced the final response at229.830s. No post-batch tool call or source reread occurred. Two worker command envelopes overlap58.033s; that is not full-lifecycle/provider-span overlap. Both bound public notes reached the coordinator unchanged,244/285text bytes (3345bytes including metadata), complete MODEL_REPORTED with parent acceptance required. Neither final verifier/done shared an array; locations combined its final mechanical readback with done instead. No terminal-shortcut speedup is established.

The coordinator made no DCF navigation call: DCF is the source being repaired, not evidence that navigation was invoked. Do not attribute total gains to DCF navigation or the optional note alone. Gains under that V462 composition were observed on R19 and DCF2, one pair each; provider/native timing varies, and transfer observations are not stable universal or isolated causal estimates. The negative zero-worker pair remains above. No production source change, new release or additional model pair was needed after this cross-task acceptance.

## Ultra Picker Profiles And Second Task

Rows below use an Astra/xhigh coordinator and a fresh native Astra/xhigh control. Source/configuration controls and root CLI usage reconciliation passed. All reported coordinator, worker and native-child usage is retained, including incomplete attempts and repairs. The final V462 row has one unreported usage record and is explicitly incomplete. Frozen task/quality rules did not change.

| Task / requested worker tier | Ultra seconds | Native seconds | Time delta | Ultra / native API-equivalent USD | Cost delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| R19 / high, Sol6.1-xhigh | 475.8 | 334.0 | +42.46% | 0.9655116 / 3.2562060 | -70.35% |
| R19 / deployed medium default, Terra6.1-xhigh | 245.5 | 289.4 | -15.17% | 0.7196218 / 2.9877760 | -75.92% |
| DCF2 / deployed medium default, Terra6.1-xhigh | 420.6 | 522.1 | -19.44% | 1.1305374 / 3.9113420 | -71.10% |
| DCF2 / V458 medium default, Terra6.1-xhigh | 327.6 | 505.0 | -35.13% | 0.8433554 / 2.9232180 | -71.15% |
| DCF2 / V462 medium default, Terra6.1-xhigh | 332.3 | 538.7 | wall only -38.31% | UNKNOWN (known 0.8686608) / 3.6983240 | UNKNOWN |

Each is one descriptive pair, not a stable or randomized causal estimate. The earlier slow Sol-worker result is retained, not replaced by the faster default profile. Native timing also varied; Efficient/Sol and Ultra/Astra baselines must not be pooled.

DCF2's four Terra workers include an initial two-worker batch that returned without source changes, followed by corrected read-context delegation to two replacement workers. Initial absent/unadmitted validator and storage reads, failed combined acceptance and repair costs remain charged. Both final fixed quality/integrity checks passed (688/701 tests, ten allowed skips each); the pre-existing accepted-reference exclusions are identical for all arms, not a whole-repository acceptance claim.

The first three Ultra rows precede V457. The typed missing-file repair was validated deterministically, not credited with those percentages. The V458 row follows verified worker/verifier context projection: both workers finished in one dispatch, root had four calls, and quality/integrity passed (688/693 tests, ten allowed skips each). Total coordinator and worker/native-child costs are included. The parent also added storage and other relevant dependencies to its initial read scopes, unlike the earlier failed first batch. Thus absence of repair and faster time are observed, not a controlled causal effect of the new metadata alone. Coordinator contract drafting, discovery and delivery remain charged; calling them coordination does not remove their tokens or time from accounting.

V462 quality/integrity passed692/705tests with the same ten accepted-reference skips. Ultra made4Astra root calls and two Terra workers;19calls have usage, and one additional recorded worker runtime has no usage (20recorded calls total). Native used22Astra root+9Astra child calls, complete$3.698324. Ultra's known subtotal$0.8686608 is not its full cost, so the shared scoreboard correctly classifies COST_UNKNOWN and emits no joint gain ratio. Both root CLI totals reconciled exactly and source/configuration/effort controls matched. Public native commands showed no custom Nokiy or navigation use.

Ultra parent dispatch143.583s, batch return266.908s and final331.601s included no post-batch source reread; root local catalog/listing needed two calls before dispatch. One worker emitted a bound292-byte note, the other ended question after its verifier imported a sibling's not-yet-created projection module. The joined gate later passed and the coordinator correctly accepted both source changes using parent-owned acceptance; the member's BLOCKED/COMMAND_FAILURE history was not erased. Command envelopes overlap35.873s, not complete worker/provider lifetimes. Neither final verifier/done was merged. No extra integration ledger or permission relaxation was required.

The missing usage came from local early cancellation after a failed patch. V463 tried waiting for provider completion to collect usage, but that introduced a demonstrated repair-latency regression (750ms delayed mock returned after854.9345ms). It was superseded rather than retained for prettier accounting. V464 preserves fail-fast cancellation and uses the normal metrics path only when the provider is already finished; cancelled usage remains UNKNOWN. Its regression covers both fast failure/usageNone and ready usage1/1/2, with no later command effects. Neither version reconstructs historical usage or makes this V462 cost complete, and no fresh model pair was run to replace it. Existing launch_arms/prep_run evidence is ephemeral single-turn execution, not same-native-session multistage resume or compaction acceptance; long-session gains were not measured in those frozen pairs.

Earlier whole-workflow reports that counted only the last response's tokens are not reused. Source/tests/deployment receipts and private raw traces are different evidence; raw private traces are not included here. No upstream acceptance or independent replication is claimed.

## Earlier V464 / 89c7889 Efficient R19 Pair

On2026-10-28, one fresh pair used the earlier V464 caller/runtime and Switchboard89c7889 composition, the same frozen R19 task, concurrent arms and independent fixed quality gate. This is not a measurement of the current 35e4fe4 source. Both coordinators used Sol6.1/max; Efficient selected two Terra6.1/xhigh workers. Source/configuration controls, including server/profile/route/policy bytes before and after, matched. Root reported input/cached/output usage reconciled exactly to each CLI, and all worker/native-child usage was complete. Costs retain frozen2026-10-22 API-equivalent weights, not subscription billing or current invoices.

| Metric | Efficient | Native Sol6.1 | Difference |
| --- | ---: | ---: | ---: |
| Workflow wall seconds | 253.9 | 439.4 | -42.22% |
| Full estimated cost USD | 0.2467354 | 0.4943428 | -50.09% |
| Total input plus output tokens | 456451 | 1194949 | -61.80% |
| Uncached input | 115546 | 106719 | +8.27% |
| Output tokens, including reasoning | 8489 | 17382 | -51.16% |
| Coordinator calls | 3 | 16 | |
| Execution-child calls | 11 across2Terra workers | 7 in1Sol native child | |
| Fixed quality/integrity | PASS67tests | PASS71tests | Same validator, no failures/errors/skips |

The native baseline was allowed its ordinary native child; this is not a no-subagent or separately isolated DCF/J-Space ablation. No custom Nokiy/navigation use was observed in root command traces. External independent quality evaluation occurred after the model barrier, as in earlier pairs, and is not included in CLI workflow wall time.

Efficient coordinator cost was$0.1442472(58.46% of its total); workers cost$0.1024882. Coordinator cached-input share was60.78% versus native93.64%, yet complete cost halved. Lower cache ratio or higher uncached input alone is therefore not a failure criterion. Root request timing reported124.935s summed upstream spans and0.015s local predispatch; spans include transport/provider scheduling and are not pure reasoning or an additive whole-workflow decomposition.

Versus the earlier V462 positive R19 pair, Efficient itself changed303.3to253.9s and$0.2662306to$0.2467354(-16.29% time/-7.32% cost), with root calls4to3. Observed prep-return-to-dispatch did not improve(72.682to72.910s). Batch span fell147.716to125.772s, and return-to-terminal fell73.498to44.238s; the older post-batch source check was absent in the new run. Expanded CLI batch bytes are not model-authored output, and public captures alone do not establish optional-note activation. These are descriptive execution differences, not a controlled causal effect of the advisory-reminder or fail-fast patches. Preserve the earlier zero-worker regression and incomplete Ultra cost. Statistical stability and sustained same-ID continuation/compaction benefits remain unproven.

## Mixed Same-ID Three-Phase Sample

The complete retained 2026-10-28 sample compared Efficient with native Sol6.1/max across planning, implementation and acceptance in the same-ID workflow. It includes all retained correlated provider telemetry, with no independent CLI usage reconciliation for these App turns.

| Metric | Efficient | Native Sol6.1/max | Difference |
| --- | ---: | ---: | ---: |
| API-equivalent USD | 0.63624948 | 0.5501194 | +15.66% |
| Total tokens | 1826678 | 1311551 | |
| Sum of active-turn seconds | 629.473 | 766.505 | -17.88% |
| Reported calls | 26 | 16 | |
| Quality tests | 69 | 69 | |

Active-turn durations are summed and exclude inter-turn gaps, not presented as an end-to-end workflow wall clock. End-of-run native-binary identity drift affected the control; filesystem replacement timestamps were after the measured phases. Treat this as descriptive observation, not a strict unchanged-executable causal comparison. The complete multi-turn sample establishes no joint cost/time improvement: Efficient cost more despite less summed active time.

## Efficient Observation-Policy Before/After Pair

On2026-10-29, one sequential fresh-CLI acceptance-only pair compared Efficient before and after the observation-policy change. Both coordinators used gpt-6.1-sol/max (Sol6.1/max), with zero workers each. This is an Efficient before/after comparison, not Efficient-versus-native.

| Metric | Before | After | Difference |
| --- | ---: | ---: | ---: |
| Coordinator calls | 6 | 3 | |
| Wait calls | 3 | 0 | |
| Total tokens | 213834 | 106463 | -50.21% |
| API-equivalent USD | 0.1049896 | 0.0908988 | -13.42% |
| Wall seconds | 68.62877 | 66.89733 | -2.52% |
| Quality tests | 69 | 69 | |

Both root CLI usage totals reconciled exactly (REPORTED_TOTALS_MATCH). Controls were stable within each arm; the only cross-arm changes were coordinator observation-guidance source and admission release metadata. Totals include measured coordinator/correlated usage; engineering-chat usage is excluded by operator scope. Removing cached input reduces tokens with a lower API cost weight, so token savings need not equal cost savings.

This one pair does not establish full repair-workflow or same-ID/multiturn stability. The small 2.52% wall-time difference is insufficient for a stable speedup claim. Costs use frozen2026-10-22 standard API-equivalent rates, not subscription consumption or invoices. Current Switchboard source is 35e4fe4 with caller-only V468 and unchanged V464 Rust. The complete R19 pair above was measured at 372c9b2 before the native-catalog repair; no model measurement here proves that repair caused a gain.
