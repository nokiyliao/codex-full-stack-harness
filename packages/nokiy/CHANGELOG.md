# Changelog

All notable changes to this project will be documented here.

The format follows Keep a Changelog, and the project intends to use Semantic
Versioning after the first public release.

## [Unreleased]

## [0.3.10] - 2026-09-05

### Changed

- Reference the existing TaskPacket mission ID in read-only terminal templates
  instead of duplicating the full task instructions. The task body preserves
  the exact instructions, while capsule and callback identities and historical
  terminal decoding remain unchanged.

## [0.3.9] - 2026-09-05

### Added

- Add execution-profile v2 with `inherit`, `preferred`, and `pinned` model
  selection policies. Ordinary tasks can inherit Native Codex settings or
  request an initial model without invalidating later model changes; exact pins
  remain available for benchmarks and reproductions.
- Version the conditional `create_thread` argument shape as dispatch-plan v4.
- Preserve read compatibility for historical v1 profiles as exact pinned
  profiles.

## [0.3.8] - 2026-09-05

### Changed

- Make the read-only fast-path dispatch contract self-contained and explicitly
  forbid a redundant Skill-file read. This trades a few hundred deterministic
  prompt bytes for removal of an entire Native tool continuation; the rule is
  limited to the read-only fast path and leaves long-task Skill policy lazy.

## [0.3.7] - 2026-09-05

### Added

- Add `inspect-packets --summary` so a callback task can retain the exact full
  inventory digest without carrying every packet row through another provider
  turn.

### Changed

- Require read-only fast paths to obtain `CODEX_THREAD_ID` inside the first
  task-read batch, forbid a later identity-only tool round, and return only a
  fixed delivery acknowledgement after callback success.
- Reduce the installed Tura Skill to its executable contract; detailed topology
  remains lazy in the existing reference file.

## [0.3.6] - 2026-09-05

### Added

- Add read-only `tura-taskpacket inspect-packets` inventory with deterministic
  `CURRENT_PROFILED`, `LEGACY_READABLE`, and `REJECTED` classifications.
- Report exact dispatch, task-projection, J-Space-policy UTF-8 byte counts and
  inline evidence-reference count in Native dispatch plan v2.
- Bind each compact dispatch and Skill installation receipt to the canonical
  three-member Tura Skill contract digest.

### Changed

- Move repeated execution policy out of every Native dispatch and keep it in the
  versioned Skill. A representative read-only context dispatch falls from 4,331
  to 2,971 UTF-8 bytes while retaining its exact terminal template.
- Accept historical digest-only filenames for immutable v1 packet reads without
  migrating them or making unprofiled packets dispatchable.
- Bound Native terminal callbacks to 65,536 UTF-8 bytes and 32 evidence items;
  larger artifacts must be returned by immutable reference and digest.

## [0.3.5] - 2026-09-05

### Fixed

- Prevent read-only fast-path probes from assigning zsh special parameters such
  as `path` and `status`, and require hidden-root-aware file enumeration. This
  removes two observed pre-terminal command failures without adding a helper
  process or alternate execution path.

## [0.3.4] - 2026-09-05

### Fixed

- Embed the exact canonical two-line terminal template in every read-only fast
  path dispatch. The worker no longer needs source/test discovery and cannot
  silently substitute a historical callback marker that parent intake rejects.

## [0.3.3] - 2026-09-05

### Changed

- Emit `NATIVE_TURA_READ_ONLY_FAST_PATH_V1` when every admitted execution scope
  is explicitly `read:`-only. The worker uses one batched read stage, resolves
  its task identity from `CODEX_THREAD_ID`, and proceeds directly to the single
  canonical callback.
- Remove redundant child-side package/test discovery, terminal render/parse
  self-verification, and progress narration from the read-only path. Parent
  callback parsing, exact task bindings, and no-blind-retry remain unchanged.

## [0.3.2] - 2026-09-05

### Fixed

- Define Native callback success from the actual `CallToolResult` contract,
  avoiding false `CALLBACK_DELIVERY_UNSETTLED` results when the official tool
  returns a destination content block without `structuredContent.status`.
- Fix Native Tura dispatch reasoning at the supported `max` ceiling and reject
  `ultra` rather than silently relabeling or downgrading a task.
- Prevent every terminal path from releasing a packet lease while a persisted
  step effect remains unsettled, without partially recording the final effect.
- Convert malformed executor returns into harness-origin typed failures that
  retain the exact lease and explicit reconciliation path.
- Require recovery steps to bind an admitted proposal action and match its
  operation digest and effect class.
- Preserve existing content-addressed identities for non-recovery step records.

### Added

- Add a content-addressed Native execution profile that compiles exact model,
  reasoning, and official `create_thread` target arguments without creating a
  second dispatcher or lifecycle owner.
- Add `tura-taskpacket prepare-dispatch` so a Commander can mechanically prepare
  one first-class Native task from a verified profile-bound capsule.
- Add a canonical `[TURA_NATIVE_TERMINAL_V1]` JSON callback envelope, parser,
  and schema with exact callback, parent, and task identity checks.
- Publish one shared task-projection schema and validator for bounded DCF/J-Space
  context producers.
- Add an explicit, rollback-preserving `install-skill --replace` adoption path
  while keeping drift rejection as the default behavior.
- Add a compact `tura-taskpacket load --format dispatch` view that invokes the
  Native Tura Skill with a pre-verified task projection and J-Space policy,
  avoiding an extra bootstrap tool turn and repeated evidence hashing.
- Package the source-authoritative `$tura-kernel` Skill, metadata, and Native
  topology, plus an atomic `tura-taskpacket install-skill` parity installer.
- Verify exact Skill member identities in source tests, public readiness,
  wheel/sdist inspection, and clean-install smoke coverage.
- Add a content-addressed Native Tura task capsule and dependency-free
  `tura-taskpacket` loader so a Native Codex child can recover its exact bounded
  task from its canonical task name without a Codex core patch or second
  lifecycle service.
- Support monotonic immutable packet revisions for a resumed Native child;
  loading selects the unique highest revision without a mutable current pointer.

### Changed

- Verify every source package member byte-for-byte in both wheel and sdist,
  then repeat those checks after a clean isolated wheel install.
- Remove stale reproducible packaging state and require two builds under the
  same `SOURCE_DATE_EPOCH` to produce identical artifact digests, including a
  content-preserving normalization of sdist ownership, timestamps, and order.
- Move networked external-runtime lineage checks to a separate scheduled/manual
  component-conformance workflow so they cannot block Native package releases.
- Bind the packaged Native Tura role to the verified capsule bootstrap when a
  dynamic parent message is unavailable or unreadable.

## [0.2.1] - 2026-09-03

### Changed

- Introduce an internal adapter-contract facade and route the Tura adapter
  through it, preserving the existing public classes, Enum identities, wire
  names, and content-addressed records while reducing direct coupling to the
  monolithic `core.py` implementation.
- Package the reviewed thin Native Tura agent role and document Native Codex as
  the sole persistence, lifecycle, tool, effect, and callback owner.
- Reclassify the external Tura runtime adapter and AGPL component as an optional
  compatibility and provenance profile rather than a Native deployment
  dependency.

## [0.2.0] - 2026-08-31

### Breaking changes and migration

- `MissionReadback` is replaced by `MissionSnapshotReadback`. Integrations must
  send the complete ordered predicate truth vector plus a monotonic
  `parent_state_sequence`; a partial or stale readback is no longer accepted.
- Direct `TaskPacket` construction now requires `recovery_budget`, and direct
  terminal/verification/callback record construction requires the new
  failure-origin, parent-snapshot, convergence, or delivery-reconciliation
  identities appropriate to that record. Prefer `CollaborationHarness.plan()`
  and the staged harness methods instead of constructing derived records.
- The Tura request wire identity changes because `recovery_budget` and
  `protocol_version=tura-collaboration/v1` are now canonical request fields.
  Consumers must regenerate request identities and validate against the
  packaged v1 schema/golden vectors; persisted v0.1 request identities are not
  replay-compatible with v0.2.
- String lookalikes for Enum values and truthy/falsy non-boolean values are now
  rejected at runtime. Adapters must decode external JSON once at their wire
  boundary and pass canonical Python Enum/bool/int values into the core.

### Fixed

- Enforce canonical Enum, bool, integer, packet, effect, and failure-origin
  types at runtime instead of relying on Python annotations or truthiness.
- Replace permanent predicate latching with complete parent snapshots and a
  monotonic parent-state sequence; stale snapshots cannot overwrite newer
  mission truth.
- Persist callback delivery start/uncertainty and require an exact destination
  absence or convergence proof before retry or acknowledgement.
- Preserve store-classified duplicate-effect failures as reconcilable attempts
  instead of stranding their leases.
- Prevent policy, authority, effect-uncertainty, and other non-local blockers
  from self-labelling as safe local recovery.
- Verify content-addressed task packets before granting a lease.

### Added

- Typed step attempts, blockers, model-generated recovery proposals,
  deterministic recovery admission, duplicate action fingerprints, and
  parent-bounded recovery budgets.
- Parent-authored route dispositions and evidence-only mission supersession
  classification.
- Content-addressed identities for parent snapshots, convergence proofs,
  acknowledgements, terminal receipts, and recovery records.
- Versioned Tura request/terminal JSON schemas and cross-language golden
  request/result/failure vectors packaged in the wheel.
- Networked public Tura component validation for exact branch, trees, ancestry,
  license, and modification notice.
- Pinned GitHub Actions, clean-wheel installation verification, deterministic
  checksums, and tag-triggered build provenance attestation.

### Boundaries

- The package remains an in-memory reference. Durable event logs,
  transactional outbox/inbox, distributed fencing, authenticated transports,
  real effect authority, and installed/runtime acceptance remain integration
  responsibilities.

## [0.1.1] - 2026-08-31

### Fixed

- Preserve a Tura executor's typed failure or rejection code, detail digest,
  and observed effect identity across the adapter-to-core boundary instead of
  flattening the outcome to `EXECUTOR_ERROR`.
- Require explicit effect reconciliation after a structured executor failure
  and reject a second execution attempt before it can reach the Tura client.

### Added

- Generic `ExecutorFailureSignal` support for provider-neutral structured
  executor failures.
- End-to-end Tura composition fixtures for settled-effect and proven-no-effect
  failure paths.

## [0.1.0] - 2026-08-31

### Added

- Original, domain-neutral Python collaboration state machine.
- Ordered mission predicates and first-false selection.
- Bounded task packets with route, expected delta, and abandon condition.
- In-memory lease/CAS ownership reference.
- Typed execution, same-attempt effect reconciliation, terminal receipt,
  destination, convergence, and mission verification records.
- Exact task-lease release at terminal receipt persistence and stable
  continuation identity across callback recovery.
- Deterministic end-to-end and failure-path tests.
- Public architecture, trust-boundary, provenance, security, governance, and
  review documentation.

### Limitations

- No durable/distributed store, production network transport, worker sandbox,
  cryptographic receipt, or production effect authorization.
- Internal benchmark context is not reproducible from this repository and is
  not evidence of this public implementation's performance.
