# SPDX-License-Identifier: MIT
from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import replace
from threading import Barrier

from codex_collaboration_harness import (
    BlockerClass,
    BlockerPhase,
    BlockerReport,
    CollaborationHarness,
    Continuation,
    ContinuationDeliveryAbsenceProof,
    ContinuationDeliveryFailure,
    ContinuationReconciliation,
    ContinuationReconciliationState,
    ContinuationState,
    ConvergenceProof,
    Destination,
    EffectClass,
    EffectReconciliation,
    EffectState,
    ExecutionFailure,
    ExecutionResult,
    ExitPredicate,
    FailureCode,
    FailureOrigin,
    FailureReconciliation,
    HarnessViolation,
    InMemoryStore,
    Mission,
    MissionNextAction,
    MissionSnapshotReadback,
    MissionSupersessionDisposition,
    MissionSupersessionState,
    PredicateReadback,
    PredicateTruth,
    RecoveryAction,
    RecoveryAdmissionState,
    RecoveryProposal,
    ResumeProof,
    RetrySafety,
    Route,
    RouteDisposition,
    RouteDispositionState,
    StepAttempt,
    StepEffectReconciliation,
    StoreSnapshot,
    TerminalStatus,
    canonical_sha256,
    verify_identity,
)

EXECUTOR_ID = "executor.synthetic"
DESTINATION = Destination("coordinator.public", "thread.synthetic")


def make_route(
    predicate_key: str = "result_materialized",
    *,
    route_id: str = "route.preferred",
    executor_id: str = EXECUTOR_ID,
    scope: tuple[str, ...] = ("artifact:synthetic-result",),
    rank: int = 1,
) -> Route:
    return Route(
        route_id=route_id,
        predicate_key=predicate_key,
        executor_id=executor_id,
        scope=scope,
        expected_delta="a public result exists",
        abandon_if="the route needs a private dependency",
        rank=rank,
    )


def make_mission(
    *,
    mission_id: str = "mission.synthetic",
    revision: int = 1,
    scope: tuple[str, ...] = ("artifact:synthetic-result",),
    destination: Destination = DESTINATION,
) -> Mission:
    return Mission(
        mission_id=mission_id,
        revision=revision,
        mode="delivery",
        predicates=(
            ExitPredicate("inputs_ready", True),
            ExitPredicate("result_materialized", False),
        ),
        routes=(
            make_route(
                route_id="route.fallback",
                executor_id="executor.never-selected",
                scope=scope,
                rank=2,
            ),
            make_route(scope=scope),
        ),
        destination=destination,
    )


def make_readback(
    mission: Mission,
    *,
    satisfied: bool = True,
    predicate_key: str = "result_materialized",
    parent_state_revision: str = "parent-state.synthetic.1",
    parent_state_sequence: int = 1,
    truths: dict[str, PredicateTruth] | None = None,
) -> MissionSnapshotReadback:
    resolved_truths = {
        predicate.key: (
            PredicateTruth.SATISFIED
            if predicate.satisfied
            else PredicateTruth.UNSATISFIED
        )
        for predicate in mission.predicates
    }
    if truths is None:
        resolved_truths[predicate_key] = (
            PredicateTruth.SATISFIED if satisfied else PredicateTruth.UNSATISFIED
        )
    else:
        resolved_truths = truths
    predicates = tuple(
        PredicateReadback(
            predicate_key=predicate.key,
            truth=resolved_truths[predicate.key],
            evidence_digest=canonical_sha256(
                {
                    "predicate_key": predicate.key,
                    "truth": resolved_truths[predicate.key],
                    "owner": "parent",
                }
            ),
        )
        for predicate in mission.predicates
    )
    return MissionSnapshotReadback(
        mission_id=mission.mission_id,
        mission_revision=mission.revision,
        parent_state_revision=parent_state_revision,
        parent_state_sequence=parent_state_sequence,
        predicates=predicates,
        evidence_digest=canonical_sha256(
            {
                "mission_id": mission.mission_id,
                "mission_revision": mission.revision,
                "parent_state_revision": parent_state_revision,
                "parent_state_sequence": parent_state_sequence,
                "predicates": predicates,
                "owner": "parent",
            }
        ),
    )


class RecordingExecutor:
    def __init__(
        self,
        *,
        effect_id: str = "effect.synthetic.1",
        effect_state: EffectState = EffectState.SETTLED,
        predicate_satisfied: bool = True,
        executor_id: str = EXECUTOR_ID,
    ) -> None:
        self.executor_id = executor_id
        self.effect_id = effect_id
        self.effect_state = effect_state
        self.predicate_satisfied = predicate_satisfied
        self.calls = 0

    def execute(self, packet, lease) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            executor_id=self.executor_id,
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            effect_id=self.effect_id,
            effect_state=self.effect_state,
            output_digest=canonical_sha256(
                {"packet_id": packet.packet_id, "value": "synthetic-output"}
            ),
            predicate_satisfied=self.predicate_satisfied,
        )


class RaisingExecutor:
    executor_id = EXECUTOR_ID

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, packet, lease):
        self.calls += 1
        raise RuntimeError("synthetic provider failure")


class CommanderOutcomeExecutor:
    executor_id = EXECUTOR_ID

    def execute(self, packet, lease):
        raise HarnessViolation(
            FailureCode.MISSION_COMPLETE,
            "child attempted to emit a parent mission outcome",
        )


class MismatchedResultExecutor(RecordingExecutor):
    def execute(self, packet, lease) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            executor_id=self.executor_id,
            packet_id="packet_changed_identity",
            lease_id=lease.lease_id,
            effect_id=self.effect_id,
            effect_state=EffectState.SETTLED,
            output_digest=canonical_sha256({"value": "untrusted-result"}),
            predicate_satisfied=True,
        )


class BarrierStore(InMemoryStore):
    """Release two claim callers together before the store lock boundary."""

    def __init__(self, barrier: Barrier) -> None:
        super().__init__()
        self.barrier = barrier

    def claim(self, packet):
        self.barrier.wait(timeout=5)
        return super().claim(packet)


class DeterministicTransport:
    def __init__(
        self,
        *,
        resume_destination: Destination | None = None,
        proof_destination: Destination | None = None,
        completed: bool = True,
    ) -> None:
        self.resume_destination = resume_destination
        self.proof_destination = proof_destination
        self.completed = completed
        self.resume_calls = 0
        self.start_turn_calls = 0

    def resume(self, continuation: Continuation) -> ResumeProof:
        self.resume_calls += 1
        return ResumeProof(
            continuation_id=continuation.continuation_id,
            destination=self.resume_destination or continuation.destination,
            resume_token=canonical_sha256(
                {"step": "resume", "continuation_id": continuation.continuation_id}
            ),
        )

    def start_turn(self, continuation, resume_proof, receipt) -> ConvergenceProof:
        self.start_turn_calls += 1
        return ConvergenceProof(
            continuation_id=continuation.continuation_id,
            receipt_id=receipt.receipt_id,
            destination=self.proof_destination or continuation.destination,
            turn_request_id=continuation.turn_request_id,
            completed_turn_id=canonical_sha256(
                {
                    "step": "turn",
                    "resume_token": resume_proof.resume_token,
                    "receipt_id": receipt.receipt_id,
                }
            ),
            completed=self.completed,
        )


class CommitThenErrorTransport(DeterministicTransport):
    def __init__(self) -> None:
        super().__init__()
        self.committed_request_ids: list[str] = []

    def start_turn(self, continuation, resume_proof, receipt):
        self.start_turn_calls += 1
        self.committed_request_ids.append(continuation.turn_request_id)
        raise TimeoutError("synthetic response lost after destination commit")


class HarnessTestCase(unittest.TestCase):
    def assert_code(self, expected: FailureCode, operation) -> HarnessViolation:
        with self.assertRaises(HarnessViolation) as caught:
            operation()
        self.assertEqual(caught.exception.code, expected)
        return caught.exception

    def terminalize(self, harness: CollaborationHarness):
        packet = harness.plan(make_mission())
        lease = harness.claim(packet)
        receipt = harness.execute(packet, lease, RecordingExecutor())
        return packet, lease, receipt


class RuntimeTypeValidationTests(HarnessTestCase):
    def test_malformed_executor_result_records_typed_failure(self) -> None:
        class NoneExecutor:
            executor_id = EXECUTOR_ID

            def execute(self, packet, lease):
                return None

        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        lease = harness.claim(packet)

        self.assert_code(
            FailureCode.EXECUTOR_ERROR,
            lambda: harness.execute(packet, lease, NoneExecutor()),
        )

        failure = harness.store.failure_for_packet(packet.packet_id)
        self.assertIsNotNone(failure)
        self.assertIs(failure.failure_origin, FailureOrigin.HARNESS)
        self.assertEqual(harness.store.snapshot().active_lease_count, 1)
        self.assertEqual(harness.store.snapshot().terminal_receipt_count, 0)

    def test_string_false_readback_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            PredicateReadback(
                predicate_key="result_materialized",
                truth="false",
                evidence_digest=canonical_sha256({"owner": "parent"}),
            )

    def test_string_false_convergence_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ConvergenceProof(
                continuation_id="continuation.synthetic",
                receipt_id="receipt.synthetic",
                destination=DESTINATION,
                turn_request_id="turn-request.synthetic",
                completed_turn_id="turn.synthetic",
                completed="false",
            )

    def test_string_effect_state_cannot_strand_lease(self) -> None:
        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        lease = harness.claim(packet)

        with self.assertRaises(TypeError):
            ExecutionResult(
                executor_id=EXECUTOR_ID,
                packet_id=packet.packet_id,
                lease_id=lease.lease_id,
                effect_id="effect.synthetic.invalid",
                effect_state="settled",
                output_digest=canonical_sha256({"invalid": True}),
                predicate_satisfied=True,
            )

        self.assertEqual(harness.store.snapshot().execution_attempt_count, 0)
        receipt = harness.execute(packet, lease, RecordingExecutor())
        self.assertEqual(receipt.status, TerminalStatus.SUCCEEDED)
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)

    def test_raw_none_with_effect_id_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            FailureReconciliation(
                packet_id="packet.synthetic",
                lease_id="lease.synthetic",
                failure_code=FailureCode.EXECUTOR_ERROR,
                effect_state="none",
                effect_id="effect.forbidden",
                proof_digest=canonical_sha256({"effect": "none"}),
                output_digest=canonical_sha256({"terminal": "failed"}),
            )

    def test_enum_and_string_cannot_share_runtime_identity(self) -> None:
        canonical = ExecutionResult(
            executor_id=EXECUTOR_ID,
            packet_id="packet.synthetic",
            lease_id="lease.synthetic",
            effect_id="effect.synthetic",
            effect_state=EffectState.SETTLED,
            output_digest=canonical_sha256({"output": "synthetic"}),
            predicate_satisfied=True,
        )
        with self.assertRaises(TypeError):
            replace(canonical, effect_state="settled")

    def test_bool_cannot_enter_integer_fields(self) -> None:
        with self.assertRaises(TypeError):
            make_mission(revision=True)
        with self.assertRaises(TypeError):
            make_route(rank=False)

    def test_executor_cannot_emit_mission_complete(self) -> None:
        with self.assertRaises(HarnessViolation) as constructed:
            ExecutionFailure(
                failure_id="execution_failure.invalid",
                packet_id="packet.invalid",
                lease_id="lease.invalid",
                failure_code=FailureCode.MISSION_COMPLETE,
                detail_digest=canonical_sha256({"failure": "invalid"}),
                observed_effect_id=None,
            )
        self.assertEqual(constructed.exception.code, FailureCode.INVALID_FAILURE_CODE)

        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        lease = harness.claim(packet)

        self.assert_code(
            FailureCode.INVALID_FAILURE_CODE,
            lambda: harness.execute(packet, lease, CommanderOutcomeExecutor()),
        )
        failure = harness.store.failure_for_packet(packet.packet_id)
        self.assertIsInstance(failure, ExecutionFailure)
        self.assertEqual(failure.failure_code, FailureCode.INVALID_FAILURE_CODE)
        self.assertTrue(verify_identity(failure))
        with self.assertRaises(HarnessViolation) as caught:
            FailureReconciliation(
                packet_id=packet.packet_id,
                lease_id=lease.lease_id,
                failure_code=FailureCode.MISSION_COMPLETE,
                effect_state=EffectState.NONE,
                effect_id=None,
                proof_digest=canonical_sha256({"effect": "none"}),
                output_digest=canonical_sha256({"terminal": "invalid"}),
            )
        self.assertEqual(caught.exception.code, FailureCode.INVALID_FAILURE_CODE)


class PersistedIdentityTests(HarnessTestCase):
    def test_content_addressed_records_round_trip_and_detect_tamper(self) -> None:
        harness = CollaborationHarness()
        mission = make_mission()
        readback = make_readback(mission)
        result = harness.run(
            mission,
            RecordingExecutor(),
            DeterministicTransport(),
            readback,
        )
        records_and_fields = (
            (result.packet, "route_id"),
            (result.lease, "packet_id"),
            (result.receipt, "output_digest"),
            (result.continuation, "turn_request_id"),
            (readback, "evidence_digest"),
            (result.convergence_proof, "completed_turn_id"),
            (result.acknowledgement, "proof_id"),
            (result.verification, "readback_evidence_digest"),
        )

        for record, field_name in records_and_fields:
            with self.subTest(record=type(record).__name__):
                self.assertTrue(verify_identity(record))
                tampered = copy(record)
                object.__setattr__(tampered, field_name, "tampered")
                self.assertFalse(verify_identity(tampered))

    def test_verification_id_recomputes_from_persisted_record(self) -> None:
        mission = make_mission()
        readback = make_readback(mission)
        result = CollaborationHarness().run(
            mission,
            RecordingExecutor(),
            DeterministicTransport(),
            readback,
        )

        self.assertEqual(result.verification.readback_id, readback.readback_id)
        self.assertEqual(
            result.verification.readback_evidence_digest,
            readback.evidence_digest,
        )
        self.assertEqual(
            result.acknowledgement.proof_id,
            result.convergence_proof.proof_id,
        )
        self.assertTrue(verify_identity(result.verification))


class RouteAndSupersessionTests(HarnessTestCase):
    def test_disposed_route_falls_back_and_cannot_repeat(self) -> None:
        mission = Mission(
            mission_id="mission.route-disposition",
            revision=1,
            mode="delivery",
            predicates=(ExitPredicate("result_materialized", False),),
            routes=(
                make_route(route_id="route.preferred", rank=0),
                make_route(route_id="route.fallback", rank=1),
            ),
            destination=DESTINATION,
        )
        harness = CollaborationHarness()
        preferred = harness.plan(mission)
        disposition = RouteDisposition(
            mission_id=mission.mission_id,
            mission_revision=mission.revision,
            predicate_key=preferred.predicate_key,
            route_id=preferred.route_id,
            state=RouteDispositionState.EXHAUSTED,
            evidence_digest=canonical_sha256({"attempt": "preferred", "failed": True}),
        )

        first = harness.dispose_route(disposition)
        replay = harness.dispose_route(disposition)
        fallback = harness.plan(mission)

        self.assertEqual(first, replay)
        self.assertTrue(verify_identity(first))
        self.assertEqual(fallback.route_id, "route.fallback")
        self.assert_code(
            FailureCode.STALE_IDENTITY,
            lambda: harness.claim(preferred),
        )
        self.assert_code(
            FailureCode.REPLAY_IDENTITY_CONFLICT,
            lambda: harness.dispose_route(
                replace(disposition, state=RouteDispositionState.BLOCKED)
            ),
        )

    def _superseded_ack(self):
        harness = CollaborationHarness()
        old_mission = make_mission(mission_id="mission.supersession", revision=1)
        packet = harness.plan(old_mission)
        lease = harness.claim(packet)
        receipt = harness.execute(packet, lease, RecordingExecutor())
        _, _, _, acknowledgement = harness.continue_and_ack(
            receipt, DeterministicTransport()
        )
        new_mission = make_mission(mission_id=old_mission.mission_id, revision=2)
        harness.store.register_mission(new_mission)
        return harness, old_mission, new_mission, receipt, acknowledgement

    def test_stale_ack_can_be_classified_obsolete(self) -> None:
        harness, old_mission, new_mission, receipt, acknowledgement = (
            self._superseded_ack()
        )
        self.assert_code(
            FailureCode.STALE_IDENTITY,
            lambda: harness.verify_mission(acknowledgement, make_readback(old_mission)),
        )
        disposition = MissionSupersessionDisposition(
            mission_id=old_mission.mission_id,
            superseded_revision=old_mission.revision,
            superseding_revision=new_mission.revision,
            acknowledgement_id=acknowledgement.acknowledgement_id,
            receipt_id=receipt.receipt_id,
            state=MissionSupersessionState.OBSOLETE,
            evidence_digest=canonical_sha256({"classification": "obsolete"}),
        )

        recorded = harness.classify_superseded_mission(disposition)

        self.assertEqual(harness.classify_superseded_mission(disposition), recorded)
        self.assertTrue(verify_identity(recorded))
        self.assert_code(
            FailureCode.REPLAY_IDENTITY_CONFLICT,
            lambda: harness.classify_superseded_mission(
                replace(disposition, state=MissionSupersessionState.ADOPTED)
            ),
        )

    def test_adopted_old_evidence_does_not_satisfy_new_mission(self) -> None:
        harness, old_mission, new_mission, receipt, acknowledgement = (
            self._superseded_ack()
        )
        disposition = MissionSupersessionDisposition(
            mission_id=old_mission.mission_id,
            superseded_revision=old_mission.revision,
            superseding_revision=new_mission.revision,
            acknowledgement_id=acknowledgement.acknowledgement_id,
            receipt_id=receipt.receipt_id,
            state=MissionSupersessionState.ADOPTED,
            evidence_digest=canonical_sha256({"classification": "adopted-evidence"}),
        )

        harness.classify_superseded_mission(disposition)
        packet = harness.plan(new_mission)

        self.assertEqual(packet.mission_revision, new_mission.revision)
        self.assertEqual(packet.predicate_key, "result_materialized")
        self.assertTrue(verify_identity(disposition))


class HappyPathTests(HarnessTestCase):
    def test_complete_cycle(self) -> None:
        harness = CollaborationHarness()
        executor = RecordingExecutor()
        transport = DeterministicTransport()
        mission = make_mission()

        result = harness.run(mission, executor, transport, make_readback(mission))

        self.assertEqual(result.packet.predicate_key, "result_materialized")
        self.assertEqual(result.packet.route_id, "route.preferred")
        self.assertEqual(result.receipt.status, TerminalStatus.SUCCEEDED)
        self.assertEqual(result.receipt.effect_state, EffectState.SETTLED)
        self.assertEqual(result.continuation.state, ContinuationState.ACKNOWLEDGED)
        self.assertEqual(result.convergence_proof.destination, DESTINATION)
        self.assertTrue(result.verification.predicate_satisfied)
        self.assertEqual(
            result.verification.next_action, MissionNextAction.MISSION_COMPLETE
        )
        self.assertEqual(executor.calls, 1)
        self.assertEqual(transport.resume_calls, 1)
        self.assertEqual(transport.start_turn_calls, 1)
        self.assertEqual(
            harness.store.snapshot(),
            StoreSnapshot(1, 0, 1, 1, 1, 1, 1, 1),
        )

        second_mission = make_mission()
        second = CollaborationHarness().run(
            second_mission,
            RecordingExecutor(),
            DeterministicTransport(),
            make_readback(second_mission),
        )
        self.assertEqual(result.packet.packet_id, second.packet.packet_id)
        self.assertEqual(result.receipt.receipt_id, second.receipt.receipt_id)
        self.assertEqual(
            result.verification.verification_id, second.verification.verification_id
        )


class VerificationContractTests(HarnessTestCase):
    def test_first_false_predicate_wins_over_later_higher_rank_route(self) -> None:
        mission = Mission(
            "mission.order",
            1,
            "delivery",
            (
                ExitPredicate("ready", True),
                ExitPredicate("first_false", False),
                ExitPredicate("later_false", False),
            ),
            (
                make_route("later_false", route_id="route.later", rank=0),
                make_route("first_false", route_id="route.first", rank=9),
            ),
            DESTINATION,
        )
        packet = CollaborationHarness().plan(mission)
        self.assertEqual(packet.predicate_key, "first_false")
        self.assertEqual(packet.route_id, "route.first")

    def test_overlapping_active_lease_is_rejected(self) -> None:
        harness = CollaborationHarness()
        first = harness.plan(make_mission(mission_id="mission.first"))
        second = harness.plan(make_mission(mission_id="mission.second"))
        harness.claim(first)
        self.assert_code(FailureCode.LEASE_CONFLICT, lambda: harness.claim(second))

    def test_pre_execution_rejection_is_typed_terminal_and_releases_lease(self) -> None:
        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        lease = harness.claim(packet)
        wrong = RecordingExecutor(executor_id="executor.wrong")

        receipt = harness.execute(packet, lease, wrong)

        self.assertEqual(wrong.calls, 0)
        self.assertEqual(receipt.status, TerminalStatus.BLOCKED)
        self.assertEqual(receipt.failure_code, FailureCode.STALE_IDENTITY)
        self.assertEqual(receipt.effect_state, EffectState.NONE)
        self.assertFalse(receipt.predicate_satisfied)
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)
        self.assertEqual(harness.store.snapshot().terminal_receipt_count, 1)

    def test_unsettled_effect_never_reexecutes_and_reconciles_same_attempt(
        self,
    ) -> None:
        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        lease = harness.claim(packet)
        executor = RecordingExecutor(effect_state=EffectState.UNSETTLED)

        self.assert_code(
            FailureCode.UNSETTLED_EFFECT,
            lambda: harness.execute(packet, lease, executor),
        )
        self.assert_code(
            FailureCode.DUPLICATE_OR_REPLAY,
            lambda: harness.execute(packet, lease, executor),
        )
        self.assertEqual(executor.calls, 1)
        self.assertEqual(harness.store.snapshot().active_lease_count, 1)
        self.assertEqual(harness.store.snapshot().terminal_receipt_count, 0)

        receipt = harness.reconcile_effect(
            packet,
            lease,
            EffectReconciliation(
                packet.packet_id,
                lease.lease_id,
                executor.effect_id,
                EffectState.SETTLED,
                canonical_sha256({"effect_id": executor.effect_id, "settled": True}),
            ),
        )
        self.assertEqual(receipt.status, TerminalStatus.SUCCEEDED)
        self.assertEqual(receipt.effect_id, executor.effect_id)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(harness.store.snapshot().effect_count, 1)
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)
        self.assert_code(
            FailureCode.DUPLICATE_OR_REPLAY,
            lambda: harness.reconcile_effect(
                packet,
                lease,
                EffectReconciliation(
                    packet.packet_id,
                    lease.lease_id,
                    executor.effect_id,
                    EffectState.SETTLED,
                    "proof",
                ),
            ),
        )

    def test_callback_target_mismatch_has_no_ack_and_no_task_lease(self) -> None:
        harness = CollaborationHarness()
        _, _, receipt = self.terminalize(harness)
        wrong = Destination("coordinator.public", "thread.other")

        self.assert_code(
            FailureCode.CALLBACK_DELIVERY_UNSETTLED,
            lambda: harness.continue_and_ack(
                receipt, DeterministicTransport(proof_destination=wrong)
            ),
        )
        snapshot = harness.store.snapshot()
        self.assertEqual(snapshot.active_lease_count, 0)
        self.assertEqual(snapshot.continuation_count, 1)
        self.assertEqual(snapshot.acknowledgement_count, 0)
        current = harness.store.continuation_for_receipt(receipt.receipt_id)
        self.assertEqual(current.state, ContinuationState.DELIVERY_UNSETTLED)

    def test_callback_retry_reuses_same_prepared_continuation(self) -> None:
        harness = CollaborationHarness()
        _, _, receipt = self.terminalize(harness)
        wrong = Destination("coordinator.public", "thread.other")
        self.assert_code(
            FailureCode.CALLBACK_TARGET_MISMATCH,
            lambda: harness.continue_and_ack(
                receipt, DeterministicTransport(resume_destination=wrong)
            ),
        )
        prepared = harness.store.continuation_for_receipt(receipt.receipt_id)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.state, ContinuationState.PREPARED)

        acknowledged, _, _, _ = harness.continue_existing(
            prepared, receipt, DeterministicTransport()
        )
        self.assertEqual(acknowledged.continuation_id, prepared.continuation_id)
        self.assertEqual(acknowledged.state, ContinuationState.ACKNOWLEDGED)
        self.assertEqual(harness.store.snapshot().continuation_count, 1)

    def test_callback_commit_then_transport_error_blocks_blind_retry(self) -> None:
        harness = CollaborationHarness()
        _, _, receipt = self.terminalize(harness)
        transport = CommitThenErrorTransport()

        with self.assertRaises(ContinuationDeliveryFailure) as caught:
            harness.continue_and_ack(receipt, transport)

        unsettled = harness.store.continuation_for_receipt(receipt.receipt_id)
        self.assertEqual(unsettled.state, ContinuationState.DELIVERY_UNSETTLED)
        self.assertEqual(caught.exception.continuation, unsettled)
        self.assertEqual(caught.exception.code, FailureCode.CALLBACK_DELIVERY_UNSETTLED)
        self.assertEqual(transport.committed_request_ids, [unsettled.turn_request_id])
        self.assert_code(
            FailureCode.CALLBACK_DELIVERY_UNSETTLED,
            lambda: harness.continue_and_ack(receipt, transport),
        )
        self.assertEqual(transport.start_turn_calls, 1)
        self.assertEqual(harness.store.snapshot().acknowledgement_count, 0)

    def test_none_reconciliation_permits_exactly_one_retry(self) -> None:
        harness = CollaborationHarness()
        _, _, receipt = self.terminalize(harness)
        first_transport = CommitThenErrorTransport()
        with self.assertRaises(ContinuationDeliveryFailure):
            harness.continue_and_ack(receipt, first_transport)
        unsettled = harness.store.continuation_for_receipt(receipt.receipt_id)
        self.assert_code(
            FailureCode.CONVERGENCE_NOT_PROVEN,
            lambda: harness.reconcile_continuation(
                receipt,
                ContinuationReconciliation(
                    continuation_id=unsettled.continuation_id,
                    receipt_id=receipt.receipt_id,
                    turn_request_id=unsettled.turn_request_id,
                    state=ContinuationReconciliationState.NONE,
                    proof_digest="not-an-authoritative-proof",
                ),
            ),
        )
        self.assertEqual(
            harness.store.continuation_for_receipt(receipt.receipt_id).state,
            ContinuationState.DELIVERY_UNSETTLED,
        )
        absence_proof = ContinuationDeliveryAbsenceProof(
            continuation_id=unsettled.continuation_id,
            receipt_id=receipt.receipt_id,
            destination=unsettled.destination,
            turn_request_id=unsettled.turn_request_id,
            authority_source="destination.turn-index",
            evidence_digest=canonical_sha256(
                {"turn_request_id": unsettled.turn_request_id, "present": False}
            ),
            delivery_absent=True,
        )
        reconciliation = ContinuationReconciliation(
            continuation_id=unsettled.continuation_id,
            receipt_id=receipt.receipt_id,
            turn_request_id=unsettled.turn_request_id,
            state=ContinuationReconciliationState.NONE,
            proof_digest=canonical_sha256(absence_proof),
        )

        prepared, acknowledgement = harness.reconcile_continuation(
            receipt,
            reconciliation,
            absence_proof,
        )

        self.assertIsNone(acknowledgement)
        self.assertEqual(prepared.state, ContinuationState.PREPARED)
        self.assertEqual(prepared.continuation_id, unsettled.continuation_id)
        self.assertEqual(prepared.turn_request_id, unsettled.turn_request_id)
        retry_transport = DeterministicTransport()
        acknowledged, _, proof, _ = harness.continue_existing(
            prepared, receipt, retry_transport
        )
        self.assertEqual(acknowledged.state, ContinuationState.ACKNOWLEDGED)
        self.assertEqual(proof.turn_request_id, unsettled.turn_request_id)
        self.assertEqual(first_transport.start_turn_calls, 1)
        self.assertEqual(retry_transport.start_turn_calls, 1)
        self.assert_code(
            FailureCode.REPLAY_IDENTITY_CONFLICT,
            lambda: harness.continue_existing(
                prepared, receipt, DeterministicTransport()
            ),
        )

    def test_committed_reconciliation_acks_without_second_send(self) -> None:
        harness = CollaborationHarness()
        _, _, receipt = self.terminalize(harness)
        transport = CommitThenErrorTransport()
        with self.assertRaises(ContinuationDeliveryFailure):
            harness.continue_and_ack(receipt, transport)
        unsettled = harness.store.continuation_for_receipt(receipt.receipt_id)
        proof = ConvergenceProof(
            continuation_id=unsettled.continuation_id,
            receipt_id=receipt.receipt_id,
            destination=unsettled.destination,
            turn_request_id=unsettled.turn_request_id,
            completed_turn_id="turn.synthetic.reconciled",
            completed=True,
        )

        acknowledged, acknowledgement = harness.reconcile_continuation(
            receipt,
            ContinuationReconciliation(
                continuation_id=unsettled.continuation_id,
                receipt_id=receipt.receipt_id,
                turn_request_id=unsettled.turn_request_id,
                state=ContinuationReconciliationState.COMMITTED,
                proof_digest=canonical_sha256(proof),
            ),
            proof,
        )

        self.assertEqual(acknowledged.state, ContinuationState.ACKNOWLEDGED)
        self.assertIsNotNone(acknowledgement)
        self.assertEqual(transport.start_turn_calls, 1)
        self.assertEqual(harness.store.snapshot().acknowledgement_count, 1)

    def test_continuation_reconciliation_identity_mismatch_fails_closed(
        self,
    ) -> None:
        harness = CollaborationHarness()
        _, _, receipt = self.terminalize(harness)
        transport = CommitThenErrorTransport()
        with self.assertRaises(ContinuationDeliveryFailure):
            harness.continue_and_ack(receipt, transport)
        unsettled = harness.store.continuation_for_receipt(receipt.receipt_id)
        wrong_destination_proof = ConvergenceProof(
            continuation_id=unsettled.continuation_id,
            receipt_id=receipt.receipt_id,
            destination=Destination("coordinator.public", "thread.wrong"),
            turn_request_id=unsettled.turn_request_id,
            completed_turn_id="turn.synthetic.wrong-destination",
            completed=True,
        )
        reconciliation = ContinuationReconciliation(
            continuation_id=unsettled.continuation_id,
            receipt_id=receipt.receipt_id,
            turn_request_id=unsettled.turn_request_id,
            state=ContinuationReconciliationState.COMMITTED,
            proof_digest=canonical_sha256(wrong_destination_proof),
        )

        self.assert_code(
            FailureCode.CALLBACK_TARGET_MISMATCH,
            lambda: harness.reconcile_continuation(
                receipt, reconciliation, wrong_destination_proof
            ),
        )
        wrong_request_proof = replace(
            wrong_destination_proof,
            destination=unsettled.destination,
            turn_request_id="turn_request.changed",
        )
        self.assert_code(
            FailureCode.CALLBACK_TARGET_MISMATCH,
            lambda: harness.reconcile_continuation(
                receipt,
                replace(
                    reconciliation,
                    proof_digest=canonical_sha256(wrong_request_proof),
                ),
                wrong_request_proof,
            ),
        )
        self.assert_code(
            FailureCode.STALE_IDENTITY,
            lambda: harness.reconcile_continuation(
                receipt,
                replace(reconciliation, turn_request_id="turn_request.changed"),
                wrong_destination_proof,
            ),
        )
        current = harness.store.continuation_for_receipt(receipt.receipt_id)
        self.assertEqual(current.state, ContinuationState.DELIVERY_UNSETTLED)
        self.assertEqual(transport.start_turn_calls, 1)

    def test_identical_acknowledged_replay_is_empty(self) -> None:
        harness = CollaborationHarness()
        mission = make_mission()
        result = harness.run(
            mission,
            RecordingExecutor(),
            DeterministicTransport(),
            make_readback(mission),
        )
        before = harness.store.snapshot()
        replay = harness.store.replay_acknowledged(result.continuation)
        self.assertEqual(
            (replay.effects, replay.continuations, replay.acknowledgements),
            ((), (), ()),
        )
        self.assertEqual(harness.store.snapshot(), before)

    def test_changed_replay_identity_is_conflict(self) -> None:
        harness = CollaborationHarness()
        mission = make_mission()
        result = harness.run(
            mission,
            RecordingExecutor(),
            DeterministicTransport(),
            make_readback(mission),
        )
        changed = replace(
            result.continuation,
            destination=Destination("coordinator.public", "thread.changed"),
        )
        self.assert_code(
            FailureCode.REPLAY_IDENTITY_CONFLICT,
            lambda: harness.store.replay_acknowledged(changed),
        )

    def test_acknowledgement_count_is_exactly_one(self) -> None:
        harness = CollaborationHarness()
        mission = make_mission()
        result = harness.run(
            mission,
            RecordingExecutor(),
            DeterministicTransport(),
            make_readback(mission),
        )
        ack = result.acknowledgement
        self.assertEqual(harness.store.snapshot().acknowledgement_count, 1)
        self.assertEqual(
            len({ack.callback_ack_id, ack.receipt_ack_id, ack.continuation_ack_id}), 3
        )
        harness.store.replay_acknowledged(result.continuation)
        self.assertEqual(harness.store.snapshot().acknowledgement_count, 1)

    def test_terminal_outcome_returns_to_parent_mission_verification(self) -> None:
        mission = Mission(
            "mission.return",
            1,
            "delivery",
            (
                ExitPredicate("ready", True),
                ExitPredicate("current", False),
                ExitPredicate("next", False),
            ),
            (make_route("current", route_id="route.current"),),
            DESTINATION,
        )
        result = CollaborationHarness().run(
            mission,
            RecordingExecutor(),
            DeterministicTransport(),
            make_readback(mission, predicate_key="current"),
        )
        self.assertEqual(result.verification.predicate_key, "current")
        self.assertEqual(result.verification.next_predicate_key, "next")
        self.assertEqual(
            result.verification.next_action, MissionNextAction.ROUTE_SELECTION
        )


class ParentReadbackAndFailureRecoveryTests(HarnessTestCase):
    def test_false_parent_readback_cannot_complete_worker_claim(self) -> None:
        mission = make_mission()
        result = CollaborationHarness().run(
            mission,
            RecordingExecutor(predicate_satisfied=True),
            DeterministicTransport(),
            make_readback(mission, satisfied=False),
        )
        self.assertTrue(result.receipt.predicate_satisfied)
        self.assertFalse(result.verification.predicate_satisfied)
        self.assertEqual(
            result.verification.next_action, MissionNextAction.ROUTE_SELECTION
        )
        self.assertEqual(result.verification.next_predicate_key, "result_materialized")

    def test_true_parent_readback_completes_despite_false_worker_claim(self) -> None:
        mission = make_mission()
        result = CollaborationHarness().run(
            mission,
            RecordingExecutor(predicate_satisfied=False),
            DeterministicTransport(),
            make_readback(mission, satisfied=True),
        )
        self.assertFalse(result.receipt.predicate_satisfied)
        self.assertTrue(result.verification.predicate_satisfied)
        self.assertEqual(
            result.verification.next_action, MissionNextAction.MISSION_COMPLETE
        )

    def test_prior_true_predicate_regression_blocks_completion(self) -> None:
        mission = make_mission()
        result = CollaborationHarness().run(
            mission,
            RecordingExecutor(),
            DeterministicTransport(),
            make_readback(
                mission,
                truths={
                    "inputs_ready": PredicateTruth.UNSATISFIED,
                    "result_materialized": PredicateTruth.SATISFIED,
                },
            ),
        )

        self.assertTrue(result.verification.predicate_satisfied)
        self.assertEqual(
            result.verification.next_action, MissionNextAction.ROUTE_SELECTION
        )
        self.assertEqual(result.verification.next_predicate_key, "inputs_ready")

    def test_indeterminate_readback_can_recheck_without_execution(self) -> None:
        harness = CollaborationHarness()
        mission = make_mission()
        executor = RecordingExecutor()
        packet = harness.plan(mission)
        lease = harness.claim(packet)
        receipt = harness.execute(packet, lease, executor)
        _, _, _, acknowledgement = harness.continue_and_ack(
            receipt, DeterministicTransport()
        )

        indeterminate = harness.verify_mission(
            acknowledgement,
            make_readback(
                mission,
                parent_state_revision="parent-state.synthetic.indeterminate",
                parent_state_sequence=1,
                truths={
                    "inputs_ready": PredicateTruth.UNSATISFIED,
                    "result_materialized": PredicateTruth.INDETERMINATE,
                },
            ),
        )

        self.assertEqual(indeterminate.next_action, MissionNextAction.READBACK_RECHECK)
        self.assertEqual(indeterminate.next_predicate_key, "result_materialized")
        self.assertIs(indeterminate.predicate_truth, PredicateTruth.INDETERMINATE)
        self.assert_code(
            FailureCode.MISSION_READBACK_INDETERMINATE,
            lambda: harness.plan(mission),
        )

        complete = harness.verify_mission(
            acknowledgement,
            make_readback(
                mission,
                parent_state_revision="parent-state.synthetic.complete",
                parent_state_sequence=2,
                truths={
                    "inputs_ready": PredicateTruth.SATISFIED,
                    "result_materialized": PredicateTruth.SATISFIED,
                },
            ),
        )

        self.assertEqual(complete.next_action, MissionNextAction.MISSION_COMPLETE)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(harness.store.snapshot().execution_attempt_count, 1)
        self.assertEqual(harness.store.snapshot().verification_count, 2)

    def test_stale_parent_snapshot_cannot_overwrite_newer_truth(self) -> None:
        harness = CollaborationHarness()
        mission = make_mission()
        packet = harness.plan(mission)
        lease = harness.claim(packet)
        receipt = harness.execute(packet, lease, RecordingExecutor())
        _, _, _, acknowledgement = harness.continue_and_ack(
            receipt, DeterministicTransport()
        )
        current = harness.verify_mission(
            acknowledgement,
            make_readback(
                mission,
                satisfied=False,
                parent_state_revision="parent-state.synthetic.current",
                parent_state_sequence=2,
            ),
        )
        self.assertEqual(current.next_action, MissionNextAction.ROUTE_SELECTION)

        self.assert_code(
            FailureCode.STALE_IDENTITY,
            lambda: harness.verify_mission(
                acknowledgement,
                make_readback(
                    mission,
                    satisfied=True,
                    parent_state_revision="parent-state.synthetic.stale",
                    parent_state_sequence=1,
                ),
            ),
        )
        self.assertEqual(harness.store.first_false(mission).key, "result_materialized")

    def test_mismatched_parent_readback_is_rejected(self) -> None:
        harness = CollaborationHarness()
        _, _, receipt = self.terminalize(harness)
        _, _, _, acknowledgement = harness.continue_and_ack(
            receipt, DeterministicTransport()
        )
        wrong = replace(
            make_readback(make_mission()),
            mission_id="mission.other",
            evidence_digest=canonical_sha256({"owner": "parent", "wrong": True}),
        )
        self.assert_code(
            FailureCode.STALE_IDENTITY,
            lambda: harness.verify_mission(acknowledgement, wrong),
        )
        self.assertEqual(harness.store.snapshot().verification_count, 0)

    def test_executor_exception_requires_explicit_no_effect_reconciliation(
        self,
    ) -> None:
        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        lease = harness.claim(packet)
        executor = RaisingExecutor()

        self.assert_code(
            FailureCode.EXECUTOR_ERROR,
            lambda: harness.execute(packet, lease, executor),
        )
        failure = harness.store.failure_for_packet(packet.packet_id)
        self.assertIsNotNone(failure)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(harness.store.snapshot().active_lease_count, 1)
        self.assertEqual(harness.store.snapshot().terminal_receipt_count, 0)
        self.assert_code(
            FailureCode.DUPLICATE_OR_REPLAY,
            lambda: harness.execute(packet, lease, executor),
        )
        self.assertEqual(executor.calls, 1)

        receipt = harness.reconcile_failure(
            packet,
            lease,
            FailureReconciliation(
                packet_id=packet.packet_id,
                lease_id=lease.lease_id,
                failure_code=FailureCode.EXECUTOR_ERROR,
                effect_state=EffectState.NONE,
                effect_id=None,
                proof_digest=canonical_sha256({"effect": "none", "proved": True}),
                output_digest=canonical_sha256({"terminal": "executor_failed"}),
            ),
        )
        self.assertEqual(receipt.status, TerminalStatus.FAILED)
        self.assertEqual(receipt.effect_state, EffectState.NONE)
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)

    def test_mismatched_result_reconciles_observed_effect_without_rerun(self) -> None:
        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        lease = harness.claim(packet)
        executor = MismatchedResultExecutor(effect_id="effect.mismatch.1")

        self.assert_code(
            FailureCode.STALE_IDENTITY,
            lambda: harness.execute(packet, lease, executor),
        )
        failure = harness.store.failure_for_packet(packet.packet_id)
        self.assertEqual(failure.observed_effect_id, executor.effect_id)
        self.assertEqual(executor.calls, 1)
        self.assert_code(
            FailureCode.DUPLICATE_OR_REPLAY,
            lambda: harness.execute(packet, lease, executor),
        )
        self.assertEqual(executor.calls, 1)

        receipt = harness.reconcile_failure(
            packet,
            lease,
            FailureReconciliation(
                packet_id=packet.packet_id,
                lease_id=lease.lease_id,
                failure_code=FailureCode.STALE_IDENTITY,
                effect_state=EffectState.SETTLED,
                effect_id=executor.effect_id,
                proof_digest=canonical_sha256(
                    {"effect_id": executor.effect_id, "settled": True}
                ),
                output_digest=canonical_sha256({"terminal": "identity_mismatch"}),
            ),
        )
        self.assertEqual(receipt.effect_state, EffectState.SETTLED)
        self.assertEqual(receipt.effect_id, executor.effect_id)
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)
        self.assertEqual(harness.store.snapshot().effect_count, 1)

    def test_revision_drift_closes_exact_pre_execution_lease(self) -> None:
        harness = CollaborationHarness()
        packet = harness.plan(make_mission(revision=1))
        lease = harness.claim(packet)
        harness.store.register_mission(make_mission(revision=2))
        executor = RecordingExecutor()

        receipt = harness.execute(packet, lease, executor)

        self.assertEqual(executor.calls, 0)
        self.assertEqual(receipt.status, TerminalStatus.BLOCKED)
        self.assertEqual(receipt.failure_code, FailureCode.STALE_IDENTITY)
        self.assertEqual(receipt.effect_state, EffectState.NONE)
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)


class RecoveryEnvelopeTests(HarnessTestCase):
    def active_attempt(self):
        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        lease = harness.claim(packet)
        harness.store.begin_execution(packet, lease)
        return harness, packet, lease

    def make_step(
        self,
        packet,
        lease,
        *,
        effect_state: EffectState = EffectState.NONE,
        effect_id: str | None = None,
        effect_class: EffectClass = EffectClass.READ_ONLY,
    ) -> StepAttempt:
        return StepAttempt(
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            operation_digest=canonical_sha256({"operation": "inspect"}),
            tool_id="tool.local",
            precondition_digest=canonical_sha256({"precondition": "active"}),
            effect_class=effect_class,
            effect_id=effect_id,
            effect_state=effect_state,
            result_digest=canonical_sha256({"result": effect_state}),
        )

    def make_blocker(
        self,
        packet,
        lease,
        step,
        *,
        effect_state: EffectState | None = None,
        retry_safety: RetrySafety = RetrySafety.SAFE_LOCAL,
        state: str = "missing-local-input",
    ) -> BlockerReport:
        resolved_effect_state = (
            step.effect_state if effect_state is None else effect_state
        )
        return BlockerReport(
            mission_id=packet.mission_id,
            mission_revision=packet.mission_revision,
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            step_id=step.step_id,
            phase=BlockerPhase.EXECUTION,
            blocker_class=BlockerClass.DIAGNOSTIC,
            state_digest=canonical_sha256({"state": state}),
            evidence_refs=(step.step_id,),
            effect_state=resolved_effect_state,
            observed_effect_ids=() if step.effect_id is None else (step.effect_id,),
            missing_capabilities=(),
            violated_preconditions=("local input is absent",),
            retry_safety=retry_safety,
        )

    def make_proposal(
        self,
        packet,
        lease,
        blocker,
        *,
        action: RecoveryAction | None = None,
        required_scope: tuple[str, ...] | None = None,
        authority_delta: int = 0,
        verification_plan: tuple[str, ...] = ("focused unit test",),
        budget: int = 1,
        confidence: float = 0.5,
        predicate_key: str | None = None,
        expected_delta: str | None = None,
        destination: Destination | None = None,
    ) -> RecoveryProposal:
        resolved_action = action or RecoveryAction(
            action_kind="repair_local_input",
            scope=(packet.scope[0],),
            effect_class=EffectClass.REVERSIBLE_LOCAL,
            operation_digest=canonical_sha256({"repair": "local-input"}),
        )
        return RecoveryProposal(
            blocker_id=blocker.blocker_id,
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            predicate_key=predicate_key or packet.predicate_key,
            expected_delta=expected_delta or packet.expected_delta,
            destination=destination or packet.destination,
            action_graph=(resolved_action,),
            required_scope=required_scope
            if required_scope is not None
            else resolved_action.scope,
            authority_delta=authority_delta,
            verification_plan=verification_plan,
            budget=budget,
            confidence=confidence,
        )

    def test_bounded_local_recovery_is_admitted_without_terminalizing(self) -> None:
        harness, packet, lease = self.active_attempt()
        step = harness.record_step_attempt(packet, lease, self.make_step(packet, lease))
        blocker = harness.record_blocker(
            packet, lease, self.make_blocker(packet, lease, step)
        )
        action = RecoveryAction(
            action_kind="repair_local_input",
            scope=packet.scope,
            effect_class=EffectClass.REVERSIBLE_LOCAL,
            operation_digest=canonical_sha256({"repair": "bounded"}),
        )
        proposal = self.make_proposal(
            packet, lease, blocker, action=action, confidence=0.01
        )

        admission = harness.admit_recovery(packet, lease, blocker, proposal)

        self.assertIs(admission.state, RecoveryAdmissionState.ADMITTED)
        self.assertEqual(admission.reason_codes, ())
        self.assertTrue(verify_identity(step))
        self.assertTrue(verify_identity(blocker))
        self.assertTrue(verify_identity(action))
        self.assertTrue(verify_identity(proposal))
        self.assertTrue(verify_identity(admission))
        snapshot = harness.store.snapshot()
        self.assertEqual(snapshot.active_lease_count, 1)
        self.assertEqual(snapshot.terminal_receipt_count, 0)

    def test_recovery_step_requires_exact_admitted_action(self) -> None:
        harness, packet, lease = self.active_attempt()
        parent = harness.record_step_attempt(
            packet, lease, self.make_step(packet, lease)
        )
        blocker = harness.record_blocker(
            packet, lease, self.make_blocker(packet, lease, parent)
        )
        action = RecoveryAction(
            action_kind="repair_local_input",
            scope=packet.scope,
            effect_class=EffectClass.REVERSIBLE_LOCAL,
            operation_digest=canonical_sha256({"repair": "admitted"}),
        )
        proposal = self.make_proposal(packet, lease, blocker, action=action)
        admission = harness.admit_recovery(packet, lease, blocker, proposal)
        recovery = StepAttempt(
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            operation_digest=action.operation_digest,
            tool_id="tool.local",
            precondition_digest=canonical_sha256({"precondition": "admitted"}),
            effect_class=action.effect_class,
            effect_id="effect.recovery.admitted",
            effect_state=EffectState.SETTLED,
            result_digest=canonical_sha256({"result": "repaired"}),
            recovery_parent_step_id=parent.step_id,
            recovery_admission_id=admission.admission_id,
            recovery_action_id=action.action_id,
        )

        self.assertEqual(
            harness.record_step_attempt(packet, lease, recovery), recovery
        )
        unauthorized = replace(
            recovery,
            recovery_action_id="recovery_action.not-admitted",
        )
        self.assert_code(
            FailureCode.STALE_IDENTITY,
            lambda: harness.record_step_attempt(packet, lease, unauthorized),
        )

    def test_recovery_step_rejects_unadmitted_action(self) -> None:
        harness, packet, lease = self.active_attempt()
        parent = harness.record_step_attempt(
            packet, lease, self.make_step(packet, lease)
        )

        with self.assertRaises(ValueError):
            replace(
                parent,
                recovery_parent_step_id=parent.step_id,
                recovery_admission_id=None,
                recovery_action_id=None,
            )

    def test_scope_authority_and_protected_effect_expansion_escalate(self) -> None:
        harness, packet, lease = self.active_attempt()
        step = harness.record_step_attempt(packet, lease, self.make_step(packet, lease))
        blocker = harness.record_blocker(
            packet, lease, self.make_blocker(packet, lease, step)
        )
        cases = (
            (
                RecoveryAction(
                    "expanded_scope",
                    ("artifact:outside",),
                    EffectClass.REVERSIBLE_LOCAL,
                    canonical_sha256("expanded-scope"),
                ),
                ("artifact:outside",),
                0,
                "SCOPE_EXPANSION",
            ),
            (
                RecoveryAction(
                    "expanded_authority",
                    packet.scope,
                    EffectClass.REVERSIBLE_LOCAL,
                    canonical_sha256("expanded-authority"),
                ),
                packet.scope,
                1,
                "AUTHORITY_EXPANSION",
            ),
            (
                RecoveryAction(
                    "external_effect",
                    packet.scope,
                    EffectClass.EXTERNAL,
                    canonical_sha256("external-effect"),
                ),
                packet.scope,
                0,
                "NEW_PROTECTED_EFFECT",
            ),
        )
        for action, scope, authority_delta, reason in cases:
            with self.subTest(reason=reason):
                admission = harness.admit_recovery(
                    packet,
                    lease,
                    blocker,
                    self.make_proposal(
                        packet,
                        lease,
                        blocker,
                        action=action,
                        required_scope=scope,
                        authority_delta=authority_delta,
                    ),
                )
                self.assertIs(admission.state, RecoveryAdmissionState.ESCALATED)
                self.assertIn(reason, admission.reason_codes)
        self.assertEqual(harness.store.snapshot().active_lease_count, 1)
        self.assertEqual(harness.store.snapshot().terminal_receipt_count, 0)

    def test_policy_blocker_cannot_self_label_as_safe_local(self) -> None:
        harness, packet, lease = self.active_attempt()
        step = harness.record_step_attempt(packet, lease, self.make_step(packet, lease))
        blocker = harness.record_blocker(
            packet,
            lease,
            replace(
                self.make_blocker(
                    packet,
                    lease,
                    step,
                    retry_safety=RetrySafety.SAFE_LOCAL,
                ),
                blocker_class=BlockerClass.POLICY_AUTHORITY,
            ),
        )

        admission = harness.admit_recovery(
            packet, lease, blocker, self.make_proposal(packet, lease, blocker)
        )

        self.assertIs(admission.state, RecoveryAdmissionState.ESCALATED)
        self.assertIn("BLOCKER_REQUIRES_PARENT_AUTHORITY", admission.reason_codes)

    def test_recovery_budget_is_parent_bounded(self) -> None:
        harness, packet, lease = self.active_attempt()
        step = harness.record_step_attempt(packet, lease, self.make_step(packet, lease))
        blocker = harness.record_blocker(
            packet, lease, self.make_blocker(packet, lease, step)
        )

        admission = harness.admit_recovery(
            packet,
            lease,
            blocker,
            self.make_proposal(packet, lease, blocker, budget=2),
        )

        self.assertEqual(packet.recovery_budget, 1)
        self.assertIs(admission.state, RecoveryAdmissionState.ESCALATED)
        self.assertIn("RECOVERY_BUDGET_EXHAUSTED", admission.reason_codes)

    def test_unsettled_step_requires_exact_reconciliation_before_recovery(
        self,
    ) -> None:
        harness, packet, lease = self.active_attempt()
        step = harness.record_step_attempt(
            packet,
            lease,
            self.make_step(
                packet,
                lease,
                effect_state=EffectState.UNSETTLED,
                effect_id="effect.step.unknown",
                effect_class=EffectClass.REVERSIBLE_LOCAL,
            ),
        )
        blocker = harness.record_blocker(
            packet,
            lease,
            self.make_blocker(
                packet,
                lease,
                step,
                retry_safety=RetrySafety.RECONCILIATION_ONLY,
            ),
        )
        blocked = harness.admit_recovery(
            packet,
            lease,
            blocker,
            self.make_proposal(packet, lease, blocker),
        )
        self.assertIs(blocked.state, RecoveryAdmissionState.ESCALATED)
        self.assertIn("STEP_EFFECT_UNSETTLED", blocked.reason_codes)

        reconciliation = StepEffectReconciliation(
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            step_id=step.step_id,
            effect_id="effect.step.unknown",
            effect_state=EffectState.NONE,
            proof_digest=canonical_sha256({"effect": "none", "observed": True}),
        )
        self.assertEqual(
            harness.reconcile_step_effect(packet, lease, reconciliation),
            reconciliation,
        )
        self.assertEqual(
            harness.reconcile_step_effect(packet, lease, reconciliation),
            reconciliation,
        )
        self.assertTrue(verify_identity(reconciliation))
        mismatch = StepEffectReconciliation(
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            step_id=step.step_id,
            effect_id="effect.step.different",
            effect_state=EffectState.NONE,
            proof_digest=canonical_sha256({"effect": "different"}),
        )
        self.assert_code(
            FailureCode.STALE_IDENTITY,
            lambda: harness.reconcile_step_effect(packet, lease, mismatch),
        )

        reconciled_blocker = harness.record_blocker(
            packet,
            lease,
            self.make_blocker(
                packet,
                lease,
                step,
                effect_state=EffectState.NONE,
                retry_safety=RetrySafety.SAFE_LOCAL,
                state="effect-reconciled",
            ),
        )
        admitted = harness.admit_recovery(
            packet,
            lease,
            reconciled_blocker,
            self.make_proposal(packet, lease, reconciled_blocker),
        )
        self.assertIs(admitted.state, RecoveryAdmissionState.ADMITTED)

    def test_unsettled_step_blocks_terminalization_until_reconciled(self) -> None:
        harness, packet, lease = self.active_attempt()
        step = harness.record_step_attempt(
            packet,
            lease,
            self.make_step(
                packet,
                lease,
                effect_state=EffectState.UNSETTLED,
                effect_id="effect.step.before-terminal",
                effect_class=EffectClass.REVERSIBLE_LOCAL,
            ),
        )
        result = ExecutionResult(
            executor_id=packet.executor_id,
            packet_id=packet.packet_id,
            lease_id=lease.lease_id,
            effect_id="effect.final.after-step",
            effect_state=EffectState.SETTLED,
            output_digest=canonical_sha256({"terminal": "attempted"}),
            predicate_satisfied=True,
        )

        self.assert_code(
            FailureCode.UNSETTLED_EFFECT,
            lambda: harness.store.record_execution(packet, lease, result),
        )
        snapshot = harness.store.snapshot()
        self.assertEqual(snapshot.execution_attempt_count, 1)
        self.assertEqual(snapshot.effect_count, 0)
        self.assertEqual(snapshot.active_lease_count, 1)
        self.assertEqual(snapshot.terminal_receipt_count, 0)

        harness.reconcile_step_effect(
            packet,
            lease,
            StepEffectReconciliation(
                packet_id=packet.packet_id,
                lease_id=lease.lease_id,
                step_id=step.step_id,
                effect_id=step.effect_id,
                effect_state=EffectState.SETTLED,
                proof_digest=canonical_sha256({"effect": "settled"}),
            ),
        )
        receipt = harness.store.record_execution(packet, lease, result)
        self.assertIs(receipt.status, TerminalStatus.SUCCEEDED)
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)

    def test_duplicate_blocker_state_action_fingerprint_is_empty_replay(self) -> None:
        harness, packet, lease = self.active_attempt()
        step = harness.record_step_attempt(packet, lease, self.make_step(packet, lease))
        blocker = harness.record_blocker(
            packet, lease, self.make_blocker(packet, lease, step)
        )
        first = self.make_proposal(packet, lease, blocker, confidence=0.1)
        second = self.make_proposal(packet, lease, blocker, confidence=0.99)
        self.assertNotEqual(first.proposal_id, second.proposal_id)

        admitted = harness.admit_recovery(packet, lease, blocker, first)
        duplicate = harness.admit_recovery(packet, lease, blocker, second)

        self.assertIs(admitted.state, RecoveryAdmissionState.ADMITTED)
        self.assertIs(duplicate.state, RecoveryAdmissionState.DUPLICATE)
        self.assertEqual(duplicate.reason_codes, ("DUPLICATE_FINGERPRINT",))
        self.assertEqual(duplicate.recovery_fingerprint, admitted.recovery_fingerprint)

    def test_proposal_cannot_change_parent_bound_fields(self) -> None:
        harness, packet, lease = self.active_attempt()
        step = harness.record_step_attempt(packet, lease, self.make_step(packet, lease))
        blocker = harness.record_blocker(
            packet, lease, self.make_blocker(packet, lease, step)
        )
        cases = (
            (
                {"predicate_key": "different_predicate"},
                "PREDICATE_MISMATCH",
            ),
            (
                {"expected_delta": "model supplied a different delta"},
                "EXPECTED_DELTA_MISMATCH",
            ),
            (
                {"destination": Destination("other", "other")},
                "DESTINATION_MISMATCH",
            ),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                proposal = self.make_proposal(
                    packet,
                    lease,
                    blocker,
                    action=RecoveryAction(
                        action_kind=reason.lower(),
                        scope=packet.scope,
                        effect_class=EffectClass.REVERSIBLE_LOCAL,
                        operation_digest=canonical_sha256(reason),
                    ),
                    **overrides,
                )
                admission = harness.admit_recovery(packet, lease, blocker, proposal)
                self.assertIs(admission.state, RecoveryAdmissionState.ESCALATED)
                self.assertIn(reason, admission.reason_codes)
        self.assertEqual(harness.store.snapshot().terminal_receipt_count, 0)


class IdentityAndOwnershipTests(HarnessTestCase):
    def test_tampered_packet_identity_is_rejected_before_claim(self) -> None:
        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        tampered = copy(packet)
        object.__setattr__(tampered, "packet_id", "packet_tampered")

        self.assertFalse(verify_identity(tampered))
        self.assert_code(
            FailureCode.REPLAY_IDENTITY_CONFLICT,
            lambda: harness.claim(tampered),
        )
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)

    def test_cross_packet_duplicate_effect_is_persisted_and_reconcilable(self) -> None:
        harness = CollaborationHarness()
        effect_id = "effect.shared.synthetic"
        first = harness.plan(
            make_mission(
                mission_id="mission.effect.first",
                scope=("artifact:first",),
            )
        )
        first_lease = harness.claim(first)
        harness.execute(first, first_lease, RecordingExecutor(effect_id=effect_id))

        second = harness.plan(
            make_mission(
                mission_id="mission.effect.second",
                scope=("artifact:second",),
            )
        )
        second_lease = harness.claim(second)
        self.assert_code(
            FailureCode.DUPLICATE_OR_REPLAY,
            lambda: harness.execute(
                second, second_lease, RecordingExecutor(effect_id=effect_id)
            ),
        )
        failure = harness.store.failure_for_packet(second.packet_id)
        self.assertIsNotNone(failure)
        self.assertIs(failure.failure_origin, FailureOrigin.HARNESS)
        self.assertEqual(failure.failure_code, FailureCode.DUPLICATE_OR_REPLAY)
        self.assertEqual(harness.store.snapshot().active_lease_count, 1)

        receipt = harness.reconcile_failure(
            second,
            second_lease,
            FailureReconciliation(
                packet_id=second.packet_id,
                lease_id=second_lease.lease_id,
                failure_code=FailureCode.DUPLICATE_OR_REPLAY,
                effect_state=EffectState.NONE,
                effect_id=None,
                proof_digest=canonical_sha256(
                    {"new_effect": "none", "duplicate_observed": effect_id}
                ),
                output_digest=canonical_sha256({"terminal": "duplicate-effect"}),
            ),
        )
        self.assertIs(receipt.failure_origin, FailureOrigin.HARNESS)
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)
        self.assertEqual(harness.store.snapshot().effect_count, 1)

    def test_concurrent_overlapping_claim_has_one_winner(self) -> None:
        barrier = Barrier(3)
        harness = CollaborationHarness(BarrierStore(barrier))
        first = harness.plan(make_mission(mission_id="mission.concurrent.first"))
        second = harness.plan(make_mission(mission_id="mission.concurrent.second"))

        def attempt(packet):
            try:
                return ("lease", harness.claim(packet))
            except HarnessViolation as error:
                return ("error", error.code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, first), pool.submit(attempt, second)]
            barrier.wait(timeout=5)
            outcomes = [future.result(timeout=5) for future in futures]

        self.assertEqual(sum(kind == "lease" for kind, _ in outcomes), 1)
        self.assertEqual(
            [value for kind, value in outcomes if kind == "error"],
            [FailureCode.LEASE_CONFLICT],
        )
        self.assertEqual(harness.store.snapshot().active_lease_count, 1)

    def test_stale_mission_identity_is_rejected_before_claim(self) -> None:
        harness = CollaborationHarness()
        packet = harness.plan(make_mission(revision=1))
        harness.store.register_mission(make_mission(revision=2))
        self.assert_code(FailureCode.STALE_IDENTITY, lambda: harness.claim(packet))
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)

    def test_duplicate_dispatch_is_rejected(self) -> None:
        harness = CollaborationHarness()
        packet = harness.plan(make_mission())
        harness.claim(packet)
        self.assert_code(FailureCode.DUPLICATE_OR_REPLAY, lambda: harness.claim(packet))

    def test_stale_cas_snapshot_is_rejected_after_prior_terminalization(self) -> None:
        harness = CollaborationHarness()
        first = harness.plan(make_mission(mission_id="mission.cas.first"))
        stale = harness.plan(make_mission(mission_id="mission.cas.second"))
        lease = harness.claim(first)
        harness.execute(first, lease, RecordingExecutor())
        self.assertEqual(harness.store.snapshot().active_lease_count, 0)
        self.assert_code(FailureCode.CAS_MISMATCH, lambda: harness.claim(stale))

    def test_missing_convergence_proof_never_acknowledges(self) -> None:
        harness = CollaborationHarness()
        _, _, receipt = self.terminalize(harness)
        self.assert_code(
            FailureCode.CALLBACK_DELIVERY_UNSETTLED,
            lambda: harness.continue_and_ack(
                receipt, DeterministicTransport(completed=False)
            ),
        )
        self.assertEqual(harness.store.snapshot().acknowledgement_count, 0)


if __name__ == "__main__":
    unittest.main()
