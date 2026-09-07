from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from hands_free_auto_lab.lab_acceptance import (
    LabAcceptanceError,
    execute_lab_acceptance,
    seed_lab_acceptance_test,
)
from hands_free_auto_lab.lab_action import (
    build_lab_action,
)
from hands_free_auto_lab.lab_controller import (
    run_auto_lab,
)
from hands_free_auto_lab.lab_model_adapter import (
    LabModelAdapterError,
    LabModelTurn,
    parse_model_decision,
)
from hands_free_auto_lab.lab_policy import (
    PYTHON,
    RESERVED_ACCEPTANCE_PATH,
    RESERVED_ACCEPTANCE_SANDBOX_PATH,
    LabPolicyError,
    evaluate_lab_policy,
)
from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)


SESSION_ID = "a" * 64


ACCEPTANCE_SOURCE = """\
import importlib.util
import sys
import unittest


TARGET = "/workspace/calculator.py"

spec = importlib.util.spec_from_file_location(
    "calculator_under_test",
    TARGET,
)

if spec is None or spec.loader is None:
    raise RuntimeError(
        "cannot load calculator target"
    )

calculator = importlib.util.module_from_spec(
    spec
)

spec.loader.exec_module(
    calculator
)


class AcceptanceTests(unittest.TestCase):
    def test_runner_is_isolated(self):
        self.assertEqual(
            sys.flags.isolated,
            1,
        )
        self.assertNotIn(
            "/workspace",
            sys.path,
        )

    def test_add_cases(self):
        self.assertEqual(
            calculator.add(2, 3),
            5,
        )
        self.assertEqual(
            calculator.add(-2, 2),
            0,
        )

    def test_subtract_cases(self):
        self.assertEqual(
            calculator.subtract(7, 2),
            5,
        )
        self.assertEqual(
            calculator.subtract(-1, 2),
            -3,
        )


if __name__ == "__main__":
    unittest.main()
"""


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


class LabAcceptanceTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-acceptance-tests-"
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

    def _seed_calculator(self):
        (
            self.workspace_path
            / "calculator.py"
        ).write_text(
            (
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "def subtract(a, b):\n"
                "    return a - b\n"
            ),
            encoding="utf-8",
        )

    def test_model_cannot_write_acceptance_file(self):
        action = build_lab_action(
            kind="WRITE_FILE",
            path=RESERVED_ACCEPTANCE_PATH,
            content="raise SystemExit('replace')\n",
        )

        with self.assertRaises(
            LabPolicyError
        ):
            evaluate_lab_policy(
                action
            )

    def test_exact_isolated_runner_allowed(self):
        action = build_lab_action(
            kind="RUN",
            argv=(
                PYTHON,
                "-I",
                RESERVED_ACCEPTANCE_SANDBOX_PATH,
            ),
        )

        self.assertTrue(
            evaluate_lab_policy(
                action
            )["eligible"]
        )

    def test_other_isolated_script_refused(self):
        action = build_lab_action(
            kind="RUN",
            argv=(
                PYTHON,
                "-I",
                "/workspace/worker.py",
            ),
        )

        with self.assertRaises(
            LabPolicyError
        ):
            evaluate_lab_policy(
                action
            )

    def test_acceptance_identity_tamper_refused(self):
        seed = seed_lab_acceptance_test(
            content=ACCEPTANCE_SOURCE,
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
        )

        (
            self.workspace_path
            / RESERVED_ACCEPTANCE_PATH
        ).write_text(
            "tampered = True\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabAcceptanceError,
            "identity changed",
        ):
            execute_lab_acceptance(
                expected_sha256=seed.sha256,
                workspace_parent=str(
                    self.parent
                ),
                workspace=self.workspace,
                timeout_seconds=10,
            )

    def test_workspace_unittest_shadow_is_ignored(self):
        self._seed_calculator()

        (
            self.workspace_path
            / "unittest.py"
        ).write_text(
            "raise RuntimeError('shadow loaded')\n",
            encoding="utf-8",
        )

        seed = seed_lab_acceptance_test(
            content=ACCEPTANCE_SOURCE,
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
        )

        result = execute_lab_acceptance(
            expected_sha256=seed.sha256,
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            timeout_seconds=10,
        )

        self.assertTrue(
            result.succeeded
        )

        self.assertIn(
            "Ran 3 tests",
            result.run_result.stderr,
        )

    def test_weak_worker_test_fails_acceptance_then_recovers(self):
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
                    "\n"
                    "class WorkerTest(unittest.TestCase):\n"
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
                "decision": "action",
                "kind": "WRITE_FILE",
                "path": "calculator.py",
                "content": (
                    "def add(a, b):\n"
                    "    return a + b\n"
                    "\n"
                    "def subtract(a, b):\n"
                    "    return a - b\n"
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
                "summary": "must not be requested",
            },
        )

        result = run_auto_lab(
            goal=(
                "Implement add and subtract and verify both."
            ),
            model="test-model",
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            acceptance_test=ACCEPTANCE_SOURCE,
            max_iterations=6,
            requester=requester,
        )

        self.assertTrue(
            result.succeeded
        )

        self.assertEqual(
            result.status,
            "done",
        )

        self.assertEqual(
            result.iterations,
            5,
        )

        self.assertEqual(
            result.workspace_generation,
            3,
        )

        self.assertEqual(
            result.tested_generation,
            3,
        )

        self.assertEqual(
            result.acceptance_generation,
            3,
        )

        self.assertIsNone(
            result.summary
        )

        self.assertEqual(
            len(requester.calls),
            5,
        )

        self.assertEqual(
            len(requester.items),
            1,
        )

        first_run = result.steps[
            2
        ]

        second_run = result.steps[
            4
        ]

        self.assertEqual(
            first_run.decision,
            "action",
        )

        self.assertIsNotNone(
            first_run.result
        )

        self.assertTrue(
            first_run.result.succeeded
        )

        self.assertIsNotNone(
            first_run.acceptance
        )

        self.assertFalse(
            first_run.acceptance.succeeded
        )

        self.assertEqual(
            second_run.decision,
            "action",
        )

        self.assertIsNotNone(
            second_run.result
        )

        self.assertTrue(
            second_run.result.succeeded
        )

        self.assertIsNotNone(
            second_run.acceptance
        )

        self.assertTrue(
            second_run.acceptance.succeeded
        )


    def test_done_refused_when_worker_test_alone_passes(self):
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
                    "\n"
                    "class WorkerTest(unittest.TestCase):\n"
                    "    def test_add(self):\n"
                    "        self.assertEqual(add(1, 2), 3)\n"
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
                "summary": "Worker test passes.",
            },
        )

        result = run_auto_lab(
            goal="Implement add and subtract.",
            model="test-model",
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            acceptance_test=ACCEPTANCE_SOURCE,
            max_iterations=4,
            requester=requester,
        )

        self.assertFalse(
            result.succeeded
        )

        self.assertEqual(
            result.status,
            "iteration_limit",
        )

        self.assertIsNone(
            result.acceptance_generation
        )

        self.assertEqual(
            result.steps[-1].decision,
            "done_refused",
        )

        self.assertIn(
            "acceptance",
            result.steps[
                -1
            ].completion_refused_reason,
        )


    def test_noop_stall_acceptance_runs_once_per_generation(self):
        write = {
            "decision": "action",
            "kind": "WRITE_FILE",
            "path": "calculator.py",
            "content": (
                "def add(a, b):\n"
                "    return a + b\n"
            ),
        }

        requester = ScriptedRequester(
            write,
            write,
            write,
        )

        from unittest.mock import patch

        with patch(
            "hands_free_auto_lab.lab_controller."
            "execute_lab_acceptance",
            wraps=execute_lab_acceptance,
        ) as acceptance_call:
            result = run_auto_lab(
                goal="Implement add and subtract.",
                model="test-model",
                workspace_parent=str(
                    self.parent
                ),
                workspace=self.workspace,
                acceptance_test=ACCEPTANCE_SOURCE,
                max_iterations=3,
                requester=requester,
            )

        self.assertEqual(
            acceptance_call.call_count,
            1,
        )

        self.assertIsNotNone(
            result.steps[1].acceptance
        )

        self.assertFalse(
            result.steps[1].acceptance.succeeded
        )

        self.assertIs(
            result.steps[1].acceptance,
            result.steps[2].acceptance,
        )

        self.assertIsNone(
            result.tested_generation
        )

        self.assertIsNone(
            result.acceptance_generation
        )

    def test_diagnostic_acceptance_success_does_not_authorize_done(self):
        passing_acceptance = """\
import importlib.util
import unittest


spec = importlib.util.spec_from_file_location(
    "calculator_under_test",
    "/workspace/calculator.py",
)

if spec is None or spec.loader is None:
    raise RuntimeError(
        "cannot load calculator"
    )

calculator = importlib.util.module_from_spec(
    spec
)

spec.loader.exec_module(
    calculator
)


class Acceptance(unittest.TestCase):
    def test_add(self):
        self.assertEqual(
            calculator.add(2, 3),
            5,
        )


if __name__ == "__main__":
    unittest.main()
"""

        write = {
            "decision": "action",
            "kind": "WRITE_FILE",
            "path": "calculator.py",
            "content": (
                "def add(a, b):\n"
                "    return a + b\n"
            ),
        }

        requester = ScriptedRequester(
            write,
            write,
            {
                "decision": "done",
                "summary": "Diagnostic acceptance passed.",
            },
        )

        result = run_auto_lab(
            goal="Implement add.",
            model="test-model",
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            acceptance_test=passing_acceptance,
            max_iterations=3,
            requester=requester,
        )

        diagnostic = result.steps[
            1
        ].acceptance

        self.assertIsNotNone(
            diagnostic
        )

        self.assertTrue(
            diagnostic.succeeded
        )

        self.assertIsNone(
            result.tested_generation
        )

        self.assertIsNone(
            result.acceptance_generation
        )

        self.assertEqual(
            result.steps[-1].decision,
            "done_refused",
        )

        third_context = requester.calls[
            2
        ]["context"]

        self.assertFalse(
            third_context[
                "current_generation_has_successful_run"
            ]
        )

        self.assertFalse(
            third_context[
                "current_generation_has_successful_acceptance"
            ]
        )

        self.assertTrue(
            third_context[
                "latest_acceptance_evidence"
            ][
                "result"
            ][
                "succeeded"
            ]
        )

    def test_noop_diagnostic_acceptance_repeats_after_new_generation(self):
        first = {
            "decision": "action",
            "kind": "WRITE_FILE",
            "path": "calculator.py",
            "content": (
                "def add(a, b):\n"
                "    return a + b\n"
            ),
        }

        second = {
            "decision": "action",
            "kind": "WRITE_FILE",
            "path": "calculator.py",
            "content": (
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "def subtract(a, b):\n"
                "    return a - b\n"
            ),
        }

        requester = ScriptedRequester(
            first,
            first,
            second,
            second,
        )

        from unittest.mock import patch

        with patch(
            "hands_free_auto_lab.lab_controller."
            "execute_lab_acceptance",
            wraps=execute_lab_acceptance,
        ) as acceptance_call:
            result = run_auto_lab(
                goal="Implement add and subtract.",
                model="test-model",
                workspace_parent=str(
                    self.parent
                ),
                workspace=self.workspace,
                acceptance_test=ACCEPTANCE_SOURCE,
                max_iterations=4,
                requester=requester,
            )

        self.assertEqual(
            acceptance_call.call_count,
            2,
        )

        self.assertEqual(
            result.workspace_generation,
            2,
        )

        self.assertIsNotNone(
            result.steps[1].acceptance
        )

        self.assertIsNotNone(
            result.steps[3].acceptance
        )

        self.assertIsNone(
            result.acceptance_generation
        )


    def test_identical_successful_run_is_refused_on_same_generation(self):
        run = {
            "decision": "action",
            "kind": "RUN",
            "argv": [
                "/usr/bin/python3",
                "-m",
                "unittest",
                "test_calculator",
            ],
        }

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
                    "\n"
                    "class WorkerTest(unittest.TestCase):\n"
                    "    def test_add(self):\n"
                    "        self.assertEqual(add(2, 3), 5)\n"
                ),
            },
            run,
            run,
        )

        result = run_auto_lab(
            goal="Implement add and subtract.",
            model="test-model",
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            acceptance_test=ACCEPTANCE_SOURCE,
            max_iterations=4,
            requester=requester,
        )

        self.assertEqual(
            result.steps[2].decision,
            "action",
        )

        self.assertTrue(
            result.steps[2].result.succeeded
        )

        self.assertIsNotNone(
            result.steps[2].acceptance
        )

        self.assertFalse(
            result.steps[2].acceptance.succeeded
        )

        self.assertEqual(
            result.steps[3].decision,
            "action_refused",
        )

        self.assertIsNone(
            result.steps[3].result
        )

        self.assertIn(
            "RUN refused as redundant",
            result.steps[3].error,
        )

        self.assertIs(
            result.steps[2].acceptance,
            result.steps[3].acceptance,
        )

        self.assertEqual(
            result.tested_generation,
            result.workspace_generation,
        )

        self.assertIsNone(
            result.acceptance_generation
        )

    def test_materially_different_run_remains_allowed(self):
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
                    "\n"
                    "class WorkerTest(unittest.TestCase):\n"
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
                    "test_calculator",
                ],
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
        )

        result = run_auto_lab(
            goal="Implement add and subtract.",
            model="test-model",
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            acceptance_test=ACCEPTANCE_SOURCE,
            max_iterations=4,
            requester=requester,
        )

        self.assertEqual(
            result.steps[2].decision,
            "action",
        )

        self.assertEqual(
            result.steps[3].decision,
            "action",
        )

        self.assertTrue(
            result.steps[2].result.succeeded
        )

        self.assertTrue(
            result.steps[3].result.succeeded
        )

        self.assertNotEqual(
            result.steps[2].action.action_id,
            result.steps[3].action.action_id,
        )


    def test_model_context_deduplicates_current_acceptance_evidence(self):
        write = {
            "decision": "action",
            "kind": "WRITE_FILE",
            "path": "calculator.py",
            "content": (
                "def add(a, b):\n"
                "    return a + b\n"
            ),
        }

        requester = ScriptedRequester(
            write,
            write,
            write,
            write,
            write,
        )

        result = run_auto_lab(
            goal="Implement add and subtract.",
            model="test-model",
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            acceptance_test=ACCEPTANCE_SOURCE,
            max_iterations=5,
            requester=requester,
        )

        context = requester.calls[
            -1
        ][
            "context"
        ]

        self.assertLessEqual(
            len(
                context[
                    "recent_steps"
                ]
            ),
            3,
        )

        for step_context in context[
            "recent_steps"
        ]:
            self.assertNotIn(
                "acceptance",
                step_context,
            )

        latest = context[
            "latest_acceptance_evidence"
        ]

        self.assertIsNotNone(
            latest
        )

        self.assertEqual(
            latest[
                "workspace_generation"
            ],
            1,
        )

        self.assertFalse(
            latest[
                "result"
            ][
                "succeeded"
            ]
        )

        serialized = json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        self.assertEqual(
            serialized.count(
                '"kind":"ACCEPTANCE"'
            ),
            1,
        )

        retained_acceptance_steps = [
            step
            for step in result.steps
            if step.acceptance is not None
        ]

        self.assertGreaterEqual(
            len(
                retained_acceptance_steps
            ),
            3,
        )


if __name__ == "__main__":
    unittest.main()
