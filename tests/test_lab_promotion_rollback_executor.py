from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import hands_free_auto_lab.lab_promotion_rollback_executor as rollback_executor

from hands_free_auto_lab.lab_promotion_applied_target import (
    inspect_promotion_applied_target,
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
from hands_free_auto_lab.lab_promotion_rollback_executor import (
    LabPromotionRollbackExecutorError,
    execute_lab_promotion_rollback,
)
from hands_free_auto_lab.lab_promotion_rollback_plan import (
    build_lab_promotion_rollback_plan,
)
from hands_free_auto_lab.lab_promotion_transaction_journal import (
    LabPromotionTransactionJournalError,
    append_promotion_transaction_snapshot,
    create_promotion_transaction_journal,
    initialize_promotion_transaction_journal_root,
    load_promotion_transaction_journal,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_PENDING,
    PROGRESS_RESTORED,
    STATE_APPLYING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    build_lab_promotion_transaction,
    transition_lab_promotion_transaction,
)


OLD_ALPHA = b"old alpha\n"
NEW_ALPHA = b"new alpha\n"
OLD_TEST = b"def test_old():\n    assert True\n"
NEW_TEST = b"def test_new():\n    assert True\n"
NEW_ADDED = b"added\n"

CANDIDATE_ID = "c" * 64
RUN_ID = "d" * 64


def sha(
    raw: bytes,
) -> str:
    return hashlib.sha256(
        raw
    ).hexdigest()


class LabPromotionRollbackExecutorTests(
    unittest.TestCase
):
    def setUp(
        self,
    ):
        self.base = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-promotion-rollback-executor-"
            )
        )

        os.chmod(
            self.base,
            0o700,
        )

        self.repository = (
            self.base
            / "target"
        )

        self.repository.mkdir(
            mode=0o700
        )

        self._git(
            "init",
            "-b",
            "main",
        )

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

        tests_dir.mkdir(
            mode=0o755
        )

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

        self.journal_root = (
            self.base
            / "journal"
        )

        self.recovery_root = (
            self.base
            / "recovery"
        )

        self.journal_root.mkdir(
            mode=0o700
        )

        self.recovery_root.mkdir(
            mode=0o700
        )

        initialize_promotion_transaction_journal_root(
            self.journal_root
        )

        initialize_promotion_recovery_materials_root(
            self.recovery_root
        )

    def tearDown(
        self,
    ):
        shutil.rmtree(
            self.base
        )

    def _git(
        self,
        *arguments: str,
        capture: bool = False,
    ) -> str:
        completed = subprocess.run(
            [
                "git",
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
            text=True,
            encoding="utf-8",
        )

        if capture:
            return completed.stdout

        return ""

    def _proposal(
        self,
        *,
        include_nested: bool = True,
        include_add: bool = False,
    ) -> LabPromotionProposal:
        repository_state = os.stat(
            self.repository
        )

        files: list[
            LabPromotionProposalFile
        ] = [
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
                source_kind="coding_candidate",
                source_id=CANDIDATE_ID,
            ),
        ]

        if include_add:
            files.append(
                LabPromotionProposalFile(
                    operation=PROMOTION_OPERATION_ADD,
                    path="new.txt",
                    before_exists=False,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                    after_bytes=len(
                        NEW_ADDED
                    ),
                    after_sha256=sha(
                        NEW_ADDED
                    ),
                    after_mode=0o644,
                    after_content=NEW_ADDED.decode(
                        "utf-8"
                    ),
                    source_kind="coding_candidate",
                    source_id=CANDIDATE_ID,
                )
            )

        if include_nested:
            files.append(
                LabPromotionProposalFile(
                    operation=PROMOTION_OPERATION_MODIFY,
                    path="tests/test_alpha.py",
                    before_exists=True,
                    before_bytes=len(
                        OLD_TEST
                    ),
                    before_sha256=sha(
                        OLD_TEST
                    ),
                    before_mode=0o644,
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
                    source_kind="coding_candidate",
                    source_id=CANDIDATE_ID,
                )
            )

        frozen_files = tuple(
            sorted(
                files,
                key=lambda item: item.path,
            )
        )

        identity = _proposal_identity_object(
            candidate_id=CANDIDATE_ID,
            repository_path=str(
                self.repository.resolve()
            ),
            repository_device=repository_state.st_dev,
            repository_inode=repository_state.st_ino,
            branch="main",
            head=self.head,
            files=frozen_files,
        )

        proposal_id = hashlib.sha256(
            _PROPOSAL_ID_DOMAIN
            + _canonical_json_bytes(
                identity
            )
        ).hexdigest()

        return LabPromotionProposal(
            component=PROMOTION_PROPOSAL_COMPONENT,
            schema_version=PROMOTION_PROPOSAL_SCHEMA_VERSION,
            proposal_id=proposal_id,
            candidate_id=CANDIDATE_ID,
            repository_path=str(
                self.repository.resolve()
            ),
            repository_device=repository_state.st_dev,
            repository_inode=repository_state.st_ino,
            branch="main",
            head=self.head,
            files=frozen_files,
        )

    def _preimages(
        self,
        transaction,
    ) -> dict[str, bytes]:
        result: dict[
            str,
            bytes,
        ] = {}

        for item in transaction.files:
            if (
                item.operation
                != PROMOTION_OPERATION_MODIFY
            ):
                continue

            if item.path == "alpha.txt":
                result[
                    item.path
                ] = OLD_ALPHA

            elif item.path == "tests/test_alpha.py":
                result[
                    item.path
                ] = OLD_TEST

            else:
                raise AssertionError(
                    f"unexpected MODIFY path: {item.path}"
                )

        return result

    def _after_bytes(
        self,
        path: str,
    ) -> bytes:
        if path == "alpha.txt":
            return NEW_ALPHA

        if path == "tests/test_alpha.py":
            return NEW_TEST

        if path == "new.txt":
            return NEW_ADDED

        raise AssertionError(
            f"unexpected path: {path}"
        )

    def _physical_path(
        self,
        path: str,
    ) -> Path:
        return (
            self.repository
            / path
        )

    def _write_exact_temp(
        self,
        *,
        transaction_file,
        material_file,
    ) -> None:
        destination = self._physical_path(
            transaction_file.path
        )

        temporary = (
            destination.parent
            / material_file.after_temp_name
        )

        temporary.write_bytes(
            self._after_bytes(
                transaction_file.path
            )
        )

        temporary.chmod(
            transaction_file.after_mode
        )

    def _activate(
        self,
        *,
        installed_paths: set[str],
        include_nested: bool = True,
        include_add: bool = False,
    ):
        transaction = build_lab_promotion_transaction(
            proposal=self._proposal(
                include_nested=include_nested,
                include_add=include_add,
            ),
            run_id=RUN_ID,
        )

        materials = create_promotion_recovery_materials(
            self.recovery_root,
            transaction=transaction,
            preimages=self._preimages(
                transaction
            ),
        )

        create_promotion_transaction_journal(
            self.journal_root,
            transaction=transaction,
        )

        materials_by_path = {
            item.path: item
            for item in materials.files
        }

        progress: dict[
            str,
            str,
        ] = {}

        for item in transaction.files:
            if item.path in installed_paths:
                destination = self._physical_path(
                    item.path
                )

                destination.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                destination.write_bytes(
                    self._after_bytes(
                        item.path
                    )
                )

                destination.chmod(
                    item.after_mode
                )

                progress[
                    item.path
                ] = PROGRESS_INSTALLED

            else:
                self._write_exact_temp(
                    transaction_file=item,
                    material_file=materials_by_path[
                        item.path
                    ],
                )

                progress[
                    item.path
                ] = PROGRESS_PENDING

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path=progress,
        )

        applying = append_promotion_transaction_snapshot(
            self.journal_root,
            successor=applying,
            expected_previous_snapshot_id=transaction.snapshot_id,
        )

        return (
            applying,
            materials,
        )

    def _plan(
        self,
        transaction,
    ):
        inspection = inspect_promotion_applied_target(
            journal_root=self.journal_root,
            recovery_root=self.recovery_root,
            transaction=transaction,
        )

        return build_lab_promotion_rollback_plan(
            transaction=transaction,
            inspection=inspection,
        )

    def _execute(
        self,
        transaction,
        plan,
    ):
        return execute_lab_promotion_rollback(
            journal_root=self.journal_root,
            recovery_root=self.recovery_root,
            transaction=transaction,
            plan=plan,
        )

    def test_two_file_nested_modify_rollback_success(
        self,
    ):
        transaction, _ = self._activate(
            installed_paths={
                "alpha.txt",
                "tests/test_alpha.py",
            }
        )

        result = self._execute(
            transaction,
            self._plan(
                transaction
            ),
        )

        self.assertEqual(
            result.state,
            STATE_ROLLED_BACK,
        )

        self.assertEqual(
            {
                item.path: item.progress
                for item in result.files
            },
            {
                "alpha.txt": PROGRESS_RESTORED,
                "tests/test_alpha.py": PROGRESS_RESTORED,
            },
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            OLD_ALPHA,
        )

        self.assertEqual(
            (
                self.repository
                / "tests"
                / "test_alpha.py"
            ).read_bytes(),
            OLD_TEST,
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

        self.assertEqual(
            load_promotion_transaction_journal(
                self.journal_root,
                transaction_id=result.transaction_id,
            ),
            result,
        )

    def test_partial_apply_restores_changed_and_preserves_pending(
        self,
    ):
        transaction, materials = self._activate(
            installed_paths={
                "alpha.txt",
            }
        )

        pending_material = next(
            item
            for item in materials.files
            if item.path == "tests/test_alpha.py"
        )

        pending_temp = (
            self.repository
            / "tests"
            / pending_material.after_temp_name
        )

        self.assertTrue(
            pending_temp.exists()
        )

        result = self._execute(
            transaction,
            self._plan(
                transaction
            ),
        )

        self.assertEqual(
            result.state,
            STATE_ROLLED_BACK,
        )

        self.assertEqual(
            {
                item.path: item.progress
                for item in result.files
            },
            {
                "alpha.txt": PROGRESS_RESTORED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            OLD_ALPHA,
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
            pending_temp.exists()
        )

    def test_already_restored_is_reconciled_without_rewrite(
        self,
    ):
        applying, _ = self._activate(
            installed_paths={
                "alpha.txt",
            }
        )

        rolling = transition_lab_promotion_transaction(
            applying,
            state=STATE_ROLLING_BACK,
            progress_by_path={
                item.path: item.progress
                for item in applying.files
            },
        )

        rolling = append_promotion_transaction_snapshot(
            self.journal_root,
            successor=rolling,
            expected_previous_snapshot_id=applying.snapshot_id,
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

        restored = transition_lab_promotion_transaction(
            rolling,
            state=STATE_ROLLING_BACK,
            progress_by_path={
                "alpha.txt": PROGRESS_RESTORED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        restored = append_promotion_transaction_snapshot(
            self.journal_root,
            successor=restored,
            expected_previous_snapshot_id=rolling.snapshot_id,
        )

        before_identity = alpha.stat()

        result = self._execute(
            restored,
            self._plan(
                restored
            ),
        )

        after_identity = alpha.stat()

        self.assertEqual(
            result.state,
            STATE_ROLLED_BACK,
        )

        self.assertEqual(
            (
                before_identity.st_dev,
                before_identity.st_ino,
            ),
            (
                after_identity.st_dev,
                after_identity.st_ino,
            ),
        )

        self.assertEqual(
            alpha.read_bytes(),
            OLD_ALPHA,
        )

    def test_add_rollback_is_refused_before_executor_mutation(
        self,
    ):
        transaction, materials = self._activate(
            installed_paths={
                "alpha.txt",
            },
            include_nested=False,
            include_add=True,
        )

        plan = self._plan(
            transaction
        )

        snapshot = transaction.snapshot_id

        with self.assertRaisesRegex(
            LabPromotionRollbackExecutorError,
            "MODIFY-only",
        ):
            self._execute(
                transaction,
                plan,
            )

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=transaction.transaction_id,
        )

        self.assertEqual(
            durable.snapshot_id,
            snapshot,
        )

        add_material = next(
            item
            for item in materials.files
            if item.path == "new.txt"
        )

        self.assertTrue(
            (
                self.repository
                / add_material.after_temp_name
            ).exists()
        )

        self.assertFalse(
            (
                self.repository
                / "new.txt"
            ).exists()
        )

    def test_stale_plan_destination_drift_is_refused_before_rollback(
        self,
    ):
        transaction, _ = self._activate(
            installed_paths={
                "alpha.txt",
                "tests/test_alpha.py",
            }
        )

        plan = self._plan(
            transaction
        )

        (
            self.repository
            / "alpha.txt"
        ).write_bytes(
            b"drift\n"
        )

        (
            self.repository
            / "alpha.txt"
        ).chmod(
            0o755
        )

        with self.assertRaises(
            LabPromotionRollbackExecutorError
        ):
            self._execute(
                transaction,
                plan,
            )

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=transaction.transaction_id,
        )

        self.assertEqual(
            durable,
            transaction,
        )

    def test_unsafe_parent_is_refused_before_journal_change(
        self,
    ):
        transaction, _ = self._activate(
            installed_paths={
                "alpha.txt",
                "tests/test_alpha.py",
            }
        )

        plan = self._plan(
            transaction
        )

        tests_dir = (
            self.repository
            / "tests"
        )

        tests_dir.chmod(
            0o777
        )

        try:
            with self.assertRaisesRegex(
                LabPromotionRollbackExecutorError,
                "writable",
            ):
                self._execute(
                    transaction,
                    plan,
                )

            durable = load_promotion_transaction_journal(
                self.journal_root,
                transaction_id=transaction.transaction_id,
            )

            self.assertEqual(
                durable,
                transaction,
            )

        finally:
            tests_dir.chmod(
                0o755
            )

    def test_recovery_material_plan_identity_mismatch_is_refused(
        self,
    ):
        transaction, _ = self._activate(
            installed_paths={
                "alpha.txt",
                "tests/test_alpha.py",
            }
        )

        plan = self._plan(
            transaction
        )

        changed = replace(
            plan,
            materials_id="0" * 64,
        )

        with self.assertRaisesRegex(
            LabPromotionRollbackExecutorError,
            "plan does not match",
        ):
            self._execute(
                transaction,
                changed,
            )

        self.assertEqual(
            load_promotion_transaction_journal(
                self.journal_root,
                transaction_id=transaction.transaction_id,
            ),
            transaction,
        )

    def test_promotion_temp_unlink_parent_fsync_failure_is_recovery_required(
        self,
    ):
        transaction, materials = self._activate(
            installed_paths={
                "alpha.txt",
            }
        )

        pending_path = "tests/test_alpha.py"

        pending_material = next(
            item
            for item in materials.files
            if item.path == pending_path
        )

        pending_temp = (
            self._physical_path(
                pending_path
            ).parent
            / pending_material.after_temp_name
        )

        self.assertTrue(
            pending_temp.exists()
        )

        pending_progress_before = next(
            item.progress
            for item in transaction.files
            if item.path == pending_path
        )

        plan = self._plan(
            transaction
        )

        real_fsync = rollback_executor.os.fsync
        failed_once = False

        def flaky_fsync(fd):
            nonlocal failed_once

            if (
                not failed_once
                and not pending_temp.exists()
            ):
                failed_once = True
                raise OSError(
                    "injected parent fsync failure after "
                    "promotion-temp unlink"
                )

            return real_fsync(fd)

        with patch.object(
            rollback_executor.os,
            "fsync",
            side_effect=flaky_fsync,
        ):
            with self.assertRaisesRegex(
                LabPromotionRollbackExecutorError,
                "RECOVERY_REQUIRED",
            ):
                self._execute(
                    transaction,
                    plan,
                )

        self.assertTrue(
            failed_once
        )

        self.assertFalse(
            pending_temp.exists()
        )

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_RECOVERY_REQUIRED,
        )

        self.assertEqual(
            next(
                item.progress
                for item in durable.files
                if item.path == pending_path
            ),
            pending_progress_before,
        )

    def test_restore_temporary_readback_tamper_is_recovery_required(
        self,
    ):
        transaction, _ = self._activate(
            installed_paths={
                "alpha.txt",
                "tests/test_alpha.py",
            }
        )

        plan = self._plan(
            transaction
        )

        before_entries = {
            entry.name
            for entry in self.repository.iterdir()
        }

        real_read_exact_regular_file = (
            rollback_executor._read_exact_regular_file
        )
        real_replace = rollback_executor.os.replace

        tampered_once = False
        replace_called = False
        tampered_bytes = None

        def tampering_read_exact_regular_file(
            parent_fd,
            *,
            name,
            path,
            expected_bytes,
            expected_sha256,
            expected_mode,
        ):
            nonlocal tampered_once
            nonlocal tampered_bytes

            if (
                path
                == "alpha.txt [rollback preimage temporary]"
                and not tampered_once
            ):
                self.assertIsNotNone(
                    expected_bytes
                )

                original = bytes(
                    expected_bytes
                )

                self.assertGreater(
                    len(original),
                    0,
                )

                tampered = (
                    bytes(
                        [
                            original[0]
                            ^ 0x01
                        ]
                    )
                    + original[1:]
                )

                self.assertEqual(
                    len(tampered),
                    len(original),
                )
                self.assertNotEqual(
                    tampered,
                    original,
                )

                flags = (
                    rollback_executor.os.O_WRONLY
                )

                flags |= getattr(
                    rollback_executor.os,
                    "O_CLOEXEC",
                    0,
                )
                flags |= getattr(
                    rollback_executor.os,
                    "O_NOFOLLOW",
                    0,
                )

                tamper_fd = rollback_executor.os.open(
                    name,
                    flags,
                    dir_fd=parent_fd,
                )

                try:
                    rollback_executor._write_all(
                        tamper_fd,
                        tampered,
                    )
                    rollback_executor.os.fsync(
                        tamper_fd
                    )
                finally:
                    rollback_executor.os.close(
                        tamper_fd
                    )

                tampered_once = True
                tampered_bytes = tampered

            return real_read_exact_regular_file(
                parent_fd,
                name=name,
                path=path,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
                expected_mode=expected_mode,
            )

        def tracking_replace(*args, **kwargs):
            nonlocal replace_called

            result = real_replace(
                *args,
                **kwargs,
            )
            replace_called = True
            return result

        with patch.object(
            rollback_executor,
            "_read_exact_regular_file",
            side_effect=(
                tampering_read_exact_regular_file
            ),
        ):
            with patch.object(
                rollback_executor.os,
                "replace",
                side_effect=tracking_replace,
            ):
                with self.assertRaisesRegex(
                    LabPromotionRollbackExecutorError,
                    "RECOVERY_REQUIRED.*SHA-256 mismatch",
                ):
                    self._execute(
                        transaction,
                        plan,
                    )

        self.assertTrue(
            tampered_once
        )
        self.assertFalse(
            replace_called
        )
        self.assertIsNotNone(
            tampered_bytes
        )

        after_entries = {
            entry.name
            for entry in self.repository.iterdir()
        }
        preserved_entries = (
            after_entries
            - before_entries
        )

        self.assertEqual(
            len(preserved_entries),
            1,
        )

        preserved_temporary = (
            self.repository
            / next(iter(preserved_entries))
        )

        self.assertTrue(
            preserved_temporary.is_file()
        )
        self.assertEqual(
            preserved_temporary.read_bytes(),
            tampered_bytes,
        )
        self.assertNotEqual(
            preserved_temporary.read_bytes(),
            OLD_ALPHA,
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            NEW_ALPHA,
        )

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_RECOVERY_REQUIRED,
        )

        self.assertEqual(
            {
                item.path: item.progress
                for item in durable.files
            },
            {
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_INSTALLED,
            },
        )

    def test_restore_temporary_fsync_failure_is_recovery_required(
        self,
    ):
        transaction, _ = self._activate(
            installed_paths={
                "alpha.txt",
                "tests/test_alpha.py",
            }
        )

        plan = self._plan(
            transaction
        )

        before_entries = {
            entry.name
            for entry in self.repository.iterdir()
        }

        real_write_all = rollback_executor._write_all
        real_replace = rollback_executor.os.replace
        real_fsync = rollback_executor.os.fsync

        write_armed = False
        fsyncs_after_write = 0
        replaced_once = False
        failed_once = False

        def tracking_write_all(fd, data):
            nonlocal write_armed

            result = real_write_all(
                fd,
                data,
            )
            write_armed = True
            return result

        def tracking_replace(*args, **kwargs):
            nonlocal replaced_once

            result = real_replace(
                *args,
                **kwargs,
            )
            replaced_once = True
            return result

        def flaky_fsync(fd):
            nonlocal fsyncs_after_write
            nonlocal failed_once

            if (
                write_armed
                and not failed_once
            ):
                fsyncs_after_write += 1

                if fsyncs_after_write == 1:
                    failed_once = True
                    raise OSError(
                        "injected rollback temporary fsync failure"
                    )

            return real_fsync(fd)

        with patch.object(
            rollback_executor,
            "_write_all",
            side_effect=tracking_write_all,
        ):
            with patch.object(
                rollback_executor.os,
                "replace",
                side_effect=tracking_replace,
            ):
                with patch.object(
                    rollback_executor.os,
                    "fsync",
                    side_effect=flaky_fsync,
                ):
                    with self.assertRaisesRegex(
                        LabPromotionRollbackExecutorError,
                        "RECOVERY_REQUIRED",
                    ):
                        self._execute(
                            transaction,
                            plan,
                        )

        self.assertTrue(
            write_armed
        )
        self.assertEqual(
            fsyncs_after_write,
            1,
        )
        self.assertFalse(
            replaced_once
        )
        self.assertTrue(
            failed_once
        )

        after_entries = {
            entry.name
            for entry in self.repository.iterdir()
        }
        preserved_entries = (
            after_entries
            - before_entries
        )

        self.assertEqual(
            len(preserved_entries),
            1,
        )

        preserved_temporary = (
            self.repository
            / next(iter(preserved_entries))
        )

        self.assertTrue(
            preserved_temporary.is_file()
        )
        self.assertEqual(
            preserved_temporary.read_bytes(),
            OLD_ALPHA,
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            NEW_ALPHA,
        )

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_RECOVERY_REQUIRED,
        )

        self.assertEqual(
            {
                item.path: item.progress
                for item in durable.files
            },
            {
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_INSTALLED,
            },
        )

    def test_restore_pre_replace_parent_fsync_failure_is_recovery_required(
        self,
    ):
        transaction, _ = self._activate(
            installed_paths={
                "alpha.txt",
                "tests/test_alpha.py",
            }
        )

        plan = self._plan(
            transaction
        )

        before_entries = {
            entry.name
            for entry in self.repository.iterdir()
        }

        real_write_all = rollback_executor._write_all
        real_replace = rollback_executor.os.replace
        real_fsync = rollback_executor.os.fsync

        write_armed = False
        fsyncs_after_write = 0
        replaced_once = False
        failed_once = False

        def tracking_write_all(fd, data):
            nonlocal write_armed

            result = real_write_all(
                fd,
                data,
            )
            write_armed = True
            return result

        def tracking_replace(*args, **kwargs):
            nonlocal replaced_once

            result = real_replace(
                *args,
                **kwargs,
            )
            replaced_once = True
            return result

        def flaky_fsync(fd):
            nonlocal fsyncs_after_write
            nonlocal failed_once

            if (
                write_armed
                and not failed_once
            ):
                fsyncs_after_write += 1

                if fsyncs_after_write == 2:
                    failed_once = True
                    raise OSError(
                        "injected parent fsync failure before "
                        "rollback preimage replace"
                    )

            return real_fsync(fd)

        with patch.object(
            rollback_executor,
            "_write_all",
            side_effect=tracking_write_all,
        ):
            with patch.object(
                rollback_executor.os,
                "replace",
                side_effect=tracking_replace,
            ):
                with patch.object(
                    rollback_executor.os,
                    "fsync",
                    side_effect=flaky_fsync,
                ):
                    with self.assertRaisesRegex(
                    LabPromotionRollbackExecutorError,
                    "RECOVERY_REQUIRED",
                    ):
                        self._execute(
                            transaction,
                            plan,
                        )

        self.assertTrue(
            write_armed
        )
        self.assertEqual(
            fsyncs_after_write,
            2,
        )
        self.assertFalse(
            replaced_once
        )
        self.assertTrue(
            failed_once
        )

        after_entries = {
            entry.name
            for entry in self.repository.iterdir()
        }
        preserved_entries = (
            after_entries
            - before_entries
        )

        self.assertEqual(
            len(preserved_entries),
            1,
        )

        preserved_temporary = (
            self.repository
            / next(iter(preserved_entries))
        )

        self.assertTrue(
            preserved_temporary.is_file()
        )

        self.assertTrue(
            failed_once
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            NEW_ALPHA,
        )

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_RECOVERY_REQUIRED,
        )

        self.assertEqual(
            {
                item.path: item.progress
                for item in durable.files
            },
            {
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_INSTALLED,
            },
        )

    def test_restore_post_replace_parent_fsync_failure_is_recovery_required(
        self,
    ):
        transaction, _ = self._activate(
            installed_paths={
                "alpha.txt",
                "tests/test_alpha.py",
            }
        )

        plan = self._plan(
            transaction
        )

        real_replace = rollback_executor.os.replace
        real_fsync = rollback_executor.os.fsync

        replaced_once = False
        failed_once = False

        def tracking_replace(*args, **kwargs):
            nonlocal replaced_once

            result = real_replace(
                *args,
                **kwargs,
            )
            replaced_once = True
            return result

        def flaky_fsync(fd):
            nonlocal failed_once

            if (
                replaced_once
                and not failed_once
            ):
                failed_once = True
                raise OSError(
                    "injected parent fsync failure after "
                    "rollback preimage replace"
                )

            return real_fsync(fd)

        with patch.object(
            rollback_executor.os,
            "replace",
            side_effect=tracking_replace,
        ):
            with patch.object(
                rollback_executor.os,
                "fsync",
                side_effect=flaky_fsync,
            ):
                with self.assertRaisesRegex(
                    LabPromotionRollbackExecutorError,
                    "RECOVERY_REQUIRED",
                ):
                    self._execute(
                        transaction,
                        plan,
                    )

        self.assertTrue(
            replaced_once
        )

        self.assertTrue(
            failed_once
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            OLD_ALPHA,
        )

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_RECOVERY_REQUIRED,
        )

        self.assertEqual(
            {
                item.path: item.progress
                for item in durable.files
            },
            {
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_INSTALLED,
            },
        )

    def test_replace_before_progress_failure_is_recovery_required(
        self,
    ):
        transaction, _ = self._activate(
            installed_paths={
                "alpha.txt",
                "tests/test_alpha.py",
            }
        )

        plan = self._plan(
            transaction
        )

        failed_once = False

        def flaky_append(
            root,
            *,
            successor,
            expected_previous_snapshot_id,
        ):
            nonlocal failed_once

            has_restored = any(
                item.progress
                == PROGRESS_RESTORED
                for item in successor.files
            )

            if (
                not failed_once
                and successor.state
                == STATE_ROLLING_BACK
                and has_restored
            ):
                failed_once = True

                raise LabPromotionTransactionJournalError(
                    "injected post-replace journal failure"
                )

            return append_promotion_transaction_snapshot(
                root,
                successor=successor,
                expected_previous_snapshot_id=(
                    expected_previous_snapshot_id
                ),
            )

        with patch.object(
            rollback_executor,
            "append_promotion_transaction_snapshot",
            side_effect=flaky_append,
        ):
            with self.assertRaisesRegex(
                LabPromotionRollbackExecutorError,
                "RECOVERY_REQUIRED",
            ):
                self._execute(
                    transaction,
                    plan,
                )

        self.assertTrue(
            failed_once
        )

        durable = load_promotion_transaction_journal(
            self.journal_root,
            transaction_id=transaction.transaction_id,
        )

        self.assertEqual(
            durable.state,
            STATE_RECOVERY_REQUIRED,
        )

        self.assertEqual(
            (
                self.repository
                / "alpha.txt"
            ).read_bytes(),
            OLD_ALPHA,
        )

        self.assertEqual(
            {
                item.path: item.progress
                for item in durable.files
            },
            {
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_INSTALLED,
            },
        )

    def test_module_has_bounded_repository_execution_authority(
        self,
    ):
        path = Path(
            rollback_executor.__file__
        )

        source = path.read_text(
            encoding="utf-8"
        )

        tree = ast.parse(
            source,
            filename=str(
                path
            ),
        )

        imported_modules = set()

        for node in ast.walk(
            tree
        ):
            if isinstance(
                node,
                ast.Import,
            ):
                imported_modules.update(
                    item.name
                    for item in node.names
                )

            elif isinstance(
                node,
                ast.ImportFrom,
            ):
                if node.module:
                    imported_modules.add(
                        node.module
                    )

        joined = "\n".join(
            sorted(
                imported_modules
            )
        )

        for forbidden in (
            "lab_promotion_approval_store",
            "lab_promotion_executor",
            "subprocess",
            "socket",
            "requests",
            "urllib",
        ):
            self.assertNotIn(
                forbidden,
                joined,
            )

        self.assertIn(
            "os.replace",
            source,
        )

        self.assertIn(
            "os.unlink",
            source,
        )

        for forbidden_text in (
            "git add",
            "git commit",
            "git push",
            "os.system(",
            "subprocess.",
        ):
            self.assertNotIn(
                forbidden_text,
                source,
            )


if __name__ == "__main__":
    unittest.main()
