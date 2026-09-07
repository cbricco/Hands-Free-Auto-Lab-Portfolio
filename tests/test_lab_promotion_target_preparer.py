from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import hands_free_auto_lab.lab_promotion_target_preparer as preparer

from hands_free_auto_lab.lab_promotion_approval_store import (
    LabPromotionApprovalStoreError,
    create_promotion_challenge,
    decide_promotion_challenge,
    initialize_promotion_approval_state_root,
    query_promotion_approval,
)
from hands_free_auto_lab.lab_promotion_prepared_target import (
    TEMP_ABSENT,
    TEMP_EXACT,
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
from hands_free_auto_lab.lab_promotion_target_preparer import (
    LabPromotionTargetPreparationError,
    prepare_promotion_target,
)
from hands_free_auto_lab.lab_promotion_transaction_journal import (
    create_promotion_transaction_journal,
    initialize_promotion_transaction_journal_root,
    load_promotion_transaction_journal,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    STATE_PREPARED,
    PROGRESS_PENDING,
    build_lab_promotion_transaction,
)


GIT = "/usr/bin/git"

OLD_ALPHA = b"old alpha\n"
NEW_ALPHA = b"new alpha\n"
NEW_TEST = b"assert True\n"

CANDIDATE_ID = "a" * 64
RUN_ID = "9" * 64
OTHER_PROPOSAL_ID = "d" * 64


def sha(
    raw: bytes,
) -> str:
    return hashlib.sha256(
        raw
    ).hexdigest()


class LabPromotionTargetPreparerTests(
    unittest.TestCase
):
    SOURCE_KIND = "legacy_write_action"
    SOURCE_IDS = ("b" * 64, "c" * 64)

    def setUp(self):
        self.base = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-target-preparer-"
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

        self.transaction = build_lab_promotion_transaction(
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
            transaction=self.transaction,
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

        self.approval_root = (
            self.base
            / "approval"
        )

        self.approval_root.mkdir(
            mode=0o700
        )

        initialize_promotion_approval_state_root(
            self.approval_root
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

    def approve(
        self,
        *,
        proposal_id: str | None = None,
    ):
        if proposal_id is None:
            proposal_id = self.transaction.proposal_id

        challenge = create_promotion_challenge(
            self.approval_root,
            proposal_id=proposal_id,
        )

        result = decide_promotion_challenge(
            self.approval_root,
            challenge_id=challenge["challenge_id"],
            proposal_id=proposal_id,
            decision="approve",
        )

        return result["approval"]

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

    def seed_exact_temp(
        self,
        index: int,
    ) -> Path:
        transaction_file = self.transaction.files[
            index
        ]

        material = self.materials.files[
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

    def prepare(
        self,
        approval_id: str,
        *,
        journal_root=None,
    ):
        if journal_root is None:
            journal_root = self.journal_root

        return prepare_promotion_target(
            approval_root=self.approval_root,
            journal_root=journal_root,
            recovery_root=self.recovery_root,
            transaction=self.transaction,
            approval_id=approval_id,
        )

    def query(
        self,
        approval_id: str,
        *,
        proposal_id: str | None = None,
    ):
        if proposal_id is None:
            proposal_id = self.transaction.proposal_id

        return query_promotion_approval(
            self.approval_root,
            approval_id=approval_id,
            proposal_id=proposal_id,
        )

    def test_success_creates_exact_temps_without_installing_destinations(self):
        approval = self.approve()

        result = self.prepare(
            approval["approval_id"]
        )

        self.assertEqual(
            [
                item.status
                for item in result.temps
            ],
            [
                TEMP_EXACT,
                TEMP_EXACT,
            ],
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            OLD_ALPHA,
        )

        self.assertFalse(
            (
                self.repository
                / "tests"
                / "test_alpha.py"
            ).exists()
        )

        self.assertEqual(
            self.temp_path(
                0
            ).stat().st_mode
            & 0o777,
            0o755,
        )

        self.assertEqual(
            self.temp_path(
                1
            ).stat().st_mode
            & 0o777,
            0o644,
        )

        current = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=self.transaction.transaction_id,
        )

        self.assertEqual(
            current,
            self.transaction,
        )

        self.assertEqual(
            current.state,
            STATE_PREPARED,
        )

        self.assertTrue(
            all(
                item.progress
                == PROGRESS_PENDING
                for item in current.files
            )
        )

        queried = self.query(
            approval["approval_id"]
        )

        self.assertEqual(
            queried["status"],
            "consumed",
        )

        self.assertEqual(
            queried["terminal"]["run_id"],
            self.transaction.run_id,
        )

    def test_existing_exact_temp_refused_before_consuming_approval(self):
        approval = self.approve()

        self.seed_exact_temp(
            0
        )

        with self.assertRaisesRegex(
            LabPromotionTargetPreparationError,
            "temporaries ABSENT",
        ):
            self.prepare(
                approval["approval_id"]
            )

        queried = self.query(
            approval["approval_id"]
        )

        self.assertEqual(
            queried["status"],
            "approved",
        )

    def test_foreign_untracked_state_refused_before_consuming_approval(self):
        approval = self.approve()

        (
            self.repository
            / "foreign.txt"
        ).write_text(
            "foreign\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabPromotionTargetPreparationError,
            "preflight",
        ):
            self.prepare(
                approval["approval_id"]
            )

        queried = self.query(
            approval["approval_id"]
        )

        self.assertEqual(
            queried["status"],
            "approved",
        )

    def test_wrong_proposal_approval_refused_without_target_temps(self):
        approval = self.approve(
            proposal_id=OTHER_PROPOSAL_ID,
        )

        with self.assertRaisesRegex(
            LabPromotionTargetPreparationError,
            "approval consumption",
        ):
            self.prepare(
                approval["approval_id"]
            )

        self.assertFalse(
            self.temp_path(
                0
            ).exists()
        )

        self.assertFalse(
            self.temp_path(
                1
            ).exists()
        )

    def test_missing_exact_journal_refused_before_consuming_approval(self):
        approval = self.approve()

        empty_root = (
            self.base
            / "empty-journal"
        )

        empty_root.mkdir(
            mode=0o700
        )

        initialize_promotion_transaction_journal_root(
            empty_root
        )

        with self.assertRaisesRegex(
            LabPromotionTargetPreparationError,
            "journal",
        ):
            self.prepare(
                approval["approval_id"],
                journal_root=empty_root,
            )

        queried = self.query(
            approval["approval_id"]
        )

        self.assertEqual(
            queried["status"],
            "approved",
        )

    def test_consumed_approval_cannot_be_reused_even_if_tests_remove_temps(self):
        approval = self.approve()

        self.prepare(
            approval["approval_id"]
        )

        self.temp_path(
            0
        ).unlink()

        self.temp_path(
            1
        ).unlink()

        with self.assertRaisesRegex(
            LabPromotionTargetPreparationError,
            "approval consumption",
        ):
            self.prepare(
                approval["approval_id"]
            )

        self.assertFalse(
            self.temp_path(
                0
            ).exists()
        )

        self.assertFalse(
            self.temp_path(
                1
            ).exists()
        )

    def test_partial_failure_leaves_exact_temp_and_consumed_approval(self):
        approval = self.approve()

        original = preparer._prepare_one_temp
        calls = 0

        def fail_second(
            repository_fd,
            *,
            destination_path,
            temp_name,
            data,
            expected_mode,
        ):
            nonlocal calls
            calls += 1

            if calls == 2:
                raise LabPromotionTargetPreparationError(
                    "simulated second-temp failure"
                )

            return original(
                repository_fd,
                destination_path=destination_path,
                temp_name=temp_name,
                data=data,
                expected_mode=expected_mode,
            )

        with mock.patch.object(
            preparer,
            "_prepare_one_temp",
            side_effect=fail_second,
        ):
            with self.assertRaisesRegex(
                LabPromotionTargetPreparationError,
                "after approval consumption",
            ):
                self.prepare(
                    approval["approval_id"]
                )

        queried = self.query(
            approval["approval_id"]
        )

        self.assertEqual(
            queried["status"],
            "consumed",
        )

        inspection = inspect_promotion_prepared_target(
            recovery_root=self.recovery_root,
            transaction=self.transaction,
        )

        self.assertEqual(
            [
                item.status
                for item in inspection.temps
            ],
            [
                TEMP_EXACT,
                TEMP_ABSENT,
            ],
        )

    def test_candidate_provenance_partial_failure_preserves_interrupted_state(self):
        fixture = type(self)(
            "test_partial_failure_leaves_exact_temp_and_consumed_approval"
        )
        fixture.SOURCE_KIND = "coding_candidate"
        fixture.SOURCE_IDS = (CANDIDATE_ID, CANDIDATE_ID)
        fixture.setUp()
        try:
            self.assertTrue(
                all(
                    item.source_kind == "coding_candidate"
                    and item.source_id == CANDIDATE_ID
                    for item in fixture.transaction.files
                )
            )
            fixture.test_partial_failure_leaves_exact_temp_and_consumed_approval()
        finally:
            fixture.tearDown()

    def test_post_consume_drift_is_refused_before_temp_writer(self):
        approval = self.approve()

        real_consume = preparer.consume_promotion_approval

        def consume_then_drift(
            root,
            *,
            approval_id,
            proposal_id,
            run_id,
        ):
            result = real_consume(
                root,
                approval_id=approval_id,
                proposal_id=proposal_id,
                run_id=run_id,
            )

            (
                self.repository
                / "foreign-after-consume.txt"
            ).write_text(
                "drift\n",
                encoding="utf-8",
            )

            return result

        with mock.patch.object(
            preparer,
            "consume_promotion_approval",
            side_effect=consume_then_drift,
        ):
            with self.assertRaisesRegex(
                LabPromotionTargetPreparationError,
                "after approval consumption",
            ):
                self.prepare(
                    approval["approval_id"]
                )

        queried = self.query(
            approval["approval_id"]
        )

        self.assertEqual(
            queried["status"],
            "consumed",
        )

        self.assertFalse(
            self.temp_path(
                0
            ).exists()
        )

        self.assertFalse(
            self.temp_path(
                1
            ).exists()
        )

    def test_racing_temp_collision_is_never_overwritten(self):
        approval = self.approve()

        original = preparer._prepare_one_temp
        injected = False

        def race_first(
            repository_fd,
            *,
            destination_path,
            temp_name,
            data,
            expected_mode,
        ):
            nonlocal injected

            if not injected:
                injected = True

                collision = self.temp_path(
                    0
                )

                collision.write_bytes(
                    b"attacker\n"
                )

                collision.chmod(
                    0o600
                )

            return original(
                repository_fd,
                destination_path=destination_path,
                temp_name=temp_name,
                data=data,
                expected_mode=expected_mode,
            )

        with mock.patch.object(
            preparer,
            "_prepare_one_temp",
            side_effect=race_first,
        ):
            with self.assertRaisesRegex(
                LabPromotionTargetPreparationError,
                "after approval consumption",
            ):
                self.prepare(
                    approval["approval_id"]
                )

        self.assertEqual(
            self.temp_path(
                0
            ).read_bytes(),
            b"attacker\n",
        )

        queried = self.query(
            approval["approval_id"]
        )

        self.assertEqual(
            queried["status"],
            "consumed",
        )

    def test_runtime_has_only_prepare_write_authority(self):
        source = Path(
            preparer.__file__
        ).read_text(
            encoding="utf-8"
        )

        required = (
            "O_CREAT",
            "O_EXCL",
            "O_NOFOLLOW",
            "os.fchmod",
            "os.write",
            "os.fsync",
            "consume_promotion_approval",
            "inspect_promotion_prepared_target",
        )

        forbidden = (
            "os.replace",
            "os.rename",
            "os.unlink",
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.chmod",
            "git add",
            "git commit",
            "git push",
            "append_promotion_transaction_snapshot",
            "transition_lab_promotion_transaction",
        )

        for token in required:
            self.assertIn(
                token,
                source,
            )

        for token in forbidden:
            self.assertNotIn(
                token,
                source,
            )


if __name__ == "__main__":
    unittest.main()
