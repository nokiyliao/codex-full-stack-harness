# SPDX-License-Identifier: MIT
"""Run one deterministic, in-memory collaboration cycle with synthetic data."""

from __future__ import annotations

import json
from dataclasses import asdict

from codex_collaboration_harness import (
    CollaborationHarness,
    Continuation,
    ConvergenceProof,
    Destination,
    EffectState,
    ExecutionResult,
    ExitPredicate,
    Lease,
    Mission,
    MissionSnapshotReadback,
    PredicateReadback,
    PredicateTruth,
    ResumeProof,
    Route,
    TaskPacket,
    TerminalReceipt,
    canonical_sha256,
)

DESTINATION = Destination("coordinator.demo", "thread.demo")


class SyntheticExecutor:
    executor_id = "executor.demo"

    def execute(self, packet: TaskPacket, lease: Lease) -> ExecutionResult:
        return ExecutionResult(
            executor_id=self.executor_id,
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            effect_id="effect.demo.1",
            effect_state=EffectState.SETTLED,
            output_digest=canonical_sha256(
                {"kind": "synthetic-output", "packet_id": packet.packet_id}
            ),
            predicate_satisfied=True,
        )


class SyntheticTransport:
    def resume(self, continuation: Continuation) -> ResumeProof:
        return ResumeProof(
            continuation_id=continuation.continuation_id,
            destination=continuation.destination,
            resume_token=canonical_sha256(
                {"continuation_id": continuation.continuation_id, "step": "resume"}
            ),
        )

    def start_turn(
        self,
        continuation: Continuation,
        resume_proof: ResumeProof,
        receipt: TerminalReceipt,
    ) -> ConvergenceProof:
        return ConvergenceProof(
            continuation_id=continuation.continuation_id,
            receipt_id=receipt.receipt_id,
            destination=continuation.destination,
            turn_request_id=continuation.turn_request_id,
            completed_turn_id=canonical_sha256(
                {
                    "receipt_id": receipt.receipt_id,
                    "resume_token": resume_proof.resume_token,
                    "step": "completed-turn",
                }
            ),
            completed=True,
        )


def make_mission() -> Mission:
    return Mission(
        mission_id="mission.demo",
        revision=1,
        mode="delivery",
        predicates=(
            ExitPredicate("input_declared", True),
            ExitPredicate("artifact_verified", False),
        ),
        routes=(
            Route(
                route_id="route.demo",
                predicate_key="artifact_verified",
                executor_id="executor.demo",
                scope=("artifact:demo",),
                expected_delta="the synthetic artifact is verified",
                abandon_if="the route requires external state",
            ),
        ),
        destination=DESTINATION,
    )


def run_demo() -> dict[str, object]:
    harness = CollaborationHarness()
    mission = make_mission()
    predicates = tuple(
        PredicateReadback(
            predicate_key=predicate.key,
            truth=PredicateTruth.SATISFIED,
            evidence_digest=canonical_sha256(
                {
                    "source": "synthetic-parent-verifier",
                    "predicate_key": predicate.key,
                    "truth": PredicateTruth.SATISFIED,
                }
            ),
        )
        for predicate in mission.predicates
    )
    readback = MissionSnapshotReadback(
        mission_id=mission.mission_id,
        mission_revision=mission.revision,
        parent_state_revision="parent-state.demo.1",
        parent_state_sequence=1,
        predicates=predicates,
        evidence_digest=canonical_sha256(
            {
                "source": "synthetic-parent-verifier",
                "parent_state_revision": "parent-state.demo.1",
                "predicates": predicates,
            }
        ),
    )
    result = harness.run(
        mission,
        SyntheticExecutor(),
        SyntheticTransport(),
        readback,
    )
    return {
        "mission_id": result.verification.mission_id,
        "predicate_key": result.verification.predicate_key,
        "predicate_satisfied": result.verification.predicate_satisfied,
        "next_action": result.verification.next_action.value,
        "packet_id": result.packet.packet_id,
        "receipt_id": result.receipt.receipt_id,
        "continuation_id": result.continuation.continuation_id,
        "acknowledgement_id": result.acknowledgement.acknowledgement_id,
        "verification_id": result.verification.verification_id,
        "store_snapshot": asdict(harness.store.snapshot()),
    }


def main() -> int:
    first = run_demo()
    second = run_demo()
    if first != second:
        raise RuntimeError("synthetic demo produced non-deterministic output")
    print(json.dumps(first, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
