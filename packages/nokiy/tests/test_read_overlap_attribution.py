# SPDX-License-Identifier: MIT
"""Bounded attribution of synthetic, already-receipted source-read outputs."""
from copy import deepcopy
import unittest

from codex_collaboration_harness import result_inspection as inspector


_ABSENT = object()
_COUNTERS = ("range_after_search", "search_after_range", "range_after_range",
             "search_after_search", "unknown_mode")


def _output(rows=((1, "café"),), *, search=False, mode=_ABSENT, total=1,
            path="src/example.py", sha="b" * 64, numbered=True,
            call_id="nokiy.tool.command_run:call_overlap:0"):
    end = rows[-1][0] if rows else 0
    output = {
        "path": path, "source_sha256": sha,
        "start_line": rows[0][0] if rows else 1, "end_line": end, "total_lines": total,
        "line_numbers": numbered,
        "stdout": "".join(f"{n}: {text}\n" if numbered else text + "\n" for n, text in rows),
        "exit_code": 0, "at_eof": end == total,
        "next_line": None if end == total else end + 1,
        "ends_with_newline": bool(rows), "truncated": False, "truncation_reason": None,
        "terminal_receipt": {
            "schema_version": "tura_command_terminal_receipt_v1", "exit_code": 0,
            "outcome": "known", "terminal_state": "completed", "process_reaped": True,
            "process_group_empty": True, "termination_proven": True, "call_id": call_id},
    }
    if search:
        output["search_matches"] = [number for number, _ in rows]
    if mode is not _ABSENT:
        output["mode"] = mode
    return output


def _add(reads, output, receipt="matched"):
    reads.add({"command_type": "source_read"}, output, receipt)


class ReadOverlapAttributionTests(unittest.TestCase):
    def assert_summary(self, reads, expected, *, coverage="complete", aggregate_coverage="complete",
                       index_complete=True, proof_complete=True):
        summary = reads.summary(index_complete, proof_complete)
        overlap = summary["overlap_classification"]
        self.assertEqual(set(overlap), set(_COUNTERS) | {"schema_version", "scope", "coverage"})
        self.assertEqual(overlap["schema_version"], "nokiy_source_read_overlap_v1")
        self.assertEqual(overlap["scope"], "observed_command_outputs_only")
        self.assertEqual(overlap["coverage"], coverage)
        self.assertEqual(summary["coverage"], aggregate_coverage)
        for key in _COUNTERS:
            self.assertIs(type(overlap[key]), int)
            self.assertGreaterEqual(overlap[key], 0)
            self.assertEqual(overlap[key], expected.get(key, 0))
        self.assertEqual(sum(overlap[key] for key in _COUNTERS), summary["repeated_lines"])
        return summary

    def test_67_search_snippet_lines_then_full_range_are_not_repeated_ranges(self):
        snippets = [(n, f"line-{n}") for n in range(2, 135, 2)]
        full = [(n, f"line-{n}") for n in range(1, 135)]
        reads = inspector._ReadEfficiency()
        _add(reads, _output(snippets, search=True, mode="search", total=134))
        _add(reads, _output(full, mode="range", total=134))
        summary = self.assert_summary(reads, {"range_after_search": 67})
        self.assertEqual(summary["returned_lines"], 201)
        self.assertEqual(summary["unique_lines"], 134)
        self.assertEqual(summary["source_bytes_excluding_newlines"],
                         sum(len(text.encode("utf-8")) for _, text in snippets + full))
        _add(reads, _output(full, total=134))
        self.assert_summary(reads, {"range_after_search": 67, "range_after_range": 134})

    def test_all_known_transitions_use_the_last_matching_observation(self):
        reads = inspector._ReadEfficiency()
        for search in (True, False, False, True, True):
            _add(reads, _output(search=search))
        summary = self.assert_summary(reads, {key: 1 for key in _COUNTERS if key != "unknown_mode"})
        self.assertEqual({key: value for key, value in summary.items() if key != "overlap_classification"},
                         {"coverage": "complete", "source_read_calls": 5, "command_run_batches": 1,
                          "source_bytes_excluding_newlines": 25, "returned_lines": 5, "unique_lines": 1,
                          "repeated_lines": 4, "skipped_unproven": 0, "conflicting_lines": 0,
                          "index_complete": True})

    def test_preceding_kind_is_per_physical_line_not_per_call(self):
        reads = inspector._ReadEfficiency()
        rows = [(1, "one"), (2, "two"), (3, "three"), (4, "four")]
        _add(reads, _output([rows[0], rows[2]], search=True, total=4))
        _add(reads, _output(rows, total=4))
        _add(reads, _output([rows[1], rows[2]], search=True, total=4))
        summary = self.assert_summary(reads, {"range_after_search": 2, "search_after_range": 2})
        self.assertEqual(summary["unique_lines"], 4)

    def test_conflicting_text_neither_matches_nor_replaces_the_last_matching_kind(self):
        reads = inspector._ReadEfficiency()
        _add(reads, _output(search=True))
        _add(reads, _output([(1, "conflict")], mode="range"))
        _add(reads, _output(mode="range"))
        _add(reads, _output(search=True))
        _add(reads, _output([(1, "conflict")], search=True))
        summary = self.assert_summary(reads, {"range_after_search": 1, "search_after_range": 1},
                                      coverage="unproven", aggregate_coverage="unproven")
        self.assertEqual(summary["conflicting_lines"], 2)
        self.assertEqual(summary["returned_lines"], 5)
        self.assertEqual(summary["unique_lines"], 1)

    def test_paths_and_source_shas_keep_preimage_and_changed_source_separate(self):
        for changed_text in ("café", "changed source"):
            with self.subTest(changed_text=changed_text):
                reads = inspector._ReadEfficiency()
                _add(reads, _output(search=True))
                _add(reads, _output(path="src/other.py"))
                _add(reads, _output([(1, changed_text)], sha="c" * 64))
                _add(reads, _output())
                _add(reads, _output([(1, changed_text)], sha="c" * 64, search=True))
                _add(reads, _output(path="src/other.py", search=True))
                summary = self.assert_summary(reads, {"range_after_search": 1, "search_after_range": 2})
                self.assertEqual(summary["unique_lines"], 3)
                self.assertEqual(summary["conflicting_lines"], 0)

    def test_mode_and_search_metadata_are_classified_without_guessing(self):
        cases = [({}, "range"), ({"mode": "range"}, "range"),
                 ({"search_matches": [1]}, "search"),
                 ({"search_matches": [1], "mode": "search"}, "search"),
                 ({"search_matches": []}, "search"),
                 ({"search_matches": [], "mode": "search"}, "search")]
        unknown = [{"mode": value} for value in ("search", None, "", "other", 7, {}, [])]
        unknown += [{"search_matches": [1], "mode": value} for value in ("range", None, "other")]
        unknown += [{"search_matches": None}, {"search_matches": None, "mode": "range"},
                    {"search_matches": None, "mode": "search"}, {"search_matches": [], "mode": "range"}]
        for metadata, kind in cases + [(metadata, "unknown") for metadata in unknown]:
            with self.subTest(metadata=metadata):
                reads = inspector._ReadEfficiency()
                _add(reads, _output(search=True))
                output = _output()
                output.update(metadata)
                _add(reads, output)
                _add(reads, _output(mode="range"))
                _add(reads, _output())
                expected = ({"range_after_search": 1, "range_after_range": 2} if kind == "range" else
                            {"search_after_search": 1, "range_after_search": 1, "range_after_range": 1}
                            if kind == "search" else {"unknown_mode": 2, "range_after_range": 1})
                self.assert_summary(reads, expected, coverage="unproven" if kind == "unknown" else "complete")

    def test_unknown_after_unknown_and_then_known_still_advance_matching_kind(self):
        reads = inspector._ReadEfficiency()
        for output in (_output(mode=None), _output(mode="other"), _output(search=True), _output()):
            _add(reads, output)
        self.assert_summary(reads, {"unknown_mode": 2, "range_after_search": 1}, coverage="unproven")

    def test_unproven_or_failed_receipts_do_not_enter_attribution(self):
        cases = [("schema_version", "wrong"), ("outcome", "unknown"), ("terminal_state", "failed"),
                 ("exit_code", 1), ("process_reaped", False), ("process_group_empty", False),
                 ("termination_proven", False), ("call_id", "not-a-command-run-entry"), (None, None)]
        for key, value in cases:
            with self.subTest(key=key):
                reads = inspector._ReadEfficiency()
                _add(reads, _output(search=True))
                rejected = _output(mode="other")
                if key is not None:
                    rejected["terminal_receipt"][key] = value
                _add(reads, rejected, "matched" if key is not None else "unproven")
                _add(reads, _output())
                summary = self.assert_summary(reads, {"range_after_search": 1},
                                              coverage="unproven", aggregate_coverage="unproven")
                self.assertEqual(summary["source_read_calls"], 3)
                self.assertEqual(summary["returned_lines"], 2)
                self.assertEqual(summary["skipped_unproven"], 1)

    def test_identity_and_physical_line_rejections_do_not_advance_kind(self):
        cases = [{"path": ""}, {"path": 1}, {"source_sha256": "invalid"}, {"source_sha256": "B" * 64},
                 {"stdout": "unlabelled\n"}, {"stdout": "2: café\n"}, {"start_line": 0},
                 {"total_lines": 0}, {"exit_code": 1}, {"line_numbers": "true"},
                 {"search_matches": "not-a-list"}, {"search_matches": [True]},
                 {"stdout": "1: café", "ends_with_newline": False, "total_lines": 2,
                  "at_eof": False, "next_line": 2, "truncated": True, "truncation_reason": "response_limit"}]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                reads = inspector._ReadEfficiency()
                _add(reads, _output(search=True))
                rejected = _output()
                rejected.update(metadata)
                _add(reads, rejected)
                _add(reads, _output())
                summary = self.assert_summary(reads, {"range_after_search": 1},
                                              coverage="unproven", aggregate_coverage="unproven")
                self.assertEqual(summary["returned_lines"], 2)
                self.assertEqual(summary["skipped_unproven"], 1)

    def test_nested_flattened_and_direct_results_share_only_validated_observations(self):
        reads = inspector._ReadEfficiency()
        search = _output(search=True)
        proof = search.pop("terminal_receipt")
        flattened = _output(call_id="nokiy.tool.command_run:call_overlap:1")
        flattened["command_type"] = "source_read"
        output = {"results": [None, {}, {"command_type": "source_read", "output": search,
                                        "terminal_receipt": proof, "success": True}, flattened,
                              {"command_type": "task_status", "output": {"stdout": "ignored"}}]}
        item = {"command_type": "command_run"}
        before = deepcopy((item, output))
        reads.add(item, output, "matched")
        reads.add({"command": "source_read"}, _output(search=True), "matched")
        summary = self.assert_summary(reads, {"range_after_search": 1, "search_after_range": 1})
        self.assertEqual(summary["source_read_calls"], 3)
        self.assertEqual(summary["command_run_batches"], 1)
        self.assertEqual((item, output), before)

    def test_nested_entries_require_success_proof_and_matching_path(self):
        for case in ("missing_receipt", "failed", "path_mismatch", "outer_unproven"):
            with self.subTest(case=case):
                reads = inspector._ReadEfficiency()
                _add(reads, _output(search=True))
                payload = _output()
                entry = {"command_type": "source_read", "output": payload, "success": True}
                if case == "missing_receipt":
                    payload.pop("terminal_receipt")
                elif case == "failed":
                    entry["success"] = False
                elif case == "path_mismatch":
                    entry["path"] = "src/different.py"
                reads.add({"command_type": "command_run"}, {"results": [entry]},
                          "unproven" if case == "outer_unproven" else "matched")
                _add(reads, _output())
                summary = self.assert_summary(reads, {"range_after_search": 1},
                                              coverage="unproven", aggregate_coverage="unproven")
                self.assertEqual(summary["skipped_unproven"], 1)
                self.assertEqual(summary["returned_lines"], 2)

    def test_sparse_search_context_and_partial_pages_keep_empty_and_unicode_lines(self):
        reads = inspector._ReadEfficiency()
        search = _output([(2, ""), (5, "🙂")], search=True, total=6)
        search.update(start_line=1, search_matches=[5], truncated=True, truncation_reason="response_limit")
        page = _output([(1, "café"), (2, ""), (3, "tail")], total=6, numbered=False)
        page.update(truncated=True, truncation_reason="line_limit")
        final = _output([(4, "new"), (5, "🙂"), (6, "last")], total=6, numbered=False)
        final.update(stdout=final["stdout"][:-1], ends_with_newline=False,
                     truncated=True, truncation_reason="end_of_file")
        context = _output([(3, "tail"), (6, "last")], search=True, total=6)
        context["search_matches"] = [6]
        for output in (search, page, final, context):
            _add(reads, output)
        summary = self.assert_summary(reads, {"range_after_search": 2, "search_after_range": 2})
        self.assertEqual(summary["returned_lines"], 10)
        self.assertEqual(summary["unique_lines"], 6)
        self.assertEqual(summary["source_bytes_excluding_newlines"], 32)

    def test_no_read_runs_and_incomplete_aggregate_coverage(self):
        for index_complete, proof_complete in ((True, True), (False, True), (True, False), (False, False)):
            with self.subTest(index_complete=index_complete, proof_complete=proof_complete):
                coverage = "complete" if index_complete and proof_complete else "unproven"
                summary = self.assert_summary(inspector._ReadEfficiency(), {}, coverage=coverage,
                                              aggregate_coverage=coverage, index_complete=index_complete,
                                              proof_complete=proof_complete)
                self.assertEqual(summary["source_read_calls"], 0)
                self.assertEqual(summary["unique_lines"], 0)
        reads = inspector._ReadEfficiency()
        _add(reads, _output(search=True))
        _add(reads, _output())
        self.assert_summary(reads, {"range_after_search": 1}, coverage="unproven",
                            aggregate_coverage="unproven", proof_complete=False)

    def test_empty_accepted_reads_can_make_only_classification_coverage_unproven(self):
        for search, mode, coverage in ((False, _ABSENT, "complete"), (True, _ABSENT, "complete"),
                                       (False, "search", "unproven"), (True, "range", "unproven")):
            with self.subTest(search=search, mode=mode):
                reads = inspector._ReadEfficiency()
                _add(reads, _output([], total=0, search=search, mode=mode))
                summary = self.assert_summary(reads, {}, coverage=coverage)
                self.assertEqual(summary["source_read_calls"], 1)
                self.assertEqual(summary["returned_lines"], 0)
                self.assertEqual(summary["source_bytes_excluding_newlines"], 0)
                _add(reads, _output())
                self.assert_summary(reads, {}, coverage=coverage)

    def test_inputs_and_earlier_summaries_are_detached_from_later_updates(self):
        reads = inspector._ReadEfficiency()
        search, range_read = _output(search=True), _output()
        before = deepcopy((search, range_read))
        _add(reads, search)
        first = self.assert_summary(reads, {})
        snapshot = deepcopy(first)
        _add(reads, range_read)
        second = self.assert_summary(reads, {"range_after_search": 1})
        self.assertEqual(first, snapshot)
        self.assertEqual((search, range_read), before)
        second["overlap_classification"]["range_after_search"] = 999
        second["overlap_classification"]["coverage"] = "unproven"
        _add(reads, range_read)
        self.assert_summary(reads, {"range_after_search": 1, "range_after_range": 1})


if __name__ == "__main__":
    unittest.main()
