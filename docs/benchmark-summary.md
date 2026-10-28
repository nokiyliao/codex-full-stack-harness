# Benchmark Observations

These are descriptive engineering measurements, not universal speedups, subscription billing or isolated causal estimates. Complete reported coordinator, worker and native-child usage is included; unreported provider attempts are not asserted to be covered.

Latest fixed-quality offline engineering pair:

| Metric | Efficient | Native control |
| --- | ---: | ---: |
| Coordinator | Sol 6.1/max | Sol 6.1/max |
| Workers | Two Terra 6.1/xhigh | One native Sol child |
| End-to-end seconds | 279.3 | 357.1 |
| API-equivalent USD | 0.2746386 | 0.5229860 |
| Total tokens | 561,229 | 1,219,945 |
| Uncached input | 120,409 | 100,486 |
| Fixed quality and source integrity | PASS | PASS |

Observed time -21.79%, cost -47.50%, total tokens -54.00%; uncached input +19.83%. The final test counts included permitted additions (69/76), not a reduction of frozen acceptance. This one pair does not establish stable gains.

The coordinator retained serial validators. A separate no-model native-fence check ran the same 42 JUnit IDs in 64.8545s serial and 17.2444s with four test processes; that local speed reduction did not establish automatic workflow adoption and must not be credited for the matched pair's timing.

Negative control: three earlier Efficient-versus-native-Terra pairs averaged 323.6s/$0.374 versus 180.9s/$0.219. Efficient was 78.9% slower and 70.7% more expensive on those controls. Baseline model and workload shape matter.

Earlier whole-workflow reports that counted only the last response's tokens are not reused. Source/tests/deployment receipts and private raw traces are different evidence; raw private traces are not included here. No upstream acceptance or independent replication is claimed.
