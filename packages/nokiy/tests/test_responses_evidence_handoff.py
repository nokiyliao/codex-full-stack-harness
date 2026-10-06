# SPDX-License-Identifier: MIT
"""Task-local real-runtime ordered evidence handoff test candidate.

Unintegrated: parent owns syntax/runtime tests, integration and acceptance.
Loopback HTTP is valid; this fixture uses the direct Codex Responses SSE protocol.
The native launch fence is strengthened to deny auth.json reads and cloud traffic.

Set NOKIY_RUNTIME_EVIDENCE_HANDOFF_IMAGE to an installed frozen runtime-image.json.
An optional NOKIY_RUNTIME_EVIDENCE_HANDOFF_IMAGE_SHA256 pins that manifest too.
Absent native/runtime capabilities skip; supplied image drift and regressions fail.
These tests establish ordered execution/evidence, not model quality, billing or speed.
"""
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_full_core as fixtures
from test_embedded_nokiy import file_sha256
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core as core, local_context
from codex_collaboration_harness import result_inspection as inspector


IMAGE_ENV = "NOKIY_RUNTIME_EVIDENCE_HANDOFF_IMAGE"
MODEL = "gpt-6.1-sol"
CODEX_RESPONSES_PATH = "/backend-api/codex/responses"
DUMMY_API_KEY = "loopback-fixture-not-a-secret"
DUMMY_ACCOUNT_ID = "loopback-fixture-account"
NATIVE_SIGNAL_FENCE = "(version 1) (allow default) (deny signal) (allow signal (target same-sandbox))"
REFUSAL = "The focused verifier failed; completion is withheld."
NATIVE_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
MISSING_CAPABILITIES = {
    "NOKIY_FULL_CORE_COMMAND_RECEIPT_BINDING_REQUIRED",
    "NOKIY_FULL_CORE_EXECUTION_BUDGET_REQUIRED",
    "NOKIY_FULL_CORE_INITIAL_TASK_STATE_REQUIRED",
    "NOKIY_FULL_CORE_VERIFIER_PARENT_CHANNEL_REQUIRED",
}


def _command(kind, line, step):
    return {"command_type": kind, "command_line": line, "step": step}


def _tool_message(commands, index):
    return {"id": f"fc-scripted-command-{index}", "call_id": f"scripted-command-{index}",
            "type": "function_call", "status": "completed", "name": "command_run",
            "arguments": json.dumps({"commands": commands})}


@contextmanager
def _loopback_provider(messages, *, stream_gate=None):
    """Codex Responses SSE output, never forged execution/terminal events."""
    requests, request_bodies, errors = [], [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_args):
            pass  # In particular, never retain Authorization headers.

        def do_POST(self):
            index = len(requests)
            requests.append(self.path)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8 * 1024 * 1024:
                    raise ValueError("invalid provider request size")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("provider request must be an object")
                request_bodies.append(body)  # In-memory model input only; never headers.
                if self.path != CODEX_RESPONSES_PATH:
                    raise ValueError("unexpected provider endpoint")
                reasoning = body.get("reasoning")
                if (body.get("stream") is not True or body.get("model") != MODEL
                        or not isinstance(reasoning, dict) or reasoning.get("effort") != "max"
                        or "service_tier" in body):  # Native default serializes as omission.
                    raise ValueError("unexpected Codex fixture request settings")
                if (self.headers.get("Authorization") != f"Bearer {DUMMY_API_KEY}"
                        or self.headers.get("ChatGPT-Account-Id") != DUMMY_ACCOUNT_ID):
                    raise ValueError("unexpected fixture identity")
                if index >= len(messages):
                    errors.append("unscripted provider request")
                    self.send_error(400, "Unscripted provider request")
                    self.close_connection = True
                    return
                item = messages[index]
                response = {"id": f"resp-loopback-{index}", "object": "response", "created_at": 1,
                            "status": "in_progress", "model": MODEL, "output": [], "usage": None}
                added = {**item, "status": "in_progress"}
                if item["type"] == "function_call":
                    added["arguments"] = ""
                else:
                    added["content"] = []
                chunks = [
                    {"type": "response.created", "response": response},
                    {"type": "response.in_progress", "response": response},
                    {"type": "response.output_item.added", "output_index": 0, "item": added},
                ]
                common = {"item_id": item["id"], "output_index": 0}
                if item["type"] == "function_call":
                    chunks.extend([
                        {**common, "type": "response.function_call_arguments.delta",
                         "delta": item["arguments"]},
                        {**common, "type": "response.function_call_arguments.done",
                         "name": item["name"], "arguments": item["arguments"]},
                    ])
                else:
                    part = item["content"][0]
                    common["content_index"] = 0
                    chunks.extend([
                        {**common, "type": "response.content_part.added",
                         "part": {**part, "text": ""}},
                        {**common, "type": "response.output_text.delta", "delta": part["text"]},
                        {**common, "type": "response.output_text.done", "text": part["text"]},
                        {**common, "type": "response.content_part.done", "part": part},
                    ])
                # Synthetic usage exercises protocol parsing only; it is not billing evidence.
                usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                         "input_tokens_details": {"cached_tokens": 0},
                         "output_tokens_details": {"reasoning_tokens": 0}}
                chunks.extend([
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {
                        **response, "status": "completed", "output": [item], "usage": usage}},
                ])
                frames = [
                    ("event: " + chunk["type"] + "\ndata: " +
                     json.dumps({**chunk, "sequence_number": sequence}) + "\n\n").encode()
                    for sequence, chunk in enumerate(chunks)]
                raw = b"".join(frames)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                if stream_gate is None or index != 0:
                    self.wfile.write(raw)
                else:
                    # Hold only response.completed; complete function-call arguments
                    # and output_item.done are already flushed to the real runtime.
                    self.wfile.write(b"".join(frames[:-1]))
                    self.wfile.flush()
                    try:
                        stream_gate()
                    finally:
                        # Release the tail even if observation/assertion fails.
                        self.wfile.write(frames[-1])
                        self.wfile.flush()
            except (OSError, ValueError, TypeError) as error:
                errors.append({"type": type(error).__name__, "reason": str(error),
                               "settings": {key: body.get(key) for key in
                                            ("model", "stream", "reasoning", "service_tier")}
                               if isinstance(locals().get("body"), dict) else None})
                self.send_error(400, "Fixture request validation failed")
                self.close_connection = True

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              name="handoff-loopback-provider", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, errors, request_bodies
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        if thread.is_alive():
            raise AssertionError("loopback provider cleanup unproven")


class RuntimeEvidenceHandoffTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            self.skipTest("real full-core tests require the Darwin process fence")
        self.native_environment = {key: os.environ.get(key)
                                   for key in ("HOME", "CODEX_HOME", "CODEX_THREAD_ID")}
        if not self.native_environment["CODEX_THREAD_ID"]:
            self.skipTest("real full-core tests require the current native thread identity")
        selected = os.environ.get(IMAGE_ENV)
        if not selected:
            self.skipTest(f"set {IMAGE_ENV} to an installed frozen full-core image")
        try:
            image_path = Path(selected).expanduser().resolve(strict=True)
        except FileNotFoundError:
            self.skipTest(f"{IMAGE_ENV} names an unavailable installed image")
        self.source_manifest = json.loads(image_path.read_text())
        if not core.REQUIRED.issubset(self.source_manifest.get("artifacts", {})):
            self.skipTest("selected image lacks the full exec/runtime/router/session_db inventory")
        self.installed_identity = caller.FileIdentity(
            image_path, os.environ.get(IMAGE_ENV + "_SHA256") or file_sha256(image_path))
        self.installed = caller.verify_runtime_image(self.installed_identity,
                                                    required_artifacts=core.REQUIRED)
        for name in ("tura_exec", "tura_runtime", "tura_router", "tura_session_db"):
            path = self.installed.artifacts[name].path
            with path.open("rb") as stream:
                native = stream.read(4) in NATIVE_MAGIC
            if not native or not os.access(path, os.X_OK):
                self.skipTest(f"{name} is not a real executable native runtime artifact")
        if b"nokiy_terminal_evidence_v1" not in self.installed.artifacts["tura_exec"].path.read_bytes():
            self.skipTest("installed exec lacks evidence-only terminal capability")
        if b"TURA_NOKIY_EVIDENCE_ONLY_TERMINAL" not in self.installed.artifacts["tura_runtime"].path.read_bytes():
            self.skipTest("installed runtime lacks evidence-only terminal capability")

        helper = fixtures.FullCoreTests()
        try:
            helper.setUp()
        finally:
            for key, value in self.native_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(helper.doCleanups)
        self.f = helper.f
        self.f.native_thread_id = self.native_environment["CODEX_THREAD_ID"]
        # Never execute the helper's synthetic binaries. Use a separate complete
        # inventory, including all extra immutable artifacts in the installed image.
        self.f.runtime = self.f.root / "installed-loopback-runtime"
        self.f.runtime.mkdir()
        self.paths = {}
        for name, identity in self.installed.artifacts.items():
            destination = self.f.runtime / identity.path.relative_to(self.installed.runtime_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(identity.path, destination)
            self.assertEqual(file_sha256(destination), identity.sha256)
            self.paths[name] = destination
        self.f.runtime_image = self.f.root / "real-loopback-image.json"

    def _request(self, base_url, *, preload=False):
        routes = {"thinking", "fast", "codex/gpt-5.5", "codex/gpt-5.6",
                  "codex/gpt-5.6-sol", "codex/gpt-5.6-terra", "codex/gpt-5.6-luna",
                  "embedding_high", "embedding_low", "codex/" + MODEL}
        # Installed artifacts may be read-only; only this copied config changes.
        self.paths["provider_config"].chmod(0o600)
        self.paths["provider_config"].write_text(json.dumps({
            "provider_base_url": {"codex": base_url}, "routes": {
                route: {"default_temperature": 0.0, "providers": [
                    {"provider": "codex", "model": MODEL, "temperature": 0.0}]}
                for route in sorted(routes)}}))
        manifest = dict(self.source_manifest, runtime_root=str(self.f.runtime), artifacts={
            name: {"path": str(path), "sha256": file_sha256(path), "size": path.stat().st_size}
            for name, path in self.paths.items()})
        self.f.runtime_image.write_text(json.dumps(manifest))

        (self.f.workspace / "answer.txt").write_text("broken\n")
        self.scratch = self.f.artifacts / "verifier"
        self.scratch.mkdir()
        script = self.f.workspace / "verify.py"
        script.write_text(
            "from pathlib import Path\nimport sys\n"
            f"scratch = Path({str(self.scratch)!r})\n"
            "answer = Path('answer.txt').read_text()\n"
            "runs = scratch / 'runs'\n"
            "runs.write_text(runs.read_text() + 'x' if runs.exists() else 'x')\n"
            "(scratch / 'observed-answer.txt').write_text(answer)\n"
            "print('public-verifier: ' + repr(answer), flush=True)\n"
            "sys.exit(0 if answer == 'fixed\\n' else 1)\n")
        python = Path(sys.executable).resolve()
        action = {
            "mission": {"mission_id": "real-runtime-handoff", "task_id": "ordered-verifier",
                        "mode": "DELIVERY", "objective": "Prove real ordered verifier handoff",
                        "current_predicate": "real ordered handoff not yet observed"},
            "context_summary": "Disposable fixture; loopback scripted model only. Parent owns acceptance.",
            "operations": ["read", "command", "modify"], "source_read": True,
            "read_scopes": ["answer.txt", "verify.py"], "write_scopes": ["answer.txt"],
            "target_paths": ["answer.txt"], "command_templates": [],
            "forbidden_effects": ["network", "delete"],
            "verifier_commands": [{"argv": [str(python), str(script)],
                "executable_sha256": file_sha256(python),
                "pinned_files": [{"path": str(script), "sha256": file_sha256(script)}],
                "timeout_seconds": 5, "scratch_root": str(self.scratch), "network": False}],
        }
        if preload:
            source = self.f.workspace / "references" / "preload_source.py"
            source.parent.mkdir()
            text = ("# preload-provider-wire-section-89d38a8eaaaa6571\n"
                    "EXPECTED_ANSWER = 'fixed\\n'\n")
            source.write_text("# excluded-provider-wire-prefix\n" + text
                              + "# excluded-provider-wire-suffix\n")
            self.preloaded_section = {
                "path": "references/preload_source.py", "start_line": 2, "end_line": 3,
                "text": text, "source_sha256": file_sha256(source),
                "section_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            action["read_scopes"].append(self.preloaded_section["path"])
            action["read_directories"] = ["references"]
            # Preload is evidence, not an authorization change, even with mixed scopes.
            _, unpreloaded_contract = local_context.compile_context(
                self.f.workspace, action, artifact_root=self.f.artifacts)
            action["source_sections"] = [{key: self.preloaded_section[key]
                                          for key in ("path", "start_line", "end_line")}]
        capsule, self.contract = local_context.compile_context(
            self.f.workspace, action, artifact_root=self.f.artifacts)
        if preload:
            self.assertEqual(self.contract["authorization_semantic_sha256"],
                             unpreloaded_contract["authorization_semantic_sha256"])
        self.f.context.write_text(json.dumps(capsule))
        self.f.jspace.write_text(json.dumps(self.contract))
        path = self.f._write_request(prompt="Apply the correction, run the admitted verifier, then finish.")
        value = json.loads(path.read_text())
        value.update(execution_profile="direct", authority_effect="workspace", model=MODEL,
                     reasoning_effort="max", service_tier="default",
                     terminal_delivery="evidence_only", timeout_seconds=120,
                     max_trajectory_bytes=4 * 1024 * 1024, max_result_bytes=1024,
                     initial_task_state={"task_group": "runtime verifier tests", "task_type": ["debug"]})
        path.write_text(json.dumps(value))
        return caller.load_request(path)

    def _execute(self, *, separate_done=False, failing=False, stream_gate=False, recovery=False,
                 json_projection=False, preload=False):
        final = "still-broken\n" if failing and not recovery else "fixed\n"
        first = "still-broken\n" if recovery else final
        preimage = "broken\n"
        prefix = []
        if json_projection:
            preimage = json.dumps({"focused_verifiers": [], "padding": "x" * 16000},
                                  separators=(",", ":")) + "\n"
            source_sha256 = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
            prefix = [
                _command("apply_patch", "*** Begin Patch\n*** Update File: answer.txt\n@@\n"
                         "-broken\n+" + preimage + "*** End Patch\n", 1),
                _command("source_read", json.dumps({
                    "path": "answer.txt", "json_pointer": "/focused_verifiers",
                    "expected_sha256": source_sha256}), 2),
            ]
        work = prefix + [
            _command("apply_patch", "*** Begin Patch\n*** Update File: answer.txt\n@@\n"
                     "-" + preimage + "+" + first + "*** End Patch\n", len(prefix) + 1),
            _command("focused_verifier", json.dumps({"verifier_index": 0}), len(prefix) + 2),
        ]
        done = _command("task_status", json.dumps({"status": "done"}), len(work) + 1)
        messages = ([_tool_message(work, 0), _tool_message([done], 1)] if separate_done
                    else [_tool_message(work + [done], 0)])
        if recovery:
            repair = [
                _command("apply_patch", "*** Begin Patch\n*** Update File: answer.txt\n@@\n"
                         "-still-broken\n+fixed\n*** End Patch\n", 1),
                _command("focused_verifier", json.dumps({"verifier_index": 0}), 2),
            ]
            # The failed batch must fence its done; repair continues in the same
            # request/session, not a fresh caller launch or a synthetic terminal.
            messages = [_tool_message(work + [done], 0), _tool_message(repair + [done], 1)]
        elif failing:
            # If the runtime seeks feedback after fencing step 3, allow exactly
            # one refusal response, not another done or a synthetic terminal event.
            messages.append({"id": "msg-scripted-refusal", "type": "message", "role": "assistant",
                             "status": "completed", "content": [
                                 {"type": "output_text", "text": REFUSAL, "annotations": []}]})
        overlap = {"patch_before_tail": False}

        def observe_patch_before_tail():
            deadline = time.monotonic() + 10  # Liveness bound, not a timing benchmark.
            answer = self.f.workspace / "answer.txt"
            while time.monotonic() < deadline:
                try:
                    overlap["answer_bytes"] = answer.read_bytes()
                except FileNotFoundError:
                    overlap["answer_bytes"] = None
                if overlap["answer_bytes"] == final.encode():
                    overlap["patch_before_tail"] = True
                    return
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.01, remaining))

        # Preserve native caller identity without copying credentials. Dummy values
        # do not replace the parent's auth.json-read/non-loopback outbound denial fence.
        # The copied provider configuration selects only the Codex Responses stub.
        environment = {key: os.environ[key] for key in
                       ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR") if key in os.environ}
        environment.update({key: value for key, value in self.native_environment.items()
                            if value is not None})
        environment.update(PYTHONPATH=str(Path(caller.__file__).resolve().parent.parent),
                           OPENAI_API_KEY=DUMMY_API_KEY, OPENAI_ACCOUNT_ID=DUMMY_ACCOUNT_ID,
                           OPENAI_TOKEN_EXPIRES="4102444800000", NO_PROXY="127.0.0.1,localhost")
        with _loopback_provider(messages, stream_gate=observe_patch_before_tail if stream_gate else None) \
                as (base_url, requests, errors, request_bodies), \
                patch.dict(os.environ, {**environment,
                                       "OPENAI_CODEX_ENDPOINT": base_url + CODEX_RESPONSES_PATH},
                           clear=True):
            request = self._request(base_url, preload=preload)
            ready = caller.preflight(request)
            if ready.get("first_typed_blocker") in MISSING_CAPABILITIES:
                self.skipTest(ready["first_typed_blocker"])
            self.assertEqual(ready["status"], "READY", ready)
            self.assertEqual(requests, [])
            if stream_gate:
                self.assertEqual((self.f.workspace / "answer.txt").read_bytes(), b"broken\n")
            # Strengthen the actual native launch fence, never replace its rules.
            # A nested outer sandbox cannot apply Nokiy's same-sandbox signal rule.
            auth = Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")) / "auth.json"
            denied = " ".join("(literal " + json.dumps(path) + ")" for path in
                              sorted({str(auth), str(auth.resolve(strict=True))}))
            restriction = (" (deny file-read* file-write* " + denied + ")"
                           '(deny network-outbound)(allow network-outbound '
                           '(remote ip "localhost:*") (remote unix-socket))')
            original_run = caller._run_process
            fences = []

            def restricted_run(command, **kwargs):
                self.assertEqual(command[:3], ["/usr/bin/sandbox-exec", "-p", NATIVE_SIGNAL_FENCE])
                command = list(command)
                command[2] += restriction
                fences.append(command[2])
                return original_run(command, **kwargs)

            with patch.object(caller, "_run_process", side_effect=restricted_run):
                terminal = caller.execute(request)
            self.assertEqual(len(fences), 1)
            run = request.artifact_root / request.request_id
            self.assertEqual(caller.read_terminal(request.artifact_root, request.request_id), terminal)
            self.assertTrue((run / "core.jsonl").is_file(), {
                "terminal": terminal, "provider_requests": requests, "provider_errors": errors})
            events = [json.loads(line) for line in (run / "core.jsonl").read_text().splitlines()]
            self.diagnostics = {"stderr": (run / "core.stderr").read_text(errors="replace")[-4000:],
                                "events": events, "provider_errors": errors}
            inspected = inspector.inspect(request.artifact_root, request.request_id, request.native_thread_id)
            self.assertTrue(terminal["cleanup_pass"], terminal)
            self.assertTrue(terminal["cleanup"]["engine_reaped"])
            self.assertTrue(terminal["cleanup"]["no_live_descendants"])
            self.assertEqual(terminal["cleanup"]["parent_verifier"]["calls"], 2 if recovery else 1, {
                "status": terminal["status"], "blocker": terminal.get("first_typed_blocker"),
                "result": terminal.get("result_text"), "provider_requests": requests,
                "provider_errors": errors,
                "stderr": (run / "core.stderr").read_text(errors="replace")[-2500:],
                "events": events[-3:],
            })
            self.assertTrue(terminal["cleanup"]["parent_verifier"]["cleanup_pass"])
            self.assertFalse(terminal["cleanup"]["parent_verifier"]["channel_failure"])
            self.assertFalse((self.f.workspace / ".tura").exists())
            self.assertFalse((self.scratch / ".tura").exists())
            self.assertEqual((self.f.workspace / "answer.txt").read_text(), final)
            self.assertEqual((self.scratch / "observed-answer.txt").read_text(), final)
            self.assertEqual((self.scratch / "runs").read_text(), "xx" if recovery else "x")
            count = len(requests)
            self.assertEqual(caller.execute(request), terminal)  # Read-only same-request recovery.
            self.assertEqual(len(requests), count)
            self.assertEqual((self.scratch / "runs").read_text(), "xx" if recovery else "x")
            self.assertEqual(errors, [])
            self.assertEqual(len(request_bodies), len(requests))
            caller.verify_runtime_image(request.runtime_image, required_artifacts=core.REQUIRED)
            caller.verify_runtime_image(self.installed_identity, required_artifacts=core.REQUIRED)
            for name, identity in self.installed.artifacts.items():
                if name != "provider_config":
                    self.assertEqual(file_sha256(self.paths[name]), identity.sha256)
        self.provider_request_bodies = request_bodies
        if stream_gate:
            self.assertTrue(overlap["patch_before_tail"], {
                "assertion": "patch must execute before response.completed is released",
                "observation": overlap})
        return request, terminal, events, inspected, count

    def _outputs(self, events):
        for index, event in enumerate(events):
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "command_execution":
                output = core._structured_output(item)
                yield index, output
                for entry in output.get("results", []):
                    if isinstance(entry, dict) and isinstance(entry.get("output"), dict):
                        yield index, entry["output"]

    def _verifier_receipt(self, request, events, exit_code, *, ordinal=0, expected_calls=1):
        observations = [(index, output) for index, output in self._outputs(events)
                        if output.get("executor") == "parent_focused_verifier"]
        self.assertEqual(len(observations), expected_calls, observations)
        index, output = observations[ordinal]
        receipt = output["terminal_receipt"]
        self.assertEqual(output["exit_code"], exit_code)
        self.assertEqual(receipt["schema_version"], "tura_command_terminal_receipt_v1")
        self.assertEqual(receipt["termination_origin"], "parent_verifier")
        self.assertEqual(receipt["outcome"], "known")
        self.assertEqual(receipt["exit_code"], exit_code)
        self.assertEqual(receipt["terminal_state"], "failed" if exit_code else "completed")
        for key in ("process_reaped", "process_group_empty", "termination_proven"):
            self.assertIs(receipt[key], True)
        path = Path(output["terminal_receipt_path"])
        self.assertEqual(path.parent, request.artifact_root / request.request_id /
                         "execution-state" / "command_receipts")
        self.assertEqual(json.loads(path.read_text()), receipt)
        if exit_code == 0:
            self.assertEqual(output["verification_evidence"]["call_id"], receipt["call_id"])
            self.assertEqual(output["verification_evidence"]["authorization_semantic_sha256"],
                             self.contract["authorization_semantic_sha256"])
        else:
            self.assertNotIn("verification_evidence", output)
        return index

    def _assert_persisted_session(self, request, marker):
        # Read-only lookup through the real DB's existing runtime-location index.
        # A CLI marker alone must not stand in for a persisted same-session closeout.
        root = request.artifact_root / request.request_id / "execution-state" / "session_log"
        index = root / "index.sqlite3"
        self.assertTrue(index.is_file())
        self.assertFalse(index.is_symlink())
        with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(
                "SELECT workspace_db_path FROM runtime_locations WHERE runtime_id = ? AND session_id = ?",
                (marker["runtime_id"], marker["session_id"])).fetchall()
        self.assertEqual(len(rows), 1, rows)
        path = Path(rows[0][0])
        self.assertTrue(path.is_absolute() and path.is_relative_to(root), path)
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.resolve(strict=True), path)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute(
                "SELECT session_id, lease_active, terminal FROM runtimes WHERE runtime_id = ?",
                (marker["runtime_id"],)).fetchone()
        self.assertEqual(row, (marker["session_id"], 0, 1))

    def _assert_success(self, result, expected_requests, *, recovery=False):
        request, terminal, events, inspected, count = result
        self.assertEqual(count, expected_requests)
        self.assertEqual(terminal["status"], "RESULT_AVAILABLE", self.diagnostics)
        if recovery and inspected["command_success"] == "known_failure":
            # Inspection retains historical failures; later resolution is proved
            # by the authenticated passing receipt and persisted done below.
            self.assertNotEqual(inspected["status"], "EVIDENCE_VERIFIED", inspected)
            self.assertEqual(inspected["first_blocker"], "COMMAND_FAILURE", inspected)
        else:
            self.assertEqual(inspected["status"], "EVIDENCE_VERIFIED", inspected)
            self.assertEqual(inspected["command_success"], "known_success")
        self.assertEqual(inspected["file_change_success"], "known_success")
        self.assertEqual(terminal["mission_acceptance"], "parent_owned")
        self.assertEqual(terminal["observed_terminal_delivery"], "evidence_only")
        marker = terminal["terminal_evidence"]
        self.assertEqual(marker["session_id"], "full-" + request.request_sha256)
        self.assertTrue(marker["runtime_id"])
        self.assertEqual(marker["terminal_status"], "done")
        self.assertIs(marker["parent_acceptance_required"], True)
        self.assertIs(marker["final_summary_turn_executed"], False)
        self._assert_persisted_session(request, marker)
        # Read the real persisted marker and task transition, not provider text.
        markers = [(n, event) for n, event in enumerate(events)
                   if event.get("type") == "nokiy.terminal_evidence"]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0][1], marker)
        turns = [event for event in events if event.get("type") == "turn.completed"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["session_id"], marker["session_id"])
        verifier_index = self._verifier_receipt(
            request, events, 0, ordinal=1 if recovery else 0, expected_calls=2 if recovery else 1)
        edits = [n for n, event in enumerate(events) if event.get("type") == "item.completed"
                 and event.get("item", {}).get("type") == "file_change"]
        done = [n for n, output in self._outputs(events)
                if output.get("task_status", {}).get("status") == "done"]
        self.assertTrue(edits)
        if recovery:
            failed_index = self._verifier_receipt(request, events, 1, expected_calls=2)
            self.assertEqual(len(edits), 2)
            self.assertLess(edits[0], failed_index)
            self.assertLess(failed_index, edits[1])
        self.assertEqual(len(done), 1)
        self.assertLess(max(edits), verifier_index)
        self.assertLess(verifier_index, done[0])
        self.assertLess(done[0], markers[0][0])
        # The existing CLI emits an empty final_text envelope even without prose.
        # It is not another provider response or a model-authored recap.
        replies = [event["item"] for event in events
                   if event.get("item", {}).get("type") == "assistant_message"]
        self.assertEqual(replies, [{"type": "assistant_message", "text": ""}])

    def test_preload_and_mixed_scopes_reach_provider_in_one_response(self):
        result = self._execute(preload=True)
        self._assert_success(result, 1)
        self.assertEqual(len(self.provider_request_bodies), 1)
        body = self.provider_request_bodies[0]
        self.assertIsInstance(body["input"], list)
        input_texts = [part["text"] for item in body["input"]
                       for part in item.get("content", [])
                       if isinstance(part, dict) and isinstance(part.get("text"), str)]
        self.assertTrue(input_texts)
        if isinstance(body.get("instructions"), str):
            input_texts.append(body["instructions"])
        wire_text = "\n".join(input_texts)

        # Parse the presentation delivered to the actual Responses boundary, not
        # capsule.json or prompt.txt. A unique token also detects duplicate input.
        capsule_marker = "Presentation: task_context_presentation_v1\n"
        self.assertEqual(wire_text.count(capsule_marker), 1)
        wire_capsule, _ = json.JSONDecoder().raw_decode(
            wire_text.split(capsule_marker, 1)[1].lstrip())
        self.assertEqual(wire_capsule["context_summary"]["source_sections"],
                         [self.preloaded_section])
        self.assertEqual(wire_capsule["dcf_generation"]["source_sections"],
                         [{key: value for key, value in self.preloaded_section.items()
                           if key != "text"}])
        self.assertEqual(wire_text.count("preload-provider-wire-section-89d38a8eaaaa6571"), 1)
        self.assertNotIn("excluded-provider-wire-prefix", wire_text)
        self.assertNotIn("excluded-provider-wire-suffix", wire_text)

        projection_marker = "Verified execution capability (caller projection):\n"
        self.assertEqual(wire_text.count(projection_marker), 1)
        projection, _ = json.JSONDecoder().raw_decode(
            wire_text.split(projection_marker, 1)[1].lstrip())
        self.assertEqual(projection["jspace_semantic_sha256"],
                         self.contract["authorization_semantic_sha256"])
        self.assertEqual(wire_capsule["jspace_semantic_sha256"],
                         self.contract["authorization_semantic_sha256"])
        self.assertIs(projection["source_read"], True)
        self.assertEqual(projection["read_scopes"],
                         ["answer.txt", "references/**", "references/preload_source.py", "verify.py"])
        self.assertEqual(projection["read_scopes"], self.contract["read_scopes"])
        self.assertCountEqual(projection["allowed_operations"], ["read", "command", "modify"])
        self.assertEqual(projection["allowed_operations"], self.contract["allowed_operations"])
        self.assertEqual(projection["denied_operations"], ["create", "delete", "network"])
        self.assertEqual(projection["denied_operations"], self.contract["denied_operations"])
        self.assertEqual(self.contract["write_scopes"], ["answer.txt"])
        self.assertEqual(self.contract["declared_targets"], ["answer.txt"])
        self.assertEqual(self.contract["command_templates"], [])
        self.assertEqual(projection["read_commands"]["roots"], ["references"])
        for name in ("rg", "cat"):
            self.assertEqual(projection["read_commands"][name], self.contract["read_commands"][name])

        for guidance in (
                "Capsule: evidence, not a read grant.",
                "directory scopes never qualify.",
                "No new read/write authority.",
                "Optional source_postimages: known exit0/reaped/group-empty success only.",
                "Missing/overflow/stale/uncovered evidence: ordinary granted source_read.",
                "Preserve raw stdout/stderr, verifier/receipt/cleanup facts; no new authority, "
                "acceptance or automatic done.",
                "Task-permitted mixed responses: Once final patches are fully known, "
                "exact-write patches precede the admitted focused_verifier "
                "at a strictly later positive step in the same command_run response.",
                "Mechanical missing readbacks only; mandatory task_status done "
                "at a strictly later positive step in the same existing command_run.",
                "No output-dependent decisions; safe exact read+write paths only.",
                "no failed/unknown-effect closure or expanded grants.",
                "Checks must pass before done executes, not before proposing.",
                "Scope/effect/receipt gates still apply."):
            self.assertIn(guidance, wire_text)

        # The real batch contains only patch -> verifier -> done. Existing success
        # assertions check authenticated receipts, ordering, same-session closeout,
        # no final summary turn, cleanup and read-only recovery without a new request.
        commands = [json.loads(event["item"]["command"]) for event in result[2]
                    if event.get("type") == "item.completed"
                    and event.get("item", {}).get("type") == "command_execution"]
        self.assertEqual(commands, [{"verifier_index": 0}, {"status": "done"}])
        edits = [event for event in result[2]
                 if event.get("type") == "item.completed"
                 and event.get("item", {}).get("type") == "file_change"]
        self.assertEqual(len(edits), 1)
        self.assertFalse(any(output.get("mode") in ("range", "search", "json_projection")
                             for _, output in self._outputs(result[2])))

    def test_json_projection_crosses_router_and_runtime_in_one_response(self):
        result = self._execute(json_projection=True)
        self._assert_success(result, 1)
        projections = [output for _, output in self._outputs(result[2])
                       if "json_pointer" in output]
        self.assertEqual(len(projections), 1, self.diagnostics)
        output = projections[0]
        source = (json.dumps({"focused_verifiers": [], "padding": "x" * 16000},
                             separators=(",", ":")) + "\n").encode("utf-8")
        self.assertEqual(output["mode"], "json_projection")
        self.assertEqual(output["stdout"], "[]")
        self.assertEqual(output["json_pointer"], "/focused_verifiers")
        self.assertEqual(output["path"], "answer.txt")
        self.assertEqual(output["source_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(output["file_bytes"], len(source))
        self.assertGreater(len(source), 12288)
        self.assertIs(output["truncated"], False)
        self.assertEqual(output["exit_code"], 0)
        receipt = output["terminal_receipt"]
        self.assertEqual(receipt["schema_version"], "tura_command_terminal_receipt_v1")
        self.assertEqual(receipt["terminal_state"], "completed")
        self.assertEqual(receipt["exit_code"], 0)
        for key in ("start_line", "end_line", "requested_end_line", "next_line",
                    "total_lines", "line_numbers"):
            self.assertNotIn(key, output)

    def test_ordered_patch_verifier_done_needs_one_provider_response(self):
        self._assert_success(self._execute(), 1)

    def test_failed_terminal_batch_repairs_and_finishes_in_same_session(self):
        self._assert_success(self._execute(recovery=True), 2, recovery=True)

    def test_separate_done_control_needs_one_extra_provider_response(self):
        self._assert_success(self._execute(separate_done=True), 2)

    def test_patch_executes_before_provider_response_completed(self):
        self._assert_success(self._execute(separate_done=True, stream_gate=True), 2)

    def test_failed_verifier_fences_done_and_successful_terminal_evidence(self):
        request, terminal, events, inspected, count = self._execute(failing=True)
        self._verifier_receipt(request, events, 1)
        self.assertFalse(any(output.get("task_status", {}).get("status") == "done"
                             for _, output in self._outputs(events)))
        self.assertNotEqual(inspected["status"], "EVIDENCE_VERIFIED", inspected)
        self.assertEqual(inspected["command_success"], "known_failure", inspected)
        self.assertEqual(inspected["first_blocker"], "COMMAND_FAILURE", inspected)
        self.assertFalse(any(event.get("type") == "nokiy.terminal_evidence"
                            and event.get("terminal_status") == "done" for event in events))
        marker = terminal.get("terminal_evidence")
        if marker is not None:
            self.assertEqual(marker["session_id"], "full-" + request.request_sha256)
            self.assertEqual(marker["terminal_status"], "blocked")
        refusal_observed = any(event.get("item", {}).get("type") == "assistant_message"
                               and event["item"].get("text") == REFUSAL for event in events)
        self.assertEqual(count, 2 if refusal_observed else 1)
        self.assertTrue(refusal_observed or marker is not None or terminal["status"] == "BLOCKED")


if __name__ == "__main__":
    unittest.main()
