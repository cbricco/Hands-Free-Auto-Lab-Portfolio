from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hands_free_auto_lab.lab_promotion_executor as executor
from hands_free_auto_lab.lab_promotion_approval_store import (
    create_promotion_challenge,
    decide_promotion_challenge,
    initialize_promotion_approval_state_root,
    query_promotion_approval,
)
from hands_free_auto_lab.lab_promotion_proposal import (
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
    PROMOTION_PROPOSAL_COMPONENT,
    PROMOTION_PROPOSAL_SCHEMA_VERSION,
    LabPromotionProposal,
    LabPromotionProposalFile,
    _PROPOSAL_ID_DOMAIN,
    _canonical_json_bytes,
    _proposal_identity_object,
)
from hands_free_auto_lab.lab_promotion_recovery_materials import (
    create_promotion_recovery_materials,
    initialize_promotion_recovery_materials_root,
)
from hands_free_auto_lab.lab_promotion_target_preparer import (
    prepare_promotion_target,
)
from hands_free_auto_lab.lab_promotion_transaction_journal import (
    LabPromotionTransactionJournalError,
    create_promotion_transaction_journal,
    initialize_promotion_transaction_journal_root,
    load_promotion_transaction_journal,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_PENDING,
    PROGRESS_VERIFIED,
    STATE_COMPLETED,
    STATE_PREPARED,
    STATE_RECOVERY_REQUIRED,
    build_lab_promotion_transaction,
)


OLD_ALPHA = b"old alpha\n"
NEW_ALPHA = b"new alpha\n"
OLD_TEST = b"def test_old():\n    assert True\n"
NEW_TEST = b"def test_new():\n    assert True\n"
CANDIDATE_ID = "c" * 64
RUN_ID = "d" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LabPromotionExecutorTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-promotion-executor-"
            )
        )
        os.chmod(self.base, 0o700)

        self.repository = self.base / "target"
        self.repository.mkdir(mode=0o700)

        self._git("init", "-b", "main")
        self._git(
            "config",
            "user.email",
            "auto-lab@example.invalid",
        )
        self._git(
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
            0o755
        )

        tests_dir = (
            self.repository
            / "tests"
        )
        tests_dir.mkdir()

        (
            tests_dir
            / "test_alpha.py"
        ).write_bytes(
            OLD_TEST
        )
        (
            tests_dir
            / "test_alpha.py"
        ).chmod(
            0o644
        )

        self._git(
            "add",
            "alpha.txt",
            "tests/test_alpha.py",
        )
        self._git(
            "commit",
            "-m",
            "baseline",
        )

        self.head = self._git(
            "rev-parse",
            "HEAD",
            capture=True,
        ).strip()

        self.files = (
            LabPromotionProposalFile(
                operation=PROMOTION_OPERATION_MODIFY,
                path="alpha.txt",
                before_exists=True,
                before_bytes=len(OLD_ALPHA),
                before_sha256=sha(OLD_ALPHA),
                before_mode=0o755,
                after_bytes=len(NEW_ALPHA),
                after_sha256=sha(NEW_ALPHA),
                after_mode=0o755,
                after_content=NEW_ALPHA.decode("utf-8"),
                source_kind="coding_candidate",
                source_id=CANDIDATE_ID,
            ),
            LabPromotionProposalFile(
                operation=PROMOTION_OPERATION_MODIFY,
                path="tests/test_alpha.py",
                before_exists=True,
                before_bytes=len(OLD_TEST),
                before_sha256=sha(OLD_TEST),
                before_mode=0o644,
                after_bytes=len(NEW_TEST),
                after_sha256=sha(NEW_TEST),
                after_mode=0o644,
                after_content=NEW_TEST.decode("utf-8"),
                source_kind="coding_candidate",
                source_id=CANDIDATE_ID,
            ),
        )

        self.proposal = self._proposal(
            self.files
        )

        self.transaction = (
            build_lab_promotion_transaction(
                proposal=self.proposal,
                run_id=RUN_ID,
            )
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

        self.materials = (
            create_promotion_recovery_materials(
                self.recovery_root,
                transaction=self.transaction,
                preimages={
                    "alpha.txt": OLD_ALPHA,
                    "tests/test_alpha.py": OLD_TEST,
                },
            )
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

        challenge = create_promotion_challenge(
            self.approval_root,
            proposal_id=self.proposal.proposal_id,
        )

        approval = decide_promotion_challenge(
            self.approval_root,
            challenge_id=challenge["challenge_id"],
            proposal_id=self.proposal.proposal_id,
            decision="approve",
        )[
            "approval"
        ]

        self.approval_id = approval[
            "approval_id"
        ]

        self.preparation = prepare_promotion_target(
            approval_root=self.approval_root,
            journal_root=self.journal_root,
            recovery_root=self.recovery_root,
            transaction=self.transaction,
            approval_id=self.approval_id,
        )

    def tearDown(self):
        shutil.rmtree(
            self.base
        )

    def _git(
        self,
        *args,
        capture=False,
    ):
        completed = subprocess.run(
            [
                "/usr/bin/git",
                *args,
            ],
            cwd=self.repository,
            check=True,
            stdout=(
                subprocess.PIPE
                if capture
                else subprocess.DEVNULL
            ),
            stderr=subprocess.PIPE,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.base),
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

    def _proposal(
        self,
        files,
    ):
        repository_state = (
            self.repository.stat()
        )

        frozen = tuple(
            files
        )

        identity = _proposal_identity_object(
            candidate_id=CANDIDATE_ID,
            repository_path=str(
                self.repository.resolve()
            ),
            repository_device=(
                repository_state.st_dev
            ),
            repository_inode=(
                repository_state.st_ino
            ),
            branch="main",
            head=self.head,
            files=frozen,
        )

        proposal_id = hashlib.sha256(
            _PROPOSAL_ID_DOMAIN
            + _canonical_json_bytes(
                identity
            )
        ).hexdigest()

        return LabPromotionProposal(
            component=(
                PROMOTION_PROPOSAL_COMPONENT
            ),
            schema_version=(
                PROMOTION_PROPOSAL_SCHEMA_VERSION
            ),
            proposal_id=proposal_id,
            candidate_id=CANDIDATE_ID,
            repository_path=str(
                self.repository.resolve()
            ),
            repository_device=(
                repository_state.st_dev
            ),
            repository_inode=(
                repository_state.st_ino
            ),
            branch="main",
            head=self.head,
            files=frozen,
        )

    def _temp_path(
        self,
        index,
    ):
        item = self.materials.files[
            index
        ]

        destination = Path(
            item.path
        )

        return (
            self.repository
            / destination.parent
            / item.after_temp_name
        )

    def _execute(self):
        return (
            executor.execute_lab_promotion_transaction(
                approval_root=self.approval_root,
                journal_root=self.journal_root,
                recovery_root=self.recovery_root,
                transaction=self.transaction,
                preparation=self.preparation,
            )
        )

    def test_success_installs_and_durably_verifies_modify_files(
        self,
    ):
        completed = self._execute()

        self.assertEqual(
            completed.state,
            STATE_COMPLETED,
        )

        self.assertTrue(
            all(
                item.progress
                == PROGRESS_VERIFIED
                for item in completed.files
            )
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            NEW_ALPHA,
        )

        self.assertEqual(
            (
                self.repository
                / "tests"
                / "test_alpha.py"
            ).read_bytes(),
            NEW_TEST,
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).stat().st_mode
            & 0o777,
            0o755,
        )

        self.assertEqual(
            (
                self.repository
                / "tests"
                / "test_alpha.py"
            ).stat().st_mode
            & 0o777,
            0o644,
        )

        self.assertFalse(
            self._temp_path(
                0
            ).exists()
        )

        self.assertFalse(
            self._temp_path(
                1
            ).exists()
        )

        durable = (
            load_promotion_transaction_journal(
                self.journal_root,
                transaction_id=(
                    self.transaction.transaction_id
                ),
            )
        )

        self.assertEqual(
            durable,
            completed,
        )

        queried = query_promotion_approval(
            self.approval_root,
            approval_id=self.approval_id,
            proposal_id=(
                self.proposal.proposal_id
            ),
        )

        self.assertEqual(
            queried["status"],
            "consumed",
        )

        self.assertEqual(
            queried["terminal"]["run_id"],
            self.transaction.run_id,
        )

    def test_add_is_refused_before_journal_or_repository_mutation(
        self,
    ):
        add_file = LabPromotionProposalFile(
            operation=PROMOTION_OPERATION_ADD,
            path="new.txt",
            before_exists=False,
            before_bytes=None,
            before_sha256=None,
            before_mode=None,
            after_bytes=len(
                b"new\n"
            ),
            after_sha256=sha(
                b"new\n"
            ),
            after_mode=0o644,
            after_content="new\n",
            source_kind="coding_candidate",
            source_id=CANDIDATE_ID,
        )

        add_transaction = (
            build_lab_promotion_transaction(
                proposal=self._proposal(
                    (
                        add_file,
                    )
                ),
                run_id="e" * 64,
            )
        )

        with self.assertRaisesRegex(
            executor.LabPromotionExecutorError,
            "supports MODIFY only",
        ):
            (
                executor.execute_lab_promotion_transaction(
                    approval_root=(
                        self.approval_root
                    ),
                    journal_root=(
                        self.journal_root
                    ),
                    recovery_root=(
                        self.recovery_root
                    ),
                    transaction=add_transaction,
                    preparation=(
                        self.preparation
                    ),
                )
            )

        self.assertFalse(
            (
                self.repository
                / "new.txt"
            ).exists()
        )

        current = (
            load_promotion_transaction_journal(
                self.journal_root,
                transaction_id=(
                    self.transaction.transaction_id
                ),
            )
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

    def test_failure_after_replace_marks_recovery_required_without_cleanup(
        self,
    ):
        real_append = (
            executor.append_promotion_transaction_snapshot
        )

        calls = 0

        def fail_first_installed(
            root,
            *,
            successor,
            expected_previous_snapshot_id,
        ):
            nonlocal calls
            calls += 1

            if calls == 2:
                raise (
                    LabPromotionTransactionJournalError(
                        "simulated journal failure "
                        "after physical install"
                    )
                )

            return real_append(
                root,
                successor=successor,
                expected_previous_snapshot_id=(
                    expected_previous_snapshot_id
                ),
            )

        with mock.patch.object(
            executor,
            "append_promotion_transaction_snapshot",
            side_effect=fail_first_installed,
        ):
            with self.assertRaisesRegex(
                executor.LabPromotionExecutorError,
                "RECOVERY_REQUIRED",
            ):
                self._execute()

        durable = (
            load_promotion_transaction_journal(
                self.journal_root,
                transaction_id=(
                    self.transaction.transaction_id
                ),
            )
        )

        self.assertEqual(
            durable.state,
            STATE_RECOVERY_REQUIRED,
        )

        self.assertTrue(
            all(
                item.progress
                == PROGRESS_PENDING
                for item in durable.files
            )
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            NEW_ALPHA,
        )

        self.assertEqual(
            (
                self.repository
                / "tests"
                / "test_alpha.py"
            ).read_bytes(),
            OLD_TEST,
        )

        self.assertFalse(
            self._temp_path(
                0
            ).exists()
        )

        self.assertTrue(
            self._temp_path(
                1
            ).exists()
        )

    def test_group_writable_repository_is_refused_before_applying(
        self,
    ):
        os.chmod(
            self.repository,
            0o770,
        )

        with self.assertRaisesRegex(
            executor.LabPromotionExecutorError,
            "group- or other-writable",
        ):
            self._execute()

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=self.transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_PREPARED,
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            OLD_ALPHA,
        )

        self.assertTrue(
            self._temp_path(
                0
            ).exists()
        )

        self.assertTrue(
            self._temp_path(
                1
            ).exists()
        )

    def test_group_writable_nested_parent_is_refused_before_applying(
        self,
    ):
        os.chmod(
            self.repository
            / "tests",
            0o770,
        )

        with self.assertRaisesRegex(
            executor.LabPromotionExecutorError,
            "group- or other-writable",
        ):
            self._execute()

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=self.transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_PREPARED,
        )

        self.assertEqual(
            (
                self.repository
                / "tests"
                / "test_alpha.py"
            ).read_bytes(),
            OLD_TEST,
        )

        self.assertTrue(
            self._temp_path(
                0
            ).exists()
        )

        self.assertTrue(
            self._temp_path(
                1
            ).exists()
        )

    def test_group_writable_git_directory_is_refused_before_applying(
        self,
    ):
        os.chmod(
            self.repository
            / ".git",
            0o770,
        )

        with self.assertRaisesRegex(
            executor.LabPromotionExecutorError,
            "group- or other-writable",
        ):
            self._execute()

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=self.transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_PREPARED,
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            OLD_ALPHA,
        )

        self.assertTrue(
            self._temp_path(0).exists()
        )

        self.assertTrue(
            self._temp_path(1).exists()
        )

    def test_group_writable_git_head_is_refused_before_applying(
        self,
    ):
        os.chmod(
            self.repository
            / ".git"
            / "HEAD",
            0o660,
        )

        with self.assertRaisesRegex(
            executor.LabPromotionExecutorError,
            "group- or other-writable",
        ):
            self._execute()

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=self.transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_PREPARED,
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            OLD_ALPHA,
        )

    def test_group_writable_nested_git_directory_is_refused_before_applying(
        self,
    ):
        os.chmod(
            self.repository
            / ".git"
            / "refs"
            / "heads",
            0o770,
        )

        with self.assertRaisesRegex(
            executor.LabPromotionExecutorError,
            "group- or other-writable",
        ):
            self._execute()

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=self.transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_PREPARED,
        )

        self.assertEqual(
            (
                self.repository
                / "tests"
                / "test_alpha.py"
            ).read_bytes(),
            OLD_TEST,
        )

    def test_runtime_authority_surface_is_bounded(
        self,
    ):
        source = Path(
            executor.__file__
        ).read_text(
            encoding="utf-8"
        )

        required = (
            "query_promotion_approval",
            "inspect_promotion_prepared_target",
            "inspect_promotion_applied_target",
            "os.replace",
            "os.fsync",
            "append_promotion_transaction_snapshot",
            "STATE_RECOVERY_REQUIRED",
            "_require_safe_git_metadata",
        )

        forbidden = (
            "consume_promotion_approval",
            "subprocess",
            "os.system",
            "Popen",
            "shell=True",
            "os.unlink",
            "os.remove",
            "os.rmdir",
            "git add",
            "git commit",
            "git push",
            "eval(",
            "exec(",
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
