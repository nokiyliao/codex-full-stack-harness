# Source Provenance

## Current V464 Surface

This is a source publication, not a redistribution of installed executables or machine configuration.

| Component | Source commit | Correspondence |
| --- | --- | --- |
| Python caller | `ffafb43816ea188b080a7978894ca98711625e9b` | Unchanged package source; all 48 non-cache installed package files match byte-for-byte; installed release wheel version 0.3.464.dev0 |
| Rust worker payload | `1304065c69b9c5c6a702a05754526395c9a41678` | All 602 recorded build-source inputs match; rebuilt with cargo --locked, four binary identities match the installed V464 image; fail-fast retained, ready metrics reused, cancelled usage UNKNOWN |
| Switchboard | `fa0b537ada314cdc17274e27d3956533bb4f3668` | Projects optional public notes with their model-claim classification and limits Efficient native implementation to one coherent branch; model/effort topology unchanged; private diagnostics and compiled caches excluded |

These source repositories have separate histories. Their commits are provenance references, not claims that they are ancestors in this aggregate repository. Per-file hashes in releases/v464.json identify the published contents. Publication-only documentation and preserved licenses are identified separately.

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
