# SPDX-License-Identifier: MIT
"""Deterministic reference core for a bounded collaboration cycle.

The module deliberately models identities and state transitions, not providers,
networks, persistence engines, or domain-specific work.  Every public record is
immutable and every generated identity is derived from canonical public data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
from functools import wraps
from hashlib import sha256
from threading import RLock
from typing import Any, Protocol, TypeVar


class EffectState(str, Enum):
    """Whether an executor effect is safe to terminalize."""

    NONE = "none"
    SETTLED = "settled"
    UNSETTLED = "unsettled"


class ContinuationState(str, Enum):
    """Durable ordering for a destination-bound continuation."""

    PREPARED = "prepared"
    DELIVERY_STARTED = "delivery_started"
    DELIVERY_UNSETTLED = "delivery_unsettled"
    ACKNOWLEDGED = "acknowledged"


class ContinuationReconciliationState(str, Enum):
    """Explicit disposition for a callback with unknown delivery."""

    NONE = "none"
    COMMITTED = "committed"


class TerminalStatus(str, Enum):
    """Terminal task outcome, independent from callback delivery."""

    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"


class MissionNextAction(str, Enum):
    """Where control returns after a terminal child cycle."""

    ROUTE_SELECTION = "route_selection"
    READBACK_RECHECK = "readback_recheck"
    MISSION_COMPLETE = "mission_complete"


class PredicateTruth(str, Enum):
    """Authoritative parent truth for one predicate in a versioned snapshot."""

    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    INDETERMINATE = "indeterminate"


class RouteDispositionState(str, Enum):
    """Parent-authored terminal disposition for one bounded route."""

    ABANDONED = "abandoned"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"


class MissionSupersessionState(str, Enum):
    """Evidence-only classification of an outcome from an older revision."""

    OBSOLETE = "obsolete"
    ADOPTED = "adopted"


class BlockerPhase(str, Enum):
    PLANNING = "planning"
    PRE_EXECUTION = "pre_execution"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    CALLBACK = "callback"


class BlockerClass(str, Enum):
    DIAGNOSTIC = "diagnostic"
    PLAN_GAP = "plan_gap"
    CAPABILITY_GAP = "capability_gap"
    STATE_CONFLICT = "state_conflict"
    EFFECT_UNCERTAINTY = "effect_uncertainty"
    POLICY_AUTHORITY = "policy_authority"
    MISSION_AMBIGUITY = "mission_ambiguity"


class RetrySafety(str, Enum):
    SAFE_LOCAL = "safe_local"
    RECONCILIATION_ONLY = "reconciliation_only"
    ESCALATE = "escalate"


class EffectClass(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE_LOCAL = "reversible_local"
    IRREVERSIBLE_LOCAL = "irreversible_local"
    EXTERNAL = "external"


class RecoveryAdmissionState(str, Enum):
    ADMITTED = "admitted"
    ESCALATED = "escalated"
    DUPLICATE = "duplicate"


class FailureOrigin(str, Enum):
    """Authority plane that classified an execution failure."""

    EXECUTOR = "executor"
    HARNESS = "harness"


class FailureCode(str, Enum):
    """Typed fail-closed outcomes exposed by the reference harness."""

    STALE_IDENTITY = "STALE_IDENTITY"
    DUPLICATE_OR_REPLAY = "DUPLICATE_OR_REPLAY"
    UNSETTLED_EFFECT = "UNSETTLED_EFFECT"
    LEASE_CONFLICT = "LEASE_CONFLICT"
    CAS_MISMATCH = "CAS_MISMATCH"
    CALLBACK_TARGET_MISMATCH = "CALLBACK_TARGET_MISMATCH"
    CONVERGENCE_NOT_PROVEN = "CONVERGENCE_NOT_PROVEN"
    REPLAY_IDENTITY_CONFLICT = "REPLAY_IDENTITY_CONFLICT"
    EXECUTOR_ERROR = "EXECUTOR_ERROR"
    CALLBACK_DELIVERY_UNSETTLED = "CALLBACK_DELIVERY_UNSETTLED"
    MISSION_READBACK_INDETERMINATE = "MISSION_READBACK_INDETERMINATE"
    INVALID_FAILURE_CODE = "INVALID_FAILURE_CODE"
    MISSION_COMPLETE = "MISSION_COMPLETE"
    NO_ROUTE = "NO_ROUTE"


EXECUTOR_ORIGIN_FAILURE_CODES = frozenset(
    {
        FailureCode.STALE_IDENTITY,
        FailureCode.UNSETTLED_EFFECT,
        FailureCode.CAS_MISMATCH,
        FailureCode.EXECUTOR_ERROR,
        FailureCode.INVALID_FAILURE_CODE,
    }
)


class HarnessViolation(RuntimeError):
    """A deterministic rejection with a stable machine-readable code."""

    def __init__(self, code: FailureCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


EnumT = TypeVar("EnumT", bound=Enum)


def _atomic(method: Any) -> Any:
    """Run one store operation under its single re-entrant state lock."""

    @wraps(method)
    def locked(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _canonical(getattr(value, item.name)) for item in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 of canonical, compact UTF-8 JSON."""

    encoded = json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{canonical_sha256(value)}"


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_bool(name: str, value: object) -> None:
    if type(value) is not bool:
        raise TypeError(f"{name} must be bool")


def _require_enum(name: str, value: object, enum_type: type[EnumT]) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(
            f"{name} must be {enum_type.__name__}, got {type(value).__name__}"
        )


def _require_optional_enum(name: str, value: object, enum_type: type[EnumT]) -> None:
    if value is not None:
        _require_enum(name, value, enum_type)


def _require_executor_failure_code(name: str, value: object) -> None:
    _require_enum(name, value, FailureCode)
    if value not in EXECUTOR_ORIGIN_FAILURE_CODES:
        raise HarnessViolation(
            FailureCode.INVALID_FAILURE_CODE,
            f"{name} is not an executor-origin failure code",
        )


def _require_reconcilable_failure_code(name: str, value: object) -> None:
    _require_enum(name, value, FailureCode)
    if value in {
        FailureCode.CALLBACK_DELIVERY_UNSETTLED,
        FailureCode.CONVERGENCE_NOT_PROVEN,
        FailureCode.MISSION_READBACK_INDETERMINATE,
        FailureCode.MISSION_COMPLETE,
        FailureCode.NO_ROUTE,
    }:
        raise HarnessViolation(
            FailureCode.INVALID_FAILURE_CODE,
            f"{name} is not an execution-attempt failure code",
        )


def _require_int(name: str, value: object) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be int")


def _require_text_tuple(
    name: str, value: object, *, allow_empty: bool = True, unique: bool = False
) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{name} must be tuple")
    if not allow_empty and not value:
        raise ValueError(f"{name} must contain at least one item")
    for index, item in enumerate(value):
        _require_text(f"{name}[{index}]", item)
    if unique and len(value) != len(set(value)):
        raise ValueError(f"{name} must contain unique items")


def _require_scope_version_ints(
    name: str, scope_versions: tuple[tuple[str, int], ...]
) -> None:
    for scope, version in scope_versions:
        _require_int(f"{name}[{scope!r}]", version)


@dataclass(frozen=True, slots=True)
class Destination:
    """Exact coordinator and thread that must receive the continuation."""

    coordinator_id: str
    thread_id: str

    def __post_init__(self) -> None:
        _require_text("coordinator_id", self.coordinator_id)
        _require_text("thread_id", self.thread_id)


@dataclass(frozen=True, slots=True)
class ExitPredicate:
    key: str
    satisfied: bool = False

    def __post_init__(self) -> None:
        _require_text("predicate key", self.key)
        _require_bool("satisfied", self.satisfied)


@dataclass(frozen=True, slots=True)
class Route:
    """One bounded route for exactly one mission predicate."""

    route_id: str
    predicate_key: str
    executor_id: str
    scope: tuple[str, ...]
    expected_delta: str
    abandon_if: str
    rank: int = 0
    recovery_budget: int = 1

    def __post_init__(self) -> None:
        for name in (
            "route_id",
            "predicate_key",
            "executor_id",
            "expected_delta",
            "abandon_if",
        ):
            _require_text(name, getattr(self, name))
        if not self.scope:
            raise ValueError("scope must contain at least one item")
        if self.scope != tuple(sorted(set(self.scope))):
            raise ValueError("scope must be sorted and contain unique items")
        _require_int("rank", self.rank)
        if self.rank < 0:
            raise ValueError("rank must be non-negative")
        _require_int("recovery_budget", self.recovery_budget)
        if self.recovery_budget < 0:
            raise ValueError("recovery_budget must be non-negative")


@dataclass(frozen=True, slots=True)
class Mission:
    """The parent-owned ordered predicates and candidate routes."""

    mission_id: str
    revision: int
    mode: str
    predicates: tuple[ExitPredicate, ...]
    routes: tuple[Route, ...]
    destination: Destination

    def __post_init__(self) -> None:
        _require_text("mission_id", self.mission_id)
        _require_text("mode", self.mode)
        _require_int("revision", self.revision)
        if self.revision < 1:
            raise ValueError("revision must be at least 1")
        if not self.predicates:
            raise ValueError("mission must contain at least one predicate")
        predicate_keys = tuple(item.key for item in self.predicates)
        if len(predicate_keys) != len(set(predicate_keys)):
            raise ValueError("predicate keys must be unique")
        route_ids = tuple(item.route_id for item in self.routes)
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("route ids must be unique")
        unknown = {item.predicate_key for item in self.routes} - set(predicate_keys)
        if unknown:
            raise ValueError(f"routes reference unknown predicates: {sorted(unknown)}")


@dataclass(frozen=True, slots=True)
class RouteDisposition:
    """Parent evidence that excludes one route from its exact mission revision."""

    mission_id: str
    mission_revision: int
    predicate_key: str
    route_id: str
    state: RouteDispositionState
    evidence_digest: str
    disposition_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "mission_id",
            "predicate_key",
            "route_id",
            "evidence_digest",
        ):
            _require_text(name, getattr(self, name))
        _require_int("mission_revision", self.mission_revision)
        _require_enum("state", self.state, RouteDispositionState)
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "disposition_id"
        }
        object.__setattr__(
            self, "disposition_id", _stable_id("route_disposition", payload)
        )


@dataclass(frozen=True, slots=True)
class MissionSupersessionDisposition:
    """Evidence-only classification of an ACK from a superseded revision."""

    mission_id: str
    superseded_revision: int
    superseding_revision: int
    acknowledgement_id: str
    receipt_id: str
    state: MissionSupersessionState
    evidence_digest: str
    disposition_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "mission_id",
            "acknowledgement_id",
            "receipt_id",
            "evidence_digest",
        ):
            _require_text(name, getattr(self, name))
        _require_int("superseded_revision", self.superseded_revision)
        _require_int("superseding_revision", self.superseding_revision)
        if self.superseding_revision <= self.superseded_revision:
            raise ValueError("superseding_revision must be newer")
        _require_enum("state", self.state, MissionSupersessionState)
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "disposition_id"
        }
        object.__setattr__(
            self,
            "disposition_id",
            _stable_id("mission_supersession", payload),
        )


@dataclass(frozen=True, slots=True)
class TaskContextBinding:
    """Content-addressed task-local context and its J-Space authority contract."""

    task_id: str
    artifact_root: str
    context_path: str
    context_sha256: str
    jspace_path: str
    jspace_sha256: str
    dcf_generation_id: str
    dcf_input_fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "task_id",
            "artifact_root",
            "context_path",
            "jspace_path",
            "dcf_generation_id",
        ):
            _require_text(name, getattr(self, name))
        for name in (
            "context_sha256",
            "jspace_sha256",
            "dcf_input_fingerprint",
        ):
            value = getattr(self, name)
            _require_text(name, value)
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _task_packet_identity_payload(packet: "TaskPacket") -> dict[str, Any]:
    payload = {
        item.name: getattr(packet, item.name)
        for item in fields(packet)
        if item.name not in {"packet_id", "task_context_binding"}
    }
    if packet.task_context_binding is not None:
        payload["task_context_binding"] = packet.task_context_binding
    return payload


@dataclass(frozen=True, slots=True)
class TaskPacket:
    """Content-addressed decision and bounded dispatch packet."""

    mission_id: str
    mission_revision: int
    mission_mode: str
    predicate_key: str
    route_id: str
    executor_id: str
    scope: tuple[str, ...]
    scope_versions: tuple[tuple[str, int], ...]
    expected_delta: str
    abandon_if: str
    recovery_budget: int
    destination: Destination
    task_context_binding: TaskContextBinding | None = None
    packet_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_int("mission_revision", self.mission_revision)
        _require_int("recovery_budget", self.recovery_budget)
        if self.recovery_budget < 0:
            raise ValueError("recovery_budget must be non-negative")
        _require_scope_version_ints("scope_versions", self.scope_versions)
        if self.task_context_binding is not None and not isinstance(
            self.task_context_binding, TaskContextBinding
        ):
            raise TypeError("task_context_binding must be TaskContextBinding or None")
        object.__setattr__(
            self,
            "packet_id",
            _stable_id("packet", _task_packet_identity_payload(self)),
        )


@dataclass(frozen=True, slots=True)
class Lease:
    """Exclusive scope ownership acquired through compare-and-swap."""

    lease_id: str
    packet_id: str
    scope: tuple[str, ...]
    scope_versions: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _require_scope_version_ints("scope_versions", self.scope_versions)


@dataclass(frozen=True, slots=True)
class StepAttempt:
    """One persisted recovery-visible step and its observed effect state."""

    packet_id: str
    lease_id: str
    operation_digest: str
    tool_id: str
    precondition_digest: str
    effect_class: EffectClass
    effect_id: str | None
    effect_state: EffectState
    result_digest: str
    recovery_parent_step_id: str | None = None
    recovery_admission_id: str | None = None
    recovery_action_id: str | None = None
    step_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "packet_id",
            "lease_id",
            "operation_digest",
            "tool_id",
            "precondition_digest",
            "result_digest",
        ):
            _require_text(name, getattr(self, name))
        recovery_bindings = (
            self.recovery_parent_step_id,
            self.recovery_admission_id,
            self.recovery_action_id,
        )
        if any(item is not None for item in recovery_bindings):
            if not all(item is not None for item in recovery_bindings):
                raise ValueError(
                    "recovery step requires parent step, admission, and action identities"
                )
            _require_text("recovery_parent_step_id", self.recovery_parent_step_id)
            _require_text("recovery_admission_id", self.recovery_admission_id)
            _require_text("recovery_action_id", self.recovery_action_id)
        _require_enum("effect_class", self.effect_class, EffectClass)
        _require_enum("effect_state", self.effect_state, EffectState)
        if (
            self.effect_class is EffectClass.READ_ONLY
            and self.effect_state is not EffectState.NONE
        ):
            raise ValueError("read-only steps cannot declare an effect")
        if self.effect_state is EffectState.NONE and self.effect_id is not None:
            raise ValueError("a no-effect step cannot carry effect_id")
        if self.effect_state is not EffectState.NONE:
            _require_text("effect_id", self.effect_id)
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "step_id"
        }
        if self.recovery_parent_step_id is None:
            payload.pop("recovery_admission_id")
            payload.pop("recovery_action_id")
        object.__setattr__(self, "step_id", _stable_id("step_attempt", payload))


@dataclass(frozen=True, slots=True)
class StepEffectReconciliation:
    """Explicit proof for the exact effect identity of an unsettled step."""

    packet_id: str
    lease_id: str
    step_id: str
    effect_id: str
    effect_state: EffectState
    proof_digest: str
    reconciliation_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "packet_id",
            "lease_id",
            "step_id",
            "effect_id",
            "proof_digest",
        ):
            _require_text(name, getattr(self, name))
        _require_enum("effect_state", self.effect_state, EffectState)
        if self.effect_state not in (EffectState.NONE, EffectState.SETTLED):
            raise ValueError("step reconciliation must prove NONE or SETTLED")
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "reconciliation_id"
        }
        object.__setattr__(
            self,
            "reconciliation_id",
            _stable_id("step_effect_reconciliation", payload),
        )


@dataclass(frozen=True, slots=True)
class BlockerReport:
    """Typed blocker bound to one active packet and its step evidence."""

    mission_id: str
    mission_revision: int
    packet_id: str
    lease_id: str
    step_id: str | None
    phase: BlockerPhase
    blocker_class: BlockerClass
    state_digest: str
    evidence_refs: tuple[str, ...]
    effect_state: EffectState
    observed_effect_ids: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    violated_preconditions: tuple[str, ...]
    retry_safety: RetrySafety
    blocker_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("mission_id", "packet_id", "lease_id", "state_digest"):
            _require_text(name, getattr(self, name))
        _require_int("mission_revision", self.mission_revision)
        if self.step_id is not None:
            _require_text("step_id", self.step_id)
        _require_enum("phase", self.phase, BlockerPhase)
        _require_enum("blocker_class", self.blocker_class, BlockerClass)
        _require_enum("effect_state", self.effect_state, EffectState)
        _require_enum("retry_safety", self.retry_safety, RetrySafety)
        for name in (
            "evidence_refs",
            "observed_effect_ids",
            "missing_capabilities",
            "violated_preconditions",
        ):
            _require_text_tuple(name, getattr(self, name), unique=True)
        if self.effect_state is not EffectState.NONE and not self.observed_effect_ids:
            raise ValueError("an effect-bearing blocker requires an observed effect id")
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "blocker_id"
        }
        object.__setattr__(self, "blocker_id", _stable_id("blocker", payload))


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """One proposed operation; it carries no execution authority by itself."""

    action_kind: str
    scope: tuple[str, ...]
    effect_class: EffectClass
    operation_digest: str
    action_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text("action_kind", self.action_kind)
        _require_text_tuple("scope", self.scope, allow_empty=False, unique=True)
        if self.scope != tuple(sorted(self.scope)):
            raise ValueError("scope must use canonical sorted order")
        _require_enum("effect_class", self.effect_class, EffectClass)
        _require_text("operation_digest", self.operation_digest)
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "action_id"
        }
        object.__setattr__(self, "action_id", _stable_id("recovery_action", payload))


@dataclass(frozen=True, slots=True)
class RecoveryProposal:
    """Untrusted model-assisted proposal constrained by the original packet."""

    blocker_id: str
    packet_id: str
    lease_id: str
    predicate_key: str
    expected_delta: str
    destination: Destination
    action_graph: tuple[RecoveryAction, ...]
    required_scope: tuple[str, ...]
    authority_delta: int
    verification_plan: tuple[str, ...]
    budget: int
    confidence: float
    action_fingerprint: str = field(init=False)
    proposal_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "blocker_id",
            "packet_id",
            "lease_id",
            "predicate_key",
            "expected_delta",
        ):
            _require_text(name, getattr(self, name))
        if not isinstance(self.destination, Destination):
            raise TypeError("destination must be Destination")
        if not isinstance(self.action_graph, tuple):
            raise TypeError("action_graph must be tuple")
        for action in self.action_graph:
            if not isinstance(action, RecoveryAction):
                raise TypeError("action_graph items must be RecoveryAction")
        _require_text_tuple("required_scope", self.required_scope, unique=True)
        if self.required_scope != tuple(sorted(self.required_scope)):
            raise ValueError("required_scope must use canonical sorted order")
        _require_int("authority_delta", self.authority_delta)
        _require_text_tuple("verification_plan", self.verification_plan, unique=True)
        _require_int("budget", self.budget)
        if type(self.confidence) not in (int, float):
            raise TypeError("confidence must be a number")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        action_fingerprint = canonical_sha256(self.action_graph)
        object.__setattr__(self, "action_fingerprint", action_fingerprint)
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "proposal_id"
        }
        object.__setattr__(
            self, "proposal_id", _stable_id("recovery_proposal", payload)
        )


@dataclass(frozen=True, slots=True)
class RecoveryAdmission:
    """Deterministic gate outcome; only ADMITTED permits a local recovery."""

    proposal_id: str
    blocker_id: str
    packet_id: str
    lease_id: str
    state: RecoveryAdmissionState
    reason_codes: tuple[str, ...]
    recovery_fingerprint: str
    admission_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "proposal_id",
            "blocker_id",
            "packet_id",
            "lease_id",
            "recovery_fingerprint",
        ):
            _require_text(name, getattr(self, name))
        _require_enum("state", self.state, RecoveryAdmissionState)
        _require_text_tuple("reason_codes", self.reason_codes, unique=True)
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "admission_id"
        }
        object.__setattr__(
            self, "admission_id", _stable_id("recovery_admission", payload)
        )


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Executor-supplied result bound to the admitted packet and lease."""

    executor_id: str
    packet_id: str
    lease_id: str
    effect_id: str
    effect_state: EffectState
    output_digest: str
    predicate_satisfied: bool

    def __post_init__(self) -> None:
        for name in (
            "executor_id",
            "packet_id",
            "lease_id",
            "effect_id",
            "output_digest",
        ):
            _require_text(name, getattr(self, name))
        _require_enum("effect_state", self.effect_state, EffectState)
        _require_bool("predicate_satisfied", self.predicate_satisfied)
        if self.effect_state is EffectState.NONE:
            raise ValueError(
                "executor results must declare settled or unsettled effect state"
            )


@dataclass(frozen=True, slots=True)
class EffectReconciliation:
    """Settlement proof for the already-recorded effect of one attempt."""

    packet_id: str
    lease_id: str
    effect_id: str
    effect_state: EffectState
    settlement_proof_digest: str

    def __post_init__(self) -> None:
        for name in (
            "packet_id",
            "lease_id",
            "effect_id",
            "settlement_proof_digest",
        ):
            _require_text(name, getattr(self, name))
        _require_enum("effect_state", self.effect_state, EffectState)


@dataclass(frozen=True, slots=True)
class ExecutionFailure:
    """A begun attempt that needs explicit effect reconciliation."""

    failure_id: str
    packet_id: str
    lease_id: str
    failure_code: FailureCode
    detail_digest: str
    observed_effect_id: str | None
    failure_origin: FailureOrigin = FailureOrigin.EXECUTOR

    def __post_init__(self) -> None:
        _require_enum("failure_origin", self.failure_origin, FailureOrigin)
        if self.failure_origin is FailureOrigin.EXECUTOR:
            _require_executor_failure_code("failure_code", self.failure_code)
        else:
            _require_reconcilable_failure_code("failure_code", self.failure_code)


class ExecutorFailureSignal(RuntimeError):
    """Generic structured signal preserving an executor's failure identity."""

    def __init__(self, failure: ExecutionFailure) -> None:
        for name in ("packet_id", "lease_id", "detail_digest"):
            _require_text(name, getattr(failure, name))
        self.failure = failure
        super().__init__(f"{failure.failure_code.value}: structured executor failure")


class ContinuationDeliveryFailure(HarnessViolation):
    """A started callback whose destination-side effect is not yet reconciled."""

    def __init__(self, continuation: Continuation, detail_digest: str) -> None:
        _require_text("detail_digest", detail_digest)
        self.continuation = continuation
        self.detail_digest = detail_digest
        super().__init__(
            FailureCode.CALLBACK_DELIVERY_UNSETTLED,
            "callback delivery started but its destination outcome is unknown",
        )


@dataclass(frozen=True, slots=True)
class FailureReconciliation:
    """Explicit proof that a failed attempt had no effect or a settled effect."""

    packet_id: str
    lease_id: str
    failure_code: FailureCode
    effect_state: EffectState
    effect_id: str | None
    proof_digest: str
    output_digest: str

    def __post_init__(self) -> None:
        for name in ("packet_id", "lease_id", "proof_digest", "output_digest"):
            _require_text(name, getattr(self, name))
        _require_reconcilable_failure_code("failure_code", self.failure_code)
        _require_enum("effect_state", self.effect_state, EffectState)
        if self.effect_state not in (EffectState.NONE, EffectState.SETTLED):
            raise ValueError("failure reconciliation must prove NONE or SETTLED")
        if self.effect_state is EffectState.SETTLED and not self.effect_id:
            raise ValueError("a settled failure reconciliation requires effect_id")
        if self.effect_state is EffectState.NONE and self.effect_id is not None:
            raise ValueError("a no-effect reconciliation cannot carry effect_id")


@dataclass(frozen=True, slots=True)
class TerminalReceipt:
    """Store-authored terminal identity after effect reconciliation."""

    receipt_id: str
    mission_id: str
    mission_revision: int
    packet_id: str
    lease_id: str
    executor_id: str
    status: TerminalStatus
    effect_id: str | None
    effect_state: EffectState
    output_digest: str
    settlement_proof_digest: str | None
    predicate_satisfied: bool
    failure_code: FailureCode | None
    failure_origin: FailureOrigin | None

    def __post_init__(self) -> None:
        _require_int("mission_revision", self.mission_revision)
        _require_enum("status", self.status, TerminalStatus)
        _require_enum("effect_state", self.effect_state, EffectState)
        _require_bool("predicate_satisfied", self.predicate_satisfied)
        _require_optional_enum("failure_code", self.failure_code, FailureCode)
        _require_optional_enum("failure_origin", self.failure_origin, FailureOrigin)
        if (self.failure_code is None) != (self.failure_origin is None):
            raise ValueError("failure_code and failure_origin must be present together")


@dataclass(frozen=True, slots=True)
class Continuation:
    """Destination-bound callback state derived from one terminal receipt."""

    continuation_id: str
    receipt_id: str
    destination: Destination
    turn_request_id: str
    state: ContinuationState

    def __post_init__(self) -> None:
        _require_text("turn_request_id", self.turn_request_id)
        _require_enum("state", self.state, ContinuationState)


@dataclass(frozen=True, slots=True)
class ContinuationReconciliation:
    """Proof-bound disposition for one unsettled callback delivery."""

    continuation_id: str
    receipt_id: str
    turn_request_id: str
    state: ContinuationReconciliationState
    proof_digest: str

    def __post_init__(self) -> None:
        for name in (
            "continuation_id",
            "receipt_id",
            "turn_request_id",
            "proof_digest",
        ):
            _require_text(name, getattr(self, name))
        _require_enum("state", self.state, ContinuationReconciliationState)


@dataclass(frozen=True, slots=True)
class ContinuationDeliveryAbsenceProof:
    """Authoritative destination readback proving no turn was committed."""

    continuation_id: str
    receipt_id: str
    destination: Destination
    turn_request_id: str
    authority_source: str
    evidence_digest: str
    delivery_absent: bool
    proof_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "continuation_id",
            "receipt_id",
            "turn_request_id",
            "authority_source",
            "evidence_digest",
        ):
            _require_text(name, getattr(self, name))
        if not isinstance(self.destination, Destination):
            raise TypeError("destination must be Destination")
        _require_bool("delivery_absent", self.delivery_absent)
        if not self.delivery_absent:
            raise ValueError("absence proof must affirm delivery_absent")
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "proof_id"
        }
        object.__setattr__(self, "proof_id", _stable_id("delivery_absence", payload))


@dataclass(frozen=True, slots=True)
class ResumeProof:
    """Proof that the exact destination was resumed."""

    continuation_id: str
    destination: Destination
    resume_token: str


@dataclass(frozen=True, slots=True)
class ConvergenceProof:
    """Proof that the exact completed turn appeared at the destination."""

    continuation_id: str
    receipt_id: str
    destination: Destination
    turn_request_id: str
    completed_turn_id: str
    completed: bool
    proof_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text("turn_request_id", self.turn_request_id)
        _require_bool("completed", self.completed)
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "proof_id"
        }
        object.__setattr__(self, "proof_id", _stable_id("convergence_proof", payload))


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    """Three identities sequenced atomically after convergence."""

    acknowledgement_id: str
    packet_id: str
    receipt_id: str
    continuation_id: str
    proof_id: str
    callback_ack_id: str
    receipt_ack_id: str
    continuation_ack_id: str


@dataclass(frozen=True, slots=True)
class MissionVerification:
    """Parent mission readback after acknowledgement."""

    verification_id: str
    mission_id: str
    mission_revision: int
    parent_state_revision: str
    parent_state_sequence: int
    predicate_key: str
    predicate_truth: PredicateTruth
    next_predicate_key: str | None
    next_action: MissionNextAction
    acknowledgement_id: str
    readback_id: str
    readback_evidence_digest: str

    def __post_init__(self) -> None:
        _require_int("mission_revision", self.mission_revision)
        _require_int("parent_state_sequence", self.parent_state_sequence)
        if self.parent_state_sequence < 0:
            raise ValueError("parent_state_sequence must be non-negative")
        _require_text("parent_state_revision", self.parent_state_revision)
        _require_text("readback_id", self.readback_id)
        _require_text("readback_evidence_digest", self.readback_evidence_digest)
        _require_enum("predicate_truth", self.predicate_truth, PredicateTruth)
        _require_enum("next_action", self.next_action, MissionNextAction)

    @property
    def predicate_satisfied(self) -> bool:
        return self.predicate_truth is PredicateTruth.SATISFIED


@dataclass(frozen=True, slots=True)
class PredicateReadback:
    """One predicate truth from a single parent-state snapshot."""

    predicate_key: str
    truth: PredicateTruth
    evidence_digest: str

    def __post_init__(self) -> None:
        _require_text("predicate_key", self.predicate_key)
        _require_enum("truth", self.truth, PredicateTruth)
        _require_text("evidence_digest", self.evidence_digest)


@dataclass(frozen=True, slots=True)
class MissionSnapshotReadback:
    """Complete ordered mission truth from one versioned parent state."""

    mission_id: str
    mission_revision: int
    parent_state_revision: str
    parent_state_sequence: int
    predicates: tuple[PredicateReadback, ...]
    evidence_digest: str
    readback_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("mission_id", "parent_state_revision", "evidence_digest"):
            _require_text(name, getattr(self, name))
        _require_int("mission_revision", self.mission_revision)
        _require_int("parent_state_sequence", self.parent_state_sequence)
        if self.mission_revision < 1:
            raise ValueError("mission_revision must be at least 1")
        if self.parent_state_sequence < 0:
            raise ValueError("parent_state_sequence must be non-negative")
        if not self.predicates:
            raise ValueError("snapshot must contain at least one predicate")
        keys = tuple(item.predicate_key for item in self.predicates)
        if len(keys) != len(set(keys)):
            raise ValueError("snapshot predicate keys must be unique")
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "readback_id"
        }
        object.__setattr__(self, "readback_id", _stable_id("readback", payload))


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Complete evidence chain for one successful bounded cycle."""

    packet: TaskPacket
    lease: Lease
    receipt: TerminalReceipt
    continuation: Continuation
    resume_proof: ResumeProof
    convergence_proof: ConvergenceProof
    acknowledgement: Acknowledgement
    verification: MissionVerification


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """An acknowledged replay is deliberately empty."""

    packet_id: str
    effects: tuple[str, ...] = ()
    continuations: tuple[str, ...] = ()
    acknowledgements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    mission_count: int
    active_lease_count: int
    execution_attempt_count: int
    effect_count: int
    terminal_receipt_count: int
    continuation_count: int
    acknowledgement_count: int
    verification_count: int

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_int(item.name, getattr(self, item.name))


def verify_identity(record: object) -> bool:
    """Recompute a supported content-addressed record from persisted fields."""

    if isinstance(record, TaskPacket):
        return record.packet_id == _stable_id(
            "packet", _task_packet_identity_payload(record)
        )
    if isinstance(record, Lease):
        payload = {
            "packet_id": record.packet_id,
            "scope_versions": record.scope_versions,
        }
        return record.lease_id == _stable_id("lease", payload)
    if isinstance(record, StepAttempt):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "step_id"
        }
        if record.recovery_parent_step_id is None:
            payload.pop("recovery_admission_id")
            payload.pop("recovery_action_id")
        return record.step_id == _stable_id("step_attempt", payload)
    if isinstance(record, StepEffectReconciliation):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "reconciliation_id"
        }
        return record.reconciliation_id == _stable_id(
            "step_effect_reconciliation", payload
        )
    if isinstance(record, BlockerReport):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "blocker_id"
        }
        return record.blocker_id == _stable_id("blocker", payload)
    if isinstance(record, RecoveryAction):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "action_id"
        }
        return record.action_id == _stable_id("recovery_action", payload)
    if isinstance(record, RecoveryProposal):
        action_fingerprint = canonical_sha256(record.action_graph)
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "proposal_id"
        }
        return (
            record.action_fingerprint == action_fingerprint
            and record.proposal_id == _stable_id("recovery_proposal", payload)
            and all(verify_identity(action) for action in record.action_graph)
        )
    if isinstance(record, RecoveryAdmission):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "admission_id"
        }
        return record.admission_id == _stable_id("recovery_admission", payload)
    if isinstance(record, ExecutionFailure):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "failure_id"
        }
        return record.failure_id == _stable_id("execution_failure", payload)
    if isinstance(record, TerminalReceipt):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "receipt_id"
        }
        return record.receipt_id == _stable_id("receipt", payload)
    if isinstance(record, Continuation):
        continuation_id = _stable_id(
            "continuation",
            {"receipt_id": record.receipt_id, "destination": record.destination},
        )
        turn_request_id = _stable_id(
            "turn_request",
            {
                "continuation_id": record.continuation_id,
                "receipt_id": record.receipt_id,
                "destination": record.destination,
            },
        )
        return (
            record.continuation_id == continuation_id
            and record.turn_request_id == turn_request_id
        )
    if isinstance(record, ContinuationDeliveryAbsenceProof):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "proof_id"
        }
        return record.proof_id == _stable_id("delivery_absence", payload)
    if isinstance(record, RouteDisposition):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "disposition_id"
        }
        return record.disposition_id == _stable_id("route_disposition", payload)
    if isinstance(record, MissionSupersessionDisposition):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "disposition_id"
        }
        return record.disposition_id == _stable_id("mission_supersession", payload)
    if isinstance(record, MissionSnapshotReadback):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "readback_id"
        }
        return record.readback_id == _stable_id("readback", payload)
    if isinstance(record, ConvergenceProof):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "proof_id"
        }
        return record.proof_id == _stable_id("convergence_proof", payload)
    if isinstance(record, Acknowledgement):
        payload = {
            "packet_id": record.packet_id,
            "receipt_id": record.receipt_id,
            "continuation_id": record.continuation_id,
            "proof_id": record.proof_id,
        }
        return (
            record.acknowledgement_id == _stable_id("ack", payload)
            and record.callback_ack_id == _stable_id("callback_ack", payload)
            and record.receipt_ack_id == _stable_id("receipt_ack", payload)
            and record.continuation_ack_id == _stable_id("continuation_ack", payload)
        )
    if isinstance(record, MissionVerification):
        payload = {
            item.name: getattr(record, item.name)
            for item in fields(record)
            if item.name != "verification_id"
        }
        return record.verification_id == _stable_id("verification", payload)
    raise TypeError(f"unsupported identity record: {type(record).__name__}")


class Executor(Protocol):
    """One-shot work implementation selected by a route."""

    executor_id: str

    def execute(self, packet: TaskPacket, lease: Lease) -> ExecutionResult:
        """Perform one bounded attempt and return its exact identities."""


class ContinuationTransport(Protocol):
    """Guided continuation transport; it never owns mission verification."""

    def resume(self, continuation: Continuation) -> ResumeProof:
        """Resume the exact destination before a new turn is started."""

    def start_turn(
        self,
        continuation: Continuation,
        resume_proof: ResumeProof,
        receipt: TerminalReceipt,
    ) -> ConvergenceProof:
        """Start and prove the exact completed destination turn."""


class InMemoryStore:
    """Deterministic reference store with lease, CAS, replay, and ACK rules."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._missions: dict[str, Mission] = {}
        self._scope_versions: dict[str, int] = {}
        self._active_leases: dict[str, Lease] = {}
        self._leases_by_packet: dict[str, Lease] = {}
        self._packets: dict[str, TaskPacket] = {}
        self._execution_started: set[str] = set()
        self._attempts: dict[str, ExecutionResult] = {}
        self._execution_failures: dict[str, ExecutionFailure] = {}
        self._effect_owner: dict[str, str] = {}
        self._receipts: dict[str, TerminalReceipt] = {}
        self._receipt_by_packet: dict[str, TerminalReceipt] = {}
        self._continuations: dict[str, Continuation] = {}
        self._continuation_by_receipt: dict[str, str] = {}
        self._acknowledgements: dict[str, Acknowledgement] = {}
        self._ack_by_packet: dict[str, Acknowledgement] = {}
        self._verifications: dict[str, MissionVerification] = {}
        self._mission_snapshots: dict[tuple[str, int], MissionSnapshotReadback] = {}
        self._route_dispositions: dict[tuple[str, int, str], RouteDisposition] = {}
        self._mission_supersessions: dict[
            tuple[str, int, int, str], MissionSupersessionDisposition
        ] = {}
        self._step_attempts: dict[str, StepAttempt] = {}
        self._steps_by_packet: dict[str, list[str]] = {}
        self._step_effect_owner: dict[str, str] = {}
        self._step_reconciliations: dict[str, StepEffectReconciliation] = {}
        self._blockers: dict[str, BlockerReport] = {}
        self._recovery_proposals: dict[str, RecoveryProposal] = {}
        self._recovery_admissions: dict[str, RecoveryAdmission] = {}
        self._recovery_admission_by_id: dict[str, RecoveryAdmission] = {}
        self._recovery_fingerprints: set[str] = set()
        self._recovery_budget_consumed: dict[str, int] = {}

    @_atomic
    def register_mission(self, mission: Mission) -> None:
        current = self._missions.get(mission.mission_id)
        if current is None:
            self._missions[mission.mission_id] = mission
            return
        if mission == current:
            return
        if mission.revision <= current.revision:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                f"mission {mission.mission_id!r} is not newer than revision {current.revision}",
            )
        self._missions[mission.mission_id] = mission

    @_atomic
    def record_route_disposition(
        self, disposition: RouteDisposition
    ) -> RouteDisposition:
        mission = self._missions.get(disposition.mission_id)
        route = None
        if mission is not None and mission.revision == disposition.mission_revision:
            route = next(
                (
                    item
                    for item in mission.routes
                    if item.route_id == disposition.route_id
                ),
                None,
            )
        if route is None or route.predicate_key != disposition.predicate_key:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "route disposition does not bind a current mission route",
            )
        if not verify_identity(disposition):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "route disposition identity was tampered",
            )
        key = (
            disposition.mission_id,
            disposition.mission_revision,
            disposition.route_id,
        )
        existing = self._route_dispositions.get(key)
        if existing is not None:
            if existing == disposition:
                return existing
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "route already has a different terminal disposition",
            )
        self._route_dispositions[key] = disposition
        return disposition

    @_atomic
    def route_is_disposed(
        self, mission_id: str, mission_revision: int, route_id: str
    ) -> bool:
        return (mission_id, mission_revision, route_id) in self._route_dispositions

    @_atomic
    def record_mission_supersession(
        self, disposition: MissionSupersessionDisposition
    ) -> MissionSupersessionDisposition:
        current = self._missions.get(disposition.mission_id)
        acknowledgement = self._acknowledgements.get(disposition.acknowledgement_id)
        receipt = self._receipts.get(disposition.receipt_id)
        packet = (
            None
            if acknowledgement is None
            else self._packets.get(acknowledgement.packet_id)
        )
        if (
            current is None
            or current.revision != disposition.superseding_revision
            or acknowledgement is None
            or receipt is None
            or packet is None
            or acknowledgement.receipt_id != receipt.receipt_id
            or packet.mission_id != disposition.mission_id
            or packet.mission_revision != disposition.superseded_revision
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "supersession disposition does not bind old evidence and current mission",
            )
        if (
            not verify_identity(disposition)
            or not verify_identity(acknowledgement)
            or not verify_identity(receipt)
        ):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "supersession evidence identity was tampered",
            )
        key = (
            disposition.mission_id,
            disposition.superseded_revision,
            disposition.superseding_revision,
            disposition.acknowledgement_id,
        )
        existing = self._mission_supersessions.get(key)
        if existing is not None:
            if existing == disposition:
                return existing
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "old mission evidence already has a different disposition",
            )
        self._mission_supersessions[key] = disposition
        return disposition

    @_atomic
    def scope_snapshot(self, scope: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
        return tuple((item, self._scope_versions.get(item, 0)) for item in scope)

    @_atomic
    def first_false(self, mission: Mission) -> ExitPredicate | None:
        snapshot = self._mission_snapshots.get((mission.mission_id, mission.revision))
        if snapshot is None:
            truths = tuple(
                PredicateTruth.SATISFIED
                if predicate.satisfied
                else PredicateTruth.UNSATISFIED
                for predicate in mission.predicates
            )
        else:
            truths = tuple(item.truth for item in snapshot.predicates)
        indeterminate = next(
            (
                predicate
                for predicate, truth in zip(mission.predicates, truths, strict=True)
                if truth is PredicateTruth.INDETERMINATE
            ),
            None,
        )
        if indeterminate is not None:
            raise HarnessViolation(
                FailureCode.MISSION_READBACK_INDETERMINATE,
                f"predicate {indeterminate.key!r} requires parent readback recheck",
            )
        for predicate, truth in zip(mission.predicates, truths, strict=True):
            if truth is PredicateTruth.UNSATISFIED:
                return predicate
        return None

    def _validate_packet(self, packet: TaskPacket) -> Mission:
        if not verify_identity(packet):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "packet identity does not match its content-addressed fields",
            )
        mission = self._missions.get(packet.mission_id)
        if mission is None or mission.revision != packet.mission_revision:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                f"packet {packet.packet_id} does not match the authoritative mission revision",
            )
        predicate = self.first_false(mission)
        if predicate is None or predicate.key != packet.predicate_key:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "packet no longer targets the first false predicate",
            )
        route = next(
            (item for item in mission.routes if item.route_id == packet.route_id), None
        )
        if (
            route is not None
            and (mission.mission_id, mission.revision, route.route_id)
            in self._route_dispositions
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "packet route has a terminal parent disposition",
            )
        expected = None
        if route is not None:
            expected = (
                route.predicate_key,
                route.executor_id,
                route.scope,
                route.expected_delta,
                route.abandon_if,
                route.recovery_budget,
            )
        actual = (
            packet.predicate_key,
            packet.executor_id,
            packet.scope,
            packet.expected_delta,
            packet.abandon_if,
            packet.recovery_budget,
        )
        if (
            packet.mission_mode != mission.mode
            or packet.destination != mission.destination
            or expected != actual
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "packet fields do not match the authoritative route",
            )
        return mission

    @_atomic
    def claim(self, packet: TaskPacket) -> Lease:
        self._validate_packet(packet)
        if packet.packet_id in self._leases_by_packet:
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                f"packet {packet.packet_id} was already claimed",
            )
        requested = set(packet.scope)
        for lease in self._active_leases.values():
            overlap = sorted(requested.intersection(lease.scope))
            if overlap:
                raise HarnessViolation(
                    FailureCode.LEASE_CONFLICT,
                    f"scope already leased: {overlap}",
                )
        current_versions = self.scope_snapshot(packet.scope)
        if current_versions != packet.scope_versions:
            raise HarnessViolation(
                FailureCode.CAS_MISMATCH,
                f"scope version changed from {packet.scope_versions} to {current_versions}",
            )
        claimed_versions = tuple(
            (item, version + 1) for item, version in current_versions
        )
        for item, version in claimed_versions:
            self._scope_versions[item] = version
        lease_id = _stable_id(
            "lease",
            {
                "packet_id": packet.packet_id,
                "scope_versions": claimed_versions,
            },
        )
        lease = Lease(lease_id, packet.packet_id, packet.scope, claimed_versions)
        self._packets[packet.packet_id] = packet
        self._active_leases[lease_id] = lease
        self._leases_by_packet[packet.packet_id] = lease
        return lease

    def _validate_active_claim(
        self, packet: TaskPacket, lease: Lease, *, current_mission_required: bool = True
    ) -> None:
        if current_mission_required:
            self._validate_packet(packet)
        stored = self._active_leases.get(lease.lease_id)
        if (
            stored != lease
            or self._packets.get(packet.packet_id) != packet
            or self._leases_by_packet.get(packet.packet_id) != lease
            or lease.packet_id != packet.packet_id
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "execution does not hold the active lease for this packet",
            )

    @_atomic
    def begin_execution(self, packet: TaskPacket, lease: Lease) -> None:
        if (
            packet.packet_id in self._execution_started
            or packet.packet_id in self._receipt_by_packet
        ):
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                f"packet {packet.packet_id} already has an execution attempt",
            )
        self._validate_active_claim(packet, lease)
        self._execution_started.add(packet.packet_id)

    @_atomic
    def record_step_attempt(
        self, packet: TaskPacket, lease: Lease, step: StepAttempt
    ) -> StepAttempt:
        """Persist one exact step while the packet still owns its lease."""

        self._validate_active_claim(packet, lease)
        if packet.packet_id not in self._execution_started:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY, "step packet execution was never started"
            )
        if step.packet_id != packet.packet_id or step.lease_id != lease.lease_id:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "step does not bind the active packet and lease",
            )
        if not verify_identity(step):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "step identity was tampered",
            )
        if step.recovery_parent_step_id is not None:
            parent = self._step_attempts.get(step.recovery_parent_step_id)
            if parent is None or parent.packet_id != packet.packet_id:
                raise HarnessViolation(
                    FailureCode.STALE_IDENTITY,
                    "recovery step does not bind a prior step in this packet",
                )
            admission = self._recovery_admission_by_id.get(
                step.recovery_admission_id or ""
            )
            proposal = (
                None
                if admission is None
                else self._recovery_proposals.get(admission.proposal_id)
            )
            action = (
                None
                if proposal is None
                else next(
                    (
                        item
                        for item in proposal.action_graph
                        if item.action_id == step.recovery_action_id
                    ),
                    None,
                )
            )
            if (
                admission is None
                or admission.state is not RecoveryAdmissionState.ADMITTED
                or admission.packet_id != packet.packet_id
                or admission.lease_id != lease.lease_id
                or proposal is None
                or proposal.blocker_id != admission.blocker_id
                or action is None
                or action.operation_digest != step.operation_digest
                or action.effect_class is not step.effect_class
            ):
                raise HarnessViolation(
                    FailureCode.STALE_IDENTITY,
                    "recovery step does not bind an admitted action for this packet",
                )
        existing = self._step_attempts.get(step.step_id)
        if existing is not None:
            if existing == step:
                return existing
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "step id conflicts with persisted fields",
            )
        if step.effect_id is not None:
            owner = self._step_effect_owner.get(step.effect_id)
            if owner is not None and owner != step.step_id:
                raise HarnessViolation(
                    FailureCode.REPLAY_IDENTITY_CONFLICT,
                    "step effect id is already owned by another step",
                )
            self._step_effect_owner[step.effect_id] = step.step_id
        self._step_attempts[step.step_id] = step
        self._steps_by_packet.setdefault(packet.packet_id, []).append(step.step_id)
        return step

    def _effective_step_state(self, step: StepAttempt) -> EffectState:
        reconciliation = self._step_reconciliations.get(step.step_id)
        return (
            step.effect_state if reconciliation is None else reconciliation.effect_state
        )

    @_atomic
    def reconcile_step_effect(
        self,
        packet: TaskPacket,
        lease: Lease,
        reconciliation: StepEffectReconciliation,
    ) -> StepEffectReconciliation:
        """Resolve only the exact unsettled effect previously recorded for a step."""

        self._validate_active_claim(packet, lease)
        step = self._step_attempts.get(reconciliation.step_id)
        if (
            step is None
            or step.packet_id != packet.packet_id
            or step.lease_id != lease.lease_id
            or reconciliation.packet_id != packet.packet_id
            or reconciliation.lease_id != lease.lease_id
            or step.effect_state is not EffectState.UNSETTLED
            or reconciliation.effect_id != step.effect_id
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "step reconciliation does not bind the exact unsettled effect",
            )
        if not verify_identity(reconciliation):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "step reconciliation identity was tampered",
            )
        existing = self._step_reconciliations.get(step.step_id)
        if existing is not None:
            if existing == reconciliation:
                return existing
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "step already has a different effect reconciliation",
            )
        self._step_reconciliations[step.step_id] = reconciliation
        return reconciliation

    @_atomic
    def record_blocker(
        self, packet: TaskPacket, lease: Lease, blocker: BlockerReport
    ) -> BlockerReport:
        """Persist a blocker without terminalizing its packet or mission."""

        self._validate_active_claim(packet, lease)
        if (
            blocker.mission_id != packet.mission_id
            or blocker.mission_revision != packet.mission_revision
            or blocker.packet_id != packet.packet_id
            or blocker.lease_id != lease.lease_id
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "blocker does not bind the active mission, packet, and lease",
            )
        if not verify_identity(blocker):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "blocker identity was tampered",
            )
        if blocker.step_id is None:
            if blocker.effect_state is not EffectState.NONE:
                raise HarnessViolation(
                    FailureCode.STALE_IDENTITY,
                    "an effect-bearing blocker must bind a persisted step",
                )
        else:
            step = self._step_attempts.get(blocker.step_id)
            if (
                step is None
                or step.packet_id != packet.packet_id
                or blocker.effect_state is not self._effective_step_state(step)
            ):
                raise HarnessViolation(
                    FailureCode.STALE_IDENTITY,
                    "blocker does not match the effective state of its step",
                )
            expected_effects = () if step.effect_id is None else (step.effect_id,)
            if blocker.observed_effect_ids != expected_effects:
                raise HarnessViolation(
                    FailureCode.STALE_IDENTITY,
                    "blocker observed effects do not match its exact step",
                )
        existing = self._blockers.get(blocker.blocker_id)
        if existing is not None:
            if existing == blocker:
                return existing
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "blocker id conflicts with persisted fields",
            )
        self._blockers[blocker.blocker_id] = blocker
        return blocker

    @_atomic
    def admit_recovery(
        self,
        packet: TaskPacket,
        lease: Lease,
        blocker: BlockerReport,
        proposal: RecoveryProposal,
    ) -> RecoveryAdmission:
        """Admit a proposal only inside the original packet authority envelope."""

        self._validate_active_claim(packet, lease)
        if self._blockers.get(blocker.blocker_id) != blocker:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "recovery does not bind a persisted blocker",
            )
        if (
            proposal.blocker_id != blocker.blocker_id
            or proposal.packet_id != packet.packet_id
            or proposal.lease_id != lease.lease_id
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "recovery proposal does not bind the exact blocker and lease",
            )
        if not verify_identity(proposal):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "recovery proposal identity was tampered",
            )
        existing = self._recovery_admissions.get(proposal.proposal_id)
        if existing is not None:
            return existing

        reasons: list[str] = []
        packet_scope = set(packet.scope)
        if not set(proposal.required_scope).issubset(packet_scope) or any(
            not set(action.scope).issubset(packet_scope)
            for action in proposal.action_graph
        ):
            reasons.append("SCOPE_EXPANSION")
        if proposal.authority_delta != 0:
            reasons.append("AUTHORITY_EXPANSION")
        if blocker.retry_safety is not RetrySafety.SAFE_LOCAL:
            reasons.append("RETRY_NOT_SAFE_LOCAL")
        if blocker.blocker_class not in {
            BlockerClass.DIAGNOSTIC,
            BlockerClass.PLAN_GAP,
        }:
            reasons.append("BLOCKER_REQUIRES_PARENT_AUTHORITY")
        if blocker.effect_state is EffectState.UNSETTLED or any(
            self._effective_step_state(self._step_attempts[step_id])
            is EffectState.UNSETTLED
            for step_id in self._steps_by_packet.get(packet.packet_id, ())
        ):
            reasons.append("STEP_EFFECT_UNSETTLED")
        if not proposal.verification_plan:
            reasons.append("VERIFICATION_PLAN_REQUIRED")
        if not proposal.action_graph:
            reasons.append("ACTION_GRAPH_REQUIRED")
        if any(
            action.effect_class
            in (EffectClass.IRREVERSIBLE_LOCAL, EffectClass.EXTERNAL)
            for action in proposal.action_graph
        ):
            reasons.append("NEW_PROTECTED_EFFECT")
        if proposal.predicate_key != packet.predicate_key:
            reasons.append("PREDICATE_MISMATCH")
        if proposal.expected_delta != packet.expected_delta:
            reasons.append("EXPECTED_DELTA_MISMATCH")
        if proposal.destination != packet.destination:
            reasons.append("DESTINATION_MISMATCH")
        consumed_budget = self._recovery_budget_consumed.get(packet.packet_id, 0)
        if proposal.budget <= 0 or (
            consumed_budget + proposal.budget > packet.recovery_budget
        ):
            reasons.append("RECOVERY_BUDGET_EXHAUSTED")

        recovery_fingerprint = canonical_sha256(
            {
                "blocker_id": blocker.blocker_id,
                "state_digest": blocker.state_digest,
                "action_fingerprint": proposal.action_fingerprint,
            }
        )
        if recovery_fingerprint in self._recovery_fingerprints:
            state = RecoveryAdmissionState.DUPLICATE
            reasons = ["DUPLICATE_FINGERPRINT"]
        elif reasons:
            state = RecoveryAdmissionState.ESCALATED
        else:
            state = RecoveryAdmissionState.ADMITTED
            self._recovery_fingerprints.add(recovery_fingerprint)
            self._recovery_budget_consumed[packet.packet_id] = (
                consumed_budget + proposal.budget
            )
        admission = RecoveryAdmission(
            proposal_id=proposal.proposal_id,
            blocker_id=blocker.blocker_id,
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            state=state,
            reason_codes=tuple(reasons),
            recovery_fingerprint=recovery_fingerprint,
        )
        self._recovery_proposals[proposal.proposal_id] = proposal
        self._recovery_admissions[proposal.proposal_id] = admission
        self._recovery_admission_by_id[admission.admission_id] = admission
        return admission

    def _assert_packet_effects_reconciled_before_terminal(
        self, packet: TaskPacket
    ) -> None:
        unsettled = tuple(
            step_id
            for step_id in self._steps_by_packet.get(packet.packet_id, ())
            if self._effective_step_state(self._step_attempts[step_id])
            is EffectState.UNSETTLED
        )
        if unsettled:
            raise HarnessViolation(
                FailureCode.UNSETTLED_EFFECT,
                "packet has unresolved step effects: " + ", ".join(unsettled),
            )

    def _persist_terminal(
        self,
        packet: TaskPacket,
        lease: Lease,
        *,
        status: TerminalStatus,
        effect_id: str | None,
        effect_state: EffectState,
        output_digest: str,
        settlement_proof_digest: str | None,
        predicate_satisfied: bool,
        failure_code: FailureCode | None,
        failure_origin: FailureOrigin | None,
    ) -> TerminalReceipt:
        if packet.packet_id in self._receipt_by_packet:
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                f"packet {packet.packet_id} already has a terminal receipt",
            )
        self._validate_active_claim(packet, lease, current_mission_required=False)
        self._assert_packet_effects_reconciled_before_terminal(packet)
        receipt_payload = {
            "mission_id": packet.mission_id,
            "mission_revision": packet.mission_revision,
            "packet_id": packet.packet_id,
            "lease_id": lease.lease_id,
            "executor_id": packet.executor_id,
            "status": status,
            "effect_id": effect_id,
            "effect_state": effect_state,
            "output_digest": output_digest,
            "settlement_proof_digest": settlement_proof_digest,
            "predicate_satisfied": predicate_satisfied,
            "failure_code": failure_code,
            "failure_origin": failure_origin,
        }
        receipt = TerminalReceipt(
            receipt_id=_stable_id("receipt", receipt_payload), **receipt_payload
        )
        self._receipts[receipt.receipt_id] = receipt
        self._receipt_by_packet[packet.packet_id] = receipt
        self._active_leases.pop(lease.lease_id)
        return receipt

    @_atomic
    def terminalize_pre_execution(
        self,
        packet: TaskPacket,
        lease: Lease,
        code: FailureCode,
        detail: str,
    ) -> TerminalReceipt:
        """Persist a typed blocker without inventing an executor result."""

        if packet.packet_id in self._execution_started:
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                "execution already started; rejection is no longer pre-execution",
            )
        self._validate_active_claim(packet, lease, current_mission_required=False)
        return self._persist_terminal(
            packet,
            lease,
            status=TerminalStatus.BLOCKED,
            effect_id=None,
            effect_state=EffectState.NONE,
            output_digest=canonical_sha256(
                {"failure_code": code, "detail": detail, "phase": "pre_execution"}
            ),
            settlement_proof_digest=None,
            predicate_satisfied=False,
            failure_code=code,
            failure_origin=FailureOrigin.HARNESS,
        )

    @_atomic
    def record_execution_failure(
        self,
        packet: TaskPacket,
        lease: Lease,
        code: FailureCode,
        detail_digest: str,
        *,
        observed_effect_id: str | None = None,
        failure_origin: FailureOrigin = FailureOrigin.EXECUTOR,
    ) -> ExecutionFailure:
        """Record effect uncertainty without guessing that no effect occurred."""

        self._validate_active_claim(packet, lease, current_mission_required=False)
        if packet.packet_id not in self._execution_started:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY, "executor attempt was never started"
            )
        if (
            packet.packet_id in self._execution_failures
            or packet.packet_id in self._receipt_by_packet
        ):
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                "execution failure was already recorded or terminalized",
            )
        _require_text("detail_digest", detail_digest)
        payload = {
            "packet_id": packet.packet_id,
            "lease_id": lease.lease_id,
            "failure_code": code,
            "detail_digest": detail_digest,
            "observed_effect_id": observed_effect_id,
            "failure_origin": failure_origin,
        }
        failure = ExecutionFailure(
            failure_id=_stable_id("execution_failure", payload), **payload
        )
        self._execution_failures[packet.packet_id] = failure
        if (
            observed_effect_id is not None
            and observed_effect_id not in self._effect_owner
        ):
            self._effect_owner[observed_effect_id] = packet.packet_id
        return failure

    @_atomic
    def failure_for_packet(self, packet_id: str) -> ExecutionFailure | None:
        return self._execution_failures.get(packet_id)

    @_atomic
    def reconcile_failure(
        self,
        packet: TaskPacket,
        lease: Lease,
        reconciliation: FailureReconciliation,
    ) -> TerminalReceipt:
        """Terminalize one failed attempt only from explicit effect proof."""

        failure = self._execution_failures.get(packet.packet_id)
        if failure is None or packet.packet_id in self._receipt_by_packet:
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                "packet has no unresolved execution failure",
            )
        self._validate_active_claim(packet, lease, current_mission_required=False)
        self._assert_packet_effects_reconciled_before_terminal(packet)
        if (
            reconciliation.packet_id != packet.packet_id
            or reconciliation.lease_id != lease.lease_id
            or reconciliation.failure_code is not failure.failure_code
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "failure reconciliation does not bind the recorded failure",
            )
        if reconciliation.effect_state is EffectState.SETTLED:
            if (
                failure.observed_effect_id is not None
                and reconciliation.effect_id != failure.observed_effect_id
            ):
                raise HarnessViolation(
                    FailureCode.STALE_IDENTITY,
                    "settled effect differs from the executor-observed effect",
                )
            owner = self._effect_owner.get(reconciliation.effect_id or "")
            if owner is not None and owner != packet.packet_id:
                raise HarnessViolation(
                    FailureCode.DUPLICATE_OR_REPLAY,
                    f"effect is already owned by packet {owner}",
                )
            self._effect_owner[reconciliation.effect_id or ""] = packet.packet_id
        elif (
            failure.observed_effect_id is not None
            and self._effect_owner.get(failure.observed_effect_id) == packet.packet_id
        ):
            self._effect_owner.pop(failure.observed_effect_id)
        return self._persist_terminal(
            packet,
            lease,
            status=TerminalStatus.FAILED,
            effect_id=reconciliation.effect_id,
            effect_state=reconciliation.effect_state,
            output_digest=reconciliation.output_digest,
            settlement_proof_digest=reconciliation.proof_digest,
            predicate_satisfied=False,
            failure_code=failure.failure_code,
            failure_origin=failure.failure_origin,
        )

    @_atomic
    def record_execution(
        self, packet: TaskPacket, lease: Lease, result: ExecutionResult
    ) -> TerminalReceipt:
        self._validate_active_claim(packet, lease, current_mission_required=False)
        self._assert_packet_effects_reconciled_before_terminal(packet)
        if packet.packet_id not in self._execution_started:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY, "executor attempt was never started"
            )
        if packet.packet_id in self._attempts:
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                f"packet {packet.packet_id} already returned a result",
            )
        if (
            result.executor_id != packet.executor_id
            or result.packet_id != packet.packet_id
            or result.lease_id != lease.lease_id
        ):
            self.record_execution_failure(
                packet,
                lease,
                FailureCode.STALE_IDENTITY,
                canonical_sha256(
                    {
                        "kind": "executor_result_identity_mismatch",
                        "packet_id": packet.packet_id,
                        "lease_id": lease.lease_id,
                    }
                ),
                observed_effect_id=result.effect_id,
                failure_origin=FailureOrigin.HARNESS,
            )
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "executor result identity does not match packet, executor, and lease",
            )
        owner = self._effect_owner.get(result.effect_id)
        if owner is not None:
            self.record_execution_failure(
                packet,
                lease,
                FailureCode.DUPLICATE_OR_REPLAY,
                canonical_sha256(
                    {
                        "kind": "duplicate_effect_identity",
                        "effect_id": result.effect_id,
                        "existing_owner": owner,
                    }
                ),
                observed_effect_id=result.effect_id,
                failure_origin=FailureOrigin.HARNESS,
            )
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                f"effect {result.effect_id!r} is already owned by packet {owner}",
            )
        self._attempts[packet.packet_id] = result
        self._effect_owner[result.effect_id] = packet.packet_id
        if result.effect_state is not EffectState.SETTLED:
            raise HarnessViolation(
                FailureCode.UNSETTLED_EFFECT,
                f"effect {result.effect_id!r} has no settlement proof",
            )
        return self._persist_terminal(
            packet,
            lease,
            status=TerminalStatus.SUCCEEDED,
            effect_id=result.effect_id,
            effect_state=result.effect_state,
            output_digest=result.output_digest,
            settlement_proof_digest=result.output_digest,
            predicate_satisfied=result.predicate_satisfied,
            failure_code=None,
            failure_origin=None,
        )

    @_atomic
    def reconcile_effect(
        self,
        packet: TaskPacket,
        lease: Lease,
        reconciliation: EffectReconciliation,
    ) -> TerminalReceipt:
        """Settle the recorded attempt without invoking its executor again."""

        result = self._attempts.get(packet.packet_id)
        if result is None or result.effect_state is not EffectState.UNSETTLED:
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                "packet has no unsettled recorded effect to reconcile",
            )
        self._validate_active_claim(packet, lease, current_mission_required=False)
        self._assert_packet_effects_reconciled_before_terminal(packet)
        if (
            reconciliation.packet_id != packet.packet_id
            or reconciliation.lease_id != lease.lease_id
            or reconciliation.effect_id != result.effect_id
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "reconciliation does not match the recorded attempt and effect",
            )
        if reconciliation.effect_state is not EffectState.SETTLED:
            raise HarnessViolation(
                FailureCode.UNSETTLED_EFFECT,
                f"effect {result.effect_id!r} remains unsettled",
            )
        settled = replace(result, effect_state=EffectState.SETTLED)
        self._attempts[packet.packet_id] = settled
        return self._persist_terminal(
            packet,
            lease,
            status=TerminalStatus.SUCCEEDED,
            effect_id=settled.effect_id,
            effect_state=settled.effect_state,
            output_digest=settled.output_digest,
            settlement_proof_digest=reconciliation.settlement_proof_digest,
            predicate_satisfied=settled.predicate_satisfied,
            failure_code=None,
            failure_origin=None,
        )

    @_atomic
    def terminal_receipt_for_packet(self, packet_id: str) -> TerminalReceipt | None:
        return self._receipt_by_packet.get(packet_id)

    @_atomic
    def prepare_continuation(self, receipt: TerminalReceipt) -> Continuation:
        if self._receipts.get(receipt.receipt_id) != receipt:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY, "terminal receipt is not authoritative"
            )
        existing_id = self._continuation_by_receipt.get(receipt.receipt_id)
        if existing_id is not None:
            existing = self._continuations[existing_id]
            if existing.state is ContinuationState.PREPARED:
                return existing
            if existing.state is ContinuationState.DELIVERY_UNSETTLED:
                raise HarnessViolation(
                    FailureCode.CALLBACK_DELIVERY_UNSETTLED,
                    "callback delivery requires explicit reconciliation",
                )
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                f"receipt {receipt.receipt_id} already has a started continuation",
            )
        packet = self._packets[receipt.packet_id]
        continuation_id = _stable_id(
            "continuation",
            {"receipt_id": receipt.receipt_id, "destination": packet.destination},
        )
        turn_request_id = _stable_id(
            "turn_request",
            {
                "continuation_id": continuation_id,
                "receipt_id": receipt.receipt_id,
                "destination": packet.destination,
            },
        )
        continuation = Continuation(
            continuation_id=continuation_id,
            receipt_id=receipt.receipt_id,
            destination=packet.destination,
            turn_request_id=turn_request_id,
            state=ContinuationState.PREPARED,
        )
        self._continuations[continuation_id] = continuation
        self._continuation_by_receipt[receipt.receipt_id] = continuation_id
        return continuation

    @_atomic
    def continuation_for_receipt(self, receipt_id: str) -> Continuation | None:
        continuation_id = self._continuation_by_receipt.get(receipt_id)
        if continuation_id is None:
            return None
        return self._continuations[continuation_id]

    @_atomic
    def validate_prepared_continuation(
        self, continuation: Continuation, receipt: TerminalReceipt
    ) -> None:
        current = self._continuations.get(continuation.continuation_id)
        if (
            current is not None
            and current.state is ContinuationState.DELIVERY_UNSETTLED
        ):
            raise HarnessViolation(
                FailureCode.CALLBACK_DELIVERY_UNSETTLED,
                "callback delivery requires explicit reconciliation",
            )
        if (
            current != continuation
            or current.state is not ContinuationState.PREPARED
            or continuation.receipt_id != receipt.receipt_id
            or self._receipts.get(receipt.receipt_id) != receipt
        ):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "continuation, receipt, or prepared state identity changed",
            )

    @_atomic
    def validate_resume(
        self, continuation: Continuation, proof: ResumeProof
    ) -> Continuation:
        current = self._continuations.get(continuation.continuation_id)
        if current != continuation or current.state is not ContinuationState.PREPARED:
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                "continuation is not the current prepared identity",
            )
        if (
            proof.continuation_id != continuation.continuation_id
            or proof.destination != continuation.destination
        ):
            raise HarnessViolation(
                FailureCode.CALLBACK_TARGET_MISMATCH,
                "resume proof does not bind the requested destination",
            )
        _require_text("resume_token", proof.resume_token)
        return continuation

    @_atomic
    def begin_delivery(
        self, continuation: Continuation, receipt: TerminalReceipt
    ) -> Continuation:
        current = self._continuations.get(continuation.continuation_id)
        if (
            current != continuation
            or current.state is not ContinuationState.PREPARED
            or continuation.receipt_id != receipt.receipt_id
            or self._receipts.get(receipt.receipt_id) != receipt
        ):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "callback delivery identity changed before submission",
            )
        started = replace(continuation, state=ContinuationState.DELIVERY_STARTED)
        self._continuations[continuation.continuation_id] = started
        return started

    @_atomic
    def mark_delivery_unsettled(
        self, continuation: Continuation, receipt: TerminalReceipt
    ) -> Continuation:
        current = self._continuations.get(continuation.continuation_id)
        if (
            current != continuation
            or current.state is not ContinuationState.DELIVERY_STARTED
            or continuation.receipt_id != receipt.receipt_id
        ):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "only the exact started delivery can become unsettled",
            )
        unsettled = replace(continuation, state=ContinuationState.DELIVERY_UNSETTLED)
        self._continuations[continuation.continuation_id] = unsettled
        return unsettled

    def _acknowledge_delivery(
        self,
        continuation: Continuation,
        receipt: TerminalReceipt,
        proof: ConvergenceProof,
        *,
        expected_state: ContinuationState,
    ) -> tuple[Continuation, Acknowledgement]:
        current = self._continuations.get(continuation.continuation_id)
        if current != continuation or current.state is not expected_state:
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                "continuation is not in the expected delivery state",
            )
        if (
            continuation.receipt_id != receipt.receipt_id
            or proof.continuation_id != continuation.continuation_id
            or proof.receipt_id != receipt.receipt_id
            or proof.destination != continuation.destination
            or proof.turn_request_id != continuation.turn_request_id
        ):
            raise HarnessViolation(
                FailureCode.CALLBACK_TARGET_MISMATCH,
                "completed turn proof does not bind receipt, destination, and request",
            )
        if not proof.completed or not proof.completed_turn_id.strip():
            raise HarnessViolation(
                FailureCode.CONVERGENCE_NOT_PROVEN,
                "destination has no exact completed turn proof",
            )
        if not verify_identity(proof):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "convergence proof identity does not match its persisted fields",
            )
        ack_base = {
            "packet_id": receipt.packet_id,
            "receipt_id": receipt.receipt_id,
            "continuation_id": continuation.continuation_id,
            "proof_id": proof.proof_id,
        }
        ack = Acknowledgement(
            acknowledgement_id=_stable_id("ack", ack_base),
            packet_id=receipt.packet_id,
            receipt_id=receipt.receipt_id,
            continuation_id=continuation.continuation_id,
            proof_id=proof.proof_id,
            callback_ack_id=_stable_id("callback_ack", ack_base),
            receipt_ack_id=_stable_id("receipt_ack", ack_base),
            continuation_ack_id=_stable_id("continuation_ack", ack_base),
        )
        acknowledged = replace(continuation, state=ContinuationState.ACKNOWLEDGED)
        self._continuations[continuation.continuation_id] = acknowledged
        self._acknowledgements[ack.acknowledgement_id] = ack
        self._ack_by_packet[receipt.packet_id] = ack
        return acknowledged, ack

    @_atomic
    def acknowledge_after_convergence(
        self,
        continuation: Continuation,
        receipt: TerminalReceipt,
        proof: ConvergenceProof,
    ) -> tuple[Continuation, Acknowledgement]:
        return self._acknowledge_delivery(
            continuation,
            receipt,
            proof,
            expected_state=ContinuationState.DELIVERY_STARTED,
        )

    @_atomic
    def reconcile_continuation(
        self,
        receipt: TerminalReceipt,
        reconciliation: ContinuationReconciliation,
        proof: ConvergenceProof | ContinuationDeliveryAbsenceProof | None = None,
    ) -> tuple[Continuation, Acknowledgement | None]:
        current = self._continuations.get(reconciliation.continuation_id)
        if (
            current is None
            or current.state is not ContinuationState.DELIVERY_UNSETTLED
            or self._receipts.get(receipt.receipt_id) != receipt
        ):
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                "continuation is not an authoritative unsettled delivery",
            )
        if (
            reconciliation.receipt_id != receipt.receipt_id
            or current.receipt_id != receipt.receipt_id
            or reconciliation.turn_request_id != current.turn_request_id
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "reconciliation does not bind the exact receipt and turn request",
            )
        if reconciliation.state is ContinuationReconciliationState.NONE:
            if (
                not isinstance(proof, ContinuationDeliveryAbsenceProof)
                or reconciliation.proof_digest != canonical_sha256(proof)
                or not verify_identity(proof)
                or proof.continuation_id != current.continuation_id
                or proof.receipt_id != receipt.receipt_id
                or proof.destination != current.destination
                or proof.turn_request_id != current.turn_request_id
            ):
                raise HarnessViolation(
                    FailureCode.CONVERGENCE_NOT_PROVEN,
                    "no-delivery reconciliation requires exact authoritative absence proof",
                )
            prepared = replace(current, state=ContinuationState.PREPARED)
            self._continuations[current.continuation_id] = prepared
            return prepared, None
        if not isinstance(
            proof, ConvergenceProof
        ) or reconciliation.proof_digest != canonical_sha256(proof):
            raise HarnessViolation(
                FailureCode.CONVERGENCE_NOT_PROVEN,
                "committed reconciliation requires its exact convergence proof",
            )
        acknowledged, acknowledgement = self._acknowledge_delivery(
            current,
            receipt,
            proof,
            expected_state=ContinuationState.DELIVERY_UNSETTLED,
        )
        return acknowledged, acknowledgement

    @_atomic
    def verify_mission(
        self, ack: Acknowledgement, readback: MissionSnapshotReadback
    ) -> MissionVerification:
        if self._acknowledgements.get(ack.acknowledgement_id) != ack:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY, "acknowledgement is not authoritative"
            )
        if not verify_identity(ack) or not verify_identity(readback):
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "acknowledgement or parent snapshot identity was tampered",
            )
        packet = self._packets[ack.packet_id]
        mission = self._missions[packet.mission_id]
        if (
            mission.revision != packet.mission_revision
            or readback.mission_id != packet.mission_id
            or readback.mission_revision != packet.mission_revision
        ):
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "parent snapshot does not bind the current mission revision",
            )
        expected_keys = tuple(item.key for item in mission.predicates)
        observed_keys = tuple(item.predicate_key for item in readback.predicates)
        if observed_keys != expected_keys:
            raise HarnessViolation(
                FailureCode.STALE_IDENTITY,
                "parent snapshot must contain every mission predicate in exact order",
            )
        snapshot_key = (mission.mission_id, mission.revision)
        previous = self._mission_snapshots.get(snapshot_key)
        if previous is not None:
            if readback.parent_state_sequence < previous.parent_state_sequence:
                raise HarnessViolation(
                    FailureCode.STALE_IDENTITY,
                    "parent snapshot sequence is older than current mission truth",
                )
            if (
                readback.parent_state_sequence == previous.parent_state_sequence
                and previous != readback
            ):
                raise HarnessViolation(
                    FailureCode.REPLAY_IDENTITY_CONFLICT,
                    "parent_state_sequence was reused with different snapshot content",
                )
        current_readback = next(
            item
            for item in readback.predicates
            if item.predicate_key == packet.predicate_key
        )
        indeterminate_readback = next(
            (
                item
                for item in readback.predicates
                if item.truth is PredicateTruth.INDETERMINATE
            ),
            None,
        )
        next_readback = indeterminate_readback or next(
            (
                item
                for item in readback.predicates
                if item.truth is PredicateTruth.UNSATISFIED
            ),
            None,
        )
        if indeterminate_readback is not None:
            next_action = MissionNextAction.READBACK_RECHECK
        elif next_readback is None:
            next_action = MissionNextAction.MISSION_COMPLETE
        else:
            next_action = MissionNextAction.ROUTE_SELECTION
        payload = {
            "mission_id": mission.mission_id,
            "mission_revision": mission.revision,
            "parent_state_revision": readback.parent_state_revision,
            "parent_state_sequence": readback.parent_state_sequence,
            "predicate_key": packet.predicate_key,
            "predicate_truth": current_readback.truth,
            "next_predicate_key": None
            if next_readback is None
            else next_readback.predicate_key,
            "next_action": next_action,
            "acknowledgement_id": ack.acknowledgement_id,
            "readback_id": readback.readback_id,
            "readback_evidence_digest": readback.evidence_digest,
        }
        verification = MissionVerification(
            verification_id=_stable_id("verification", payload), **payload
        )
        existing = self._verifications.get(verification.verification_id)
        if existing is not None:
            return existing
        self._mission_snapshots[snapshot_key] = readback
        self._verifications[verification.verification_id] = verification
        return verification

    @_atomic
    def replay_acknowledged(self, continuation: Continuation) -> ReplayResult:
        current = self._continuations.get(continuation.continuation_id)
        if current != continuation:
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "replay changed continuation payload or target identity",
            )
        if continuation.state is not ContinuationState.ACKNOWLEDGED:
            raise HarnessViolation(
                FailureCode.DUPLICATE_OR_REPLAY,
                "only an acknowledged continuation has an empty replay",
            )
        receipt = self._receipts[continuation.receipt_id]
        if receipt.packet_id not in self._ack_by_packet:
            raise HarnessViolation(
                FailureCode.REPLAY_IDENTITY_CONFLICT,
                "acknowledged continuation is not bound to an acknowledgement",
            )
        return ReplayResult(packet_id=receipt.packet_id)

    @_atomic
    def snapshot(self) -> StoreSnapshot:
        return StoreSnapshot(
            mission_count=len(self._missions),
            active_lease_count=len(self._active_leases),
            execution_attempt_count=len(self._execution_started),
            effect_count=len(self._effect_owner),
            terminal_receipt_count=len(self._receipts),
            continuation_count=len(self._continuations),
            acknowledgement_count=len(self._acknowledgements),
            verification_count=len(self._verifications),
        )


class RecoveryGate:
    """Deterministic recovery policy over one authoritative store."""

    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    def admit(
        self,
        packet: TaskPacket,
        lease: Lease,
        blocker: BlockerReport,
        proposal: RecoveryProposal,
    ) -> RecoveryAdmission:
        return self.store.admit_recovery(packet, lease, blocker, proposal)


class CollaborationHarness:
    """Small orchestrator that keeps every graph edge explicit and testable."""

    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.store = store if store is not None else InMemoryStore()

    def plan(
        self,
        mission: Mission,
        *,
        task_context_binding: TaskContextBinding | None = None,
    ) -> TaskPacket:
        self.store.register_mission(mission)
        predicate = self.store.first_false(mission)
        if predicate is None:
            raise HarnessViolation(
                FailureCode.MISSION_COMPLETE,
                f"mission {mission.mission_id!r} is complete",
            )
        routes = sorted(
            (
                item
                for item in mission.routes
                if item.predicate_key == predicate.key
                and not self.store.route_is_disposed(
                    mission.mission_id, mission.revision, item.route_id
                )
            ),
            key=lambda item: (item.rank, item.route_id),
        )
        if not routes:
            raise HarnessViolation(
                FailureCode.NO_ROUTE,
                f"no route exists for first false predicate {predicate.key!r}",
            )
        route = routes[0]
        return TaskPacket(
            mission_id=mission.mission_id,
            mission_revision=mission.revision,
            mission_mode=mission.mode,
            predicate_key=predicate.key,
            route_id=route.route_id,
            executor_id=route.executor_id,
            scope=route.scope,
            scope_versions=self.store.scope_snapshot(route.scope),
            expected_delta=route.expected_delta,
            abandon_if=route.abandon_if,
            recovery_budget=route.recovery_budget,
            destination=mission.destination,
            task_context_binding=task_context_binding,
        )

    def dispose_route(self, disposition: RouteDisposition) -> RouteDisposition:
        """Persist a parent decision; abandon_if prose remains host-owned."""

        return self.store.record_route_disposition(disposition)

    def classify_superseded_mission(
        self, disposition: MissionSupersessionDisposition
    ) -> MissionSupersessionDisposition:
        """Classify old evidence without changing current mission truth."""

        return self.store.record_mission_supersession(disposition)

    def claim(self, packet: TaskPacket) -> Lease:
        return self.store.claim(packet)

    def record_step_attempt(
        self, packet: TaskPacket, lease: Lease, step: StepAttempt
    ) -> StepAttempt:
        return self.store.record_step_attempt(packet, lease, step)

    def reconcile_step_effect(
        self,
        packet: TaskPacket,
        lease: Lease,
        reconciliation: StepEffectReconciliation,
    ) -> StepEffectReconciliation:
        return self.store.reconcile_step_effect(packet, lease, reconciliation)

    def record_blocker(
        self, packet: TaskPacket, lease: Lease, blocker: BlockerReport
    ) -> BlockerReport:
        return self.store.record_blocker(packet, lease, blocker)

    def admit_recovery(
        self,
        packet: TaskPacket,
        lease: Lease,
        blocker: BlockerReport,
        proposal: RecoveryProposal,
    ) -> RecoveryAdmission:
        return RecoveryGate(self.store).admit(packet, lease, blocker, proposal)

    def execute(
        self, packet: TaskPacket, lease: Lease, executor: Executor
    ) -> TerminalReceipt:
        if executor.executor_id != packet.executor_id:
            return self.store.terminalize_pre_execution(
                packet,
                lease,
                FailureCode.STALE_IDENTITY,
                "selected executor does not match the bounded route",
            )
        try:
            self.store.begin_execution(packet, lease)
        except HarnessViolation as error:
            if error.code is FailureCode.STALE_IDENTITY:
                return self.store.terminalize_pre_execution(
                    packet, lease, error.code, error.detail
                )
            raise
        try:
            result = executor.execute(packet, lease)
        except ExecutorFailureSignal as signal:
            failure = signal.failure
            if (
                failure.packet_id != packet.packet_id
                or failure.lease_id != lease.lease_id
            ):
                code = FailureCode.STALE_IDENTITY
                detail_digest = canonical_sha256(
                    {
                        "kind": "structured_failure_identity_mismatch",
                        "source_detail_digest": failure.detail_digest,
                    }
                )
            else:
                code = failure.failure_code
                detail_digest = failure.detail_digest
            self.store.record_execution_failure(
                packet,
                lease,
                code,
                detail_digest,
                observed_effect_id=failure.observed_effect_id,
            )
            raise HarnessViolation(
                code,
                "executor reported a structured failure; reconciliation is required",
            ) from signal
        except HarnessViolation as error:
            code = (
                error.code
                if error.code in EXECUTOR_ORIGIN_FAILURE_CODES
                else FailureCode.INVALID_FAILURE_CODE
            )
            self.store.record_execution_failure(
                packet,
                lease,
                code,
                canonical_sha256(
                    {
                        "kind": "invalid_executor_failure_code",
                        "source_code": error.code,
                        "detail_digest": canonical_sha256(error.detail),
                    }
                ),
            )
            raise HarnessViolation(
                code,
                "executor failure code is rejected; reconciliation is required",
            ) from error
        except Exception as error:
            self.store.record_execution_failure(
                packet,
                lease,
                FailureCode.EXECUTOR_ERROR,
                canonical_sha256(
                    {
                        "kind": "executor_exception",
                        "error_type": type(error).__name__,
                        "message_digest": canonical_sha256(str(error)),
                    }
                ),
            )
            raise HarnessViolation(
                FailureCode.EXECUTOR_ERROR,
                "executor failed; explicit effect reconciliation is required",
            ) from error
        if type(result) is not ExecutionResult:
            self.store.record_execution_failure(
                packet,
                lease,
                FailureCode.EXECUTOR_ERROR,
                canonical_sha256(
                    {
                        "kind": "malformed_executor_result",
                        "result_type": type(result).__name__,
                    }
                ),
                failure_origin=FailureOrigin.HARNESS,
            )
            raise HarnessViolation(
                FailureCode.EXECUTOR_ERROR,
                "executor returned a malformed result; reconciliation is required",
            )
        return self.store.record_execution(packet, lease, result)

    def reconcile_effect(
        self,
        packet: TaskPacket,
        lease: Lease,
        reconciliation: EffectReconciliation,
    ) -> TerminalReceipt:
        return self.store.reconcile_effect(packet, lease, reconciliation)

    def reconcile_failure(
        self,
        packet: TaskPacket,
        lease: Lease,
        reconciliation: FailureReconciliation,
    ) -> TerminalReceipt:
        return self.store.reconcile_failure(packet, lease, reconciliation)

    def continue_and_ack(
        self, receipt: TerminalReceipt, transport: ContinuationTransport
    ) -> tuple[Continuation, ResumeProof, ConvergenceProof, Acknowledgement]:
        continuation = self.store.prepare_continuation(receipt)
        return self.continue_existing(continuation, receipt, transport)

    def continue_existing(
        self,
        continuation: Continuation,
        receipt: TerminalReceipt,
        transport: ContinuationTransport,
    ) -> tuple[Continuation, ResumeProof, ConvergenceProof, Acknowledgement]:
        """Submit only a prepared identity; unknown delivery requires reconciliation."""

        self.store.validate_prepared_continuation(continuation, receipt)
        resume_proof = transport.resume(continuation)
        prepared = self.store.validate_resume(continuation, resume_proof)
        started = self.store.begin_delivery(prepared, receipt)
        try:
            convergence_proof = transport.start_turn(started, resume_proof, receipt)
            acknowledged, acknowledgement = self.store.acknowledge_after_convergence(
                started, receipt, convergence_proof
            )
        except Exception as error:
            unsettled = self.store.mark_delivery_unsettled(started, receipt)
            detail_digest = canonical_sha256(
                {
                    "kind": "continuation_delivery_failure",
                    "error_type": type(error).__name__,
                    "message_digest": canonical_sha256(str(error)),
                    "turn_request_id": started.turn_request_id,
                }
            )
            raise ContinuationDeliveryFailure(unsettled, detail_digest) from error
        return acknowledged, resume_proof, convergence_proof, acknowledgement

    def reconcile_continuation(
        self,
        receipt: TerminalReceipt,
        reconciliation: ContinuationReconciliation,
        proof: ConvergenceProof | ContinuationDeliveryAbsenceProof | None = None,
    ) -> tuple[Continuation, Acknowledgement | None]:
        return self.store.reconcile_continuation(receipt, reconciliation, proof)

    def verify_mission(
        self, acknowledgement: Acknowledgement, readback: MissionSnapshotReadback
    ) -> MissionVerification:
        return self.store.verify_mission(acknowledgement, readback)

    def run(
        self,
        mission: Mission,
        executor: Executor,
        transport: ContinuationTransport,
        readback: MissionSnapshotReadback,
    ) -> CycleResult:
        packet = self.plan(mission)
        lease = self.claim(packet)
        receipt = self.execute(packet, lease, executor)
        continuation, resume_proof, convergence_proof, acknowledgement = (
            self.continue_and_ack(receipt, transport)
        )
        verification = self.verify_mission(acknowledgement, readback)
        return CycleResult(
            packet=packet,
            lease=lease,
            receipt=receipt,
            continuation=continuation,
            resume_proof=resume_proof,
            convergence_proof=convergence_proof,
            acknowledgement=acknowledgement,
            verification=verification,
        )
