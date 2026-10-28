"""Disposable adapter fixtures; no installed paths, provider calls or real cutover."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_nokiy_caller_release.py"
spec = importlib.util.spec_from_file_location("caller_release_preparation", SCRIPT)
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)

THREAD = "019fd4f0-1079-77e2-8be7-cbf75ca28df5"
PINNED_LEGACY = adapter.LEGACY_PATH
PINNED_LEGACY_SHA256 = adapter.LEGACY_SHA256
IMPORT_LEGACY = adapter._import_legacy
EXECUTE_PAIRED_WIRE = adapter.Adapter.execute_paired_wire


def compact_evidence_bytes(result, root):
    fixture_root = json.dumps(str(root))[1:-1]
    return len(json.dumps(result).replace(fixture_root, "<fixture-root>").encode())


class CompactEvidenceBudgetTests(unittest.TestCase):
    def test_root_depth_is_not_payload_growth(self):
        short, deep = "/tmp/a", "/tmp/" + "deep/" * 80 + "a"
        make = lambda root: {"receipt": {"path": root + "/receipt.json", "sha256": "a" * 64}}
        self.assertEqual(compact_evidence_bytes(make(short), short), compact_evidence_bytes(make(deep), deep))
        oversized = {**make(deep), "unexpected_inline_payload": "x" * 8192}
        self.assertGreater(compact_evidence_bytes(oversized, deep), 8192)


class CallerReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo, self.task_root = self.root / "repo", self.root / "task"
        self.old, self.new = self.root / "releases/old", self.root / "releases/new"
        self.home = self.root / "home"
        self.pkg = self.old / adapter.SITE / adapter.PACKAGE
        self.source = self.repo / "src" / adapter.PACKAGE
        self.modules = ["changed.py", "protocol/new.json"]
        self.tests = ["test_changed"]
        for name, content in (("changed.py", "old = True\n"), ("unchanged.py", "preserved = True\n")):
            self.write(self.pkg / name, content)
            self.write(self.source / name, content)
        self.write(self.source / "changed.py", "new = True\n")
        self.write(self.source / "protocol/new.json", "{}\n")
        self.write(self.repo / "tests/test_changed.py", "# mocked suite\n")
        self.write(self.repo / "tests/test_verifier_parent.py", "# pinned native wire fixture\n")
        self.native_cli = self.root / "native-codex"
        self.write(self.native_cli, "native sandbox CLI fixture\n")
        for name in adapter.GUIDANCE:
            self.write(self.repo / name, "fixture\n")
        self.fixture_script = self.repo / adapter.SCRIPT
        self.write(self.fixture_script, "# pinned adapter fixture\n")
        self.source_python = self.repo / ".venv/bin/python"
        self.candidate_python = self.old / "caller/bin/python"
        self.write(self.source_python, "fixture source interpreter\n")
        self.write(self.candidate_python, "fixture candidate interpreter\n")
        self.write(self.old / "caller/bin/nokiy-embedded-run", "fixture entry\n")
        self.entry = self.home / ".local/bin/nokiy-embedded-run"
        self.entry.parent.mkdir(parents=True)
        self.entry.symlink_to(self.old / "caller/bin/nokiy-embedded-run")
        self.stdlib = self.root / "python-runtime"
        self.write(self.stdlib / "dependency.py", "pinned runtime dependency\n")
        self.config = self.home / ".codex/config.toml"
        self.write(self.config, 'model = "existing-model"\n')
        artifacts = {}
        for name in ("tura_router", "tura_session_db"):
            path = self.root / "runtime" / name
            self.write(path, "unchanged binary\n")
            artifacts[name] = adapter.identity(path)
        adapter.save(self.old / "runtime-image.json", {"artifacts": artifacts})
        adapter.save(self.old / "caller-binding.json", {"default_profile": {"model": "existing-model", "effort": "high"}})
        adapter.save(self.old / "source-provenance.json", {"build_id": "old"})
        self.legacy_file = self.root / "legacy/delivery.py"
        self.release_file = self.root / "recipe/release.py"
        self.deploy_file = self.root / "recipe/deploy.py"
        self.installer_file = self.root / "cache/cutover_v22_once.py"
        for path in (self.legacy_file, self.release_file, self.deploy_file, self.installer_file):
            self.write(path, "# mocked trusted template\n")
        self.lock = self.root / "installer.lock"
        self.write(self.lock, "")
        self.lock.chmod(0o600)
        self.task_root.mkdir()
        adapter.save(self.task_root / "authorization.json", {
            "native_thread_id": THREAD, "not_an_authority_issuer": True,
            "operator_request": "fixture-only", "outer_repo_lease": {"parent_owned": True}})
        inputs = [f"src/{adapter.PACKAGE}/{name}" for name in self.modules]
        inputs += ["tests/test_changed.py", *adapter.GUIDANCE]
        self.data = {
            "schema_version": adapter.SCHEMA, "native_thread_id": THREAD,
            "repo": str(self.repo), "artifact_root": str(self.task_root),
            "environment": {"HOME": str(self.home), "CODEX_HOME": None},
            "installed": {"release": str(self.old), "entry": adapter.identity(self.entry),
                          "metadata": {name: adapter.sha(self.old / name) for name in adapter.METADATA},
                          "package_sha256": adapter.tree_sha256(self.pkg)},
            "candidate": {"release": str(self.new), "version": "0.3.163.dev0", "build_id": "fixture-v163"},
            "action_id": "fixture-caller-v163", "modules": self.modules, "tests": self.tests,
            "postimages": {name: adapter.sha(self.repo / name) for name in inputs},
            "adapter": adapter.identity(self.fixture_script),
            "templates": {key: adapter.identity(path) for key, path in {
                "legacy": self.legacy_file, "release": self.release_file, "deploy": self.deploy_file,
                "installer": self.installer_file}.items()},
            "interpreters": {"controller": adapter.identity(self.source_python),
                             "source": adapter.identity(self.source_python), "candidate": adapter.identity(self.candidate_python)},
            "runtime_dependencies": {"files": [], "trees": [{"path": str(self.stdlib), "sha256": adapter.tree_sha256(self.stdlib)}]},
            "config": adapter.identity(self.config), "authorization_ref": adapter.identity(self.task_root / "authorization.json"),
            "expires_at": time.time() + 1800}
        self.manifest = self.task_root / "TASK.json"
        self.write_manifest()
        env = {"HOME": str(self.home), "CODEX_THREAD_ID": THREAD}
        self.env_patch = patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for name, value in (("__file__", str(self.fixture_script)), ("LEGACY_PATH", self.legacy_file),
                            ("LEGACY_SHA256", adapter.sha(self.legacy_file))):
            self.start_patch(patch.object(adapter, name, value))
        self.start_patch(patch.object(sys, "executable", str(self.source_python)))
        self.template = self.make_template()

        def imported_template(_path):
            # A real import supplies the original suite runner on every phase.
            self.template.tests = self.test_executions
            self.release.save = adapter.save
            if hasattr(self, "catalog_validator_template"):
                self.deploy.validate = self.catalog_validator_template
            return self.template

        self.import_mock = self.start_patch(patch.object(adapter, "_import_legacy", side_effect=imported_template))
        self.start_patch(patch.object(adapter, "NATIVE_CLI", self.native_cli))
        self.wire_execution = self.start_patch(patch.object(adapter.Adapter, "execute_paired_wire",
            side_effect=self.execute_fixture_wire))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def write_manifest(self):
        # Fixture producers may rewrite their input before a run; adapter evidence never does.
        self.manifest.write_text(json.dumps(self.data, sort_keys=True))
        self.manifest_sha = adapter.sha(self.manifest)

    def load(self):
        return adapter.Task.load(str(self.manifest), self.manifest_sha)

    def make_template(self):
        legacy = types.ModuleType("mocked_delivery")
        legacy.tests = Mock(side_effect=lambda *args: self.execute_fixture_tests(legacy, *args))
        self.test_executions = legacy.tests
        release = types.SimpleNamespace(save=adapter.save)
        release.stage = Mock(side_effect=lambda: self.stage_fixture(legacy, release))
        installer = types.SimpleNamespace(__file__=str(self.installer_file), LOCK=self.lock)
        deploy = types.SimpleNamespace(installer=installer, validate=Mock(), main=Mock(return_value=None))
        self.release, self.deploy = release, deploy
        legacy.load = Mock(side_effect=lambda name, path: release if name == "release" else deploy)

        def recipes():
            package = legacy.load("release", legacy.RECIPE / "release.py")
            original_save = package.save

            def historical_save(path, value):
                if Path(path).name == "caller-binding.json":
                    value["default_profile"]["model"] = "historical-forced-model"
                original_save(path, value)

            package.save = historical_save
            package.NEW, package.VERSION, package.BUILD_ID = legacy.NEW, "0.3.69.dev0", "historical-build"
            package.MODULES, package.RUNTIME_BINS, package.RUST_CHECKS = legacy.MODULES, (), ()
            package.tests = legacy.tests
            deployment = legacy.load("concurrency_deploy", legacy.RECIPE / "deploy.py")
            deployment.OUT, deployment.ACTION, deployment.THREAD = legacy.HERE / "cutover", legacy.ACTION, legacy.THREAD
            deployment.tests = legacy.tests
            return package, deployment

        legacy.recipes = recipes
        return legacy

    def execute_fixture_tests(self, legacy, label, package_root, test_root=None):
        # Match the pinned helper's path override, not just the adapter's env.
        runtime_artifacts = adapter.document(legacy.OLD / "runtime-image.json")["artifacts"]
        log = legacy.HERE / (label + ".log")
        with log.open("xb") as stream:
            stream.write(b"mocked passed candidate suite\n")
        adapter.save(legacy.HERE / (label + ".json"), {
            "exit_code": 0, "log": str(log), "sha256": adapter.sha(log), "source_hashes": legacy.hashes(),
            "modules": {name: adapter.sha(package_root / adapter.PACKAGE / name) for name in legacy.MODULES},
            "interpreter": str((self.source_python if label == "source-tests" else self.candidate_python).resolve()),
            "runtime_image": os.environ["NOKIY_RUNTIME_EVIDENCE_HANDOFF_IMAGE"],
            "runtime_router": runtime_artifacts["tura_router"]["path"],
            "runtime_session_db": runtime_artifacts["tura_session_db"]["path"],
            "provider_calls": 0})

    def execute_fixture_wire(self, binding_path, receipt_path, log):
        """Hermetic publisher evidence only; real producer/consumer is parent-native."""
        self.assertTrue(self.new.is_dir())
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        bound = adapter.document(binding_path)
        self.assertEqual(bound["package_root"], str(self.new / adapter.SITE))
        self.assertEqual(bound["runtime_image"], adapter.identity(self.new / "runtime-image.json"))
        root = receipt_path.parent / "paired-wire-output"
        root.mkdir()
        streams = {}
        for name in ("stdout", "stderr"):
            path = root / name
            path.write_bytes((name + " fixture\n").encode())
            streams[name] = {**adapter.identity(path), "bytes": path.stat().st_size,
                             "complete": True, "durable": True}
        log.write_text("hermetic paired wire execution fixture\n")
        adapter.save(receipt_path, {"binding": adapter.identity(binding_path), "test": adapter.PAIRED_WIRE_TEST,
            "passed": True, "execution_reused": False,
            "result": {"success": True, "exit_code": 0, "outcome": "known", "process_reaped": True,
                       "process_group_empty": True, "verifier_output": {"streams": streams}}})
        return 0

    def test_paired_gate_is_mandatory_after_stage_before_plan_for_both_runtime_modes(self):
        self.enable_runtime_update()
        self.wire_execution.side_effect = lambda *_: 1
        self.assert_rejected("PAIRED_WIRE_FAILED")
        self.release.stage.assert_called_once()
        self.deploy.main.assert_not_called()
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        self.assertEqual(self.entry.resolve(), self.old / "caller/bin/nokiy-embedded-run")

    def test_paired_gate_caller_only_cannot_pass_by_skipping(self):
        self.wire_execution.side_effect = lambda *_: 0
        self.assert_rejected("PAIRED_WIRE_EXECUTION_REQUIRED")
        self.deploy.main.assert_not_called()
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())

    def test_paired_gate_rejects_stale_preexisting_receipt_without_execution(self):
        original_stage = self.release.stage.side_effect
        def stage():
            original_stage()
            adapter.save(self.task_root / "deployment-evidence/paired-wire.json", {"passed": True})
        self.release.stage.side_effect = stage
        self.assert_rejected("PAIRED_WIRE_EVIDENCE_EXISTS")
        self.wire_execution.assert_not_called()

    def test_paired_gate_rejects_swapped_receipt_binding(self):
        def execute(binding, receipt, log):
            self.execute_fixture_wire(binding, receipt, log)
            value = adapter.document(receipt)
            value["binding"]["sha256"] = "0" * 64
            receipt.write_text(json.dumps(value))
            return 0
        self.wire_execution.side_effect = execute
        self.assert_rejected("PAIRED_WIRE_RECEIPT_INVALID")

    def test_paired_gate_rejects_reused_execution(self):
        def execute(binding, receipt, log):
            self.execute_fixture_wire(binding, receipt, log)
            value = adapter.document(receipt)
            value["execution_reused"] = True
            receipt.write_text(json.dumps(value))
            return 0
        self.wire_execution.side_effect = execute
        self.assert_rejected("PAIRED_WIRE_RECEIPT_INVALID")

    def test_paired_gate_binds_candidate_caller_and_runtime_before_preflight_and_apply(self):
        self.prepare()
        task = self.load()
        self.approve_environment(task)
        instance = adapter.Adapter(task)
        bound = instance.paired_wire_binding()
        self.assertEqual(bound["package"], adapter.package_bytes(self.new / adapter.SITE / adapter.PACKAGE))
        self.assertEqual(bound["candidate"], self.data["candidate"])
        self.assertEqual(bound["runtime_artifacts"]["tura_router"],
                         adapter.identity(Path(adapter.document(self.new / "runtime-image.json")["artifacts"]["tura_router"]["path"])))
        for key in ("package", "runtime_artifacts"):
            with self.subTest(binding=key), patch.object(instance, "paired_wire_binding", return_value={**bound, key: {}}):
                for phase in ("preflight", "apply"):
                    self.assert_rejected("PAIRED_WIRE_BINDING_DRIFT", lambda: instance.phase(phase))
                    self.assertFalse((task.out / ("adapter-" + phase + ".started.json")).exists())
        self.deploy.main.assert_not_called()
        self.assertEqual(self.entry.resolve(), self.old / "caller/bin/nokiy-embedded-run")

    def test_paired_gate_pins_complete_output_and_checks_drift_before_installer(self):
        result = self.prepare()
        task = self.load()
        wire = result["preparation_evidence"]["paired_wire"]
        pins = adapter.document(task.out / "pins.json")
        for ref in [wire["binding"], wire["receipt"], wire["log"], *wire["outputs"]]:
            self.assertEqual(pins[ref["path"]], ref["sha256"])
        Path(wire["outputs"][0]["path"]).write_text("swapped output")
        self.approve_environment(task)
        self.assert_rejected("PIN_DRIFT", lambda: adapter.run(self.load(), "preflight"))
        self.deploy.main.assert_not_called()

    def test_paired_gate_fixed_native_invocation_scrubs_credentials_and_cannot_select_model(self):
        self.prepare()
        task = self.load()
        instance = adapter.Adapter(task)
        binding = task.here / "paired-wire-binding.json"
        receipt, log = task.here / "unexecuted-wire.json", task.here / "unexecuted-wire.log"
        for codex_home in (None, "", str(self.root / "configured-codex-home")):
            with self.subTest(codex_home=codex_home):
                ambient = {**os.environ, "OPENAI_API_KEY": "must-not-inherit", "NOKIY_SKIP_TESTS": "1",
                           "NOKIY_VERIFIER_TEST_ROUTER": "/swapped-router"}
                if codex_home is None:
                    ambient.pop("CODEX_HOME", None)
                else:
                    ambient["CODEX_HOME"] = codex_home
                with patch.dict(os.environ, ambient, clear=True), \
                        patch.object(instance, "paired_wire_binding", return_value=adapter.document(binding)), \
                        patch.object(adapter.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
                    self.assertEqual(EXECUTE_PAIRED_WIRE(instance, binding, receipt, log), 0)
                argv = run.call_args.args[0]
                kwargs = run.call_args.kwargs
                self.assertEqual(argv, [str(self.candidate_python),
                    "-B", "-m", "unittest", adapter.PAIRED_WIRE_TEST, "-v"])
                self.assertEqual(kwargs["env"]["PYTHONPATH"], str(self.new / adapter.SITE) + os.pathsep + str(self.repo / "tests"))
                self.assertEqual(kwargs["env"]["NOKIY_NATIVE_SANDBOX_TEST_BIN"], str(self.native_cli))
                self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                self.assertNotIn("NOKIY_SKIP_TESTS", kwargs["env"])
                self.assertEqual(kwargs["env"]["HOME"], ambient["HOME"])
                if codex_home is None:
                    self.assertNotIn("CODEX_HOME", kwargs["env"])
                else:
                    self.assertEqual(kwargs["env"]["CODEX_HOME"], codex_home)
                scratch = task.here / "paired-wire-scratch"
                self.assertEqual(kwargs["env"]["TMPDIR"], str(scratch))
                self.assertEqual(kwargs["env"]["NOKIY_TEST_ROOT"], str(scratch))
                self.assertFalse((task.here / "paired-wire-home").exists())
                self.assertFalse((self.root / "configured-codex-home").exists())
                self.assertEqual(kwargs["env"]["NOKIY_VERIFIER_TEST_ROUTER"],
                                 adapter.document(binding)["runtime_artifacts"]["tura_router"]["path"])
                self.assertNotIn("/usr/bin/sandbox-exec", argv)
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                scratch.rmdir()
                log.unlink()

    def test_paired_gate_rechecks_binding_before_native_unittest_dispatch(self):
        self.prepare()
        task = self.load()
        binding = task.here / "paired-wire-binding.json"
        bound = adapter.document(binding)
        bound["test"] = "untrusted_test.test_model"
        binding.write_text(json.dumps(bound), encoding="utf-8")
        with patch.object(adapter.subprocess, "run") as run:
            self.assert_rejected("PAIRED_WIRE_BINDING_DRIFT", lambda: EXECUTE_PAIRED_WIRE(
                adapter.Adapter(task), binding, task.here / "unexecuted-wire.json",
                task.here / "unexecuted-wire.log"))
        run.assert_not_called()
        self.assertFalse((task.here / "paired-wire-scratch").exists())
        self.assertFalse((task.here / "unexecuted-wire.log").exists())

    def test_paired_services_compose_base_confinement_runtime_support_and_loopback_once(self):
        import test_verifier_parent as wire

        (self.source_python.parent.parent / "lib").mkdir()
        scratch = self.root / "service-scratch"
        scratch.mkdir()
        is_file = Path.is_file
        with patch.object(wire.graph_entry.sys, "platform", "darwin"), \
                patch.object(Path, "is_file", autospec=True, side_effect=lambda path:
                    path == Path("/usr/bin/sandbox-exec") or is_file(path)):
            argv = wire._paired_service_prefix(self.native_cli, self.repo, scratch,
                                               ["tests/test_verifier_parent.py"])
        self.assertEqual(argv[:2], ["/usr/bin/sandbox-exec", "-p"])
        self.assertEqual(argv[3:], [str(self.native_cli)])
        self.assertEqual(argv.count("/usr/bin/sandbox-exec"), 1)
        profile = argv[2]
        self.assertIn("(deny default)", profile)
        self.assertNotIn("(allow default)", profile)
        self.assertIn(f'(literal {json.dumps(str(self.repo / "tests/test_verifier_parent.py"))})', profile)
        self.assertIn(f'(allow file-write* (subpath {json.dumps(str(scratch))}) '
                      '(literal "/dev/null"))', profile)
        self.assertIn(f'(deny file-write-unlink (subpath {json.dumps(str(self.repo))}))', profile)
        self.assertIn('(deny file-read* file-write* (regex #"/auth[.]json$"))', profile)
        self.assertIn("(allow process-info-listpids)", profile)
        self.assertIn("(deny network*)", profile)
        self.assertIn('(allow network-inbound (local ip "localhost:*"))', profile)
        self.assertIn('(allow network-outbound (remote ip "localhost:*"))', profile)
        self.assertNotIn("unix-socket", profile)

    def stage_fixture(self, legacy, release):
        postimages = adapter.document(legacy.HERE / "adoption.json")["postimages"]
        for path, expected in postimages.items():
            self.assertEqual(adapter.sha(Path(path)), expected)
        build = adapter.document(legacy.HERE / "rust-build.json")
        self.assertEqual(build["source_hashes"], postimages)
        self.assertEqual(adapter.sha(Path(build["log"])), build["sha256"])
        catalogs = build.get("catalog_updates", {})
        staging = self.new.with_name("." + self.new.name + ".staging") if catalogs else self.new
        shutil.copytree(self.old, staging)
        if release.RUNTIME_BINS:
            self.assertEqual(release.RUNTIME_BINS, adapter.RUNTIME_BINS)
            self.assertEqual(self.deploy.RUNTIME_BINS, release.RUNTIME_BINS)
            self.assertEqual(release.CACHE, self.installer_file.parent)
            image = adapter.document(staging / "runtime-image.json")
            for name in release.RUNTIME_BINS:
                compiled = release.CACHE / "target/release" / name
                self.assertEqual(adapter.sha(compiled), build["binary_hashes"][name])
                target = staging / "runtime" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(compiled, target)
                image["artifacts"][name] = {**adapter.identity(target),
                                            "path": str(self.new / target.relative_to(staging))}
            if catalogs:
                for name in catalogs:
                    old = adapter.document(self.old / "runtime-image.json")["artifacts"][name]
                    target = staging / Path(old["path"]).relative_to(self.old)
                    target.unlink()
                    os.link(old["path"], target)
                    image["artifacts"][name] = {**old, "path": str(self.new / target.relative_to(staging))}
                self.assertFalse(self.new.exists())
                (staging / "runtime-image.json").unlink()
                release.save(staging / "runtime-image.json", image)
                self.assertFalse(self.new.exists())
                for name, pin in catalogs.items():
                    target = staging / Path(image["artifacts"][name]["path"]).relative_to(self.new)
                    self.assertEqual(adapter.sha(target), pin["sha256"])
                    self.assertNotEqual(target.stat().st_ino,
                                        Path(adapter.document(self.old / "runtime-image.json")["artifacts"][name]["path"]).stat().st_ino)
                release.save(staging / "release-provenance.json", {
                    "changed_runtime_artifacts": list(release.RUNTIME_BINS)})
                binding = adapter.document(staging / "caller-binding.json")
                binding["runtime_image"] = {"path": str(self.new / "runtime-image.json"),
                                            "sha256": adapter.sha(staging / "runtime-image.json")}
                (staging / "caller-binding.json").write_text(json.dumps(binding, sort_keys=True))
                staging.rename(self.new)
            else:
                (self.new / "runtime-image.json").write_text(json.dumps(image, sort_keys=True))
        for name in legacy.MODULES:
            target = self.new / adapter.SITE / adapter.PACKAGE / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.source / name, target)
        # Exercise the original save API without overwriting the copied fixture binding.
        probe = self.root / "binding-probe/caller-binding.json"
        probe.parent.mkdir()
        release.save(probe, {"default_profile": {"model": "existing-model", "effort": "high"}})
        release.tests("staged-tests", self.new / adapter.SITE, legacy.HERE / "validation-tests")

    def enable_runtime_update(self, *, caller_modules=False):
        """Byte/receipt fixtures only: no cargo execution or real Rust acceptance."""
        self.data["modules"] = list(self.modules) if caller_modules else []
        inputs = [f"src/{adapter.PACKAGE}/{name}" for name in self.data["modules"]]
        inputs += [f"tests/{name}.py" for name in self.tests] + list(adapter.GUIDANCE)
        self.data["postimages"] = {name: adapter.sha(self.repo / name) for name in inputs}
        self.rust_source = self.repo / "rust/src/runtime.rs"
        self.rust_lock = self.repo / "rust/Cargo.lock"
        self.transitive_source = self.root / "build-inputs/shared.rs"
        for path in (self.rust_source, self.rust_lock, self.transitive_source):
            self.write(path, "// fixture build dependency\n")
        self.compiled = {name: self.installer_file.parent / "target/release" / name for name in adapter.RUNTIME_BINS}
        for name, path in self.compiled.items():
            self.write(path, name + " new runtime byte fixture\n")
            path.chmod(0o755)
        self.build_file, self.build_log = self.task_root / "parent-rust-build.json", self.task_root / "parent-rust-build.log"
        self.write(self.build_log, "mocked build receipt; no compiler executed\n")
        self.build = {"argv": ["cargo", "build", "--release"] +
                      [arg for name in adapter.RUNTIME_BINS for arg in ("--bin", name)],
                      "exit_code": 0, "log": str(self.build_log), "sha256": adapter.sha(self.build_log), "seconds": 0,
                      "source_hashes": {str(path): adapter.sha(path)
                                        for path in (self.rust_source, self.rust_lock, self.transitive_source)},
                      "external_model_calls": 0,
                      "binary_hashes": {name: adapter.sha(path) for name, path in self.compiled.items()}}
        self.write_runtime_build()

    def write_runtime_build(self):
        self.build_file.write_text(json.dumps(self.build, sort_keys=True))
        self.data["runtime_build"] = adapter.identity(self.build_file)
        self.write_manifest()

    def enable_catalog_update(self, *, prompt=False):
        self.enable_runtime_update()
        suffix = ("crates/tools/src/commands/task_status/prompt.md" if prompt
                  else "crates/tools/src/command_run/schema.json")
        self.old_catalog = self.old / "runtime" / suffix
        self.catalog_source = self.repo / "rust" / suffix
        self.write(self.old_catalog, '{"name":"command_run","input_schema":{"type":"object"}}\n')
        self.write(self.catalog_source, '{"name":"command_run","input_schema":{"type":"object","properties":{"arguments":{"type":"object"}}}}\n')
        if prompt:
            self.old_catalog.write_text("Old completion guidance.\n")
            self.catalog_source.write_text("Complete assigned work; parent-owned checks remain pending.\n")
        image = adapter.document(self.old / "runtime-image.json")
        image["artifacts"]["command_catalog_0"] = {**adapter.identity(self.old_catalog),
                                                   "size": self.old_catalog.stat().st_size}
        (self.old / "runtime-image.json").write_text(json.dumps(image, sort_keys=True))
        self.data["installed"]["metadata"]["runtime-image.json"] = adapter.sha(self.old / "runtime-image.json")
        self.build["source_hashes"][str(self.catalog_source)] = adapter.sha(self.catalog_source)
        self.build["catalog_updates"] = {"command_catalog_0": {
            **adapter.identity(self.catalog_source), "previous_sha256": adapter.sha(self.old_catalog)}}
        self.write(self.deploy_file, '''def validate():
    old_image = json.loads((OLD / "runtime-image.json").read_text())
    image = json.loads((NEW / "runtime-image.json").read_text())
    build = json.loads((HERE / "rust-build.json").read_text())
    assert binding_guard()
    for name, entry in image["artifacts"].items():
        expected = build["binary_hashes"][name] if name in RUNTIME_BINS else old_image["artifacts"][name]["sha256"]
        assert entry["sha256"] == expected
        assert sha(Path(entry["path"])) == expected
    assert final_guard()
''')
        self.validator_binding_guard = Mock(return_value=True)
        self.validator_final_guard = Mock(return_value=True)
        namespace = dict(json=json, Path=Path, OLD=self.old, NEW=self.new,
                         HERE=self.task_root / "deployment-evidence", RUNTIME_BINS=adapter.RUNTIME_BINS,
                         sha=adapter.sha, binding_guard=self.validator_binding_guard,
                         final_guard=self.validator_final_guard)
        exec(compile(self.deploy_file.read_text(), str(self.deploy_file), "exec"), namespace)
        self.catalog_validator_template = self.deploy.validate = namespace["validate"]
        self.data["templates"]["deploy"] = adapter.identity(self.deploy_file)
        self.write_runtime_build()

    def prepare(self):
        return adapter.run(self.load(), "prepare")

    def assert_rejected(self, code, function=None):
        with self.assertRaisesRegex(adapter.PreparationError, code):
            (function or self.prepare)()

    def approve_environment(self, task):
        plan = adapter.document(task.out / "plan.json")
        os.environ.update(NOKIY_DEPLOYMENT_ACTION_ID=self.data["action_id"], NOKIY_DEPLOYMENT_PLAN_SHA256=adapter.canonical(plan))

    def pin_source_interpreter_symlink(self):
        target = self.root / "source-python-real"
        self.source_python.rename(target)
        self.source_python.symlink_to(target)
        for name in ("controller", "source"):
            self.data["interpreters"][name] = adapter.identity(self.source_python)
        self.write_manifest()
        return target

    def pin_controller_source_without_project_environment(self):
        target = self.root / "source-runtime/bin/python"
        target.parent.mkdir(parents=True)
        self.source_python.rename(target)
        shutil.rmtree(self.repo / ".venv")
        self.source_python = target
        self.start_patch(patch.object(sys, "executable", str(target)))
        for name in ("controller", "source"):
            self.data["interpreters"][name] = adapter.identity(target)
        self.write_manifest()
        return target

    def test_absent_project_environment_uses_only_pinned_current_controller(self):
        target = self.pin_controller_source_without_project_environment()
        task = self.load()
        self.assertEqual(task.data["interpreters"]["source"], adapter.identity(target))
        self.assertEqual(task.hashes()[str(target)], adapter.sha(target))
        self.assertFalse(os.path.lexists(self.repo / ".venv"))

    def test_absent_project_environment_rejects_arbitrary_source_interpreter(self):
        self.pin_controller_source_without_project_environment()
        alternate = self.root / "unrequested-python"
        self.write(alternate, "another interpreter\n")
        self.data["interpreters"]["source"] = adapter.identity(alternate)
        self.write_manifest()
        self.assert_rejected("INTERPRETER_IDENTITY_INVALID: source", self.load)
        self.import_mock.assert_not_called()

    def test_existing_project_environment_does_not_use_controller_fallback(self):
        controller = self.root / "controller-python"
        self.write(controller, "controller interpreter\n")
        self.start_patch(patch.object(sys, "executable", str(controller)))
        self.data["interpreters"]["controller"] = adapter.identity(controller)
        self.write_manifest()
        self.load()
        self.data["interpreters"]["source"] = adapter.identity(controller)
        self.write_manifest()
        self.assert_rejected("INTERPRETER_IDENTITY_INVALID: source", self.load)

    def test_incomplete_project_environment_does_not_use_controller_fallback(self):
        self.pin_controller_source_without_project_environment()
        (self.repo / ".venv").mkdir()
        self.assert_rejected("INTERPRETER_IDENTITY_INVALID: source", self.load)
        self.import_mock.assert_not_called()

    def test_broken_project_environment_symlink_does_not_use_controller_fallback(self):
        self.pin_controller_source_without_project_environment()
        (self.repo / ".venv").symlink_to(self.root / "missing-project-environment")
        self.assert_rejected("INTERPRETER_IDENTITY_INVALID: source", self.load)
        self.import_mock.assert_not_called()

    def test_hashes_rechecks_project_environment_selection_after_load(self):
        self.pin_controller_source_without_project_environment()
        task = self.load()
        (self.repo / ".venv").mkdir()
        self.assert_rejected("INTERPRETER_IDENTITY_INVALID: source", task.hashes)

    def test_absent_project_environment_interpreter_content_drift_fails(self):
        target = self.pin_controller_source_without_project_environment()
        task = self.load()
        self.write(target, "changed controller interpreter\n")
        self.assert_rejected("INPUT_DRIFT", task.hashes)

    def test_absent_project_environment_same_content_retarget_fails(self):
        target = self.pin_controller_source_without_project_environment()
        real = target.with_name("python-real")
        target.rename(real)
        target.symlink_to(real)
        for name in ("controller", "source"):
            self.data["interpreters"][name] = adapter.identity(target)
        self.write_manifest()
        task = self.load()
        alternate = target.with_name("python-alternate")
        self.write(alternate, real.read_text())
        target.unlink()
        target.symlink_to(alternate)
        self.assert_rejected("RESOLVED_IDENTITY_REQUIRED", task.hashes)

    def test_absent_project_environment_fingerprints_selected_interpreter_config(self):
        target = self.pin_controller_source_without_project_environment()
        config = target.parent.parent / "pyvenv.cfg"
        self.write(config, "home = pinned-runtime\n")
        task = self.load()
        before = task.hashes()
        self.assertEqual(before[str(config)], adapter.sha(config))
        self.write(config, "home = changed-runtime\n")
        self.assertNotEqual(task.hashes(), before)

    def test_absent_project_environment_disposable_transaction_preserves_phase_guards(self):
        self.pin_controller_source_without_project_environment()
        self.use_transaction_fixture()
        self.prepare()
        task = self.load()
        self.assert_rejected("PARENT_PLAN_BINDING_REQUIRED", lambda: adapter.run(self.load(), "preflight"))
        self.approve_environment(task)
        for phase in adapter.PHASES:
            adapter.run(self.load(), phase)
        self.assertEqual(self.phase_executions, list(adapter.PHASES))
        self.assertEqual(self.entry.resolve(), self.new / "caller/bin/nokiy-embedded-run")
        self.assertEqual([call.args[0] for call in self.test_executions.call_args_list],
                         ["source-tests", "candidate-tests"])

    def test_pinned_legacy_runner_uses_manifest_source_without_project_environment(self):
        source = self.pin_controller_source_without_project_environment()
        self.data["templates"]["legacy"] = adapter.identity(PINNED_LEGACY)
        self.write_manifest()

        def imported_template(path):
            legacy = IMPORT_LEGACY(path)
            legacy.load = self.template.load
            return legacy

        self.import_mock.side_effect = imported_template
        with patch.object(adapter, "LEGACY_PATH", PINNED_LEGACY), \
                patch.object(adapter, "LEGACY_SHA256", PINNED_LEGACY_SHA256), patch.dict(sys.modules):
            task = self.load()
            task.here.mkdir()
            candidate = task.here / "validation-package"
            shutil.copytree(self.source, candidate / adapter.PACKAGE)
            prepared = adapter.Adapter(task)
            self.assertEqual(prepared.executed_tests.__code__.co_filename, str(PINNED_LEGACY))
            expected = [("source-tests", self.repo / "src", source),
                        ("candidate-tests", candidate, self.candidate_python)]

            def execute(argv, **kwargs):
                label, package_root, interpreter = expected[runner.call_count - 1]
                self.assertEqual(argv, [str(interpreter), "-B", "-m", "unittest", *self.tests, "-v"])
                self.assertEqual(kwargs["cwd"], self.repo)
                self.assertEqual(kwargs["env"]["PYTHONPATH"],
                                 str(package_root) + os.pathsep + str(self.repo / "tests"))
                self.assertEqual(kwargs["timeout"], 120)
                kwargs["stdout"].write((label + " passed\n").encode())
                return types.SimpleNamespace(returncode=0)

            with patch.object(prepared.legacy.subprocess, "run", side_effect=execute) as runner:
                for label, package_root, interpreter in expected:
                    prepared.tests(label, package_root)
                    receipt = adapter.document(task.here / (label + ".json"))
                    binding = adapter.document(task.here / (label + "-binding.json"))
                    self.assertEqual(receipt["interpreter"], str(interpreter.resolve()))
                    self.assertEqual(receipt["source_hashes"], task.hashes())
                    self.assertEqual(receipt["sha256"], adapter.sha(Path(receipt["log"])))
                    self.assertEqual(binding["interpreter"], adapter.identity(interpreter))
                self.assertEqual(runner.call_count, 2)
            self.test_executions.assert_not_called()
            self.assertFalse(os.path.lexists(self.repo / ".venv"))

    def use_transaction_fixture(self, *, help_exit_code=0):
        """Concrete disposable deploy.main; import/stage and suite runners remain fixtures.

        Interpreter files are byte fixtures, source/candidate receipts are stubbed,
        and this tiny executable stands in for the CLI: no package build/install.
        Adapter.phase, check_pins and tests are all unmodified.
        """
        legacy, deploy = self.template, self.deploy
        self.phase_executions, self.live_test_calls = [], []
        executable = self.old / "caller/bin/nokiy-embedded-run"
        self.write(executable, "#!/bin/sh\n[ \"$1\" = \"--help\" ] || exit 2\n"
                   "printf 'fixture caller help\\n'\n" + f"exit {help_exit_code}\n")
        executable.chmod(0o755)
        self.data["installed"]["entry"] = adapter.identity(self.entry)
        self.write_manifest()

        def validate():
            for path, expected in adapter.document(deploy.OUT / "pins.json").items():
                self.assertEqual(adapter.sha(Path(path)), expected)

        def main(phase):
            self.phase_executions.append(phase)
            plan = adapter.document(deploy.OUT / "plan.json")
            self.assertEqual(os.environ["NOKIY_DEPLOYMENT_PLAN_SHA256"], adapter.canonical(plan))
            self.assertEqual((plan["action_id"], plan["native_thread_id"]), (deploy.ACTION, deploy.THREAD))
            self.assertEqual(os.environ["NOKIY_DEPLOYMENT_ACTION_ID"], deploy.ACTION)
            self.assertEqual(os.environ["CODEX_THREAD_ID"], deploy.THREAD)
            temporary = None
            try:
                with deploy.installer.LOCK.open("rb") as lock:
                    info = os.fstat(lock.fileno())
                    self.assertEqual(
                        {"dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid,
                         "mode": info.st_mode & 0o7777}, adapter.document(deploy.OUT / "lock-identity.json"))
                    deploy.validate()
                    old_entry, new_entry = (root / "caller/bin/nokiy-embedded-run" for root in (legacy.OLD, legacy.NEW))
                    self.assertEqual(self.entry.resolve(strict=True), new_entry if phase == "verify" else old_entry)
                    if phase == "preflight":
                        adapter.save(deploy.OUT / "fixture-preflight.json", {"status": "admitted", "entry": str(old_entry)})
                        return 0
                    if phase == "apply":
                        temporary = self.entry.with_name(self.entry.name + ".fixture-next")
                        self.assertFalse(os.path.lexists(temporary))
                        temporary.symlink_to(new_entry)
                        os.replace(temporary, self.entry)
                        directory_fd = os.open(self.entry.parent, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                        self.assertEqual(self.entry.resolve(strict=True), new_entry)
                        adapter.save(deploy.OUT / "fixture-apply.json", {"status": "LINK_ADOPTED", "entry": str(new_entry)})
                        return 0
                    self.assertEqual(phase, "verify")
                    marker = legacy.HERE / "validation-tests"
                    self.live_test_calls.append(("live-tests", legacy.NEW / legacy.SITE, marker, self.entry.resolve()))
                    deploy.tests("live-tests", legacy.NEW / legacy.SITE, marker)
                    completed = subprocess.run([str(self.entry), "--help"], capture_output=True, timeout=10)
                    with (deploy.OUT / "entry-help.log").open("xb") as stream:
                        stream.write(completed.stdout + completed.stderr)
                    self.assertEqual(
                        adapter.document(legacy.NEW / "caller-binding.json")["default_profile"],
                        adapter.document(legacy.OLD / "caller-binding.json")["default_profile"])
                    adapter.save(deploy.OUT / "fixture-verify.json", {
                        "status": "verified" if completed.returncode == 0 else "failed",
                        "healthy": completed.returncode == 0,
                        "entry": str(new_entry), "help_exit_code": completed.returncode})
                    return completed.returncode
            finally:
                if temporary is not None and temporary.is_symlink():
                    temporary.unlink()

        deploy.validate, deploy.main = validate, main

    def test_pinned_symlink_interpreter_whole_release_transaction(self):
        target = self.pin_source_interpreter_symlink()
        self.use_transaction_fixture()
        old_entry, new_entry = (root / "caller/bin/nokiy-embedded-run" for root in (self.old, self.new))
        prepared = self.prepare()
        task = self.load()
        self.assertEqual(self.phase_executions, [])
        self.assertEqual(self.entry.resolve(), old_entry)
        plan_sha = adapter.canonical(adapter.document(task.out / "plan.json"))
        self.assertEqual(prepared["sha256"], plan_sha)
        for name in ("controller", "source"):
            self.assertEqual(self.data["interpreters"][name]["realpath"], str(target))
        pins = adapter.document(task.out / "pins.json")
        for path in (self.source_python, target):
            self.assertEqual(pins[str(path)], adapter.sha(target))
        # Reverting check_pins to absolute(path) must fail the real preflight below.
        self.assert_rejected("CANONICAL_PATH_REQUIRED", lambda: adapter.absolute(str(self.source_python)))
        self.approve_environment(task)
        for phase in adapter.PHASES:
            with self.subTest(phase=phase):
                self.assertIsNone(adapter.run(self.load(), phase))
                self.assertTrue(self.entry.is_symlink())
                self.assertEqual(self.entry.resolve(), old_entry if phase == "preflight" else new_entry)
                started = adapter.document(task.out / ("adapter-" + phase + ".started.json"))
                self.assertEqual(started, {"manifest_sha256": self.manifest_sha, "plan_sha256": plan_sha})
                self.assertEqual(adapter.document(task.out / ("adapter-" + phase + ".completed.json")),
                                 {"plan_sha256": plan_sha})
        self.assertEqual(self.phase_executions, list(adapter.PHASES))
        self.assertNotIsInstance(self.deploy.main, Mock)
        self.assertIs(self.deploy.tests.__func__, adapter.Adapter.tests)
        self.assertEqual(adapter.document(task.out / "fixture-preflight.json")["status"], "admitted")
        self.assertEqual(adapter.document(task.out / "fixture-apply.json")["status"], "LINK_ADOPTED")
        verified = adapter.document(task.out / "fixture-verify.json")
        self.assertEqual(verified["status"], "verified")
        self.assertTrue(verified["healthy"])
        self.assertEqual(verified["help_exit_code"], 0)
        self.assertEqual((task.out / "entry-help.log").read_bytes(), b"fixture caller help\n")
        marker = task.here / "validation-tests"
        # Reverting the live marker allowance must fail verify, not just a direct tests() call.
        self.assertEqual(self.live_test_calls, [("live-tests", self.new / adapter.SITE, marker, new_entry)])
        self.assertFalse(marker.exists())
        live = adapter.document(task.here / "live-tests.json")
        self.assertTrue(live["execution_reused"])
        self.assertEqual(live["source_hashes"], task.hashes())
        self.assertEqual(live["verified_package_root"], str(self.new / adapter.SITE))
        self.assertEqual(live["reused_from"], {"path": str(task.here / "candidate-tests.json"),
                                             "sha256": adapter.sha(task.here / "candidate-tests.json")})
        self.assertEqual([call.args[0] for call in self.test_executions.call_args_list], ["source-tests", "candidate-tests"])
        self.assertFalse(os.path.lexists(self.entry.with_name(self.entry.name + ".fixture-next")))

    def test_failed_post_apply_verify_keeps_adoption_evidence_and_refuses_replay(self):
        self.pin_source_interpreter_symlink()
        self.use_transaction_fixture(help_exit_code=1)
        self.prepare()
        task = self.load()
        self.approve_environment(task)
        for phase in ("preflight", "apply"):
            adapter.run(self.load(), phase)
        adopted_entry = self.entry.resolve(strict=True)
        adopted_link = os.readlink(self.entry)
        self.assertEqual(adopted_entry, self.new / "caller/bin/nokiy-embedded-run")
        self.assert_rejected("INSTALLER_PHASE_FAILED", lambda: adapter.run(self.load(), "verify"))
        self.assertTrue(self.entry.is_symlink())
        self.assertEqual(self.entry.resolve(strict=True), adopted_entry)
        self.assertEqual(os.readlink(self.entry), adopted_link)
        for phase in ("preflight", "apply"):
            self.assertTrue((task.out / ("adapter-" + phase + ".completed.json")).is_file())
        self.assertEqual(adapter.document(task.out / "adapter-verify.started.json"), {
            "manifest_sha256": self.manifest_sha,
            "plan_sha256": adapter.canonical(adapter.document(task.out / "plan.json"))})
        self.assertFalse((task.out / "adapter-verify.completed.json").exists())
        failed = adapter.document(task.out / "fixture-verify.json")
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["healthy"])
        self.assertEqual(failed["help_exit_code"], 1)
        self.assertEqual(failed["entry"], str(adopted_entry))
        self.assertEqual(self.live_test_calls, [
            ("live-tests", self.new / adapter.SITE, task.here / "validation-tests", adopted_entry)])
        self.assertTrue(adapter.document(task.here / "live-tests.json")["execution_reused"])
        evidence = {str(path.relative_to(task.here)): path.read_bytes()
                    for path in task.here.rglob("*") if path.is_file()}
        self.assert_rejected("PHASE_ALREADY_ATTEMPTED", lambda: adapter.run(self.load(), "verify"))
        self.assert_rejected("INSTALLED_PREIMAGE_DRIFT", lambda: adapter.run(self.load(), "apply"))
        self.assertEqual(evidence, {str(path.relative_to(task.here)): path.read_bytes()
                                   for path in task.here.rglob("*") if path.is_file()})
        self.assertEqual(self.phase_executions, ["preflight", "apply", "verify"])
        self.assertEqual(self.entry.resolve(strict=True), adopted_entry)
        self.assertEqual(os.readlink(self.entry), adopted_link)
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse(os.path.lexists(self.entry.with_name(self.entry.name + ".fixture-next")))

    def test_manifest_sha_before_import_or_effect(self):
        self.assert_rejected("MANIFEST_SHA_MISMATCH", lambda: adapter.Task.load(str(self.manifest), "0" * 64))
        self.import_mock.assert_not_called()
        self.assertFalse((self.task_root / "deployment-evidence").exists())

    def test_thread_source_template_and_script_drift(self):
        os.environ["CODEX_THREAD_ID"] = "019fd4f0-1079-77e2-8be7-cbf75ca28df6"
        self.assert_rejected("THREAD_DRIFT")
        os.environ["CODEX_THREAD_ID"] = THREAD
        for path in (self.source / "changed.py", self.release_file, self.deploy_file, self.installer_file,
                     self.legacy_file, self.fixture_script):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"drift\n")
                self.assert_rejected("INPUT_DRIFT")
                path.write_bytes(original)
        self.import_mock.assert_not_called()

    def test_path_escape_and_invalid_modules(self):
        for name in ("../outside.py", "/outside.py", "protocol/../changed.py", "./changed.py", "x\\y.py", "x//y.py", ".git/config.py", "bad.txt"):
            with self.subTest(name=name):
                self.data["modules"] = [name]
                self.write_manifest()
                self.assert_rejected("RELATIVE_PATH_INVALID|MODULES_INVALID")
        self.import_mock.assert_not_called()

    def test_source_symlink_rejected(self):
        source = self.source / "changed.py"
        content = source.read_text()
        source.unlink()
        outside = self.root / "outside.py"
        outside.write_text(content)
        source.symlink_to(outside)
        self.assert_rejected("CANONICAL_PATH_REQUIRED")
        self.import_mock.assert_not_called()

    def test_prepare_single_plan_and_bound_argv_never_deploys(self):
        result = self.prepare()
        task = self.load()
        plan = adapter.document(Path(result["plan"]))
        self.assertEqual(result["sha256"], adapter.canonical(plan))
        self.assertEqual(plan["schema_version"], "nokiy_deployment_plan_v1")
        self.assertEqual(set(plan["commands"]), set(adapter.PHASES))
        for phase, command in plan["commands"].items():
            self.assertEqual(command["argv"], task.argv(phase))
            identities = {item["path"] for item in command["files"]}
            self.assertTrue({str(self.manifest), str(self.fixture_script), str(self.source_python),
                             *[pin["path"] for pin in self.data["templates"].values()]} <= identities)
        self.deploy.main.assert_not_called()
        self.deploy.validate.assert_called_once()
        self.release.stage.assert_called_once()
        self.assertEqual([call.args[0] for call in self.test_executions.call_args_list], ["source-tests", "candidate-tests"])
        self.assertEqual(self.entry.resolve(), self.old / "caller/bin/nokiy-embedded-run")
        self.assert_rejected("PARTIAL_PREPARATION_EXISTS")
        self.assertEqual(adapter.document(task.out / "plan.json"), plan)
        self.assertEqual(self.release.stage.call_count, 1)

    def assert_preparation_evidence(self, result, *, runtime=False):
        task = self.load()
        evidence = result["preparation_evidence"]
        self.assertEqual(set(result), {"plan", "sha256", "preparation_evidence"})
        self.assertEqual(evidence["status"], "PREPARED_NOT_DEPLOYED")
        self.assertIs(evidence["deployment_executed"], False)
        self.assertIs(evidence["parent_acceptance"], False)
        self.assertEqual((evidence["native_thread_id"], evidence["action_id"]), (THREAD, self.data["action_id"]))
        self.assertEqual(evidence["manifest"], {"path": str(self.manifest), "sha256": self.manifest_sha})
        self.assertEqual(evidence["preparation"], adapter.identity(task.here / "preparation.json"))
        self.assertEqual(evidence["plan"], adapter.identity(Path(result["plan"])))
        self.assertEqual(result["sha256"], adapter.canonical(adapter.document(Path(result["plan"]))))
        self.assertEqual(evidence["candidate"], {
            **self.data["candidate"], "runtime_image": adapter.identity(self.new / "runtime-image.json")})
        self.assertEqual(evidence["runtime_mode"], "runtime-build" if runtime else "caller-only")
        self.assertEqual(evidence["runtime_build"], self.data.get("runtime_build"))
        self.assertIs(evidence["adapter_executed_build"], False)
        labels = ("source-tests", "candidate-tests", "staged-tests")
        self.assertEqual(set(evidence["tests"]), set(labels))
        pins = adapter.document(task.out / "pins.json")
        for label in labels:
            row = evidence["tests"][label]
            reused = not runtime and label == "staged-tests"
            origin = "candidate-tests" if reused else label
            self.assertEqual(set(row), {"receipt", "log", "binding", "package_root", "exit_code",
                                        "execution_reused", "runtime_image"} | ({"reused_from"} if reused else set()))
            self.assertEqual(row["receipt"], adapter.identity(task.here / (label + ".json")))
            self.assertEqual(row["log"], adapter.identity(task.here / (origin + ".log")))
            self.assertEqual(row["binding"], adapter.identity(task.here / (origin + "-binding.json")))
            roots = {"source-tests": self.repo / "src", "candidate-tests": task.here / "validation-package",
                     "staged-tests": self.new / adapter.SITE}
            self.assertEqual(row["package_root"], str(roots[label]))
            image_root = self.new if runtime and label == "staged-tests" else self.old
            self.assertEqual(row["runtime_image"], adapter.identity(image_root / "runtime-image.json"))
            self.assertEqual(row["exit_code"], 0)
            self.assertIs(row["execution_reused"], reused)
            if reused:
                self.assertEqual(row["reused_from"], evidence["tests"]["candidate-tests"]["receipt"])
            for key in ("receipt", "log", "binding", "runtime_image"):
                ref = row[key]
                self.assertEqual(pins[ref["path"]], ref["sha256"])
        encoded = json.dumps(result)
        # Compactness must not depend on the host's temporary-directory depth.
        self.assertLess(compact_evidence_bytes(result, self.root), 8192)
        for key in ("input_hashes", "source_hashes", "runtime_artifacts", "provider_calls", "external_model_calls", "billing"):
            self.assertNotIn('"' + key + '"', encoded)

    def test_prepare_returns_compact_bound_evidence_with_caller_only_reuse(self):
        template_load = self.template.load
        with patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected summary subprocess")) as processes:
            result = self.prepare()
        self.assert_preparation_evidence(result)
        self.assertEqual([call.args[0] for call in self.test_executions.call_args_list], ["source-tests", "candidate-tests"])
        self.assertEqual([call.args[0] for call in template_load.call_args_list], ["release", "concurrency_deploy"])
        self.import_mock.assert_called_once()
        processes.assert_not_called()
        self.release.stage.assert_called_once()
        self.deploy.validate.assert_called_once()
        self.deploy.main.assert_not_called()
        self.assertEqual(self.entry.resolve(), self.old / "caller/bin/nokiy-embedded-run")

    def test_prepare_runtime_evidence_requires_executed_staged_tests(self):
        self.enable_runtime_update()
        original_build = self.build_file.read_bytes()
        with patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected build/model/summary subprocess")) as processes:
            result = self.prepare()
        self.assert_preparation_evidence(result, runtime=True)
        self.assertEqual([call.args[0] for call in self.test_executions.call_args_list],
                         ["source-tests", "candidate-tests", "staged-tests"])
        self.assertEqual(self.build_file.read_bytes(), original_build)
        processes.assert_not_called()
        self.import_mock.assert_called_once()
        self.release.stage.assert_called_once()
        self.deploy.validate.assert_called_once()
        self.deploy.main.assert_not_called()
        self.assertEqual(self.entry.resolve(), self.old / "caller/bin/nokiy-embedded-run")

    def assert_invalid_source_receipt(self, **changes):
        def corrupt_receipt(*args):
            self.execute_fixture_tests(self.template, *args)
            if args[0] == "source-tests":
                path = self.task_root / "deployment-evidence/source-tests.json"
                receipt = adapter.document(path)
                receipt.update(changes)
                path.write_text(json.dumps(receipt))

        self.test_executions.side_effect = corrupt_receipt
        self.assert_rejected("TEST_RECEIPT_INVALID")
        self.assertEqual(self.test_executions.call_count, 1)
        self.release.stage.assert_not_called()
        self.deploy.main.assert_not_called()
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())

    def test_prepare_evidence_rejects_failed_receipt(self):
        self.assert_invalid_source_receipt(exit_code=1)

    def test_prepare_evidence_rejects_noninteger_exit_code(self):
        self.assert_invalid_source_receipt(exit_code=False)

    def test_prepare_evidence_rejects_malformed_log_reference(self):
        self.assert_invalid_source_receipt(log=None)

    def test_prepare_evidence_rejects_other_manifest_receipt(self):
        other_hashes = self.load().hashes()
        other_hashes[str(self.manifest)] = "0" * 64
        self.assert_invalid_source_receipt(source_hashes=other_hashes)

    def test_prepare_evidence_rejects_executed_receipt_marked_reused(self):
        self.assert_invalid_source_receipt(execution_reused=True)

    def test_prepare_evidence_requires_caller_staged_phase(self):
        original_stage = self.release.stage.side_effect

        def skipped_tests():
            original_tests = self.release.tests
            self.release.tests = Mock()
            try:
                original_stage()
            finally:
                self.release.tests = original_tests

        self.release.stage.side_effect = skipped_tests
        self.assert_rejected("PREPARATION_TEST_EVIDENCE_REQUIRED")
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        self.deploy.main.assert_not_called()

    def test_prepare_evidence_rejects_missing_previously_checked_receipt(self):
        original_stage = self.release.stage.side_effect

        def missing_receipt():
            original_stage()
            (self.task_root / "deployment-evidence/source-tests.json").unlink()

        self.release.stage.side_effect = missing_receipt
        self.assert_rejected("PREPARATION_EVIDENCE_DRIFT")
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        self.deploy.main.assert_not_called()

    def test_prepare_evidence_rejects_receipt_drift_after_validation(self):
        original_stage = self.release.stage.side_effect

        def drifted_receipt():
            original_stage()
            path = self.task_root / "deployment-evidence/source-tests.json"
            receipt = adapter.document(path)
            receipt["exit_code"] = 1
            path.write_text(json.dumps(receipt))

        self.release.stage.side_effect = drifted_receipt
        self.assert_rejected("PREPARATION_EVIDENCE_DRIFT")
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        self.deploy.main.assert_not_called()

    def test_prepare_evidence_rejects_log_drift_after_pins(self):
        def drifted_log():
            (self.task_root / "deployment-evidence/candidate-tests.log").write_text("changed after pins\n")

        self.deploy.validate.side_effect = drifted_log
        self.assert_rejected("INPUT_DRIFT")
        self.assertEqual(self.test_executions.call_count, 2)
        self.deploy.validate.assert_called_once()
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        self.deploy.main.assert_not_called()

    def test_prepare_with_empty_optional_runtime_dependencies(self):
        self.data["runtime_dependencies"] = {"files": [], "trees": []}
        self.write_manifest()
        result = self.prepare()
        plan = adapter.document(Path(result["plan"]))
        self.assertEqual(plan["schema_version"], "nokiy_deployment_plan_v1")
        self.assertEqual(result["sha256"], adapter.canonical(plan))
        self.assertNotIn(str(self.stdlib), self.load().hashes())
        self.release.stage.assert_called_once()
        self.deploy.main.assert_not_called()

    def test_same_complete_hashes_for_execution_and_reuse(self):
        self.prepare()
        task = self.load()
        hashes = task.hashes()
        for label in ("source-tests", "candidate-tests", "staged-tests"):
            self.assertEqual(adapter.document(task.here / (label + ".json"))["source_hashes"], hashes)
        binding = adapter.document(task.here / "candidate-tests-binding.json")
        self.assertEqual(binding["input_hashes"], hashes)
        self.assertTrue({str(self.manifest), str(self.installer_file), str(self.stdlib), str(self.config),
                         str(self.source_python), str(self.candidate_python), str(self.source),
                         str(self.repo / "tests")} <= set(hashes))
        live = adapter.Adapter(task)
        live.tests("live-tests", self.new / adapter.SITE, task.here / "validation-tests")
        receipt = adapter.document(task.here / "live-tests.json")
        self.assertTrue(receipt["execution_reused"])
        self.assertEqual(receipt["source_hashes"], hashes)
        self.assertEqual(receipt["verified_package_root"], str(self.new / adapter.SITE))
        self.assertEqual(self.test_executions.call_count, 2)
        for label in ("staged-tests", "live-tests"):
            self.assertTrue(adapter.document(task.here / (label + ".json"))["execution_reused"])
            self.assertFalse((task.here / (label + "-binding.json")).exists())
            self.assertFalse((task.here / (label + "-refresh.json")).exists())
        self.assertNotIn("execution_refresh", live.test_evidence["live-tests"])

    def candidate_test_files(self, task):
        return {name: (task.here / name).read_bytes() for name in (
            "candidate-tests.json", "candidate-tests.log", "candidate-tests-binding.json")}

    def assert_environment_refresh(self, task, label, original, evidence=None):
        actual_path = task.here / (label + ".json")
        actual = adapter.document(actual_path)
        binding_path = task.here / (label + "-binding.json")
        binding = adapter.document(binding_path)
        baseline = adapter.document(task.here / "candidate-tests-binding.json")
        self.assertEqual({key: value for key, value in baseline.items() if key != "environment_sha256"},
                         {key: value for key, value in binding.items() if key != "environment_sha256"})
        self.assertNotEqual(baseline["environment_sha256"], binding["environment_sha256"])
        refresh = {"cause": "environment_sha256_changed",
                   "candidate_receipt": adapter.identity(task.here / "candidate-tests.json"),
                   "candidate_binding": adapter.identity(task.here / "candidate-tests-binding.json"),
                   "previous_environment_sha256": baseline["environment_sha256"],
                   "environment_sha256": binding["environment_sha256"]}
        refresh_path = task.here / (label + "-refresh.json")
        audit = adapter.document(refresh_path)
        self.assertEqual(audit, {**actual, "execution_reused": False,
                                 "execution_receipt": adapter.identity(actual_path), "execution_refresh": refresh})
        self.assertIs(actual.get("execution_reused", False), False)
        self.assertIs(audit["execution_reused"], False)
        self.assertEqual(actual["exit_code"], 0)
        self.assertEqual(actual["source_hashes"], task.hashes())
        self.assertEqual(actual["interpreter"], str(self.candidate_python.resolve()))
        self.assertEqual(actual["sha256"], adapter.sha(Path(actual["log"])))
        self.assertEqual(actual["runtime_image"], str(self.old / "runtime-image.json"))
        artifacts = adapter.document(self.old / "runtime-image.json")["artifacts"]
        self.assertEqual(actual["runtime_router"], artifacts["tura_router"]["path"])
        self.assertEqual(actual["runtime_session_db"], artifacts["tura_session_db"]["path"])
        if evidence is not None:
            self.assertIs(evidence["execution_reused"], False)
            self.assertEqual(evidence["receipt"], adapter.identity(refresh_path))
            self.assertEqual(evidence["binding"], adapter.identity(binding_path))
            self.assertEqual(evidence["execution_receipt"], adapter.identity(actual_path))
            self.assertEqual(evidence["execution_refresh"], refresh)
        self.assertEqual(self.candidate_test_files(task), original)

    def live_refresh_fixture(self):
        self.prepare()
        task = self.load()
        original = self.candidate_test_files(task)
        live = adapter.Adapter(task)
        os.environ["NOKIY_FIXTURE_TEST_ENV"] = "live-refresh"
        return task, live, original

    def assert_caller_refresh_rejected(self, task, instance, error):
        for label in ("staged-tests", "live-tests"):
            with self.subTest(label=label):
                run = lambda: instance.tests(label, self.new / adapter.SITE, task.here / "validation-tests")
                if isinstance(error, str):
                    self.assert_rejected(error, run)
                else:
                    with self.assertRaises(error):
                        run()
                self.assertFalse((task.here / (label + ".log")).exists())
                self.assertFalse((task.here / (label + "-binding.json")).exists())
                self.assertFalse((task.here / (label + "-refresh.json")).exists())
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertEqual(instance.test_evidence, {})

    def test_environment_only_drift_executes_fresh_staged_and_live_tests(self):
        original_stage = self.release.stage.side_effect
        original = {}

        def changed_environment():
            task = self.load()
            original.update(self.candidate_test_files(task))
            os.environ["NOKIY_FIXTURE_TEST_ENV"] = "stage-refresh"
            original_stage()

        self.release.stage.side_effect = changed_environment
        result = self.prepare()
        task = self.load()
        self.assert_environment_refresh(task, "staged-tests", original,
                                        result["preparation_evidence"]["tests"]["staged-tests"])
        self.assertEqual([call.args[0] for call in self.test_executions.call_args_list],
                         ["source-tests", "candidate-tests", "staged-tests"])
        live = adapter.Adapter(task)
        os.environ["NOKIY_FIXTURE_TEST_ENV"] = "live-refresh"
        live.tests("live-tests", self.new / adapter.SITE, task.here / "validation-tests")
        self.assert_environment_refresh(task, "live-tests", original, live.test_evidence["live-tests"])
        self.assertEqual([call.args[0] for call in self.test_executions.call_args_list],
                         ["source-tests", "candidate-tests", "staged-tests", "live-tests"])
        self.assertEqual(live.runtime_tests_executed, set())
        self.assertEqual(self.template.OLD, task.old)
        self.deploy.main.assert_not_called()

    def test_refresh_rejects_non_environment_or_mixed_binding_drift(self):
        self.prepare()
        task = self.load()
        instance = adapter.Adapter(task)
        original = self.candidate_test_files(task)
        path = task.here / "candidate-tests-binding.json"
        baseline = adapter.document(path)
        cases = [(key, value) for key, value in (
            ("input_hashes", {}), ("package", {}), ("interpreter", {}), ("test_modules", []),
            ("unexpected_key", True))] + [("missing_key", None)]
        for environment in ({}, {"NOKIY_FIXTURE_TEST_ENV": "mixed-drift"}):
            with patch.dict(os.environ, environment):
                for key, value in cases:
                    with self.subTest(environment=environment, field=key):
                        changed = dict(baseline)
                        if key == "missing_key":
                            changed.pop("package")
                        else:
                            changed[key] = value
                        path.write_text(json.dumps(changed))
                        try:
                            self.assert_caller_refresh_rejected(task, instance, "REUSE_BINDING_DRIFT")
                        finally:
                            path.write_bytes(original[path.name])
        self.assertEqual(self.candidate_test_files(task), original)

    def test_environment_refresh_rejects_changed_package_inputs_interpreter_and_tests(self):
        task, instance, original = self.live_refresh_fixture()
        cases = (
            (self.new / adapter.SITE / adapter.PACKAGE / "unchanged.py", "REUSE_BINDING_DRIFT"),
            (self.source / "changed.py", "INPUT_DRIFT"),
            (self.candidate_python, "INPUT_DRIFT"),
            (self.repo / "tests/test_changed.py", "INPUT_DRIFT"),
        )
        for path, error in cases:
            with self.subTest(path=path):
                content = path.read_bytes()
                path.write_bytes(content + b"drift\n")
                try:
                    self.assert_caller_refresh_rejected(task, instance, error)
                finally:
                    path.write_bytes(content)
        self.write(self.repo / "tests/helper.py", "changed test dependency\n")
        self.assert_caller_refresh_rejected(task, instance, "REUSE_RECEIPT_INVALID")
        self.assertEqual(self.candidate_test_files(task), original)

    def test_environment_refresh_rejects_invalid_candidate_receipts_before_execution(self):
        task, instance, original = self.live_refresh_fixture()
        path = task.here / "candidate-tests.json"
        prior = adapter.document(path)
        cases = [{**prior, **changes} for changes in (
            {"exit_code": 1}, {"exit_code": False}, {"execution_reused": True}, {"source_hashes": {}},
            {"interpreter": "wrong-interpreter"}, {"log": "wrong-log"}, {"sha256": "0" * 64})]
        cases += [{key: value for key, value in prior.items() if key != missing}
                  for missing in ("exit_code", "source_hashes", "interpreter", "log", "sha256")]
        for receipt in cases:
            with self.subTest(receipt=receipt):
                path.write_text(json.dumps(receipt))
                try:
                    self.assert_caller_refresh_rejected(task, instance, "REUSE_RECEIPT_INVALID")
                finally:
                    path.write_bytes(original[path.name])
        self.assertEqual(self.candidate_test_files(task), original)

    def test_environment_refresh_rejects_missing_or_malformed_candidate_evidence(self):
        task, instance, original = self.live_refresh_fixture()
        for name in ("candidate-tests.json", "candidate-tests-binding.json"):
            path = task.here / name
            for content, error in ((None, FileNotFoundError), (b"{", json.JSONDecodeError),
                                   (b"[]", adapter.PreparationError)):
                with self.subTest(name=name, content=content):
                    if content is None:
                        path.unlink()
                    else:
                        path.write_bytes(content)
                    try:
                        self.assert_caller_refresh_rejected(task, instance, error)
                    finally:
                        path.write_bytes(original[name])
        self.assertEqual(self.candidate_test_files(task), original)

    def test_environment_refresh_requires_valid_prior_and_current_environment_digests(self):
        task, instance, original = self.live_refresh_fixture()
        path = task.here / "candidate-tests-binding.json"
        baseline = adapter.document(path)
        cases = [{key: value for key, value in baseline.items() if key != "environment_sha256"}]
        cases += [{**baseline, "environment_sha256": value} for value in (None, "", "f" * 63, "g" * 64, 64)]
        for binding in cases:
            with self.subTest(binding=binding):
                with patch.object(instance, "test_binding", return_value=binding):
                    self.assert_caller_refresh_rejected(task, instance, adapter.PreparationError)
                path.write_text(json.dumps(binding))
                try:
                    self.assert_caller_refresh_rejected(task, instance, adapter.PreparationError)
                finally:
                    path.write_bytes(original[path.name])
        self.assertEqual(self.candidate_test_files(task), original)

    def assert_invalid_environment_refresh_receipt(self, *, malformed=False, **changes):
        task, live, original = self.live_refresh_fixture()

        def corrupt_receipt(*args):
            self.execute_fixture_tests(self.template, *args)
            path = task.here / "live-tests.json"
            receipt = adapter.document(path)
            receipt.update(changes)
            path.write_text("{" if malformed else json.dumps(receipt))

        self.test_executions.side_effect = corrupt_receipt
        run = lambda: live.tests("live-tests", self.new / adapter.SITE, task.here / "validation-tests")
        if malformed:
            with self.assertRaises(json.JSONDecodeError):
                run()
        else:
            self.assert_rejected("TEST_RECEIPT_INVALID", run)
        self.assertEqual(self.test_executions.call_count, 3)
        self.assertNotIn("live-tests", live.test_evidence)
        self.assertFalse((task.here / "live-tests-binding.json").exists())
        self.assertFalse((task.here / "live-tests-refresh.json").exists())
        self.assertEqual(self.candidate_test_files(task), original)

    def test_environment_refresh_rejects_failed_test_receipt(self):
        self.assert_invalid_environment_refresh_receipt(exit_code=1)

    def test_environment_refresh_rejects_boolean_exit_code(self):
        self.assert_invalid_environment_refresh_receipt(exit_code=False)

    def test_environment_refresh_rejects_reused_execution_receipt(self):
        self.assert_invalid_environment_refresh_receipt(execution_reused=True)

    def test_environment_refresh_rejects_execution_source_hash_drift(self):
        self.assert_invalid_environment_refresh_receipt(source_hashes={})

    def test_environment_refresh_rejects_execution_interpreter_drift(self):
        self.assert_invalid_environment_refresh_receipt(interpreter="wrong-interpreter")

    def test_environment_refresh_rejects_execution_log_path_drift(self):
        self.assert_invalid_environment_refresh_receipt(log="wrong-log")

    def test_environment_refresh_rejects_execution_log_digest_drift(self):
        self.assert_invalid_environment_refresh_receipt(sha256="0" * 64)

    def test_environment_refresh_rejects_malformed_execution_receipt(self):
        self.assert_invalid_environment_refresh_receipt(malformed=True)

    def test_environment_refresh_propagates_test_execution_failure(self):
        task, live, original = self.live_refresh_fixture()
        self.test_executions.side_effect = RuntimeError("fixture suite failure")
        self.assert_rejected("TEST_EXECUTION_FAILED",
                             lambda: live.tests("live-tests", self.new / adapter.SITE, task.here / "validation-tests"))
        self.assertEqual(self.test_executions.call_count, 3)
        self.assertNotIn("live-tests", live.test_evidence)
        self.assertFalse((task.here / "live-tests.json").exists())
        self.assertFalse((task.here / "live-tests-binding.json").exists())
        self.assertFalse((task.here / "live-tests-refresh.json").exists())
        self.assertEqual(self.candidate_test_files(task), original)

    def assert_environment_refresh_run_drift(self, mutate):
        task, live, original = self.live_refresh_fixture()

        def changed_during_execution(*args):
            self.execute_fixture_tests(self.template, *args)
            mutate()

        self.test_executions.side_effect = changed_during_execution
        self.assert_rejected("TEST_INPUT_DRIFT",
                             lambda: live.tests("live-tests", self.new / adapter.SITE, task.here / "validation-tests"))
        self.assertEqual(self.test_executions.call_count, 3)
        self.assertNotIn("live-tests", live.test_evidence)
        self.assertTrue((task.here / "live-tests.json").exists())
        self.assertFalse((task.here / "live-tests-binding.json").exists())
        self.assertFalse((task.here / "live-tests-refresh.json").exists())
        self.assertEqual(self.candidate_test_files(task), original)

    def test_environment_refresh_rejects_environment_drift_during_execution(self):
        self.assert_environment_refresh_run_drift(lambda: os.environ.update(NOKIY_FIXTURE_TEST_ENV="during-run"))

    def test_environment_refresh_rejects_package_drift_during_execution(self):
        self.assert_environment_refresh_run_drift(
            lambda: self.write(self.new / adapter.SITE / adapter.PACKAGE / "unchanged.py", "during-run drift\n"))

    def test_environment_refresh_rejects_test_tree_drift_during_execution(self):
        self.assert_environment_refresh_run_drift(
            lambda: self.write(self.repo / "tests/helper.py", "during-run test dependency\n"))

    def test_environment_only_drift_whole_release_transaction_verifies_without_replay(self):
        self.use_transaction_fixture()
        before_config = self.config.read_bytes()
        self.prepare()
        task = self.load()
        original = self.candidate_test_files(task)
        plan = (task.out / "plan.json").read_bytes()
        self.approve_environment(task)
        for phase in ("preflight", "apply"):
            self.assertIsNone(adapter.run(self.load(), phase))
        completed = {phase: (task.out / ("adapter-" + phase + ".completed.json")).read_bytes()
                     for phase in ("preflight", "apply")}
        adopted_entry = self.entry.resolve(strict=True)
        os.environ["NOKIY_FIXTURE_TEST_ENV"] = "post-apply-refresh"
        self.assertIsNone(adapter.run(self.load(), "verify"))
        self.assert_environment_refresh(task, "live-tests", original)
        self.assertEqual(self.phase_executions, list(adapter.PHASES))
        self.assertEqual([call.args[0] for call in self.test_executions.call_args_list],
                         ["source-tests", "candidate-tests", "live-tests"])
        self.assertEqual(self.live_test_calls, [
            ("live-tests", self.new / adapter.SITE, task.here / "validation-tests", adopted_entry)])
        self.assertEqual(adopted_entry, self.new / "caller/bin/nokiy-embedded-run")
        self.assertEqual(self.entry.resolve(strict=True), adopted_entry)
        self.assertTrue(adapter.document(task.out / "fixture-verify.json")["healthy"])
        self.assertTrue((task.out / "adapter-verify.completed.json").is_file())
        self.assertEqual((task.out / "plan.json").read_bytes(), plan)
        self.assertEqual(self.config.read_bytes(), before_config)
        for phase, receipt in completed.items():
            self.assertEqual((task.out / ("adapter-" + phase + ".completed.json")).read_bytes(), receipt)
        evidence = {str(path.relative_to(task.here)): path.read_bytes()
                    for path in task.here.rglob("*") if path.is_file()}
        self.assert_rejected("PHASE_ALREADY_ATTEMPTED", lambda: adapter.run(self.load(), "verify"))
        self.assertEqual(evidence, {str(path.relative_to(task.here)): path.read_bytes()
                                    for path in task.here.rglob("*") if path.is_file()})
        self.assertEqual(self.phase_executions, list(adapter.PHASES))
        self.assertEqual(self.test_executions.call_count, 3)

    def test_legacy_packaging_projects_files_and_preserves_tree_witnesses(self):
        self.prepare()
        task = self.load()
        inputs = task.hashes()
        files = {path: value for path, value in inputs.items() if Path(path).is_file()}
        trees = {path: value for path, value in inputs.items() if Path(path).is_dir()}
        self.assertTrue(trees)
        for name, key in (("adoption", "postimages"), ("rust-build", "source_hashes")):
            self.assertEqual(adapter.document(task.here / (name + ".json"))[key], files)
        self.assertEqual(adapter.document(task.here / "preparation.json")["input_hashes"], inputs)
        self.assertEqual(adapter.document(task.here / "candidate-tests-binding.json")["input_hashes"], inputs)
        input_trees = task.out / "input-trees.json"
        self.assertEqual(adapter.document(input_trees), trees)
        pins = adapter.document(task.out / "pins.json")
        for path, expected in files.items():
            self.assertEqual(pins[path], expected)
        self.assertEqual(pins[str(input_trees)], adapter.sha(input_trees))
        self.assertFalse((task.here / "validation-tests").exists())

    def test_legacy_marker_rejects_other_labels_and_paths(self):
        self.prepare()
        task = self.load()
        live = adapter.Adapter(task)
        marker = task.here / "validation-tests"
        staged = task.new / adapter.SITE
        cases = (
            ("source-tests", task.repo / "src", marker),
            ("candidate-tests", task.here / "validation-package", marker),
        ) + tuple(
            (label, staged, test_root)
            for label in ("staged-tests", "live-tests")
            for test_root in (
                task.here / "validation-tests-other",
                task.root / "validation-tests",
                marker / "test_changed.py",
                task.here / "other/../validation-tests",
            )
        )
        for label, package_root, test_root in cases:
            with self.subTest(label=label, test_root=test_root):
                self.assert_rejected("TEST_ROOT_INVALID", lambda: live.tests(label, package_root, test_root))
        self.assert_rejected("TEST_LABEL_INVALID", lambda: live.tests("stage-tests", staged, marker))
        for label in ("staged-tests", "live-tests"):
            with self.subTest(label=label):
                self.assert_rejected("TEST_PACKAGE_ROOT_INVALID",
                                     lambda: live.tests(label, task.here / "validation-package", marker))
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse((task.here / "live-tests.json").exists())

    def test_staged_marker_reuse_rejects_runtime_tree_drift(self):
        original_stage = self.release.stage.side_effect

        def changed_runtime():
            self.write(self.stdlib / "dependency.py", "drift\n")
            original_stage()

        self.release.stage.side_effect = changed_runtime
        self.assert_rejected("INPUT_DRIFT")
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse((self.task_root / "deployment-evidence/staged-tests.json").exists())
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        self.deploy.main.assert_not_called()

    def test_staged_marker_reuse_rejects_unlisted_test_helper_drift(self):
        original_stage = self.release.stage.side_effect

        def changed_helper():
            self.write(self.repo / "tests/helper.py", "changed test dependency\n")
            original_stage()

        self.release.stage.side_effect = changed_helper
        self.assert_rejected("REUSE_RECEIPT_INVALID")
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse((self.task_root / "deployment-evidence/staged-tests.json").exists())
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        self.deploy.main.assert_not_called()

    def test_reuse_rejects_unlisted_package_and_runtime_drift_without_execution(self):
        self.prepare()
        task = self.load()
        live = adapter.Adapter(task)
        marker = task.here / "validation-tests"
        unlisted = self.new / adapter.SITE / adapter.PACKAGE / "unchanged.py"
        original = unlisted.read_bytes()
        unlisted.write_bytes(original + b"drift\n")
        self.assert_rejected("REUSE_BINDING_DRIFT", lambda: live.tests("live-tests", self.new / adapter.SITE, marker))
        unlisted.write_bytes(original)
        self.write(self.stdlib / "dependency.py", "drift\n")
        self.assert_rejected("INPUT_DRIFT", lambda: live.tests("live-tests", self.new / adapter.SITE, marker))
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse((task.here / "live-tests.json").exists())

    def test_reuse_rejects_unlisted_test_helper_drift(self):
        self.prepare()
        task = self.load()
        live = adapter.Adapter(task)
        marker = task.here / "validation-tests"
        self.write(self.repo / "tests/helper.py", "changed test dependency\n")
        self.assert_rejected("REUSE_RECEIPT_INVALID", lambda: live.tests("live-tests", self.new / adapter.SITE, marker))
        self.assertEqual(self.test_executions.call_count, 2)

    def test_partial_evidence_is_preserved_and_not_resumed(self):
        self.release.stage.side_effect = RuntimeError("fixture interruption")
        with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
            self.prepare()
        here = self.task_root / "deployment-evidence"
        before = {str(path.relative_to(here)): path.read_bytes() for path in here.rglob("*") if path.is_file()}
        self.assert_rejected("PARTIAL_PREPARATION_EXISTS")
        after = {str(path.relative_to(here)): path.read_bytes() for path in here.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.test_executions.call_count, 2)
        self.deploy.main.assert_not_called()
        self.assertFalse((here / "cutover/plan.json").exists())

    def test_no_model_config_environment_or_authorization_change(self):
        before_config, before_env = self.config.read_bytes(), dict(os.environ)
        self.prepare()
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(dict(os.environ), before_env)
        self.assertEqual(adapter.document(self.new / "caller-binding.json"), adapter.document(self.old / "caller-binding.json"))
        self.assertEqual(adapter.document(self.root / "binding-probe/caller-binding.json")["default_profile"]["model"], "existing-model")
        task = self.load()
        self.assertEqual((task.out / "authorization.json").read_bytes(), (self.task_root / "authorization.json").read_bytes())
        self.assertEqual(self.release.VERSION, self.data["candidate"]["version"])
        self.assertEqual(self.release.BUILD_ID, self.deploy.BUILD_ID)
        self.assertEqual(self.release.RUNTIME_BINS, ())
        self.assertEqual(self.release.RUST_CHECKS, ())

    def test_changed_profile_never_emits_a_plan(self):
        original_stage = self.release.stage.side_effect

        def changed_profile():
            original_stage()
            binding = self.new / "caller-binding.json"
            value = adapter.document(binding)
            value["default_profile"]["model"] = "unrequested-model"
            binding.write_text(json.dumps(value))

        self.release.stage.side_effect = changed_profile
        self.assert_rejected("MODEL_PROFILE_DRIFT")
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        self.deploy.main.assert_not_called()
        self.assertEqual(self.entry.resolve(), self.old / "caller/bin/nokiy-embedded-run")

    def test_installed_preimage_drift_before_import(self):
        self.entry.unlink()
        alternate = self.root / "alternate-entry"
        self.write(alternate, "not installed preimage\n")
        self.entry.symlink_to(alternate)
        self.assert_rejected("INSTALLED_PREIMAGE_DRIFT")
        self.import_mock.assert_not_called()

    def test_pinned_interpreter_symlink_passes_check_pins_and_preflight(self):
        target = self.pin_source_interpreter_symlink()
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        pins = adapter.document(task.out / "pins.json")
        for path in (self.source_python, target):
            self.assertEqual(pins[str(path)], adapter.sha(target))
        # Non-canonical paths remain invalid outside the exact identity proof.
        self.assert_rejected("CANONICAL_PATH_REQUIRED", lambda: adapter.absolute(str(self.source_python)))
        phase.check_pins()
        self.approve_environment(task)
        phase.phase("preflight")
        self.deploy.main.assert_called_once_with("preflight")
        self.assertTrue((task.out / "adapter-preflight.completed.json").is_file())

    def test_check_pins_rejects_same_content_interpreter_retarget(self):
        target = self.pin_source_interpreter_symlink()
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        self.approve_environment(task)
        alternate = self.root / "alternate-python"
        self.write(alternate, target.read_text())
        self.source_python.unlink()
        self.source_python.symlink_to(alternate)
        self.assertEqual(adapter.sha(self.source_python), adapter.sha(target))
        self.assert_rejected("RESOLVED_IDENTITY_REQUIRED", phase.check_pins)
        self.assert_rejected("RESOLVED_IDENTITY_REQUIRED", lambda: phase.phase("preflight"))
        self.deploy.main.assert_not_called()
        self.assertFalse((task.out / "adapter-preflight.started.json").exists())

    def test_check_pins_rejects_interpreter_target_content_drift(self):
        target = self.pin_source_interpreter_symlink()
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        self.approve_environment(task)
        self.write(target, "changed source interpreter\n")
        self.assert_rejected("INPUT_DRIFT", phase.check_pins)
        self.assert_rejected("INPUT_DRIFT", lambda: phase.phase("preflight"))
        self.deploy.main.assert_not_called()
        self.assertFalse((task.out / "adapter-preflight.started.json").exists())

    def test_check_pins_compares_verified_input_with_persisted_sha(self):
        self.pin_source_interpreter_symlink()
        self.prepare()
        phase = adapter.Adapter(self.load())
        pins_path = phase.task.out / "pins.json"
        pins = adapter.document(pins_path)
        pins[str(self.source_python)] = "0" * 64
        pins_path.write_text(json.dumps(pins))
        self.assert_rejected("PIN_DRIFT", phase.check_pins)

    def test_check_pins_rejects_unbound_same_content_evidence_symlink(self):
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        self.approve_environment(task)
        log = task.here / "source-tests.log"
        self.assertNotIn(str(log), task.hashes())
        target = self.root / "source-tests-copy.log"
        self.write(target, log.read_text())
        log.unlink()
        log.symlink_to(target)
        self.assertEqual(adapter.sha(log), adapter.document(task.out / "pins.json")[str(log)])
        self.assert_rejected("CANONICAL_PATH_REQUIRED", phase.check_pins)
        self.assert_rejected("CANONICAL_PATH_REQUIRED", lambda: phase.phase("preflight"))
        self.deploy.main.assert_not_called()
        self.assertFalse((task.out / "adapter-preflight.started.json").exists())

    def test_check_pins_rejects_evidence_content_drift(self):
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        self.approve_environment(task)
        self.write(task.here / "source-tests.log", "changed evidence\n")
        self.assert_rejected("PIN_DRIFT", phase.check_pins)
        self.assert_rejected("PIN_DRIFT", lambda: phase.phase("preflight"))
        self.deploy.main.assert_not_called()
        self.assertFalse((task.out / "adapter-preflight.started.json").exists())

    def test_phase_requires_parent_binding_and_never_replays(self):
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        self.assert_rejected("PARENT_PLAN_BINDING_REQUIRED", lambda: phase.phase("preflight"))
        self.approve_environment(task)
        phase.phase("preflight")
        self.deploy.main.assert_called_once_with("preflight")
        self.assert_rejected("PHASE_ALREADY_ATTEMPTED", lambda: phase.phase("preflight"))
        self.assertEqual(self.deploy.main.call_count, 1)

    def test_failed_phase_is_not_replayed(self):
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        self.approve_environment(task)
        self.deploy.main.return_value = 1
        self.assert_rejected("INSTALLER_PHASE_FAILED", lambda: phase.phase("preflight"))
        self.assertFalse((task.out / "adapter-preflight.completed.json").exists())
        self.assert_rejected("PHASE_ALREADY_ATTEMPTED", lambda: phase.phase("preflight"))
        self.assertEqual(self.deploy.main.call_count, 1)

    def test_existing_main_zero_exit_keeps_phase_receipt(self):
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        self.approve_environment(task)
        self.deploy.main.side_effect = SystemExit(0)
        phase.phase("preflight")
        self.assertTrue((task.out / "adapter-preflight.completed.json").is_file())
        self.assert_rejected("PHASE_ALREADY_ATTEMPTED", lambda: phase.phase("preflight"))

    def test_config_and_native_environment_drift_rejected(self):
        os.environ["CODEX_HOME"] = str(self.root / "other-codex-home")
        self.assert_rejected("NATIVE_ENVIRONMENT_DRIFT")
        os.environ.pop("CODEX_HOME")
        self.write(self.config, 'model = "different"\n')
        self.assert_rejected("INPUT_DRIFT")
        self.import_mock.assert_not_called()

    def test_manifest_cannot_introduce_commands(self):
        self.data["commands"] = {"apply": ["sh", "-c", "forbidden"]}
        self.write_manifest()
        self.assert_rejected("MANIFEST_FIELDS_INVALID")
        self.import_mock.assert_not_called()

    def test_expired_manifest_rejected_before_import(self):
        self.data["expires_at"] = time.time() - 1
        self.write_manifest()
        self.assert_rejected("AUTHORIZATION_EXPIRED")
        self.import_mock.assert_not_called()

    def test_runtime_only_whole_release_transaction_uses_new_image_and_four_bins(self):
        self.enable_runtime_update()
        self.use_transaction_fixture()
        before_env, before_config = dict(os.environ), self.config.read_bytes()
        old_caller = adapter.package_bytes(self.old / "caller")
        original_receipt = self.build_file.read_bytes()
        self.prepare()
        task = self.load()
        self.assertEqual(dict(os.environ), before_env)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(self.phase_executions, [])
        self.assertEqual(self.data["modules"], [])
        self.assertEqual(adapter.package_bytes(self.new / "caller"), old_caller)
        self.assertEqual(adapter.document(self.new / "caller-binding.json")["default_profile"],
                         adapter.document(self.old / "caller-binding.json")["default_profile"])
        self.assertEqual(self.release.RUNTIME_BINS, self.deploy.RUNTIME_BINS)
        self.assertEqual(self.release.RUNTIME_BINS, adapter.RUNTIME_BINS)
        adoption = adapter.document(task.here / "adoption.json")
        projection = adapter.document(task.here / "rust-build.json")
        self.assertFalse(adoption["unchanged_runtime"])
        self.assertFalse((task.here / "runtime-unchanged.json").exists())
        self.assertEqual(projection["source_hashes"], adoption["postimages"])
        self.assertEqual(projection["receipt_source_hashes"], self.build["source_hashes"])
        self.assertEqual(projection["build_receipt"], self.data["runtime_build"])
        self.assertFalse(projection["adapter_executed_build"])
        for key in ("argv", "exit_code", "log", "sha256", "seconds", "external_model_calls", "binary_hashes"):
            self.assertEqual(projection[key], self.build[key])
        self.assertEqual(self.build_file.read_bytes(), original_receipt)
        hashes, pins = task.hashes(), adapter.document(task.out / "pins.json")
        for path in (self.build_file, self.build_log, self.rust_source, self.rust_lock,
                     self.transitive_source, *self.compiled.values()):
            self.assertEqual(hashes[str(path)], adapter.sha(path))
            self.assertEqual(pins[str(path)], hashes[str(path)])
        image = adapter.document(self.new / "runtime-image.json")
        self.assertEqual(set(image["artifacts"]), set(adapter.RUNTIME_BINS))
        for name, value in image["artifacts"].items():
            self.assertTrue(Path(value["path"]).is_relative_to(self.new))
            self.assertEqual(value["sha256"], self.build["binary_hashes"][name])
            self.assertEqual(pins[value["path"]], value["sha256"])
        self.approve_environment(task)
        for phase in adapter.PHASES:
            self.assertIsNone(adapter.run(self.load(), phase))
            self.assertTrue((task.out / ("adapter-" + phase + ".completed.json")).is_file())
        self.assertEqual(self.phase_executions, list(adapter.PHASES))
        self.assertEqual(self.entry.resolve(), self.new / "caller/bin/nokiy-embedded-run")
        self.assertTrue(adapter.document(task.out / "fixture-verify.json")["healthy"])
        for label in ("source-tests", "candidate-tests", "staged-tests", "live-tests"):
            receipt = adapter.document(task.here / (label + ".json"))
            binding = adapter.document(task.here / (label + "-binding.json"))
            runtime = label in ("staged-tests", "live-tests")
            expected_image = (self.new if runtime else self.old) / "runtime-image.json"
            artifacts = adapter.document(expected_image)["artifacts"]
            self.assertFalse(receipt.get("execution_reused", False))
            self.assertEqual(receipt["source_hashes"], hashes)
            self.assertEqual(receipt["runtime_image"], str(expected_image))
            self.assertEqual(receipt["runtime_router"], artifacts["tura_router"]["path"])
            self.assertEqual(receipt["runtime_session_db"], artifacts["tura_session_db"]["path"])
            self.assertEqual(binding.get("runtime_acceptance", False), runtime)
            if runtime:
                self.assertEqual(binding["runtime_image"], adapter.identity(expected_image))
                self.assertEqual(set(binding["runtime_artifacts"]), set(adapter.RUNTIME_BINS))
        self.assertEqual([call.args[0] for call in self.test_executions.call_args_list],
                         ["source-tests", "candidate-tests", "staged-tests", "live-tests"])
        self.assertEqual(adapter.package_bytes(self.old / "caller"), old_caller)
        self.assertEqual(self.build_file.read_bytes(), original_receipt)

    def test_runtime_update_with_declared_caller_modules(self):
        self.enable_runtime_update(caller_modules=True)
        self.prepare()
        for name in self.modules:
            self.assertEqual((self.new / adapter.SITE / adapter.PACKAGE / name).read_bytes(),
                             (self.source / name).read_bytes())
        self.assertEqual((self.new / adapter.SITE / adapter.PACKAGE / "unchanged.py").read_bytes(),
                         (self.pkg / "unchanged.py").read_bytes())
        self.assertEqual(self.test_executions.call_count, 3)
        self.deploy.main.assert_not_called()

    def test_catalog_update_is_staged_before_tests_without_mutating_old_inode(self):
        self.enable_catalog_update()
        prior = (self.old_catalog.read_bytes(), self.old_catalog.stat())
        self.prepare()
        task = self.load()
        image = adapter.document(self.new / "runtime-image.json")
        target = Path(image["artifacts"]["command_catalog_0"]["path"])
        self.assertEqual(target.read_bytes(), self.catalog_source.read_bytes())
        self.assertEqual(self.old_catalog.read_bytes(), prior[0])
        self.assertEqual(self.old_catalog.stat().st_ino, prior[1].st_ino)
        self.assertEqual(self.old_catalog.stat().st_mode, prior[1].st_mode)
        self.assertNotEqual(target.stat().st_ino, prior[1].st_ino)
        binding = adapter.document(self.new / "caller-binding.json")
        self.assertEqual(binding["runtime_image"], adapter.identity(self.new / "runtime-image.json"))
        provenance = adapter.document(self.new / "release-provenance.json")
        self.assertEqual(provenance["catalog_updates"], self.build["catalog_updates"])
        self.assertEqual(provenance["changed_runtime_artifacts"], [*adapter.RUNTIME_BINS, "command_catalog_0"])
        staged = adapter.document(task.here / "staged-tests-binding.json")
        self.assertEqual(staged["runtime_artifacts"]["command_catalog_0"], adapter.identity(target))
        pins = adapter.document(task.out / "pins.json")
        self.assertEqual(pins[str(self.catalog_source)], adapter.sha(self.catalog_source))
        self.assertEqual(pins[str(target)], adapter.sha(target))
        self.assertEqual(self.entry.resolve(), self.old / "caller/bin/nokiy-embedded-run")
        self.validator_binding_guard.assert_called_once()
        self.validator_final_guard.assert_called_once()
        self.deploy.main.assert_not_called()

    def test_prompt_catalog_update_uses_existing_immutable_stage(self):
        self.enable_catalog_update(prompt=True)
        prior = (self.old_catalog.read_bytes(), self.old_catalog.stat().st_ino)
        self.prepare()
        task = self.load()
        image = adapter.document(self.new / "runtime-image.json")
        target = Path(image["artifacts"]["command_catalog_0"]["path"])
        self.assertEqual(target.read_bytes(), self.catalog_source.read_bytes())
        self.assertEqual((self.old_catalog.read_bytes(), self.old_catalog.stat().st_ino), prior)
        self.assertNotEqual(target.stat().st_ino, prior[1])
        pins = adapter.document(task.out / "pins.json")
        self.assertEqual(pins[str(target)], adapter.sha(target))
        self.assertEqual(pins[str(self.catalog_source)], adapter.sha(self.catalog_source))
        self.assertEqual(self.entry.resolve(), self.old / "caller/bin/nokiy-embedded-run")
        self.deploy.main.assert_not_called()

    def test_prompt_catalog_rejects_empty_invalid_utf8_and_oversized_content(self):
        self.enable_catalog_update(prompt=True)
        for value in (b" \n", b"\xff", b"x" * (adapter.LIMIT + 1)):
            with self.subTest(size=len(value)):
                self.catalog_source.write_bytes(value)
                digest = adapter.sha(self.catalog_source)
                self.build["source_hashes"][str(self.catalog_source)] = digest
                self.build["catalog_updates"]["command_catalog_0"]["sha256"] = digest
                self.write_runtime_build()
                self.assert_rejected("CATALOG_PROMPT_INVALID")
        self.import_mock.assert_not_called()

    def test_catalog_validator_retains_non_catalog_and_all_other_predicates(self):
        self.enable_catalog_update()
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        self.validator_binding_guard.return_value = False
        with self.assertRaises(AssertionError):
            phase.deploy.validate()
        self.validator_binding_guard.return_value = True
        self.validator_final_guard.return_value = False
        with self.assertRaises(AssertionError):
            phase.deploy.validate()
        self.validator_final_guard.return_value = True
        image_path = self.new / "runtime-image.json"
        original = image_path.read_text()
        image = json.loads(original)
        image["artifacts"]["unlisted_config"] = adapter.identity(self.rust_source)
        image_path.write_text(json.dumps(image))
        with self.assertRaises(KeyError):
            phase.deploy.validate()
        image_path.write_text(original)
        phase.deploy.validate()

    def test_catalog_validator_rejects_unrecognized_template_predicate(self):
        self.enable_catalog_update()
        self.deploy_file.write_text(self.deploy_file.read_text().replace(
            'expected = build["binary_hashes"][name] if name in RUNTIME_BINS else old_image["artifacts"][name]["sha256"]',
            'expected = entry["sha256"]'))
        self.data["templates"]["deploy"] = adapter.identity(self.deploy_file)
        self.write_manifest()
        self.assert_rejected("CATALOG_VALIDATOR_API_DRIFT")
        self.release.stage.assert_not_called()

    def test_build_receipt_can_exceed_input_limit_without_relaxing_task_or_schema_limits(self):
        self.enable_catalog_update()
        self.build["argv"] = ["cargo", "x" * adapter.LIMIT]
        self.write_runtime_build()
        self.assertGreater(self.build_file.stat().st_size, adapter.LIMIT)
        self.assertEqual(self.load().runtime_build()["argv"], self.build["argv"])
        self.assert_rejected("JSON_TOO_LARGE", lambda: adapter.document(self.build_file))
        self.catalog_source.write_text(json.dumps({"name": "command_run", "input_schema": {
            "type": "object", "description": "x" * adapter.LIMIT}}))
        value = adapter.sha(self.catalog_source)
        self.build["source_hashes"][str(self.catalog_source)] = value
        self.build["catalog_updates"]["command_catalog_0"]["sha256"] = value
        self.write_runtime_build()
        self.assert_rejected("JSON_TOO_LARGE")
        self.import_mock.assert_not_called()

    def test_catalog_update_rejects_duplicate_existing_destination(self):
        self.enable_catalog_update()
        image = adapter.document(self.old / "runtime-image.json")
        image["artifacts"]["command_catalog_1"] = dict(image["artifacts"]["command_catalog_0"])
        (self.old / "runtime-image.json").write_text(json.dumps(image))
        self.data["installed"]["metadata"]["runtime-image.json"] = adapter.sha(self.old / "runtime-image.json")
        self.build["catalog_updates"]["command_catalog_1"] = dict(self.build["catalog_updates"]["command_catalog_0"])
        self.write_runtime_build()
        self.assert_rejected("CATALOG_DESTINATION_COLLISION")
        self.import_mock.assert_not_called()

    def test_catalog_update_rejects_missing_identity_scope_and_source_pin_before_import(self):
        self.enable_catalog_update()
        original = json.loads(json.dumps(self.build))
        pin = original["catalog_updates"]["command_catalog_0"]
        invalid = (
            ([], "CATALOG_UPDATES_INVALID"),
            ({"tura_runtime": pin}, "CATALOG_SCOPE_INVALID"),
            ({"command_catalog_99": pin}, "CATALOG_SCOPE_INVALID"),
            ({"command_catalog_0": {**pin, "extra": True}}, "CATALOG_IDENTITY_INVALID"),
            ({"command_catalog_0": {**pin, "previous_sha256": "0" * 64}}, "CATALOG_PREIMAGE_DRIFT"),
            ({"command_catalog_0": {**pin, "sha256": "0" * 64}}, "CATALOG_SOURCE_UNBOUND"),
        )
        for updates, code in invalid:
            with self.subTest(code=code):
                self.build = {**original, "catalog_updates": updates}
                self.write_runtime_build()
                self.assert_rejected(code)
        self.build = json.loads(json.dumps(original))
        self.build["source_hashes"].pop(str(self.catalog_source))
        self.write_runtime_build()
        self.assert_rejected("CATALOG_SOURCE_UNBOUND")
        self.import_mock.assert_not_called()

    def test_catalog_update_rejects_prompt_config_alias_and_wrong_directory(self):
        self.enable_catalog_update()
        pin = self.build["catalog_updates"]["command_catalog_0"]
        for suffix in ("crates/tools/src/command_run/prompt.md", "provider_config.json",
                       "crates/tools/src/commands/bash/schema.json"):
            with self.subTest(suffix=suffix):
                source = self.repo / "replacement" / suffix
                self.write(source, self.catalog_source.read_text())
                pin["path"], pin["sha256"] = str(source), adapter.sha(source)
                self.build["source_hashes"][str(source)] = pin["sha256"]
                self.write_runtime_build()
                self.assert_rejected("CATALOG_SCOPE_INVALID")
        alias = self.repo / "alias/crates/tools/src/command_run/schema.json"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(self.catalog_source)
        pin["path"], pin["sha256"] = str(alias), adapter.sha(alias)
        self.build["source_hashes"][str(alias)] = pin["sha256"]
        self.write_runtime_build()
        self.assert_rejected("CANONICAL_PATH_REQUIRED")
        self.import_mock.assert_not_called()

    def test_catalog_update_rejects_renamed_schema_duplicate_keys_and_nonfinite(self):
        self.enable_catalog_update()
        for text, code in (
            ('{"name":"other","input_schema":{"type":"object"}}', "CATALOG_SCHEMA_INVALID"),
            ('{"name":"command_run","input_schema":{"type":"array"}}', "CATALOG_SCHEMA_INVALID"),
            ('{"type":"object","type":"object"}', "JSON_DUPLICATE_KEY"),
            ('{"type":"object","minimum":NaN}', "JSON_NONFINITE"),
        ):
            with self.subTest(code=code):
                self.catalog_source.write_text(text)
                self.build["source_hashes"][str(self.catalog_source)] = adapter.sha(self.catalog_source)
                self.build["catalog_updates"]["command_catalog_0"]["sha256"] = adapter.sha(self.catalog_source)
                self.write_runtime_build()
                self.assert_rejected(code)
        self.import_mock.assert_not_called()

    def test_catalog_source_and_destination_drift_reject_phase_without_restage(self):
        self.enable_catalog_update()
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        self.approve_environment(task)
        target = Path(adapter.document(self.new / "runtime-image.json")["artifacts"]["command_catalog_0"]["path"])
        for path in (self.catalog_source, target):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                self.assert_rejected("INPUT_DRIFT|PIN_DRIFT", lambda: phase.phase("preflight"))
                path.write_bytes(original)
                self.assertFalse((task.out / "adapter-preflight.started.json").exists())
        self.assertEqual(self.release.stage.call_count, 1)
        self.deploy.main.assert_not_called()

    def test_catalog_update_must_use_exact_staged_destination_and_single_save(self):
        self.enable_catalog_update()
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        image_path = self.new / "runtime-image.json"
        original = image_path.read_text()
        image = json.loads(original)
        target = Path(image["artifacts"]["command_catalog_0"]["path"])
        alternate = self.new / "runtime/another/schema.json"
        self.write(alternate, target.read_text())
        image["artifacts"]["command_catalog_0"]["path"] = str(alternate)
        image_path.write_text(json.dumps(image))
        self.assert_rejected("RUNTIME_DRIFT", phase.runtime_artifacts)
        image_path.write_text(original)
        phase.catalog_staged = True
        staging = self.new.with_name("." + self.new.name + ".staging")
        self.assert_rejected("CATALOG_STAGE_INVALID", lambda: phase.stage_save(adapter.save, staging / "runtime-image.json", image))

    def test_runtime_receipt_schema_status_sources_and_exact_bin_set(self):
        self.enable_runtime_update()
        original = json.loads(json.dumps(self.build))
        cases = (
            ("exit_code", 1, "RUNTIME_BUILD_FAILED"),
            ("exit_code", False, "RUNTIME_BUILD_FAILED"),
            ("external_model_calls", 1, "RUNTIME_BUILD_MODEL_CALLS"),
            ("external_model_calls", False, "RUNTIME_BUILD_MODEL_CALLS"),
            ("argv", [], "RUNTIME_BUILD_ARGV_INVALID"),
            ("argv", ["cargo", None], "RUNTIME_BUILD_ARGV_INVALID"),
            ("seconds", -1, "RUNTIME_BUILD_SECONDS_INVALID"),
            ("source_hashes", {}, "RUNTIME_BUILD_SOURCES_INVALID"),
            ("source_hashes", [], "RUNTIME_BUILD_SOURCES_INVALID"),
            ("source_hashes", {"relative.rs": "0" * 64}, "PATH_INVALID"),
            ("source_hashes", {str(self.rust_source): "0" * 64}, "INPUT_DRIFT"),
            ("sha256", "0" * 64, "INPUT_DRIFT"),
            ("binary_hashes", {k: v for k, v in original["binary_hashes"].items() if k != "tura_exec"},
             "RUNTIME_BUILD_BINS_INVALID"),
            ("binary_hashes", {**original["binary_hashes"], "extra": "0" * 64}, "RUNTIME_BUILD_BINS_INVALID"),
            ("binary_hashes", {**original["binary_hashes"], "tura_runtime": "0" * 64}, "INPUT_DRIFT"),
        )
        for key, value, code in cases:
            with self.subTest(key=key, value=value):
                self.build = {**original, key: value}
                self.write_runtime_build()
                self.assert_rejected(code)
        for field_change in ("missing", "extra"):
            self.build = dict(original)
            if field_change == "missing":
                self.build.pop("log")
            else:
                self.build["commands"] = {"apply": ["sh", "-c", "not executed"]}
            self.write_runtime_build()
            self.assert_rejected("RUNTIME_BUILD_FIELDS_INVALID")
        self.import_mock.assert_not_called()
        self.assertFalse((self.task_root / "deployment-evidence").exists())

    def test_runtime_update_requires_sha_bound_receipt_identity(self):
        self.enable_runtime_update()
        pin = dict(self.data["runtime_build"])
        for value in (None, self.build, {**pin, "commands": []}):
            self.data["runtime_build"] = value
            self.write_manifest()
            self.assert_rejected("RUNTIME_BUILD_IDENTITY_INVALID")
        self.import_mock.assert_not_called()
        self.assertFalse((self.task_root / "deployment-evidence").exists())

    def test_runtime_receipt_and_transitive_source_log_binary_drift_before_import(self):
        self.enable_runtime_update()
        for path in (self.build_file, self.build_log, self.rust_source, self.rust_lock,
                     self.transitive_source, *self.compiled.values()):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"drift\n")
                self.assert_rejected("INPUT_DRIFT")
                path.write_bytes(original)
        self.import_mock.assert_not_called()
        self.assertFalse((self.task_root / "deployment-evidence").exists())

    def test_runtime_build_requires_canonical_sources_log_and_existing_compiled_bins(self):
        self.enable_runtime_update()
        original = json.loads(json.dumps(self.build))
        alias = self.root / "source-alias.rs"
        alias.symlink_to(self.rust_source)
        for key, value in (("source_hashes", {str(alias): adapter.sha(self.rust_source)}),
                           ("log", str(self.rust_source.parent / ".." / "runtime.rs"))):
            self.build = {**original, key: value}
            self.write_runtime_build()
            self.assert_rejected("CANONICAL_PATH_REQUIRED|PATH_INVALID")
        self.build = original
        self.write_runtime_build()
        compiled = self.compiled["tura_exec"]
        moved = compiled.with_name("missing-tura_exec")
        compiled.rename(moved)
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        moved.rename(compiled)
        self.import_mock.assert_not_called()
        self.assertFalse((self.task_root / "deployment-evidence").exists())

    def test_runtime_identities_remain_bound_before_any_deployment_phase(self):
        self.enable_runtime_update()
        self.prepare()
        task = self.load()
        self.approve_environment(task)
        phase = adapter.Adapter(task)
        artifacts = adapter.document(self.new / "runtime-image.json")["artifacts"]
        paths = (self.build_file, self.build_log, self.rust_source, self.rust_lock, self.transitive_source,
                 *self.compiled.values(), *(Path(value["path"]) for value in artifacts.values()),
                 task.here / "rust-build.json", task.here / "staged-tests.log", self.new / "runtime-image.json")
        for path in paths:
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"drift\n")
                self.assert_rejected("INPUT_DRIFT|PIN_DRIFT", lambda: phase.phase("preflight"))
                path.write_bytes(original)
                self.assertFalse((task.out / "adapter-preflight.started.json").exists())
        self.deploy.main.assert_not_called()

    def test_runtime_image_rejects_missing_extra_bins_and_cache_references(self):
        self.enable_runtime_update()
        self.prepare()
        task = self.load()
        phase = adapter.Adapter(task)
        path = self.new / "runtime-image.json"
        original = path.read_bytes()
        for change in ("missing", "extra", "cache"):
            image = json.loads(original)
            if change == "missing":
                image["artifacts"].pop("tura_runtime")
            elif change == "extra":
                image["artifacts"]["unexpected"] = image["artifacts"]["tura_exec"]
            else:
                image["artifacts"]["tura_exec"] = adapter.identity(self.compiled["tura_exec"])
            path.write_text(json.dumps(image))
            self.assert_rejected("RUNTIME_DRIFT", phase.runtime_artifacts)
            path.write_bytes(original)
        self.deploy.main.assert_not_called()

    def test_runtime_tests_reject_unlisted_helper_drift(self):
        self.enable_runtime_update()
        original_stage = self.release.stage.side_effect

        def changed_helper():
            self.write(self.repo / "tests/helper.py", "unapproved changed dependency\n")
            original_stage()

        self.release.stage.side_effect = changed_helper
        self.assert_rejected("TEST_INPUT_DRIFT")
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse((self.task_root / "deployment-evidence/staged-tests.json").exists())
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())

    def test_runtime_suite_rejects_receipt_marked_reused(self):
        self.enable_runtime_update()

        def reused_receipt(*args):
            self.execute_fixture_tests(self.template, *args)
            if args[0] == "staged-tests":
                path = self.task_root / "deployment-evidence/staged-tests.json"
                receipt = adapter.document(path)
                receipt["execution_reused"] = True
                path.write_text(json.dumps(receipt))

        self.test_executions.side_effect = reused_receipt
        self.assert_rejected("TEST_RECEIPT_INVALID")
        self.assertFalse((self.task_root / "deployment-evidence/staged-tests-binding.json").exists())
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())

    def test_runtime_image_identity_cannot_drift_after_staged_suite(self):
        self.enable_runtime_update()
        original_stage = self.release.stage.side_effect

        def changed_image():
            original_stage()
            path = self.new / "runtime-image.json"
            image = adapter.document(path)
            image["unapproved_post_suite_change"] = True
            path.write_text(json.dumps(image))

        self.release.stage.side_effect = changed_image
        self.assert_rejected("TEST_INPUT_DRIFT")
        self.assertEqual(self.test_executions.call_count, 3)
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())

    def test_runtime_stage_requires_actual_new_image_suite(self):
        self.enable_runtime_update()
        original_stage = self.release.stage.side_effect

        def skipped_tests():
            original_tests = self.release.tests
            self.release.tests = Mock()
            try:
                original_stage()
            finally:
                self.release.tests = original_tests

        self.release.stage.side_effect = skipped_tests
        self.assert_rejected("RUNTIME_TEST_EXECUTION_REQUIRED")
        self.assertEqual(self.test_executions.call_count, 2)
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())

    def test_runtime_verify_requires_actual_live_suite_after_adoption(self):
        self.enable_runtime_update()
        self.use_transaction_fixture()
        self.prepare()
        task = self.load()
        self.approve_environment(task)
        for phase in ("preflight", "apply"):
            adapter.run(self.load(), phase)
        original_main = self.deploy.main

        def skipped_live_tests(phase):
            self.deploy.tests = Mock()
            return original_main(phase)

        self.deploy.main = skipped_live_tests
        self.assert_rejected("RUNTIME_TEST_EXECUTION_REQUIRED", lambda: adapter.run(self.load(), "verify"))
        self.assertEqual(self.entry.resolve(), self.new / "caller/bin/nokiy-embedded-run")
        self.assertTrue((task.out / "adapter-verify.started.json").is_file())
        self.assertFalse((task.out / "adapter-verify.completed.json").exists())
        self.assertFalse((task.here / "live-tests.json").exists())

    def test_caller_only_empty_modules_still_rejected(self):
        self.data["modules"] = []
        self.write_manifest()
        self.assert_rejected("MODULES_INVALID")
        self.import_mock.assert_not_called()

    def test_caller_only_retains_runtime_unchanged_record_and_suite_reuse(self):
        self.prepare()
        task = self.load()
        self.assertEqual(self.release.RUNTIME_BINS, ())
        self.assertEqual(self.deploy.RUNTIME_BINS, ())
        self.assertTrue(adapter.document(task.here / "adoption.json")["unchanged_runtime"])
        self.assertTrue((task.here / "runtime-unchanged.json").is_file())
        build = adapter.document(task.here / "rust-build.json")
        self.assertFalse(build["executed_build"])
        self.assertEqual(build["binary_hashes"], {})
        self.assertTrue(adapter.document(task.here / "staged-tests.json")["execution_reused"])
        self.assertEqual(self.test_executions.call_count, 2)

    def test_caller_only_still_rejects_runtime_changes(self):
        original_stage = self.release.stage.side_effect

        def changed_runtime():
            original_stage()
            path = self.new / "runtime/tura_router"
            self.write(path, "unrequested new runtime\n")
            image_path = self.new / "runtime-image.json"
            image = adapter.document(image_path)
            image["artifacts"]["tura_router"] = adapter.identity(path)
            image_path.write_text(json.dumps(image))

        self.release.stage.side_effect = changed_runtime
        self.assert_rejected("RUNTIME_DRIFT")
        self.assertFalse((self.task_root / "deployment-evidence/cutover/plan.json").exists())
        self.deploy.main.assert_not_called()


if __name__ == "__main__":
    unittest.main()
