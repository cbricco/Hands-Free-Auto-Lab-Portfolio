from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from hands_free_auto_lab.lab_action import build_lab_action
from hands_free_auto_lab.lab_write_file import (
    LabWriteFileError,
    execute_lab_write_file,
)
from hands_free_auto_lab.lab_workspace import create_lab_workspace


SESSION_ID = "c" * 64


class LabWriteFileTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-write-tests-"
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

    def _write_action(
        self,
        path: str,
        content: str,
        *,
        cwd: str = ".",
    ):
        return build_lab_action(
            kind="WRITE_FILE",
            path=path,
            cwd=cwd,
            content=content,
        )

    def _execute(
        self,
        action,
    ):
        return execute_lab_write_file(
            action,
            workspace_parent=str(self.parent),
            workspace=self.workspace,
        )

    def test_new_file_is_written_exactly(self):
        action = self._write_action(
            "hello.txt",
            "hello auto lab\n",
        )

        result = self._execute(
            action
        )

        self.assertTrue(
            result.succeeded
        )
        self.assertFalse(
            result.replaced_existing
        )
        self.assertEqual(
            result.path,
            "hello.txt",
        )
        self.assertEqual(
            result.bytes_written,
            len(
                "hello auto lab\n".encode(
                    "utf-8"
                )
            ),
        )
        self.assertEqual(
            result.sha256,
            hashlib.sha256(
                b"hello auto lab\n"
            ).hexdigest(),
        )
        self.assertEqual(
            (
                self.workspace_path
                / "hello.txt"
            ).read_text(
                encoding="utf-8"
            ),
            "hello auto lab\n",
        )

    def test_existing_regular_file_is_atomically_replaced(self):
        target = (
            self.workspace_path
            / "replace.txt"
        )

        target.write_text(
            "old\n",
            encoding="utf-8",
        )

        old_inode = target.stat().st_ino

        action = self._write_action(
            "replace.txt",
            "new\n",
        )

        result = self._execute(
            action
        )

        self.assertTrue(
            result.replaced_existing
        )
        self.assertEqual(
            target.read_text(
                encoding="utf-8"
            ),
            "new\n",
        )
        self.assertNotEqual(
            target.stat().st_ino,
            old_inode,
        )

        temporary = (
            self.workspace_path
            / (
                ".hands-free-auto-lab-write-"
                f"{action.action_id}.tmp"
            )
        )

        self.assertFalse(
            temporary.exists()
        )

    def test_nested_real_directory_is_supported(self):
        nested = (
            self.workspace_path
            / "src"
            / "package"
        )

        nested.mkdir(
            parents=True
        )

        action = self._write_action(
            "src/package/example.py",
            "value = 7\n",
        )

        result = self._execute(
            action
        )

        self.assertTrue(
            result.succeeded
        )
        self.assertEqual(
            (
                nested
                / "example.py"
            ).read_text(
                encoding="utf-8"
            ),
            "value = 7\n",
        )

    def test_missing_parent_directory_is_refused(self):
        action = self._write_action(
            "missing/example.txt",
            "no\n",
        )

        with self.assertRaises(
            LabWriteFileError
        ):
            self._execute(
                action
            )

        self.assertFalse(
            (
                self.workspace_path
                / "missing"
            ).exists()
        )

    def test_symlink_parent_is_refused(self):
        outside = (
            self.test_root
            / "outside"
        )

        outside.mkdir()

        (
            self.workspace_path
            / "link"
        ).symlink_to(
            outside,
            target_is_directory=True,
        )

        action = self._write_action(
            "link/escape.txt",
            "must not escape\n",
        )

        with self.assertRaises(
            LabWriteFileError
        ):
            self._execute(
                action
            )

        self.assertFalse(
            (
                outside
                / "escape.txt"
            ).exists()
        )

    def test_symlink_destination_is_refused(self):
        outside = (
            self.test_root
            / "outside.txt"
        )

        outside.write_text(
            "outside original\n",
            encoding="utf-8",
        )

        target = (
            self.workspace_path
            / "target.txt"
        )

        target.symlink_to(
            outside
        )

        action = self._write_action(
            "target.txt",
            "must not escape\n",
        )

        with self.assertRaises(
            LabWriteFileError
        ):
            self._execute(
                action
            )

        self.assertEqual(
            outside.read_text(
                encoding="utf-8"
            ),
            "outside original\n",
        )

        self.assertTrue(
            target.is_symlink()
        )

    def test_directory_destination_is_refused(self):
        (
            self.workspace_path
            / "target"
        ).mkdir()

        action = self._write_action(
            "target",
            "not a directory replacement\n",
        )

        with self.assertRaises(
            LabWriteFileError
        ):
            self._execute(
                action
            )

        self.assertTrue(
            (
                self.workspace_path
                / "target"
            ).is_dir()
        )

    def test_non_root_cwd_is_refused_initially(self):
        action = self._write_action(
            "example.txt",
            "no\n",
            cwd="src",
        )

        with self.assertRaises(
            LabWriteFileError
        ):
            self._execute(
                action
            )

        self.assertFalse(
            (
                self.workspace_path
                / "example.txt"
            ).exists()
        )

    def test_non_write_actions_are_refused(self):
        actions = (
            build_lab_action(
                kind="READ_FILE",
                path="example.txt",
            ),
            build_lab_action(
                kind="RUN",
                argv=(
                    "/usr/bin/python3",
                    "-m",
                    "unittest",
                ),
            ),
        )

        for action in actions:
            with self.subTest(
                kind=action.kind
            ):
                with self.assertRaises(
                    LabWriteFileError
                ):
                    self._execute(
                        action
                    )

    def test_workspace_identity_tampering_is_refused(self):
        action = self._write_action(
            "example.txt",
            "no\n",
        )

        tampered = replace(
            self.workspace,
            inode=self.workspace.inode + 1,
        )

        with self.assertRaises(
            LabWriteFileError
        ):
            execute_lab_write_file(
                action,
                workspace_parent=str(self.parent),
                workspace=tampered,
            )

        self.assertFalse(
            (
                self.workspace_path
                / "example.txt"
            ).exists()
        )

    def test_known_temporary_collision_fails_closed(self):
        action = self._write_action(
            "target.txt",
            "new\n",
        )

        target = (
            self.workspace_path
            / "target.txt"
        )

        target.write_text(
            "old\n",
            encoding="utf-8",
        )

        temporary = (
            self.workspace_path
            / (
                ".hands-free-auto-lab-write-"
                f"{action.action_id}.tmp"
            )
        )

        temporary.write_text(
            "unexpected temporary state\n",
            encoding="utf-8",
        )

        with self.assertRaises(
            LabWriteFileError
        ):
            self._execute(
                action
            )

        self.assertEqual(
            target.read_text(
                encoding="utf-8"
            ),
            "old\n",
        )

        self.assertEqual(
            temporary.read_text(
                encoding="utf-8"
            ),
            "unexpected temporary state\n",
        )


if __name__ == "__main__":
    unittest.main()
