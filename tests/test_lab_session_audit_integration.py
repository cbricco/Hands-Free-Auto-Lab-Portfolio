from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from hands_free_auto_lab.lab_audit import (
    LabAuditError,
    write_lab_session_audit,
)
from hands_free_auto_lab.lab_model_adapter import (
    LabModelTurn,
    parse_model_decision,
)
from hands_free_auto_lab.lab_session import (
    lab_session_record_to_wire,
    run_lab_session,
)
from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)


SESSION_ID = "c" * 64


class ScriptedRequester:
    def __init__(self, *items):
        self.items = list(items)
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

        item = self.items.pop(0)

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


class SessionAuditIntegrationTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-session-audit-integration-"
            )
        )

        os.chmod(
            self.root,
            0o700,
        )

        self.workspace_parent = (
            self.root
            / "workspaces"
        )
        self.workspace_parent.mkdir(
            mode=0o700
        )

        self.evidence = (
            self.root
            / "evidence"
        )
        self.evidence.mkdir(
            mode=0o700
        )

        self.workspace = create_lab_workspace(
            str(
                self.workspace_parent
            ),
            session_id=SESSION_ID,
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def test_session_then_explicit_audit_preserves_exact_record(self):
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

        with patch(
            "hands_free_auto_lab.lab_session._utc_timestamp",
            side_effect=[
                "2026-08-19T03:00:00.000001Z",
                "2026-08-19T03:00:01.000002Z",
            ],
        ):
            record = run_lab_session(
                goal="Create and run one smoke test.",
                model="test-model",
                workspace_parent=str(
                    self.workspace_parent
                ),
                workspace=self.workspace,
                max_iterations=3,
                requester=requester,
            )

        self.assertTrue(
            record.succeeded
        )

        self.assertEqual(
            len(requester.calls),
            3,
        )

        self.assertEqual(
            list(
                self.evidence.iterdir()
            ),
            [],
        )

        wire = lab_session_record_to_wire(
            record
        )

        audit = write_lab_session_audit(
            record=record,
            evidence_directory=str(
                self.evidence
            ),
        )

        destination = Path(
            audit.path
        )

        self.assertEqual(
            destination.parent,
            self.evidence,
        )

        self.assertEqual(
            destination.name,
            (
                "session-"
                + SESSION_ID
                + ".json"
            ),
        )

        final_stat = destination.stat()

        self.assertEqual(
            stat.S_IMODE(
                final_stat.st_mode
            ),
            0o600,
        )

        self.assertEqual(
            final_stat.st_nlink,
            1,
        )

        persisted = json.loads(
            destination.read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            persisted,
            wire,
        )

        self.assertEqual(
            persisted[
                "workspace"
            ][
                "session_id"
            ],
            SESSION_ID,
        )

        controller = persisted[
            "controller"
        ]

        self.assertEqual(
            controller[
                "status"
            ],
            "done",
        )

        self.assertEqual(
            controller[
                "iterations"
            ],
            3,
        )

        run_step = controller[
            "steps"
        ][1]

        self.assertEqual(
            run_step[
                "action"
            ][
                "kind"
            ],
            "RUN",
        )

        run_result = run_step[
            "result"
        ]

        self.assertFalse(
            run_result[
                "timed_out"
            ]
        )

        self.assertFalse(
            run_result[
                "stdout_truncated"
            ]
        )

        self.assertFalse(
            run_result[
                "stderr_truncated"
            ]
        )

        self.assertEqual(
            run_result[
                "exit_status"
            ],
            0,
        )

        self.assertIsNone(
            run_result[
                "signal"
            ]
        )

        with self.assertRaisesRegex(
            LabAuditError,
            "already exists",
        ):
            write_lab_session_audit(
                record=record,
                evidence_directory=str(
                    self.evidence
                ),
            )

        self.assertEqual(
            json.loads(
                destination.read_text(
                    encoding="utf-8"
                )
            ),
            persisted,
        )


if __name__ == "__main__":
    unittest.main()
