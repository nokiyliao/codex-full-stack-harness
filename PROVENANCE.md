# Source Provenance

## Current V461 Surface

This is a source publication, not a redistribution of installed executables or machine configuration.

| Component | Source commit | Correspondence |
| --- | --- | --- |
| Python caller | `62bb896be5693217aad4b7efd86dde5526826169` | Verified parent source-context delivery and explicit-null capture omission repair; all 48 non-cache installed package files match byte-for-byte; installed wheel version 0.3.461.dev0 |
| Rust worker payload | `5154083b3a2cee92dc3264920f0df480f0fda389` | All 602 recorded build-source inputs match; rebuilt with cargo --locked, four binary identities match the installed V457 image |
| Switchboard | `472f90e35df498dc1825881d2b1267584b13cbcf` | Preserves verified definition/delta schema semantics in parent delivery, retaining stable external-evidence preparation identity; unchanged workflow topology; private diagnostics and compiled caches excluded |

These source repositories have separate histories. Their commits are provenance references, not claims that they are ancestors in this aggregate repository. Per-file hashes in releases/v461.json identify the published contents. Publication-only documentation and preserved licenses are identified separately.

The caller working tree contains an unrelated test modification, and the runtime working tree contains pending-message deletions; neither was adopted. Source was exported from exact commits, not by copying dirty worktrees or installed environment directories.

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
