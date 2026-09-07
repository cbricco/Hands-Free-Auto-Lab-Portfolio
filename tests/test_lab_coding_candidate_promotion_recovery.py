from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from hands_free_auto_lab.lab_coding_candidate import (
    CANDIDATE_COMPONENT,
    CANDIDATE_SCHEMA_VERSION,
    LabCodingCandidate,
    LabCodingCandidateFile,
    _candidate_id,
)
from hands_free_auto_lab.lab_promotion_proposal import (
    LabPromotionRepositoryFileState,
    build_lab_coding_candidate_promotion_proposal,
)
from hands_free_auto_lab.lab_promotion_recovery_materials import (
    LabPromotionRecoveryMaterialsError,
    build_promotion_recovery_materials,
    create_promotion_recovery_materials,
    initialize_promotion_recovery_materials_root,
    load_promotion_recovery_materials,
    load_promotion_recovery_preimages,
)
from hands_free_auto_lab.lab_promotion_source import (
    SOURCE_KIND_CODING_CANDIDATE,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    LabPromotionTransactionStateError,
    build_lab_promotion_transaction,
)


RUN_ID = "9" * 64
OLD_ALPHA = b"old alpha\n"
NEW_ALPHA = "new alpha\n"
NEW_TEST = "assert True\n"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _candidate() -> LabCodingCandidate:
    files = (
        LabCodingCandidateFile(
            operation="MODIFIED",
            path="alpha.txt",
            before_bytes=len(OLD_ALPHA),
            before_sha256=_sha(OLD_ALPHA),
            before_mode=0o644,
            final_bytes=len(NEW_ALPHA.encode("utf-8")),
            final_sha256=_sha(NEW_ALPHA.encode("utf-8")),
            final_mode=0o644,
            content=NEW_ALPHA,
        ),
        LabCodingCandidateFile(
            operation="ADDED",
            path="tests/test_alpha.py",
            before_bytes=None,
            before_sha256=None,
            before_mode=None,
            final_bytes=len(NEW_TEST.encode("utf-8")),
            final_sha256=_sha(NEW_TEST.encode("utf-8")),
            final_mode=0o644,
            content=NEW_TEST,
        ),
    )
    prototype = LabCodingCandidate(
        component=CANDIDATE_COMPONENT,
        schema_version=CANDIDATE_SCHEMA_VERSION,
        candidate_id="0" * 64,
        workspace_session_id="a" * 64,
        workspace_device=1,
        workspace_inode=2,
        before_snapshot_id="b" * 64,
        after_snapshot_id="c" * 64,
        physical_diff_id="d" * 64,
        files=files,
    )
    return replace(prototype, candidate_id=_candidate_id(prototype))


class LabCodingCandidatePromotionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="auto-lab-candidate-recovery-"))
        os.chmod(self.root, 0o700)
        self.repository = self.root / "sample-repository"
        self.repository.mkdir(mode=0o700)
        self.recovery_root = self.root / "recovery"
        self.recovery_root.mkdir(mode=0o700)
        initialize_promotion_recovery_materials_root(self.recovery_root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def transaction(self):
        candidate = _candidate()
        repository_state = self.repository.stat()
        proposal = build_lab_coding_candidate_promotion_proposal(
            candidate=candidate,
            repository_path=str(self.repository),
            repository_device=repository_state.st_dev,
            repository_inode=repository_state.st_ino,
            branch="main",
            head="1" * 40,
            before_states=(
                LabPromotionRepositoryFileState(
                    path="alpha.txt",
                    exists=True,
                    bytes=len(OLD_ALPHA),
                    sha256=_sha(OLD_ALPHA),
                    mode=0o644,
                ),
                LabPromotionRepositoryFileState(
                    path="tests/test_alpha.py",
                    exists=False,
                    bytes=None,
                    sha256=None,
                    mode=None,
                ),
            ),
        )
        return candidate, build_lab_promotion_transaction(
            proposal=proposal,
            run_id=RUN_ID,
        )

    def test_exact_modify_preimage_and_add_absence_survive_round_trip(self):
        candidate, transaction = self.transaction()

        self.assertEqual(
            {
                (item.source_kind, item.source_id)
                for item in transaction.files
            },
            {(SOURCE_KIND_CODING_CANDIDATE, candidate.candidate_id)},
        )

        materials = create_promotion_recovery_materials(
            self.recovery_root,
            transaction=transaction,
            preimages={"alpha.txt": OLD_ALPHA},
        )
        initialize_promotion_recovery_materials_root(self.recovery_root)

        self.assertEqual(
            load_promotion_recovery_materials(
                self.recovery_root,
                transaction=transaction,
            ),
            materials,
        )
        self.assertEqual(
            load_promotion_recovery_preimages(
                self.recovery_root,
                transaction=transaction,
            ),
            {"alpha.txt": OLD_ALPHA},
        )
        self.assertIsNotNone(materials.files[0].preimage_name)
        self.assertIsNone(materials.files[1].preimage_name)

    def test_recovery_material_identity_is_stable_for_candidate_provenance(self):
        _candidate_evidence, transaction = self.transaction()

        first, first_preimages = build_promotion_recovery_materials(
            transaction=transaction,
            preimages={"alpha.txt": OLD_ALPHA},
        )
        second, second_preimages = build_promotion_recovery_materials(
            transaction=transaction,
            preimages={"alpha.txt": OLD_ALPHA},
        )

        self.assertEqual(first, second)
        self.assertEqual(first.materials_id, second.materials_id)
        self.assertEqual(first_preimages, second_preimages)

    def test_stale_modify_preimage_is_refused_for_candidate_provenance(self):
        _candidate_evidence, transaction = self.transaction()

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "byte count|SHA-256",
        ):
            build_promotion_recovery_materials(
                transaction=transaction,
                preimages={"alpha.txt": b"stale alpha\n"},
            )

    def test_add_preimage_claim_is_refused_for_candidate_provenance(self):
        _candidate_evidence, transaction = self.transaction()

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

    def test_transaction_provenance_tampering_is_refused(self):
        _candidate_evidence, transaction = self.transaction()
        tampered = replace(
            transaction,
            files=(
                replace(transaction.files[0], source_id="e" * 64),
                transaction.files[1],
            ),
        )

        with self.assertRaisesRegex(
            LabPromotionRecoveryMaterialsError,
            "transaction failed validation",
        ):
            build_promotion_recovery_materials(
                transaction=tampered,
                preimages={"alpha.txt": OLD_ALPHA},
            )

        with self.assertRaises(LabPromotionTransactionStateError):
            from hands_free_auto_lab.lab_promotion_transaction_state import (
                validate_lab_promotion_transaction,
            )
            validate_lab_promotion_transaction(tampered)


if __name__ == "__main__":
    unittest.main()
