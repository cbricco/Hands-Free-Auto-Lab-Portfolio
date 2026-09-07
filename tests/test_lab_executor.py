from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from hands_free_auto_lab.lab_action import build_lab_action
from hands_free_auto_lab.lab_executor import (
    LabExecutorError,
    OUTPUT_LIMIT_BYTES,
    execute_lab_run,
)
from hands_free_auto_lab.lab_workspace import create_lab_workspace


SESSION_ID = "b" * 64


class LabExecutorTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-executor-tests-"
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

    def _write_module(
        self,
        name: str,
        source: str,
    ) -> None:
        (
            self.workspace_path
            / f"{name}.py"
        ).write_text(
            source,
            encoding="utf-8",
        )

    def _run_module(
        self,
        name: str,
        *,
        timeout_seconds: int = 10,
    ):
        action = build_lab_action(
            kind="RUN",
            argv=(
                "/usr/bin/python3",
                "-m",
                "unittest",
                "-v",
                name,
            ),
        )

        return execute_lab_run(
            action,
            workspace_parent=str(self.parent),
            workspace=self.workspace,
            timeout_seconds=timeout_seconds,
        )

    def test_passing_unittest_executes_inside_sandbox(self):
        self._write_module(
            "test_pass",
            (
                "import unittest\n"
                "class PassTest(unittest.TestCase):\n"
                "    def test_pass(self):\n"
                "        self.assertEqual(2 + 2, 4)\n"
            ),
        )

        result = self._run_module(
            "test_pass"
        )

        self.assertTrue(
            result.succeeded
        )
        self.assertFalse(
            result.timed_out
        )
        self.assertFalse(
            result.output_limit_exceeded
        )
        self.assertEqual(
            result.exit_status,
            0,
        )
        self.assertIsNone(
            result.signal
        )
        self.assertIn(
            "OK",
            result.stderr,
        )

    def test_failing_unittest_returns_failure_evidence(self):
        self._write_module(
            "test_fail",
            (
                "import unittest\n"
                "class FailTest(unittest.TestCase):\n"
                "    def test_fail(self):\n"
                "        self.fail('EXPECTED_AUTO_LAB_FAILURE')\n"
            ),
        )

        result = self._run_module(
            "test_fail"
        )

        self.assertFalse(
            result.succeeded
        )
        self.assertFalse(
            result.timed_out
        )
        self.assertFalse(
            result.output_limit_exceeded
        )
        self.assertEqual(
            result.exit_status,
            1,
        )
        self.assertIsNone(
            result.signal
        )
        self.assertIn(
            "EXPECTED_AUTO_LAB_FAILURE",
            result.stderr,
        )

    def test_run_cannot_write_persistent_workspace(self):
        self._write_module(
            "test_marker",
            (
                "from pathlib import Path\n"
                "import unittest\n"
                "class MarkerTest(unittest.TestCase):\n"
                "    def test_marker(self):\n"
                "        Path('marker.txt').write_text(\n"
                "            'must-not-persist\\n', encoding='utf-8'\n"
                "        )\n"
            ),
        )

        result = self._run_module(
            "test_marker"
        )

        self.assertFalse(
            result.succeeded
        )

        self.assertFalse(
            (
                self.workspace_path
                / "marker.txt"
            ).exists()
        )

    def test_run_can_use_disposable_tmp(self):
        self._write_module(
            "test_tmp",
            (
                "from pathlib import Path\n"
                "import unittest\n"
                "class TmpTest(unittest.TestCase):\n"
                "    def test_tmp(self):\n"
                "        target = Path('/tmp/example.txt')\n"
                "        target.write_text(\n"
                "            'temporary\\n', encoding='utf-8'\n"
                "        )\n"
                "        self.assertEqual(\n"
                "            target.read_text(encoding='utf-8'),\n"
                "            'temporary\\n',\n"
                "        )\n"
            ),
        )

        result = self._run_module(
            "test_tmp"
        )

        self.assertTrue(
            result.succeeded
        )

    def test_host_paths_are_invisible(self):
        self._write_module(
            "test_visibility",
            (
                "from pathlib import Path\n"
                "import unittest\n"
                "class VisibilityTest(unittest.TestCase):\n"
                "    def test_hidden(self):\n"
                "        for value in (\n"
                "            '/etc/passwd',\n"
                "            '/home/testuser',\n"
                "            '/root',\n"
                "            '/mnt',\n"
                "        ):\n"
                "            self.assertFalse(\n"
                "                Path(value).exists(), value\n"
                "            )\n"
            ),
        )

        self.assertTrue(
            self._run_module(
                "test_visibility"
            ).succeeded
        )

    def test_nested_user_namespace_is_refused(self):
        self._write_module(
            "test_user_namespace",
            (
                "import subprocess\n"
                "import unittest\n"
                "class UserNamespaceTest(unittest.TestCase):\n"
                "    def test_nested_user_namespace(self):\n"
                "        result = subprocess.run(\n"
                "            (\n"
                "                '/usr/bin/unshare',\n"
                "                '--user',\n"
                "                '--map-root-user',\n"
                "                '/usr/bin/true',\n"
                "            ),\n"
                "            stdout=subprocess.PIPE,\n"
                "            stderr=subprocess.PIPE,\n"
                "            check=False,\n"
                "        )\n"
                "        self.assertNotEqual(\n"
                "            result.returncode,\n"
                "            0,\n"
                "            result.stderr.decode(\n"
                "                'utf-8', errors='replace'\n"
                "            ),\n"
                "        )\n"
            ),
        )

        result = self._run_module(
            "test_user_namespace"
        )

        self.assertTrue(
            result.succeeded
        )

    def test_write_file_is_refused_by_run_executor(self):
        action = build_lab_action(
            kind="WRITE_FILE",
            path="marker.txt",
            content="not through RUN\n",
        )

        with self.assertRaises(
            LabExecutorError
        ):
            execute_lab_run(
                action,
                workspace_parent=str(self.parent),
                workspace=self.workspace,
            )

        self.assertFalse(
            (
                self.workspace_path
                / "marker.txt"
            ).exists()
        )

    def test_non_root_cwd_is_refused_initially(self):
        action = build_lab_action(
            kind="RUN",
            cwd="src",
            argv=(
                "/usr/bin/python3",
                "-m",
                "unittest",
            ),
        )

        with self.assertRaises(
            LabExecutorError
        ):
            execute_lab_run(
                action,
                workspace_parent=str(self.parent),
                workspace=self.workspace,
            )

    def test_invalid_timeout_is_refused(self):
        action = build_lab_action(
            kind="RUN",
            argv=(
                "/usr/bin/python3",
                "-m",
                "unittest",
            ),
        )

        for value in (
            0,
            31,
            True,
            1.5,
        ):
            with self.subTest(
                value=value
            ):
                with self.assertRaises(
                    LabExecutorError
                ):
                    execute_lab_run(
                        action,
                        workspace_parent=str(self.parent),
                        workspace=self.workspace,
                        timeout_seconds=value,
                    )

    def test_timeout_is_recorded_without_retry(self):
        self._write_module(
            "test_timeout",
            (
                "import time\n"
                "import unittest\n"
                "class TimeoutTest(unittest.TestCase):\n"
                "    def test_timeout(self):\n"
                "        time.sleep(5)\n"
            ),
        )

        result = self._run_module(
            "test_timeout",
            timeout_seconds=1,
        )

        self.assertFalse(
            result.succeeded
        )
        self.assertTrue(
            result.timed_out
        )
        self.assertFalse(
            result.output_limit_exceeded
        )
        self.assertIsNone(
            result.exit_status
        )
        self.assertEqual(
            result.signal,
            9,
        )

    def test_stdout_is_bounded(self):
        self._write_module(
            "test_stdout_limit",
            (
                "import unittest\n"
                "class OutputTest(unittest.TestCase):\n"
                "    def test_output(self):\n"
                "        for _ in range(400):\n"
                "            print('X' * 4096)\n"
            ),
        )

        result = self._run_module(
            "test_stdout_limit"
        )

        self.assertFalse(
            result.succeeded
        )
        self.assertFalse(
            result.timed_out
        )
        self.assertTrue(
            result.output_limit_exceeded
        )
        self.assertTrue(
            result.stdout_truncated
        )
        self.assertFalse(
            result.stderr_truncated
        )
        self.assertLessEqual(
            len(
                result.stdout.encode(
                    "utf-8"
                )
            ),
            OUTPUT_LIMIT_BYTES,
        )

    def test_stderr_is_bounded(self):
        self._write_module(
            "test_stderr_limit",
            (
                "import sys\n"
                "import unittest\n"
                "class ErrorOutputTest(unittest.TestCase):\n"
                "    def test_output(self):\n"
                "        for _ in range(400):\n"
                "            print('E' * 4096, file=sys.stderr)\n"
            ),
        )

        result = self._run_module(
            "test_stderr_limit"
        )

        self.assertFalse(
            result.succeeded
        )
        self.assertFalse(
            result.timed_out
        )
        self.assertTrue(
            result.output_limit_exceeded
        )
        self.assertFalse(
            result.stdout_truncated
        )
        self.assertTrue(
            result.stderr_truncated
        )
        self.assertLessEqual(
            len(
                result.stderr.encode(
                    "utf-8"
                )
            ),
            OUTPUT_LIMIT_BYTES,
        )


if __name__ == "__main__":
    unittest.main()
