#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify a candidate via the real native catalog parser and public model/list.

No thread/start, turn/start, provider generation, live catalog publication or
service restart is requested. The temporary public stdio process is reaped.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import model_topology as topology


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def public_model_list(native: Path, catalog: Path) -> dict:
    process = subprocess.Popen(
        [str(native), "-c", "model_catalog_json=" + json.dumps(str(catalog)),
         "app-server", "--strict-config", "--listen", "stdio://"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    pending = b""
    stderr_bytes = 0
    consumed = 0
    requested = []
    observed = []

    def send(method, params, request_id=None):
        message = {"method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        requested.append(method)
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    try:
        send("initialize", {"clientInfo": {"name": "nokiy-topology-model-list-test", "version": "1"},
                            "capabilities": {"experimentalApi": True}}, 1)
        deadline = time.monotonic() + 45
        next_id = 2
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("native model/list process exited before completion")
            for key, _ in selector.select(timeout=min(1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                consumed += len(chunk)
                if consumed > 16 * 1024 * 1024:
                    raise RuntimeError("native model/list output exceeded budget")
                if key.data == "stderr":
                    stderr_bytes += len(chunk)
                    continue
                pending += chunk
                if len(pending) > 8 * 1024 * 1024:
                    raise RuntimeError("native response line exceeded budget")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if "id" in item and "method" in item:
                        # A listing verifier grants no approval or dynamic tool effects.
                        process.stdin.write((json.dumps({"id": item["id"], "error": {
                            "code": -32001, "message": "Read-only catalog verifier grants no effects"}}) + "\n").encode())
                        process.stdin.flush()
                        raise RuntimeError("unexpected effect/approval request while listing models")
                    if item.get("id") == 1:
                        if "error" in item:
                            raise RuntimeError("initialize rejected: " + str(item["error"].get("code")))
                        send("initialized", {})
                        send("model/list", {"includeHidden": False, "limit": 100}, next_id)
                    elif item.get("id") == next_id:
                        if "error" in item:
                            raise RuntimeError("model/list rejected: " + str(item["error"].get("code")))
                        page = item["result"]
                        observed.extend(page["data"])
                        cursor = page.get("nextCursor")
                        if not cursor:
                            return {"models": observed, "requested_methods": requested,
                                    "stderr_bytes": stderr_bytes, "complete": True}
                        if next_id >= 8:
                            raise RuntimeError("model/list pagination exceeded budget")
                        next_id += 1
                        send("model/list", {"includeHidden": False, "limit": 100, "cursor": cursor}, next_id)
        raise TimeoutError("native model/list deadline expired")
    finally:
        selector.close()
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        for stream in (process.stdout, process.stderr):
            stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--native-sha256", required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--preserve-file", type=Path, action="append", default=[])
    args = parser.parse_args()
    if (not args.native.is_absolute() or args.native.resolve(strict=True) != args.native
            or digest(args.native) != args.native_sha256):
        raise RuntimeError("native executable binding mismatch")
    if not args.output_dir.is_absolute() or args.output_dir.exists():
        raise RuntimeError("new absolute output directory is required")
    preserve = [args.catalog, *args.preserve_file]
    before = {str(path): digest(path) for path in preserve}
    source = caller._load_json(args.catalog, limit=16 * 1024 * 1024, code="CATALOG_INVALID")
    candidate = topology.build_candidate_catalog(source)
    args.output_dir.mkdir(mode=0o700, parents=False)
    candidate_path = args.output_dir / "candidate-models.json"
    caller._write_create_only(candidate_path, candidate)
    receipt = {"schema_version": "nokiy_model_picker_verification_v1",
               "generated_at": datetime.now(timezone.utc).isoformat(),
               "native": {"path": str(args.native), "sha256": args.native_sha256},
               "candidate": {"path": str(candidate_path), "sha256": digest(candidate_path)},
               "live_catalog_modified": False, "source_files_before": before,
               "root_topology_verified": False, "provider_observed_model": None,
               "deployment_admitted": False}
    error = None
    try:
        version = subprocess.run([str(args.native), "--version"], capture_output=True, timeout=10, check=True)
        receipt["native"]["version"] = version.stdout.decode().strip()
        loaded = subprocess.run([str(args.native), "-c", "model_catalog_json=" + json.dumps(str(candidate_path)),
                                 "debug", "models"], capture_output=True, timeout=30, check=True)
        if len(loaded.stdout) > 16 * 1024 * 1024:
            raise RuntimeError("native catalog dump exceeded budget")
        parsed = json.loads(loaded.stdout)
        debug_rows = {row["slug"]: row for row in parsed["models"]}
        for name in topology.MODES:
            if [row["effort"] for row in debug_rows[name]["supported_reasoning_levels"]] != list(topology.FAMILIES):
                raise RuntimeError("native catalog parser changed worker selector")
        receipt["native_parser_pass"] = True
        listing = public_model_list(args.native, candidate_path)
        rows = {row["model"]: row for row in listing["models"]}
        snapshot = []
        for name in topology.MODES:
            row = rows[name]
            efforts = [item["reasoningEffort"] for item in row["supportedReasoningEfforts"]]
            if efforts != list(topology.FAMILIES) or row["defaultReasoningEffort"] != "sol":
                raise RuntimeError("public model/list changed the selector")
            snapshot.append({"model": name, "displayName": row["displayName"],
                             "workerOptions": efforts, "defaultWorker": row["defaultReasoningEffort"]})
        receipt.update(public_model_list_pass=True, picker_snapshot=snapshot,
                       requested_methods=listing["requested_methods"], list_complete=listing["complete"],
                       turn_start_requests=0, status="CANDIDATE_LISTED_NOT_ADMITTED")
    except Exception as exc:
        error = exc
        receipt.update(status="BLOCKED", error_type=type(exc).__name__, error=str(exc)[:800])
    finally:
        after = {str(path): digest(path) for path in preserve}
        receipt["source_files_after"] = after
        receipt["preserved_inputs_unchanged"] = before == after
        receipt["native_unchanged"] = digest(args.native) == args.native_sha256
        if before != after or not receipt["native_unchanged"]:
            receipt["status"] = "INPUT_DRIFT"
            error = RuntimeError("verification input changed concurrently")
        caller._write_create_only(args.output_dir / "verification.json", receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 2 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
