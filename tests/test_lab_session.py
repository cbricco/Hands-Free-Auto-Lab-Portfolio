from __future__ import annotations

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
from hands_free_auto_lab.lab_session import (
    SESSION_COMPONENT,
    SESSION_SCHEMA_VERSION,
    lab_session_record_to_wire,
    run_lab_session,
)
from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)


SESSION_ID = "a" * 64


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


class LabSessionTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-session-tests-"
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
            mode=0o700,
        )

        self.workspace = create_lab_workspace(
            str(self.parent),
            session_id=SESSION_ID,
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def test_session_wraps_complete_controller_evidence(self):
        requester = ScriptedRequester(
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
                "summary": "Smoke test passed.",
            },
        )

        timestamps = [
            "2026-08-19T01:00:00.000001Z",
            "2026-08-19T01:00:01.000002Z",
        ]

        with patch(
            "hands_free_auto_lab.lab_session._utc_timestamp",
            side_effect=timestamps,
        ):
            record = run_lab_session(
                goal="Create and run one smoke test.",
                model="test-model",
                workspace_parent=str(
                    self.parent
                ),
                workspace=self.workspace,
                max_iterations=3,
                requester=requester,
            )

        self.assertTrue(
            record.succeeded
        )

        self.assertEqual(
            record.component,
            SESSION_COMPONENT,
        )

        self.assertEqual(
            record.schema_version,
            SESSION_SCHEMA_VERSION,
        )

        self.assertEqual(
            record.started_at_utc,
            timestamps[0],
        )

        self.assertEqual(
            record.finished_at_utc,
            timestamps[1],
        )

        self.assertEqual(
            record.workspace,
            self.workspace,
        )

        self.assertEqual(
            record.controller.status,
            "done",
        )

        self.assertEqual(
            record.controller.iterations,
            3,
        )

        self.assertEqual(
            len(
                record.controller.steps
            ),
            3,
        )

        run_step = record.controller.steps[
            1
        ]

        self.assertEqual(
            run_step.action.kind,
            "RUN",
        )

        self.assertIsNotNone(
            run_step.result
        )

        self.assertTrue(
            run_step.result.succeeded
        )

        wire = lab_session_record_to_wire(
            record
        )

        encoded = json.dumps(
            wire,
            sort_keys=True,
        )

        self.assertIn(
            SESSION_COMPONENT,
            encoded,
        )

        self.assertIn(
            SESSION_ID,
            encoded,
        )

        self.assertIn(
            "Smoke test passed.",
            encoded,
        )

        self.assertEqual(
            len(requester.calls),
            3,
        )

    def test_session_wrapper_does_not_persist_audit_file(self):
        requester = ScriptedRequester(
            {
                "decision": "done",
                "summary": "not yet tested",
            },
        )

        with patch(
            "hands_free_auto_lab.lab_session._utc_timestamp",
            side_effect=[
                "2026-08-19T02:00:00.000001Z",
                "2026-08-19T02:00:01.000002Z",
            ],
        ):
            record = run_lab_session(
                goal="Do not complete.",
                model="test-model",
                workspace_parent=str(
                    self.parent
                ),
                workspace=self.workspace,
                max_iterations=1,
                requester=requester,
            )

        self.assertFalse(
            record.succeeded
        )

        self.assertEqual(
            record.controller.status,
            "iteration_limit",
        )

        outside_workspace_files = [
            path
            for path in self.root.rglob("*")
            if (
                path.is_file()
                and self.workspace.path
                not in str(path)
            )
        ]

        self.assertEqual(
            outside_workspace_files,
            [],
        )

    def test_wire_conversion_is_json_native_and_round_trip_stable(self):
        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "note.txt",
                "content": "wire format test\n",
            },
        )

        with patch(
            "hands_free_auto_lab.lab_session._utc_timestamp",
            side_effect=[
                "2026-08-19T04:00:00.000001Z",
                "2026-08-19T04:00:01.000002Z",
            ],
        ):
            record = run_lab_session(
                goal="Create one test file.",
                model="test-model",
                workspace_parent=str(
                    self.parent
                ),
                workspace=self.workspace,
                max_iterations=1,
                requester=requester,
            )

        wire = lab_session_record_to_wire(
            record
        )

        self.assertIsInstance(
            wire[
                "controller"
            ][
                "steps"
            ],
            list,
        )

        self.assertIsInstance(
            wire[
                "controller"
            ][
                "steps"
            ][0][
                "action"
            ][
                "argv"
            ],
            list,
        )

        encoded = json.dumps(
            wire,
            sort_keys=True,
        )

        decoded = json.loads(
            encoded
        )

        self.assertEqual(
            decoded,
            wire,
        )

    def test_wire_conversion_rejects_wrong_type(self):
        with self.assertRaises(
            TypeError
        ):
            lab_session_record_to_wire(
                object()
            )


if __name__ == "__main__":
    unittest.main()
