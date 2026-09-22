# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from check_components import verify_component


class ComponentConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.component = {
            "repository": "https://github.com/example/runtime",
            "upstream_repository": "https://github.com/upstream/runtime",
            "public_ref": "refs/heads/public-v1",
            "public_ref_commit": "a" * 40,
            "public_ref_tree": "1" * 40,
            "modified_source_parent": "b" * 40,
            "modified_source_tree": "2" * 40,
            "benchmarked_candidate_commit": "c" * 40,
            "benchmarked_candidate_tree": "3" * 40,
            "upstream_base_commit": "d" * 40,
            "upstream_base_tree": "4" * 40,
        }
        self.remote_trees = {
            self.component["public_ref_commit"]: "1" * 40,
            self.component["modified_source_parent"]: "2" * 40,
            self.component["benchmarked_candidate_commit"]: "3" * 40,
            self.component["upstream_base_commit"]: "4" * 40,
        }

    def fetch(self, path: str):
        component = self.component
        if "/git/ref/heads/public-v1" in path:
            return {"object": {"sha": component["public_ref_commit"]}}
        if "/contents/" in path:
            return {"type": "file"}
        if "/compare/" in path:
            return {"status": "ahead"}
        commits = {
            component["public_ref_commit"]: (
                self.remote_trees[component["public_ref_commit"]],
                [component["modified_source_parent"]],
            ),
            component["modified_source_parent"]: (
                self.remote_trees[component["modified_source_parent"]],
                [component["benchmarked_candidate_commit"]],
            ),
            component["benchmarked_candidate_commit"]: (
                self.remote_trees[component["benchmarked_candidate_commit"]],
                [component["upstream_base_commit"]],
            ),
            component["upstream_base_commit"]: (
                self.remote_trees[component["upstream_base_commit"]],
                [],
            ),
        }
        sha = path.rsplit("/", 1)[-1]
        tree, parents = commits[sha]
        return {
            "sha": sha,
            "tree": {"sha": tree},
            "parents": [{"sha": parent} for parent in parents],
        }

    def test_exact_component_lineage_passes(self) -> None:
        self.assertEqual(verify_component(self.component, self.fetch), [])

    def test_tree_drift_fails(self) -> None:
        self.component["public_ref_tree"] = "f" * 40
        self.assertIn(
            "public_ref_tree does not match public_ref_commit",
            verify_component(self.component, self.fetch),
        )


if __name__ == "__main__":
    unittest.main()
