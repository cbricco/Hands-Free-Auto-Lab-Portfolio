from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import unittest

from hands_free_auto_lab.lab_coding_integration_contract import (
    build_lab_coding_integration_request,
)
from hands_free_auto_lab.lab_coding_job_lifecycle import (
    build_lab_coding_job_lifecycle,
)
from hands_free_auto_lab.lab_coding_job_queue import (
    CODING_JOB_QUEUE_COMPONENT,
    CODING_JOB_QUEUE_ITEM_COMPONENT,
    CODING_JOB_QUEUE_SCHEMA_VERSION,
    MAX_CODING_JOB_QUEUE_ITEMS,
    LabCodingJobQueueError,
    LabCodingJobQueueItem,
    build_lab_coding_job_queue,
    build_lab_coding_job_queue_item,
    validate_lab_coding_job_queue,
    validate_lab_coding_job_queue_item,
)
from hands_free_auto_lab.lab_worker_change_policy import (
    LabWorkerChangeTarget,
    build_lab_worker_change_policy,
)


RUN_ID_A = "1" * 64
RUN_ID_B = "2" * 64

COMMIT_OID = "a" * 40
BEFORE_SHA = "b" * 64


def request_for_goal(goal: str):
    target = LabWorkerChangeTarget(
        operation="MODIFIED",
        path="src/example.py",
        max_final_bytes=4096,
        final_mode=0o644,
        before_bytes=1,
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


class LabCodingJobQueueTests(unittest.TestCase):
    def test_item_is_immutable_and_binds_existing_job_identity(self):
        request = request_for_goal("first")
        item = build_lab_coding_job_queue_item(
            request=request,
            run_id=RUN_ID_A,
        )
        lifecycle = build_lab_coding_job_lifecycle(
            request_id=request.request_id,
            run_id=RUN_ID_A,
        )

        self.assertEqual(
            item.component,
            CODING_JOB_QUEUE_ITEM_COMPONENT,
        )
        self.assertEqual(item.request, request)
        self.assertEqual(item.run_id, RUN_ID_A)
        self.assertEqual(item.job_id, lifecycle.job_id)

        with self.assertRaises(FrozenInstanceError):
            item.run_id = RUN_ID_B

    def test_invalid_request_or_run_id_fails_closed(self):
        request = request_for_goal("first")

        with self.assertRaises(Exception):
            build_lab_coding_job_queue_item(
                request=object(),
                run_id=RUN_ID_A,
            )

        with self.assertRaises(Exception):
            build_lab_coding_job_queue_item(
                request=request,
                run_id="not-valid",
            )

    def test_item_tampering_is_rejected(self):
        item = build_lab_coding_job_queue_item(
            request=request_for_goal("first"),
            run_id=RUN_ID_A,
        )

        with self.assertRaises(LabCodingJobQueueError):
            validate_lab_coding_job_queue_item(
                replace(
                    item,
                    job_id="f" * 64,
                )
            )

        with self.assertRaises(LabCodingJobQueueError):
            validate_lab_coding_job_queue_item(
                replace(
                    item,
                    component="wrong-component",
                )
            )

    def test_queue_is_immutable_bounded_and_preserves_submission_order(self):
        item_a = build_lab_coding_job_queue_item(
            request=request_for_goal("first"),
            run_id=RUN_ID_A,
        )
        item_b = build_lab_coding_job_queue_item(
            request=request_for_goal("second"),
            run_id=RUN_ID_B,
        )

        queue = build_lab_coding_job_queue(
            items=(item_a, item_b),
        )

        self.assertEqual(
            queue.component,
            CODING_JOB_QUEUE_COMPONENT,
        )
        self.assertEqual(
            queue.schema_version,
            CODING_JOB_QUEUE_SCHEMA_VERSION,
        )
        self.assertEqual(
            queue.items,
            (item_a, item_b),
        )

        with self.assertRaises(FrozenInstanceError):
            queue.queue_id = "0" * 64

    def test_queue_identity_is_deterministic_and_order_sensitive(self):
        item_a = build_lab_coding_job_queue_item(
            request=request_for_goal("first"),
            run_id=RUN_ID_A,
        )
        item_b = build_lab_coding_job_queue_item(
            request=request_for_goal("second"),
            run_id=RUN_ID_B,
        )

        first = build_lab_coding_job_queue(
            items=(item_a, item_b),
        )
        again = build_lab_coding_job_queue(
            items=(item_a, item_b),
        )
        reversed_queue = build_lab_coding_job_queue(
            items=(item_b, item_a),
        )

        self.assertEqual(first, again)
        self.assertNotEqual(
            first.queue_id,
            reversed_queue.queue_id,
        )

    def test_queue_rejects_empty_non_tuple_and_over_limit_items(self):
        with self.assertRaises(LabCodingJobQueueError):
            build_lab_coding_job_queue(
                items=[],
            )

        with self.assertRaises(LabCodingJobQueueError):
            build_lab_coding_job_queue(
                items=(),
            )

        items = tuple(
            build_lab_coding_job_queue_item(
                request=request_for_goal(f"goal-{index}"),
                run_id=f"{index + 1:064x}",
            )
            for index in range(MAX_CODING_JOB_QUEUE_ITEMS + 1)
        )

        with self.assertRaises(LabCodingJobQueueError):
            build_lab_coding_job_queue(
                items=items,
            )

    def test_queue_rejects_over_limit_before_item_validation(self):
        items = tuple(
            object()
            for _ in range(MAX_CODING_JOB_QUEUE_ITEMS + 1)
        )

        with self.assertRaisesRegex(
            LabCodingJobQueueError,
            "exceeds maximum item count",
        ):
            build_lab_coding_job_queue(
                items=items,
            )

    def test_queue_rejects_duplicate_request_id_even_with_new_run_id(self):
        request = request_for_goal("same request")

        item_a = build_lab_coding_job_queue_item(
            request=request,
            run_id=RUN_ID_A,
        )
        item_b = build_lab_coding_job_queue_item(
            request=request,
            run_id=RUN_ID_B,
        )

        with self.assertRaisesRegex(
            LabCodingJobQueueError,
            "duplicate request_id",
        ):
            build_lab_coding_job_queue(
                items=(item_a, item_b),
            )

    def test_queue_rejects_duplicate_run_id(self):
        item_a = build_lab_coding_job_queue_item(
            request=request_for_goal("first"),
            run_id=RUN_ID_A,
        )
        item_b = build_lab_coding_job_queue_item(
            request=request_for_goal("second"),
            run_id=RUN_ID_A,
        )

        with self.assertRaisesRegex(
            LabCodingJobQueueError,
            "duplicate run_id",
        ):
            build_lab_coding_job_queue(
                items=(item_a, item_b),
            )

    def test_queue_tampering_is_rejected(self):
        item = build_lab_coding_job_queue_item(
            request=request_for_goal("first"),
            run_id=RUN_ID_A,
        )
        queue = build_lab_coding_job_queue(
            items=(item,),
        )

        with self.assertRaises(LabCodingJobQueueError):
            validate_lab_coding_job_queue(
                replace(
                    queue,
                    queue_id="f" * 64,
                )
            )

        with self.assertRaises(LabCodingJobQueueError):
            validate_lab_coding_job_queue(
                replace(
                    queue,
                    component="wrong-component",
                )
            )

    def test_queue_item_requires_exact_record_type(self):
        item = build_lab_coding_job_queue_item(
            request=request_for_goal("first"),
            run_id=RUN_ID_A,
        )

        with self.assertRaises(LabCodingJobQueueError):
            validate_lab_coding_job_queue_item(object())

        with self.assertRaises(LabCodingJobQueueError):
            build_lab_coding_job_queue(
                items=(object(),),
            )

        self.assertIsInstance(
            validate_lab_coding_job_queue_item(item),
            LabCodingJobQueueItem,
        )

    def test_queue_model_imports_no_execution_persistence_or_authority(self):
        import hands_free_auto_lab.lab_coding_job_queue as queue_module

        source = Path(queue_module.__file__).read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        forbidden = {
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "lab_coding_integration",
            "lab_coding_job_lifecycle_journal",
            "lab_coding_job_status",
            "lab_coding_workflow",
            "lab_coding_job",
            "lab_worker_job",
            "lab_codex_worker",
            "lab_promotion",
            "lab_promotion_approval_store",
            "lab_promotion_target_preparer",
        }

        imported = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(
                    alias.name
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    imported.add(node.module)

        for name in imported:
            for blocked in forbidden:
                self.assertFalse(
                    name == blocked
                    or name.endswith("." + blocked),
                    msg=f"forbidden import: {name}",
                )


if __name__ == "__main__":
    unittest.main()
