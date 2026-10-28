# SPDX-License-Identifier: MIT
"""Member change delivery: exact diffs for parent review; offline, no provider calls."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_collaboration_harness import batch
from test_nokiy_batch import BatchFixture


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class MemberChanges(BatchFixture):
    def run_edits(self, edits, *, status=None):
        def execute(request):
            index = self.requests.index(request)
            edits[index](self.workspace / f"output-{index}.txt")
            return self.terminal(request, *( (status[index], True) if status else ()))
        self.mocks(execute=execute)
        return batch.run_batch(self.plan, self.digest)

    def test_modify_and_create_deliver_exact_bound_diffs(self):
        (self.workspace / "output-0.txt").write_text("alpha\nbeta\n")
        result = self.run_edits([lambda p: p.write_text("alpha\nBETA\ngamma\n"),
                                 lambda p: p.write_text("created")])
        first, second = (row["changes"] for row in result["members"])
        self.assertEqual(first["status"], "COMPLETE")
        self.assertEqual(first["files"], [{"path": "output-0.txt", "change": "modify",
                                           "preimage_sha256": sha("alpha\nbeta\n"),
                                           "postimage_sha256": sha("alpha\nBETA\ngamma\n"),
                                           "added": 2, "removed": 1}])
        self.assertEqual(second["files"][0]["change"], "create")
        self.assertIsNone(second["files"][0]["preimage_sha256"])
        diff = Path(first["diff_path"]).read_text()
        self.assertIn("--- a/output-0.txt\n+++ b/output-0.txt\n", diff)
        self.assertIn(" alpha\n-beta\n+BETA\n+gamma\n", diff)
        self.assertIn("\\ No newline at end of file\n", Path(second["diff_path"]).read_text())
        self.assertEqual(hashlib.sha256(Path(first["diff_path"]).read_bytes()).hexdigest(), first["diff_sha256"])
        # Published once, create-only and read-only: recovery never recomputes it.
        self.assertFalse(Path(first["diff_path"]).stat().st_mode & 0o222)
        recovered = batch._read_rows(batch.load_plan(self.plan, self.digest))
        self.assertEqual(recovered[self.requests[0].request_id]["changes"], first)
        summary = batch.expand_summary(batch.summarize(result))
        delivered = summary["members"][0]["changes"]
        self.assertEqual(delivered["diff"], diff)
        self.assertFalse(delivered["diff_inline_truncated"])
        self.assertEqual(summary["members"][1]["changes"]["files"], second["files"])

    def test_failed_member_still_reports_its_partial_edit(self):
        result = self.run_edits([lambda p: p.write_text("partial\n"), lambda p: None],
                                status=["BLOCKED", "RESULT_AVAILABLE"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["members"][0]["changes"]["files"][0]["change"], "create")
        untouched = result["members"][1]["changes"]
        self.assertEqual((untouched["status"], untouched["files"], untouched["diff_bytes"]), ("COMPLETE", [], 0))
        self.assertNotIn("diff_path", untouched)

    def test_read_only_batch_keeps_the_legacy_summary(self):
        for index, request in enumerate(self.requests):
            self.write_scopes(request.jspace_contract.path, [f"input-{index}.txt"], [])
            request.jspace_contract.sha256 = hashlib.sha256(request.jspace_contract.path.read_bytes()).hexdigest()
        self.mocks()
        result = batch.run_batch(self.plan, self.digest)
        self.assertTrue(all("changes" not in row for row in result["members"]))
        self.assertEqual(list(self.requests[0].artifact_root.iterdir()), [])
        with patch.object(batch, "_fit_summary", wraps=batch._fit_summary) as fit:
            batch.summarize(result)
        self.assertEqual(fit.call_args.kwargs["max_bytes"], batch.MAX_PROJECTED_BYTES)

    def test_inline_budget_keeps_small_diffs_whole_and_cuts_large_at_a_line(self):
        big = "".join(f"line {n}\n" for n in range(2000))
        result = self.run_edits([lambda p: p.write_text(big), lambda p: p.write_text("small\n")])
        with patch.object(batch, "MAX_INLINE_DIFF_BYTES", 6000):
            summary = batch.expand_summary(batch.summarize(result))
        large, small = (row["changes"] for row in summary["members"])
        self.assertFalse(small["diff_inline_truncated"])
        self.assertTrue(large["diff_inline_truncated"])
        self.assertTrue(large["diff"].endswith("\n"))
        self.assertTrue(Path(large["diff_path"]).read_text().startswith(large["diff"]))
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=True)), batch.MAX_BATCH_SUMMARY_BYTES)

    def test_unavailable_preimage_is_explicit_and_never_blocks_the_batch(self):
        with patch.object(batch, "_read_plain", side_effect=OSError("unreadable")):
            result = self.run_edits([lambda p: p.write_text("a\n"), lambda p: p.write_text("b\n")])
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        self.assertEqual({row["changes"]["status"] for row in result["members"]}, {"UNAVAILABLE"})


def unified(files: dict[str, tuple[str | None, str | None]]) -> str:
    return "".join(batch._unified(name, None if old is None else old.encode(),
                                  None if new is None else new.encode())[0]
                   for name, (old, new) in files.items())


class InlineDiffAllocation(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def row(self, name: str, text: str) -> dict:
        data = text.encode()
        path = self.root / f"{name}.diff"
        path.write_bytes(data)
        return {"changes": {"status": "COMPLETE", "files": [], "diff_bytes": len(data),
                            "diff_sha256": hashlib.sha256(data).hexdigest(), "diff_path": str(path)}}

    def test_sections_split_by_hunk_counts_and_rejoin_exactly(self):
        # A removed SQL comment renders as a '--- ' line inside a hunk, not a new file.
        text = unified({"db/schema.sql": ("-- old\nkeep\n", "keep\n"),
                        "gone.py": ("x = 1", None), "new.py": (None, "y = 2\n")})
        sections = batch._diff_sections(text)
        self.assertEqual([path for path, _ in sections], ["db/schema.sql", "gone.py", "new.py"])
        self.assertEqual("".join(part for _, part in sections), text)
        self.assertIsNone(batch._diff_sections(text.replace("@@ -1,2 +1 @@", "@@ -1,3 +1 @@")))

    def test_every_members_implementation_precedes_any_tests(self):
        def body(prefix: str, count: int) -> str:
            return "".join(f"{prefix}_{n} = {n}\n" for n in range(count))
        # Whole-diff-smallest-first would inline b completely (with its tests) and cut a's
        # implementation to a short prefix; fair allocation keeps both implementations.
        a = unified({"pkg/core.py": (body("old", 5), body("core", 260)),
                     "tests/test_core.py": (None, body("case", 400))})
        b = unified({"pkg/io.py": (None, body("io", 60)),
                     "pkg/io_test.py": (None, body("check", 300))})
        rows = [self.row("a", a), self.row("b", b)]
        impl = {name: part for diff in (a, b) for name, part in batch._diff_sections(diff)}
        budget = batch._json_cost(impl["pkg/core.py"]) + batch._json_cost(impl["pkg/io.py"]) + 3000
        self.assertLess(batch._json_cost(b), budget)
        self.assertLess(budget, batch._json_cost(a))
        batch._inline_diffs(rows, budget)
        first, second = (row["changes"] for row in rows)
        self.assertTrue(first["diff"].startswith(impl["pkg/core.py"]))
        self.assertTrue(second["diff"].startswith(impl["pkg/io.py"]))
        self.assertEqual((first["diff_inline_truncated"], second["diff_inline_truncated"]), (True, True))
        self.assertEqual([f["path"] for f in first["diff_inline_files"]], ["tests/test_core.py"])
        self.assertEqual([f["path"] for f in second["diff_inline_files"]], ["pkg/io_test.py"])
        for changes in (first, second):
            self.assertTrue(changes["diff"].endswith("\n"))
            self.assertIn(changes["diff_inline_files"][0]["inline"], {"partial", "omitted"})
        added = sum(len(json.dumps({"diff": c["diff"], "diff_inline_files": c["diff_inline_files"]},
                                   ensure_ascii=True, sort_keys=True)) for c in (first, second))
        self.assertLessEqual(added, budget + 2 * len('{"diff": , }') + 64)

    def test_unparsed_diff_falls_back_to_one_prefix_without_manifest(self):
        text = "".join(f"free form {n}\n" for n in range(500))
        rows = [self.row("raw", text)]
        batch._inline_diffs(rows, 4000)
        changes = rows[0]["changes"]
        self.assertNotIn("diff_inline_files", changes)
        self.assertTrue(changes["diff_inline_truncated"])
        self.assertTrue(changes["diff"] and text.startswith(changes["diff"]))
        self.assertTrue(changes["diff"].endswith("\n"))
        self.assertLessEqual(batch._json_cost(changes["diff"]), 4000)
