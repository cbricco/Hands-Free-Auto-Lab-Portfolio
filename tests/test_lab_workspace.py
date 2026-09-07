from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from hands_free_auto_lab.lab_workspace import (
    LabWorkspaceError,
    create_lab_workspace,
    validate_lab_workspace,
)


SESSION_ID = "a" * 64


class LabWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-workspace-tests-",
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

    def tearDown(self):
        shutil.rmtree(
            self.test_root
        )

    def test_create_private_workspace(self):
        workspace = create_lab_workspace(
            str(self.parent),
            session_id=SESSION_ID,
        )

        path = Path(
            workspace.path
        )

        self.assertTrue(
            path.is_dir()
        )

        self.assertEqual(
            stat.S_IMODE(
                path.stat().st_mode
            ),
            0o700,
        )

        self.assertEqual(
            list(path.iterdir()),
            [],
        )

    def test_validate_created_workspace(self):
        workspace = create_lab_workspace(
            str(self.parent),
            session_id=SESSION_ID,
        )

        validated = validate_lab_workspace(
            str(self.parent),
            workspace,
        )

        self.assertEqual(
            validated,
            workspace,
        )

    def test_existing_child_is_refused(self):
        child = (
            self.parent
            / SESSION_ID
        )

        child.mkdir(
            mode=0o700,
        )

        with self.assertRaises(
            LabWorkspaceError
        ):
            create_lab_workspace(
                str(self.parent),
                session_id=SESSION_ID,
            )

    def test_relative_parent_is_refused(self):
        with self.assertRaises(
            LabWorkspaceError
        ):
            create_lab_workspace(
                "workspaces",
                session_id=SESSION_ID,
            )

    def test_noncanonical_parent_is_refused(self):
        with self.assertRaises(
            LabWorkspaceError
        ):
            create_lab_workspace(
                str(self.parent) + "/.",
                session_id=SESSION_ID,
            )

    def test_symlink_parent_is_refused(self):
        link = (
            self.test_root
            / "workspaces-link"
        )

        link.symlink_to(
            self.parent,
            target_is_directory=True,
        )

        with self.assertRaises(
            LabWorkspaceError
        ):
            create_lab_workspace(
                str(link),
                session_id=SESSION_ID,
            )

    def test_nonprivate_parent_mode_is_refused(self):
        os.chmod(
            self.parent,
            0o755,
        )

        with self.assertRaises(
            LabWorkspaceError
        ):
            create_lab_workspace(
                str(self.parent),
                session_id=SESSION_ID,
            )

    def test_uppercase_session_id_is_refused(self):
        with self.assertRaises(
            LabWorkspaceError
        ):
            create_lab_workspace(
                str(self.parent),
                session_id="A" * 64,
            )

    def test_invalid_session_id_is_refused(self):
        with self.assertRaises(
            LabWorkspaceError
        ):
            create_lab_workspace(
                str(self.parent),
                session_id=("a" * 63) + "/",
            )

    def test_workspace_replacement_is_detected(self):
        workspace = create_lab_workspace(
            str(self.parent),
            session_id=SESSION_ID,
        )

        original = Path(
            workspace.path
        )

        moved = (
            self.parent
            / "moved-workspace"
        )

        original.rename(
            moved
        )

        original.mkdir(
            mode=0o700,
        )

        with self.assertRaises(
            LabWorkspaceError
        ):
            validate_lab_workspace(
                str(self.parent),
                workspace,
            )

    def test_workspace_mode_change_is_detected(self):
        workspace = create_lab_workspace(
            str(self.parent),
            session_id=SESSION_ID,
        )

        os.chmod(
            workspace.path,
            0o755,
        )

        with self.assertRaises(
            LabWorkspaceError
        ):
            validate_lab_workspace(
                str(self.parent),
                workspace,
            )

    def test_tampered_identity_is_refused(self):
        workspace = create_lab_workspace(
            str(self.parent),
            session_id=SESSION_ID,
        )

        tampered = replace(
            workspace,
            inode=workspace.inode + 1,
        )

        with self.assertRaises(
            LabWorkspaceError
        ):
            validate_lab_workspace(
                str(self.parent),
                tampered,
            )


if __name__ == "__main__":
    unittest.main()
