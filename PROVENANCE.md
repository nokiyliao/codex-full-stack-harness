# Source Provenance

## Current V465 Caller / V464 Rust / 2026-10-29 Source Surface

This is a source publication, not a redistribution of installed executables or machine configuration.

| Component | Source commit | Correspondence |
| --- | --- | --- |
| Python caller | `bf6463bfb2538d9dde675f13f44185bf6650d2d8` | Deployed caller-only V465; source package version 0.3.309.dev0, release wheel version 0.3.465.dev0 |
| Rust worker payload | `1304065c69b9c5c6a702a05754526395c9a41678` | All 602 recorded build-source inputs match; the existing V464 rebuild used cargo --locked, four binary identities match the installed V464 image; fail-fast retained, ready metrics reused, cancelled usage UNKNOWN |
| Switchboard | `35e4fe45a0e6170e539bc567def3451e93c69ca3` | Verified hot deployment of native-catalog fallback repair; fresh complete catalogs recover exec-less chains; exact same-connection verified fallback survives omitted-tool deltas; prior continuation, observation and publisher-directory repairs retained |

The current Switchboard source has 1,056 passing source tests and verified hot deployment (`hot_reload`), with no App restart or permission/model changes. Unsupported execution, synthetic stale catalogs and unknown unbound chains remain non-dispatching; full-request helper behavior is retained. Original affected target-task recovery remains unverified; no latency/cost percentage is assigned to this protocol repair. Rust remains V464, with no new rebuild; V465 changes only the caller, not a universal runtime version. Permissions, model/effort configuration and trading behavior are unchanged; private diagnostics and compiled caches remain excluded.

The caller release is `nokiy-20261029-v465-batch-diff-context`; wheel SHA-256 is `3ef04c96aa3d0f0f4d9a644e8fae2bbd617f368ecaa0573b2260b6b91b193a4c`. Lossless batch diff context dedup preserves the complete diff and nontext proof, retains independent context for partial/mismatched/tiny cases, and adds no caps or truncation. A real-code-delta replay measured 58,078 to 44,285 encoded bytes (-23.75%), not historical exact-wire replay or measured provider token/cost/time savings. A post-cutover native tool read succeeded.

V465 native focused tests passed 49 tests. The full source run executed 2,290 tests (2,166 passed, 124 skipped, zero failures/errors), including pre-existing uncommitted local-context tests preserved but neither adopted nor published; this is not a clean exported-tree count. Source/candidate/staged tests and paired-wire checks passed; staged receipts reused equivalent candidate evidence, so overlapping counts must not be summed.

Measurements retain their actual source/policy composition in [Benchmark observations](docs/benchmark-summary.md). The newly accepted second same-ID task used Switchboard35e4fe4 + V464 caller/Rust BEFORE V465: Efficient 327.835s/$0.5753314 versus native 726.842s/$0.64037 (-54.90% App turn duration/-10.16% API-equivalent cost), 690 quality tests per arm with ten skips and no failures/errors/integrity failures. Code/config/runtime hashes matched; cache warmth was uncontrolled, worker wire effort is UNKNOWN, and App CLI reconciliation is unavailable. Full contiguous E2E wall was unobserved. Efficient's first uncached request cost $0.280182 (48.70% of its estimate); its cause remains UNKNOWN and V465 did not affect it. Original test chats were formally archived, without proving original live-target recovery.

The complete concurrent frozen R19 pair used earlier Switchboard `372c9b26b65b3ce9319877651bf63a1696232b24`, before the catalog repair: Efficient 240.9s/$0.2512/489,262 total tokens versus native Sol 390.1s/$0.5439/1,399,271 tokens (-38.25% time/-53.82% cost), quality PASS at 67 versus 73 tests. Controls matched and root CLI usage reconciled; totals include both coordinators, two Efficient Workers and the correlated native child. Complete reported usage at fixed historical 2026-10-22 API-equivalent rates is not subscription or complete provider billing. These pairs prove neither isolated causal gains nor stable same-ID long-workflow gains.

The earlier 89c7889 R19 observation (253.9s/$0.2467354 versus 439.4s/$0.4943428) remains separate. The negative same-ID cost result (+15.66%, with end-of-run native-binary identity drift) and the Efficient-only acceptance observation-policy pair (-13.42% cost/-2.52% time) retain their existing measurement and control limits.

These source repositories have separate histories. Their commits are provenance references, not claims that they are ancestors in this aggregate repository. Per-file hashes in [releases/v465-caller-controls.json](releases/v465-caller-controls.json) identify the published contents. Publication-only documentation and preserved licenses are identified separately.

The caller working tree contains pre-existing local-context test changes, and the runtime working tree contains pending-message deletions; neither was adopted. Source was exported from exact commits, not by copying dirty worktrees or installed environment directories.

The historical V464 canonical runtime-release validation includes that preserved local-context test delta among its eleven test modules; it was used as a bound test input, not installed package code or a published source change. Therefore its728test stage totals are not claimed to be byte-reproducible solely from the committed public test export. The six publication-verifier tests and source hashes have their own separate scope. V463's forced-drain prototype and rejected wait tradeoff remain in Git history and its historical manifest; V464 remains the corrected Rust surface.

## Excluded Material

- Native binaries, wheels, virtual environments, node_modules, build caches and compiled Python files.
- Runtime db/ and trace-evidence/ state, sessions, credentials, machine installation/admission records and raw private logs.
- Operator-only external-latency diagnostics containing a local capability token. The diagnostic is not needed by the running service.
- Private workspace DCF graphs, project authorization contracts, research data and account/order information.

The generic runtime still uses historical tura_* executable and module names. They are modified source lineage, not a revived Skill or permission to operate private native state.

## Licenses

- packages/nokiy retains MIT and included third-party notices, including its DCF navigation notice.
- runtime is modified Tura-derived AGPL-3.0-or-later code. The existing published runtime/LICENSE is preserved because the maintained source tree did not carry a separate copy.
- packages/switchboard is published under this integration repository's AGPL-3.0-or-later license; npm dependencies are referenced, not vendored.

Upstream Tura: https://github.com/Tura-AI/tura
Modified public lineage: https://github.com/nokiyliao/tura
Earlier public fork reference: `5c1c45c53a3ce29750f31c9d37eb7a326a3f5c78`.

This is not a clean-room implementation or a claim of sole authorship. No upstream acceptance, independent audit, clean-machine installation or byte-identical binary rebuild is implied.

## Previous Publications

The former v157 publication was based on caller 8950bf3 and runtime bda10dc plus then-uncommitted changes. Its historical documentation/manifest remains history, not proof of current contents. The CURRENT.json pointer selects the current manifest.
