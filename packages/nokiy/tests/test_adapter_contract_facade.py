# SPDX-License-Identifier: MIT
from __future__ import annotations

import inspect
import unittest

import codex_collaboration_harness.core as core
from codex_collaboration_harness import _adapter_contracts
from codex_collaboration_harness.adapters import nokiy


class AdapterContractFacadeTests(unittest.TestCase):
    def test_facade_preserves_core_class_enum_and_helper_identity(self) -> None:
        for name in _adapter_contracts.__all__:
            self.assertIs(
                getattr(_adapter_contracts, name),
                getattr(core, name),
                name,
            )

    def test_nokiy_uses_facade_without_changing_runtime_type_identity(self) -> None:
        source = inspect.getsource(nokiy)
        self.assertIn("from .._adapter_contracts import (", source)
        self.assertNotIn("from ..core import (", source)
        for name in (
            "Destination",
            "EffectState",
            "ExecutionFailure",
            "ExecutionResult",
            "FailureCode",
            "FailureOrigin",
            "Lease",
            "TaskPacket",
        ):
            self.assertIs(
                getattr(nokiy, name),
                getattr(core, name),
                name,
            )
            self.assertEqual(
                getattr(nokiy, name).__module__,
                "codex_collaboration_harness.core",
                name,
            )


if __name__ == "__main__":
    unittest.main()
