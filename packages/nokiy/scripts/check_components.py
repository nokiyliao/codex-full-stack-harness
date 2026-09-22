# SPDX-License-Identifier: MIT
"""Verify public component refs, trees, and ancestry against GitHub."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "components" / "nokiy-runtime.json"
JsonFetcher = Callable[[str], dict[str, Any]]


def _repo_slug(repository: str) -> str:
    prefix = "https://github.com/"
    if not repository.startswith(prefix):
        raise ValueError("component repository must be an HTTPS GitHub URL")
    slug = repository.removeprefix(prefix).removesuffix(".git").strip("/")
    if slug.count("/") != 1:
        raise ValueError("component repository must name one GitHub owner/repo")
    return slug


def _github_json(path: str) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "codex-collaboration-harness-component-check",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify_component(
    component: dict[str, Any], fetch_json: JsonFetcher = _github_json
) -> list[str]:
    """Return exact public-lineage mismatches for one component manifest."""

    errors: list[str] = []
    repository = _repo_slug(component["repository"])
    upstream = _repo_slug(component["upstream_repository"])
    ref = component["public_ref"]
    if not ref.startswith("refs/heads/"):
        return ["public_ref must be a branch under refs/heads/"]
    ref_path = urllib.parse.quote(ref.removeprefix("refs/"), safe="/")
    remote_ref = fetch_json(f"/repos/{repository}/git/ref/{ref_path}")
    if remote_ref.get("object", {}).get("sha") != component["public_ref_commit"]:
        errors.append("public_ref does not resolve to public_ref_commit")

    commit_specs = (
        (repository, "public_ref_commit", "public_ref_tree"),
        (repository, "modified_source_parent", "modified_source_tree"),
        (repository, "benchmarked_candidate_commit", "benchmarked_candidate_tree"),
        (upstream, "upstream_base_commit", "upstream_base_tree"),
    )
    commits: dict[str, dict[str, Any]] = {}
    for repo, commit_field, tree_field in commit_specs:
        commit_sha = component[commit_field]
        record = fetch_json(f"/repos/{repo}/git/commits/{commit_sha}")
        commits[commit_field] = record
        if record.get("sha") != commit_sha:
            errors.append(f"{commit_field} did not resolve exactly")
        if record.get("tree", {}).get("sha") != component[tree_field]:
            errors.append(f"{tree_field} does not match {commit_field}")

    public_parents = {
        item.get("sha") for item in commits["public_ref_commit"].get("parents", [])
    }
    if component["modified_source_parent"] not in public_parents:
        errors.append("modified_source_parent is not a parent of public_ref_commit")

    for base_field, head_field in (
        ("upstream_base_commit", "benchmarked_candidate_commit"),
        ("benchmarked_candidate_commit", "modified_source_parent"),
        ("modified_source_parent", "public_ref_commit"),
    ):
        base = component[base_field]
        head = component[head_field]
        comparison = fetch_json(f"/repos/{repository}/compare/{base}...{head}")
        if comparison.get("status") not in {"ahead", "identical"}:
            errors.append(f"{base_field} is not an ancestor of {head_field}")

    for required_path in ("LICENSE", "MODIFICATIONS.md"):
        encoded = urllib.parse.quote(required_path, safe="/")
        content = fetch_json(
            f"/repos/{repository}/contents/{encoded}"
            f"?ref={component['public_ref_commit']}"
        )
        if content.get("type") != "file":
            errors.append(f"public component is missing {required_path}")
    return errors


def main() -> int:
    try:
        component = json.loads(MANIFEST.read_text(encoding="utf-8"))
        errors = verify_component(component)
    except (KeyError, OSError, ValueError, urllib.error.URLError) as error:
        print(f"component conformance check failed: {error}")
        return 1
    if errors:
        print("component conformance check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "component conformance check passed: public ref, trees, ancestry, "
        "license, and modification notice"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
