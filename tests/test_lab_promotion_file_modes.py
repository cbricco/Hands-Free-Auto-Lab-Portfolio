from __future__ import annotations

import hashlib
import unittest

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
    build_promotion_recovery_materials,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    LabPromotionTransactionStateError,
    build_lab_promotion_transaction,
)


CANDIDATE_ID = "a" * 64
RUN_ID = "9" * 64
HEAD = "1" * 40

OLD = b"old\n"
NEW = b"new\n"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _proposal(
    *,
    modify_mode: int = 0o644,
    modify_after_mode: int | None = None,
    add_after_mode: int = 0o644,
) -> LabPromotionProposal:
    if modify_after_mode is None:
        modify_after_mode = modify_mode

    files = (
        LabPromotionProposalFile(
            operation=PROMOTION_OPERATION_MODIFY,
            path="alpha.txt",
            before_exists=True,
            before_bytes=len(OLD),
            before_sha256=_sha(OLD),
            before_mode=modify_mode,
            after_bytes=len(NEW),
            after_sha256=_sha(NEW),
            after_mode=modify_after_mode,
            after_content=NEW.decode("utf-8"),
            source_kind="legacy_write_action",
            source_id="b" * 64,
        ),
        LabPromotionProposalFile(
            operation=PROMOTION_OPERATION_ADD,
            path="tests/test_alpha.py",
            before_exists=False,
            before_bytes=None,
            before_sha256=None,
            before_mode=None,
            after_bytes=len(b"assert True\n"),
            after_sha256=_sha(b"assert True\n"),
            after_mode=add_after_mode,
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
        + _canonical_json_bytes(identity)
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


class LabPromotionFileModeTests(unittest.TestCase):
    def test_mode_change_changes_proposal_identity(self):
        self.assertNotEqual(
            _proposal(modify_mode=0o644).proposal_id,
            _proposal(modify_mode=0o755).proposal_id,
        )

    def test_modify_mode_is_preserved_into_transaction(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(modify_mode=0o755),
            run_id=RUN_ID,
        )

        self.assertEqual(
            transaction.files[0].before_mode,
            0o755,
        )
        self.assertEqual(
            transaction.files[0].after_mode,
            0o755,
        )

    def test_add_mode_is_explicit_0644(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        self.assertIsNone(
            transaction.files[1].before_mode
        )
        self.assertEqual(
            transaction.files[1].after_mode,
            0o644,
        )

    def test_modify_mode_change_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "preserve before_mode",
        ):
            build_lab_promotion_transaction(
                proposal=_proposal(
                    modify_mode=0o644,
                    modify_after_mode=0o600,
                ),
                run_id=RUN_ID,
            )

    def test_non_0644_add_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "ADD after_mode",
        ):
            build_lab_promotion_transaction(
                proposal=_proposal(
                    add_after_mode=0o600,
                ),
                run_id=RUN_ID,
            )

    def test_special_mode_bits_are_refused(self):
        for mode in (0o4644, 0o2644, 0o1644):
            with self.subTest(mode=oct(mode)):
                with self.assertRaisesRegex(
                    LabPromotionTransactionStateError,
                    "0000 through 0777",
                ):
                    build_lab_promotion_transaction(
                        proposal=_proposal(
                            modify_mode=mode,
                            modify_after_mode=mode,
                        ),
                        run_id=RUN_ID,
                    )

    def test_recovery_material_identity_binds_modes(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(
                modify_mode=0o755,
            ),
            run_id=RUN_ID,
        )

        materials, _ = build_promotion_recovery_materials(
            transaction=transaction,
            preimages={
                "alpha.txt": OLD,
            },
        )

        self.assertEqual(
            materials.files[0].before_mode,
            0o755,
        )
        self.assertEqual(
            materials.files[0].after_mode,
            0o755,
        )
        self.assertEqual(
            materials.files[1].after_mode,
            0o644,
        )


if __name__ == "__main__":
    unittest.main()
