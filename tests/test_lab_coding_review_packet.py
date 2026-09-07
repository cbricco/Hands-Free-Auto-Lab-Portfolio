from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import unittest

from hands_free_auto_lab.lab_coding_candidate import (
    CANDIDATE_COMPONENT,
    CANDIDATE_SCHEMA_VERSION,
    LabCodingCandidate,
    LabCodingCandidateFile,
    _candidate_id,
    validate_lab_coding_candidate,
)
from hands_free_auto_lab.lab_coding_integration_contract import (
    build_lab_coding_integration_request,
    build_lab_coding_integration_result,
)
from hands_free_auto_lab.lab_coding_job_lifecycle import (
    build_lab_coding_job_lifecycle,
    transition_lab_coding_job_lifecycle,
)
from hands_free_auto_lab.lab_coding_job_queue import (
    build_lab_coding_job_queue,
    build_lab_coding_job_queue_item,
)
from hands_free_auto_lab.lab_coding_review_packet import (
    CODING_REVIEW_PACKET_COMPONENT,
    CODING_REVIEW_PACKET_SCHEMA_VERSION,
    LabCodingReviewPacket,
    LabCodingReviewPacketError,
    build_lab_coding_review_item,
    build_lab_coding_review_packet,
    lab_coding_review_packet_from_bytes,
    lab_coding_review_packet_from_wire,
    lab_coding_review_packet_to_bytes,
    lab_coding_review_packet_to_wire,
    validate_lab_coding_review_item,
    validate_lab_coding_review_packet,
)
from hands_free_auto_lab.lab_worker_change_policy import (
    LabWorkerChangeTarget,
    build_lab_worker_change_policy,
)


COMMIT_OID = "a" * 40
BEFORE_CONTENT = "before\n"
BEFORE_BYTES = BEFORE_CONTENT.encode("utf-8")
BEFORE_SHA = hashlib.sha256(BEFORE_BYTES).hexdigest()


def request_for_goal(goal: str):
    target = LabWorkerChangeTarget(
        operation="MODIFIED",
        path="src/example.py",
        max_final_bytes=4096,
        final_mode=0o644,
        before_bytes=len(BEFORE_BYTES),
        before_sha256=BEFORE_SHA,
        before_mode=0o644,
    )

    policy = build_lab_worker_change_policy(
        targets=(target,),
    )

    return build_lab_coding_integration_request(
        repository_path="/tmp/example-repository",
        commit_oid=COMMIT_OID,
        expected_branch="main",
        relative_paths=("src/example.py",),
        goal=goal,
        change_policy=policy,
        max_runtime_seconds=120,
    )


def candidate_for_session(
    session_id: str,
    label: str,
):
    content = f"after {label}\n"
    encoded = content.encode("utf-8")

    candidate_file = LabCodingCandidateFile(
        operation="MODIFIED",
        path="src/example.py",
        before_bytes=len(BEFORE_BYTES),
        before_sha256=BEFORE_SHA,
        before_mode=0o644,
        final_bytes=len(encoded),
        final_sha256=hashlib.sha256(encoded).hexdigest(),
        final_mode=0o644,
        content=content,
    )

    provisional = LabCodingCandidate(
        component=CANDIDATE_COMPONENT,
        schema_version=CANDIDATE_SCHEMA_VERSION,
        candidate_id="0" * 64,
        workspace_session_id=session_id,
        workspace_device=1,
        workspace_inode=2,
        before_snapshot_id="a" * 64,
        after_snapshot_id="b" * 64,
        physical_diff_id="c" * 64,
        files=(candidate_file,),
    )

    candidate = replace(
        provisional,
        candidate_id=_candidate_id(
            provisional
        ),
    )

    return validate_lab_coding_candidate(
        candidate
    )


def completed_lifecycle(
    *,
    request_id: str,
    run_id: str,
    session_id: str,
    worker_request_id: str,
    worker_result_id: str,
    candidate_id: str,
    integration_result_id: str,
):
    requested = build_lab_coding_job_lifecycle(
        request_id=request_id,
        run_id=run_id,
    )

    running = transition_lab_coding_job_lifecycle(
        requested,
        state="RUNNING",
        session_id=session_id,
        worker_request_id=worker_request_id,
    )

    return transition_lab_coding_job_lifecycle(
        running,
        state="COMPLETED",
        worker_result_id=worker_result_id,
        candidate_id=candidate_id,
        integration_result_id=integration_result_id,
    )


def evidence(
    *,
    goal: str,
    run_digit: str,
    session_digit: str,
    worker_request_digit: str,
    worker_result_digit: str,
):
    request = request_for_goal(
        goal
    )

    queue_item = build_lab_coding_job_queue_item(
        request=request,
        run_id=run_digit * 64,
    )

    candidate = candidate_for_session(
        session_digit * 64,
        goal,
    )

    result = build_lab_coding_integration_result(
        request=request,
        candidate_id=candidate.candidate_id,
    )

    lifecycle = completed_lifecycle(
        request_id=request.request_id,
        run_id=queue_item.run_id,
        session_id=candidate.workspace_session_id,
        worker_request_id=worker_request_digit * 64,
        worker_result_id=worker_result_digit * 64,
        candidate_id=candidate.candidate_id,
        integration_result_id=result.result_id,
    )

    review_item = build_lab_coding_review_item(
        lifecycle=lifecycle,
        result=result,
        candidate=candidate,
    )

    return (
        queue_item,
        review_item,
        request,
        lifecycle,
        result,
        candidate,
    )


def single_packet():
    (
        queue_item,
        review_item,
        request,
        lifecycle,
        result,
        candidate,
    ) = evidence(
        goal="first",
        run_digit="1",
        session_digit="2",
        worker_request_digit="3",
        worker_result_digit="4",
    )

    queue = build_lab_coding_job_queue(
        items=(queue_item,),
    )

    packet = build_lab_coding_review_packet(
        queue=queue,
        items=(review_item,),
    )

    return (
        packet,
        queue_item,
        review_item,
        request,
        lifecycle,
        result,
        candidate,
    )


class LabCodingReviewPacketTests(unittest.TestCase):
    def test_review_item_and_packet_are_immutable_and_versioned(self):
        packet, _, review_item, _, _, _, _ = single_packet()

        self.assertEqual(
            packet.component,
            CODING_REVIEW_PACKET_COMPONENT,
        )
        self.assertEqual(
            packet.schema_version,
            CODING_REVIEW_PACKET_SCHEMA_VERSION,
        )
        self.assertEqual(
            validate_lab_coding_review_item(review_item),
            review_item,
        )
        self.assertEqual(
            validate_lab_coding_review_packet(packet),
            packet,
        )

        with self.assertRaises(FrozenInstanceError):
            packet.packet_id = "f" * 64

        with self.assertRaises(FrozenInstanceError):
            review_item.lifecycle = object()

    def test_packet_identity_is_deterministic_and_order_sensitive(self):
        first = evidence(
            goal="first",
            run_digit="1",
            session_digit="2",
            worker_request_digit="3",
            worker_result_digit="4",
        )
        second = evidence(
            goal="second",
            run_digit="5",
            session_digit="6",
            worker_request_digit="7",
            worker_result_digit="8",
        )

        queue_ab = build_lab_coding_job_queue(
            items=(first[0], second[0]),
        )
        queue_ba = build_lab_coding_job_queue(
            items=(second[0], first[0]),
        )

        packet_ab_1 = build_lab_coding_review_packet(
            queue=queue_ab,
            items=(first[1], second[1]),
        )
        packet_ab_2 = build_lab_coding_review_packet(
            queue=queue_ab,
            items=(first[1], second[1]),
        )
        packet_ba = build_lab_coding_review_packet(
            queue=queue_ba,
            items=(second[1], first[1]),
        )

        self.assertEqual(
            packet_ab_1.packet_id,
            packet_ab_2.packet_id,
        )
        self.assertNotEqual(
            packet_ab_1.packet_id,
            packet_ba.packet_id,
        )

    def test_packet_requires_exact_item_count_and_tuple(self):
        packet, _, review_item, _, _, _, _ = single_packet()

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "exact tuple",
        ):
            build_lab_coding_review_packet(
                queue=packet.queue,
                items=[review_item],
            )

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "count",
        ):
            build_lab_coding_review_packet(
                queue=packet.queue,
                items=(),
            )

    def test_review_item_rejects_request_binding_mismatch(self):
        _, _, review_item, _, lifecycle, _, candidate = single_packet()

        wrong_request = request_for_goal(
            "different request"
        )

        wrong_result = build_lab_coding_integration_result(
            request=wrong_request,
            candidate_id=candidate.candidate_id,
        )

        bad = replace(
            review_item,
            result=wrong_result,
        )

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "request_id mismatch",
        ):
            validate_lab_coding_review_item(
                bad
            )

        self.assertEqual(
            lifecycle.request_id,
            review_item.result.request_id,
        )

    def test_packet_rejects_queue_run_and_job_binding_mismatch(self):
        packet, _, review_item, request, _, _, _ = single_packet()

        other_queue_item = build_lab_coding_job_queue_item(
            request=request,
            run_id="9" * 64,
        )
        other_queue = build_lab_coding_job_queue(
            items=(other_queue_item,),
        )

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "run_id mismatch",
        ):
            build_lab_coding_review_packet(
                queue=other_queue,
                items=(review_item,),
            )

        self.assertNotEqual(
            packet.queue.items[0].job_id,
            other_queue_item.job_id,
        )

    def test_review_item_requires_completed_lifecycle(self):
        packet, queue_item, _, _, _, result, candidate = single_packet()

        requested = build_lab_coding_job_lifecycle(
            request_id=queue_item.request.request_id,
            run_id=queue_item.run_id,
        )
        running = transition_lab_coding_job_lifecycle(
            requested,
            state="RUNNING",
            session_id=candidate.workspace_session_id,
            worker_request_id="d" * 64,
        )

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "must be COMPLETED",
        ):
            build_lab_coding_review_item(
                lifecycle=running,
                result=result,
                candidate=candidate,
            )

        self.assertEqual(
            packet.items[0].lifecycle.state,
            "COMPLETED",
        )

    def test_review_item_rejects_candidate_identity_mismatch(self):
        _, _, review_item, _, _, _, _ = single_packet()

        other_candidate = candidate_for_session(
            "e" * 64,
            "other",
        )

        bad = replace(
            review_item,
            candidate=other_candidate,
        )

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "candidate_id mismatch",
        ):
            validate_lab_coding_review_item(
                bad
            )

    def test_review_item_rejects_session_binding_mismatch(self):
        _, queue_item, _, _, _, result, candidate = single_packet()

        wrong_session_lifecycle = completed_lifecycle(
            request_id=queue_item.request.request_id,
            run_id=queue_item.run_id,
            session_id="e" * 64,
            worker_request_id="d" * 64,
            worker_result_id="c" * 64,
            candidate_id=candidate.candidate_id,
            integration_result_id=result.result_id,
        )

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "workspace_session_id mismatch",
        ):
            build_lab_coding_review_item(
                lifecycle=wrong_session_lifecycle,
                result=result,
                candidate=candidate,
            )

    def test_packet_id_tamper_is_refused(self):
        packet, _, _, _, _, _, _ = single_packet()

        tampered = replace(
            packet,
            packet_id="f" * 64,
        )

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "packet_id does not match",
        ):
            validate_lab_coding_review_packet(
                tampered
            )

    def test_wire_and_bytes_round_trip_exactly(self):
        packet, _, _, _, _, _, _ = single_packet()

        wire = lab_coding_review_packet_to_wire(
            packet
        )
        decoded_wire = lab_coding_review_packet_from_wire(
            wire
        )

        self.assertEqual(
            decoded_wire,
            packet,
        )

        encoded = lab_coding_review_packet_to_bytes(
            packet
        )
        decoded_bytes = lab_coding_review_packet_from_bytes(
            encoded
        )

        self.assertEqual(
            decoded_bytes,
            packet,
        )

        self.assertEqual(
            encoded,
            json.dumps(
                wire,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8"),
        )

    def test_wire_and_bytes_fail_closed_on_inexact_structure(self):
        packet, _, _, _, _, _, _ = single_packet()

        wire = lab_coding_review_packet_to_wire(
            packet
        )
        malformed = dict(wire)
        malformed["unexpected"] = True

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "fields do not exactly match",
        ):
            lab_coding_review_packet_from_wire(
                malformed
            )

        encoded = lab_coding_review_packet_to_bytes(
            packet
        )

        with self.assertRaisesRegex(
            LabCodingReviewPacketError,
            "not canonical JSON",
        ):
            lab_coding_review_packet_from_bytes(
                b" " + encoded
            )

    def test_module_has_no_execution_promotion_approval_or_io_authority(self):
        module_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hands_free_auto_lab"
            / "lab_coding_review_packet.py"
        )

        source = module_path.read_text(
            encoding="utf-8"
        )
        tree = ast.parse(
            source,
            filename=str(module_path),
        )

        imported_modules = []

        for node in tree.body:
            if isinstance(node, ast.Import):
                imported_modules.extend(
                    alias.name
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                imported_modules.append(
                    node.module or ""
                )

        self.assertEqual(
            imported_modules,
            [
                "__future__",
                "dataclasses",
                "hashlib",
                "json",
                "lab_coding_candidate",
                "lab_coding_integration_contract",
                "lab_coding_job_lifecycle",
                "lab_coding_job_queue",
            ],
        )

        forbidden_imports = {
            "os",
            "pathlib",
            "subprocess",
            "socket",
            "requests",
            "urllib",
            "http",
            "lab_coding_integration",
            "lab_coding_job_dispatcher",
            "lab_coding_job_lifecycle_journal",
            "lab_coding_candidate_store",
            "lab_promotion_proposal",
            "lab_promotion_inspector",
            "lab_promotion_approval_store",
            "lab_codex_worker",
            "lab_worker",
        }

        self.assertTrue(
            forbidden_imports.isdisjoint(
                set(imported_modules)
            )
        )

        call_names = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            if isinstance(node.func, ast.Name):
                call_names.add(
                    node.func.id
                )
            elif isinstance(node.func, ast.Attribute):
                call_names.add(
                    node.func.attr
                )

        forbidden_calls = {
            "open",
            "write",
            "mkdir",
            "unlink",
            "remove",
            "rename",
            "replace",
            "chmod",
            "system",
            "Popen",
            "run_lab_coding_job_queue",
            "run_lab_coding_integration_request_with_lifecycle",
            "load_lab_coding_job_status",
            "load_lab_coding_job_lifecycle_journal",
            "load_lab_coding_candidate",
            "persist_lab_coding_candidate",
            "inspect_lab_promotion_repository",
            "build_lab_coding_candidate_promotion_proposal",
            "create_promotion_challenge",
            "decide_promotion_challenge",
            "consume_promotion_approval",
        }

        self.assertTrue(
            forbidden_calls.isdisjoint(
                call_names
            )
        )
