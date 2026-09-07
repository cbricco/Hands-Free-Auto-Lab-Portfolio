from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import hands_free_auto_lab.lab_read_file as read_module
from hands_free_auto_lab.lab_action import build_lab_action
from hands_free_auto_lab.lab_read_file import (
    LabReadFileError,
    READ_LIMIT_BYTES,
    execute_lab_read_file,
)
from hands_free_auto_lab.lab_workspace import create_lab_workspace


SESSION_ID = "d" * 64


class LabReadFileTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-read-tests-"
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

    def _action(
        self,
        path: str,
        *,
        cwd: str = ".",
    ):
        return build_lab_action(
            kind="READ_FILE",
            path=path,
            cwd=cwd,
        )

    def _execute(
        self,
        action,
    ):
        return execute_lab_read_file(
            action,
            workspace_parent=str(self.parent),
            workspace=self.workspace,
        )

    def test_regular_utf8_file_is_read_exactly(self):
        target = (
            self.workspace_path
            / "example.txt"
        )

        target.write_text(
            "hello read evidence\n",
            encoding="utf-8",
        )

        action = self._action(
            "example.txt"
        )

        result = self._execute(
            action
        )

        self.assertTrue(
            result.succeeded
        )
        self.assertEqual(
            result.content,
            "hello read evidence\n",
        )
        self.assertEqual(
            result.bytes_read,
            len(
                b"hello read evidence\n"
            ),
        )
        self.assertEqual(
            result.sha256,
            hashlib.sha256(
                b"hello read evidence\n"
            ).hexdigest(),
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

        (
            nested
            / "module.py"
        ).write_text(
            "value = 11\n",
            encoding="utf-8",
        )

        result = self._execute(
            self._action(
                "src/package/module.py"
            )
        )

        self.assertEqual(
            result.content,
            "value = 11\n",
        )

    def test_missing_parent_is_refused(self):
        with self.assertRaises(
            LabReadFileError
        ):
            self._execute(
                self._action(
                    "missing/file.txt"
                )
            )

    def test_missing_file_is_refused(self):
        with self.assertRaises(
            LabReadFileError
        ):
            self._execute(
                self._action(
                    "missing.txt"
                )
            )

    def test_symlink_parent_is_refused(self):
        outside = (
            self.test_root
            / "outside"
        )

        outside.mkdir()

        (
            outside
            / "secret.txt"
        ).write_text(
            "outside\n",
            encoding="utf-8",
        )

        (
            self.workspace_path
            / "link"
        ).symlink_to(
            outside,
            target_is_directory=True,
        )

        with self.assertRaises(
            LabReadFileError
        ):
            self._execute(
                self._action(
                    "link/secret.txt"
                )
            )

    def test_symlink_target_is_refused(self):
        outside = (
            self.test_root
            / "outside.txt"
        )

        outside.write_text(
            "outside\n",
            encoding="utf-8",
        )

        (
            self.workspace_path
            / "target.txt"
        ).symlink_to(
            outside
        )

        with self.assertRaises(
            LabReadFileError
        ):
            self._execute(
                self._action(
                    "target.txt"
                )
            )

    def test_directory_target_is_refused(self):
        (
            self.workspace_path
            / "directory"
        ).mkdir()

        with self.assertRaises(
            LabReadFileError
        ):
            self._execute(
                self._action(
                    "directory"
                )
            )

    def test_oversized_file_is_refused(self):
        (
            self.workspace_path
            / "large.txt"
        ).write_bytes(
            b"A"
            * (
                READ_LIMIT_BYTES
                + 1
            )
        )

        with self.assertRaises(
            LabReadFileError
        ):
            self._execute(
                self._action(
                    "large.txt"
                )
            )

    def test_invalid_utf8_is_refused(self):
        (
            self.workspace_path
            / "binary.dat"
        ).write_bytes(
            b"\xff\xfe\xfd"
        )

        with self.assertRaises(
            LabReadFileError
        ):
            self._execute(
                self._action(
                    "binary.dat"
                )
            )

    def test_non_root_cwd_is_refused_initially(self):
        (
            self.workspace_path
            / "example.txt"
        ).write_text(
            "text\n",
            encoding="utf-8",
        )

        with self.assertRaises(
            LabReadFileError
        ):
            self._execute(
                self._action(
                    "example.txt",
                    cwd="src",
                )
            )

    def test_non_read_actions_are_refused(self):
        actions = (
            build_lab_action(
                kind="WRITE_FILE",
                path="example.txt",
                content="no\n",
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
                    LabReadFileError
                ):
                    self._execute(
                        action
                    )

    def test_workspace_identity_tampering_is_refused(self):
        (
            self.workspace_path
            / "example.txt"
        ).write_text(
            "text\n",
            encoding="utf-8",
        )

        tampered = replace(
            self.workspace,
            inode=self.workspace.inode + 1,
        )

        with self.assertRaises(
            LabReadFileError
        ):
            execute_lab_read_file(
                self._action(
                    "example.txt"
                ),
                workspace_parent=str(self.parent),
                workspace=tampered,
            )

    def test_file_change_during_read_is_refused(self):
        target = (
            self.workspace_path
            / "changing.txt"
        )

        target.write_text(
            "A" * 100,
            encoding="utf-8",
        )

        real_read = os.read
        changed = False

        def mutating_read(
            fd,
            size,
        ):
            nonlocal changed

            chunk = real_read(
                fd,
                size,
            )

            if chunk and not changed:
                changed = True

                target.write_text(
                    "B" * 100,
                    encoding="utf-8",
                )

            return chunk

        with mock.patch.object(
            read_module.os,
            "read",
            side_effect=mutating_read,
        ):
            with self.assertRaises(
                LabReadFileError
            ):
                self._execute(
                    self._action(
                        "changing.txt"
                    )
                )


if __name__ == "__main__":
    unittest.main()
