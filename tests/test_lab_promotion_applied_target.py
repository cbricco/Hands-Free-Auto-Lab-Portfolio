from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import hands_free_auto_lab.lab_promotion_applied_target as applied

from hands_free_auto_lab.lab_promotion_applied_target import (
    DESTINATION_AFTER,
    DESTINATION_BEFORE,
    DESTINATION_OTHER,
    LabPromotionAppliedTargetError,
    inspect_promotion_applied_target,
)
from hands_free_auto_lab.lab_promotion_prepared_target import (
    TEMP_ABSENT,
    TEMP_EXACT,
    TEMP_UNEXPECTED,
)
from hands_free_auto_lab.lab_promotion_proposal import (
    LabPromotionProposal,
    LabPromotionProposalFile,
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
    PROMOTION_PROPOSAL_COMPONENT,
    PROMOTION_PROPOSAL_SCHEMA_VERSION,
    _PROPOSAL_ID_DOMAIN,
    _canonical_json_bytes,
    _proposal_identity_object,
)
from hands_free_auto_lab.lab_promotion_recovery_materials import (
    create_promotion_recovery_materials,
    initialize_promotion_recovery_materials_root,
)
from hands_free_auto_lab.lab_promotion_transaction_journal import (
    append_promotion_transaction_snapshot,
    create_promotion_transaction_journal,
    initialize_promotion_transaction_journal_root,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_PENDING,
    STATE_APPLYING,
    build_lab_promotion_transaction,
    transition_lab_promotion_transaction,
    PROGRESS_RESTORED,
    STATE_VERIFYING,
    STATE_ROLLING_BACK,
)


GIT = "/usr/bin/git"

OLD_ALPHA = b"old alpha\n"
NEW_ALPHA = b"new alpha\n"
NEW_TEST = b"assert True\n"

CANDIDATE_ID = "a" * 64
RUN_ID = "9" * 64


def sha(
    raw: bytes,
) -> str:
    return hashlib.sha256(
        raw
    ).hexdigest()


class LabPromotionAppliedTargetTests(
    unittest.TestCase
):
    SOURCE_KIND = "legacy_write_action"
    SOURCE_IDS = ("b" * 64, "c" * 64)

    def setUp(self):
        self.base = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-applied-target-"
            )
        )

        self.repository = (
            self.base
            / "target"
        )

        self.repository.mkdir()

        self.git(
            "init",
            "-b",
            "main",
        )

        self.git(
            "config",
            "user.email",
            "auto-lab@example.invalid",
        )

        self.git(
            "config",
            "user.name",
            "Auto Lab Test",
        )

        alpha = (
            self.repository
            / "alpha.txt"
        )

        alpha.write_bytes(
            OLD_ALPHA
        )

        alpha.chmod(
            0o755
        )

        tests_dir = (
            self.repository
            / "tests"
        )

        tests_dir.mkdir()

        (
            tests_dir
            / ".keep"
        ).write_text(
            "",
            encoding="utf-8",
        )

        self.git(
            "add",
            "alpha.txt",
            "tests/.keep",
        )

        self.git(
            "commit",
            "-m",
            "baseline",
        )

        head = self.git(
            "rev-parse",
            "HEAD",
            capture=True,
        ).strip()

        repo_state = self.repository.stat()

        files = (
            LabPromotionProposalFile(
                operation=PROMOTION_OPERATION_MODIFY,
                path="alpha.txt",
                before_exists=True,
                before_bytes=len(
                    OLD_ALPHA
                ),
                before_sha256=sha(
                    OLD_ALPHA
                ),
                before_mode=0o755,
                after_bytes=len(
                    NEW_ALPHA
                ),
                after_sha256=sha(
                    NEW_ALPHA
                ),
                after_mode=0o755,
                after_content=NEW_ALPHA.decode(
                    "utf-8"
                ),
                source_kind=self.SOURCE_KIND,
                source_id=self.SOURCE_IDS[0],
            ),
            LabPromotionProposalFile(
                operation=PROMOTION_OPERATION_ADD,
                path="tests/test_alpha.py",
                before_exists=False,
                before_bytes=None,
                before_sha256=None,
                before_mode=None,
                after_bytes=len(
                    NEW_TEST
                ),
                after_sha256=sha(
                    NEW_TEST
                ),
                after_mode=0o644,
                after_content=NEW_TEST.decode(
                    "utf-8"
                ),
                source_kind=self.SOURCE_KIND,
                source_id=self.SOURCE_IDS[1],
            ),
        )

        identity = _proposal_identity_object(
            candidate_id=CANDIDATE_ID,
            repository_path=str(
                self.repository
            ),
            repository_device=repo_state.st_dev,
            repository_inode=repo_state.st_ino,
            branch="main",
            head=head,
            files=files,
        )

        proposal_id = hashlib.sha256(
            _PROPOSAL_ID_DOMAIN
            + _canonical_json_bytes(
                identity
            )
        ).hexdigest()

        proposal = LabPromotionProposal(
            component=PROMOTION_PROPOSAL_COMPONENT,
            schema_version=PROMOTION_PROPOSAL_SCHEMA_VERSION,
            proposal_id=proposal_id,
            candidate_id=CANDIDATE_ID,
            repository_path=str(
                self.repository
            ),
            repository_device=repo_state.st_dev,
            repository_inode=repo_state.st_ino,
            branch="main",
            head=head,
            files=files,
        )

        prepared = build_lab_promotion_transaction(
            proposal=proposal,
            run_id=RUN_ID,
        )

        self.journal_root = (
            self.base
            / "journal"
        )

        self.journal_root.mkdir(
            mode=0o700
        )

        initialize_promotion_transaction_journal_root(
            self.journal_root
        )

        create_promotion_transaction_journal(
            self.journal_root,
            transaction=prepared,
        )

        self.recovery_root = (
            self.base
            / "recovery"
        )

        self.recovery_root.mkdir(
            mode=0o700
        )

        initialize_promotion_recovery_materials_root(
            self.recovery_root
        )

        self.materials = create_promotion_recovery_materials(
            self.recovery_root,
            transaction=prepared,
            preimages={
                "alpha.txt": OLD_ALPHA,
            },
        )

        applying = transition_lab_promotion_transaction(
            prepared,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_PENDING
                for item in prepared.files
            },
        )

        append_promotion_transaction_snapshot(
            self.journal_root,
            successor=applying,
            expected_previous_snapshot_id=prepared.snapshot_id,
        )

        self.transaction = applying

        self.seed_exact_temp(
            0
        )

        self.seed_exact_temp(
            1
        )

    def tearDown(self):
        shutil.rmtree(
            self.base
        )

    def git(
        self,
        *arguments: str,
        capture: bool = False,
    ) -> str:
        completed = subprocess.run(
            [
                GIT,
                "-C",
                str(
                    self.repository
                ),
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(
                    self.base
                ),
                "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )

        if not capture:
            return ""

        return completed.stdout.decode(
            "utf-8"
        )

    def temp_path(
        self,
        index: int,
    ) -> Path:
        destination = Path(
            self.materials.files[
                index
            ].path
        )

        return (
            self.repository
            / destination.parent
            / self.materials.files[
                index
            ].after_temp_name
        )

    def destination_path(
        self,
        index: int,
    ) -> Path:
        return (
            self.repository
            / self.transaction.files[
                index
            ].path
        )

    def seed_exact_temp(
        self,
        index: int,
    ) -> Path:
        material = self.materials.files[
            index
        ]

        transaction_file = self.transaction.files[
            index
        ]

        path = self.temp_path(
            index
        )

        path.write_bytes(
            transaction_file.after_content.encode(
                "utf-8"
            )
        )

        path.chmod(
            material.after_mode
        )

        return path

    def inspect(
        self,
        *,
        transaction=None,
    ):
        if transaction is None:
            transaction = self.transaction

        return inspect_promotion_applied_target(
            journal_root=self.journal_root,
            recovery_root=self.recovery_root,
            transaction=transaction,
        )

    def advance_first_installed(self):
        successor = transition_lab_promotion_transaction(
            self.transaction,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        append_promotion_transaction_snapshot(
            self.journal_root,
            successor=successor,
            expected_previous_snapshot_id=self.transaction.snapshot_id,
        )

        self.transaction = successor

    def test_all_pending_reports_before_and_exact_temps(self):
        inspection = self.inspect()

        self.assertEqual(
            [
                item.destination.status
                for item in inspection.files
            ],
            [
                DESTINATION_BEFORE,
                DESTINATION_BEFORE,
            ],
        )

        self.assertEqual(
            [
                item.temporary.status
                for item in inspection.files
            ],
            [
                TEMP_EXACT,
                TEMP_EXACT,
            ],
        )

        self.assertTrue(
            all(
                item.journal_progress
                == PROGRESS_PENDING
                for item in inspection.files
            )
        )

    def test_modify_replace_before_journal_progress_is_observable(self):
        os.replace(
            self.temp_path(
                0
            ),
            self.destination_path(
                0
            ),
        )

        inspection = self.inspect()

        first = inspection.files[
            0
        ]

        self.assertEqual(
            first.journal_progress,
            PROGRESS_PENDING,
        )

        self.assertEqual(
            first.destination.status,
            DESTINATION_AFTER,
        )

        self.assertEqual(
            first.temporary.status,
            TEMP_ABSENT,
        )

    def test_add_replace_before_journal_progress_is_observable(self):
        os.replace(
            self.temp_path(
                1
            ),
            self.destination_path(
                1
            ),
        )

        inspection = self.inspect()

        second = inspection.files[
            1
        ]

        self.assertEqual(
            second.journal_progress,
            PROGRESS_PENDING,
        )

        self.assertEqual(
            second.destination.status,
            DESTINATION_AFTER,
        )

        self.assertEqual(
            second.temporary.status,
            TEMP_ABSENT,
        )

    def test_partial_apply_mixed_physical_state_is_observable(self):
        os.replace(
            self.temp_path(
                0
            ),
            self.destination_path(
                0
            ),
        )

        inspection = self.inspect()

        self.assertEqual(
            [
                (
                    item.destination.status,
                    item.temporary.status,
                )
                for item in inspection.files
            ],
            [
                (
                    DESTINATION_AFTER,
                    TEMP_ABSENT,
                ),
                (
                    DESTINATION_BEFORE,
                    TEMP_EXACT,
                ),
            ],
        )

    def test_installed_journal_progress_can_match_after_state(self):
        os.replace(
            self.temp_path(
                0
            ),
            self.destination_path(
                0
            ),
        )

        self.advance_first_installed()

        inspection = self.inspect()

        first = inspection.files[
            0
        ]

        self.assertEqual(
            first.journal_progress,
            PROGRESS_INSTALLED,
        )

        self.assertEqual(
            first.destination.status,
            DESTINATION_AFTER,
        )

        self.assertEqual(
            first.temporary.status,
            TEMP_ABSENT,
        )

    def test_wrong_destination_content_is_other(self):
        self.destination_path(
            0
        ).write_bytes(
            b"wrong\n"
        )

        self.destination_path(
            0
        ).chmod(
            0o755
        )

        inspection = self.inspect()

        self.assertEqual(
            inspection.files[
                0
            ].destination.status,
            DESTINATION_OTHER,
        )

    def test_symlink_destination_is_other_without_following(self):
        outside = (
            self.base
            / "outside"
        )

        outside.write_bytes(
            NEW_ALPHA
        )

        self.destination_path(
            0
        ).unlink()

        self.destination_path(
            0
        ).symlink_to(
            outside
        )

        inspection = self.inspect()

        self.assertEqual(
            inspection.files[
                0
            ].destination.status,
            DESTINATION_OTHER,
        )

    def test_hardlinked_destination_is_other(self):
        outside = (
            self.base
            / "outside-hardlink"
        )

        outside.write_bytes(
            NEW_ALPHA
        )

        outside.chmod(
            0o755
        )

        self.destination_path(
            0
        ).unlink()

        os.link(
            outside,
            self.destination_path(
                0
            ),
        )

        inspection = self.inspect()

        self.assertEqual(
            inspection.files[
                0
            ].destination.status,
            DESTINATION_OTHER,
        )

    def test_wrong_temp_is_unexpected(self):
        self.temp_path(
            0
        ).write_bytes(
            b"wrong temp\n"
        )

        self.temp_path(
            0
        ).chmod(
            0o755
        )

        inspection = self.inspect()

        self.assertEqual(
            inspection.files[
                0
            ].temporary.status,
            TEMP_UNEXPECTED,
        )

    def test_foreign_untracked_state_is_refused(self):
        (
            self.repository
            / "foreign.txt"
        ).write_text(
            "foreign\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabPromotionAppliedTargetError,
            "unrelated untracked",
        ):
            self.inspect()

    def test_staged_expected_destination_is_refused(self):
        self.destination_path(
            0
        ).write_bytes(
            NEW_ALPHA
        )

        self.destination_path(
            0
        ).chmod(
            0o755
        )

        self.git(
            "add",
            "alpha.txt",
        )

        with self.assertRaisesRegex(
            LabPromotionAppliedTargetError,
            "staged/index",
        ):
            self.inspect()

    def test_branch_change_is_refused(self):
        self.git(
            "checkout",
            "-b",
            "other",
        )

        with self.assertRaisesRegex(
            LabPromotionAppliedTargetError,
            "Git snapshot|branch",
        ):
            self.inspect()

    def test_head_change_is_refused(self):
        self.git(
            "commit",
            "--allow-empty",
            "-m",
            "drift",
        )

        with self.assertRaisesRegex(
            LabPromotionAppliedTargetError,
            "Git snapshot|HEAD",
        ):
            self.inspect()

    def test_stale_journal_snapshot_is_refused(self):
        old = self.transaction

        self.advance_first_installed()

        with self.assertRaisesRegex(
            LabPromotionAppliedTargetError,
            "journal does not match",
        ):
            self.inspect(
                transaction=old,
            )

    def test_runtime_is_read_only(self):
        source = Path(
            applied.__file__
        ).read_text(
            encoding="utf-8"
        )

        forbidden = (
            "O_WRONLY",
            "O_RDWR",
            "O_CREAT",
            "O_EXCL",
            "os.write",
            "os.replace",
            "os.rename",
            "os.unlink",
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.chmod",
            "os.fchmod",
            "git add",
            "git commit",
            "git push",
            "append_promotion_transaction_snapshot",
            "transition_lab_promotion_transaction",
        )

        for token in forbidden:
            self.assertNotIn(
                token,
                source,
            )

    def test_verifying_state_is_observable(self):
        os.replace(
            self.temp_path(
                0
            ),
            self.destination_path(
                0
            ),
        )

        os.replace(
            self.temp_path(
                1
            ),
            self.destination_path(
                1
            ),
        )

        installed = transition_lab_promotion_transaction(
            self.transaction,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_INSTALLED
                for item in self.transaction.files
            },
        )

        append_promotion_transaction_snapshot(
            self.journal_root,
            successor=installed,
            expected_previous_snapshot_id=self.transaction.snapshot_id,
        )

        verifying = transition_lab_promotion_transaction(
            installed,
            state=STATE_VERIFYING,
            progress_by_path={
                item.path: item.progress
                for item in installed.files
            },
        )

        append_promotion_transaction_snapshot(
            self.journal_root,
            successor=verifying,
            expected_previous_snapshot_id=installed.snapshot_id,
        )

        self.transaction = verifying

        inspection = self.inspect()

        self.assertEqual(
            [
                item.destination.status
                for item in inspection.files
            ],
            [
                DESTINATION_AFTER,
                DESTINATION_AFTER,
            ],
        )

        self.assertEqual(
            [
                item.temporary.status
                for item in inspection.files
            ],
            [
                TEMP_ABSENT,
                TEMP_ABSENT,
            ],
        )

    def test_rolling_back_state_is_observable(self):
        os.replace(
            self.temp_path(
                0
            ),
            self.destination_path(
                0
            ),
        )

        self.advance_first_installed()

        rolling_back = transition_lab_promotion_transaction(
            self.transaction,
            state=STATE_ROLLING_BACK,
            progress_by_path={
                item.path: item.progress
                for item in self.transaction.files
            },
        )

        append_promotion_transaction_snapshot(
            self.journal_root,
            successor=rolling_back,
            expected_previous_snapshot_id=self.transaction.snapshot_id,
        )

        self.transaction = rolling_back

        inspection = self.inspect()

        first = inspection.files[
            0
        ]

        second = inspection.files[
            1
        ]

        self.assertEqual(
            first.journal_progress,
            PROGRESS_INSTALLED,
        )

        self.assertEqual(
            first.destination.status,
            DESTINATION_AFTER,
        )

        self.assertEqual(
            first.temporary.status,
            TEMP_ABSENT,
        )

        self.assertEqual(
            second.journal_progress,
            PROGRESS_PENDING,
        )

        self.assertEqual(
            second.destination.status,
            DESTINATION_BEFORE,
        )

        self.assertEqual(
            second.temporary.status,
            TEMP_EXACT,
        )

    def test_rolling_back_restored_modify_and_pending_untouched_are_observable(self):
        os.replace(
            self.temp_path(
                0
            ),
            self.destination_path(
                0
            ),
        )

        self.advance_first_installed()

        rolling_back = transition_lab_promotion_transaction(
            self.transaction,
            state=STATE_ROLLING_BACK,
            progress_by_path={
                item.path: item.progress
                for item in self.transaction.files
            },
        )

        append_promotion_transaction_snapshot(
            self.journal_root,
            successor=rolling_back,
            expected_previous_snapshot_id=self.transaction.snapshot_id,
        )

        self.destination_path(
            0
        ).write_bytes(
            OLD_ALPHA
        )

        self.destination_path(
            0
        ).chmod(
            0o755
        )

        restored = transition_lab_promotion_transaction(
            rolling_back,
            state=STATE_ROLLING_BACK,
            progress_by_path={
                "alpha.txt": PROGRESS_RESTORED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        append_promotion_transaction_snapshot(
            self.journal_root,
            successor=restored,
            expected_previous_snapshot_id=rolling_back.snapshot_id,
        )

        self.transaction = restored

        inspection = self.inspect()

        first = inspection.files[
            0
        ]

        second = inspection.files[
            1
        ]

        self.assertEqual(
            first.journal_progress,
            PROGRESS_RESTORED,
        )

        self.assertEqual(
            first.destination.status,
            DESTINATION_BEFORE,
        )

        self.assertEqual(
            first.temporary.status,
            TEMP_ABSENT,
        )

        self.assertEqual(
            second.journal_progress,
            PROGRESS_PENDING,
        )

        self.assertEqual(
            second.destination.status,
            DESTINATION_BEFORE,
        )

        self.assertEqual(
            second.temporary.status,
            TEMP_EXACT,
        )


class LabCodingCandidatePromotionAppliedTargetTests(
    LabPromotionAppliedTargetTests
):
    SOURCE_KIND = "coding_candidate"
    SOURCE_IDS = (CANDIDATE_ID, CANDIDATE_ID)


if __name__ == "__main__":
    unittest.main()
