from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import hands_free_auto_lab.lab_workspace_snapshot as snapshot_module

from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)
from hands_free_auto_lab.lab_workspace_snapshot import (
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    ENTRY_DIRECTORY,
    ENTRY_FILE,
    LabWorkspaceSnapshotError,
    capture_lab_workspace_snapshot,
    diff_lab_workspace_snapshots,
    validate_lab_workspace_snapshot,
)


SESSION_ID = "8" * 64


class LabWorkspaceSnapshotTests(
    unittest.TestCase
):
    def setUp(
        self,
    ):
        self.test_root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-snapshot-tests-"
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
            mode=0o700
        )

        self.workspace = create_lab_workspace(
            str(
                self.parent
            ),
            session_id=SESSION_ID,
        )

        self.root = Path(
            self.workspace.path
        )

    def tearDown(
        self,
    ):
        shutil.rmtree(
            self.test_root
        )

    def snapshot(
        self,
        **kwargs,
    ):
        return capture_lab_workspace_snapshot(
            workspace_parent=str(
                self.parent
            ),
            workspace=self.workspace,
            **kwargs,
        )

    def test_snapshot_is_deterministic_and_records_binary_files(
        self,
    ):
        src = (
            self.root
            / "src"
        )

        src.mkdir()

        empty = (
            self.root
            / "empty"
        )

        empty.mkdir()

        binary = (
            src
            / "payload.bin"
        )

        binary.write_bytes(
            b"\x00\xffABC\n"
        )

        first = self.snapshot()
        second = self.snapshot()

        self.assertEqual(
            first,
            second,
        )

        self.assertEqual(
            len(
                first.snapshot_id
            ),
            64,
        )

        by_path = {
            item.path: item
            for item in first.entries
        }

        self.assertEqual(
            by_path["src"].kind,
            ENTRY_DIRECTORY,
        )

        self.assertEqual(
            by_path["empty"].kind,
            ENTRY_DIRECTORY,
        )

        item = by_path[
            "src/payload.bin"
        ]

        self.assertEqual(
            item.kind,
            ENTRY_FILE,
        )

        self.assertEqual(
            item.bytes,
            len(
                b"\x00\xffABC\n"
            ),
        )

        self.assertEqual(
            item.sha256,
            hashlib.sha256(
                b"\x00\xffABC\n"
            ).hexdigest(),
        )

    def test_diff_detects_added_modified_deleted_and_mode_change(
        self,
    ):
        (
            self.root
            / "modified.txt"
        ).write_text(
            "before\n",
            encoding="utf-8",
        )

        (
            self.root
            / "deleted.txt"
        ).write_text(
            "delete me\n",
            encoding="utf-8",
        )

        mode_file = (
            self.root
            / "mode.txt"
        )

        mode_file.write_text(
            "same content\n",
            encoding="utf-8",
        )

        os.chmod(
            mode_file,
            0o600,
        )

        before = self.snapshot()

        (
            self.root
            / "modified.txt"
        ).write_text(
            "after\n",
            encoding="utf-8",
        )

        (
            self.root
            / "deleted.txt"
        ).unlink()

        (
            self.root
            / "added.txt"
        ).write_text(
            "new\n",
            encoding="utf-8",
        )

        os.chmod(
            mode_file,
            0o644,
        )

        after = self.snapshot()

        diff = diff_lab_workspace_snapshots(
            before,
            after,
        )

        changes = {
            item.path: item.change
            for item in diff.entries
        }

        self.assertEqual(
            changes,
            {
                "added.txt": CHANGE_ADDED,
                "deleted.txt": CHANGE_DELETED,
                "mode.txt": CHANGE_MODIFIED,
                "modified.txt": CHANGE_MODIFIED,
            },
        )

    def test_unchanged_workspace_has_empty_diff(
        self,
    ):
        (
            self.root
            / "stable.txt"
        ).write_text(
            "stable\n",
            encoding="utf-8",
        )

        before = self.snapshot()
        after = self.snapshot()

        diff = diff_lab_workspace_snapshots(
            before,
            after,
        )

        self.assertEqual(
            diff.entries,
            (),
        )

    def test_symlink_is_refused(
        self,
    ):
        outside = (
            self.test_root
            / "outside.txt"
        )

        outside.write_text(
            "outside\n",
            encoding="utf-8",
        )

        (
            self.root
            / "link"
        ).symlink_to(
            outside
        )

        with self.assertRaisesRegex(
            LabWorkspaceSnapshotError,
            "unsupported entry type",
        ):
            self.snapshot()

    def test_hardlinked_regular_file_is_refused(
        self,
    ):
        first = (
            self.root
            / "first.txt"
        )

        second = (
            self.root
            / "second.txt"
        )

        first.write_text(
            "linked\n",
            encoding="utf-8",
        )

        os.link(
            first,
            second,
        )

        with self.assertRaisesRegex(
            LabWorkspaceSnapshotError,
            "hard link",
        ):
            self.snapshot()

    def test_fifo_is_refused(
        self,
    ):
        fifo = (
            self.root
            / "pipe"
        )

        os.mkfifo(
            fifo
        )

        with self.assertRaisesRegex(
            LabWorkspaceSnapshotError,
            "unsupported entry type",
        ):
            self.snapshot()

    def test_file_size_limit_is_enforced(
        self,
    ):
        (
            self.root
            / "large.bin"
        ).write_bytes(
            b"12345"
        )

        with self.assertRaisesRegex(
            LabWorkspaceSnapshotError,
            "file-size limit",
        ):
            self.snapshot(
                max_file_bytes=4,
            )

    def test_entry_count_limit_is_enforced(
        self,
    ):
        (
            self.root
            / "one.txt"
        ).write_text(
            "one\n",
            encoding="utf-8",
        )

        (
            self.root
            / "two.txt"
        ).write_text(
            "two\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabWorkspaceSnapshotError,
            "entry-count limit",
        ):
            self.snapshot(
                max_entries=1,
            )

    def test_workspace_identity_tampering_is_refused(
        self,
    ):
        tampered = replace(
            self.workspace,
            inode=(
                self.workspace.inode
                + 1
            ),
        )

        with self.assertRaisesRegex(
            LabWorkspaceSnapshotError,
            "workspace validation failed",
        ):
            capture_lab_workspace_snapshot(
                workspace_parent=str(
                    self.parent
                ),
                workspace=tampered,
            )

    def test_snapshot_identity_tampering_is_refused(
        self,
    ):
        (
            self.root
            / "file.txt"
        ).write_text(
            "text\n",
            encoding="utf-8",
        )

        snapshot = self.snapshot()

        tampered = replace(
            snapshot,
            snapshot_id=(
                "0"
                * 64
            ),
        )

        with self.assertRaisesRegex(
            LabWorkspaceSnapshotError,
            "identity mismatch",
        ):
            validate_lab_workspace_snapshot(
                tampered
            )

    def test_diff_refuses_different_workspace_identity(
        self,
    ):
        first = self.snapshot()

        other_parent = (
            self.test_root
            / "other-workspaces"
        )

        other_parent.mkdir(
            mode=0o700
        )

        other_workspace = create_lab_workspace(
            str(
                other_parent
            ),
            session_id=(
                "9"
                * 64
            ),
        )

        second = capture_lab_workspace_snapshot(
            workspace_parent=str(
                other_parent
            ),
            workspace=other_workspace,
        )

        with self.assertRaisesRegex(
            LabWorkspaceSnapshotError,
            "different physical workspaces",
        ):
            diff_lab_workspace_snapshots(
                first,
                second,
            )

    def test_change_during_file_hash_is_refused(
        self,
    ):
        target = (
            self.root
            / "changing.txt"
        )

        target.write_bytes(
            b"A"
            * 100
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

            if (
                chunk
                and not changed
            ):
                changed = True

                target.write_bytes(
                    b"B"
                    * 100
                )

            return chunk

        with mock.patch.object(
            snapshot_module.os,
            "read",
            side_effect=mutating_read,
        ):
            with self.assertRaisesRegex(
                LabWorkspaceSnapshotError,
                "changed while being hashed",
            ):
                self.snapshot()

    def test_snapshot_module_has_no_execution_network_or_authority_imports(
        self,
    ):
        source = Path(
            snapshot_module.__file__
        ).read_text(
            encoding="utf-8"
        )

        tree = ast.parse(
            source
        )

        imported_roots = set()

        for node in ast.walk(
            tree
        ):
            if isinstance(
                node,
                ast.Import,
            ):
                for alias in node.names:
                    imported_roots.add(
                        alias.name.split(
                            "."
                        )[0]
                    )

            elif isinstance(
                node,
                ast.ImportFrom,
            ):
                if node.module:
                    imported_roots.add(
                        node.module
                    )

        allowed = {
            "__future__",
            "dataclasses",
            "hashlib",
            "json",
            "os",
            "pathlib",
            "stat",
            "lab_workspace",
        }

        unexpected = {
            item
            for item in imported_roots
            if item not in allowed
        }

        self.assertEqual(
            unexpected,
            set(),
        )

        for token in (
            "subprocess.",
            "Popen(",
            "socket.",
            "urlopen(",
            "requests.",
            "docker",
            "consume_promotion_approval(",
            "execute_lab_run(",
        ):
            self.assertNotIn(
                token,
                source,
            )


if __name__ == "__main__":
    unittest.main()
