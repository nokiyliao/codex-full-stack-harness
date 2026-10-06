# Nokiy v157 publication boundary

One aggregate version identifies a composition, not a single executable.
`releases/v157.json` binds the current caller and runtime source snapshots.
The previous public snapshot remains `v9` at commit `e331e5055d`.

The Python caller prepares context, validates request bindings, supervises Rust
execution and recovers results. The Rust engine owns the model/tool execution.
A Python launcher does not replace the runtime with a Skill.

## Identity

- Caller source head: `8950bf325b8fceb48ab8b47177e61ae6b1be6ab1`, with local uncommitted changes.
- Source package version field: `0.3.21.dev0`.
- Installed wheel version: `0.3.157.dev0`.
- Caller release: `nokiy-20261007-v157`.
- Runtime source head: `bda10dc10dd96881e151aba7af0e02fb17fd0129`, with local uncommitted changes.
- Runtime build identity: `nokiy-v157-git-and-overlap-20261007`.

## Evidence levels

- Per-file hashes bind the published source snapshots.
- Installed runtime-image and binary hashes are local installation observations;
  those binaries are not distributed here.
- Clean-machine runtime installation and reproducible binary builds remain
  unverified. Do not silently substitute an upstream Tura binary.
- Absolute installation paths are omitted from the manifest.

## Provenance and configuration

`packages/nokiy` is an MIT caller snapshot; `runtime` is an AGPL Tura-derived
snapshot with local changes. Base commits do not alone identify these dirty
trees. Existing copyright and license notices are retained. Source worktrees,
installed services, native Codex state and configuration were not modified by
this publication.

Keep HOME/CODEX_HOME and existing native authentication. Prepare context for the
actual workspace. DCF failures do not silently fall back to local mode. The
imported skill describes the original machine's paths, not a universal installer.
Deployment requires actual operator authorization and target-specific admission.

Local `output/`, `outputs/`, logs, session databases, build caches and installed
binaries are excluded. Publication does not enable a Gateway, global scheduler
or second persistent Codex session database.
