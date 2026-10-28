# Provenance and Attribution

## This Repository

The Python implementation, synthetic fixtures, and documentation in this
repository were authored as an original, from-scratch, domain-neutral reference
implementation for public review. They are licensed under the repository's MIT
license.

This repository does not redistribute a private application, private task
corpus, raw agent transcript, local session database, protected runtime receipt,
credential, account data, or host-specific runtime identity.

## Related Systems

The contribution boundary is intentionally explicit:

| System | Relationship | Included here? |
|---|---|---|
| OpenAI Codex | Intended collaboration context and external runtime family | No OpenAI source, service code, or private API material |
| Native Tura role | Preferred thin execution-policy profile for Native Codex | The exact MIT role resource is packaged; Native Codex retains persistence, lifecycle, tools, effects, and callback ownership |
| External Tura runtime | Historical and optional lineage for lifecycle, receipt, recovery, and continuation engineering | No Tura source is vendored into the MIT wheel; the public AGPL fork is bound by an exact component manifest and connected only through the optional MIT adapter |
| Deep Context Federation | Separate public MIT companion/inspiration for read-only evidence reconstruction | No vendored code and no runtime dependency |
| Private collaboration deployments | Engineering experience that motivated the generic control graph | No corpus, raw receipt, local identity, or deployment configuration |

"Inspired by" does not mean forked from, compatible with, endorsed by, or
accepted upstream. This repository is the original reference implementation of
the generic graph it documents; the related systems retain their own authorship,
licenses, trademarks, and release histories.

## Third-Party Inventory

At initial publication, runtime code is Python standard-library only. The
repository does not vendor third-party code or assets. Packaging and development
metadata remain subject to review at each release; the exact dependency list in
the source tree is authoritative.

The MIT license covers this repository's contribution. It does not relicense or
grant rights to external systems, services, trademarks, or non-public evidence.

## Public-Data Hygiene

Before a public release, reviewers should verify the complete Git history and
artifacts for:

- secrets and credentials;
- absolute host or account paths;
- raw conversation/session identities;
- private task contents and benchmark payloads;
- proprietary runtime receipts or binaries;
- uncredited third-party code/assets;
- generated archives that bypass normal source review.

Synthetic examples should use obvious placeholders and stable fake identifiers.
Hash-only evidence must still be reviewed: a digest can disclose correlation or
the existence of a private object even when it does not reveal its contents.

## Trademark and Affiliation

This independent project is not an official OpenAI product and is not endorsed
by OpenAI. OpenAI and Codex names belong to their respective owner. Their use
here describes intended context, not sponsorship or source provenance.
