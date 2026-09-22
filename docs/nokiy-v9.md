# Nokiy v9 publication boundary

One aggregate version identifies a composition, not a single executable.
The installed caller v9 binds the runtime image previously packaged under v8.
`releases/v9.json` records both component identities without relabeling binaries.

The Python caller prepares context, validates request bindings, supervises Rust
execution and recovers results. The Rust engine owns the model/tool execution.
A Python launcher does not replace the runtime with a Skill.

## Evidence levels

- Per-file hashes bind the published source snapshots.
- Installed runtime-image and binary hashes are local installation observations;
  those binaries are not distributed here.
- Clean-machine runtime installation and reproducible binary builds remain
  unverified. Do not silently substitute an upstream Tura binary.
- The publication-time Nokiy audit returned `NOKIY_EMBEDDED_TOOL_LOOP_NOT_OBSERVED`:
  its read-only action omitted command permission required by the attempted
  shell command. It is not a passing runtime canary; no automatic retry occurred.

## Provenance and configuration

`packages/nokiy` is an MIT caller snapshot; `runtime` is an AGPL Tura-derived
snapshot with local changes. Base commits do not alone identify these dirty
trees. Existing copyright and license notices are retained. Source worktrees,
installed services, native Codex state and configuration were not modified.

Keep HOME/CODEX_HOME and existing native authentication. Prepare context for the
actual workspace. DCF failures do not silently fall back to local mode. The
imported skill describes the original machine's paths, not a universal installer.
Deployment requires actual operator authorization and target-specific admission.

Optional and historical paths remain in the imported sources for their dependency
tree and tests. Publication does not enable a Gateway, global scheduler or second
persistent Codex session database.
