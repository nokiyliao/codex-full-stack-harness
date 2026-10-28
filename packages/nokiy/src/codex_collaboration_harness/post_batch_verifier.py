# SPDX-License-Identifier: MIT
"""Optional caller candidate: one prepared verifier, joined members, no replay.

The plan's post_batch_verifier is a separate prepared-request locator binding
request_path, request_file_sha256, request_id, artifact_root, native_thread_id.
Optional parent_source_targets fence a completed native parent branch: the parent
seals its snapshot with complete-batch-parent after its last edit, while the
original batch waits cooperatively. They grant reads or writes to nobody.
Its existing read-only J-Space must grant exactly one typed verifier and reads of
the combined exact source subjects, never source writes. No model or provider is
invoked for this request. Execution uses
ParentVerifier's unchanged router planning, native sandbox and scope supervisor.
Only the live ParentVerifier return authenticates an outcome here. This direct
path has no independently bound durable outcome for recovery, so receipts are
diagnostic content only: recovery stays UNKNOWN and never executes a verifier.
Installed API/exposure and mission acceptance require separate parent integration.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import time

from . import batch, embedded_nokiy as caller, full_core, local_context
from .postimage_context import known_success
from .result_inspection import _gap_diagnostic
from .verifier_parent import ParentVerifier, VERIFICATION_EVIDENCE_SCHEMA, _verification_targets, _capture_verification_sources

SCHEMA = "nokiy_post_batch_verifier_v1"
LIMIT = 1024 * 1024
# Inline failure output lets the parent repair without opening the receipt; the gate is
# charged against the batch summary before member diffs are inlined.
INLINE_EVIDENCE_BYTES = 32 * 1024
# Files a live verifier leaves at its scratch root (reports such as quality JSON) are
# listed so the parent can cite or copy them without searching receipts.
VERIFIER_OUTPUT_FILES = 16


def prepare_request(draft_path, action_path, surface_id, output):
    # Only this explicit gate preparation path can split scratch and receipts.
    # It never falls back to inspect and exposes no --run option.
    from .full_stack import _prepare_request
    return _prepare_request(draft_path, action_path, surface_id, output, post_batch_verifier=True)


def validate(raw, members, *, recovery=False):
    keys = {"request_path", "request_file_sha256", "request_id", "artifact_root", "native_thread_id"}
    if (not isinstance(raw, dict) or not keys <= set(raw)
            or set(raw) - keys - {"parent_source_targets"}):
        batch._fail("post_batch_verifier fields differ")
    spec = dict(raw)
    if "parent_source_targets" in spec:
        names = spec["parent_source_targets"]
        if (not isinstance(names, list) or not names or len(names) > 128
                or any(full_core._exact_presentation_path(name, absolute=False) is None for name in names)
                or len(set(names)) != len(names)):
            batch._fail("post_batch_verifier parent subjects must be exact unique relative files")
        spec["parent_source_targets"] = list(names)
    caller._require_sha256("post_batch_verifier.request_file_sha256", spec["request_file_sha256"])
    if (not isinstance(spec["request_id"], str) or not caller.REQUEST_ID_PATTERN.fullmatch(spec["request_id"])
            or not isinstance(spec["native_thread_id"], str)
            or not caller.THREAD_ID_PATTERN.fullmatch(spec["native_thread_id"])
            or spec["native_thread_id"] != os.environ.get("CODEX_THREAD_ID")):
        batch._fail("post_batch_verifier owner or request identity differs")
    for key in ("request_path", "artifact_root"):
        name = spec[key]
        if (not isinstance(name, str) or not name or "\0" in name
                or not Path(name).is_absolute() or str(Path(name)) != name
                or ".." in Path(name).parts or ".git" in Path(name).parts):
            batch._fail("post_batch_verifier path must be canonical and traversal-free")
        if not recovery and (any(p.is_symlink() for p in (Path(name), *Path(name).parents))
                             or Path(name).resolve(strict=True) != Path(name)):
            batch._fail("post_batch_verifier path cannot traverse aliases")
    root = Path(spec["artifact_root"])
    roots = {m.artifact_root for m in members}
    overlap = (any(root == other or root in other.parents or other in root.parents for other in roots)
               if recovery else batch._overlap({root}, roots))
    if (any(spec["request_id"] == m.request_id or Path(spec["request_path"]) == m.path for m in members)
            or overlap):
        batch._fail("post_batch_verifier must have separate request and artifact ownership")
    return spec


def _run(spec):
    # Consume the original prepared request's existing create-only run identity.
    # It cannot subsequently be dispatched as a model request or another gate.
    return Path(spec["artifact_root"]) / spec["request_id"]


def exists(spec):
    return os.path.lexists(_run(spec))


def _load(spec):
    request, raw = batch.load_prepared_request(Path(spec["request_path"]))
    if (hashlib.sha256(raw).hexdigest() != spec["request_file_sha256"]
            or request.request_id != spec["request_id"]
            or str(request.artifact_root) != spec["artifact_root"]
            or request.native_thread_id != spec["native_thread_id"]
            or request.schema_version != caller.REQUEST_SCHEMA_VERSION or request.execution_profile != "direct"):
        batch._fail("post_batch_verifier prepared identity differs")
    contract = caller._bound_json(request.jspace_contract, code=batch.CODE)
    if not isinstance(contract, dict):
        batch._fail("post_batch_verifier requires an existing typed J-Space object")
    commands = contract.get("verifier_commands")
    if not isinstance(commands, list) or len(commands) != 1:
        batch._fail("post_batch_verifier requires exactly one existing typed grant")
    operations = contract.get("allowed_operations")
    if (request.authority_effect != "none"
            or contract.get("write_scopes") != [] or contract.get("declared_targets") != []
            or not isinstance(operations, list) or set(operations) != {"read", "command"}):
        batch._fail("post_batch_verifier requires a read/command-only final request")
    return request, contract


def _binding(spec, sha256, request, contract, source_targets):
    return {"schema_version": SCHEMA, "plan_sha256": sha256, "request": spec,
            "jspace_sha256": request.jspace_contract.sha256,
            "authorization_semantic_sha256": contract["authorization_semantic_sha256"],
            "command": contract["verifier_commands"][0],
            "source_targets": _verification_targets(request.workspace, contract,
                                                       source_targets=source_targets)}


@dataclass
class Prepared:
    spec: dict
    request: object
    contract: dict
    router: object
    binding: dict
    protected_paths: frozenset = frozenset()
    member_ids: tuple = ()


def _scratch_isolated(request, contract, protected):
    _, effects = batch._scopes(request, post_batch_verifier=True)
    if batch._overlap(effects, set(protected)):
        batch._fail("post_batch_verifier scratch overlaps protected member, source or receipt paths")
    for path in protected:
        if any(node.is_symlink() for node in (path, *path.parents)):
            batch._fail("post_batch_verifier protected path alias changed")


def prepare(spec, path, sha256, members, requests, scopes):
    """Read-only admission before the first member starts; no synthetic grants."""
    request, contract = _load(spec)
    if any(r.workspace != request.workspace for r in requests):
        batch._fail("post_batch_verifier currently requires one shared workspace")
    if batch._overlap({request.artifact_root}, {r.workspace for r in requests}):
        batch._fail("post_batch_verifier artifact root overlaps workspace")
    split_root = contract.get("verifier_artifact_root") != str(request.artifact_root)
    ready, runtime, _, admitted = full_core.prepare(request,
        **({"post_batch_verifier": True} if split_root else {}))
    blocker = full_core.verifier_dispatch_blocker(contract)
    if ready.get("status") != "READY" or blocker:
        batch._fail(blocker or ready.get("first_typed_blocker") or "post_batch_verifier not READY")
    if admitted != contract:
        batch._fail("post_batch_verifier admission changed")
    batch._scopes(request, post_batch_verifier=True)
    command = local_context._verifier_commands(contract["verifier_commands"], request.workspace,
        Path(contract["verifier_artifact_root"]), contract["read_scopes"])[0]
    union = sorted({name for r in requests for name, _ in batch._source_write_targets(r)})
    parent_targets = spec.get("parent_source_targets", [])
    if set(parent_targets) & set(union):
        batch._fail("post_batch_verifier parent and worker source targets overlap")
    union = sorted(set(union) | set(parent_targets))
    targets = _verification_targets(request.workspace, contract, source_targets=union)
    if targets is None or targets != union:
        batch._fail("post_batch_verifier must bind all member source targets with existing readable grants")
    scratch = Path(command["scratch_root"])
    immutable = {Path(path), Path(spec["request_path"]), request.jspace_contract.path,
                 request.context_capsule.path, request.runtime_image.path, request.codex.path,
                 Path(command["argv"][0])}
    immutable.update(entry.path for entry in runtime.artifacts.values())
    immutable.update(Path(entry["path"]) for entry in command["pinned_files"])
    protected = immutable | {request.workspace, _run(spec)}
    protected.update(member.artifact_root for member in members)
    protected.update(member.path for member in members)
    for member_request in requests:
        protected.add(member_request.artifact_root)
        protected.update(identity.path for identity in (member_request.context_capsule,
            member_request.jspace_contract, member_request.runtime_image, member_request.codex))
        member_runtime = (runtime if member_request.runtime_image == request.runtime_image else
            caller.verify_runtime_image(member_request.runtime_image, required_artifacts=full_core.REQUIRED))
        protected.update(identity.path for identity in member_runtime.artifacts.values())
    for reads, effects in scopes:
        protected.update(reads | effects)
    _scratch_isolated(request, contract, protected)
    writes = set().union(*(scope[1] for scope in scopes))
    if (batch._overlap(immutable, writes | {scratch, _run(spec)})
            or batch._overlap({scratch}, {_run(spec)})
            or any(batch._overlap({request.artifact_root}, reads | effects) for reads, effects in scopes)):
        batch._fail("post_batch_verifier immutable, scratch or receipt ownership overlap")
    return Prepared(deepcopy(spec), request, deepcopy(contract), runtime.artifacts["tura_router"],
                    deepcopy(_binding(spec, sha256, request, contract, targets)), frozenset(protected),
                    tuple(member.request_id for member in members))


def _directory(spec):
    root = Path(spec["artifact_root"])
    if (any(p.is_symlink() for p in (root, *root.parents)) or root.resolve(strict=True) != root
            or not root.is_dir()):
        batch._fail("post_batch_verifier artifact root unavailable")
    return root


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def claim(gate, path):
    _directory(gate.spec)
    _, raw = batch._json_snapshot(path, limit=batch.MAX_PLAN_BYTES)
    if hashlib.sha256(raw).hexdigest() != gate.binding["plan_sha256"]:
        batch._fail("post_batch_verifier original plan changed before claim")
    try:
        _run(gate.spec).mkdir(mode=0o700)
    except FileExistsError:
        return False  # Existing claim, even without receipt, is never replayable.
    _sync(_run(gate.spec).parent)
    batch._create_only(_run(gate.spec) / "plan.json", raw)
    caller._write_create_only(_run(gate.spec) / "claim.json", gate.binding)
    _sync(_run(gate.spec))
    return True


def acknowledge_parent(gate):
    """Seal a completed parent branch; no source writes or verifier execution."""
    names = gate.spec.get("parent_source_targets")
    if not names or os.environ.get("CODEX_THREAD_ID") != gate.spec["native_thread_id"]:
        batch._fail("post_batch_verifier parent acknowledgement owner or subjects differ")
    _directory(gate.spec)
    run = _run(gate.spec)
    if run.is_symlink() or run.resolve(strict=True) != run or os.path.lexists(run / "receipt.json"):
        batch._fail("post_batch_verifier parent acknowledgement is not pending")
    original, _ = batch._json_snapshot(run / "claim.json", limit=LIMIT)
    request, contract = _load(gate.spec)
    binding = _binding(gate.spec, gate.binding["plan_sha256"], request, contract,
                       gate.binding["source_targets"])
    if (caller._canonical_sha256(original) != caller._canonical_sha256(binding)
            or caller._canonical_sha256(binding) != caller._canonical_sha256(gate.binding)):
        batch._fail("post_batch_verifier parent acknowledgement binding changed")
    sources = _capture_verification_sources(request.workspace, contract, source_targets=names)
    if sources is None:
        batch._fail("post_batch_verifier parent source snapshot unavailable")
    completion = {"schema_version": "nokiy_parent_completion_v1",
                  "binding_sha256": caller._canonical_sha256(binding),
                  "native_thread_id": request.native_thread_id,
                  "source_postimages": {name: row[0] for name, row in sources.items()}}
    caller._write_create_only(run / "parent-complete.json", completion)
    _sync(run)
    return {"status": "PARENT_SNAPSHOT_SEALED", "verifier_executed": False,
            "path": str(run / "parent-complete.json")}


def acknowledge_parent_plan(path, sha256):
    """Recover immutable plan bindings only; never dispatch or replay members."""
    verifiers = []
    members, _ = batch._load_plan(path, sha256, post_batch_verifiers=verifiers)
    if not verifiers or not verifiers[0].get("parent_source_targets"):
        batch._fail("batch has no admitted parent completion barrier")
    spec = verifiers[0]
    request, contract = _load(spec)
    worker_targets = set()
    for member in members:
        worker, raw = batch.load_prepared_request(member.path)
        if (hashlib.sha256(raw).hexdigest() != member.file_sha256
                or worker.request_id != member.request_id
                or worker.artifact_root != member.artifact_root
                or worker.workspace != request.workspace
                or worker.native_thread_id != request.native_thread_id
                or worker.schema_version != caller.REQUEST_SCHEMA_VERSION
                or worker.execution_profile != "direct"):
            batch._fail("parent completion original worker binding changed")
        worker_targets.update(name for name, _ in batch._source_write_targets(worker))
    if worker_targets.intersection(spec["parent_source_targets"]):
        batch._fail("parent completion source targets overlap workers")
    targets = sorted(worker_targets | set(spec["parent_source_targets"]))
    binding = _binding(spec, sha256, request, contract, targets)
    if binding["source_targets"] != targets:
        batch._fail("parent completion source targets lack readable grants")
    return acknowledge_parent(Prepared(spec, request, contract, None, binding))


def _wait_for_parent(gate, cancel):
    names = gate.spec.get("parent_source_targets")
    if names is None:
        return True
    path = _run(gate.spec) / "parent-complete.json"
    while not os.path.lexists(path):
        if cancel.is_set():
            return False
        time.sleep(0.05)
    if cancel.is_set():
        return False
    return _check_parent_completion(gate)


def _check_parent_completion(gate):
    names = gate.spec.get("parent_source_targets")
    if names is None:
        return True
    path = _run(gate.spec) / "parent-complete.json"
    completion, _ = batch._json_snapshot(path, limit=LIMIT)
    sources = _capture_verification_sources(gate.request.workspace, gate.contract, source_targets=names)
    expected = {"schema_version": "nokiy_parent_completion_v1",
                "binding_sha256": caller._canonical_sha256(gate.binding),
                "native_thread_id": gate.request.native_thread_id,
                "source_postimages": ({name: row[0] for name, row in sources.items()}
                                      if sources is not None else None)}
    if sources is None or caller._canonical_sha256(completion) != caller._canonical_sha256(expected):
        batch._fail("post_batch_verifier parent completion or source snapshot changed")
    return True


def _status(observation, request, contract, binding):
    """Validate a live ParentVerifier return; never authenticate stored JSON."""
    if observation is None:
        return "NOT_RUN"
    if (not isinstance(observation, dict)
            or set(observation) - {"success", "exit_code", "stdout", "stderr", "process_reaped",
                                  "process_group_empty", "outcome", "verification_evidence"}
            or type(observation.get("exit_code")) is not int
            or any(type(observation.get(k)) is not bool for k in ("success", "process_reaped", "process_group_empty"))
            or any(not isinstance(observation.get(k), str) for k in ("stdout", "stderr", "outcome"))
            or observation["outcome"] != "known" or observation["process_reaped"] is not True
            or observation["process_group_empty"] is not True):
        return "UNKNOWN"
    if observation["success"] is False and observation["exit_code"] != 0:
        return "FAILED"
    if not known_success(observation):
        return "UNKNOWN"
    sources = _capture_verification_sources(request.workspace, contract,
                                           source_targets=binding["source_targets"])
    if sources is None:
        return "UNKNOWN"
    expected = {"schema_version": VERIFICATION_EVIDENCE_SCHEMA,
                "authorization_semantic_sha256": binding["authorization_semantic_sha256"],
                "verifier_index": 0, "verifier_sha256": caller._canonical_sha256(binding["command"]),
                "call_id": "post-batch-" + binding["plan_sha256"],
                "source_postimages": {name: row[0] for name, row in sources.items()}}
    return ("PASSED" if caller._canonical_sha256(observation.get("verification_evidence"))
            == caller._canonical_sha256(expected) else "UNKNOWN")


def _verifier_outputs(binding):
    """Regular scratch-root files, with optional complete text from the hashing read."""
    command = binding.get("command") if isinstance(binding, dict) else None
    root = command.get("scratch_root") if isinstance(command, dict) else None
    if not isinstance(root, str):
        return None
    try:
        with os.scandir(root) as scan:
            entries = sorted((entry for entry in scan if not entry.name.startswith(".")
                              and entry.is_file(follow_symlinks=False)), key=lambda entry: entry.name)
    except OSError:
        return None
    rows = []
    for entry in entries[:VERIFIER_OUTPUT_FILES]:
        try:
            with os.fdopen(os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                data = handle.read(LIMIT + 1)
                size = os.fstat(handle.fileno()).st_size
        except OSError:
            continue
        row = {"path": entry.path, "bytes": size}
        if len(data) <= LIMIT:
            row["sha256"] = hashlib.sha256(data).hexdigest()
        if (len(data) == size and len(data) <= INLINE_EVIDENCE_BYTES
                and Path(entry.name).suffix.lower() in {".log", ".txt"}):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                if all(char.isprintable() or char in "\t\r\n" for char in text):
                    row["text"] = text
        rows.append(row)
    return {"files": rows, "truncated": len(entries) > VERIFIER_OUTPUT_FILES}


def _attach(result, status, spec, *, raw=None, observation=None, binding=None, diagnostic_sources=None):
    gate = {"schema_version": SCHEMA, "status": status,
            "receipt_path": str(_run(spec) / "receipt.json"),
            "receipt_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
            "parent_acceptance_required": True, "auto_retry_allowed": False}
    evidence = {"binding": binding, "source_postimages": diagnostic_sources}
    if isinstance(observation, dict) and isinstance(observation.get("verification_evidence"), dict):
        evidence["source_postimages"] = observation["verification_evidence"].get("source_postimages")
    if binding is not None:
        evidence_raw = caller._canonical_bytes(evidence)
        gate.update(evidence_sha256=hashlib.sha256(evidence_raw).hexdigest(), evidence_bytes=len(evidence_raw),
                    evidence_authority="live_return" if observation is not None
                    and status in {"PASSED", "FAILED"} else "diagnostic_only")
        if len(evidence_raw) <= INLINE_EVIDENCE_BYTES:
            gate.update(deepcopy(evidence))
        else:
            gate["evidence_omission"] = "INLINE_EVIDENCE_BYTE_LIMIT"
    if isinstance(observation, dict):
        gate.update({key: observation.get(key) for key in ("exit_code", "outcome", "process_reaped", "process_group_empty")})
        for key in ("stdout", "stderr"):
            text = observation.get(key)
            data = text.encode("utf-8") if isinstance(text, str) else b""
            gate[key + "_sha256"], gate[key + "_bytes"] = hashlib.sha256(data).hexdigest(), len(data)
        outputs = _verifier_outputs(binding)
        if outputs is not None:
            gate.update(verifier_outputs=outputs,
                        verifier_outputs_authority="untrusted_verifier_written_files_not_parent_acceptance")
        file_texts = batch._drop_optional_verifier_text(gate)
        preview = {key: observation[key] for key in ("stdout", "stderr")
                   if isinstance(observation.get(key), str)}
        candidate = {**gate, "output_preview": preview,
                     "output_preview_authority": "untrusted_verifier_output_not_parent_acceptance"}
        if len(caller._canonical_bytes(candidate)) <= INLINE_EVIDENCE_BYTES:
            gate = candidate
        else:
            gate["output_preview_omission"] = "INLINE_EVIDENCE_BYTE_LIMIT"
        visible_preview = gate.get("output_preview") or {}
        for file, key, text in file_texts:
            if not text or any(text in visible_preview.get(channel, "")
                               for channel in ("stdout", "stderr")):
                continue
            file[key] = text
            if len(caller._canonical_bytes(gate)) > INLINE_EVIDENCE_BYTES:
                file.pop(key)
    result = {**result, "post_batch_verifier": gate}
    if status != "PASSED" and result["status"] == "RESULT_AVAILABLE":
        result.update(status="BLOCKED", first_typed_blocker="NOKIY_POST_BATCH_VERIFIER_" + status)
    return result


def recover(spec, sha256, result):
    """Read only; no durable independent outcome anchor means no recovered pass."""
    try:
        _directory(spec)
        run = _run(spec)
        if run.is_symlink() or run.resolve(strict=True) != run:
            raise ValueError("aliased claim")
        request, contract = _load(spec)
        claim_value, _ = batch._json_snapshot(run / "claim.json", limit=LIMIT)
        binding = _binding(spec, sha256, request, contract, claim_value["source_targets"])
        _, plan_raw = batch._json_snapshot(run / "plan.json", limit=batch.MAX_PLAN_BYTES)
        receipt, raw = batch._json_snapshot(run / "receipt.json", limit=LIMIT)
        if (caller._canonical_sha256(claim_value) != caller._canonical_sha256(binding)
                or hashlib.sha256(plan_raw).hexdigest() != sha256
                or not isinstance(receipt, dict) or set(receipt) != {"binding", "status", "observation"}
                or caller._canonical_sha256(receipt["binding"]) != caller._canonical_sha256(binding)):
            raise ValueError("receipt binding differs")
        # Claim/plan/command identities do not bind the original outcome. The
        # observation and its source hashes can be rewritten together, including
        # a failed or interrupted attempt made to look successful. The digest
        # below identifies current receipt bytes only; it is not an attestation.
        observation = receipt["observation"]
        evidence = observation.get("verification_evidence") if isinstance(observation, dict) else None
        return _attach(result, "UNKNOWN", spec, raw=raw, binding=binding,
                       diagnostic_sources=evidence.get("source_postimages") if isinstance(evidence, dict) else None)
    except (caller.EmbeddedNokiyError, OSError, ValueError, RuntimeError, KeyError, TypeError):
        return _attach(result, "UNKNOWN", spec)


class _Cancelled:
    def __init__(self, signals, prior):
        self.signals, self.prior = signals, prior

    def is_set(self):
        return bool(self.signals) or self.prior


def _settled_diagnostic_gap(gate, terminal, inspection):
    """Keep an inspected model diagnostic, never derive grants from its handoff."""
    gap = terminal.get("capability_gap")
    expected = _gap_diagnostic()
    if (not isinstance(gap, dict) or set(gap) != set(expected) | {"binding", "handoff"}
            or any(type(gap[key]) is not type(value) or gap[key] != value
                   for key, value in expected.items())):
        return False
    binding, handoff = gap["binding"], gap["handoff"]
    if (not isinstance(binding, dict)
            or set(binding) != {"request_id", "request_sha256", "native_thread_id",
                                "terminal_sha256", "terminal_content_sha256"}
            or not isinstance(handoff, dict) or handoff.get("schema_version") != expected["schema_version"]
            or terminal.get("native_thread_id") != gate.spec["native_thread_id"]
            or terminal.get("request_id") not in gate.member_ids
            or terminal.get("request_id") != "tura_embedded_" + str(terminal.get("request_sha256"))
            or any(binding[key] != terminal.get(key)
                   for key in ("request_id", "request_sha256", "native_thread_id"))
            or binding["terminal_sha256"] != inspection.get("terminal_sha256")):
        return False
    return all(isinstance(binding[key], str) and len(binding[key]) == 64
               and not set(binding[key]) - set("0123456789abcdef")
               for key in ("request_sha256", "terminal_sha256", "terminal_content_sha256"))


def _settled_failed_batch(gate, result):
    """An independent final check may run without promoting failed member evidence."""
    rows = result.get("members")
    if (result.get("status") != "BLOCKED" or result.get("first_typed_blocker") != "COMMAND_FAILURE"
            or not isinstance(rows, list) or not gate.member_ids
            or tuple(row.get("request_id") for row in rows if isinstance(row, dict)) != gate.member_ids):
        return False
    for row in rows:
        terminal = row.get("terminal")
        inspection = terminal.get("result_inspection") if isinstance(terminal, dict) else None
        if (row.get("status") != "RESULT_AVAILABLE" or row.get("cleanup_pass") is not True
                or not isinstance(inspection, dict) or inspection.get("artifact_integrity") != "verified"
                or inspection.get("projection_partial") is True
                or terminal.get("cleanup_pass") is not True or terminal.get("status") != "RESULT_AVAILABLE"
                or terminal.get("first_typed_blocker") is not None or terminal.get("first_blocker") is not None
                or terminal.get("fallback_used") is not False
                or type(terminal.get("runtime_exit_code")) is not int or terminal["runtime_exit_code"] != 0):
            return False
        if inspection.get("status") == "EVIDENCE_VERIFIED" and inspection.get("first_blocker") is None:
            if terminal.get("capability_gap") is not None:
                return False
            continue
        settlement, counts = inspection.get("post_batch_verifier_settlement"), inspection.get("counts")
        if (inspection.get("status") != "INCOMPLETE_EVIDENCE" or inspection.get("first_blocker") != "COMMAND_FAILURE"
                or inspection.get("command_success") != "known_failure"
                or not isinstance(counts, dict) or counts.get("index_complete") is not True
                or not isinstance(settlement, dict)
                or set(settlement) != {"schema_version", "terminal_sha256", "commands", "failed",
                                      "policy_denials", "unsettled_commands"}
                or settlement["schema_version"] not in {"nokiy_post_batch_verifier_settlement_v1",
                                                       "nokiy_post_batch_verifier_settlement_v2"}
                or settlement["terminal_sha256"] != inspection.get("terminal_sha256")
                or type(settlement["unsettled_commands"]) is not int or settlement["unsettled_commands"] != 0
                or any(type(settlement[key]) is not int or settlement[key] != counts.get(key)
                       for key in ("commands", "failed"))
                or not 0 < settlement["failed"] <= settlement["commands"]
                or type(settlement["policy_denials"]) is not int
                or not 0 <= settlement["policy_denials"] <= settlement["failed"]):
            return False
        if terminal.get("capability_gap") is not None and (
                settlement["schema_version"] != "nokiy_post_batch_verifier_settlement_v2"
                or settlement["policy_denials"] != 0
                or not _settled_diagnostic_gap(gate, terminal, inspection)):
            return False
    return True


def finish(gate, result, *, cancelled=False):
    observation, status = None, "NOT_RUN"
    try:
        _directory(gate.spec)
        run = _run(gate.spec)
        if run.is_symlink() or run.resolve(strict=True) != run:
            raise ValueError("aliased claim")
        if os.path.lexists(run / "receipt.json"):
            return _attach(result, "UNKNOWN", gate.spec)
        original, _ = batch._json_snapshot(run / "claim.json", limit=LIMIT)
        if caller._canonical_sha256(original) != caller._canonical_sha256(gate.binding):
            raise ValueError("original claim changed")
        with caller._cancellation_signals() as signals:
            cancel = _Cancelled(signals, cancelled)
            if ((result["status"] == "RESULT_AVAILABLE" or _settled_failed_batch(gate, result))
                    and not cancel.is_set() and _wait_for_parent(gate, cancel)):
                # Recheck original owner and inputs without refreshing or expanding authority.
                request, contract = _load(gate.spec)
                current_claim, _ = batch._json_snapshot(run / "claim.json", limit=LIMIT)
                if (os.environ.get("CODEX_THREAD_ID") != gate.spec["native_thread_id"]
                        or caller._canonical_sha256(current_claim) != caller._canonical_sha256(gate.binding)
                        or _binding(gate.spec, gate.binding["plan_sha256"], request, contract,
                                    gate.binding["source_targets"]) != gate.binding):
                    raise ValueError("original binding changed")
                _scratch_isolated(request, contract, gate.protected_paths)
                runner = ParentVerifier(gate.request.workspace, gate.contract, gate.router, None,
                    verification_source_targets=gate.binding["source_targets"])
                observation = runner.handle_request(0, "post-batch-" + gate.binding["plan_sha256"], cancel)
                observation = {k: v for k, v in observation.items() if k != "source_postimages"}
                status = _status(observation, gate.request, gate.contract, gate.binding)
                _check_parent_completion(gate)
                if not runner.cleanup_pass or cancel.is_set():
                    status = "UNKNOWN"
        receipt = {"binding": gate.binding, "status": status, "observation": observation}
        original_raw = caller._canonical_bytes(receipt) + b"\n"
        caller._write_create_only(_run(gate.spec) / "receipt.json", receipt)
        _sync(_run(gate.spec))
        _, raw = batch._json_snapshot(_run(gate.spec) / "receipt.json", limit=LIMIT)
        if raw != original_raw:
            raise ValueError("published receipt differs from live outcome")
        return _attach(result, status, gate.spec, raw=raw, observation=observation, binding=gate.binding)
    except Exception:
        # A dispatch/receipt failure may have run the command. Never retry it.
        return _attach(result, "UNKNOWN", gate.spec)
