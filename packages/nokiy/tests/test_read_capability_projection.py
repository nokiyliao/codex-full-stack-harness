"""A DCF evidence capsule is not a substitute for tool capability discovery."""
import copy
import json
import shlex
import unittest
from unittest.mock import patch

from codex_collaboration_harness import full_core as core


class EvidenceOnlyCompletionPromptTests(unittest.TestCase):
    def _grants(self):
        source = {
            "authorization_semantic_sha256": "a" * 64,
            "source_read": True, "allowed_operations": ["command", "read"],
            "read_scopes": ["src/answer.py"], "write_scopes": [],
            "denied_operations": ["delete", "network"],
        }
        verifiers = [{"argv": ["python", "tests/verify.py"]}]
        return {
            "none": {},
            "command_only": {"allowed_operations": ["command"]},
            "no_command": {"allowed_operations": ["read"], "source_read": True,
                           "verifier_commands": verifiers},
            "source": source,
            "verifier": {**source, "source_read": False, "allowed_operations": ["command"],
                         "verifier_commands": verifiers},
            "source_verifier": {**source, "verifier_commands": verifiers},
            "source_write": {**source, "allowed_operations": ["command", "read", "modify"],
                             "write_scopes": ["src/answer.py"]},
            "source_write_verifier": {
                **source, "allowed_operations": ["command", "read", "modify"],
                "write_scopes": ["src/answer.py"], "verifier_commands": verifiers},
            "directory": {**source, "source_read": False, "read_scopes": ["src/**"],
                          "read_commands": {
                              "roots": ["src"],
                              "rg": {"path": "/opt/pinned tools/rg", "sha256": "b" * 64},
                              "cat": {"path": "/bin/cat", "sha256": "c" * 64}}},
        }

    def test_default_bytes_and_exact_opt_in_across_every_prompt_route(self):
        guidance = core._EVIDENCE_ONLY_COMPLETION_GUIDANCE
        marker = "Verified execution capability (caller projection):\n"
        for shape, grant in self._grants().items():
            before = copy.deepcopy(grant)
            for prompt in ("original", "original\n", "résumé"):
                with self.subTest(shape=shape, prompt=prompt):
                    default = core._provider_prompt(prompt, grant)
                    explicit = core._provider_prompt(prompt, grant, terminal_delivery="assistant_reply")
                    evidence = core._provider_prompt(prompt, grant, terminal_delivery="evidence_only")
                    self.assertEqual(default.encode("utf-8"), explicit.encode("utf-8"))
                    self.assertNotIn("Evidence-only completion:", default)
                    self.assertEqual(evidence.count(guidance), 1)
                    # The legacy rendering changes only by the opt-in input appendix.
                    self.assertEqual(evidence.encode("utf-8"),
                                     core._provider_prompt(prompt + guidance, grant).encode("utf-8"))
                    self.assertTrue(evidence.endswith(core.CAPABILITY_GAP_GUIDANCE))
                    if shape in {"none", "command_only", "no_command"}:
                        self.assertNotIn(marker, evidence)
                    else:
                        projections = [json.loads(text.split(marker, 1)[1].splitlines()[0])
                                       for text in (default, evidence)]
                        self.assertEqual(projections[0], projections[1])
                        self.assertEqual("source_read" in projections[1], grant["source_read"])
                        self.assertEqual("focused_verifier" in projections[1],
                                         "verifier_commands" in grant)
                        self.assertEqual("read_commands" in projections[1], "read_commands" in grant)
                    self.assertEqual(grant, before)

    def test_no_capability_default_bytes_and_visible_gap_handoff_are_preserved(self):
        for prompt in ("original", "original\n", ""):
            expected = prompt + ("" if prompt.endswith("\n") else "\n") + core.CAPABILITY_GAP_GUIDANCE
            with self.subTest(prompt=prompt):
                for mode in ("assistant_reply", "evidence_only"):
                    text = core._provider_prompt(prompt, {}, terminal_delivery=mode)
                    if mode == "assistant_reply":
                        self.assertEqual(text.encode("utf-8"), expected.encode("utf-8"))
                    else:
                        self.assertIn("publish the visible capability-gap handoff before terminal "
                                      "task_status question/done", text)
                    self.assertEqual(text.count(core.CAPABILITY_GAP_GUIDANCE), 1)
                    self.assertTrue(text.endswith(core.CAPABILITY_GAP_GUIDANCE))

    def test_parent_owned_checks_and_worker_verification_scope_across_prompt_routes(self):
        prompts = (
            "Worker assignment: source edits and fresh readbacks only. "
            "Parent explicitly owns all test execution, deployment and final acceptance.",
            "Worker assignment: source edits, required worker tests and fresh readbacks. "
            "Parent owns final acceptance only.",
        )
        for shape, grant in self._grants().items():
            before = copy.deepcopy(grant)
            for prompt in prompts:
                for mode in (None, "assistant_reply", "evidence_only"):
                    with self.subTest(shape=shape, prompt=prompt, mode=mode):
                        text = (core._provider_prompt(prompt, grant) if mode is None else
                                core._provider_prompt(prompt, grant, terminal_delivery=mode))
                        guidance = text[len(prompt):]
                        self.assertIn(
                            "path/operation/tool blocks unfinished worker-assigned work, preserve "
                            "the first actual blocker.", guidance)
                        self.assertIn(
                            "Explicitly parent-owned verification/deployment/acceptance are not "
                            "worker blockers and must not be claimed passed.", guidance)
                        self.assertIn(
                            "Missing verifier capability alone never transfers required worker "
                            "verification to the parent or excuses it.", guidance)
                        self.assertIn("Do not self-grant, switch denied paths or replay effects.",
                                      guidance)
                        self.assertEqual(core._EVIDENCE_ONLY_COMPLETION_GUIDANCE in guidance,
                                         mode == "evidence_only")
                        self.assertTrue(text.endswith(core.CAPABILITY_GAP_GUIDANCE))
                        self.assertEqual(grant, before)

    def test_completion_guidance_is_bounded_and_never_skips_required_work(self):
        guidance = core._EVIDENCE_ONLY_COMPLETION_GUIDANCE
        self.assertLessEqual(len(guidance.encode("utf-8")), 1100)
        for required in (
            "Complete worker work and required checks/readbacks before existing task_status done",
            "Explicitly parent-owned checks are not worker blockers; never claim them passed",
            "Parent acceptance remains required",
            "No prose-only recap",
            "When task instructions permit",
            "semantic decisions are settled",
            "all remaining exact artifacts/effects/checks/readbacks are known",
            "need no result-dependent interpretation",
            "planned apply_patch step 1, deterministic verification/readbacks step 2, "
            "task_status done step 3",
            "strictly later positive steps, same response",
            "Checks must pass before done executes, not before proposing",
            "Failure/timeout/uncertainty or result-dependent review needs a later model repair/review turn",
            "No done on failed/unknown effects, skipped required checks, or unfinished work",
            "Missing verifiers never transfer required worker verification to the parent or excuse it",
            "For real worker blockers",
            "publish the visible capability-gap handoff before terminal task_status question/done",
            "do not self-grant tools",
            "Scope/effect/receipt gates still apply",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guidance)


class DirectoryReadCapabilityProjectionTests(unittest.TestCase):
    def setUp(self):
        self.grant = {
            "authorization_semantic_sha256": "a" * 64,
            "source_read": False, "allowed_operations": ["command", "read"],
            "read_scopes": ["src/**", "tests/**"], "write_scopes": [],
            "declared_targets": [], "command_templates": [],
            "denied_operations": ["delete", "network"],
            "read_commands": {
                "roots": ["src", "tests"],
                "rg": {"path": "/opt/pinned tools/rg", "sha256": "b" * 64},
                "cat": {"path": "/bin/cat", "sha256": "c" * 64},
            },
        }

    def _projection(self, grant=None):
        text = core._provider_prompt("original", self.grant if grant is None else grant)
        parts = text.split("Verified execution capability (caller projection):\n", 1)
        return json.loads(parts[1].splitlines()[0]) if len(parts) == 2 else {}

    def _assert_omitted(self, grant):
        original = copy.deepcopy(grant)
        original.pop("read_commands", None)
        self.assertEqual(core._provider_prompt("original", grant),
                         core._provider_prompt("original", original))

    def test_directory_only_lists_with_pinned_argv_and_no_invented_file_read(self):
        for source_read in (False, None):
            with self.subTest(source_read=source_read):
                grant = copy.deepcopy(self.grant)
                if source_read is None:
                    del grant["source_read"]
                route = self._projection(grant)["read_commands"]
                policy = grant["read_commands"]
                self.assertEqual({key: route[key] for key in policy}, policy)
                self.assertEqual(route["listing_example"], {
                    "commands": [{"command_type": "bash", "step": 1,
                                  "command_line": shlex.join([
                                      policy["rg"]["path"], "--no-config", "--max-filesize=1M",
                                      "--files", "--", "src"])}],
                })
                self.assertEqual(route["cat_argv_prefix"], [policy["cat"]["path"], "--"])
                projection = self._projection(grant)
                for key in ("source_read", "source_read_example", "focused_verifier",
                            "known_source_sha256_by_path"):
                    self.assertNotIn(key, projection)
                self.assertEqual(set(route), {"roots", "rg", "cat", "listing_example",
                                             "cat_argv_prefix"})
                self.assertIn("directory scopes never qualify", core._provider_prompt("original", grant))

    def test_mixed_routes_preserve_exact_reads_verifiers_and_source_pins(self):
        self.grant.update(source_read=True, repo_root="/workspace",
                          read_scopes=["src/**", "src/known.py", "tests/**"],
                          verifier_commands=[{"argv": ["python", "tests/verify.py"],
                                              "pinned_files": [{"path": "/workspace/src/known.py",
                                                                "sha256": "d" * 64}]}])
        before = copy.deepcopy(self.grant)
        original = copy.deepcopy(self.grant)
        del original["read_commands"]
        projection = self._projection()
        self.assertIn("read_commands", projection)
        self.assertEqual(json.loads(projection["source_read_example"]["commands"][0]["command_line"]),
                         {"path": "src/known.py", "start_line": 1, "end_line": 80,
                          "expected_sha256": "d" * 64})
        del projection["read_commands"]
        self.assertEqual(projection, self._projection(original))
        self.assertEqual(self.grant, before)

    def _read_write_grant(self, with_verifier):
        grant = copy.deepcopy(self.grant)
        grant.update(source_read=True, allowed_operations=["command", "read", "modify"],
                     read_scopes=["src/**", "tests/pinned.py", "tests/**", "src/answer.py"],
                     write_scopes=["src/write_only.py", "src/answer.py"])
        if with_verifier:
            grant.update(repo_root="/workspace", verifier_commands=[{
                "argv": ["python", "tests/pinned.py"],
                "pinned_files": [{"path": "/workspace/tests/pinned.py", "sha256": "d" * 64}],
            }])
        return grant

    def _assert_workflow_guidance(self, grant, *, allowed, with_verifier):
        before = copy.deepcopy(grant)
        text = core._provider_prompt("original", grant)
        for marker, present in (
            ("Optional source_postimages:", allowed and with_verifier),
            ("Mechanical missing readbacks only;", allowed and with_verifier),
            ("Source-read-only final edits:", allowed and not with_verifier),
        ):
            self.assertEqual(text.count(marker), int(present), marker)
        self.assertEqual(grant, before)
        return text

    def test_mixed_read_write_scopes_restore_existing_guidance_and_preserve_projection(self):
        directory_guidance = (
            "\nread_commands: bash listing_example; shell-quote pinned cat_argv_prefix "
            "plus a discovered path. source_read requires source_read:true and an exact "
            "read_scopes path; directory scopes never qualify. No guessed paths, hidden "
            "files, symlinks, traversal, pipelines, substitution, --pre or --follow; "
            "runtime pins/limits apply. No new read/write authority.\n"
        )
        for with_verifier in (False, True):
            with self.subTest(with_verifier=with_verifier):
                grant = self._read_write_grant(with_verifier)
                exact = copy.deepcopy(grant)
                exact["read_scopes"] = ["tests/pinned.py", "src/answer.py"]
                del exact["read_commands"]
                text = self._assert_workflow_guidance(grant, allowed=True,
                                                      with_verifier=with_verifier)
                exact_text = self._assert_workflow_guidance(exact, allowed=True,
                                                            with_verifier=with_verifier)
                expected = self._projection(exact)
                expected["read_scopes"] = grant["read_scopes"]
                expected["read_commands"] = core._directory_read_presentation(grant)
                self.assertEqual(self._projection(grant), expected)
                self.assertEqual(expected["read_commands"]["roots"], ["src", "tests"])
                if with_verifier:
                    self.assertEqual(expected["known_source_sha256_by_path"],
                                     {"tests/pinned.py": "d" * 64})
                marker = "Verified execution capability (caller projection):\n"
                guidance = text.split(marker, 1)[1].split("\n", 1)[1]
                exact_guidance = exact_text.split(marker, 1)[1].split("\n", 1)[1]
                self.assertEqual(guidance.count(directory_guidance), 1)
                self.assertEqual(guidance.replace(directory_guidance, "", 1).encode("utf-8"),
                                 exact_guidance.encode("utf-8"))

    def test_exact_read_write_prompt_bytes_ignore_unpresented_directory_policies(self):
        for with_verifier in (False, True):
            grant = self._read_write_grant(with_verifier)
            grant["read_scopes"] = ["tests/pinned.py", "src/answer.py"]
            del grant["read_commands"]
            expected = self._assert_workflow_guidance(grant, allowed=True,
                                                     with_verifier=with_verifier).encode("utf-8")
            for policy in (None, {}, self.grant["read_commands"], {"roots": ["src"]}):
                with self.subTest(with_verifier=with_verifier, policy=policy):
                    grant["read_commands"] = copy.deepcopy(policy)
                    self.assertIsNone(core._directory_read_presentation(grant))
                    self.assertEqual(core._provider_prompt("original", grant).encode("utf-8"),
                                     expected)

    def test_mixed_scope_workflow_rejects_wildcards_not_matching_validated_roots(self):
        for with_verifier in (False, True):
            for scope in ("other/**", "src/*", "src/**/*.py", "src/nested/**", "src/**/",
                          "src/**/**", "src/**x", "src*", "**"):
                with self.subTest(with_verifier=with_verifier, scope=scope):
                    grant = self._read_write_grant(with_verifier)
                    grant["read_scopes"].append(scope)
                    self.assertIsNotNone(core._directory_read_presentation(grant))
                    self._assert_workflow_guidance(grant, allowed=False,
                                                   with_verifier=with_verifier)

    def test_mixed_scope_workflow_requires_well_formed_pinned_directory_routes(self):
        policy = self.grant["read_commands"]
        policies = [None, {}, {"roots": ["src"]}, dict(policy, extra=True)]
        policies += [dict(policy, roots=roots) for roots in
                     (None, "src", [], ["src", "src"], ["src/nested", "tests"])]
        for name in ("rg", "cat"):
            policies += [dict(policy, **{name: pin}) for pin in (
                None, {}, {"path": "/bin/" + name},
                {"path": "/bin/" + name, "sha256": "g" * 64},
                {"path": "/bin/" + name, "sha256": "A" * 64},
                {"path": name, "sha256": "b" * 64},
            )]
        for with_verifier in (False, True):
            for malformed in policies:
                with self.subTest(with_verifier=with_verifier, policy=malformed):
                    grant = self._read_write_grant(with_verifier)
                    grant["read_commands"] = copy.deepcopy(malformed)
                    self.assertIsNone(core._directory_read_presentation(grant))
                    self._assert_workflow_guidance(grant, allowed=False,
                                                   with_verifier=with_verifier)
            grant = self._read_write_grant(with_verifier)
            del grant["read_commands"]
            self._assert_workflow_guidance(grant, allowed=False, with_verifier=with_verifier)

    def test_mixed_scope_workflow_rejects_unsafe_roots_even_with_matching_directory_scopes(self):
        for with_verifier in (False, True):
            for root in ("", "./src", "/src", "src/..", "src//nested", "src/.hidden",
                         "src/*", "src/\nnext", "src/\ud800"):
                with self.subTest(with_verifier=with_verifier, root=root):
                    grant = self._read_write_grant(with_verifier)
                    grant["read_commands"]["roots"] = [root]
                    grant["read_scopes"] = [root + "/**", "tests/pinned.py", "src/answer.py"]
                    self.assertIsNone(core._directory_read_presentation(grant))
                    self._assert_workflow_guidance(grant, allowed=False,
                                                   with_verifier=with_verifier)

    def test_mixed_scope_workflow_rejects_unsafe_scopes_despite_exact_overlap(self):
        for with_verifier in (False, True):
            for field in ("read_scopes", "write_scopes"):
                for path in (None, [], {}, 7, "", "/src/other.py", "../src/other.py",
                             "./src/other.py", "src/../other.py", "src//other.py",
                             r"src\other.py", "src/other\n.py", "src/\ud800.py", "src/[a].py"):
                    with self.subTest(with_verifier=with_verifier, field=field, path=path):
                        grant = self._read_write_grant(with_verifier)
                        grant[field].append(path)
                        self._assert_workflow_guidance(grant, allowed=False,
                                                       with_verifier=with_verifier)

    def test_mixed_scope_workflow_requires_undenied_command_read_modify_operations(self):
        changes = [
            {"source_read": False}, {"source_read": 1},
            {"allowed_operations": ["command", "read"]},
            {"allowed_operations": ["command", "modify"]},
            {"allowed_operations": ["read", "modify"]},
            {"allowed_operations": ["command", "read", "modify", None]},
            {"denied_operations": ["command"]}, {"denied_operations": ["read"]},
            {"denied_operations": ["modify"]}, {"denied_operations": None},
            {"denied_operations": "modify"}, {"denied_operations": [None]},
        ]
        for with_verifier in (False, True):
            for change in changes:
                with self.subTest(with_verifier=with_verifier, change=change):
                    grant = self._read_write_grant(with_verifier)
                    grant.update(change)
                    self._assert_workflow_guidance(grant, allowed=False,
                                                   with_verifier=with_verifier)

    def test_mixed_scope_workflow_requires_exact_write_scopes_and_exact_read_write_overlap(self):
        changes = [
            {"write_scopes": []}, {"write_scopes": ["src/**"]},
            {"write_scopes": ["src/answer.py", "src/**"]},
            {"write_scopes": ["src/*.py"]}, {"write_scopes": ["src/new.py"]},
            {"read_scopes": ["src/**", "tests/**"]},
            {"read_scopes": ["src/**", "tests/**", "tests/pinned.py"]},
            {"read_scopes": ["src/**", "tests/**", "src/other.py"]},
        ]
        for with_verifier in (False, True):
            for change in changes:
                with self.subTest(with_verifier=with_verifier, change=change):
                    grant = self._read_write_grant(with_verifier)
                    grant.update(change)
                    self.assertIsNotNone(core._directory_read_presentation(grant))
                    self._assert_workflow_guidance(grant, allowed=False,
                                                   with_verifier=with_verifier)

    def test_recursive_scopes_do_not_become_exact_source_read_examples(self):
        self.grant["source_read"] = True
        projection = self._projection()
        self.assertIn("read_commands", projection)
        self.assertNotIn("source_read_example", projection)
        self.assertEqual(projection["read_scopes"], ["src/**", "tests/**"])
        self.assertIn("source_read requires source_read:true and an exact read_scopes path",
                      core._provider_prompt("original", self.grant))

    def test_ordinary_exact_file_prompt_is_unchanged_without_directory_policy(self):
        grant = copy.deepcopy(self.grant)
        grant.update(source_read=True, read_scopes=["src/answer.py"])
        del grant["read_commands"]
        with patch.object(core, "_directory_read_presentation", return_value=None):
            original = core._provider_prompt("original", grant)
        self.assertEqual(len(original.splitlines()), 6)
        self.assertEqual(core._provider_prompt("original", grant), original)
        for policy in (None, {}, self.grant["read_commands"]):
            grant["read_commands"] = policy
            self.assertEqual(core._provider_prompt("original", grant), original)
        grant["source_read"] = False
        self.assertEqual(core._provider_prompt("original", grant), core._capability_gap_prompt("original"))

    def test_malformed_policy_shape_is_omitted_not_partially_presented(self):
        for policy in (None, False, 7, [], {}, {"roots": ["src"]},
                       dict(self.grant["read_commands"], extra=True)):
            with self.subTest(policy=policy):
                grant = copy.deepcopy(self.grant)
                grant["read_commands"] = policy
                self._assert_omitted(grant)
        for roots in (None, "src", (), [], [None], [7], ["src", "src"]):
            with self.subTest(roots=roots):
                grant = copy.deepcopy(self.grant)
                grant["read_commands"]["roots"] = roots
                self._assert_omitted(grant)

    def test_routes_require_well_formed_undenied_read_command_and_matching_scopes(self):
        updates = [
            {"allowed_operations": value} for value in
            (None, [], ["read"], ["command"], ["read", "command", None])
        ] + [
            {"denied_operations": value} for value in
            (None, "read", ["read"], ["command"], [None])
        ] + [
            {"read_scopes": value} for value in
            (None, "src/**", [], ["src", "tests/**"], ["src/**"],
             ["src*", "tests/**"], ["src/**", "tests/**", None])
        ]
        for update in updates:
            with self.subTest(update=update):
                grant = copy.deepcopy(self.grant)
                grant.update(update)
                self._assert_omitted(grant)
        self.grant["read_commands"]["roots"] = ["src/nested"]
        self._assert_omitted(self.grant)  # An ancestor scope is not a matching policy root.

    def test_unsafe_or_noncanonical_roots_are_omitted_even_with_matching_scopes(self):
        roots = ["", " ", "-", ".", "..", "/src", "./src", "src/..", "src//nested", "src/",
                 ".hidden", "src/.hidden", "src/.git", "src/*", "src/?", "src/[a]", "src/{a}",
                 "~/src", r"src\nested", "src/\nnext", "src/\x00", "src/\x7f", "src/\ud800",
                 "a" * 4097, "é" * 2049]
        for root in roots:
            with self.subTest(root=root):
                grant = copy.deepcopy(self.grant)
                grant["read_commands"]["roots"] = ["src", root]
                grant["read_scopes"] = ["src/**", root + "/**"]
                self._assert_omitted(grant)

    def test_unpinned_or_noncanonical_executables_are_omitted(self):
        bad_paths = [None, "", "rg", "/", "/opt//rg", "/opt/./rg", "/opt/../rg", "/opt/rg/",
                     r"/opt\rg", "/opt/*", "/opt/rg\n", "/opt/\ud800", "/" + "a" * 4096]
        bad_hashes = [None, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + "\n"]
        for name in ("rg", "cat"):
            pins = [None, [], {}, {"path": "/bin/" + name},
                    dict(self.grant["read_commands"][name], extra=True)]
            pins += [dict(self.grant["read_commands"][name], path=path) for path in bad_paths]
            pins += [dict(self.grant["read_commands"][name], sha256=digest) for digest in bad_hashes]
            for pin in pins:
                with self.subTest(name=name, pin=pin):
                    grant = copy.deepcopy(self.grant)
                    grant["read_commands"][name] = pin
                    self._assert_omitted(grant)

    def test_shell_metacharacters_remain_literal_quoted_argv(self):
        root = "src/it's ; $(echo nope) & café"
        self.grant["read_commands"]["roots"] = [root]
        self.grant["read_scopes"] = [root + "/**"]
        self.grant["read_commands"]["rg"]["path"] = "/opt/it's ; $(echo nope)/rg"
        self.grant["read_commands"]["cat"]["path"] = "/opt/it's tools/cat"
        before = copy.deepcopy(self.grant)
        route = self._projection()["read_commands"]
        command = route["listing_example"]["commands"][0]
        self.assertEqual(shlex.split(command["command_line"]),
                         [route["rg"]["path"], "--no-config", "--max-filesize=1M", "--files", "--", root])
        self.assertEqual(route["cat_argv_prefix"], [route["cat"]["path"], "--"])
        self.assertEqual(self.grant, before)

    def test_input_and_ascii_json_output_are_bounded(self):
        self.grant["read_commands"]["roots"] = [f"src/root{i}" for i in range(8)]
        self.grant["read_scopes"] = [root + "/**" for root in self.grant["read_commands"]["roots"]]
        route = self._projection()["read_commands"]
        self.assertLessEqual(len(json.dumps(route, sort_keys=True, separators=(",", ":"), ensure_ascii=True)),
                             core.MAX_DIRECTORY_READ_PRESENTATION_BYTES)
        for field, values in (("allowed_operations", ["read", "command"] + ["read"] * 63),
                              ("denied_operations", ["delete"] * 65),
                              ("read_scopes", self.grant["read_scopes"] + ["other/**"] * 57)):
            grant = copy.deepcopy(self.grant)
            grant[field] = values
            self._assert_omitted(grant)
        grant = copy.deepcopy(self.grant)
        grant["read_commands"]["roots"].append("src/ninth")
        grant["read_scopes"].append("src/ninth/**")
        self._assert_omitted(grant)
        for suffix, allowed in (("a" * 1000, True), ("é" * 500, False)):
            grant = copy.deepcopy(self.grant)
            root = "src/" + suffix
            grant["read_commands"]["roots"] = [root]
            grant["read_scopes"] = [root + "/**"]
            if allowed:
                self.assertIn("read_commands", self._projection(grant))
                text = core._provider_prompt("original", grant)
                self.assertLessEqual(len(text.encode("utf-8")) - len(core._capability_gap_prompt("original").encode()),
                                     core.MAX_DIRECTORY_READ_PRESENTATION_BYTES + 1024)
                self.assertEqual(core._provider_prompt("original", grant), text)
            else:
                self._assert_omitted(grant)

    def test_projection_is_immutable_and_performs_no_os_or_hash_operations(self):
        before = copy.deepcopy(self.grant)
        with patch("builtins.open") as opened, \
                patch.object(core.os, "open") as os_open, \
                patch.object(core.os, "stat") as stat, \
                patch.object(core.os, "lstat") as lstat, \
                patch.object(core.os, "readlink") as readlink, \
                patch.object(core.os, "getcwd") as getcwd, \
                patch.object(core.os, "scandir") as scandir, \
                patch.object(core.Path, "resolve") as resolve, \
                patch.object(core.Path, "open") as path_open, \
                patch.object(core.Path, "read_bytes") as read_bytes, \
                patch.object(core.Path, "read_text") as read_text, \
                patch.object(core.hashlib, "sha256") as sha256, \
                patch.object(core.caller, "_file_sha256") as file_sha256, \
                patch.object(core.subprocess, "run") as run, \
                patch.object(core.subprocess, "Popen") as popen, \
                patch.object(core, "supervise") as supervise:
            self.assertIn("read_commands", self._projection())
            malformed = copy.deepcopy(self.grant)
            del malformed["read_commands"]["cat"]["sha256"]
            self._assert_omitted(malformed)
        for effect in (opened, os_open, stat, lstat, readlink, getcwd, scandir, resolve, path_open,
                       read_bytes, read_text, sha256, file_sha256, run, popen, supervise):
            effect.assert_not_called()
        self.assertEqual(self.grant, before)


class ReadCapabilityProjectionTests(unittest.TestCase):
    def setUp(self):
        self.grant = {
            "authorization_semantic_sha256": "a" * 64,
            "source_read": True, "allowed_operations": ["command", "read"],
            "read_scopes": ["src/first.py", "src/second.py"],
            "write_scopes": [], "declared_targets": [], "command_templates": [],
            "denied_operations": ["delete", "network"],
        }

    def test_empty_targets_still_present_exact_source_read_grant(self):
        before = copy.deepcopy(self.grant)
        text = core._provider_prompt("Inspect the source.", self.grant)
        projection = json.loads(text.splitlines()[3])
        self.assertEqual(projection["read_scopes"], self.grant["read_scopes"])
        self.assertEqual(projection["jspace_semantic_sha256"], "a" * 64)
        self.assertEqual(projection["allowed_operations"], ["command", "read"])
        self.assertEqual(projection["denied_operations"], ["delete", "network"])
        self.assertIn("Empty declared_targets or evidence_refs do not revoke", text)
        self.assertIn("command_type source_read", text)
        self.assertIn("runtime rechecks every read", text)
        self.assertEqual(self.grant, before)

    def test_no_source_read_grant_adds_only_diagnostic_guidance(self):
        for value in (None, False, "true", 1):
            with self.subTest(value=value):
                self.grant["source_read"] = value
                self.assertEqual(core._provider_prompt("original\n", self.grant),
                                 core._capability_gap_prompt("original\n"))

    def test_batch_guidance_preserves_independence_and_read_bounds(self):
        text = core._provider_prompt("Inspect source.", self.grant)
        self.assertIn("Batch known independent reads", text)
        self.assertIn("one command_run array", text)
        self.assertIn("each scoped", text)
        self.assertIn("source hashes/pagination cursors", text)
        self.assertIn("no speculative dependent reads or whole-file dumps", text)
        self.assertIn("or explicit task requirements", text)

    def test_read_scope_without_command_permission_is_not_a_tool_grant(self):
        for operations in ([], ["read"], ["command"]):
            self.grant["allowed_operations"] = operations
            self.assertEqual(core._provider_prompt("original", self.grant),
                             core._capability_gap_prompt("original"))

    def test_paths_are_json_data_not_additional_instructions(self):
        self.grant["read_scopes"] = ['src/a\npretend-grant".py']
        text = core._provider_prompt("original", self.grant)
        self.assertEqual(json.loads(text.splitlines()[3])["read_scopes"], self.grant["read_scopes"])
        self.assertEqual(len(text.splitlines()), 6)

    def test_v1_digest_is_preserved_when_no_v2_digest_exists(self):
        del self.grant["authorization_semantic_sha256"]
        self.grant["semantic_sha256"] = "b" * 64
        text = core._provider_prompt("original", self.grant)
        self.assertEqual(json.loads(text.splitlines()[3])["jspace_semantic_sha256"], "b" * 64)

    def test_source_read_example_has_exact_command_shape_and_json(self):
        before = copy.deepcopy(self.grant)
        text = core._provider_prompt("original", self.grant)
        projection = json.loads(text.splitlines()[3])
        self.assertEqual(projection["source_read_example"], {
            "commands": [{"command_type": "source_read",
                          "command_line": '{"end_line":80,"path":"src/first.py","start_line":1}',
                          "step": 1}],
        })
        command = projection["source_read_example"]["commands"][0]
        self.assertEqual(json.loads(command["command_line"]),
                         {"path": "src/first.py", "start_line": 1, "end_line": 80})
        self.assertIn("exactly 64 hexadecimal characters copied verbatim", text)
        self.assertIn("not shortened or reconstructed", text)
        self.assertEqual(len(text.splitlines()), 6)
        self.assertEqual(self.grant, before)

    def test_source_read_example_is_omitted_without_undenied_capabilities(self):
        for updates in ({"source_read": False}, {"allowed_operations": []},
                        {"allowed_operations": ["command"]},
                        {"allowed_operations": ["read"]},
                        {"denied_operations": ["command"]},
                        {"denied_operations": ["read"]}):
            with self.subTest(updates=updates):
                grant = copy.deepcopy(self.grant)
                grant.update(updates)
                before = copy.deepcopy(grant)
                text = core._provider_prompt("original", grant)
                parts = text.split("Verified execution capability (caller projection):\n", 1)
                projection = json.loads(parts[1].splitlines()[0]) if len(parts) == 2 else {}
                self.assertNotIn("source_read_example", projection)
                self.assertEqual(grant, before)

    def test_source_read_example_selects_first_safe_utf8_bounded_exact_scope(self):
        unsafe = [None, 7, "", "src/*.py", "/src/first.py", "../src/first.py",
                  "src/../first.py", "./src/first.py", "src//first.py", r"src\first.py",
                  'src/a\npretend-grant".py', "a" * 257, "é" * 129, "src/\ud800.py"]
        for scopes, path in (([], None), (unsafe, None), ("src/first.py", None), (None, None),
                             (unsafe + ["src/second.py", "src/first.py"], "src/second.py"),
                             (["a" * 256], "a" * 256), (["é" * 128], "é" * 128),
                             (['src/a"b.py'], 'src/a"b.py')):
            with self.subTest(scopes=scopes):
                grant = copy.deepcopy(self.grant)
                grant["read_scopes"] = scopes
                before = copy.deepcopy(grant)
                text = core._provider_prompt("original", grant)
                projection = json.loads(text.splitlines()[3])
                self.assertEqual(projection["read_scopes"], scopes)
                if path is None:
                    self.assertNotIn("source_read_example", projection)
                else:
                    command = projection["source_read_example"]["commands"][0]
                    self.assertEqual(json.loads(command["command_line"]),
                                     {"path": path, "start_line": 1, "end_line": 80})
                self.assertEqual(len(text.splitlines()), 6)
                self.assertEqual(text.splitlines()[-1], core.CAPABILITY_GAP_GUIDANCE)
                self.assertEqual(grant, before)

    def test_source_read_example_uses_only_the_verified_matching_hash(self):
        root = "/workspace/harness"
        hashes = {"src/first.py": "a0" * 32, "src/second.py": "b1" * 32}
        for paths in (tuple(hashes), ("src/second.py",)):
            with self.subTest(paths=paths):
                grant = copy.deepcopy(self.grant)
                grant["repo_root"] = root
                grant["verifier_commands"] = [{
                    "argv": ["python", root + "/src/second.py"],
                    "scratch_root": "/scratch/first", "timeout_seconds": 7,
                    "pinned_files": [{"path": root + "/" + path, "sha256": hashes[path]}
                                     for path in paths],
                }]
                before = copy.deepcopy(grant)
                text = core._provider_prompt("original", grant)
                projection = json.loads(text.splitlines()[3])
                self.assertEqual(projection["known_source_sha256_by_path"],
                                 {path: hashes[path] for path in paths})
                command = projection["source_read_example"]["commands"][0]
                expected = {"path": "src/first.py", "start_line": 1, "end_line": 80}
                if "src/first.py" in paths:
                    expected["expected_sha256"] = hashes["src/first.py"]
                self.assertEqual(json.loads(command["command_line"]), expected)
                self.assertEqual(grant, before)


class KnownSourceHashProjectionTests(unittest.TestCase):
    def setUp(self):
        self.root = "/workspace/harness"
        self.script = "public_verifier.py"
        self.test = "tests/test_harness.py"
        self.hashes = {
            self.script: "a0b9f51b0dc7289f118b55bf36d4a2d2138593602a8a2d1a3422d284741120be",
            self.test: "609eb6bd3979b4241ed3e8808e9f5aa9f11912e8d191e7137defc0711277649b",
        }
        self.grant = {
            "authorization_semantic_sha256": "d" * 64,
            "repo_root": self.root, "source_read": True,
            "allowed_operations": ["command", "read", "modify"],
            "denied_operations": ["create", "delete"],
            "read_scopes": [self.script, self.test],
            "write_scopes": ["src/codex_collaboration_harness/core.py"],
            "verifier_commands": [{
                "argv": ["python", self.root + "/" + self.script],
                "scratch_root": "/scratch/first", "timeout_seconds": 7,
                "pinned_files": [{"path": self.root + "/" + path, "sha256": digest}
                                 for path, digest in self.hashes.items()],
            }],
        }

    def _projection(self, grant=None):
        text = core._provider_prompt("Inspect source.", self.grant if grant is None else grant)
        parts = text.split("Verified execution capability (caller projection):\n", 1)
        return json.loads(parts[1].splitlines()[0]) if len(parts) == 2 else {}

    def test_distinct_script_and_test_hashes_are_bound_to_relative_paths(self):
        projection = self._projection()
        self.assertEqual(projection["known_source_sha256_by_path"], self.hashes)
        self.assertEqual(set(projection["known_source_sha256_by_path"]),
                         {"public_verifier.py", "tests/test_harness.py"})
        self.assertNotEqual(projection["known_source_sha256_by_path"][self.script],
                            projection["known_source_sha256_by_path"][self.test])
        self.assertEqual(list(projection["known_source_sha256_by_path"]), sorted(self.hashes))
        self.assertEqual(projection["read_scopes"], self.grant["read_scopes"])
        self.assertEqual(projection["allowed_operations"], self.grant["allowed_operations"])
        self.assertEqual(projection["denied_operations"], self.grant["denied_operations"])
        self.assertEqual(projection["focused_verifier"], {
            "verifier_indices": [0],
            "example": {"commands": [{"command_type": "focused_verifier",
                                      "command_line": '{"verifier_index":0}', "step": 1}]},
        })
        prompt = "Read public_verifier.py with expected_sha256=" + self.hashes[self.test]
        text = core._provider_prompt(prompt, self.grant)
        self.assertEqual(text.split("\n\nVerified execution capability (caller projection):\n", 1)[0],
                         prompt)
        self.assertIn("Never borrow another file's SHA", text)
        self.assertIn("After mutation: fresh scoped readback, not preimage SHA", text)
        self.assertIn("Use known_source_sha256_by_path only for the matching path's", text)
        self.assertNotIn(self.root, text)

    def test_identical_pairs_and_hex_case_are_deduplicated_without_mutation(self):
        duplicate = copy.deepcopy(self.grant["verifier_commands"][0])
        duplicate["pinned_files"] = [{"path": pin["path"], "sha256": pin["sha256"].upper()}
                                     for pin in duplicate["pinned_files"]]
        self.grant["verifier_commands"].append(duplicate)
        self.grant["read_scopes"] *= 2
        before = copy.deepcopy(self.grant)
        self.assertEqual(self._projection()["known_source_sha256_by_path"], self.hashes)
        self.assertEqual(self.grant, before)

    def test_conflicts_including_malformed_hashes_omit_the_path_in_every_order(self):
        for other_hash in ("c" * 64, None, "invalid"):
            for reverse in (False, True):
                with self.subTest(other_hash=other_hash, reverse=reverse):
                    grant = copy.deepcopy(self.grant)
                    duplicate = copy.deepcopy(grant["verifier_commands"][0])
                    duplicate["pinned_files"] = [{"path": self.root + "/" + self.script,
                                                  "sha256": other_hash}]
                    grant["verifier_commands"].append(duplicate)
                    if reverse:
                        grant["verifier_commands"].reverse()
                    before = copy.deepcopy(grant)
                    self.assertEqual(self._projection(grant)["known_source_sha256_by_path"],
                                     {self.test: self.hashes[self.test]})
                    self.assertEqual(grant, before)

    def test_conflicting_or_missing_hash_entries_in_one_list_suppress_only_that_path(self):
        path = self.root + "/" + self.script
        for entry in ({"path": path, "sha256": "c" * 64},
                      {"path": path, "sha256": None},
                      {"path": path, "sha256": "invalid"}, {"path": path}):
            for reverse in (False, True):
                with self.subTest(entry=entry, reverse=reverse):
                    grant = copy.deepcopy(self.grant)
                    pins = grant["verifier_commands"][0]["pinned_files"]
                    pins.append(entry)
                    if reverse:
                        pins.reverse()
                    before = copy.deepcopy(grant)
                    self.assertEqual(self._projection(grant)["known_source_sha256_by_path"],
                                     {self.test: self.hashes[self.test]})
                    self.assertEqual(grant, before)

    def test_malformed_list_entries_and_missing_fields_do_not_fabricate_evidence(self):
        path = self.root + "/" + self.script
        digest = self.hashes[self.script]
        for entry in (None, True, 1, "pins", [], {}, {"path": path},
                      {"sha256": digest}, {"path": path, "digest": digest},
                      {"file": path, "sha256": digest},
                      {"path": [path], "sha256": digest}):
            with self.subTest(entry=entry):
                grant = copy.deepcopy(self.grant)
                pins = [entry]
                grant["verifier_commands"][0]["pinned_files"] = pins
                before = copy.deepcopy(grant)
                self.assertNotIn("known_source_sha256_by_path", self._projection(grant))
                self.assertIn("focused_verifier", self._projection(grant))
                self.assertEqual(grant, before)
                pins.append({"path": self.root + "/" + self.test,
                             "sha256": self.hashes[self.test]})
                before = copy.deepcopy(grant)
                self.assertEqual(self._projection(grant)["known_source_sha256_by_path"],
                                 {self.test: self.hashes[self.test]})
                self.assertEqual(grant, before)

    def test_malformed_hashes_are_not_presented(self):
        for digest in (None, 1, True, [], {}, b"a" * 64, "", "a" * 63, "a" * 65,
                       "g" * 64, "a" * 63 + "\n", " " + "a" * 63):
            with self.subTest(digest=digest):
                grant = copy.deepcopy(self.grant)
                grant["verifier_commands"][0]["pinned_files"][0]["sha256"] = digest
                self.assertEqual(self._projection(grant)["known_source_sha256_by_path"],
                                 {self.test: self.hashes[self.test]})

    def test_missing_or_malformed_root_and_pins_preserve_existing_prompt(self):
        prompt = "original\n\t\u00e9\x00"
        baseline = copy.deepcopy(self.grant)
        del baseline["repo_root"]
        expected = core._provider_prompt(prompt, baseline)
        cases = [baseline]
        for root in (None, False, 1, [], {}, "", "workspace/harness", "~/harness",
                     "/workspace/../harness", "/workspace/./harness", "/workspace//harness",
                     "/workspace/harness/", "/workspace/*", "/workspace/harness\x00",
                     "/workspace/link/../harness"):
            grant = copy.deepcopy(self.grant)
            grant["repo_root"] = root
            cases.append(grant)
        for pins in (None, [], (), "pins", 1, {},
                     tuple(self.grant["verifier_commands"][0]["pinned_files"]),
                     {self.root + "/" + path: digest for path, digest in self.hashes.items()},
                     {"path": self.root + "/" + self.script, "sha256": self.hashes[self.script]}):
            grant = copy.deepcopy(self.grant)
            grant["verifier_commands"][0]["pinned_files"] = pins
            cases.append(grant)
        no_pins = copy.deepcopy(self.grant)
        del no_pins["verifier_commands"][0]["pinned_files"]
        no_pins["known_source_sha256_by_path"] = self.hashes
        no_pins["evidence_refs"] = [{"id": self.script, "sha256": self.hashes[self.script]}]
        cases.append(no_pins)
        for grant in cases:
            with self.subTest(grant=grant):
                before = copy.deepcopy(grant)
                self.assertNotIn("known_source_sha256_by_path", self._projection(grant))
                self.assertEqual(core._provider_prompt(prompt, grant), expected)
                self.assertEqual(grant, before)

    def test_nonliteral_out_of_root_and_unverified_alias_pins_are_omitted(self):
        cases = [
            (None, "src/file.py"), (1, "src/file.py"),
            ("src/file.py", "src/file.py"),
            ("/outside/src/file.py", "src/file.py"),
            (self.root + "-other/src/file.py", "src/file.py"),
            (self.root + "/../outside.py", "../outside.py"),
            (self.root + "/src/../file.py", "src/../file.py"),
            (self.root + "/src/./file.py", "src/./file.py"),
            (self.root + "/link/../src/file.py", "link/../src/file.py"),
            (self.root + "//src/file.py", "/src/file.py"),
            (self.root + "/src//file.py", "src//file.py"),
            (self.root + "/src/file.py/", "src/file.py/"),
            (self.root + "/src\\file.py", "src\\file.py"),
            (self.root + "/src/*.py", "src/*.py"),
            (self.root + "/src/**/file.py", "src/**/file.py"),
            (self.root + "/src/file?.py", "src/file?.py"),
            (self.root + "/src/[ab].py", "src/[ab].py"),
            (self.root + "/src/{a,b}.py", "src/{a,b}.py"),
            (self.root + "/~/file.py", "~/file.py"),
            (self.root + "/src/file\n.py", "src/file\n.py"),
            (self.root + "/src/file\x00.py", "src/file\x00.py"),
            ("file://" + self.root + "/src/file.py", "src/file.py"),
        ]
        for path, scope in cases:
            with self.subTest(path=path):
                grant = copy.deepcopy(self.grant)
                grant["read_scopes"].append(scope)
                grant["verifier_commands"][0]["pinned_files"].append({"path": path, "sha256": "c" * 64})
                self.assertEqual(self._projection(grant)["known_source_sha256_by_path"], self.hashes)

    def test_read_scope_membership_is_exact_and_does_not_expand_scopes(self):
        cases = [None, "src", {}, (), [], ["src", "tests"], ["src/**", "tests/*"],
                 ["./" + self.script, "./" + self.test],
                 [self.root + "/" + self.script, self.root + "/" + self.test],
                 ["subdir/../" + self.script], [None, 1]]
        for scopes in cases:
            with self.subTest(scopes=scopes):
                grant = copy.deepcopy(self.grant)
                grant["read_scopes"] = scopes
                self.assertNotIn("known_source_sha256_by_path", self._projection(grant))
        grant = copy.deepcopy(self.grant)
        del grant["read_scopes"]
        self.assertNotIn("known_source_sha256_by_path", self._projection(grant))
        grant = copy.deepcopy(self.grant)
        grant["verifier_commands"][0]["pinned_files"].append({
            "path": self.root + "/src/ungranted.py", "sha256": "c" * 64})
        self.assertEqual(self._projection(grant)["known_source_sha256_by_path"], self.hashes)
        self.assertEqual(self._projection(grant)["read_scopes"], self.grant["read_scopes"])

    def test_writable_exact_ancestor_and_descendant_scopes_are_excluded(self):
        cases = [
            ([self.script], {self.test: self.hashes[self.test]}),
            ([self.root + "/" + self.script], {self.test: self.hashes[self.test]}),
            (["src"], self.hashes),
            ([self.root + "/src"], self.hashes),
            ([self.script + "/child"], {self.test: self.hashes[self.test]}),
            (["tests"], {self.script: self.hashes[self.script]}),
            ([self.root + "/tests"], {self.script: self.hashes[self.script]}),
            ([self.script, self.test], {}),
            ([self.script + "-other", "test"], self.hashes),
        ]
        for scopes, expected in cases:
            for operations in (["command", "read"], ["command", "read", "modify"]):
                with self.subTest(scopes=scopes, operations=operations):
                    grant = copy.deepcopy(self.grant)
                    grant.update(write_scopes=scopes, allowed_operations=operations)
                    projection = self._projection(grant)
                    self.assertEqual(projection.get("known_source_sha256_by_path", {}), expected)
                    if not expected:
                        self.assertNotIn("known_source_sha256_by_path", projection)
        for scopes in (None, "src", {}, (), [None], [1], [""], ["."], [".."],
                       ["src/../other"], ["src/**"], [self.root], ["/outside"]):
            with self.subTest(scopes=scopes):
                grant = copy.deepcopy(self.grant)
                grant["write_scopes"] = scopes
                self.assertNotIn("known_source_sha256_by_path", self._projection(grant))
        grant = copy.deepcopy(self.grant)
        del grant["write_scopes"]
        self.assertNotIn("known_source_sha256_by_path", self._projection(grant))

    def test_absent_denied_or_malformed_read_and_command_grants_add_no_mapping(self):
        cases = [
            {"allowed_operations": []}, {"allowed_operations": ["read"]},
            {"allowed_operations": ["command"]}, {"allowed_operations": None},
            {"allowed_operations": "command"}, {"allowed_operations": ("command", "read")},
            {"allowed_operations": ["command", "read", 1]},
            {"source_read": False}, {"source_read": None},
            {"source_read": 1}, {"source_read": "true"},
            {"denied_operations": ["read"]}, {"denied_operations": ["command"]},
            {"denied_operations": ["command", "read"]},
            {"denied_operations": None}, {"denied_operations": "read"},
            {"denied_operations": {}}, {"denied_operations": [1]},
        ]
        grants = []
        for changes in cases:
            grant = copy.deepcopy(self.grant)
            grant.update(changes)
            grants.append(grant)
        for key in ("allowed_operations", "source_read"):
            grant = copy.deepcopy(self.grant)
            del grant[key]
            grants.append(grant)
        for grant in grants:
            with self.subTest(grant=grant):
                before = copy.deepcopy(grant)
                baseline = copy.deepcopy(grant)
                del baseline["repo_root"]
                self.assertNotIn("known_source_sha256_by_path", self._projection(grant))
                self.assertEqual(core._provider_prompt("original", grant),
                                 core._provider_prompt("original", baseline))
                self.assertEqual(grant, before)

    def test_absent_or_malformed_verifiers_do_not_supply_pins(self):
        for verifiers in (None, [], {}, "verify", (), [None], [{}],
                          [{"argv": []}], [{"argv": "python"}],
                          [{"argv": ["python", 1], "pinned_files":
                            self.grant["verifier_commands"][0]["pinned_files"]}]):
            with self.subTest(verifiers=verifiers):
                grant = copy.deepcopy(self.grant)
                grant["verifier_commands"] = verifiers
                self.assertNotIn("known_source_sha256_by_path", self._projection(grant))
                self.assertNotIn("focused_verifier", self._projection(grant))
        grant = copy.deepcopy(self.grant)
        del grant["verifier_commands"]
        self.assertNotIn("known_source_sha256_by_path", self._projection(grant))

    def test_entry_cap_is_deterministic_and_conflicts_are_checked_before_capping(self):
        paths = [f"files/{index:03d}.py" for index in range(core.MAX_KNOWN_SOURCE_SHA256_ENTRIES + 8)]
        grant = copy.deepcopy(self.grant)
        grant["read_scopes"] = paths
        grant["verifier_commands"][0]["pinned_files"] = [
            {"path": self.root + "/" + path, "sha256": "a" * 64} for path in reversed(paths)]
        duplicate = copy.deepcopy(grant["verifier_commands"][0])
        duplicate["pinned_files"] = [{"path": self.root + "/" + paths[0], "sha256": "b" * 64}]
        grant["verifier_commands"].append(duplicate)
        before = copy.deepcopy(grant)
        projection = self._projection(grant)
        known = projection["known_source_sha256_by_path"]
        self.assertEqual(known, {path: "a" * 64
                                for path in paths[1:core.MAX_KNOWN_SOURCE_SHA256_ENTRIES + 1]})
        self.assertLessEqual(len(json.dumps(known, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=True)), core.MAX_KNOWN_SOURCE_SHA256_BYTES)
        self.assertEqual(projection["read_scopes"], paths)
        self.assertEqual(grant, before)
        grant["verifier_commands"].reverse()
        grant["read_scopes"].reverse()
        self.assertEqual(self._projection(grant)["known_source_sha256_by_path"], known)

    def test_ascii_json_byte_cap_and_oversize_omission_are_deterministic(self):
        oversize = "000/" + "\u00e9" * 700 + ".py"
        unicode_part = "\u00e9" * 96
        paths = [f"files/{unicode_part}/{index:03d}.py"
                 for index in range(core.MAX_KNOWN_SOURCE_SHA256_ENTRIES)]
        grant = copy.deepcopy(self.grant)
        grant["read_scopes"] = [oversize] + paths
        pins = [{"path": self.root + "/" + path, "sha256": "a" * 64}
                for path in grant["read_scopes"]]
        grant["verifier_commands"][0]["pinned_files"] = pins
        projection = self._projection(grant)
        known = projection["known_source_sha256_by_path"]
        encoded = json.dumps(known, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.assertTrue(known)
        self.assertNotIn(oversize, known)
        self.assertLess(len(known), core.MAX_KNOWN_SOURCE_SHA256_ENTRIES)
        self.assertLessEqual(len(encoded.encode("ascii")), core.MAX_KNOWN_SOURCE_SHA256_BYTES)
        self.assertEqual(projection["read_scopes"], [oversize] + paths)
        grant["verifier_commands"][0]["pinned_files"] = list(reversed(pins))
        grant["read_scopes"].reverse()
        self.assertEqual(self._projection(grant)["known_source_sha256_by_path"], known)

    def test_mapping_paths_remain_escaped_json_data(self):
        path = 'tests/caf\u00e9".py'
        self.grant["read_scopes"].append(path)
        self.grant["verifier_commands"][0]["pinned_files"].append({
            "path": self.root + "/" + path, "sha256": "c" * 64})
        text = core._provider_prompt("original", self.grant)
        projection = json.loads(text.splitlines()[3])
        self.assertEqual(projection["known_source_sha256_by_path"], {**self.hashes, path: "c" * 64})
        self.assertIn("\\u00e9", text.splitlines()[3])
        self.assertNotIn("\u00e9", text.splitlines()[3])

    def test_projection_does_not_mutate_hash_read_resolve_or_execute(self):
        before = copy.deepcopy(self.grant)
        with patch("builtins.open") as opened, \
                patch.object(core.os, "open") as os_open, \
                patch.object(core.os, "stat") as stat, \
                patch.object(core.os, "lstat") as lstat, \
                patch.object(core.os, "readlink") as readlink, \
                patch.object(core.os, "getcwd") as getcwd, \
                patch.object(core.Path, "resolve") as resolve, \
                patch.object(core.Path, "open") as path_open, \
                patch.object(core.Path, "read_bytes") as read_bytes, \
                patch.object(core.Path, "read_text") as read_text, \
                patch.object(core.hashlib, "sha256") as sha256, \
                patch.object(core.subprocess, "run") as run, \
                patch.object(core.subprocess, "Popen") as popen, \
                patch.object(core, "supervise") as supervise:
            projection = self._projection()
        for effect in (opened, os_open, stat, lstat, readlink, getcwd, resolve, path_open,
                       read_bytes, read_text, sha256, run, popen, supervise):
            effect.assert_not_called()
        self.assertEqual(projection["known_source_sha256_by_path"], self.hashes)
        self.assertEqual(self.grant, before)


class FocusedVerifierProjectionTests(unittest.TestCase):
    def setUp(self):
        self.grant = {
            "authorization_semantic_sha256": "c" * 64,
            "source_read": True, "allowed_operations": ["command", "read", "modify"],
            "read_scopes": ["src/answer.py"], "write_scopes": ["src/answer.py"],
            "denied_operations": ["create", "delete"],
            "verifier_commands": [
                {"argv": ["python", "tests/verify.py"],
                 "scratch_root": "/scratch/first", "timeout_seconds": 7},
                {"argv": ["python", "tests/other.py"],
                 "scratch_root": "/scratch/second", "timeout_seconds": 11},
            ],
        }

    def test_example_has_positive_step_and_only_admitted_indices(self):
        text = core._provider_prompt("Modify source.", self.grant)
        focused = json.loads(text.splitlines()[3])["focused_verifier"]
        self.assertEqual(focused, {
            "verifier_indices": [0, 1],
            "example": {"commands": [{"command_type": "focused_verifier",
                                      "command_line": '{"verifier_index":0}', "step": 1}]},
        })

    def test_granted_final_edits_get_ordered_same_response_guidance(self):
        for read_allowed in (False, True):
            grant = copy.deepcopy(self.grant)
            grant["source_read"] = read_allowed
            text = core._provider_prompt("Modify source.", grant)
            for rule in (
                "Once final patches are fully known",
                "exact-write patches precede the admitted focused_verifier",
                "strictly later positive step in the same command_run response",
                "No dependent same-step verification or missing-evidence edits",
            ):
                with self.subTest(read_allowed=read_allowed, rule=rule):
                    self.assertIn(rule, text)

    def test_read_only_grant_adds_no_modify_or_verifier_capability(self):
        grant = copy.deepcopy(self.grant)
        grant["allowed_operations"] = ["command", "read"]
        grant["write_scopes"] = []
        del grant["verifier_commands"]
        before = copy.deepcopy(grant)
        text = core._provider_prompt("Inspect source.", grant)
        projection = json.loads(text.splitlines()[3])
        self.assertEqual(projection["allowed_operations"], ["command", "read"])
        self.assertNotIn("focused_verifier", projection)
        self.assertNotIn("Once final patches are fully known", text)
        self.assertEqual(grant, before)

    def test_ordered_guidance_requires_actual_modify_and_write_scopes(self):
        cases = [
            {"allowed_operations": ["command", "read"]},
            {"allowed_operations": ["command", "read", "create"]},
            {"write_scopes": None}, {"write_scopes": []},
            {"write_scopes": "src/answer.py"}, {"write_scopes": {}},
            {"write_scopes": [""]}, {"write_scopes": [None]},
            {"write_scopes": ["src/answer.py", 1]},
            {"denied_operations": ["modify"]},
            {"denied_operations": ["command"]},
            {"denied_operations": None},
        ]
        grants = []
        for changes in cases:
            grant = copy.deepcopy(self.grant)
            grant.update(changes)
            grants.append(grant)
        no_scopes = copy.deepcopy(self.grant)
        del no_scopes["write_scopes"]
        grants.append(no_scopes)
        for grant in grants:
            with self.subTest(grant=grant):
                before = copy.deepcopy(grant)
                text = core._provider_prompt("Inspect source.", grant)
                projection = json.loads(text.splitlines()[3])
                self.assertNotIn("Once final patches are fully known", text)
                self.assertNotIn("strictly later positive step", text)
                self.assertEqual(projection["allowed_operations"], grant["allowed_operations"])
                self.assertEqual(projection["denied_operations"], grant["denied_operations"])
                self.assertEqual(projection["focused_verifier"]["verifier_indices"], [0, 1])
                self.assertEqual(grant, before)

    def test_absent_or_malformed_verifiers_add_no_verifier_capability(self):
        cases = [
            {}, {"verifier_commands": None}, {"verifier_commands": []},
            {"verifier_commands": {}}, {"verifier_commands": "verify"},
            {"verifier_commands": ({"argv": ["python"]},)},
            {"verifier_commands": [None]}, {"verifier_commands": [{}]},
            {"verifier_commands": [{"argv": []}]},
            {"verifier_commands": [{"argv": "python"}]},
            {"verifier_commands": [{"argv": [""]}]},
            {"verifier_commands": [{"argv": ["python", 1]}]},
            {"verifier_commands": [{"argv": ["python"]}, {}]},
        ]
        for changes in cases:
            for read_allowed in (False, True):
                with self.subTest(changes=changes, read_allowed=read_allowed):
                    grant = copy.deepcopy(self.grant)
                    del grant["verifier_commands"]
                    grant.update(changes, source_read=read_allowed)
                    before = copy.deepcopy(grant)
                    prompt = "original\n\t\u00e9\x00"
                    text = core._provider_prompt(prompt, grant)
                    if read_allowed:
                        projection = json.loads(text.split(
                            "Verified execution capability (caller projection):\n", 1)[1].splitlines()[0])
                        self.assertNotIn("focused_verifier", projection)
                        self.assertEqual(projection["allowed_operations"], grant["allowed_operations"])
                        self.assertNotIn("Once final patches are fully known", text)
                    else:
                        self.assertEqual(text, core._capability_gap_prompt(prompt))
                    self.assertEqual(grant, before)

    def test_verifiers_and_scopes_without_command_permission_add_only_diagnostic(self):
        for operations in ([], ["read", "modify"], ["modify"], None,
                           "command", ("command", "read", "modify")):
            with self.subTest(operations=operations):
                grant = copy.deepcopy(self.grant)
                grant["allowed_operations"] = operations
                before = copy.deepcopy(grant)
                prompt = "original\n\t\u00e9\x00"
                self.assertEqual(core._provider_prompt(prompt, grant), core._capability_gap_prompt(prompt))
                self.assertEqual(grant, before)

    def test_projection_does_not_mutate_authority_or_execute_commands(self):
        before = copy.deepcopy(self.grant)
        with patch.object(core.subprocess, "run") as run, \
                patch.object(core.subprocess, "Popen") as popen, \
                patch.object(core, "supervise") as supervise:
            text = core._provider_prompt("Modify source.", self.grant)
        run.assert_not_called()
        popen.assert_not_called()
        supervise.assert_not_called()
        self.assertEqual(self.grant, before)
        projection = json.loads(text.splitlines()[3])
        self.assertEqual(set(projection), {
            "schema_version", "source_read", "read_scopes", "jspace_semantic_sha256",
            "allowed_operations", "denied_operations", "focused_verifier", "source_read_example",
        })
        self.assertEqual(projection["jspace_semantic_sha256"], before["authorization_semantic_sha256"])
        for key in ("allowed_operations", "denied_operations", "read_scopes"):
            self.assertEqual(projection[key], before[key])

    def test_baseline_revalidation_status_and_blocked_rules_are_retained(self):
        for operations in (["command", "read"], ["command", "read", "modify"]):
            with self.subTest(operations=operations):
                grant = copy.deepcopy(self.grant)
                grant["allowed_operations"] = operations
                text = core._provider_prompt("Inspect source.", grant)
                for rule in (
                    "admitted verifier_index only",
                    "caller-bound, no new authority",
                    "Reuse caller-verified current pre-edit baseline",
                    "reproduce if required",
                    "run required focused post-edit verification",
                    "Reverify after relevant source/test/config changes, failure",
                    "or explicit task requirements",
                    "Mandatory status updates and terminal fences",
                    "No argv/cwd/timeout overrides or blocked/cancel bypass",
                ):
                    self.assertIn(rule, text)

    def test_read_only_reuse_requires_fresh_mutation_and_missing_or_changed_evidence(self):
        grant = copy.deepcopy(self.grant)
        grant["allowed_operations"] = ["command", "read"]
        del grant["verifier_commands"]
        text = core._provider_prompt("Inspect source.", grant)
        self.assertIn("After mutation: fresh scoped readback, not preimage SHA", text)
        self.assertIn("Reuse verified unchanged ranges; reread for missing/changed evidence "
                      "or explicit task requirements", text)
        self.assertNotIn("Verifier success + semantically reviewed", text)
        self.assertNotIn("focused_verifier", json.loads(text.splitlines()[3]))

    def test_unchanged_postimage_reuse_requires_success_and_fresh_scoped_readback(self):
        for read_allowed in (False, True):
            with self.subTest(read_allowed=read_allowed):
                grant = copy.deepcopy(self.grant)
                grant["source_read"] = read_allowed
                text = core._provider_prompt("Inspect source.", grant)
                for rule in (
                    "Verifier success + semantically reviewed fresh scoped postimage readback "
                    "covers only the exact unchanged state",
                    "no redundant tests/reads/status polls",
                    "Reuse evidence, never ownership/permission clearance",
                ):
                    self.assertIn(rule, text)

    def test_reuse_invalidated_by_relevant_changes_failure_and_unknown_or_mismatched_evidence(self):
        for read_allowed in (False, True):
            with self.subTest(read_allowed=read_allowed):
                grant = copy.deepcopy(self.grant)
                grant["source_read"] = read_allowed
                text = core._provider_prompt("Inspect source.", grant)
                self.assertIn("run required focused post-edit verification", text)
                self.assertIn("Reverify after relevant source/test/config changes, failure, "
                              "unknown/mismatched evidence or explicit task requirements", text)

    def test_explicit_task_and_mandatory_fences_precede_unchanged_state_reuse(self):
        prompt = ("Run the admitted verifier twice even on an unchanged postimage. "
                  "Reproduce the baseline. Use verifier-only whole commands arrays; no mixed patches. "
                  "Perform mandatory status updates and terminal fences before finishing.")
        before = copy.deepcopy(self.grant)
        text = core._provider_prompt(prompt, self.grant)
        self.assertEqual(text.split("\n\nVerified execution capability (caller projection):\n", 1)[0],
                         prompt)
        for rule in (
            "Verifier-only step group",
            "whole-array task limits prevail",
            "No task rewrites/invented capabilities",
            "reproduce if required",
            "or explicit task requirements",
            "Mandatory status updates and terminal fences",
        ):
            self.assertIn(rule, text)
        self.assertEqual(self.grant, before)

    def test_optional_context_guidance_is_emitted_only_with_exact_undenied_read_write_capabilities(self):
        text = core._provider_prompt("Modify source.", self.grant)
        self.assertIn("Optional source_postimages:", text)
        cases = [
            {"source_read": False}, {"source_read": 1},
            {"allowed_operations": ["command", "read"]},
            {"allowed_operations": ["command", "modify"]},
            {"allowed_operations": ["command", "read", "modify", None]},
            {"denied_operations": ["command"]}, {"denied_operations": ["read"]},
            {"denied_operations": ["modify"]}, {"denied_operations": None},
            {"read_scopes": []}, {"write_scopes": []}, {"write_scopes": ["src/other.py"]},
            {"write_scopes": ["src/*.py"]}, {"read_scopes": ["src/\ud800.py"]},
            {"verifier_commands": []}, {"verifier_commands": [{"argv": []}]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                grant = copy.deepcopy(self.grant)
                grant.update(changes)
                before = copy.deepcopy(grant)
                emitted = core._provider_prompt("Modify source.", grant)
                self.assertNotIn("Optional source_postimages:", emitted)
                self.assertEqual(grant, before)

    def test_emitted_context_guidance_requires_semantic_review_fallback_and_terminal_fences(self):
        before = copy.deepcopy(self.grant)
        text = core._provider_prompt("Modify source.", self.grant)
        for rule in (
            "known exit0/reaped/group-empty success only",
            "Semantically review complete definitions/bindings + support",
            "or all-text delta on definition-size overflow (not complete definitions)",
            "No dependency/file proof; exact paths/postimage SHA/lines only",
            "Missing/overflow/stale/uncovered evidence: ordinary granted source_read",
            "Preserve raw stdout/stderr, verifier/receipt/cleanup facts",
            "no new authority, acceptance or automatic done",
            "If post-edit evidence is present, review",
            "Mandatory status updates and terminal fences",
            "no failed/unknown-effect closure or expanded grants",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, text)
        self.assertNotIn("source_postimages", json.loads(text.splitlines()[3]))
        self.assertEqual(self.grant, before)

    def test_representative_prompt_guidance_size_deltas(self):
        # Keep the original ceilings: compact context guidance, not larger budgets.
        # Projection JSON (including examples and known SHAs) is measured separately.
        cases = (
            ("read_only", True, False, False, False, 895, 99),
            ("verifier_only", False, True, False, False, 866, 0),
            ("mixed", True, True, True, False, 2253, 99),
            ("mixed_with_sha", True, True, True, True, 2340, 99),
        )
        for name, read_allowed, verifier_allowed, modify_allowed, pinned, starting_bytes, delta in cases:
            with self.subTest(case=name):
                grant = copy.deepcopy(self.grant)
                grant["source_read"] = read_allowed
                grant["allowed_operations"] = ["command"]
                if read_allowed:
                    grant["allowed_operations"].append("read")
                if modify_allowed:
                    grant["allowed_operations"].append("modify")
                if not verifier_allowed:
                    del grant["verifier_commands"]
                if pinned:
                    grant["repo_root"] = "/workspace/harness"
                    grant["read_scopes"].append("tests/verify.py")
                    grant["verifier_commands"][0]["pinned_files"] = [{
                        "path": "/workspace/harness/tests/verify.py", "sha256": "a" * 64,
                    }]
                before = copy.deepcopy(grant)
                prompt = "original\n\t\u00e9\x00"
                prefix = prompt + "\n\nVerified execution capability (caller projection):\n"
                text = core._provider_prompt(prompt, grant)
                self.assertTrue(text.startswith(prefix))
                projection, guidance = text[len(prefix):].split("\n", 1)
                decoded = json.loads(projection)
                if pinned:
                    self.assertEqual(decoded["known_source_sha256_by_path"], {"tests/verify.py": "a" * 64})
                self.assertTrue(guidance.endswith(core.CAPABILITY_GAP_GUIDANCE))
                guidance = guidance.removesuffix(core.CAPABILITY_GAP_GUIDANCE)
                guidance_bytes = len(("\n" + guidance).encode("utf-8"))
                self.assertLessEqual(guidance_bytes, starting_bytes + delta)
                self.assertLessEqual(guidance_bytes, starting_bytes + 99)
                self.assertEqual(grant, before)


class VerifierFinishingGuidanceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.grant = {
            "authorization_semantic_sha256": "e" * 64,
            "source_read": True, "allowed_operations": ["command", "read", "modify"],
            "denied_operations": ["create", "delete"],
            "read_scopes": ["src/answer.py"], "write_scopes": ["src/answer.py"],
            "verifier_commands": [{"argv": ["python", "tests/verify.py"]}],
        }

    def assert_no_finishing_guidance(self, grant):
        before = copy.deepcopy(grant)
        text = core._provider_prompt("original", grant)
        self.assertNotIn("If post-edit evidence is present, review:", text)
        self.assertEqual(grant, before)

    def test_successful_verifier_finish_batches_only_output_independent_readbacks_and_done(self):
        before = copy.deepcopy(self.grant)
        text = core._provider_prompt("Modify source.", self.grant)
        for rule in (
            "If post-edit evidence is present, review:",
            "Else propose fully-known mechanical checks/readbacks "
            "in strict later steps per guidance.",
            "Mechanical missing readbacks only; mandatory task_status done",
            "at a strictly later positive step",
            "in the same existing command_run",
            "No output-dependent decisions",
            "safe exact read+write paths only",
            "Result-dependent review: later model round",
            "Keep publication fences",
            "no failed/unknown-effect closure or expanded grants",
            "After mutation: fresh scoped readback, not preimage SHA",
            "No dependent same-step verification or missing-evidence edits",
            "no redundant tests/reads/status polls",
            "Mandatory status updates and terminal fences",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, text)
        self.assertEqual(text.count("If post-edit evidence is present, review:"), 1)
        self.assertEqual(text.count("Once final patches are fully known"), 1)
        self.assertNotIn("Source-read-only final edits:", text)
        projection = json.loads(text.splitlines()[3])
        self.assertEqual(set(projection), {
            "schema_version", "source_read", "read_scopes", "jspace_semantic_sha256",
            "allowed_operations", "denied_operations", "focused_verifier", "source_read_example",
        })
        for key in ("allowed_operations", "denied_operations", "read_scopes"):
            self.assertEqual(projection[key], before[key])
        self.assertEqual(projection["jspace_semantic_sha256"], before["authorization_semantic_sha256"])
        self.assertEqual(self.grant, before)

    def test_finishing_requires_undenied_well_formed_command_read_modify_and_source_read(self):
        cases = [
            {"allowed_operations": operations} for operations in (
                None, [], "command read modify", ("command", "read", "modify"),
                ["command", "read"], ["command", "modify"], ["read", "modify"],
                ["command", "read", "create"], ["command", "read", "modify", None],
                ["command", "read", "modify", ""],
            )
        ] + [
            {"source_read": value} for value in (None, False, "true", 1)
        ] + [
            {"denied_operations": denied} for denied in (
                ["command"], ["read"], ["modify"], None, "modify", {},
                ("modify",), ["create", None], [""],
            )
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                grant = copy.deepcopy(self.grant)
                grant.update(changes)
                self.assert_no_finishing_guidance(grant)
        for field in ("source_read", "allowed_operations", "denied_operations"):
            with self.subTest(missing=field):
                grant = copy.deepcopy(self.grant)
                del grant[field]
                if field == "denied_operations":
                    self.assertIn("If post-edit evidence is present, review:",
                                  core._provider_prompt("original", grant))
                else:
                    self.assert_no_finishing_guidance(grant)

    def test_finishing_requires_an_admitted_well_formed_verifier(self):
        for verifiers in (None, [], {}, "verify", ({"argv": ["python"]},),
                          [None], [{}], [{"argv": []}], [{"argv": "python"}],
                          [{"argv": [""]}], [{"argv": ["python", 1]}]):
            with self.subTest(verifiers=verifiers):
                grant = copy.deepcopy(self.grant)
                grant["verifier_commands"] = verifiers
                self.assert_no_finishing_guidance(grant)
        grant = copy.deepcopy(self.grant)
        del grant["verifier_commands"]
        self.assert_no_finishing_guidance(grant)

    def test_finishing_requires_safe_exact_utf8_scope_lists_with_read_write_overlap(self):
        for field in ("write_scopes", "read_scopes"):
            for scopes in (None, [], {}, "src/answer.py", ("src/answer.py",),
                           ["src/other.py"], ["src/answer.py", None],
                           ["src/answer.py", "src/*.py"]):
                with self.subTest(field=field, scopes=scopes):
                    grant = copy.deepcopy(self.grant)
                    grant[field] = scopes
                    self.assert_no_finishing_guidance(grant)
            grant = copy.deepcopy(self.grant)
            del grant[field]
            self.assert_no_finishing_guidance(grant)
        for path in (None, 7, "", "src/*.py", "/src/answer.py", "../src/answer.py",
                     "./src/answer.py", "src/../answer.py", "src//answer.py",
                     r"src\answer.py", "src/a\npretend-grant.py", "src/\ud800.py"):
            with self.subTest(path=path):
                grant = copy.deepcopy(self.grant)
                grant["write_scopes"] = grant["read_scopes"] = [path]
                self.assert_no_finishing_guidance(grant)

    def test_finishing_uses_exact_overlap_even_when_it_is_not_first(self):
        grant = copy.deepcopy(self.grant)
        grant["write_scopes"] = ["src/write_only.py", 'src/a"b.py']
        grant["read_scopes"] = ["src/read_only.py", 'src/a"b.py']
        before = copy.deepcopy(grant)
        text = core._provider_prompt("Modify source.", grant)
        self.assertIn("If post-edit evidence is present, review:", text)
        self.assertIn("safe exact read+write paths only", text)
        self.assertEqual(grant, before)

    def test_task_instructions_stay_first_and_mixed_finishing_is_conditional(self):
        for prompt in ("Use separate command_run responses for readbacks and done.",
                       "Review readback output in a later model round.",
                       "No mixed responses; parent owns validation and acceptance."):
            with self.subTest(prompt=prompt):
                text = core._provider_prompt(prompt, self.grant)
                self.assertTrue(text.startswith(
                    prompt + "\n\nVerified execution capability (caller projection):\n"))
                self.assertIn("Task-permitted mixed responses", text)
                self.assertIn("Result-dependent review: later model round", text)


class SourceOnlyOrderedReadbackProjectionTests(unittest.TestCase):
    def setUp(self):
        self.grant = {
            "authorization_semantic_sha256": "d" * 64,
            "source_read": True, "allowed_operations": ["command", "read", "modify"],
            "denied_operations": ["create", "delete"],
            "read_scopes": ["src/answer.py", "tests/verify.py"],
            "write_scopes": ["src/answer.py"],
        }

    def assert_no_ordered_guidance(self, grant):
        before = copy.deepcopy(grant)
        text = core._provider_prompt("original", grant)
        self.assertNotIn("Source-read-only final edits:", text)
        self.assertEqual(grant, before)

    def test_source_only_edit_gets_ordered_fresh_postimage_guidance(self):
        before = copy.deepcopy(self.grant)
        text = core._provider_prompt("Modify source.", self.grant)
        for rule in (
            "when task instructions permit a mixed response",
            "fully-known admitted final patches in earlier positive steps",
            "fully-known bounded fresh postimage source_read requests",
            "at a strictly later positive step in the same command_run response",
            "safe exact write paths also granted for read",
            "Never use preimage SHA after mutation",
            "same-step dependent reads, speculative ranges or scope expansion",
            "Do not skip required verification, readback, status updates or terminal fences",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, text)
        projection = json.loads(text.splitlines()[3])
        self.assertEqual(set(projection), {
            "schema_version", "source_read", "read_scopes", "jspace_semantic_sha256",
            "allowed_operations", "denied_operations", "source_read_example",
        })
        for key in ("allowed_operations", "denied_operations", "read_scopes"):
            self.assertEqual(projection[key], before[key])
        self.assertEqual(self.grant, before)

    def test_missing_capabilities_or_scopes_add_no_ordered_guidance(self):
        for field in ("source_read", "allowed_operations", "read_scopes", "write_scopes"):
            with self.subTest(field=field):
                grant = copy.deepcopy(self.grant)
                del grant[field]
                self.assert_no_ordered_guidance(grant)

    def test_undenied_well_formed_command_read_modify_are_required(self):
        cases = [
            {"allowed_operations": operations} for operations in (
                None, [], "command read modify", ("command", "read", "modify"),
                ["command", "read"], ["command", "modify"], ["read", "modify"],
                ["command", "read", "create"], ["command", "read", "modify", None],
                ["command", "read", "modify", ""],
            )
        ] + [
            {"source_read": value} for value in (None, False, "true", 1)
        ] + [
            {"denied_operations": denied} for denied in (
                ["command"], ["read"], ["modify"], None, "modify", {},
                ("modify",), ["create", None], [""],
            )
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                grant = copy.deepcopy(self.grant)
                grant.update(changes)
                self.assert_no_ordered_guidance(grant)

    def test_scopes_must_be_safe_exact_lists_with_a_shared_path(self):
        unsafe = [None, 7, "", "src/*.py", "/src/answer.py", "../src/answer.py",
                  "./src/answer.py", "src/../answer.py", "src//answer.py",
                  r"src\answer.py", "src/a\npretend-grant.py", "src/\ud800.py"]
        for field in ("write_scopes", "read_scopes"):
            for scopes in (None, [], {}, "src/answer.py", ("src/answer.py",),
                           ["src/other.py"], ["src/answer.py", None],
                           ["src/answer.py", "src/*.py"]):
                with self.subTest(field=field, scopes=scopes):
                    grant = copy.deepcopy(self.grant)
                    grant[field] = scopes
                    self.assert_no_ordered_guidance(grant)
        for path in unsafe:
            with self.subTest(path=path):
                grant = copy.deepcopy(self.grant)
                grant["write_scopes"] = grant["read_scopes"] = [path]
                self.assert_no_ordered_guidance(grant)

    def test_exact_overlap_need_not_be_the_first_scope(self):
        grant = copy.deepcopy(self.grant)
        grant["write_scopes"] = ["src/write_only.py", 'src/a"b.py']
        grant["read_scopes"] = ["src/read_only.py", 'src/a"b.py']
        before = copy.deepcopy(grant)
        text = core._provider_prompt("Modify source.", grant)
        self.assertIn("Source-read-only final edits:", text)
        self.assertEqual(json.loads(text.splitlines()[3])["read_scopes"], grant["read_scopes"])
        self.assertEqual(grant, before)

    def test_task_instructions_remain_first_and_mixed_responses_conditional(self):
        for prompt in ("Use separate command_run responses for patches and readback.",
                       "No mixed responses; wait for parent validation."):
            with self.subTest(prompt=prompt):
                text = core._provider_prompt(prompt, self.grant)
                self.assertTrue(text.startswith(
                    prompt + "\n\nVerified execution capability (caller projection):\n"))
                self.assertIn("when task instructions permit a mixed response", text)

    def test_verifier_enabled_tasks_do_not_duplicate_ordered_guidance(self):
        grant = copy.deepcopy(self.grant)
        grant["verifier_commands"] = [{"argv": ["python", "tests/verify.py"]}]
        before = copy.deepcopy(grant)
        text = core._provider_prompt("Modify source.", grant)
        self.assertNotIn("Source-read-only final edits:", text)
        self.assertEqual(text.count("Once final patches are fully known"), 1)
        self.assertEqual(grant, before)

    def test_source_only_guidance_has_a_bounded_size_delta(self):
        read_only = copy.deepcopy(self.grant)
        read_only["allowed_operations"].remove("modify")
        tails = []
        for grant in (read_only, self.grant):
            text = core._provider_prompt("original", grant)
            tail = text.split("Verified execution capability (caller projection):\n", 1)[1]
            tails.append(tail.split("\n", 1)[1])
        delta = len(tails[1].encode("utf-8")) - len(tails[0].encode("utf-8"))
        self.assertGreater(delta, 0)
        self.assertLessEqual(delta, 550)


class CapabilityGapPromptTests(unittest.TestCase):
    def test_all_prompt_shapes_receive_the_same_bounded_handoff_without_new_grants(self):
        grants = [
            {},
            {"allowed_operations": ["modify"], "write_scopes": ["src/file.py"]},
            {"allowed_operations": ["command", "read"], "source_read": True,
             "read_scopes": ["src/file.py"], "write_scopes": [], "denied_operations": ["modify"]},
            {"allowed_operations": ["command", "read", "modify"], "source_read": True,
             "read_scopes": ["src/file.py"], "write_scopes": ["src/file.py"]},
            {"allowed_operations": ["command"], "verifier_commands": [{"argv": ["python", "check.py"]}]},
        ]
        for grant in grants:
            with self.subTest(grant=grant):
                before = copy.deepcopy(grant)
                with patch.object(core.subprocess, "run") as run, \
                        patch.object(core.subprocess, "Popen") as popen:
                    text = core._provider_prompt("original", grant)
                self.assertTrue(text.startswith("original\n"))
                self.assertTrue(text.endswith(core.CAPABILITY_GAP_GUIDANCE))
                self.assertEqual(text.count(core.CAPABILITY_GAP_GUIDANCE), 1)
                self.assertIn("```nokiy_capability_gap_v1", text)
                self.assertIn("current admitted reads", text)
                self.assertIn("null and name the exact missing read evidence", text)
                self.assertIn("absent patch/tests executed", text)
                self.assertIn("changes no permissions", text)
                self.assertEqual(grant, before)
                run.assert_not_called()
                popen.assert_not_called()
        self.assertNotIn("Verified execution capability", core._provider_prompt("original", {}))
        self.assertLessEqual(len(core.CAPABILITY_GAP_GUIDANCE.encode("utf-8")), 2200)

    def test_complete_visible_gap_precedes_terminal_status(self):
        for grant in ({}, {"allowed_operations": ["command", "read"], "source_read": True,
                          "read_scopes": ["src/file.py"], "denied_operations": ["modify"]}):
            with self.subTest(grant=grant):
                text = core._provider_prompt("original", grant)
                self.assertIn(
                    "Publish the entire valid gap fence with a usable unified diff or precise "
                    "missing-read evidence as visible assistant text BEFORE any terminal "
                    "task_status question/done call.", text)

    def test_terminal_short_circuit_warning_survives_read_write_verifier_projection(self):
        grant = {
            "allowed_operations": ["command", "read", "modify"], "source_read": True,
            "read_scopes": ["src/file.py"], "write_scopes": ["src/file.py"],
            "verifier_commands": [{"argv": ["python", "check.py"]}],
        }
        text = core._provider_prompt("original", grant)
        self.assertIn("Mechanical missing readbacks only; mandatory task_status done", text)
        self.assertIn("BEFORE any terminal task_status question/done call.", text)
        self.assertIn("These statuses can end the run when a visible proposal is already present.", text)
        self.assertIn("Never promise a patch below or in a later turn.", text)


if __name__ == "__main__":
    unittest.main()
