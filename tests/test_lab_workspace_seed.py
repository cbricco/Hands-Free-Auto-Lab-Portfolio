import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import hands_free_auto_lab.lab_workspace_seed as seed_module

from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)

from hands_free_auto_lab.lab_workspace_seed import (
    WORKSPACE_SEED_COMPONENT,
    WORKSPACE_SEED_SCHEMA_VERSION,
    LabWorkspaceSeedError,
    seed_lab_workspace,
)


SESSION_ID = "c" * 64


class LabWorkspaceSeedTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-workspace-seed-tests-"
            )
        )

        self.source = (
            self.root
            / "source"
        )
        self.source.mkdir(
            mode=0o700
        )

        self.workspace_parent = (
            self.root
            / "workspaces"
        )
        self.workspace_parent.mkdir(
            mode=0o700
        )

        self.workspace = create_lab_workspace(
            str(self.workspace_parent),
            session_id=SESSION_ID,
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def _seed(
        self,
        relative_paths,
        *,
        max_files=32,
        max_total_bytes=1024 * 1024,
    ):
        return seed_lab_workspace(
            source_root=str(self.source),
            relative_paths=relative_paths,
            workspace_parent=str(
                self.workspace_parent
            ),
            workspace=self.workspace,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
        )

    def _workspace_entries(self):
        return list(
            Path(
                self.workspace.path
            ).iterdir()
        )

    def test_copies_only_explicit_manifest_with_hashes_and_modes(self):
        package = (
            self.source
            / "pkg"
        )
        package.mkdir(
            mode=0o700
        )

        main_file = (
            package
            / "main.py"
        )
        main_file.write_text(
            "print('hello')\n",
            encoding="utf-8",
        )
        main_file.chmod(
            0o600
        )

        tool_file = (
            self.source
            / "run-tool"
        )
        tool_file.write_text(
            "#!/bin/sh\nexit 0\n",
            encoding="utf-8",
        )
        tool_file.chmod(
            0o700
        )

        ignored = (
            self.source
            / "private-note.txt"
        )
        ignored.write_text(
            "must not be copied\n",
            encoding="utf-8",
        )
        ignored.chmod(
            0o600
        )

        record = self._seed(
            (
                "run-tool",
                "pkg/main.py",
            )
        )

        self.assertEqual(
            record.component,
            WORKSPACE_SEED_COMPONENT,
        )
        self.assertEqual(
            record.schema_version,
            WORKSPACE_SEED_SCHEMA_VERSION,
        )
        self.assertEqual(
            record.source_root,
            str(self.source),
        )
        self.assertEqual(
            tuple(
                entry.relative_path
                for entry in record.entries
            ),
            (
                "pkg/main.py",
                "run-tool",
            ),
        )

        copied_main = (
            Path(self.workspace.path)
            / "pkg/main.py"
        )
        copied_tool = (
            Path(self.workspace.path)
            / "run-tool"
        )

        self.assertEqual(
            copied_main.read_text(
                encoding="utf-8"
            ),
            "print('hello')\n",
        )
        self.assertEqual(
            copied_tool.read_text(
                encoding="utf-8"
            ),
            "#!/bin/sh\nexit 0\n",
        )

        self.assertFalse(
            (
                Path(self.workspace.path)
                / "private-note.txt"
            ).exists()
        )

        entries = {
            entry.relative_path: entry
            for entry in record.entries
        }

        self.assertEqual(
            entries["pkg/main.py"].sha256,
            hashlib.sha256(
                b"print('hello')\n"
            ).hexdigest(),
        )
        self.assertEqual(
            entries["run-tool"].sha256,
            hashlib.sha256(
                b"#!/bin/sh\nexit 0\n"
            ).hexdigest(),
        )

        self.assertEqual(
            entries["pkg/main.py"].mode,
            0o600,
        )
        self.assertEqual(
            entries["run-tool"].mode,
            0o700,
        )

        self.assertEqual(
            os.stat(
                copied_main
            ).st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            os.stat(
                copied_tool
            ).st_mode & 0o777,
            0o700,
        )

        self.assertEqual(
            record.total_bytes,
            (
                len(b"print('hello')\n")
                + len(b"#!/bin/sh\nexit 0\n")
            ),
        )

    def test_seed_records_source_mode_separately_from_private_workspace_mode(self):
        source_file = self.source / "ordinary.txt"
        source_file.write_text("ordinary\n", encoding="utf-8")
        source_file.chmod(0o644)

        record = self._seed(("ordinary.txt",))
        entry = record.entries[0]
        seeded = Path(self.workspace.path) / "ordinary.txt"

        self.assertEqual(entry.source_mode, 0o644)
        self.assertEqual(entry.mode, 0o600)
        self.assertEqual(seeded.stat().st_mode & 0o777, 0o600)

    def test_rejects_traversal_git_duplicates_and_malformed_paths(self):
        invalid_manifests = (
            ("../outside",),
            (".git/config",),
            ("/etc/passwd",),
            ("pkg//file.py",),
            ("pkg/./file.py",),
            ("same.py", "same.py"),
        )

        for manifest in invalid_manifests:
            with self.subTest(
                manifest=manifest
            ):
                with self.assertRaises(
                    LabWorkspaceSeedError
                ):
                    self._seed(
                        manifest
                    )

                self.assertEqual(
                    self._workspace_entries(),
                    [],
                )

    def test_rejects_symlink_source_without_copying(self):
        real_file = (
            self.source
            / "real.txt"
        )
        real_file.write_text(
            "safe bytes\n",
            encoding="utf-8",
        )
        real_file.chmod(
            0o600
        )

        link = (
            self.source
            / "link.txt"
        )
        link.symlink_to(
            real_file.name
        )

        with self.assertRaisesRegex(
            LabWorkspaceSeedError,
            "symlink",
        ):
            self._seed(
                ("link.txt",)
            )

        self.assertEqual(
            self._workspace_entries(),
            [],
        )

    def test_rejects_unsafe_source_permissions(self):
        unsafe = (
            self.source
            / "unsafe.txt"
        )
        unsafe.write_text(
            "unsafe\n",
            encoding="utf-8",
        )
        unsafe.chmod(
            0o666
        )

        with self.assertRaisesRegex(
            LabWorkspaceSeedError,
            "unsafe ownership or permissions",
        ):
            self._seed(
                ("unsafe.txt",)
            )

        self.assertEqual(
            self._workspace_entries(),
            [],
        )

    def test_limits_fail_before_any_copy(self):
        for name in (
            "one.txt",
            "two.txt",
        ):
            path = (
                self.source
                / name
            )
            path.write_text(
                "xx",
                encoding="utf-8",
            )
            path.chmod(
                0o600
            )

        with self.assertRaisesRegex(
            LabWorkspaceSeedError,
            "max_files",
        ):
            self._seed(
                (
                    "one.txt",
                    "two.txt",
                ),
                max_files=1,
            )

        self.assertEqual(
            self._workspace_entries(),
            [],
        )

        with self.assertRaisesRegex(
            LabWorkspaceSeedError,
            "max_total_bytes",
        ):
            self._seed(
                (
                    "one.txt",
                    "two.txt",
                ),
                max_total_bytes=3,
            )

        self.assertEqual(
            self._workspace_entries(),
            [],
        )

    def test_refuses_nonempty_workspace_without_overwrite(self):
        source_file = (
            self.source
            / "candidate.txt"
        )
        source_file.write_text(
            "candidate\n",
            encoding="utf-8",
        )
        source_file.chmod(
            0o600
        )

        existing = (
            Path(self.workspace.path)
            / "existing.txt"
        )
        existing.write_text(
            "keep me\n",
            encoding="utf-8",
        )
        existing.chmod(
            0o600
        )

        with self.assertRaisesRegex(
            LabWorkspaceSeedError,
            "workspace must be empty",
        ):
            self._seed(
                ("candidate.txt",)
            )

        self.assertEqual(
            existing.read_text(
                encoding="utf-8"
            ),
            "keep me\n",
        )
        self.assertFalse(
            (
                Path(self.workspace.path)
                / "candidate.txt"
            ).exists()
        )

    def test_source_change_during_copy_fails_closed(self):
        source_file = (
            self.source
            / "changing.txt"
        )
        source_file.write_bytes(
            b"original"
        )
        source_file.chmod(
            0o600
        )

        original_read = os.read
        changed = False

        def changing_read(
            fd,
            size,
        ):
            nonlocal changed

            data = original_read(
                fd,
                size,
            )

            if data and not changed:
                changed = True

                with source_file.open(
                    "ab"
                ) as handle:
                    handle.write(
                        b"-changed"
                    )
                    handle.flush()
                    os.fsync(
                        handle.fileno()
                    )

            return data

        with mock.patch.object(
            seed_module.os,
            "read",
            side_effect=changing_read,
        ):
            with self.assertRaisesRegex(
                LabWorkspaceSeedError,
                "source changed during copy",
            ):
                self._seed(
                    ("changing.txt",)
                )

        self.assertTrue(
            changed
        )


if __name__ == "__main__":
    unittest.main()
