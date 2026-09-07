from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from hands_free_auto_lab.lab_controller import (
    CONTEXT_TEXT_PREVIEW_BYTES,
    LabControllerError,
    run_auto_lab,
)
from hands_free_auto_lab.lab_model_adapter import (
    LabModelAdapterError,
    LabModelTurn,
    parse_model_decision,
)
from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)


SESSION_ID = "e" * 64


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
            raise LabModelAdapterError(
                "scripted model exhausted"
            )

        item = self.items.pop(
            0
        )

        if isinstance(
            item,
            Exception,
        ):
            raise item

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


class LabControllerTests(
    unittest.TestCase
):
    def setUp(self):
        self.test_root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-controller-tests-"
            )
        )

        os.chmod(
            self.test_root,
            0o700,
        )

        self.parent = (
            self.test_root
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
            self.test_root
        )

    def _run(
        self,
        requester,
        *,
        max_iterations=20,
    ):
        return run_auto_lab(
            goal="Build and test a tiny calculator.",
            model="test-model",
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            max_iterations=max_iterations,
            requester=requester,
        )

    def test_end_to_end_write_test_done(self):
        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "calculator.py",
                "content": (
                    "def add(a, b):\n"
                    "    return a + b\n"
                ),
            },
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "test_calculator.py",
                "content": (
                    "import unittest\n"
                    "from calculator import add\n"
                    "class CalculatorTest(unittest.TestCase):\n"
                    "    def test_add(self):\n"
                    "        self.assertEqual(add(2, 3), 5)\n"
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
                    "test_calculator",
                ],
            },
            {
                "decision": "done",
                "summary": "Calculator tests pass.",
            },
        )

        result = self._run(
            requester
        )

        self.assertTrue(
            result.succeeded
        )

        self.assertEqual(
            result.status,
            "done",
        )

        self.assertEqual(
            result.workspace_generation,
            2,
        )

        self.assertEqual(
            result.tested_generation,
            2,
        )

        self.assertEqual(
            len(requester.calls),
            4,
        )

        self.assertTrue(
            (
                self.workspace_path
                / "calculator.py"
            ).exists()
        )

        run_step = result.steps[
            2
        ]

        self.assertTrue(
            run_step.result.succeeded
        )

    def test_done_before_test_is_refused_then_recovered(self):
        (
            self.workspace_path
            / "test_ready.py"
        ).write_text(
            (
                "import unittest\n"
                "class ReadyTest(unittest.TestCase):\n"
                "    def test_ready(self):\n"
                "        self.assertTrue(True)\n"
            ),
            encoding="utf-8",
        )

        requester = ScriptedRequester(
            {
                "decision": "done",
                "summary": "looks finished",
            },
            {
                "decision": "action",
                "kind": "RUN",
                "argv": [
                    "/usr/bin/python3",
                    "-m",
                    "unittest",
                    "-v",
                    "test_ready",
                ],
            },
            {
                "decision": "done",
                "summary": "now tested",
            },
        )

        result = self._run(
            requester
        )

        self.assertTrue(
            result.succeeded
        )

        self.assertEqual(
            result.steps[0].decision,
            "done_refused",
        )

        self.assertIn(
            "no successful RUN",
            result.steps[
                0
            ].completion_refused_reason,
        )

    def test_write_after_successful_run_requires_new_run(self):
        (
            self.workspace_path
            / "test_ready.py"
        ).write_text(
            (
                "import unittest\n"
                "class ReadyTest(unittest.TestCase):\n"
                "    def test_ready(self):\n"
                "        self.assertTrue(True)\n"
            ),
            encoding="utf-8",
        )

        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "RUN",
                "argv": [
                    "/usr/bin/python3",
                    "-m",
                    "unittest",
                    "-v",
                    "test_ready",
                ],
            },
            {
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "note.txt",
                "content": "changed\n",
            },
            {
                "decision": "done",
                "summary": "too early",
            },
            {
                "decision": "action",
                "kind": "RUN",
                "argv": [
                    "/usr/bin/python3",
                    "-m",
                    "unittest",
                    "-v",
                    "test_ready",
                ],
            },
            {
                "decision": "done",
                "summary": "tested current generation",
            },
        )

        result = self._run(
            requester
        )

        self.assertTrue(
            result.succeeded
        )

        self.assertEqual(
            result.steps[2].decision,
            "done_refused",
        )

        self.assertEqual(
            result.workspace_generation,
            1,
        )

        self.assertEqual(
            result.tested_generation,
            1,
        )

    def test_failing_run_does_not_satisfy_completion(self):
        (
            self.workspace_path
            / "test_fail.py"
        ).write_text(
            (
                "import unittest\n"
                "class FailTest(unittest.TestCase):\n"
                "    def test_fail(self):\n"
                "        self.fail('expected')\n"
            ),
            encoding="utf-8",
        )

        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "RUN",
                "argv": [
                    "/usr/bin/python3",
                    "-m",
                    "unittest",
                    "-v",
                    "test_fail",
                ],
            },
            {
                "decision": "done",
                "summary": "incorrectly claiming success",
            },
        )

        result = self._run(
            requester,
            max_iterations=2,
        )

        self.assertEqual(
            result.status,
            "iteration_limit",
        )

        self.assertIsNone(
            result.tested_generation
        )

        self.assertEqual(
            result.steps[1].decision,
            "done_refused",
        )

    def test_missing_read_stops_without_model_retry(self):
        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "READ_FILE",
                "path": "missing.txt",
            },
            {
                "decision": "done",
                "summary": "must not be reached",
            },
        )

        result = self._run(
            requester
        )

        self.assertEqual(
            result.status,
            "error",
        )

        self.assertEqual(
            len(requester.calls),
            1,
        )

        self.assertIn(
            "READ_FILE failed closed",
            result.error,
        )

    def test_model_failure_stops_without_retry(self):
        requester = ScriptedRequester(
            LabModelAdapterError(
                "expected model failure"
            ),
            {
                "decision": "done",
                "summary": "must not run",
            },
        )

        result = self._run(
            requester
        )

        self.assertEqual(
            result.status,
            "error",
        )

        self.assertEqual(
            len(requester.calls),
            1,
        )

        self.assertIn(
            "expected model failure",
            result.error,
        )

    def test_iteration_limit_stops(self):
        requester = ScriptedRequester(
            {
                "decision": "done",
                "summary": "not tested",
            },
            {
                "decision": "done",
                "summary": "still not tested",
            },
        )

        result = self._run(
            requester,
            max_iterations=2,
        )

        self.assertEqual(
            result.status,
            "iteration_limit",
        )

        self.assertEqual(
            result.iterations,
            2,
        )

        self.assertEqual(
            len(requester.calls),
            2,
        )

    def test_context_contains_bounded_run_preview(self):
        (
            self.workspace_path
            / "test_output.py"
        ).write_text(
            (
                "import unittest\n"
                "class OutputTest(unittest.TestCase):\n"
                "    def test_output(self):\n"
                "        print('X' * 20000)\n"
                "        self.assertTrue(True)\n"
            ),
            encoding="utf-8",
        )

        requester = ScriptedRequester(
            {
                "decision": "action",
                "kind": "RUN",
                "argv": [
                    "/usr/bin/python3",
                    "-m",
                    "unittest",
                    "-v",
                    "test_output",
                ],
            },
            {
                "decision": "done",
                "summary": "tested",
            },
        )

        result = self._run(
            requester
        )

        self.assertTrue(
            result.succeeded
        )

        second_context = requester.calls[
            1
        ]["context"]

        run_evidence = second_context[
            "recent_steps"
        ][0]["result"]

        stdout = run_evidence[
            "stdout"
        ]

        self.assertTrue(
            stdout["preview_truncated"]
        )

        self.assertGreater(
            stdout["byte_count"],
            CONTEXT_TEXT_PREVIEW_BYTES,
        )

        self.assertLessEqual(
            len(
                stdout["text"].encode(
                    "utf-8"
                )
            ),
            (
                CONTEXT_TEXT_PREVIEW_BYTES
                + 3
            ),
        )

    def test_invalid_limits_fail_before_model(self):
        requester = ScriptedRequester(
            {
                "decision": "done",
                "summary": "unused",
            }
        )

        invalid_cases = (
            {
                "max_iterations": 0,
            },
            {
                "max_iterations": 21,
            },
            {
                "max_iterations": True,
            },
            {
                "run_timeout_seconds": 0,
            },
            {
                "run_timeout_seconds": 31,
            },
            {
                "model_timeout_seconds": 0,
            },
        )

        for case in invalid_cases:
            with self.subTest(
                case=case
            ):
                with self.assertRaises(
                    LabControllerError
                ):
                    run_auto_lab(
                        goal="test",
                        model="test-model",
                        workspace_parent=str(
                            self.parent
                        ),
                        workspace=self.workspace,
                        requester=requester,
                        **case,
                    )

        self.assertEqual(
            requester.calls,
            [],
        )


if __name__ == "__main__":
    unittest.main()
