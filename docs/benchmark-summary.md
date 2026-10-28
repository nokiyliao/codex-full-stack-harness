# Benchmark Observations

These are descriptive engineering measurements, not universal speedups, subscription billing or isolated causal estimates. Complete reported coordinator, worker and native-child usage is included; unreported provider attempts are not asserted to be covered.

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

## Current Efficient On The Second Task

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

The coordinator made no DCF navigation call: DCF is the source being repaired, not evidence that navigation was invoked. Do not attribute total gains to DCF navigation or the optional note alone. Current-surface gains were observed on R19 and DCF2, one pair each; provider/native timing varies, and transfer observations are not stable universal or isolated causal estimates. The negative zero-worker pair remains above. No production source change, new release or additional model pair was needed after this cross-task acceptance.

## Ultra Picker Profiles And Second Task

All three rows below use an Astra/xhigh coordinator and a fresh native Astra/xhigh control. Source/configuration controls and root CLI usage reconciliation passed. All coordinator, workers and native children are counted, including incomplete attempts and their repairs. Frozen task/quality rules did not change.

| Task / requested worker tier | Ultra seconds | Native seconds | Time delta | Ultra / native API-equivalent USD | Cost delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| R19 / high, Sol6.1-xhigh | 475.8 | 334.0 | +42.46% | 0.9655116 / 3.2562060 | -70.35% |
| R19 / deployed medium default, Terra6.1-xhigh | 245.5 | 289.4 | -15.17% | 0.7196218 / 2.9877760 | -75.92% |
| DCF2 / deployed medium default, Terra6.1-xhigh | 420.6 | 522.1 | -19.44% | 1.1305374 / 3.9113420 | -71.10% |
| DCF2 / V458 medium default, Terra6.1-xhigh | 327.6 | 505.0 | -35.13% | 0.8433554 / 2.9232180 | -71.15% |

Each is one descriptive pair, not a stable or randomized causal estimate. The earlier slow Sol-worker result is retained, not replaced by the faster default profile. Native timing also varied; Efficient/Sol and Ultra/Astra baselines must not be pooled.

DCF2's four Terra workers include an initial two-worker batch that returned without source changes, followed by corrected read-context delegation to two replacement workers. Initial absent/unadmitted validator and storage reads, failed combined acceptance and repair costs remain charged. Both final fixed quality/integrity checks passed (688/701 tests, ten allowed skips each); the pre-existing accepted-reference exclusions are identical for all arms, not a whole-repository acceptance claim.

The first three Ultra rows precede V457. The typed missing-file repair was validated deterministically, not credited with those percentages. The last row follows V458's verified worker/verifier context projection: both workers finished in one dispatch, root had four calls, and quality/integrity passed (688/693 tests, ten allowed skips each). Total coordinator and worker/native-child costs are included. The parent also added storage and other relevant dependencies to its initial read scopes, unlike the earlier failed first batch. Thus absence of repair and faster time are observed, not a controlled causal effect of the new metadata alone. Coordinator contract drafting, discovery and delivery remain charged; calling them coordination does not remove their tokens or time from accounting.

Earlier whole-workflow reports that counted only the last response's tokens are not reused. Source/tests/deployment receipts and private raw traces are different evidence; raw private traces are not included here. No upstream acceptance or independent replication is claimed.
