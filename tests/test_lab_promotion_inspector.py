from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hands_free_auto_lab.lab_coding_candidate import (
    CANDIDATE_COMPONENT,
    CANDIDATE_SCHEMA_VERSION,
    LabCodingCandidate,
    LabCodingCandidateFile,
    _candidate_id as coding_candidate_id,
)
from hands_free_auto_lab.lab_promotion_candidate import (
    PROMOTION_CANDIDATE_COMPONENT,
    PROMOTION_CANDIDATE_SCHEMA_VERSION,
    LabPromotionCandidate,
    LabPromotionCandidateFile,
)
from hands_free_auto_lab.lab_promotion_inspector import (
    PROMOTION_INSPECTOR_COMPONENT,
    PROMOTION_INSPECTOR_SCHEMA_VERSION,
    LabPromotionInspectorError,
    inspect_lab_promotion_repository,
)
from hands_free_auto_lab.lab_promotion_proposal import (
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
    build_lab_coding_candidate_promotion_proposal,
    build_lab_promotion_proposal,
)


GIT = "/usr/bin/git"


def _sha(
    content: str,
) -> str:
    return hashlib.sha256(
        content.encode(
            "utf-8"
        )
    ).hexdigest()


def _run_git(
    repository: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[
    bytes
]:
    return subprocess.run(
        [
            GIT,
            "-C",
            str(
                repository
            ),
            *arguments,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
        },
    )


class LabPromotionInspectorTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-promotion-inspector-tests-"
            )
        )

        os.chmod(
            self.root,
            0o700,
        )

        self.repository = (
            self.root
            / "repo"
        )

        self.repository.mkdir(
            mode=0o700
        )

        _run_git(
            self.repository,
            "init",
            "-b",
            "main",
        )

        (
            self.repository
            / "tests"
        ).mkdir()

        (
            self.repository
            / "alpha.txt"
        ).write_text(
            "old alpha\n",
            encoding="utf-8",
        )

        os.chmod(
            self.repository / "alpha.txt",
            0o644,
        )

        (
            self.repository
            / "tests"
            / "marker.txt"
        ).write_text(
            "marker\n",
            encoding="utf-8",
        )

        _run_git(
            self.repository,
            "add",
            "alpha.txt",
            "tests/marker.txt",
        )

        _run_git(
            self.repository,
            "-c",
            "user.name=Auto Lab Test",
            "-c",
            "user.email=auto-lab@example.invalid",
            "commit",
            "-m",
            "Initial test state",
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def _candidate(
        self,
        *,
        alpha_path: str = "alpha.txt",
        add_path: str = "tests/test_alpha.py",
    ) -> LabPromotionCandidate:
        files = (
            LabPromotionCandidateFile(
                path=alpha_path,
                bytes=len(
                    "new alpha\n".encode(
                        "utf-8"
                    )
                ),
                sha256=_sha(
                    "new alpha\n"
                ),
                content="new alpha\n",
                latest_write_action_id=(
                    "a" * 64
                ),
            ),
            LabPromotionCandidateFile(
                path=add_path,
                bytes=len(
                    "test\n".encode(
                        "utf-8"
                    )
                ),
                sha256=_sha(
                    "test\n"
                ),
                content="test\n",
                latest_write_action_id=(
                    "b" * 64
                ),
            ),
        )

        return LabPromotionCandidate(
            component=(
                PROMOTION_CANDIDATE_COMPONENT
            ),
            schema_version=(
                PROMOTION_CANDIDATE_SCHEMA_VERSION
            ),
            session_id="d" * 64,
            workspace_device=10,
            workspace_inode=20,
            controller_status="done",
            workspace_generation=2,
            tested_generation=2,
            acceptance_generation=None,
            files=files,
        )

    def _coding_candidate(
        self,
        *,
        operation: str = "MODIFIED",
        path: str = "alpha.txt",
    ) -> LabCodingCandidate:
        before_content = "old alpha\n"
        final_content = "new alpha\n"
        item = LabCodingCandidateFile(
            operation=operation,
            path=path,
            before_bytes=(
                len(before_content.encode("utf-8"))
                if operation == "MODIFIED" else None
            ),
            before_sha256=(
                _sha(before_content) if operation == "MODIFIED" else None
            ),
            before_mode=(
                (self.repository / "alpha.txt").stat().st_mode & 0o777
                if operation == "MODIFIED" else None
            ),
            final_bytes=len(final_content.encode("utf-8")),
            final_sha256=_sha(final_content),
            final_mode=0o644,
            content=final_content,
        )
        prototype = LabCodingCandidate(
            component=CANDIDATE_COMPONENT,
            schema_version=CANDIDATE_SCHEMA_VERSION,
            candidate_id="0" * 64,
            workspace_session_id="c" * 64,
            workspace_device=3,
            workspace_inode=4,
            before_snapshot_id="d" * 64,
            after_snapshot_id="e" * 64,
            physical_diff_id="f" * 64,
            files=(item,),
        )
        return replace(
            prototype,
            candidate_id=coding_candidate_id(prototype),
        )

    def test_coding_candidate_inspection_binds_exact_modify_preimage(self):
        candidate = self._coding_candidate()
        inspection = inspect_lab_promotion_repository(
            candidate=candidate,
            repository_path=str(self.repository),
        )

        self.assertEqual(inspection.candidate_id, candidate.candidate_id)
        self.assertEqual(
            (
                inspection.file_states[0].exists,
                inspection.file_states[0].bytes,
                inspection.file_states[0].sha256,
                inspection.file_states[0].mode,
            ),
            (
                True,
                len(b"old alpha\n"),
                _sha("old alpha\n"),
                (self.repository / "alpha.txt").stat().st_mode & 0o777,
            ),
        )

        proposal = build_lab_coding_candidate_promotion_proposal(
            candidate=candidate,
            repository_path=inspection.repository_path,
            repository_device=inspection.repository_device,
            repository_inode=inspection.repository_inode,
            branch=inspection.branch,
            head=inspection.head,
            before_states=inspection.file_states,
        )
        self.assertEqual(proposal.files[0].source_kind, "coding_candidate")
        self.assertEqual(proposal.files[0].source_id, candidate.candidate_id)

    def test_coding_added_requires_destination_absent(self):
        candidate = self._coding_candidate(
            operation="ADDED",
            path="alpha.txt",
        )
        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "ADDED promotion destination must be absent",
        ):
            inspect_lab_promotion_repository(
                candidate=candidate,
                repository_path=str(self.repository),
            )

    def test_coding_modified_requires_destination_present(self):
        candidate = self._coding_candidate(
            operation="MODIFIED",
            path="tests/missing.py",
        )
        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "MODIFIED promotion destination must be present",
        ):
            inspect_lab_promotion_repository(
                candidate=candidate,
                repository_path=str(self.repository),
            )


    def test_coding_modified_stale_before_evidence_is_refused_by_inspector(self):
        candidate = self._coding_candidate()
        for changes in (
            {"before_bytes": 999},
            {"before_sha256": _sha("stale\n")},
            {"before_mode": 0o640},
        ):
            with self.subTest(changes=changes):
                item = replace(candidate.files[0], **changes)
                prototype = replace(candidate, candidate_id="0" * 64, files=(item,))
                stale = replace(
                    prototype,
                    candidate_id=coding_candidate_id(prototype),
                )
                with self.assertRaisesRegex(
                    LabPromotionInspectorError,
                    "before evidence does not match",
                ):
                    inspect_lab_promotion_repository(
                        candidate=stale,
                        repository_path=str(self.repository),
                    )

    def test_hardlinked_destination_is_refused(self):
        os.link(
            self.repository / "alpha.txt",
            self.repository / "alpha-hardlink.txt",
        )
        _run_git(self.repository, "add", "alpha-hardlink.txt")
        _run_git(
            self.repository,
            "-c", "user.name=Auto Lab Test",
            "-c", "user.email=auto-lab@example.invalid",
            "commit", "-m", "Add hard link",
        )
        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "must not be hard-linked",
        ):
            inspect_lab_promotion_repository(
                candidate=self._coding_candidate(),
                repository_path=str(self.repository),
            )

    def test_directory_destination_is_refused(self):
        candidate = self._coding_candidate(path="tests")
        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "must be a regular file",
        ):
            inspect_lab_promotion_repository(
                candidate=candidate,
                repository_path=str(self.repository),
            )

    def test_executable_destination_is_refused(self):
        os.chmod(self.repository / "alpha.txt", 0o755)
        _run_git(self.repository, "add", "alpha.txt")
        _run_git(
            self.repository,
            "-c", "user.name=Auto Lab Test",
            "-c", "user.email=auto-lab@example.invalid",
            "commit", "-m", "Make executable",
        )
        candidate = self._coding_candidate()
        with self.assertRaises(Exception):
            inspect_lab_promotion_repository(
                candidate=candidate,
                repository_path=str(self.repository),
            )

    def test_clean_repository_inspection_produces_exact_before_states(self):
        candidate = self._candidate()

        inspection = inspect_lab_promotion_repository(
            candidate=candidate,
            repository_path=str(
                self.repository
            ),
        )

        self.assertEqual(
            inspection.component,
            PROMOTION_INSPECTOR_COMPONENT,
        )

        self.assertEqual(
            inspection.schema_version,
            PROMOTION_INSPECTOR_SCHEMA_VERSION,
        )

        self.assertEqual(
            inspection.repository_path,
            str(
                self.repository
            ),
        )

        self.assertEqual(
            inspection.branch,
            "main",
        )

        self.assertEqual(
            len(
                inspection.head
            ),
            40,
        )

        self.assertEqual(
            [
                item.path
                for item in inspection.file_states
            ],
            [
                "alpha.txt",
                "tests/test_alpha.py",
            ],
        )

        alpha = inspection.file_states[0]
        added = inspection.file_states[1]

        self.assertTrue(
            alpha.exists
        )

        self.assertEqual(
            alpha.bytes,
            len(
                "old alpha\n".encode(
                    "utf-8"
                )
            ),
        )

        self.assertEqual(
            alpha.sha256,
            _sha(
                "old alpha\n"
            ),
        )

        self.assertFalse(
            added.exists
        )

        self.assertIsNone(
            added.bytes
        )

        self.assertIsNone(
            added.sha256
        )

        self.assertEqual(
            alpha.mode,
            (
                self.repository
                / "alpha.txt"
            ).stat().st_mode
            & 0o777,
        )

        self.assertIsNone(
            added.mode
        )

    def test_inspection_feeds_immutable_proposal(self):
        candidate = self._candidate()

        inspection = inspect_lab_promotion_repository(
            candidate=candidate,
            repository_path=str(
                self.repository
            ),
        )

        proposal = build_lab_promotion_proposal(
            candidate=candidate,
            repository_path=(
                inspection.repository_path
            ),
            repository_device=(
                inspection.repository_device
            ),
            repository_inode=(
                inspection.repository_inode
            ),
            branch=inspection.branch,
            head=inspection.head,
            before_states=(
                inspection.file_states
            ),
        )

        self.assertEqual(
            proposal.candidate_id,
            inspection.candidate_id,
        )

        self.assertEqual(
            [
                item.operation
                for item in proposal.files
            ],
            [
                PROMOTION_OPERATION_MODIFY,
                PROMOTION_OPERATION_ADD,
            ],
        )

    def test_untracked_work_refuses_inspection(self):
        (
            self.repository
            / "unknown.txt"
        ).write_text(
            "unknown\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "completely clean",
        ):
            inspect_lab_promotion_repository(
                candidate=self._candidate(),
                repository_path=str(
                    self.repository
                ),
            )

    def test_modified_tracked_work_refuses_inspection(self):
        (
            self.repository
            / "alpha.txt"
        ).write_text(
            "human work\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "completely clean",
        ):
            inspect_lab_promotion_repository(
                candidate=self._candidate(),
                repository_path=str(
                    self.repository
                ),
            )

    def test_staged_work_refuses_inspection(self):
        (
            self.repository
            / "alpha.txt"
        ).write_text(
            "staged work\n",
            encoding="utf-8",
        )

        _run_git(
            self.repository,
            "add",
            "alpha.txt",
        )

        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "completely clean",
        ):
            inspect_lab_promotion_repository(
                candidate=self._candidate(),
                repository_path=str(
                    self.repository
                ),
            )

    def test_detached_head_refuses_inspection(self):
        _run_git(
            self.repository,
            "checkout",
            "--detach",
        )

        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "attached Git branch",
        ):
            inspect_lab_promotion_repository(
                candidate=self._candidate(),
                repository_path=str(
                    self.repository
                ),
            )

    def test_missing_destination_parent_refuses_inspection(self):
        candidate = self._candidate(
            add_path="missing/test_alpha.py"
        )

        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "parent component",
        ):
            inspect_lab_promotion_repository(
                candidate=candidate,
                repository_path=str(
                    self.repository
                ),
            )

    def test_tracked_symlink_destination_refuses_inspection(self):
        (
            self.repository
            / "link-target.txt"
        ).write_text(
            "target\n",
            encoding="utf-8",
        )

        (
            self.repository
            / "linked.txt"
        ).symlink_to(
            "link-target.txt"
        )

        _run_git(
            self.repository,
            "add",
            "link-target.txt",
            "linked.txt",
        )

        _run_git(
            self.repository,
            "-c",
            "user.name=Auto Lab Test",
            "-c",
            "user.email=auto-lab@example.invalid",
            "commit",
            "-m",
            "Add tracked symlink",
        )

        candidate = self._candidate(
            alpha_path="linked.txt"
        )

        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "must not be a symlink",
        ):
            inspect_lab_promotion_repository(
                candidate=candidate,
                repository_path=str(
                    self.repository
                ),
            )

    def test_symlink_repository_path_refuses_inspection(self):
        alias = (
            self.root
            / "repo-alias"
        )

        alias.symlink_to(
            self.repository,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            LabPromotionInspectorError,
            "canonical",
        ):
            inspect_lab_promotion_repository(
                candidate=self._candidate(),
                repository_path=str(
                    alias
                ),
            )



    def test_git_branch_or_head_change_during_inspection_fails_closed(self):
        snapshots = (
            ("main", "1" * 40),
            ("changed", "2" * 40),
        )
        with patch(
            "hands_free_auto_lab.lab_promotion_inspector._git_snapshot",
            side_effect=snapshots,
        ):
            with self.assertRaisesRegex(
                LabPromotionInspectorError,
                "Git identity changed during inspection",
            ):
                inspect_lab_promotion_repository(
                    candidate=self._coding_candidate(),
                    repository_path=str(self.repository),
                )

    def test_special_bits_on_repository_destination_fail_closed(self):
        for special_bit in (0o4000, 0o2000, 0o1000):
            with self.subTest(special_bit=oct(special_bit)):
                os.chmod(self.repository / "alpha.txt", special_bit | 0o644)
                with self.assertRaisesRegex(
                    LabPromotionInspectorError,
                    "refuses setuid, setgid, or sticky mode bits",
                ):
                    inspect_lab_promotion_repository(
                        candidate=self._coding_candidate(),
                        repository_path=str(self.repository),
                    )

    def test_group_or_world_writable_destination_fails_closed(self):
        for unsafe_mode in (0o664, 0o646, 0o666):
            with self.subTest(unsafe_mode=oct(unsafe_mode)):
                os.chmod(self.repository / "alpha.txt", unsafe_mode)
                with self.assertRaisesRegex(
                    LabPromotionInspectorError,
                    "group- or other-writable",
                ):
                    inspect_lab_promotion_repository(
                        candidate=self._coding_candidate(),
                        repository_path=str(self.repository),
                    )
if __name__ == "__main__":
    unittest.main()
