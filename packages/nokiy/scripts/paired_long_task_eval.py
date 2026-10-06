"""Evaluate one frozen, offline long-task pair without executing either arm.

The caller supplies a SHA-256 of the frozen task manifest. The manifest
pins the SHA-256 of each normalized observation and the common task identity::

    {"schema_version": "paired-long-task-task/v1", "task_id": "task-1",
     "task_sha256": "<sha256 of frozen task content>", "model": "...",
     "effort": "...", "arms": {"baseline": "<observation sha256>",
                              "candidate": "<observation sha256>"},
     "verifier_receipts": {"baseline": "<receipt sha256>",
                           "candidate": "<receipt sha256>"}}

Each observation has schema_version ``paired-long-task-observation/v1``, the
same task_id/task_sha256/model/effort, its arm and unique run_id, executor_id,
run_status ``COMPLETE``, persisted timezone-aware started_at/ended_at, usage,
and a verifier object. The verifier must report source ``independent``, a
different verifier_id, PASS or FAIL status, and a persisted verified_at. For
receipt readback, the observation also supplies output_sha256; the pinned
receipt repeats task, arm, run, executor, output, and verifier identities.
Usage has status KNOWN, PARTIAL, or UNKNOWN and may contain reported
input_tokens, output_tokens, total_tokens, cached_input_tokens, and provenance.
Missing values are not inferred; cached tokens do not imply a cost figure.

The optional evidence paths permit SHA-bound receipt-reported quality readback.
The receipt producer is not cryptographically authenticated; matching bytes and
an ``independent`` label do not establish its real-world authorship. No provider
usage/timing provenance, efficiency comparability, or causal gain is inferred.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


TASK_SCHEMA = "paired-long-task-task/v1"
OBSERVATION_SCHEMA = "paired-long-task-observation/v1"
RECEIPT_SCHEMA = "paired-long-task-verifier-receipt/v1"
RESULT_SCHEMA = "paired-long-task-evaluation/v1"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_INPUT_BYTES = 1024 * 1024
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
OPTIONAL_TOKEN_FIELDS = ("cached_input_tokens",)


class EvaluationError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


def _require(condition: bool, code: str, detail: str) -> None:
    if not condition:
        raise EvaluationError(code, detail)


def _sha256(value: Any, label: str) -> str:
    _require(isinstance(value, str) and SHA256.fullmatch(value) is not None,
             "INVALID_SHA256", f"{label} must be a lowercase SHA-256")
    return value


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "INVALID_JSON", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise EvaluationError("INVALID_JSON", f"non-finite JSON value: {value}")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    _require(math.isfinite(parsed), "INVALID_JSON", f"non-finite JSON number: {value}")
    return parsed


def _load_pinned_bytes(path: Path, expected_sha256: str, label: str) -> bytes:
    _sha256(expected_sha256, f"{label} expected digest")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode),
                     "INVALID_INPUT", f"{label} must be a regular file")
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise EvaluationError("INPUT_IO_ERROR", f"cannot read {label}: {exc}") from exc
    _require(len(raw) <= MAX_INPUT_BYTES, "INPUT_TOO_LARGE", f"{label} exceeds 1 MiB")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    _require(actual_sha256 == expected_sha256, "SHA256_MISMATCH",
             f"{label} SHA-256 differs from its frozen reference")
    return raw


def _load_pinned_json(path: Path, expected_sha256: str, label: str) -> tuple[dict[str, Any], str]:
    raw = _load_pinned_bytes(path, expected_sha256, label)
    try:
        document = json.loads(raw.decode("utf-8"),
                              object_pairs_hook=_object_without_duplicates,
                              parse_constant=_reject_json_constant,
                              parse_float=_finite_json_float)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise EvaluationError("INVALID_JSON", f"invalid {label} JSON: {exc}") from exc
    _require(isinstance(document, dict), "INVALID_INPUT", f"{label} must be an object")
    return document, expected_sha256


def _nonempty_string(value: Any, label: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()),
             "INVALID_INPUT", f"{label} must be a nonempty string")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    _require(isinstance(value, str), "INVALID_TIMESTAMP", f"{label} must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvaluationError("INVALID_TIMESTAMP", f"{label} is not ISO 8601") from exc
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
             "INVALID_TIMESTAMP", f"{label} must include a UTC offset")
    return parsed


def _usage(value: Any, arm: str) -> dict[str, Any]:
    _require(isinstance(value, dict), "INVALID_USAGE", f"{arm} usage must be an object")
    status = value.get("status")
    _require(status in ("KNOWN", "PARTIAL", "UNKNOWN"),
             "INVALID_USAGE", f"{arm} usage status is invalid")
    observed = 0
    for field in TOKEN_FIELDS + OPTIONAL_TOKEN_FIELDS:
        tokens = value.get(field)
        _require(tokens is None or (type(tokens) is int and tokens >= 0),
                 "INVALID_USAGE", f"{arm} {field} must be a nonnegative integer or null")
        if field in TOKEN_FIELDS:
            observed += tokens is not None
    if "provenance" in value:
        _require(value["provenance"] in ("CLI_AGGREGATE", "CLI_REPORTED",
                                         "OBSERVATION_REPORTED"),
                 "INVALID_USAGE", f"{arm} usage provenance is unsupported")
    any_tokens = observed > 0 or value.get("cached_input_tokens") is not None
    expected_status = "KNOWN" if observed == len(TOKEN_FIELDS) else "PARTIAL" if any_tokens else "UNKNOWN"
    _require(status == expected_status, "INVALID_USAGE",
             f"{arm} usage status contradicts observed token fields")
    return value


def _validate_task(task: dict[str, Any]) -> None:
    _require(task.get("schema_version") == TASK_SCHEMA,
             "INVALID_TASK", "unsupported frozen task schema")
    _nonempty_string(task.get("task_id"), "task_id")
    _sha256(task.get("task_sha256"), "task_sha256")
    _nonempty_string(task.get("model"), "model")
    _nonempty_string(task.get("effort"), "effort")
    arms = task.get("arms")
    _require(isinstance(arms, dict) and set(arms) == {"baseline", "candidate"},
             "UNAUTHORIZED_ARM", "frozen task must authorize exactly baseline and candidate")
    for arm in ("baseline", "candidate"):
        _sha256(arms[arm], f"{arm} observation digest")
    _require(arms["baseline"] != arms["candidate"], "INVALID_TASK",
             "arms cannot reference the same observation")
    receipt_pins = task.get("verifier_receipts", {})
    _require(isinstance(receipt_pins, dict) and
             set(receipt_pins) <= {"baseline", "candidate"}, "INVALID_TASK",
             "verifier_receipts must contain only authorized arms")
    for arm, digest in receipt_pins.items():
        _sha256(digest, f"{arm} verifier receipt digest")
    if len(receipt_pins) == 2:
        _require(receipt_pins["baseline"] != receipt_pins["candidate"],
                 "INVALID_TASK", "arms cannot reference the same verifier receipt")


def _validate_observation(observation: dict[str, Any], task: dict[str, Any],
                          arm: str) -> tuple[float, dict[str, Any]]:
    _require(observation.get("schema_version") == OBSERVATION_SCHEMA,
             "INVALID_OBSERVATION", f"{arm} schema is unsupported")
    _require(observation.get("arm") == arm, "UNAUTHORIZED_ARM",
             f"{arm} file does not identify the authorized arm")
    for key in ("task_id", "task_sha256", "model", "effort"):
        _require(observation.get(key) == task[key], "IDENTITY_MISMATCH",
                 f"{arm} {key} differs from the frozen task")
    _nonempty_string(observation.get("run_id"), f"{arm} run_id")
    executor_id = _nonempty_string(observation.get("executor_id"), f"{arm} executor_id")
    if "output_sha256" in observation:
        _sha256(observation["output_sha256"], f"{arm} output_sha256")
    _require(observation.get("run_status") == "COMPLETE", "INCOMPLETE_ARM",
             f"{arm} is not a complete run")
    started = _timestamp(observation.get("started_at"), f"{arm} started_at")
    ended = _timestamp(observation.get("ended_at"), f"{arm} ended_at")
    _require(ended > started, "INVALID_TIMESTAMP",
             f"{arm} ended_at must follow started_at")
    verifier = observation.get("verifier")
    _require(isinstance(verifier, dict), "INVALID_VERIFIER",
             f"{arm} verifier must be an object")
    _require(verifier.get("source") == "independent", "INVALID_VERIFIER",
             f"{arm} verifier must be reported independent")
    verifier_id = _nonempty_string(verifier.get("verifier_id"), f"{arm} verifier_id")
    _require(verifier_id != executor_id, "INVALID_VERIFIER",
             f"{arm} cannot self-verify")
    _require(verifier.get("status") in ("PASS", "FAIL"), "INVALID_VERIFIER",
             f"{arm} verifier status must be PASS or FAIL")
    verified = _timestamp(verifier.get("verified_at"), f"{arm} verified_at")
    _require(verified >= ended, "INVALID_VERIFIER",
             f"{arm} verification predates run completion")
    usage = _usage(observation.get("usage"), arm)
    return (ended - started).total_seconds(), usage


def _validate_receipt(receipt: dict[str, Any], observation: dict[str, Any],
                      arm: str, executor_ids: set[str]) -> None:
    _require(receipt.get("schema_version") == RECEIPT_SCHEMA,
             "INVALID_VERIFIER", f"{arm} receipt schema is unsupported")
    _require("output_sha256" in observation, "INVALID_VERIFIER",
             f"{arm} observation does not pin an output identity")
    for key in ("task_id", "task_sha256", "model", "effort", "arm", "run_id",
                "executor_id", "output_sha256"):
        _require(receipt.get(key) == observation[key], "IDENTITY_MISMATCH",
                 f"{arm} receipt {key} differs from the observation")
    verifier = observation["verifier"]
    for key in ("source", "verifier_id", "status", "verified_at"):
        _require(receipt.get(key) == verifier[key], "INVALID_VERIFIER",
                 f"{arm} receipt {key} differs from the observation")
    _require(receipt["source"] == "independent" and
             receipt["verifier_id"] not in executor_ids,
             "INVALID_VERIFIER", f"{arm} receipt is not independent of both executors")


def evaluate(task_path: Path, task_sha256: str, baseline_path: Path,
             candidate_path: Path, task_content_path: Path | None = None,
             baseline_verifier_receipt_path: Path | None = None,
             candidate_verifier_receipt_path: Path | None = None) -> dict[str, Any]:
    """Read pinned evidence and return a descriptive result; never execute tasks."""
    task, task_digest = _load_pinned_json(task_path, task_sha256, "frozen task")
    _validate_task(task)
    baseline, baseline_digest = _load_pinned_json(
        baseline_path, task["arms"]["baseline"], "baseline observation")
    candidate, candidate_digest = _load_pinned_json(
        candidate_path, task["arms"]["candidate"], "candidate observation")
    baseline_seconds, baseline_usage = _validate_observation(baseline, task, "baseline")
    candidate_seconds, candidate_usage = _validate_observation(candidate, task, "candidate")
    _require(baseline["run_id"] != candidate["run_id"], "INVALID_PAIR",
             "baseline and candidate must be distinct runs")
    executor_ids = {baseline["executor_id"], candidate["executor_id"]}
    for arm, observation in (("baseline", baseline), ("candidate", candidate)):
        _require(observation["verifier"]["verifier_id"] not in executor_ids,
                 "INVALID_VERIFIER", f"{arm} verifier is an executor in the pair")
    content_checked = task_content_path is not None
    if task_content_path is not None:
        _load_pinned_bytes(task_content_path, task["task_sha256"], "frozen task content")
    receipt_paths = {"baseline": baseline_verifier_receipt_path,
                     "candidate": candidate_verifier_receipt_path}
    observations = {"baseline": baseline, "candidate": candidate}
    receipt_pins = task.get("verifier_receipts", {})
    receipt_checked: dict[str, bool] = {}
    for arm, path in receipt_paths.items():
        receipt_checked[arm] = (path is not None and arm in receipt_pins and
                                "output_sha256" in observations[arm])
        if receipt_checked[arm]:
            receipt, _ = _load_pinned_json(path, receipt_pins[arm], f"{arm} verifier receipt")
            _validate_receipt(receipt, observations[arm], arm, executor_ids)
    receipts_complete = all(receipt_checked.values())
    quality_complete = content_checked and receipts_complete
    receipt_reported_passed = (baseline["verifier"]["status"] == "PASS" and
                               candidate["verifier"]["status"] == "PASS") if quality_complete else None
    receipt_reported_status = ("PASS" if receipt_reported_passed else
                               "FAIL") if quality_complete else "UNVERIFIED"
    return {
        "schema_version": RESULT_SCHEMA,
        "pair_status": "EVIDENCE_INCOMPLETE",
        "observation_pair_status": "SHA_BOUND_PARENT_RECEIPTS" if receipts_complete else "SHA_BOUND",
        "input_sha256": {"frozen_task": task_digest, "baseline": baseline_digest,
                         "candidate": candidate_digest,
                         "task_content": task["task_sha256"] if content_checked else None,
                         "baseline_verifier_receipt": receipt_pins.get("baseline") if receipt_checked["baseline"] else None,
                         "candidate_verifier_receipt": receipt_pins.get("candidate") if receipt_checked["candidate"] else None},
        "evidence_scope": {"manifest_and_observation_bytes_checked": True,
                           "task_content_bytes_checked": content_checked,
                           "verifier_receipts_checked": receipts_complete,
                           "verifier_authorship_authenticated": False,
                           "output_bytes_checked": False,
                           "provider_or_runtime_observed": False},
        "identity": {key: task[key] for key in ("task_id", "task_sha256", "model", "effort")},
        "quality_gate": {"passed": None, "status": "UNVERIFIED",
                         "receipt_reported_passed": receipt_reported_passed,
                         "receipt_reported_status": receipt_reported_status,
                         "reported_baseline_status": baseline["verifier"]["status"],
                         "reported_candidate_status": candidate["verifier"]["status"],
                         "basis": ("SHA-bound receipt-reported verdicts only; verifier authorship and output bytes unverified"
                                   if quality_complete else
                                   "SHA-bound observations; task content or verifier receipts not checked")},
        "measurements": {
            "baseline": {"run_id": baseline["run_id"], "started_at": baseline["started_at"],
                         "ended_at": baseline["ended_at"], "elapsed_seconds": baseline_seconds,
                         "elapsed_source": "CALCULATED_FROM_REPORTED_TIMESTAMPS",
                         "usage": baseline_usage, "usage_source": "REPORTED_OBSERVATION",
                         "provider_usage_provenance": "UNKNOWN"},
            "candidate": {"run_id": candidate["run_id"], "started_at": candidate["started_at"],
                          "ended_at": candidate["ended_at"], "elapsed_seconds": candidate_seconds,
                          "elapsed_source": "CALCULATED_FROM_REPORTED_TIMESTAMPS",
                          "usage": candidate_usage, "usage_source": "REPORTED_OBSERVATION",
                          "provider_usage_provenance": "UNKNOWN"},
        },
        "comparability": {"elapsed_efficiency": False,
                          "total_tokens": False,
                          "reason": "EVIDENCE_INCOMPLETE"},
        "result": {"status": "EVIDENCE_INCOMPLETE",
                   "elapsed_direction": None,
                   "elapsed_delta_seconds": None,
                   "total_tokens_direction": None,
                   "total_tokens_delta": None,
                   "causal_gain": "NOT_ESTABLISHED"},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-task", required=True, type=Path)
    parser.add_argument("--frozen-task-sha256", required=True)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--task-content", type=Path)
    parser.add_argument("--baseline-verifier-receipt", type=Path)
    parser.add_argument("--candidate-verifier-receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate(args.frozen_task, args.frozen_task_sha256,
                          args.baseline, args.candidate, args.task_content,
                          args.baseline_verifier_receipt,
                          args.candidate_verifier_receipt)
    except EvaluationError as exc:
        print(json.dumps({"pair_status": "REJECTED", "error": {"code": exc.code,
                                                                  "detail": str(exc)}}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
