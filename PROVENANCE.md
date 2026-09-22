# Source Provenance

## Nokiy v9

This snapshot includes `packages/nokiy` (MIT, caller `0.3.21.dev0`) and
`runtime` (AGPL-3.0-or-later, modified Tura-derived engine). Their licenses and
notices are preserved. `releases/v9.json` records base commits and per-file hashes,
including local uncommitted modifications. The base commits do not identify
clean snapshots. Installed caller v9 reuses runtime-image v8; binary hashes do not
constitute a reproducible-build claim. The older component identities below are
lineage references, not the complete current snapshot.

This project is a new integration and product boundary, not a claim of clean-room
implementation.

## Tura lineage

- Upstream: [Tura-AI/tura](https://github.com/Tura-AI/tura)
- Modified public fork: [nokiyliao/tura](https://github.com/nokiyliao/tura)
- Published fork source identity: `5c1c45c53a3ce29750f31c9d37eb7a326a3f5c78`
- License: `AGPL-3.0-or-later`

Files derived from Tura must retain applicable notices and identify their source
preimage when they enter this repository. Git history and per-file notices remain
authoritative; this summary does not reassign upstream authorship.

## Companion projects

| Component | Published identity | License |
| --- | --- | --- |
| Codex Collaboration Harness | `v0.2.0` | MIT |
| Deep Context Federation | `v0.91.0` | MIT |
| Codex Session Governor | `v0.4.0` | MIT |

References to these projects do not relicense them. Any future vendoring must
preserve their notices and record the exact imported revision.
