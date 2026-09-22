# SPDX-License-Identifier: MIT
"""Internal adapter-facing facade over the public collaboration core.

Provider adapters import their shared domain records and validation helpers from
this module instead of depending directly on the monolithic ``core`` module.
The records remain defined by ``core`` so public import paths, Enum identity,
content-addressed serialization, and persisted fixture names are unchanged.
"""

from .core import (
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

__all__ = [
    "Destination",
    "EffectState",
    "ExecutionFailure",
    "ExecutionResult",
    "ExecutorFailureSignal",
    "FailureCode",
    "FailureOrigin",
    "HarnessViolation",
    "Lease",
    "TaskPacket",
    "_require_bool",
    "_require_enum",
    "_require_executor_failure_code",
    "_require_int",
    "_require_optional_enum",
    "_require_scope_version_ints",
    "canonical_sha256",
]
