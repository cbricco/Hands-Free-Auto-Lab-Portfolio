from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import hands_free_auto_lab.lab_promotion_prepared_target as prepared

from hands_free_auto_lab.lab_promotion_prepared_target import (
    LabPromotionPreparedTargetError,
    TEMP_ABSENT,
    TEMP_EXACT,
    TEMP_UNEXPECTED,
    inspect_promotion_prepared_target,
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
from hands_free_auto_lab.lab_promotion_transaction_state import (
    build_lab_promotion_transaction,
)


GIT = "/usr/bin/git"

OLD_ALPHA = b"old alpha\n"
NEW_ALPHA = b"new alpha\n"
NEW_TEST = b"assert True\n"

CANDIDATE_ID = "a" * 64
RUN_ID = "9" * 64


def sha(raw: bytes) -> str:
    return hashlib.sha256(
        raw
    ).hexdigest()


class LabPromotionPreparedTargetTests(
    unittest.TestCase
):
    SOURCE_KIND = "legacy_write_action"
    SOURCE_IDS = ("b" * 64, "c" * 64)

    def setUp(self):
        self.base = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-prepared-target-"
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

        (
            self.repository
            / "alpha.txt"
        ).write_bytes(
            OLD_ALPHA
        )

        (
            self.repository
            / "alpha.txt"
        ).chmod(
            0o644
        )

        (
            self.repository
            / "tests"
        ).mkdir()

        (
            self.repository
            / "tests"
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

        repository_state = self.repository.stat()

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
                before_mode=0o644,
                after_bytes=len(
                    NEW_ALPHA
                ),
                after_sha256=sha(
                    NEW_ALPHA
                ),
                after_mode=0o644,
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
            repository_device=repository_state.st_dev,
            repository_inode=repository_state.st_ino,
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
            repository_device=repository_state.st_dev,
            repository_inode=repository_state.st_ino,
            branch="main",
            head=head,
            files=files,
        )

        self.transaction = build_lab_promotion_transaction(
            proposal=proposal,
            run_id=RUN_ID,
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
            transaction=self.transaction,
            preimages={
                "alpha.txt": OLD_ALPHA,
            },
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

    def create_exact_temp(
        self,
        index: int,
    ) -> Path:
        material = self.materials.files[
            index
        ]

        transaction_file = self.transaction.files[
            index
        ]

        if material.path != transaction_file.path:
            raise AssertionError(
                "test fixture material/transaction path mismatch"
            )

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

    def inspect(self):
        return inspect_promotion_prepared_target(
            recovery_root=self.recovery_root,
            transaction=self.transaction,
        )

    def test_clean_target_reports_every_temp_absent(self):
        inspection = self.inspect()

        self.assertEqual(
            [
                item.status
                for item in inspection.temps
            ],
            [
                TEMP_ABSENT,
                TEMP_ABSENT,
            ],
        )

    def test_exact_modify_temp_is_accepted(self):
        self.create_exact_temp(
            0
        )

        inspection = self.inspect()

        self.assertEqual(
            inspection.temps[0].status,
            TEMP_EXACT,
        )

        self.assertEqual(
            inspection.temps[1].status,
            TEMP_ABSENT,
        )

    def test_partial_prepare_is_classified_exactly(self):
        self.create_exact_temp(
            1
        )

        inspection = self.inspect()

        self.assertEqual(
            [
                item.status
                for item in inspection.temps
            ],
            [
                TEMP_ABSENT,
                TEMP_EXACT,
            ],
        )

    def test_wrong_temp_content_is_unexpected(self):
        path = self.temp_path(
            0
        )

        path.write_bytes(
            b"wrong\n"
        )

        path.chmod(
            0o644
        )

        inspection = self.inspect()

        self.assertEqual(
            inspection.temps[0].status,
            TEMP_UNEXPECTED,
        )

    def test_wrong_temp_mode_is_unexpected(self):
        path = self.create_exact_temp(
            0
        )

        path.chmod(
            0o600
        )

        inspection = self.inspect()

        self.assertEqual(
            inspection.temps[0].status,
            TEMP_UNEXPECTED,
        )

    def test_symlink_temp_is_unexpected_without_following(self):
        outside = (
            self.base
            / "outside"
        )

        outside.write_bytes(
            NEW_ALPHA
        )

        self.temp_path(
            0
        ).symlink_to(
            outside
        )

        inspection = self.inspect()

        self.assertEqual(
            inspection.temps[0].status,
            TEMP_UNEXPECTED,
        )

    def test_hardlinked_temp_is_unexpected(self):
        outside = (
            self.base
            / "outside-hardlink"
        )

        outside.write_bytes(
            NEW_ALPHA
        )

        os.link(
            outside,
            self.temp_path(
                0
            ),
        )

        self.temp_path(
            0
        ).chmod(
            0o644
        )

        inspection = self.inspect()

        self.assertEqual(
            inspection.temps[0].status,
            TEMP_UNEXPECTED,
        )

    def test_unrelated_untracked_file_is_refused(self):
        (
            self.repository
            / "foreign.txt"
        ).write_text(
            "foreign\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabPromotionPreparedTargetError,
            "unrelated untracked",
        ):
            self.inspect()

    def test_tracked_change_is_refused(self):
        (
            self.repository
            / "alpha.txt"
        ).write_bytes(
            b"changed\n"
        )

        with self.assertRaisesRegex(
            LabPromotionPreparedTargetError,
            "tracked/index",
        ):
            self.inspect()

    def test_branch_change_is_refused(self):
        self.git(
            "checkout",
            "-b",
            "other",
        )

        with self.assertRaisesRegex(
            LabPromotionPreparedTargetError,
            "branch",
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
            LabPromotionPreparedTargetError,
            "HEAD",
        ):
            self.inspect()

    def test_add_destination_is_refused(self):
        (
            self.repository
            / "tests"
            / "test_alpha.py"
        ).write_bytes(
            NEW_TEST
        )

        with self.assertRaises(
            LabPromotionPreparedTargetError
        ):
            self.inspect()

    def test_expected_temp_must_not_be_ignored(self):
        exclude = (
            self.repository
            / ".git"
            / "info"
            / "exclude"
        )

        with exclude.open(
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write(
                ".hands-free-auto-lab-promote-*\n"
            )

        with self.assertRaisesRegex(
            LabPromotionPreparedTargetError,
            "ignored",
        ):
            self.inspect()

    def test_exact_temp_evidence_contains_hash_mode_and_bytes(self):
        self.create_exact_temp(
            0
        )

        inspection = self.inspect()
        observed = inspection.temps[
            0
        ]

        self.assertEqual(
            observed.observed_bytes,
            len(
                NEW_ALPHA
            ),
        )

        self.assertEqual(
            observed.observed_sha256,
            sha(
                NEW_ALPHA
            ),
        )

        self.assertEqual(
            observed.observed_mode,
            0o644,
        )

    def test_runtime_source_contains_no_target_write_primitives(self):
        source = Path(
            prepared.__file__
        ).read_text(
            encoding="utf-8"
        )

        forbidden = (
            "O_WRONLY",
            "O_RDWR",
            "O_CREAT",
            "O_EXCL",
            "os.write",
            "os.mkdir",
            "os.replace",
            "os.rename",
            "os.unlink",
            "os.remove",
            "os.rmdir",
            "os.chmod",
            "os.fchmod",
        )

        for token in forbidden:
            self.assertNotIn(
                token,
                source,
            )


class LabCodingCandidatePromotionPreparedTargetTests(
    LabPromotionPreparedTargetTests
):
    SOURCE_KIND = "coding_candidate"
    SOURCE_IDS = (CANDIDATE_ID, CANDIDATE_ID)


if __name__ == "__main__":
    unittest.main()
