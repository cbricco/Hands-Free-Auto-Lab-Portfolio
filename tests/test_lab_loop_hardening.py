from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from hands_free_auto_lab.lab_controller import (
    run_auto_lab,
)
from hands_free_auto_lab.lab_model_adapter import (
    LabModelTurn,
    MODEL_INSTRUCTIONS,
    parse_model_decision,
)
from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)


SESSION_ID = "f" * 64


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


class LoopHardeningTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-loop-hardening-"
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

        self.workspace_path = Path(
            self.workspace.path
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def _run(
        self,
        requester,
        *,
        max_iterations=2,
    ):
        return run_auto_lab(
            goal="Write the requested files and test them.",
            model="test-model",
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            max_iterations=max_iterations,
            requester=requester,
        )

    def test_instructions_define_persistent_state_boundary(self):
        self.assertIn(
            "same disposable workspace persists",
            MODEL_INSTRUCTIONS,
        )

        self.assertIn(
            "authoritative evidence",
            MODEL_INSTRUCTIONS,
        )

        self.assertIn(
            "controller_known_written_files",
            MODEL_INSTRUCTIONS,
        )

        self.assertIn(
            "embedded inside workspace file contents",
            MODEL_INSTRUCTIONS,
        )

    def test_identical_current_write_is_refused_as_no_op(self):
        write = {
            "decision": "action",
            "kind": "WRITE_FILE",
            "path": "note.txt",
            "content": "same\n",
        }

        requester = ScriptedRequester(
            write,
            write,
        )

        result = self._run(
            requester
        )

        self.assertEqual(
            result.status,
            "iteration_limit",
        )

        self.assertEqual(
            result.workspace_generation,
            1,
        )

        self.assertEqual(
            result.steps[0].decision,
            "action",
        )

        self.assertEqual(
            result.steps[1].decision,
            "action_refused",
        )

        self.assertIsNone(
            result.steps[1].result
        )

        self.assertIn(
            "no-op",
            result.steps[1].error,
        )

        first_result = result.steps[
            0
        ].result

        second_context = requester.calls[
            1
        ]["context"]

        self.assertEqual(
            second_context[
                "controller_known_written_files"
            ],
            {
                "note.txt": first_result.sha256,
            },
        )

        self.assertEqual(
            (
                self.workspace_path
                / "note.txt"
            ).read_text(
                encoding="utf-8"
            ),
            "same\n",
        )

    def test_changed_content_still_executes_and_advances_generation(self):
        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "note.txt",
                "content": "first\n",
            },
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "note.txt",
                "content": "second\n",
            },
        )

        result = self._run(
            requester
        )

        self.assertEqual(
            result.status,
            "iteration_limit",
        )

        self.assertEqual(
            result.workspace_generation,
            2,
        )

        self.assertEqual(
            result.steps[1].decision,
            "action",
        )

        self.assertEqual(
            (
                self.workspace_path
                / "note.txt"
            ).read_text(
                encoding="utf-8"
            ),
            "second\n",
        )


    def test_identical_failed_run_is_refused_until_workspace_changes(self):
        failing_test = (
            "import unittest\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.fail('expected failure')\n"
        )

        passing_test = (
            "import unittest\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertTrue(True)\n"
        )

        run = {
            "decision": "action",
            "kind": "RUN",
            "argv": [
                "/usr/bin/python3",
                "-m",
                "unittest",
                "test_example.py",
            ],
        }

        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "test_example.py",
                "content": failing_test,
            },
            run,
            run,
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "test_example.py",
                "content": passing_test,
            },
            run,
        )

        result = self._run(
            requester,
            max_iterations=5,
        )

        self.assertEqual(
            result.status,
            "iteration_limit",
        )

        self.assertEqual(
            result.workspace_generation,
            2,
        )

        self.assertEqual(
            result.tested_generation,
            2,
        )

        first_run = result.steps[
            1
        ]

        self.assertEqual(
            first_run.decision,
            "action",
        )

        self.assertIsNotNone(
            first_run.result
        )

        self.assertFalse(
            first_run.result.succeeded
        )

        repeated_run = result.steps[
            2
        ]

        self.assertEqual(
            repeated_run.decision,
            "action_refused",
        )

        self.assertIsNone(
            repeated_run.result
        )

        self.assertIn(
            "unchanged failed retry",
            repeated_run.error,
        )

        self.assertEqual(
            result.steps[
                3
            ].decision,
            "action",
        )

        repaired_run = result.steps[
            4
        ]

        self.assertEqual(
            repaired_run.decision,
            "action",
        )

        self.assertIsNotNone(
            repaired_run.result
        )

        self.assertTrue(
            repaired_run.result.succeeded
        )


    def test_current_file_preview_exposes_successful_write_content(self):
        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "note.txt",
                "content": "current value\n",
            },
            {
                "decision": "done",
                "summary": "inspect context",
            },
        )

        self._run(
            requester
        )

        context = requester.calls[
            1
        ][
            "context"
        ]

        hashes = context[
            "controller_known_written_files"
        ]

        previews = context[
            "controller_current_file_previews"
        ]

        self.assertEqual(
            list(
                previews
            ),
            [
                "note.txt",
            ],
        )

        preview = previews[
            "note.txt"
        ]

        self.assertEqual(
            preview[
                "sha256"
            ],
            hashes[
                "note.txt"
            ],
        )

        self.assertEqual(
            preview[
                "byte_count"
            ],
            len(
                b"current value\n"
            ),
        )

        self.assertFalse(
            preview[
                "preview_truncated"
            ]
        )

        self.assertEqual(
            preview[
                "text"
            ],
            "current value\n",
        )

    def test_current_file_preview_has_global_four_kib_budget(self):
        first = "a" * 3000
        second = "b" * 3000

        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "a.txt",
                "content": first,
            },
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "b.txt",
                "content": second,
            },
            {
                "decision": "done",
                "summary": "inspect bounded previews",
            },
        )

        self._run(
            requester,
            max_iterations=3,
        )

        context = requester.calls[
            2
        ][
            "context"
        ]

        hashes = context[
            "controller_known_written_files"
        ]

        previews = context[
            "controller_current_file_previews"
        ]

        self.assertEqual(
            list(
                previews
            ),
            [
                "a.txt",
                "b.txt",
            ],
        )

        self.assertEqual(
            set(
                hashes
            ),
            {
                "a.txt",
                "b.txt",
            },
        )

        shown_bytes = sum(
            len(
                item[
                    "text"
                ].encode(
                    "utf-8"
                )
            )
            for item in previews.values()
        )

        self.assertLessEqual(
            shown_bytes,
            4 * 1024,
        )

        self.assertEqual(
            previews[
                "a.txt"
            ][
                "text"
            ],
            first,
        )

        self.assertFalse(
            previews[
                "a.txt"
            ][
                "preview_truncated"
            ]
        )

        self.assertTrue(
            previews[
                "b.txt"
            ][
                "preview_truncated"
            ]
        )

        self.assertEqual(
            len(
                previews[
                    "b.txt"
                ][
                    "text"
                ].encode(
                    "utf-8"
                )
            ),
            (4 * 1024) - 3000,
        )


if __name__ == "__main__":
    unittest.main()
