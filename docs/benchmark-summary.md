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

The Efficient arm itself became32.7s slower (+11.55%) between these pairs, while its estimate fell24.01% and tokens fell10.36%. Native also slowed172.3s. The better relative time ratio is not proof that V460 accelerated the candidate. Coordinator calls remained4 to4: an extra post-batch source read disappeared, but a pre-dispatch manifest/directory read replaced it. Root cached-input share rose51.99% to72.57%, and root estimate fell$0.2146052 to$0.1502276; that is not proof of fewer model round trips. Test totals67/71 and68/75 include permitted additions, not changed fixed quality rules.

Crucially, actual V460 receipts had no new source-context envelopes: explicit null snapshots for planned absent test files disabled upstream context capture. Thus no observed pair gain is attributed to the context-delivery code. V461 repaired the omission and delivered9,300/6,597-byte definition envelopes in the actual pair, but coordinator calls rose4to5 and its acceptance tail rose60.159to81.525s. Efficient itself slowed15.1s and cost rose17.03% against V460. Necessary unseen tests and controller/CAS source were not automatically classified as redundant reads. Stable or isolated gains remain unproven. Frozen2026-10-22 API weights are not current billing or subscription charges.

V462 quality/integrity passed71/73 tests with complete usage and exact root CLI reconciliation. Efficient made13 root calls, no workers or native children; native made16 root plus11 child calls. Efficient input879448/cached807424/uncached72024/output15796 versus native1465379/1276160/189219/19818. The worker tool was advertised, with no preparation denial or native-fallback event. Its coordinator implemented both independent branches, so the optional same-turn worker note was never generated and its performance effect remains untested. These observations are not an excuse to omit the slower end-to-end result.

The subsequent source/deployment correction restricts Efficient's native merge exception to one coherent branch, removing a contradictory tiny-task exception. Contract tests and hot-deployment readback passed, but no later model pair was run under this bounded experiment. This is not evidence that the coordinator now follows the revised branch routing, nor that the note accelerates acceptance. All coordinator contract drafting, discovery, implementation and delivery costs remain in the totals regardless of their labels.

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
