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

## Ultra Picker Profiles And Second Task

All three rows below use an Astra/xhigh coordinator and a fresh native Astra/xhigh control. Source/configuration controls and root CLI usage reconciliation passed. All coordinator, workers and native children are counted, including incomplete attempts and their repairs. Frozen task/quality rules did not change.

| Task / requested worker tier | Ultra seconds | Native seconds | Time delta | Ultra / native API-equivalent USD | Cost delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| R19 / high, Sol6.1-xhigh | 475.8 | 334.0 | +42.46% | 0.9655116 / 3.2562060 | -70.35% |
| R19 / deployed medium default, Terra6.1-xhigh | 245.5 | 289.4 | -15.17% | 0.7196218 / 2.9877760 | -75.92% |
| DCF2 / deployed medium default, Terra6.1-xhigh | 420.6 | 522.1 | -19.44% | 1.1305374 / 3.9113420 | -71.10% |

Each is one descriptive pair, not a stable or randomized causal estimate. The earlier slow Sol-worker result is retained, not replaced by the faster default profile. Native timing also varied; Efficient/Sol and Ultra/Astra baselines must not be pooled.

DCF2's four Terra workers include an initial two-worker batch that returned without source changes, followed by corrected read-context delegation to two replacement workers. Initial absent/unadmitted validator and storage reads, failed combined acceptance and repair costs remain charged. Both final fixed quality/integrity checks passed (688/701 tests, ten allowed skips each); the pre-existing accepted-reference exclusions are identical for all arms, not a whole-repository acceptance claim.

These measurements precede V457. The typed missing-file repair was validated deterministically, not credited with these percentages or used as a reason to rerun an otherwise unchanged model benchmark.

Earlier whole-workflow reports that counted only the last response's tokens are not reused. Source/tests/deployment receipts and private raw traces are different evidence; raw private traces are not included here. No upstream acceptance or independent replication is claimed.
