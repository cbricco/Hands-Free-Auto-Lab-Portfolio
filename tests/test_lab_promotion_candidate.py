from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from hands_free_auto_lab.lab_model_adapter import (
    LabModelTurn,
    parse_model_decision,
)
from hands_free_auto_lab.lab_promotion_candidate import (
    PROMOTION_CANDIDATE_COMPONENT,
    PROMOTION_CANDIDATE_SCHEMA_VERSION,
    LabPromotionCandidateError,
    build_lab_promotion_candidate,
)
from hands_free_auto_lab.lab_session import (
    run_lab_session,
)
from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)
from hands_free_auto_lab.lab_write_file import (
    LabWriteFileResult,
)


SESSION_ID = "d" * 64


class ScriptedRequester:
    def __init__(
        self,
        *items,
    ):
        self.items = list(
            items
        )
        self.calls = []

    def __call__(
        self,
        *,
        goal,
        context,
        model,
        timeout_seconds,
    ):
        self.calls.append(
            {
                "goal": goal,
                "context": context,
                "model": model,
                "timeout_seconds": timeout_seconds,
            }
        )

        if not self.items:
            raise AssertionError(
                "scripted requester exhausted"
            )

        item = self.items.pop(
            0
        )

        text = json.dumps(
            item,
            separators=(",", ":"),
        )

        return LabModelTurn(
            model=model,
            text=text,
            response={
                "done": True,
                "response": text,
            },
            decision=parse_model_decision(
                text
            ),
        )


class LabPromotionCandidateTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-promotion-candidate-tests-"
            )
        )

        os.chmod(
            self.root,
            0o700,
        )

        self.parent = (
            self.root
            / "workspaces"
        )

        self.parent.mkdir(
            mode=0o700
        )

        self.workspace = create_lab_workspace(
            str(
                self.parent
            ),
            session_id=SESSION_ID,
        )

        self.workspace_path = Path(
            self.workspace.path
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def _completed_record(self):
        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "item.txt",
                "content": "old value\n",
            },
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "test_smoke.py",
                "content": (
                    "import unittest\n"
                    "\n"
                    "class SmokeTest(unittest.TestCase):\n"
                    "    def test_true(self):\n"
                    "        self.assertTrue(True)\n"
                ),
            },
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "item.txt",
                "content": "final value\n",
            },
            {
                "decision": "action",
                "kind": "RUN",
                "argv": [
                    "/usr/bin/python3",
                    "-m",
                    "unittest",
                    "-v",
                    "test_smoke",
                ],
            },
            {
                "decision": "done",
                "summary": "Current workspace passed.",
            },
        )

        with patch(
            "hands_free_auto_lab.lab_session._utc_timestamp",
            side_effect=[
                "2026-08-19T05:00:00.000001Z",
                "2026-08-19T05:00:01.000002Z",
            ],
        ):
            record = run_lab_session(
                goal="Produce two reviewed files.",
                model="test-model",
                workspace_parent=str(
                    self.parent
                ),
                workspace=self.workspace,
                max_iterations=5,
                requester=requester,
            )

        self.assertTrue(
            record.succeeded
        )

        self.assertEqual(
            record.controller.workspace_generation,
            3,
        )

        self.assertEqual(
            record.controller.tested_generation,
            3,
        )

        return record

    def test_candidate_securely_rereads_final_deduplicated_files(self):
        record = self._completed_record()

        candidate = build_lab_promotion_candidate(
            record=record,
            workspace_parent=str(
                self.parent
            ),
        )

        self.assertEqual(
            candidate.component,
            PROMOTION_CANDIDATE_COMPONENT,
        )

        self.assertEqual(
            candidate.schema_version,
            PROMOTION_CANDIDATE_SCHEMA_VERSION,
        )

        self.assertEqual(
            candidate.session_id,
            SESSION_ID,
        )

        self.assertEqual(
            candidate.workspace_device,
            self.workspace.device,
        )

        self.assertEqual(
            candidate.workspace_inode,
            self.workspace.inode,
        )

        self.assertEqual(
            candidate.controller_status,
            "done",
        )

        self.assertEqual(
            candidate.workspace_generation,
            3,
        )

        self.assertEqual(
            candidate.tested_generation,
            3,
        )

        self.assertIsNone(
            candidate.acceptance_generation
        )

        self.assertEqual(
            [
                item.path
                for item in candidate.files
            ],
            [
                "item.txt",
                "test_smoke.py",
            ],
        )

        self.assertEqual(
            candidate.files[0].content,
            "final value\n",
        )

        latest_item_write = [
            step.result
            for step in record.controller.steps
            if (
                isinstance(
                    step.result,
                    LabWriteFileResult,
                )
                and step.result.path
                == "item.txt"
            )
        ][-1]

        self.assertEqual(
            candidate.files[0].sha256,
            latest_item_write.sha256,
        )

        self.assertEqual(
            candidate.files[0].bytes,
            latest_item_write.bytes_written,
        )

        self.assertEqual(
            candidate.files[0].latest_write_action_id,
            latest_item_write.action_id,
        )

    def test_incomplete_session_is_refused(self):
        record = self._completed_record()

        tampered_controller = replace(
            record.controller,
            status="iteration_limit",
        )

        tampered = replace(
            record,
            controller=tampered_controller,
        )

        with self.assertRaisesRegex(
            LabPromotionCandidateError,
            "not complete",
        ):
            build_lab_promotion_candidate(
                record=tampered,
                workspace_parent=str(
                    self.parent
                ),
            )

    def test_stale_worker_test_generation_is_refused(self):
        record = self._completed_record()

        tampered_controller = replace(
            record.controller,
            tested_generation=2,
        )

        tampered = replace(
            record,
            controller=tampered_controller,
        )

        with self.assertRaisesRegex(
            LabPromotionCandidateError,
            "successful worker RUN",
        ):
            build_lab_promotion_candidate(
                record=tampered,
                workspace_parent=str(
                    self.parent
                ),
            )

    def test_stale_present_acceptance_generation_is_refused(self):
        record = self._completed_record()

        tampered_controller = replace(
            record.controller,
            acceptance_generation=2,
        )

        tampered = replace(
            record,
            controller=tampered_controller,
        )

        with self.assertRaisesRegex(
            LabPromotionCandidateError,
            "acceptance evidence is stale",
        ):
            build_lab_promotion_candidate(
                record=tampered,
                workspace_parent=str(
                    self.parent
                ),
            )

    def test_completed_record_without_write_evidence_is_refused(self):
        record = self._completed_record()

        stripped_steps = tuple(
            step
            for step in record.controller.steps
            if not isinstance(
                step.result,
                LabWriteFileResult,
            )
        )

        tampered_controller = replace(
            record.controller,
            steps=stripped_steps,
        )

        tampered = replace(
            record,
            controller=tampered_controller,
        )

        with self.assertRaisesRegex(
            LabPromotionCandidateError,
            "no files to promote",
        ):
            build_lab_promotion_candidate(
                record=tampered,
                workspace_parent=str(
                    self.parent
                ),
            )

    def test_changed_final_file_is_refused(self):
        record = self._completed_record()

        (
            self.workspace_path
            / "item.txt"
        ).write_text(
            "tampered after session\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabPromotionCandidateError,
            "does not match latest successful write",
        ):
            build_lab_promotion_candidate(
                record=record,
                workspace_parent=str(
                    self.parent
                ),
            )

    def test_write_workspace_identity_mismatch_is_refused(self):
        record = self._completed_record()

        steps = list(
            record.controller.steps
        )

        for index, step in enumerate(
            steps
        ):
            if isinstance(
                step.result,
                LabWriteFileResult,
            ):
                bad_result = replace(
                    step.result,
                    workspace_inode=(
                        step.result.workspace_inode
                        + 1
                    ),
                )

                steps[index] = replace(
                    step,
                    result=bad_result,
                )
                break
        else:
            self.fail(
                "expected WRITE_FILE result"
            )

        tampered_controller = replace(
            record.controller,
            steps=tuple(
                steps
            ),
        )

        tampered = replace(
            record,
            controller=tampered_controller,
        )

        with self.assertRaisesRegex(
            LabPromotionCandidateError,
            "workspace identity mismatch",
        ):
            build_lab_promotion_candidate(
                record=tampered,
                workspace_parent=str(
                    self.parent
                ),
            )

    def test_wrong_record_type_is_refused(self):
        with self.assertRaises(
            TypeError
        ):
            build_lab_promotion_candidate(
                record=object(),
                workspace_parent=str(
                    self.parent
                ),
            )


if __name__ == "__main__":
    unittest.main()
