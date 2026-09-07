from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import hands_free_auto_lab.lab_promotion_recovery_materials as recovery

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
    LOCK_FILENAME,
    MANIFEST_FILENAME,
    PREIMAGES_DIRECTORY,
    TRANSACTIONS_DIRECTORY,
    LabPromotionRecoveryMaterialsError,
    build_promotion_recovery_materials,
    create_promotion_recovery_materials,
    initialize_promotion_recovery_materials_root,
    load_promotion_recovery_materials,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_PENDING,
    STATE_APPLYING,
    build_lab_promotion_transaction,
    transition_lab_promotion_transaction,
)


RUN_ID = "9" * 64
CANDIDATE_ID = "a" * 64
HEAD = "1" * 40

OLD_ALPHA = b"old alpha\n"


def _sha(
    raw: bytes,
) -> str:
    return hashlib.sha256(
        raw
    ).hexdigest()


def _proposal() -> LabPromotionProposal:
    files = (
        LabPromotionProposalFile(
            operation=PROMOTION_OPERATION_MODIFY,
            path="alpha.txt",
            before_exists=True,
            before_mode=0o644,
            after_mode=0o644,
            before_bytes=len(
                OLD_ALPHA
            ),
            before_sha256=_sha(
                OLD_ALPHA
            ),
            after_bytes=len(
                b"new alpha\n"
            ),
            after_sha256=_sha(
                b"new alpha\n"
            ),
            after_content="new alpha\n",
            source_kind="legacy_write_action",
            source_id="b" * 64,
        ),
        LabPromotionProposalFile(
            operation=PROMOTION_OPERATION_ADD,
            path="tests/test_alpha.py",
            before_exists=False,
            before_mode=None,
            after_mode=0o644,
            before_bytes=None,
            before_sha256=None,
            after_bytes=len(
                b"assert True\n"
            ),
            after_sha256=_sha(
                b"assert True\n"
            ),
            after_content="assert True\n",
            source_kind="legacy_write_action",
            source_id="c" * 64,
        ),
    )

    identity = _proposal_identity_object(
        candidate_id=CANDIDATE_ID,
        repository_path="/tmp/example-repo",
        repository_device=100,
        repository_inode=200,
        branch="main",
        head=HEAD,
        files=files,
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
        repository_path="/tmp/example-repo",
        repository_device=100,
        repository_inode=200,
        branch="main",
        head=HEAD,
        files=files,
    )


def _prepared():
    return build_lab_promotion_transaction(
        proposal=_proposal(),
        run_id=RUN_ID,
    )


class LabPromotionRecoveryMaterialsTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-recovery-materials-"
            )
        )

        os.chmod(
            self.root,
            0o700,
        )

        initialize_promotion_recovery_materials_root(
            self.root
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def transaction_dir(
        self,
        transaction_id: str,
    ) -> Path:
        return (
            self.root
            / TRANSACTIONS_DIRECTORY
            / transaction_id
        )

    def create(self):
        transaction = _prepared()

        materials = create_promotion_recovery_materials(
            self.root,
            transaction=transaction,
            preimages={
                "alpha.txt": OLD_ALPHA,
            },
        )

        return transaction, materials

    def test_initialize_creates_private_fixed_layout(self):
        self.assertEqual(
            {
                path.name
                for path in self.root.iterdir()
            },
            {
                LOCK_FILENAME,
                TRANSACTIONS_DIRECTORY,
            },
        )

        self.assertEqual(
            self.root.stat().st_mode
            & 0o777,
            0o700,
        )

        self.assertEqual(
            (
                self.root
                / LOCK_FILENAME
            ).stat().st_mode
            & 0o777,
            0o600,
        )

    def test_build_binds_exact_modify_preimage_and_add_has_none(self):
        transaction = _prepared()

        materials, preimages = (
            build_promotion_recovery_materials(
                transaction=transaction,
                preimages={
                    "alpha.txt": OLD_ALPHA,
                },
            )
        )

        self.assertEqual(
            preimages,
            {
                "alpha.txt": OLD_ALPHA,
            },
        )

        self.assertEqual(
            materials.transaction_id,
            transaction.transaction_id,
        )

        self.assertIsNotNone(
            materials.files[0].preimage_name
        )

        self.assertIsNone(
            materials.files[1].preimage_name
        )

    def test_after_temp_names_are_deterministic_safe_basenames(self):
        transaction = _prepared()

        first, _ = build_promotion_recovery_materials(
            transaction=transaction,
            preimages={
                "alpha.txt": OLD_ALPHA,
            },
        )

        second, _ = build_promotion_recovery_materials(
            transaction=transaction,
            preimages={
                "alpha.txt": OLD_ALPHA,
            },
        )

        self.assertEqual(
            [
                item.after_temp_name
                for item in first.files
            ],
            [
                item.after_temp_name
                for item in second.files
            ],
        )

        for item in first.files:
            self.assertTrue(
                item.after_temp_name.startswith(
                    ".hands-free-auto-lab-promote-"
                )
            )

            self.assertNotIn(
                "/",
                item.after_temp_name,
            )

            self.assertNotIn(
                "\\",
                item.after_temp_name,
            )

            self.assertLessEqual(
                len(
                    item.after_temp_name.encode(
                        "utf-8"
                    )
                ),
                255,
            )

    def test_create_round_trip_preserves_exact_materials(self):
        transaction, materials = self.create()

        loaded = load_promotion_recovery_materials(
            self.root,
            transaction=transaction,
        )

        self.assertEqual(
            loaded,
            materials,
        )

        directory = self.transaction_dir(
            transaction.transaction_id
        )

        self.assertTrue(
            (
                directory
                / MANIFEST_FILENAME
            ).is_file()
        )

        self.assertTrue(
            (
                directory
                / PREIMAGES_DIRECTORY
            ).is_dir()
        )

    def test_exact_preimage_bytes_survive_reopen(self):
        transaction, materials = self.create()

        modify = materials.files[0]

        path = (
            self.transaction_dir(
                transaction.transaction_id
            )
            / PREIMAGES_DIRECTORY
            / modify.preimage_name
        )

        self.assertEqual(
            path.read_bytes(),
            OLD_ALPHA,
        )

        initialize_promotion_recovery_materials_root(
            self.root
        )

        loaded = load_promotion_recovery_materials(
            self.root,
            transaction=transaction,
        )

        self.assertEqual(
            loaded,
            materials,
        )

    def test_public_preimage_loader_returns_exact_modify_bytes(self):
        transaction, _materials = self.create()

        from hands_free_auto_lab.lab_promotion_recovery_materials import (
            load_promotion_recovery_preimages,
        )

        preimages = load_promotion_recovery_preimages(
            self.root,
            transaction=transaction,
        )

        self.assertEqual(
            preimages,
            {
                "alpha.txt": OLD_ALPHA,
            },
        )

    def test_public_preimage_loader_accepts_applying_all_pending(self):
        transaction, _materials = self.create()

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_PENDING
                for item in transaction.files
            },
        )

        from hands_free_auto_lab.lab_promotion_recovery_materials import (
            load_promotion_recovery_preimages,
        )

        preimages = load_promotion_recovery_preimages(
            self.root,
            transaction=applying,
        )

        self.assertEqual(
            preimages,
            {
                "alpha.txt": OLD_ALPHA,
            },
        )

    def test_public_preimage_loader_accepts_partially_installed_applying(self):
        transaction, _materials = self.create()

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        from hands_free_auto_lab.lab_promotion_recovery_materials import (
            load_promotion_recovery_preimages,
        )

        preimages = load_promotion_recovery_preimages(
            self.root,
            transaction=applying,
        )

        self.assertEqual(
            preimages,
            {
                "alpha.txt": OLD_ALPHA,
            },
        )

    def test_public_preimage_loader_rejects_tampered_preimage(self):
        transaction, materials = self.create()

        modify = materials.files[
            0
        ]

        preimage = (
            self.transaction_dir(
                transaction.transaction_id
            )
            / PREIMAGES_DIRECTORY
            / modify.preimage_name
        )

        preimage.write_bytes(
            b"tampered\n"
        )

        preimage.chmod(
            0o600
        )

        from hands_free_auto_lab.lab_promotion_recovery_materials import (
            load_promotion_recovery_preimages,
        )

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "byte count|SHA-256",
        ):
            load_promotion_recovery_preimages(
                self.root,
                transaction=transaction,
            )

    def test_missing_modify_preimage_is_refused_before_persistence(self):
        transaction = _prepared()

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "exactly match MODIFY",
        ):
            create_promotion_recovery_materials(
                self.root,
                transaction=transaction,
                preimages={},
            )

        self.assertEqual(
            list(
                (
                    self.root
                    / TRANSACTIONS_DIRECTORY
                ).iterdir()
            ),
            [],
        )

    def test_extra_add_preimage_is_refused(self):
        transaction = _prepared()

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "extra=",
        ):
            build_promotion_recovery_materials(
                transaction=transaction,
                preimages={
                    "alpha.txt": OLD_ALPHA,
                    "tests/test_alpha.py": b"",
                },
            )

    def test_wrong_preimage_content_is_refused(self):
        transaction = _prepared()

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "byte count|SHA-256",
        ):
            build_promotion_recovery_materials(
                transaction=transaction,
                preimages={
                    "alpha.txt": b"wrong\n",
                },
            )

    def test_nonprepared_transaction_is_refused(self):
        prepared = _prepared()

        applying = transition_lab_promotion_transaction(
            prepared,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "PREPARED",
        ):
            build_promotion_recovery_materials(
                transaction=applying,
                preimages={
                    "alpha.txt": OLD_ALPHA,
                },
            )

    def test_validation_accepts_applying_all_pending_snapshot(self):
        transaction, materials = self.create()

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_PENDING
                for item in transaction.files
            },
        )

        from hands_free_auto_lab.lab_promotion_recovery_materials import (
            validate_promotion_recovery_materials,
        )

        validated = validate_promotion_recovery_materials(
            materials=materials,
            transaction=applying,
        )

        self.assertEqual(
            validated,
            materials,
        )

    def test_load_accepts_applying_all_pending_snapshot(self):
        transaction, materials = self.create()

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_PENDING
                for item in transaction.files
            },
        )

        loaded = load_promotion_recovery_materials(
            self.root,
            transaction=applying,
        )

        self.assertEqual(
            loaded,
            materials,
        )

    def test_load_accepts_applying_partially_installed_snapshot(self):
        transaction, materials = self.create()

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        loaded = load_promotion_recovery_materials(
            self.root,
            transaction=applying,
        )

        self.assertEqual(
            loaded,
            materials,
        )

    def test_creation_still_refuses_applying_snapshot(self):
        prepared = _prepared()

        applying = transition_lab_promotion_transaction(
            prepared,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_PENDING
                for item in prepared.files
            },
        )

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "PREPARED",
        ):
            create_promotion_recovery_materials(
                self.root,
                transaction=applying,
                preimages={
                    "alpha.txt": OLD_ALPHA,
                },
            )

    def test_duplicate_transaction_material_creation_is_refused(self):
        transaction, _ = self.create()

        with self.assertRaises(
            LabPromotionRecoveryMaterialsError
        ):
            create_promotion_recovery_materials(
                self.root,
                transaction=transaction,
                preimages={
                    "alpha.txt": OLD_ALPHA,
                },
            )

    def test_manifest_tamper_fails_closed(self):
        transaction, _ = self.create()

        manifest = (
            self.transaction_dir(
                transaction.transaction_id
            )
            / MANIFEST_FILENAME
        )

        parsed = json.loads(
            manifest.read_text(
                encoding="utf-8"
            )
        )

        parsed["branch"] = "tampered"

        manifest.write_bytes(
            (
                json.dumps(
                    parsed,
                    sort_keys=True,
                    separators=(
                        ",",
                        ":",
                    ),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode(
                "utf-8"
            )
        )

        manifest.chmod(
            0o600
        )

        with self.assertRaises(
            LabPromotionRecoveryMaterialsError
        ):
            load_promotion_recovery_materials(
                self.root,
                transaction=transaction,
            )

    def test_preimage_content_tamper_fails_closed(self):
        transaction, materials = self.create()

        modify = materials.files[0]

        preimage = (
            self.transaction_dir(
                transaction.transaction_id
            )
            / PREIMAGES_DIRECTORY
            / modify.preimage_name
        )

        preimage.write_bytes(
            b"tampered\n"
        )

        preimage.chmod(
            0o600
        )

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "byte count|SHA-256",
        ):
            load_promotion_recovery_materials(
                self.root,
                transaction=transaction,
            )

    def test_symlinked_preimage_fails_closed(self):
        transaction, materials = self.create()

        modify = materials.files[0]

        preimage = (
            self.transaction_dir(
                transaction.transaction_id
            )
            / PREIMAGES_DIRECTORY
            / modify.preimage_name
        )

        outside = (
            self.root.parent
            / (
                "auto-lab-recovery-symlink-"
                + transaction.transaction_id
            )
        )

        outside.write_bytes(
            OLD_ALPHA
        )

        outside.chmod(
            0o600
        )

        preimage.unlink()

        try:
            preimage.symlink_to(
                outside
            )

            with self.assertRaises(
                LabPromotionRecoveryMaterialsError
            ):
                load_promotion_recovery_materials(
                    self.root,
                    transaction=transaction,
                )

        finally:
            if outside.exists():
                outside.unlink()

    def test_hardlinked_preimage_fails_closed(self):
        transaction, materials = self.create()

        modify = materials.files[0]

        preimage = (
            self.transaction_dir(
                transaction.transaction_id
            )
            / PREIMAGES_DIRECTORY
            / modify.preimage_name
        )

        outside = (
            self.root.parent
            / (
                "auto-lab-recovery-hardlink-"
                + transaction.transaction_id
            )
        )

        try:
            os.link(
                preimage,
                outside,
            )

            with self.assertRaisesRegex(
                LabPromotionRecoveryMaterialsError,
                "hard link",
            ):
                load_promotion_recovery_materials(
                    self.root,
                    transaction=transaction,
                )

        finally:
            if outside.exists():
                outside.unlink()

    def test_partial_creation_fails_closed_and_is_not_repaired(self):
        transaction = _prepared()

        with patch(
            "hands_free_auto_lab.lab_promotion_recovery_materials._write_all",
            side_effect=LabPromotionRecoveryMaterialsError(
                "simulated preimage write failure"
            ),
        ):
            with self.assertRaisesRegex(
                LabPromotionRecoveryMaterialsError,
                "simulated",
            ):
                create_promotion_recovery_materials(
                    self.root,
                    transaction=transaction,
                    preimages={
                        "alpha.txt": OLD_ALPHA,
                    },
                )

        with self.assertRaises(
            LabPromotionRecoveryMaterialsError
        ):
            load_promotion_recovery_materials(
                self.root,
                transaction=transaction,
            )

    def test_material_identity_tamper_is_refused(self):
        transaction, materials = self.create()

        tampered = replace(
            materials,
            materials_id="f" * 64,
        )

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "materials_id",
        ):
            recovery.validate_promotion_recovery_materials(
                materials=tampered,
                transaction=transaction,
            )

    def test_module_has_no_repository_mutation_or_execution_authority(self):
        source = Path(
            recovery.__file__
        ).read_text(
            encoding="utf-8"
        )

        forbidden = (
            "subprocess",
            "/usr/bin/git",
            "os.replace",
            "os.rename",
            "os.unlink",
            "os.remove",
            "os.system",
            "Popen",
            "shell=True",
        )

        for token in forbidden:
            with self.subTest(
                token=token
            ):
                self.assertNotIn(
                    token,
                    source,
                )


if __name__ == "__main__":
    unittest.main()
