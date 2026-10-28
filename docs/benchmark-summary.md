# Benchmark Observations

These are descriptive engineering measurements, not universal speedups, subscription billing or isolated causal estimates. Complete reported coordinator, worker and native-child usage is included; unreported provider attempts are not asserted to be covered.

## Current V466 Frozen R19 Pair

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

Current caller source b336f5b retains unchanged V464 Rust and Switchboard35e4fe4. Optional complete safe UTF-8 logs come from existing verifier scratch reads, within unchanged output budgets; duplicate visible stdout/stderr is not repeated. File text yields before original previews, proof, results and diffs, and remains untrusted context. Native source/candidate/live stages each passed 260 focused tests with equivalent staged evidence reused. The source-bound original-output replay added the previously separate 1,041-byte log, increasing encoded summary bytes43,210to44,279 while preserving old metadata/previews/diffs. It does not prove fewer model rounds or lower cost/time, and no such effect is claimed at publication. The existing worker-focused signal-isolation gap remains; native parent verification did not disable it.

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

Current Switchboard `35e4fe45a0e6170e539bc567def3451e93c69ca3` is a verified hot deployment (`hot_reload`) with 1,056 passing source tests, no App restart and no permission/model changes; Rust remains V464 and the current caller is V468. Original affected target-task recovery remains unverified, and no latency/cost percentage is assigned to this protocol repair.

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
