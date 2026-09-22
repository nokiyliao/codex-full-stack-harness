# Internal Benchmark Context

> **NON-PUBLIC INTERNAL EVIDENCE.** The underlying task corpus, trial receipts,
> and runtime environment are not included in this repository and are not
> cleared for redistribution. The figures below cannot be independently
> reproduced from public bytes. They are context for reviewers, not a public
> benchmark claim or release acceptance criterion.

## Archived Aggregate

An internal, frozen, same-repository-family engineering study compared a
collaboration harness arm (`FULL`) with a clean-context arm (`CLEAN`):

The machine-readable sanitized aggregate is
[`../evidence/internal_benchmark_summary.json`](../evidence/internal_benchmark_summary.json).
Its provenance transformation is recorded in
[`../evidence/provenance_manifest.json`](../evidence/provenance_manifest.json).

| Measure | Internal observation |
|---|---:|
| Frozen snapshots | 5 |
| Paired tasks | 33 |
| Trials | 66 |
| FULL verified completions | 33/33 |
| CLEAN verified completions | 27/33 |
| Discordant pairs | 6 FULL-only, 0 CLEAN-only |
| McNemar exact two-sided p-value | 0.03125 |
| Aggregate token reduction | 86.9245% |
| Aggregate wall-time reduction | 69.216% |
| Verified-completion productivity ratio | 9.347416x |
| Recorded duplicate/protected effects | 0 / 0 |
| Recorded source escapes/cleanup residue | 0 / 0 |

## Interpretation Limits

- The task family emphasized context/evidence navigation in one private
  repository family; it is not representative of all software tasks.
- The study evaluated an integrated internal stack, not this isolated public
  Python reference implementation.
- The aggregate does not isolate the causal contribution of mission selection,
  leases, context projection, callbacks, model behavior, or any other component.
- Token and time measurements depend on the frozen runner and environment.
- Wall time must come from the external task observer's persisted start and
  terminal timestamps, never from a model-reported duration field.
- A p-value does not establish practical generality, independence of trials, or
  absence of selection bias.
- Zero recorded safety events applies only to the measured trials and counters.

Accordingly, the public claim is not "the harness improves every agent task."
The honest statement is: one archived internal study motivated publishing the
control pattern, and public users should evaluate it on redistributable tasks
with a pre-registered runner and complete receipts.

## Required Public Benchmark Before Performance Claims

A future public benchmark should publish task provenance and license, frozen
arm manifests, exclusions, token/time accounting, safety counters, complete
trial receipts, analysis code, environment identity, and confidence intervals.
Until then, these figures remain non-public internal context.
