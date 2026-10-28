# Source Provenance

## Current V464 Runtime / 2026-10-29 Source Surface

This is a source publication, not a redistribution of installed executables or machine configuration.

| Component | Source commit | Correspondence |
| --- | --- | --- |
| Python caller | `ffafb43816ea188b080a7978894ca98711625e9b` | Unchanged package source; all 48 non-cache installed package files match byte-for-byte; installed release wheel version 0.3.464.dev0 |
| Rust worker payload | `1304065c69b9c5c6a702a05754526395c9a41678` | All 602 recorded build-source inputs match; the existing V464 rebuild used cargo --locked, four binary identities match the installed V464 image; fail-fast retained, ready metrics reused, cancelled usage UNKNOWN |
| Switchboard | `35e4fe45a0e6170e539bc567def3451e93c69ca3` | Verified hot deployment of native-catalog fallback repair; fresh complete catalogs recover exec-less chains; exact same-connection verified fallback survives omitted-tool deltas; prior continuation, observation and publisher-directory repairs retained |

The current Switchboard source has 1,056 passing source tests and verified hot deployment (`hot_reload`), with no App restart or permission/model changes. Unsupported execution, synthetic stale catalogs and unknown unbound chains remain non-dispatching; full-request helper behavior is retained. Original affected target-task recovery remains unverified; no latency/cost percentage is assigned to this protocol repair. Caller and Rust source remain unchanged, with no new Rust rebuild or universal runtime version: the runtime release remains V464. Trading behavior is unchanged; private diagnostics and compiled caches remain excluded.

Measurements retain their actual source/policy composition in [Benchmark observations](docs/benchmark-summary.md). The newly accepted complete concurrent frozen R19 pair used earlier Switchboard `372c9b26b65b3ce9319877651bf63a1696232b24`, before the catalog repair: Efficient 240.9s/$0.2512/489,262 total tokens versus native Sol 390.1s/$0.5439/1,399,271 tokens (-38.25% time/-53.82% cost), quality PASS at 67 versus 73 tests. Controls matched and root CLI usage reconciled; totals include both coordinators, two Efficient Workers and the correlated native child. Complete reported usage at fixed historical 2026-10-22 API-equivalent rates is not subscription or complete provider billing. One pair proves neither a causal gain from the later repair nor stable same-ID long-workflow gains.

The earlier 89c7889 R19 observation (253.9s/$0.2467354 versus 439.4s/$0.4943428) remains separate. The negative same-ID cost result (+15.66%, with end-of-run native-binary identity drift) and the Efficient-only acceptance observation-policy pair (-13.42% cost/-2.52% time) retain their existing measurement and control limits.

These source repositories have separate histories. Their commits are provenance references, not claims that they are ancestors in this aggregate repository. Per-file hashes in releases/v464-controls.json identify the published contents. Publication-only documentation and preserved licenses are identified separately.

The caller working tree contains an unrelated test modification, and the runtime working tree contains pending-message deletions; neither was adopted. Source was exported from exact commits, not by copying dirty worktrees or installed environment directories.

The canonical runtime-release validation includes that preserved local-context test delta among its eleven test modules; it was used as a bound test input, not installed package code or a published source change. Therefore its728test stage totals are not claimed to be byte-reproducible solely from the committed public test export. The six publication-verifier tests and source hashes have their own separate scope. V463's forced-drain prototype and rejected wait tradeoff remain in Git history and its historical manifest; V464 is the current corrected surface.

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
