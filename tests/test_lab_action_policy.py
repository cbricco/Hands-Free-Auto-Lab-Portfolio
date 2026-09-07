from __future__ import annotations

from dataclasses import replace
import unittest

from hands_free_auto_lab.lab_action import (
    LabActionError,
    build_lab_action,
    validate_lab_action,
)
from hands_free_auto_lab.lab_policy import (
    LabPolicyError,
    evaluate_lab_policy,
)


class LabActionTests(unittest.TestCase):
    def test_read_is_deterministic(self):
        first = build_lab_action(
            kind="READ_FILE",
            path="src/example.py",
        )
        second = build_lab_action(
            kind="READ_FILE",
            path="src/example.py",
        )

        self.assertEqual(first, second)

    def test_write_accepts_content(self):
        action = build_lab_action(
            kind="WRITE_FILE",
            path="src/example.py",
            content="value = 1\n",
        )

        validate_lab_action(action)

    def test_run_accepts_argv(self):
        action = build_lab_action(
            kind="RUN",
            argv=(
                "/usr/bin/python3",
                "-m",
                "unittest",
            ),
        )

        validate_lab_action(action)

    def test_unknown_kind_refused(self):
        with self.assertRaises(LabActionError):
            build_lab_action(
                kind="DELETE_HOST",
            )

    def test_write_requires_content(self):
        with self.assertRaises(LabActionError):
            build_lab_action(
                kind="WRITE_FILE",
                path="README.md",
            )

    def test_run_requires_argv(self):
        with self.assertRaises(LabActionError):
            build_lab_action(
                kind="RUN",
            )

    def test_tampered_identity_refused(self):
        action = build_lab_action(
            kind="READ_FILE",
            path="README.md",
        )

        tampered = replace(
            action,
            action_id="0" * 64,
        )

        with self.assertRaises(LabActionError):
            validate_lab_action(tampered)


class LabPolicyTests(unittest.TestCase):
    def test_relative_read_allowed(self):
        action = build_lab_action(
            kind="READ_FILE",
            path="src/example.py",
        )

        self.assertTrue(
            evaluate_lab_policy(action)["eligible"]
        )

    def test_relative_write_allowed(self):
        action = build_lab_action(
            kind="WRITE_FILE",
            path="src/example.py",
            content="value = 2\n",
        )

        self.assertTrue(
            evaluate_lab_policy(action)["eligible"]
        )

    def test_absolute_path_refused(self):
        action = build_lab_action(
            kind="READ_FILE",
            path="/etc/passwd",
        )

        with self.assertRaises(LabPolicyError):
            evaluate_lab_policy(action)

    def test_parent_path_refused(self):
        action = build_lab_action(
            kind="WRITE_FILE",
            path="../outside.txt",
            content="no\n",
        )

        with self.assertRaises(LabPolicyError):
            evaluate_lab_policy(action)

    def test_noncanonical_path_refused(self):
        action = build_lab_action(
            kind="READ_FILE",
            path="src/../README.md",
        )

        with self.assertRaises(LabPolicyError):
            evaluate_lab_policy(action)

    def test_parent_cwd_refused(self):
        action = build_lab_action(
            kind="RUN",
            cwd="..",
            argv=(
                "/usr/bin/python3",
                "-m",
                "unittest",
            ),
        )

        with self.assertRaises(LabPolicyError):
            evaluate_lab_policy(action)

    def test_bash_refused(self):
        action = build_lab_action(
            kind="RUN",
            argv=(
                "/usr/bin/bash",
                "-c",
                "echo hi",
            ),
        )

        with self.assertRaises(LabPolicyError):
            evaluate_lab_policy(action)

    def test_python_dash_c_refused(self):
        action = build_lab_action(
            kind="RUN",
            argv=(
                "/usr/bin/python3",
                "-c",
                "print('hello')",
            ),
        )

        with self.assertRaises(LabPolicyError):
            evaluate_lab_policy(action)

    def test_pip_refused(self):
        action = build_lab_action(
            kind="RUN",
            argv=(
                "/usr/bin/python3",
                "-m",
                "pip",
                "install",
                "example",
            ),
        )

        with self.assertRaises(LabPolicyError):
            evaluate_lab_policy(action)

    def test_unittest_allowed(self):
        action = build_lab_action(
            kind="RUN",
            argv=(
                "/usr/bin/python3",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ),
        )

        decision = evaluate_lab_policy(action)

        self.assertEqual(
            decision["kind"],
            "RUN",
        )


if __name__ == "__main__":
    unittest.main()
