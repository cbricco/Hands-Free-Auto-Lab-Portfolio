from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import unittest

import hands_free_auto_lab.lab_promotion_transaction_state as transaction_state

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
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_PENDING,
    PROGRESS_RESTORED,
    PROGRESS_VERIFIED,
    RISK_DESTRUCTIVE_HIGH,
    RISK_WRITE,
    STATE_APPLYING,
    STATE_COMPLETED,
    STATE_PREPARED,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    STATE_VERIFYING,
    LabPromotionTransactionStateError,
    build_lab_promotion_transaction,
    transition_lab_promotion_transaction,
    validate_lab_promotion_transaction,
)


RUN_ID = "9" * 64
CANDIDATE_ID = "a" * 64
HEAD = "1" * 40


def _sha(
    text: str,
) -> str:
    return hashlib.sha256(
        text.encode(
            "utf-8"
        )
    ).hexdigest()


def _proposal(
    *,
    include_add: bool = True,
    source_kind: str = "legacy_write_action",
    source_id: str = "b" * 64,
) -> LabPromotionProposal:
    files = [
        LabPromotionProposalFile(
            operation=PROMOTION_OPERATION_MODIFY,
            path="alpha.txt",
            before_exists=True,
            before_mode=0o644,
            after_mode=0o644,
            before_bytes=len(
                b"old alpha\n"
            ),
            before_sha256=_sha(
                "old alpha\n"
            ),
            after_bytes=len(
                b"new alpha\n"
            ),
            after_sha256=_sha(
                "new alpha\n"
            ),
            after_content="new alpha\n",
            source_kind=source_kind,
            source_id=source_id,
        )
    ]

    if include_add:
        files.append(
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
                    "assert True\n"
                ),
                after_content="assert True\n",
                source_kind=source_kind,
                source_id=source_id,
            )
        )

    frozen = tuple(
        files
    )

    identity = _proposal_identity_object(
        candidate_id=CANDIDATE_ID,
        repository_path="/tmp/example-repo",
        repository_device=100,
        repository_inode=200,
        branch="main",
        head=HEAD,
        files=frozen,
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
        files=frozen,
    )


def _progress(
    transaction,
    **overrides,
):
    result = {
        item.path: item.progress
        for item in transaction.files
    }

    result.update(
        overrides
    )

    return result


class LabPromotionTransactionStateTests(
    unittest.TestCase
):
    def test_build_is_deterministic_and_binds_exact_proposal_and_run(self):
        proposal = _proposal()

        first = build_lab_promotion_transaction(
            proposal=proposal,
            run_id=RUN_ID,
        )

        second = build_lab_promotion_transaction(
            proposal=proposal,
            run_id=RUN_ID,
        )

        self.assertEqual(
            first.transaction_id,
            second.transaction_id,
        )

        self.assertEqual(
            first.snapshot_id,
            second.snapshot_id,
        )

        self.assertEqual(
            first.proposal_id,
            proposal.proposal_id,
        )

        self.assertEqual(
            first.run_id,
            RUN_ID,
        )

        self.assertEqual(
            first.state,
            STATE_PREPARED,
        )

        self.assertEqual(
            [
                item.path
                for item in first.files
            ],
            [
                "alpha.txt",
                "tests/test_alpha.py",
            ],
        )

    def test_coding_candidate_is_valid_neutral_evidence_without_authority(self):
        proposal = _proposal(
            source_kind="coding_candidate",
            source_id="d" * 64,
        )

        transaction = build_lab_promotion_transaction(
            proposal=proposal,
            run_id=RUN_ID,
        )

        self.assertEqual(
            transaction.files[0].source_kind,
            "coding_candidate",
        )
        self.assertEqual(
            transaction.files[0].source_id,
            "d" * 64,
        )
        self.assertEqual(
            transaction.state,
            STATE_PREPARED,
        )
        self.assertEqual(
            transaction.files[0].progress,
            PROGRESS_PENDING,
        )

        for attribute in (
            "approval",
            "authorization",
            "permission",
            "repository_mutation",
            "execution_authority",
        ):
            with self.subTest(attribute=attribute):
                self.assertFalse(
                    hasattr(transaction, attribute)
                )
                self.assertFalse(
                    hasattr(transaction.files[0], attribute)
                )

    def test_unsupported_source_kind_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "source_kind",
        ):
            build_lab_promotion_transaction(
                proposal=_proposal(
                    source_kind="approval",
                ),
                run_id=RUN_ID,
            )

    def test_malformed_or_noncanonical_source_id_is_refused(self):
        for source_id in (
            "d" * 63,
            "D" * 64,
            "g" * 64,
        ):
            with self.subTest(source_id=source_id):
                with self.assertRaisesRegex(
                    LabPromotionTransactionStateError,
                    "source_id",
                ):
                    build_lab_promotion_transaction(
                        proposal=_proposal(
                            source_id=source_id,
                        ),
                        run_id=RUN_ID,
                    )

    def test_transaction_construction_preserves_provenance_and_risk(self):
        for include_add, expected_risk in (
            (False, RISK_WRITE),
            (True, RISK_DESTRUCTIVE_HIGH),
        ):
            with self.subTest(
                include_add=include_add,
                expected_risk=expected_risk,
            ):
                legacy = build_lab_promotion_transaction(
                    proposal=_proposal(
                        include_add=include_add,
                    ),
                    run_id=RUN_ID,
                )
                coding = build_lab_promotion_transaction(
                    proposal=_proposal(
                        include_add=include_add,
                        source_kind="coding_candidate",
                        source_id="d" * 64,
                    ),
                    run_id=RUN_ID,
                )

                self.assertEqual(
                    {
                        (item.source_kind, item.source_id)
                        for item in coding.files
                    },
                    {("coding_candidate", "d" * 64)},
                )
                self.assertEqual(
                    coding.risk_classification,
                    legacy.risk_classification,
                )
                self.assertEqual(
                    coding.risk_classification,
                    expected_risk,
                )
                self.assertNotEqual(
                    coding.transaction_id,
                    legacy.transaction_id,
                )

    def test_modify_only_transaction_is_write(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(
                include_add=False
            ),
            run_id=RUN_ID,
        )

        self.assertEqual(
            transaction.risk_classification,
            RISK_WRITE,
        )

    def test_add_transaction_is_destructive_high_risk(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        self.assertEqual(
            transaction.risk_classification,
            RISK_DESTRUCTIVE_HIGH,
        )

    def test_different_run_changes_transaction_identity(self):
        proposal = _proposal()

        first = build_lab_promotion_transaction(
            proposal=proposal,
            run_id="1" * 64,
        )

        second = build_lab_promotion_transaction(
            proposal=proposal,
            run_id="2" * 64,
        )

        self.assertNotEqual(
            first.transaction_id,
            second.transaction_id,
        )

    def test_tampered_proposal_id_is_refused(self):
        proposal = replace(
            _proposal(),
            proposal_id="f" * 64,
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "proposal identity",
        ):
            build_lab_promotion_transaction(
                proposal=proposal,
                run_id=RUN_ID,
            )

    def test_tampered_proposal_content_is_refused(self):
        proposal = _proposal()

        tampered_file = replace(
            proposal.files[0],
            after_content="tampered\n",
        )

        tampered = replace(
            proposal,
            files=(
                tampered_file,
                proposal.files[1],
            ),
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "byte count|SHA-256",
        ):
            build_lab_promotion_transaction(
                proposal=tampered,
                run_id=RUN_ID,
            )

    def test_noncanonical_proposal_file_order_is_refused(self):
        proposal = _proposal()

        tampered = replace(
            proposal,
            files=tuple(
                reversed(
                    proposal.files
                )
            ),
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "sorted",
        ):
            build_lab_promotion_transaction(
                proposal=tampered,
                run_id=RUN_ID,
            )

    def test_prepared_requires_all_files_pending(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        self.assertTrue(
            all(
                item.progress
                == PROGRESS_PENDING
                for item in transaction.files
            )
        )

    def test_applying_can_advance_files_incrementally(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        self.assertEqual(
            applying.state,
            STATE_APPLYING,
        )

        self.assertEqual(
            [
                item.progress
                for item in applying.files
            ],
            [
                PROGRESS_INSTALLED,
                PROGRESS_PENDING,
            ],
        )

    def test_progress_paths_must_exactly_match_transaction(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "exactly match",
        ):
            transition_lab_promotion_transaction(
                transaction,
                state=STATE_APPLYING,
                progress_by_path={
                    "alpha.txt": PROGRESS_INSTALLED,
                },
            )

    def test_pending_file_cannot_skip_directly_to_verified(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "illegal file progress",
        ):
            transition_lab_promotion_transaction(
                transaction,
                state=STATE_APPLYING,
                progress_by_path={
                    "alpha.txt": PROGRESS_VERIFIED,
                    "tests/test_alpha.py": PROGRESS_PENDING,
                },
            )

    def test_verifying_requires_every_file_installed(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "every file",
        ):
            transition_lab_promotion_transaction(
                applying,
                state=STATE_VERIFYING,
                progress_by_path=_progress(
                    applying
                ),
            )

    def test_completed_requires_every_file_verified(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_INSTALLED
                for item in transaction.files
            },
        )

        verifying = transition_lab_promotion_transaction(
            applying,
            state=STATE_VERIFYING,
            progress_by_path=_progress(
                applying
            ),
        )

        partially_verified = transition_lab_promotion_transaction(
            verifying,
            state=STATE_VERIFYING,
            progress_by_path={
                "alpha.txt": PROGRESS_VERIFIED,
                "tests/test_alpha.py": PROGRESS_INSTALLED,
            },
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "every file",
        ):
            transition_lab_promotion_transaction(
                partially_verified,
                state=STATE_COMPLETED,
                progress_by_path=_progress(
                    partially_verified
                ),
            )

        fully_verified = transition_lab_promotion_transaction(
            partially_verified,
            state=STATE_VERIFYING,
            progress_by_path={
                item.path: PROGRESS_VERIFIED
                for item in partially_verified.files
            },
        )

        completed = transition_lab_promotion_transaction(
            fully_verified,
            state=STATE_COMPLETED,
            progress_by_path=_progress(
                fully_verified
            ),
        )

        self.assertEqual(
            completed.state,
            STATE_COMPLETED,
        )

    def test_rollback_preserves_untouched_and_restores_changed_files(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        rolling_back = transition_lab_promotion_transaction(
            applying,
            state=STATE_ROLLING_BACK,
            progress_by_path=_progress(
                applying
            ),
        )

        restored = transition_lab_promotion_transaction(
            rolling_back,
            state=STATE_ROLLING_BACK,
            progress_by_path={
                "alpha.txt": PROGRESS_RESTORED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        rolled_back = transition_lab_promotion_transaction(
            restored,
            state=STATE_ROLLED_BACK,
            progress_by_path=_progress(
                restored
            ),
        )

        self.assertEqual(
            rolled_back.state,
            STATE_ROLLED_BACK,
        )

    def test_recovery_required_is_available_from_active_transaction(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        recovery = transition_lab_promotion_transaction(
            applying,
            state=STATE_RECOVERY_REQUIRED,
            progress_by_path=_progress(
                applying
            ),
        )

        self.assertEqual(
            recovery.state,
            STATE_RECOVERY_REQUIRED,
        )

    def test_terminal_state_cannot_transition(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        recovery = transition_lab_promotion_transaction(
            transaction,
            state=STATE_RECOVERY_REQUIRED,
            progress_by_path=_progress(
                transaction
            ),
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "illegal transaction transition",
        ):
            transition_lab_promotion_transaction(
                recovery,
                state=STATE_APPLYING,
                progress_by_path=_progress(
                    recovery
                ),
            )

    def test_state_transition_preserves_provenance_exactly(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(
                source_kind="coding_candidate",
                source_id="d" * 64,
            ),
            run_id=RUN_ID,
        )
        before = tuple(
            (item.path, item.source_kind, item.source_id)
            for item in transaction.files
        )

        applying = transition_lab_promotion_transaction(
            transaction,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_INSTALLED
                for item in transaction.files
            },
        )

        self.assertEqual(
            tuple(
                (item.path, item.source_kind, item.source_id)
                for item in applying.files
            ),
            before,
        )

    def test_transaction_validation_detects_provenance_tampering(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        for changes in (
            {"source_kind": "coding_candidate"},
            {"source_id": "e" * 64},
        ):
            with self.subTest(changes=changes):
                changed = replace(
                    transaction.files[0],
                    **changes,
                )
                tampered = replace(
                    transaction,
                    files=(changed, transaction.files[1]),
                )

                with self.assertRaisesRegex(
                    LabPromotionTransactionStateError,
                    "proposal binding|transaction_id|snapshot_id",
                ):
                    validate_lab_promotion_transaction(tampered)

    def test_provenance_tampering_cannot_change_approval_risk_class(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(
                source_kind="coding_candidate",
                source_id="d" * 64,
            ),
            run_id=RUN_ID,
        )

        self.assertEqual(
            transaction.risk_classification,
            RISK_DESTRUCTIVE_HIGH,
        )

        for source_kind, source_id, risk_classification in (
            ("legacy_write_action", "e" * 64, RISK_WRITE),
            ("approval", "f" * 64, RISK_DESTRUCTIVE_HIGH),
        ):
            with self.subTest(
                source_kind=source_kind,
                risk_classification=risk_classification,
            ):
                changed = replace(
                    transaction.files[0],
                    source_kind=source_kind,
                    source_id=source_id,
                )
                tampered = replace(
                    transaction,
                    risk_classification=risk_classification,
                    files=(changed, transaction.files[1]),
                )

                with self.assertRaisesRegex(
                    LabPromotionTransactionStateError,
                    "source_kind|proposal binding|transaction_id|risk classification",
                ):
                    validate_lab_promotion_transaction(tampered)

    def test_snapshot_tamper_is_refused(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        tampered = replace(
            transaction,
            snapshot_id="e" * 64,
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "snapshot_id",
        ):
            validate_lab_promotion_transaction(
                tampered
            )

    def test_transaction_file_tamper_breaks_proposal_binding(self):
        transaction = build_lab_promotion_transaction(
            proposal=_proposal(),
            run_id=RUN_ID,
        )

        changed = replace(
            transaction.files[0],
            before_sha256="d" * 64,
        )

        tampered = replace(
            transaction,
            files=(
                changed,
                transaction.files[1],
            ),
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionStateError,
            "proposal binding",
        ):
            validate_lab_promotion_transaction(
                tampered
            )

    def test_model_has_no_filesystem_or_execution_authority(self):
        source = Path(
            transaction_state.__file__
        ).read_text(
            encoding="utf-8"
        )

        forbidden = (
            "subprocess",
            "os.",
            "pathlib",
            "open(",
            "unlink",
            "remove(",
            "replace(",
            "rename(",
            "mkdir",
            "rmdir",
            "git ",
            "flock",
            "fsync",
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
