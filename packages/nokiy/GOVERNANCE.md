# Governance

## Model

The project uses maintainer-led, evidence-based governance. Maintainers are
responsible for releases, security response, repository administration, and
the accuracy of public claims.

The current maintainer list is the set of repository owners with merge access.
This file does not grant access or external authority.

## Decision Process

Routine fixes may be merged after review and a passing public test suite.
Changes to state semantics, trust boundaries, persistence, provider adapters,
or compatibility commitments require a public design issue before merge.

Decisions prefer, in order:

1. preserving mission ownership and fail-closed behavior;
2. executable public evidence over prose or private observations;
3. the smallest domain-neutral mechanism that proves the invariant;
4. backward compatibility after correctness and security.

A maintainer documents material decisions in the issue or pull request. There
is no claim of consensus when a maintainer must choose among alternatives.

## Releases

A release requires:

- passing tests from a clean checkout on a supported Python version;
- an updated changelog;
- review of public API and trust-boundary changes;
- dependency, license, and secret checks appropriate to the release;
- an annotated source tag matching the packaged version and exact commit;
- GitHub Actions pinned by full commit SHA;
- a clean isolated wheel install, exact wheel/sdist checksums, and build
  provenance attestation.

A source commit, package candidate, published artifact, installed package, and
running integration are distinct states. Release notes must say which state was
actually verified.

## Maintainer Changes

New maintainers should have sustained, constructive contributions and sound
judgment around security and evidence claims. Existing maintainers may grant or
remove access through the hosting platform. When practical, a maintainer should
not make the sole decision on a complaint or security report involving
themselves.

## Project Scope

The repository owns the generic reference state machine and its public
fixtures. It does not own external agent runtimes, provider services, user
missions, deployment authority, or irreversible effects.
