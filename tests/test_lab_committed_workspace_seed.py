from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import hands_free_auto_lab.lab_committed_workspace_seed as seed_module
from hands_free_auto_lab.lab_committed_workspace_seed import (
    COMMITTED_WORKSPACE_SEED_COMPONENT,
    COMMITTED_WORKSPACE_SEED_SCHEMA_VERSION,
    LabCommittedWorkspaceSeedError,
    seed_lab_workspace_from_commit,
)
from hands_free_auto_lab.lab_workspace import create_lab_workspace


class LabCommittedWorkspaceSeedTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-committed-seed-tests-"
            )
        )

        self.repo = (
            self.root
            / "repo"
        )
        self.repo.mkdir()

        self.workspace_parent = (
            self.root
            / "workspaces"
        )
        self.workspace_parent.mkdir(
            mode=0o700
        )

        self.counter = 0

        self._git(
            "init",
            "-b",
            "main",
        )

        (
            self.repo
            / "alpha.txt"
        ).write_bytes(
            b"committed-alpha\n"
        )

        (
            self.repo
            / "tool.sh"
        ).write_bytes(
            b"#!/bin/sh\nexit 0\n"
        )

        (
            self.repo
            / "tool.sh"
        ).chmod(
            0o755
        )

        self._git(
            "add",
            "alpha.txt",
            "tool.sh",
        )

        self._git(
            "commit",
            "-m",
            "initial",
        )

        self.head = self._git_text(
            "rev-parse",
            "HEAD",
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def _git_env(self):
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }

    def _git(
        self,
        *args,
        check=True,
    ):
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Auto Lab Test",
                "-c",
                "user.email=auto-lab@example.invalid",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(self.repo),
                *args,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=self._git_env(),
        )

        if (
            check
            and result.returncode != 0
        ):
            self.fail(
                f"test Git command failed: {args!r}\n"
                + result.stderr.decode(
                    "utf-8",
                    errors="replace",
                )
            )

        return result

    def _git_text(
        self,
        *args,
    ):
        return self._git(
            *args
        ).stdout.decode(
            "utf-8"
        ).strip()

    def _workspace(self):
        self.counter += 1

        return create_lab_workspace(
            str(
                self.workspace_parent
            ),
            session_id=f"{self.counter:064x}",
        )

    def _seed(
        self,
        paths=("alpha.txt",),
        *,
        workspace=None,
        commit=None,
        branch="main",
        max_files=32,
        max_bytes=1024 * 1024,
    ):
        if workspace is None:
            workspace = self._workspace()

        return seed_lab_workspace_from_commit(
            repository_path=str(
                self.repo
            ),
            commit_oid=(
                self.head
                if commit is None
                else commit
            ),
            expected_branch=branch,
            relative_paths=paths,
            workspace_parent=str(
                self.workspace_parent
            ),
            workspace=workspace,
            max_files=max_files,
            max_total_bytes=max_bytes,
        )

    def test_exact_commit_seed_accepts_775_664_and_preserves_provenance(self):
        self.repo.chmod(
            0o775
        )

        (
            self.repo
            / ".git"
        ).chmod(
            0o775
        )

        (
            self.repo
            / "alpha.txt"
        ).chmod(
            0o664
        )

        before_head = self._git_text(
            "rev-parse",
            "HEAD",
        )

        before_status = self._git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ).stdout

        before_index = hashlib.sha256(
            (
                self.repo
                / ".git/index"
            ).read_bytes()
        ).hexdigest()

        record = self._seed(
            (
                "tool.sh",
                "alpha.txt",
            )
        )

        entries = {
            entry.relative_path: entry
            for entry in record.entries
        }

        workspace = Path(
            record.workspace.path
        )

        self.assertEqual(
            record.component,
            COMMITTED_WORKSPACE_SEED_COMPONENT,
        )

        self.assertEqual(
            record.schema_version,
            COMMITTED_WORKSPACE_SEED_SCHEMA_VERSION,
        )

        self.assertEqual(
            record.commit_oid,
            self.head,
        )

        self.assertEqual(
            record.branch,
            "main",
        )

        self.assertEqual(
            record.object_format,
            "sha1",
        )

        self.assertEqual(
            tuple(
                entries
            ),
            (
                "alpha.txt",
                "tool.sh",
            ),
        )

        self.assertEqual(
            (
                workspace
                / "alpha.txt"
            ).read_bytes(),
            b"committed-alpha\n",
        )

        self.assertEqual(
            (
                workspace
                / "tool.sh"
            ).read_bytes(),
            b"#!/bin/sh\nexit 0\n",
        )

        self.assertEqual(
            entries[
                "alpha.txt"
            ].git_mode,
            "100644",
        )

        self.assertEqual(
            entries[
                "alpha.txt"
            ].source_mode,
            0o644,
        )

        self.assertEqual(
            entries[
                "alpha.txt"
            ].mode,
            0o600,
        )

        self.assertEqual(
            entries[
                "tool.sh"
            ].git_mode,
            "100755",
        )

        self.assertEqual(
            entries[
                "tool.sh"
            ].source_mode,
            0o755,
        )

        self.assertEqual(
            entries[
                "tool.sh"
            ].mode,
            0o700,
        )

        self.assertEqual(
            (
                workspace
                / "alpha.txt"
            ).stat().st_mode
            & 0o777,
            0o600,
        )

        self.assertEqual(
            (
                workspace
                / "tool.sh"
            ).stat().st_mode
            & 0o777,
            0o700,
        )

        self.assertEqual(
            self._git_text(
                "rev-parse",
                "HEAD",
            ),
            before_head,
        )

        self.assertEqual(
            self._git(
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ).stdout,
            before_status,
        )

        self.assertEqual(
            hashlib.sha256(
                (
                    self.repo
                    / ".git/index"
                ).read_bytes()
            ).hexdigest(),
            before_index,
        )

        self.assertEqual(
            (
                self.repo
                / "alpha.txt"
            ).read_bytes(),
            b"committed-alpha\n",
        )

    def test_verified_blob_reader_uses_object_bytes_not_working_tree_bytes(self):
        blob_oid = self._git_text(
            "rev-parse",
            "HEAD:alpha.txt",
        )

        (
            self.repo
            / "alpha.txt"
        ).write_bytes(
            b"dirty-working-tree\n"
        )

        raw = seed_module._verified_object(
            str(
                self.repo
            ),
            "blob",
            blob_oid,
            1024,
        )

        self.assertEqual(
            raw,
            b"committed-alpha\n",
        )

        self.assertEqual(
            (
                self.repo
                / "alpha.txt"
            ).read_bytes(),
            b"dirty-working-tree\n",
        )

    def test_dirty_staged_and_untracked_states_fail_closed(self):
        cases = (
            "dirty",
            "staged",
            "untracked",
        )

        for case in cases:
            with self.subTest(
                case=case
            ):
                repo = (
                    self.root
                    / f"case-{case}"
                )

                shutil.copytree(
                    self.repo,
                    repo,
                )

                original = self.repo
                self.repo = repo

                try:
                    if case == "dirty":
                        (
                            repo
                            / "alpha.txt"
                        ).write_bytes(
                            b"dirty\n"
                        )

                    elif case == "staged":
                        (
                            repo
                            / "alpha.txt"
                        ).write_bytes(
                            b"staged\n"
                        )

                        self._git(
                            "add",
                            "alpha.txt",
                        )

                    else:
                        (
                            repo
                            / "extra.txt"
                        ).write_bytes(
                            b"untracked\n"
                        )

                    with self.assertRaisesRegex(
                        LabCommittedWorkspaceSeedError,
                        "clean working tree and index",
                    ):
                        self._seed()

                finally:
                    self.repo = original

    def test_exact_commit_branch_and_commit_id_validation_fail_closed(self):
        invalid = (
            {
                "commit": "0" * 40,
                "branch": "main",
                "pattern": "HEAD",
            },
            {
                "commit": "abc",
                "branch": "main",
                "pattern": "SHA-1",
            },
            {
                "commit": True,
                "branch": "main",
                "pattern": "commit_oid",
            },
            {
                "commit": self.head,
                "branch": "other",
                "pattern": "expected_branch",
            },
        )

        for case in invalid:
            with self.subTest(
                case=case
            ):
                with self.assertRaisesRegex(
                    LabCommittedWorkspaceSeedError,
                    case["pattern"],
                ):
                    self._seed(
                        commit=case[
                            "commit"
                        ],
                        branch=case[
                            "branch"
                        ],
                    )

    def test_path_manifest_rejects_missing_traversal_git_duplicates_and_directory(self):
        (
            self.repo
            / "pkg"
        ).mkdir()

        (
            self.repo
            / "pkg/file.txt"
        ).write_bytes(
            b"x\n"
        )

        self._git(
            "add",
            "pkg/file.txt",
        )

        self._git(
            "commit",
            "-m",
            "package",
        )

        self.head = self._git_text(
            "rev-parse",
            "HEAD",
        )

        invalid = (
            (
                ("missing.txt",),
                "missing",
            ),
            (
                ("../outside",),
                "canonical",
            ),
            (
                (".git/config",),
                r"\.git",
            ),
            (
                (
                    "alpha.txt",
                    "alpha.txt",
                ),
                "duplicates",
            ),
            (
                ("pkg",),
                "ordinary",
            ),
            (
                ("/etc/passwd",),
                "canonical",
            ),
            (
                ("pkg//file.txt",),
                "canonical",
            ),
        )

        for paths, pattern in invalid:
            with self.subTest(
                paths=paths
            ):
                with self.assertRaisesRegex(
                    LabCommittedWorkspaceSeedError,
                    pattern,
                ):
                    self._seed(
                        paths
                    )

    def test_symlink_blob_is_rejected(self):
        (
            self.repo
            / "link.txt"
        ).symlink_to(
            "alpha.txt"
        )

        self._git(
            "add",
            "link.txt",
        )

        self._git(
            "commit",
            "-m",
            "symlink",
        )

        self.head = self._git_text(
            "rev-parse",
            "HEAD",
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "ordinary",
        ):
            self._seed(
                (
                    "link.txt",
                )
            )

    def test_gitlink_is_rejected(self):
        previous = self.head

        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{previous},vendor",
        )

        (
            self.repo
            / "vendor"
        ).mkdir()

        self._git(
            "commit",
            "-m",
            "gitlink",
        )

        self.head = self._git_text(
            "rev-parse",
            "HEAD",
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "ordinary",
        ):
            self._seed(
                (
                    "vendor",
                )
            )

    def test_limits_are_enforced_before_blob_materialization(self):
        with mock.patch.object(
            seed_module,
            "_verified_object",
            wraps=seed_module._verified_object,
        ) as reader:
            with self.assertRaisesRegex(
                LabCommittedWorkspaceSeedError,
                "max_total_bytes",
            ):
                self._seed(
                    max_bytes=2
                )

            blob_reads = [
                call
                for call in reader.call_args_list
                if (
                    len(
                        call.args
                    ) >= 2
                    and call.args[1] == "blob"
                )
            ]

            self.assertEqual(
                blob_reads,
                [],
            )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "max_files",
        ):
            self._seed(
                (
                    "alpha.txt",
                    "tool.sh",
                ),
                max_files=1,
            )

    def test_repository_indirection_files_and_replace_refs_are_rejected(self):
        artifacts = (
            ".git/commondir",
            ".git/objects/info/alternates",
            ".git/info/grafts",
            ".git/shallow",
        )

        for relative in artifacts:
            with self.subTest(
                relative=relative
            ):
                path = (
                    self.repo
                    / relative
                )

                path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                path.write_text(
                    "unexpected\n",
                    encoding="utf-8",
                )

                try:
                    with self.assertRaisesRegex(
                        LabCommittedWorkspaceSeedError,
                        "indirection",
                    ):
                        self._seed()

                finally:
                    path.unlink()

        self._git(
            "update-ref",
            f"refs/replace/{self.head}",
            self.head,
        )

        try:
            with self.assertRaisesRegex(
                LabCommittedWorkspaceSeedError,
                "replace refs",
            ):
                self._seed()

        finally:
            self._git(
                "update-ref",
                "-d",
                f"refs/replace/{self.head}",
            )

    def test_git_directory_symlink_substitution_is_rejected(self):
        info = (
            self.repo
            / ".git/objects/info"
        )

        backup = (
            self.repo
            / ".git/objects/info-real"
        )

        info.rename(
            backup
        )

        info.symlink_to(
            backup.name
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "real directory",
        ):
            self._seed()

    def test_inherited_git_object_redirection_cannot_change_source(self):
        with mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": "/definitely/not/the/repository",
                "GIT_OBJECT_DIRECTORY": "/definitely/not/objects",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/definitely/not/alternates",
            },
            clear=False,
        ):
            record = self._seed()

        self.assertEqual(
            (
                Path(
                    record.workspace.path
                )
                / "alpha.txt"
            ).read_bytes(),
            b"committed-alpha\n",
        )

    def test_unsupported_object_format_fails_closed(self):
        original_git = seed_module._git

        def altered(
            path,
            *args,
            **kwargs,
        ):
            if args == (
                "rev-parse",
                "--show-object-format=storage",
            ):
                return subprocess.CompletedProcess(
                    args=(),
                    returncode=0,
                    stdout=b"sha256\n",
                    stderr=b"",
                )

            return original_git(
                path,
                *args,
                **kwargs,
            )

        with mock.patch.object(
            seed_module,
            "_git",
            side_effect=altered,
        ):
            with self.assertRaisesRegex(
                LabCommittedWorkspaceSeedError,
                "SHA-1",
            ):
                self._seed()

    def test_independent_commit_object_hash_mismatch_fails_closed(self):
        original = seed_module._git_oid

        def wrong(
            kind,
            raw,
        ):
            if kind == "commit":
                return "0" * 40

            return original(
                kind,
                raw,
            )

        with mock.patch.object(
            seed_module,
            "_git_oid",
            side_effect=wrong,
        ):
            with self.assertRaisesRegex(
                LabCommittedWorkspaceSeedError,
                "independent SHA-1",
            ):
                self._seed()

    def test_independent_blob_object_hash_mismatch_fails_before_write(self):
        original = seed_module._git_oid
        workspace = self._workspace()

        def wrong(
            kind,
            raw,
        ):
            if kind == "blob":
                return "0" * 40

            return original(
                kind,
                raw,
            )

        with mock.patch.object(
            seed_module,
            "_git_oid",
            side_effect=wrong,
        ):
            with self.assertRaisesRegex(
                LabCommittedWorkspaceSeedError,
                "independent SHA-1",
            ):
                self._seed(
                    workspace=workspace
                )

        self.assertEqual(
            list(
                Path(
                    workspace.path
                ).iterdir()
            ),
            [],
        )

    def test_nonempty_workspace_and_malformed_arguments_fail_closed(self):
        workspace = self._workspace()

        (
            Path(
                workspace.path
            )
            / "existing.txt"
        ).write_bytes(
            b"keep\n"
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "empty",
        ):
            self._seed(
                workspace=workspace
            )

        malformed = (
            {
                "relative_paths": [
                    "alpha.txt"
                ],
                "max_files": 32,
                "max_total_bytes": 1024,
            },
            {
                "relative_paths": (
                    True,
                ),
                "max_files": 32,
                "max_total_bytes": 1024,
            },
            {
                "relative_paths": (
                    "alpha.txt",
                ),
                "max_files": True,
                "max_total_bytes": 1024,
            },
            {
                "relative_paths": (
                    "alpha.txt",
                ),
                "max_files": 32,
                "max_total_bytes": True,
            },
        )

        for case in malformed:
            with self.subTest(
                case=case
            ):
                with self.assertRaises(
                    LabCommittedWorkspaceSeedError
                ):
                    seed_lab_workspace_from_commit(
                        repository_path=str(
                            self.repo
                        ),
                        commit_oid=self.head,
                        expected_branch="main",
                        workspace_parent=str(
                            self.workspace_parent
                        ),
                        workspace=self._workspace(),
                        **case,
                    )

    def _linked_worktree(self):
        linked = (
            self.root
            / "linked"
        )

        branch = "linked"

        self._git(
            "worktree",
            "add",
            "-b",
            branch,
            str(linked),
            self.head,
        )

        return linked, branch

    def _linked_git_dir(self, linked):
        marker = (
            linked
            / ".git"
        )

        raw = marker.read_text(
            encoding="utf-8"
        )

        prefix = "gitdir: "

        self.assertTrue(
            raw.startswith(prefix)
        )

        self.assertTrue(
            raw.endswith("\n")
        )

        self.assertEqual(
            raw.count("\n"),
            1,
        )

        return Path(
            raw[
                len(prefix) : -1
            ]
        )

    def _seed_linked(
        self,
        linked,
        branch,
    ):
        return seed_lab_workspace_from_commit(
            repository_path=str(
                linked
            ),
            commit_oid=self.head,
            expected_branch=branch,
            relative_paths=(
                "alpha.txt",
            ),
            workspace_parent=str(
                self.workspace_parent
            ),
            workspace=self._workspace(),
            max_files=32,
            max_total_bytes=1024 * 1024,
        )

    def test_linked_worktree_exact_commit_seed_preserves_provenance(self):
        linked, branch = self._linked_worktree()

        marker = (
            linked
            / ".git"
        )

        marker_before = marker.read_bytes()
        marker_state = marker.stat()

        git_dir = self._linked_git_dir(
            linked
        )

        commondir_before = (
            git_dir
            / "commondir"
        ).read_bytes()

        gitdir_before = (
            git_dir
            / "gitdir"
        ).read_bytes()

        record = self._seed_linked(
            linked,
            branch,
        )

        workspace = Path(
            record.workspace.path
        )

        self.assertEqual(
            record.repository_path,
            str(linked),
        )

        self.assertEqual(
            record.branch,
            branch,
        )

        self.assertEqual(
            record.commit_oid,
            self.head,
        )

        self.assertEqual(
            (
                record.git_device,
                record.git_inode,
            ),
            (
                marker_state.st_dev,
                marker_state.st_ino,
            ),
        )

        self.assertEqual(
            (
                workspace
                / "alpha.txt"
            ).read_bytes(),
            b"committed-alpha\n",
        )

        self.assertEqual(
            marker.read_bytes(),
            marker_before,
        )

        self.assertEqual(
            (
                git_dir
                / "commondir"
            ).read_bytes(),
            commondir_before,
        )

        self.assertEqual(
            (
                git_dir
                / "gitdir"
            ).read_bytes(),
            gitdir_before,
        )

        self.assertEqual(
            self._git_text(
                "-C",
                str(linked),
                "rev-parse",
                "HEAD",
            ),
            self.head,
        )

    def test_linked_worktree_git_defined_ref_namespaces_are_allowed(self):
        linked, branch = self._linked_worktree()

        refs_root = (
            self._linked_git_dir(linked)
            / "refs"
        )

        for namespace in (
            "bisect",
            "worktree",
            "rewritten",
        ):
            nested = (
                refs_root
                / namespace
                / "nested"
            )
            nested.mkdir(
                parents=True,
                exist_ok=True,
            )
            (
                nested
                / "example"
            ).write_text(
                self.head + "\n",
                encoding="ascii",
            )

        record = self._seed_linked(
            linked,
            branch,
        )

        self.assertEqual(
            (
                Path(record.workspace.path)
                / "alpha.txt"
            ).read_bytes(),
            b"committed-alpha\n",
        )

    def test_linked_worktree_shared_ref_namespace_fails_closed(self):
        linked, branch = self._linked_worktree()

        refs_heads = (
            self._linked_git_dir(linked)
            / "refs"
            / "heads"
        )
        refs_heads.mkdir(
            parents=True,
            exist_ok=True,
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "unsupported linked-worktree ref namespace",
        ):
            self._seed_linked(
                linked,
                branch,
            )


    def test_linked_worktree_malformed_dot_git_pointer_fails_closed(self):
        linked, branch = self._linked_worktree()

        (
            linked
            / ".git"
        ).write_text(
            "not-a-gitdir-pointer\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "pointer is malformed",
        ):
            self._seed_linked(
                linked,
                branch,
            )

    def test_linked_worktree_gitdir_back_pointer_mismatch_fails_closed(self):
        linked, branch = self._linked_worktree()

        git_dir = self._linked_git_dir(
            linked
        )

        (
            git_dir
            / "gitdir"
        ).write_text(
            str(
                self.repo
                / ".git"
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "back-pointer",
        ):
            self._seed_linked(
                linked,
                branch,
            )

    def test_linked_worktree_nonstandard_commondir_fails_closed(self):
        linked, branch = self._linked_worktree()

        git_dir = self._linked_git_dir(
            linked
        )

        (
            git_dir
            / "commondir"
        ).write_text(
            "../not-the-common-directory\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "commondir",
        ):
            self._seed_linked(
                linked,
                branch,
            )

    def test_linked_worktree_git_directory_symlink_substitution_fails_closed(self):
        linked, branch = self._linked_worktree()

        git_dir = self._linked_git_dir(
            linked
        )

        backup = git_dir.with_name(
            git_dir.name
            + "-real"
        )

        git_dir.rename(
            backup
        )

        git_dir.symlink_to(
            backup.name,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "symlink-free",
        ):
            self._seed_linked(
                linked,
                branch,
            )


if __name__ == "__main__":
    unittest.main()

# ADDED_TARGET_COMMITTED_SEED_REPAIR_REGRESSIONS_V1

import tempfile as _added_repair_tempfile
import unittest as _added_repair_unittest


def _added_repair_make_git_repo(
    tmp_root,
    files,
):
    import os
    import subprocess

    repo = tmp_root / "added-repair-source"

    repo.mkdir(
        mode=0o700,
    )

    os.chmod(
        repo,
        0o700,
    )

    subprocess.run(
        [
            "git",
            "init",
            "-b",
            "dev",
            str(repo),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "user.name",
            "Auto Lab Test",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "user.email",
            "auto-lab-test@example.invalid",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    for relative, content in files.items():
        target = repo / relative

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        target.write_bytes(
            content
        )

        os.chmod(
            target,
            0o644,
        )

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "add",
            "-A",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "commit",
            "--allow-empty",
            "-m",
            "source",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    commit_oid = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.decode(
        "ascii"
    ).strip()

    return (
        repo.resolve(),
        commit_oid,
    )


def _added_repair_make_workspace(
    tmp_root,
    session_id,
):
    import os

    from hands_free_auto_lab.lab_workspace import (
        create_lab_workspace,
    )

    parent = (
        tmp_root
        / "added-repair-workspaces"
    )

    parent.mkdir(
        mode=0o700,
    )

    os.chmod(
        parent,
        0o700,
    )

    workspace = create_lab_workspace(
        str(
            parent.resolve()
        ),
        session_id=session_id,
    )

    return (
        parent.resolve(),
        workspace,
    )


class AddedTargetCommittedSeedRepairTests(
    _added_repair_unittest.TestCase
):

    def test_existing_committed_path_still_materializes(
        self,
    ):
        from pathlib import Path

        from hands_free_auto_lab.lab_committed_workspace_seed import (
            seed_lab_workspace_from_commit,
        )

        with _added_repair_tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            repo, commit_oid = _added_repair_make_git_repo(
                root,
                {
                    "existing.txt": b"old\n",
                },
            )

            parent, workspace = _added_repair_make_workspace(
                root,
                "a" * 64,
            )

            record = seed_lab_workspace_from_commit(
                repository_path=str(repo),
                commit_oid=commit_oid,
                expected_branch="dev",
                relative_paths=(
                    "existing.txt",
                ),
                workspace_parent=str(parent),
                workspace=workspace,
                max_files=4,
                max_total_bytes=4096,
            )

            self.assertEqual(
                tuple(
                    entry.relative_path
                    for entry in record.entries
                ),
                (
                    "existing.txt",
                ),
            )

            self.assertEqual(
                (
                    Path(
                        workspace.path
                    )
                    / "existing.txt"
                ).read_bytes(),
                b"old\n",
            )

    def test_ordinary_missing_path_still_fails_closed(
        self,
    ):
        from pathlib import Path

        from hands_free_auto_lab.lab_committed_workspace_seed import (
            LabCommittedWorkspaceSeedError,
            seed_lab_workspace_from_commit,
        )

        with _added_repair_tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            repo, commit_oid = _added_repair_make_git_repo(
                root,
                {},
            )

            parent, workspace = _added_repair_make_workspace(
                root,
                "b" * 64,
            )

            with self.assertRaisesRegex(
                LabCommittedWorkspaceSeedError,
                "exact committed path is missing or ambiguous",
            ):
                seed_lab_workspace_from_commit(
                    repository_path=str(repo),
                    commit_oid=commit_oid,
                    expected_branch="dev",
                    relative_paths=(
                        "missing.txt",
                    ),
                    workspace_parent=str(parent),
                    workspace=workspace,
                    max_files=4,
                    max_total_bytes=4096,
                )

    def test_expected_absent_path_remains_absent(
        self,
    ):
        from pathlib import Path

        from hands_free_auto_lab.lab_committed_workspace_seed import (
            seed_lab_workspace_from_commit,
        )

        with _added_repair_tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            repo, commit_oid = _added_repair_make_git_repo(
                root,
                {},
            )

            parent, workspace = _added_repair_make_workspace(
                root,
                "c" * 64,
            )

            record = seed_lab_workspace_from_commit(
                repository_path=str(repo),
                commit_oid=commit_oid,
                expected_branch="dev",
                relative_paths=(
                    "new.txt",
                ),
                expected_absent_paths=(
                    "new.txt",
                ),
                workspace_parent=str(parent),
                workspace=workspace,
                max_files=4,
                max_total_bytes=4096,
            )

            self.assertEqual(
                record.entries,
                (),
            )

            self.assertFalse(
                (
                    Path(
                        workspace.path
                    )
                    / "new.txt"
                ).exists()
            )

    def test_expected_absent_existing_at_commit_fails_closed(
        self,
    ):
        from pathlib import Path

        from hands_free_auto_lab.lab_committed_workspace_seed import (
            LabCommittedWorkspaceSeedError,
            seed_lab_workspace_from_commit,
        )

        with _added_repair_tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            repo, commit_oid = _added_repair_make_git_repo(
                root,
                {
                    "existing.txt": b"already here\n",
                },
            )

            parent, workspace = _added_repair_make_workspace(
                root,
                "d" * 64,
            )

            with self.assertRaisesRegex(
                LabCommittedWorkspaceSeedError,
                "expected-absent committed path exists",
            ):
                seed_lab_workspace_from_commit(
                    repository_path=str(repo),
                    commit_oid=commit_oid,
                    expected_branch="dev",
                    relative_paths=(
                        "existing.txt",
                    ),
                    expected_absent_paths=(
                        "existing.txt",
                    ),
                    workspace_parent=str(parent),
                    workspace=workspace,
                    max_files=4,
                    max_total_bytes=4096,
                )

    def test_expected_absent_disjoint_from_seed_manifest_succeeds(
        self,
    ):
        from pathlib import Path

        from hands_free_auto_lab.lab_committed_workspace_seed import (
            seed_lab_workspace_from_commit,
        )

        with _added_repair_tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            repo, commit_oid = _added_repair_make_git_repo(
                root,
                {
                    "project.py": b"VALUE = 1\n",
                },
            )

            parent, workspace = _added_repair_make_workspace(
                root,
                "e" * 64,
            )

            record = seed_lab_workspace_from_commit(
                repository_path=str(repo),
                commit_oid=commit_oid,
                expected_branch="dev",
                relative_paths=(
                    "project.py",
                ),
                expected_absent_paths=(
                    "worker-output.txt",
                ),
                workspace_parent=str(parent),
                workspace=workspace,
                max_files=4,
                max_total_bytes=4096,
            )

            self.assertEqual(
                tuple(
                    entry.relative_path
                    for entry in record.entries
                ),
                (
                    "project.py",
                ),
            )

            self.assertEqual(
                (
                    Path(workspace.path)
                    / "project.py"
                ).read_bytes(),
                b"VALUE = 1\n",
            )

            self.assertFalse(
                (
                    Path(workspace.path)
                    / "worker-output.txt"
                ).exists()
            )

    def test_expected_absent_disjoint_existing_at_commit_fails_closed(
        self,
    ):
        from pathlib import Path

        from hands_free_auto_lab.lab_committed_workspace_seed import (
            LabCommittedWorkspaceSeedError,
            seed_lab_workspace_from_commit,
        )

        with _added_repair_tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            repo, commit_oid = _added_repair_make_git_repo(
                root,
                {
                    "project.py": b"VALUE = 1\n",
                    "worker-output.txt": b"already committed\n",
                },
            )

            parent, workspace = _added_repair_make_workspace(
                root,
                "f" * 64,
            )

            with self.assertRaisesRegex(
                LabCommittedWorkspaceSeedError,
                "expected-absent committed path exists",
            ):
                seed_lab_workspace_from_commit(
                    repository_path=str(repo),
                    commit_oid=commit_oid,
                    expected_branch="dev",
                    relative_paths=(
                        "project.py",
                    ),
                    expected_absent_paths=(
                        "worker-output.txt",
                    ),
                    workspace_parent=str(parent),
                    workspace=workspace,
                    max_files=4,
                    max_total_bytes=4096,
                )
