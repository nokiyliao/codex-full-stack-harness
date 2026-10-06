# SPDX-License-Identifier: MIT
"""Opt-in caller startup state; synthetic images only, no provider requests."""
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_embedded_nokiy as request_fixtures
import test_full_core as core_fixtures
import test_full_stack as stack_fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core as core
from codex_collaboration_harness import full_stack as stack


STATE = {"task_group": "caller setup", "task_type": ["opaque-A", "opaque-B"]}
PADDED_STATE = {"task_group": "  caller setup  ", "task_type": [" opaque-A ", "opaque-B"]}
BOUNDARY_STATE = {"task_group": "é" * 128,
                  "task_type": ["é" * 63 + f"{i:02d}" for i in range(8)]}


def advertise_state(fixture):
    runtime = fixture.f.runtime / "tura_runtime"
    runtime.write_bytes(runtime.read_bytes() + b"\n# nokiy_initial_task_state_v1\n")
    fixture._image()


class InitialTaskStateRequestTests(unittest.TestCase):
    def setUp(self):
        self.f = request_fixtures.EmbeddedNokiyFixture()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.raw = json.loads(self.f.request_path.read_text())

    def test_absent_state_preserves_wire_and_request_identities(self):
        values = [self.raw] + [dict(self.raw, execution_profile=p) for p in ("direct", "balanced")]
        for schema, keys in ((caller.LEGACY_REQUEST_SCHEMA_VERSION, caller.LEGACY_REQUEST_KEYS),
                             (caller.PREVIOUS_REQUEST_SCHEMA_VERSION, caller.PREVIOUS_REQUEST_KEYS)):
            values.append(dict({key: self.raw[key] for key in keys}, schema_version=schema))
        for value in values:
            with self.subTest(schema=value["schema_version"], profile=value.get("execution_profile")):
                request = caller.decode_request(value)
                self.assertIsNone(request.initial_task_state)
                self.assertNotIn("initial_task_state", request.to_wire())
                self.assertEqual(request.to_wire(include_identity=False), value)
                self.assertEqual(request.request_sha256, request_fixtures.canonical_sha256(value))
                self.assertEqual(request.request_id, "tura_embedded_" + request.request_sha256)

    def test_normalized_roundtrip_and_both_state_fields_bind_digest(self):
        for profile in ("direct", "balanced"):
            base = dict(self.raw, execution_profile=profile)
            value = dict(base, initial_task_state=PADDED_STATE)
            request = caller.decode_request(value)
            expected = dict(base, initial_task_state=STATE)
            with self.subTest(profile=profile):
                self.assertEqual(request.initial_task_state, STATE)
                self.assertEqual(value["initial_task_state"], PADDED_STATE)
                self.assertEqual(request.to_wire(include_identity=False), expected)
                self.assertEqual(request.request_sha256, request_fixtures.canonical_sha256(expected))
                self.assertEqual(caller.decode_request(expected), request)
                self.assertNotEqual(request.request_sha256, caller.decode_request(base).request_sha256)
                for state in (dict(STATE, task_group="other"), dict(STATE, task_type=["opaque-C"])):
                    changed = caller.decode_request(dict(base, initial_task_state=state))
                    self.assertNotEqual(request.request_sha256, changed.request_sha256)

    def test_utf8_limits_trim_and_opaque_types_without_a_caller_catalog(self):
        padded = dict(BOUNDARY_STATE, task_group=" \u2003" + BOUNDARY_STATE["task_group"] + " ",
                      task_type=[" " + item + " " for item in BOUNDARY_STATE["task_type"]])
        request = caller.decode_request(dict(self.raw, execution_profile="direct", initial_task_state=padded))
        self.assertEqual(request.initial_task_state, BOUNDARY_STATE)
        self.assertEqual(len(request.initial_task_state["task_group"].encode("utf-8")), 256)
        self.assertTrue(all(len(item.encode("utf-8")) == 128 for item in request.initial_task_state["task_type"]))

    def test_malformed_states_are_typed_errors_before_path_access(self):
        invalid = [None, True, [], "", {}, {"task_group": "x"}, dict(STATE, unknown=True)]
        invalid += [dict(STATE, task_group=x) for x in (None, True, 1, [], {}, "", "  ", "é" * 129, "\ud800")]
        invalid += [dict(STATE, task_type=x) for x in (None, True, {}, (), "", [],
                    ["x", " x "], [str(i) for i in range(9)])]
        invalid += [dict(STATE, task_type=[x]) for x in (None, True, 1, [], {}, "", "  ", "é" * 65, "\udfff")]
        for codepoint in (*range(32), *range(127, 160)):
            invalid += [dict(STATE, task_group=chr(codepoint) + "x"),
                        dict(STATE, task_type=["x" + chr(codepoint)])]
        for state in invalid:
            with self.subTest(state=repr(state)), self.assertRaises(caller.EmbeddedNokiyError) as raised:
                caller.decode_request(dict(self.raw, execution_profile="direct", initial_task_state=state,
                                           workspace=str(self.f.root / "missing")))
            self.assertEqual(raised.exception.code, "NOKIY_EMBEDDED_REQUEST_INVALID")
            self.assertIn("initial_task_state", str(raised.exception))

    def test_state_rejects_old_schemas_native_once_and_graph_profiles(self):
        values = [self.raw] + [dict(self.raw, execution_profile=p) for p in ("native_once", "graph", "pool")]
        for schema, keys in ((caller.LEGACY_REQUEST_SCHEMA_VERSION, caller.LEGACY_REQUEST_KEYS),
                             (caller.PREVIOUS_REQUEST_SCHEMA_VERSION, caller.PREVIOUS_REQUEST_KEYS)):
            values.append(dict({key: self.raw[key] for key in keys}, schema_version=schema))
        for value in values:
            with self.subTest(schema=value["schema_version"], profile=value.get("execution_profile")):
                with self.assertRaises(caller.EmbeddedNokiyError) as raised:
                    caller.decode_request(dict(value, initial_task_state=STATE))
                self.assertEqual(raised.exception.code, "NOKIY_EMBEDDED_REQUEST_INVALID")


class InitialTaskStateRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = core_fixtures.FullCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_environment_scrubs_spoof_and_mints_only_sha_session_bound_opt_in(self):
        runtime = caller.verify_runtime_image(self.fixture._request().runtime_image, required_artifacts=core.REQUIRED)
        for profile in ("direct", "balanced"):
            base = self.fixture._request(profile).to_wire(include_identity=False)
            for state in (None, STATE, BOUNDARY_STATE):
                value = base if state is None else dict(base, initial_task_state=state)
                request = caller.decode_request(value)
                with self.subTest(profile=profile, state=state), patch.dict(os.environ, {
                        core.INITIAL_TASK_STATE_ENV: '{"task_group":"spoofed"}'}):
                    env = core._environment(request, runtime, self.fixture.f.root / "state")
                if state is None:
                    self.assertNotIn(core.INITIAL_TASK_STATE_ENV, env)
                    continue
                raw = env[core.INITIAL_TASK_STATE_ENV]
                self.assertEqual(json.loads(raw), dict(state, schema_version="nokiy_initial_task_state_v1",
                    request_sha256=request.request_sha256, session_id="full-" + request.request_sha256))
                self.assertLessEqual(len(raw.encode("utf-8")), 4096)
                if state == BOUNDARY_STATE:
                    self.assertIn("é", raw)
                    self.assertNotIn("\\u00e9", raw)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_old_image_blocks_only_opt_in_before_prompt_or_provider(self):
        for profile in ("direct", "balanced"):
            baseline = self.fixture._request(profile)
            request = caller.decode_request(dict(baseline.to_wire(include_identity=False), initial_task_state=STATE))
            with self.subTest(profile=profile), patch.object(core, "_provider_prompt") as prompt, \
                    patch.object(core, "subprocess", SimpleNamespace(**vars(core.subprocess))) as process_api, \
                    patch.object(process_api, "Popen") as process:
                self.assertEqual(caller.preflight(baseline)["status"], "READY")
                ready = caller.preflight(request)
                self.assertEqual(ready["status"], "BLOCKED")
                self.assertEqual(ready["first_typed_blocker"], "NOKIY_FULL_CORE_INITIAL_TASK_STATE_REQUIRED")
                with self.assertRaises(caller.EmbeddedNokiyError) as raised:
                    core.execute_full_core(request)
                self.assertEqual(raised.exception.code, "NOKIY_FULL_CORE_INITIAL_TASK_STATE_REQUIRED")
                prompt.assert_not_called()
                process.assert_not_called()
                self.assertFalse((request.artifact_root / request.request_id).exists())
        advertise_state(self.fixture)
        for profile in ("direct", "balanced"):
            value = self.fixture._request(profile).to_wire(include_identity=False)
            self.assertEqual(caller.preflight(caller.decode_request(dict(value, initial_task_state=STATE)))["status"], "READY")


class InitialTaskStatePreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = stack_fixtures.FullStackTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_action_state_is_removed_before_compilation_and_bound_to_prepared_request(self):
        advertise_state(self.fixture.core)
        self.fixture.draft["runtime_image"]["sha256"] = request_fixtures.file_sha256(self.fixture.f.runtime_image)
        for profile in ("direct", "balanced"):
            self.fixture.output = self.fixture.f.root / ("prepared-" + profile)
            self.fixture.draft["execution_profile"] = profile
            action = {"mission": {"task_id": "test"}, "operations": ["read"],
                      "initial_task_state": PADDED_STATE, "terminal_delivery": "evidence_only"}
            self.fixture.action.write_text(json.dumps(action))
            with self.subTest(profile=profile), self.fixture.compiler() as compiler, \
                    patch.object(stack, "_compile_dcf", wraps=stack._compile_dcf) as compile_dcf, \
                    patch.object(caller, "execute") as execute:
                result = self.fixture.run_prepare()
            execute.assert_not_called()
            self.assertEqual(compiler.call_count, 1)
            compiled_action = {key: value for key, value in action.items()
                               if key not in {"initial_task_state", "terminal_delivery"}}
            self.assertEqual(compile_dcf.call_args.args[-1], compiled_action)
            preparation = json.loads((self.fixture.output / "preparation.json").read_text())
            self.assertEqual(preparation["action"], compiled_action)
            self.assertEqual(preparation["preflight"]["status"], "READY")
            request = caller.load_request(self.fixture.output / "request.json")
            self.assertEqual(request.initial_task_state, STATE)
            self.assertEqual(request.terminal_delivery, "evidence_only")
            self.assertEqual(request.request_sha256, request_fixtures.canonical_sha256(request.to_wire(include_identity=False)))
            self.assertNotIn("initial_task_state", self.fixture.draft)
            self.assertFalse(result["provider_execution_started"])

    def test_absent_preparation_omits_state_and_preserves_identity_on_old_image(self):
        with self.fixture.compiler():
            result = self.fixture.run_prepare()
        request = caller.load_request(self.fixture.output / "request.json")
        self.assertIsNone(request.initial_task_state)
        self.assertNotIn("initial_task_state", request.to_wire())
        self.assertEqual(request.request_sha256, request_fixtures.canonical_sha256(request.to_wire(include_identity=False)))
        self.assertEqual(caller.preflight(request)["status"], "READY")
        self.assertFalse(result["provider_execution_started"])

    def test_invalid_action_and_draft_state_never_compile_or_publish(self):
        action = {"mission": {"task_id": "test"}, "operations": ["read"]}
        for state in (None, {}, dict(STATE, unknown=1), dict(STATE, task_group="\tx"),
                      dict(STATE, task_type=["x", " x "])):
            self.fixture.action.write_text(json.dumps(dict(action, initial_task_state=state)))
            with self.subTest(state=state), self.fixture.compiler() as compiler:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "initial_task_state"):
                    self.fixture.run_prepare()
                compiler.assert_not_called()
                self.assertFalse(self.fixture.output.exists())
        self.fixture.action.write_text(json.dumps(action))
        for state in (None, STATE):
            self.fixture.draft["initial_task_state"] = state
            with self.subTest(draft=state), self.fixture.compiler() as compiler:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "initial_task_state belongs in action"):
                    self.fixture.run_prepare()
                compiler.assert_not_called()
                self.assertFalse(self.fixture.output.exists())

    def test_preparation_rejects_old_schema_native_once_and_graph_before_compile(self):
        self.fixture.action.write_text(json.dumps({"mission": {"task_id": "test"}, "operations": ["read"],
                                                  "initial_task_state": STATE}))
        baseline = dict(self.fixture.draft)
        changes = [{"schema_version": schema} for schema in (caller.LEGACY_REQUEST_SCHEMA_VERSION,
                                                             caller.PREVIOUS_REQUEST_SCHEMA_VERSION)]
        changes += [{"execution_profile": profile} for profile in (None, "native_once", "graph", "pool")]
        for change in changes:
            self.fixture.draft = dict(baseline, **change)
            with self.subTest(change=change), self.fixture.compiler() as compiler:
                with self.assertRaises(caller.EmbeddedNokiyError):
                    self.fixture.run_prepare()
                compiler.assert_not_called()
                self.assertFalse(self.fixture.output.exists())
