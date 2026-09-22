# SPDX-License-Identifier: MIT
"""Public, stdlib-only contract for implementing a Nokiy client adapter.

This module defines an integration boundary only.  It contains no endpoint,
transport, credential, private schema, or Nokiy implementation code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TypeAlias, TypeVar, cast

from .._adapter_contracts import (
    Destination,
    EffectState,
    ExecutionFailure,
    ExecutionResult,
    ExecutorFailureSignal,
    FailureCode,
    FailureOrigin,
    HarnessViolation,
    Lease,
    TaskPacket,
    _require_bool,
    _require_enum,
    _require_executor_failure_code,
    _require_int,
    _require_optional_enum,
    _require_scope_version_ints,
    canonical_sha256,
)

NOKIY_PROTOCOL_VERSION = "tura-collaboration/v1"


class NokiyTerminalKind(str, Enum):
    """Public terminal-envelope categories understood by the adapter."""

    RESULT = "result"
    FAILURE = "failure"


WireEnumT = TypeVar("WireEnumT", bound=Enum)


def _decode_wire_enum(
    name: str, value: object, enum_type: type[WireEnumT]
) -> WireEnumT:
    if type(value) is not str:
        raise TypeError(f"{name} must be a JSON string")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"{name} is not a valid {enum_type.__name__}") from error


def _decode_wire_text(name: str, value: object) -> str:
    if type(value) is not str or not value.strip():
        raise TypeError(f"{name} must be a non-empty JSON string")
    return cast(str, value)


def _decode_optional_wire_text(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _decode_wire_text(name, value)


def _decode_wire_bool(name: str, value: object) -> bool:
    _require_bool(name, value)
    return cast(bool, value)


@dataclass(frozen=True, slots=True)
class NokiyDispatchRequest:
    """A flattened, bounded request compiled from a TaskPacket and Lease."""

    packet_id: str
    mission_id: str
    mission_revision: int
    mission_mode: str
    predicate_key: str
    route_id: str
    executor_id: str
    scope: tuple[str, ...]
    expected_scope_versions: tuple[tuple[str, int], ...]
    claimed_scope_versions: tuple[tuple[str, int], ...]
    expected_delta: str
    abandon_if: str
    recovery_budget: int
    destination: Destination
    lease_id: str
    protocol_version: str = NOKIY_PROTOCOL_VERSION
    request_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.protocol_version != NOKIY_PROTOCOL_VERSION:
            raise ValueError(f"protocol_version must be {NOKIY_PROTOCOL_VERSION!r}")
        _require_int("mission_revision", self.mission_revision)
        _require_int("recovery_budget", self.recovery_budget)
        if self.recovery_budget < 0:
            raise ValueError("recovery_budget must be non-negative")
        _require_scope_version_ints(
            "expected_scope_versions", self.expected_scope_versions
        )
        _require_scope_version_ints(
            "claimed_scope_versions", self.claimed_scope_versions
        )
        payload = {
            "protocol_version": self.protocol_version,
            "packet_id": self.packet_id,
            "mission_id": self.mission_id,
            "mission_revision": self.mission_revision,
            "mission_mode": self.mission_mode,
            "predicate_key": self.predicate_key,
            "route_id": self.route_id,
            "executor_id": self.executor_id,
            "scope": self.scope,
            "expected_scope_versions": self.expected_scope_versions,
            "claimed_scope_versions": self.claimed_scope_versions,
            "expected_delta": self.expected_delta,
            "abandon_if": self.abandon_if,
            "recovery_budget": self.recovery_budget,
            "destination": self.destination,
            "lease_id": self.lease_id,
        }
        object.__setattr__(
            self, "request_id", f"tura_request_{canonical_sha256(payload)}"
        )


@dataclass(frozen=True, slots=True)
class NokiyTerminalEnvelope:
    """Transport-neutral terminal data returned by a third-party Nokiy client."""

    request_id: str
    packet_id: str
    lease_id: str
    executor_id: str
    kind: NokiyTerminalKind
    effect_state: EffectState
    effect_id: str | None
    output_digest: str
    predicate_satisfied: bool
    protocol_version: str = NOKIY_PROTOCOL_VERSION
    failure_code: FailureCode | None = None
    failure_detail_digest: str | None = None

    def __post_init__(self) -> None:
        if self.protocol_version != NOKIY_PROTOCOL_VERSION:
            raise ValueError(f"protocol_version must be {NOKIY_PROTOCOL_VERSION!r}")
        for name in (
            "request_id",
            "packet_id",
            "lease_id",
            "executor_id",
            "output_digest",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        _require_enum("kind", self.kind, NokiyTerminalKind)
        _require_enum("effect_state", self.effect_state, EffectState)
        _require_bool("predicate_satisfied", self.predicate_satisfied)
        _require_optional_enum("failure_code", self.failure_code, FailureCode)
        if self.kind is NokiyTerminalKind.RESULT:
            if self.effect_state is EffectState.NONE or not self.effect_id:
                raise ValueError(
                    "result envelope requires a settled or unsettled effect"
                )
            if self.failure_code is not None or self.failure_detail_digest is not None:
                raise ValueError("result envelope cannot carry failure fields")
        else:
            if self.failure_code is None or not self.failure_detail_digest:
                raise ValueError("failure envelope requires typed failure evidence")
            _require_executor_failure_code("failure_code", self.failure_code)
            if self.effect_state is EffectState.NONE and self.effect_id is not None:
                raise ValueError("no-effect failure cannot carry effect_id")
            if self.effect_state is not EffectState.NONE and not self.effect_id:
                raise ValueError("effect-bearing failure requires effect_id")


_NOKIY_TERMINAL_WIRE_FIELDS = frozenset(
    {
        "protocol_version",
        "request_id",
        "packet_id",
        "lease_id",
        "executor_id",
        "kind",
        "effect_state",
        "effect_id",
        "output_digest",
        "predicate_satisfied",
        "failure_code",
        "failure_detail_digest",
    }
)


def decode_nokiy_terminal_envelope(
    payload: Mapping[str, object],
) -> NokiyTerminalEnvelope:
    """Normalize one exact JSON terminal envelope into canonical runtime types."""

    if not isinstance(payload, Mapping):
        raise TypeError("Nokiy terminal wire payload must be a mapping")
    keys = set(payload)
    missing = sorted(_NOKIY_TERMINAL_WIRE_FIELDS - keys)
    unknown = sorted(keys - _NOKIY_TERMINAL_WIRE_FIELDS)
    if missing or unknown:
        raise ValueError(
            f"Nokiy terminal wire fields differ: missing={missing}, unknown={unknown}"
        )
    raw_failure_code = payload["failure_code"]
    failure_code = (
        None
        if raw_failure_code is None
        else _decode_wire_enum("failure_code", raw_failure_code, FailureCode)
    )
    return NokiyTerminalEnvelope(
        protocol_version=_decode_wire_text(
            "protocol_version", payload["protocol_version"]
        ),
        request_id=_decode_wire_text("request_id", payload["request_id"]),
        packet_id=_decode_wire_text("packet_id", payload["packet_id"]),
        lease_id=_decode_wire_text("lease_id", payload["lease_id"]),
        executor_id=_decode_wire_text("executor_id", payload["executor_id"]),
        kind=_decode_wire_enum("kind", payload["kind"], NokiyTerminalKind),
        effect_state=_decode_wire_enum(
            "effect_state", payload["effect_state"], EffectState
        ),
        effect_id=_decode_optional_wire_text("effect_id", payload["effect_id"]),
        output_digest=_decode_wire_text("output_digest", payload["output_digest"]),
        predicate_satisfied=_decode_wire_bool(
            "predicate_satisfied", payload["predicate_satisfied"]
        ),
        failure_code=failure_code,
        failure_detail_digest=_decode_optional_wire_text(
            "failure_detail_digest", payload["failure_detail_digest"]
        ),
    )


@dataclass(frozen=True, slots=True)
class NokiyTypedRejection:
    """Fail-closed rejection before an envelope can become a core result."""

    code: FailureCode
    mismatched_fields: tuple[str, ...]
    expected_request_id: str
    observed_request_id: str
    detail_digest: str
    observed_effect_id: str | None = None

    def __post_init__(self) -> None:
        _require_enum("code", self.code, FailureCode)


class NokiyClient(Protocol):
    """Contract a third party implements with its chosen Nokiy transport."""

    def dispatch(
        self, request: NokiyDispatchRequest
    ) -> NokiyTerminalEnvelope | Mapping[str, object]:
        """Submit one bounded request and return one terminal envelope."""


NokiyDispatchOutcome: TypeAlias = ExecutionResult | ExecutionFailure | NokiyTypedRejection


class NokiyRejectedError(ExecutorFailureSignal):
    """Executor-facing wrapper for an explicit adapter rejection."""

    def __init__(
        self, rejection: NokiyTypedRejection, packet: TaskPacket, lease: Lease
    ) -> None:
        self.rejection = rejection
        payload = {
            "packet_id": packet.packet_id,
            "lease_id": lease.lease_id,
            "failure_code": rejection.code,
            "detail_digest": rejection.detail_digest,
            "observed_effect_id": rejection.observed_effect_id,
            "failure_origin": FailureOrigin.EXECUTOR,
        }
        super().__init__(
            ExecutionFailure(
                failure_id=f"execution_failure_{canonical_sha256(payload)}",
                **payload,
            )
        )


class NokiyExecutionFailureError(ExecutorFailureSignal):
    """Executor-facing wrapper retaining the mapped ExecutionFailure."""

    def __init__(self, failure: ExecutionFailure) -> None:
        super().__init__(failure)


def build_nokiy_dispatch_request(
    packet: TaskPacket, lease: Lease
) -> NokiyDispatchRequest:
    """Compile the exact public packet/lease identity into a Nokiy request."""

    if packet.task_context_binding is not None:
        raise ValueError(
            "task-context-bound packets require the Native Nokiy task bootstrap"
        )
    if lease.packet_id != packet.packet_id or lease.scope != packet.scope:
        raise ValueError("lease does not bind the supplied packet and scope")
    claimed = tuple((name, version + 1) for name, version in packet.scope_versions)
    if lease.scope_versions != claimed:
        raise ValueError("lease CAS versions do not follow the packet snapshot")
    return NokiyDispatchRequest(
        packet_id=packet.packet_id,
        mission_id=packet.mission_id,
        mission_revision=packet.mission_revision,
        mission_mode=packet.mission_mode,
        predicate_key=packet.predicate_key,
        route_id=packet.route_id,
        executor_id=packet.executor_id,
        scope=packet.scope,
        expected_scope_versions=packet.scope_versions,
        claimed_scope_versions=lease.scope_versions,
        expected_delta=packet.expected_delta,
        abandon_if=packet.abandon_if,
        recovery_budget=packet.recovery_budget,
        destination=packet.destination,
        lease_id=lease.lease_id,
    )


def encode_nokiy_dispatch_request(
    request: NokiyDispatchRequest,
) -> dict[str, object]:
    """Return the exact dependency-free JSON wire shape for one request."""

    return {
        "protocol_version": request.protocol_version,
        "request_id": request.request_id,
        "packet_id": request.packet_id,
        "mission_id": request.mission_id,
        "mission_revision": request.mission_revision,
        "mission_mode": request.mission_mode,
        "predicate_key": request.predicate_key,
        "route_id": request.route_id,
        "executor_id": request.executor_id,
        "scope": list(request.scope),
        "expected_scope_versions": [
            list(item) for item in request.expected_scope_versions
        ],
        "claimed_scope_versions": [
            list(item) for item in request.claimed_scope_versions
        ],
        "expected_delta": request.expected_delta,
        "abandon_if": request.abandon_if,
        "recovery_budget": request.recovery_budget,
        "destination": {
            "coordinator_id": request.destination.coordinator_id,
            "thread_id": request.destination.thread_id,
        },
        "lease_id": request.lease_id,
    }


def _execution_failure(
    request: NokiyDispatchRequest,
    code: FailureCode,
    detail_digest: str,
    observed_effect_id: str | None,
) -> ExecutionFailure:
    payload = {
        "packet_id": request.packet_id,
        "lease_id": request.lease_id,
        "failure_code": code,
        "detail_digest": detail_digest,
        "observed_effect_id": observed_effect_id,
        "failure_origin": FailureOrigin.EXECUTOR,
    }
    return ExecutionFailure(
        failure_id=f"execution_failure_{canonical_sha256(payload)}",
        packet_id=request.packet_id,
        lease_id=request.lease_id,
        failure_code=code,
        detail_digest=detail_digest,
        observed_effect_id=observed_effect_id,
        failure_origin=FailureOrigin.EXECUTOR,
    )


def _invalid_failure_code_rejection(
    request: NokiyDispatchRequest,
    error: HarnessViolation,
    *,
    observed_request_id: str = "unavailable",
    observed_effect_id: str | None = None,
) -> NokiyTypedRejection:
    return NokiyTypedRejection(
        code=FailureCode.INVALID_FAILURE_CODE,
        mismatched_fields=("failure_code",),
        expected_request_id=request.request_id,
        observed_request_id=observed_request_id,
        detail_digest=canonical_sha256(
            {
                "kind": "invalid_failure_code",
                "detail_digest": canonical_sha256(error.detail),
            }
        ),
        observed_effect_id=observed_effect_id,
    )


class NokiyAdapter:
    """Map the public Nokiy client contract onto collaboration-core records."""

    def __init__(self, client: NokiyClient, executor_id: str) -> None:
        if not executor_id.strip():
            raise ValueError("executor_id must be a non-empty string")
        self.client = client
        self.executor_id = executor_id

    def dispatch(self, packet: TaskPacket, lease: Lease) -> NokiyDispatchOutcome:
        try:
            request = build_nokiy_dispatch_request(packet, lease)
        except ValueError as error:
            detail_digest = canonical_sha256(
                {"kind": "request_rejected", "reason": str(error)}
            )
            return NokiyTypedRejection(
                code=FailureCode.STALE_IDENTITY,
                mismatched_fields=("packet_or_lease",),
                expected_request_id="unavailable",
                observed_request_id="unavailable",
                detail_digest=detail_digest,
            )
        if request.executor_id != self.executor_id:
            detail_digest = canonical_sha256(
                {
                    "kind": "executor_rejected",
                    "request_executor_id": request.executor_id,
                    "adapter_executor_id": self.executor_id,
                }
            )
            return NokiyTypedRejection(
                code=FailureCode.STALE_IDENTITY,
                mismatched_fields=("executor_id",),
                expected_request_id=request.request_id,
                observed_request_id=request.request_id,
                detail_digest=detail_digest,
            )
        try:
            wire_or_envelope = self.client.dispatch(request)
        except HarnessViolation as error:
            if error.code is FailureCode.INVALID_FAILURE_CODE:
                return _invalid_failure_code_rejection(request, error)
            detail_digest = canonical_sha256(
                {
                    "kind": "transport_exception",
                    "error_type": type(error).__name__,
                    "message_digest": canonical_sha256(str(error)),
                }
            )
            return _execution_failure(
                request, FailureCode.EXECUTOR_ERROR, detail_digest, None
            )
        except Exception as error:  # noqa: BLE001 - external transport boundary
            detail_digest = canonical_sha256(
                {
                    "kind": "transport_exception",
                    "error_type": type(error).__name__,
                    "message_digest": canonical_sha256(str(error)),
                }
            )
            return _execution_failure(
                request, FailureCode.EXECUTOR_ERROR, detail_digest, None
            )
        if isinstance(wire_or_envelope, Mapping):
            try:
                envelope = decode_nokiy_terminal_envelope(wire_or_envelope)
            except HarnessViolation as error:
                observed_request_id = wire_or_envelope.get("request_id")
                observed_effect_id = wire_or_envelope.get("effect_id")
                return _invalid_failure_code_rejection(
                    request,
                    error,
                    observed_request_id=(
                        observed_request_id
                        if type(observed_request_id) is str
                        else "unavailable"
                    ),
                    observed_effect_id=(
                        observed_effect_id if type(observed_effect_id) is str else None
                    ),
                )
            except (TypeError, ValueError) as error:
                detail_digest = canonical_sha256(
                    {
                        "kind": "invalid_envelope_wire",
                        "error_type": type(error).__name__,
                        "message_digest": canonical_sha256(str(error)),
                    }
                )
                observed_request_id = wire_or_envelope.get("request_id")
                observed_effect_id = wire_or_envelope.get("effect_id")
                return NokiyTypedRejection(
                    code=FailureCode.STALE_IDENTITY,
                    mismatched_fields=("envelope_wire",),
                    expected_request_id=request.request_id,
                    observed_request_id=(
                        observed_request_id
                        if type(observed_request_id) is str
                        else "unavailable"
                    ),
                    detail_digest=detail_digest,
                    observed_effect_id=(
                        observed_effect_id if type(observed_effect_id) is str else None
                    ),
                )
        else:
            envelope = wire_or_envelope
        if not isinstance(envelope, NokiyTerminalEnvelope):
            detail_digest = canonical_sha256(
                {"kind": "invalid_envelope_type", "type": type(envelope).__name__}
            )
            return NokiyTypedRejection(
                code=FailureCode.STALE_IDENTITY,
                mismatched_fields=("envelope_type",),
                expected_request_id=request.request_id,
                observed_request_id="unavailable",
                detail_digest=detail_digest,
            )
        expected = {
            "request_id": request.request_id,
            "packet_id": request.packet_id,
            "lease_id": request.lease_id,
            "executor_id": request.executor_id,
        }
        observed = {
            "request_id": envelope.request_id,
            "packet_id": envelope.packet_id,
            "lease_id": envelope.lease_id,
            "executor_id": envelope.executor_id,
        }
        mismatched = tuple(
            name for name in expected if expected[name] != observed[name]
        )
        if mismatched:
            detail_digest = canonical_sha256(
                {"kind": "terminal_identity_mismatch", "fields": mismatched}
            )
            return NokiyTypedRejection(
                code=FailureCode.STALE_IDENTITY,
                mismatched_fields=mismatched,
                expected_request_id=request.request_id,
                observed_request_id=envelope.request_id,
                detail_digest=detail_digest,
                observed_effect_id=envelope.effect_id,
            )
        if envelope.kind is NokiyTerminalKind.FAILURE:
            return _execution_failure(
                request,
                envelope.failure_code or FailureCode.EXECUTOR_ERROR,
                envelope.failure_detail_digest or envelope.output_digest,
                envelope.effect_id,
            )
        return ExecutionResult(
            executor_id=request.executor_id,
            packet_id=request.packet_id,
            lease_id=request.lease_id,
            effect_id=envelope.effect_id or "",
            effect_state=envelope.effect_state,
            output_digest=envelope.output_digest,
            predicate_satisfied=envelope.predicate_satisfied,
        )

    def execute(self, packet: TaskPacket, lease: Lease) -> ExecutionResult:
        """Implement the core Executor protocol while preserving typed outcomes."""

        outcome = self.dispatch(packet, lease)
        if isinstance(outcome, ExecutionResult):
            return outcome
        if isinstance(outcome, ExecutionFailure):
            raise NokiyExecutionFailureError(outcome)
        raise NokiyRejectedError(outcome, packet, lease)


__all__ = [
    "NOKIY_PROTOCOL_VERSION",
    "NokiyAdapter",
    "NokiyClient",
    "NokiyDispatchOutcome",
    "NokiyDispatchRequest",
    "NokiyExecutionFailureError",
    "NokiyRejectedError",
    "NokiyTerminalEnvelope",
    "NokiyTerminalKind",
    "NokiyTypedRejection",
    "build_nokiy_dispatch_request",
    "decode_nokiy_terminal_envelope",
    "encode_nokiy_dispatch_request",
]
