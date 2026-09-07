from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import hands_free_auto_lab.lab_promotion_rollback_plan as planner
from hands_free_auto_lab.lab_promotion_applied_target import (
    APPLIED_TARGET_COMPONENT,
    APPLIED_TARGET_SCHEMA_VERSION,
    DESTINATION_AFTER,
    DESTINATION_BEFORE,
    DESTINATION_OTHER,
    TEMP_ABSENT,
    TEMP_EXACT,
    TEMP_UNEXPECTED,
    LabPromotionAppliedDestination,
    LabPromotionAppliedFile,
    LabPromotionAppliedTargetInspection,
    LabPromotionPreparedTemp,
)
from hands_free_auto_lab.lab_promotion_proposal import (
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
    PROMOTION_PROPOSAL_COMPONENT,
    PROMOTION_PROPOSAL_SCHEMA_VERSION,
    LabPromotionProposal,
    LabPromotionProposalFile,
    _PROPOSAL_ID_DOMAIN,
    _canonical_json_bytes as _proposal_canonical_json_bytes,
    _proposal_identity_object,
)
from hands_free_auto_lab.lab_promotion_rollback_plan import (
    STEP_ENTER_ROLLBACK,
    STEP_FINISH_ROLLBACK,
    STEP_MARK_RESTORED,
    STEP_RECONCILE_INSTALLED,
    STEP_REMOVE_ADDED_FILE,
    STEP_REMOVE_EXACT_TEMP,
    STEP_RESTORE_PREIMAGE,
    LabPromotionRollbackPlanError,
    build_lab_promotion_rollback_plan,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_PENDING,
    PROGRESS_RESTORED,
    PROGRESS_VERIFIED,
    RISK_WRITE,
    STATE_APPLYING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLING_BACK,
    STATE_VERIFYING,
    LabPromotionTransaction,
    LabPromotionTransactionFile,
    build_lab_promotion_transaction,
    transition_lab_promotion_transaction,
)


TRANSACTION_ID = "1" * 64
SNAPSHOT_ID = "2" * 64
PROPOSAL_ID = "3" * 64
CANDIDATE_ID = "4" * 64
RUN_ID = "5" * 64
MATERIALS_ID = "6" * 64


def _sha(
    text: str,
) -> str:
    return hashlib.sha256(
        text.encode(
            "utf-8"
        )
    ).hexdigest()


def real_proposal() -> LabPromotionProposal:
    files = (
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
            source_kind="legacy_write_action",
            source_id="9" * 64,
        ),
    )

    identity = _proposal_identity_object(
        candidate_id=CANDIDATE_ID,
        repository_path="/tmp/example",
        repository_device=11,
        repository_inode=22,
        branch="main",
        head="a" * 40,
        files=files,
    )

    proposal_id = hashlib.sha256(
        _PROPOSAL_ID_DOMAIN
        + _proposal_canonical_json_bytes(
            identity
        )
    ).hexdigest()

    return LabPromotionProposal(
        component=PROMOTION_PROPOSAL_COMPONENT,
        schema_version=PROMOTION_PROPOSAL_SCHEMA_VERSION,
        proposal_id=proposal_id,
        candidate_id=CANDIDATE_ID,
        repository_path="/tmp/example",
        repository_device=11,
        repository_inode=22,
        branch="main",
        head="a" * 40,
        files=files,
    )


def real_applying_transaction() -> LabPromotionTransaction:
    prepared = build_lab_promotion_transaction(
        proposal=real_proposal(),
        run_id=RUN_ID,
    )

    return transition_lab_promotion_transaction(
        prepared,
        state=STATE_APPLYING,
        progress_by_path={
            item.path: item.progress
            for item in prepared.files
        },
    )


def transaction_file(
    *,
    operation: str = PROMOTION_OPERATION_MODIFY,
    path: str = "alpha.txt",
    progress: str = PROGRESS_PENDING,
) -> LabPromotionTransactionFile:
    before_exists = operation == PROMOTION_OPERATION_MODIFY

    return LabPromotionTransactionFile(
        operation=operation,
        path=path,
        before_exists=before_exists,
        before_bytes=4 if before_exists else None,
        before_sha256="7" * 64 if before_exists else None,
        before_mode=0o644 if before_exists else None,
        after_bytes=4,
        after_sha256="8" * 64,
        after_mode=0o644,
        after_content="new\n",
        source_kind="legacy_write_action",
        source_id="9" * 64,
        progress=progress,
    )


def transaction(
    *,
    state: str = STATE_APPLYING,
    operation: str = PROMOTION_OPERATION_MODIFY,
    progress: str = PROGRESS_PENDING,
) -> LabPromotionTransaction:
    item = transaction_file(
        operation=operation,
        progress=progress,
    )

    return LabPromotionTransaction(
        component="test-component",
        schema_version=999,
        transaction_id=TRANSACTION_ID,
        snapshot_id=SNAPSHOT_ID,
        proposal_id=PROPOSAL_ID,
        candidate_id=CANDIDATE_ID,
        run_id=RUN_ID,
        repository_path="/tmp/example",
        repository_device=11,
        repository_inode=22,
        branch="main",
        head="a" * 40,
        risk_classification=RISK_WRITE,
        state=state,
        files=(
            item,
        ),
    )


def inspection(
    tx: LabPromotionTransaction,
    *,
    destination: str,
    temporary: str,
) -> LabPromotionAppliedTargetInspection:
    item = tx.files[
        0
    ]

    destination_item = LabPromotionAppliedDestination(
        path=item.path,
        operation=item.operation,
        journal_progress=item.progress,
        status=destination,
        observed_bytes=None,
        observed_sha256=None,
        observed_mode=None,
    )

    temp_item = LabPromotionPreparedTemp(
        path=item.path,
        temp_path=".hands-free-test-temp",
        status=temporary,
        observed_bytes=None,
        observed_sha256=None,
        observed_mode=None,
    )

    applied_file = LabPromotionAppliedFile(
        path=item.path,
        operation=item.operation,
        journal_progress=item.progress,
        destination=destination_item,
        temporary=temp_item,
    )

    return LabPromotionAppliedTargetInspection(
        component=APPLIED_TARGET_COMPONENT,
        schema_version=APPLIED_TARGET_SCHEMA_VERSION,
        transaction_id=tx.transaction_id,
        snapshot_id=tx.snapshot_id,
        materials_id=MATERIALS_ID,
        repository_path=tx.repository_path,
        repository_device=tx.repository_device,
        repository_inode=tx.repository_inode,
        branch=tx.branch,
        head=tx.head,
        files=(
            applied_file,
        ),
    )


class RollbackPlannerTests(
    unittest.TestCase
):
    def build(
        self,
        tx: LabPromotionTransaction,
        observed: LabPromotionAppliedTargetInspection,
    ):
        with patch.object(
            planner,
            "validate_lab_promotion_transaction",
            side_effect=lambda value: value,
        ):
            return build_lab_promotion_rollback_plan(
                transaction=tx,
                inspection=observed,
            )

    def kinds(
        self,
        plan,
    ):
        return [
            step.kind
            for step in plan.steps
        ]

    def test_pending_before_exact_temp_only_discards_temp(self):
        tx = transaction()

        plan = self.build(
            tx,
            inspection(
                tx,
                destination=DESTINATION_BEFORE,
                temporary=TEMP_EXACT,
            ),
        )

        self.assertEqual(
            self.kinds(
                plan
            ),
            [
                STEP_ENTER_ROLLBACK,
                STEP_REMOVE_EXACT_TEMP,
                STEP_FINISH_ROLLBACK,
            ],
        )

    def test_pending_after_reconciles_before_modify_restore(self):
        tx = transaction()

        plan = self.build(
            tx,
            inspection(
                tx,
                destination=DESTINATION_AFTER,
                temporary=TEMP_ABSENT,
            ),
        )

        self.assertEqual(
            self.kinds(
                plan
            ),
            [
                STEP_ENTER_ROLLBACK,
                STEP_RECONCILE_INSTALLED,
                STEP_RESTORE_PREIMAGE,
                STEP_MARK_RESTORED,
                STEP_FINISH_ROLLBACK,
            ],
        )

    def test_pending_after_add_plans_exact_added_file_removal(self):
        tx = transaction(
            operation=PROMOTION_OPERATION_ADD,
        )

        plan = self.build(
            tx,
            inspection(
                tx,
                destination=DESTINATION_AFTER,
                temporary=TEMP_ABSENT,
            ),
        )

        self.assertEqual(
            self.kinds(
                plan
            ),
            [
                STEP_ENTER_ROLLBACK,
                STEP_RECONCILE_INSTALLED,
                STEP_REMOVE_ADDED_FILE,
                STEP_MARK_RESTORED,
                STEP_FINISH_ROLLBACK,
            ],
        )

    def test_rolling_back_installed_before_only_marks_restored(self):
        tx = transaction(
            state=STATE_ROLLING_BACK,
            progress=PROGRESS_INSTALLED,
        )

        plan = self.build(
            tx,
            inspection(
                tx,
                destination=DESTINATION_BEFORE,
                temporary=TEMP_ABSENT,
            ),
        )

        self.assertEqual(
            self.kinds(
                plan
            ),
            [
                STEP_MARK_RESTORED,
                STEP_FINISH_ROLLBACK,
            ],
        )

    def test_rolling_back_verified_before_only_marks_restored(self):
        tx = transaction(
            state=STATE_ROLLING_BACK,
            progress=PROGRESS_VERIFIED,
        )

        plan = self.build(
            tx,
            inspection(
                tx,
                destination=DESTINATION_BEFORE,
                temporary=TEMP_ABSENT,
            ),
        )

        self.assertEqual(
            self.kinds(
                plan
            ),
            [
                STEP_MARK_RESTORED,
                STEP_FINISH_ROLLBACK,
            ],
        )

    def test_already_restored_is_noop_before_finish(self):
        tx = transaction(
            state=STATE_ROLLING_BACK,
            progress=PROGRESS_RESTORED,
        )

        plan = self.build(
            tx,
            inspection(
                tx,
                destination=DESTINATION_BEFORE,
                temporary=TEMP_ABSENT,
            ),
        )

        self.assertEqual(
            self.kinds(
                plan
            ),
            [
                STEP_FINISH_ROLLBACK,
            ],
        )

    def test_destination_other_is_refused(self):
        tx = transaction()

        with self.assertRaisesRegex(
            LabPromotionRollbackPlanError,
            "destination state is ambiguous",
        ):
            self.build(
                tx,
                inspection(
                    tx,
                    destination=DESTINATION_OTHER,
                    temporary=TEMP_EXACT,
                ),
            )

    def test_unexpected_temp_is_refused(self):
        tx = transaction()

        with self.assertRaisesRegex(
            LabPromotionRollbackPlanError,
            "temporary state is unsafe",
        ):
            self.build(
                tx,
                inspection(
                    tx,
                    destination=DESTINATION_BEFORE,
                    temporary=TEMP_UNEXPECTED,
                ),
            )

    def test_installed_before_is_refused_until_rolling_back(self):
        tx = transaction(
            state=STATE_APPLYING,
            progress=PROGRESS_INSTALLED,
        )

        with self.assertRaisesRegex(
            LabPromotionRollbackPlanError,
            "unsupported changed-file physical state",
        ):
            self.build(
                tx,
                inspection(
                    tx,
                    destination=DESTINATION_BEFORE,
                    temporary=TEMP_ABSENT,
                ),
            )

    def test_snapshot_mismatch_is_refused(self):
        tx = transaction()

        observed = inspection(
            tx,
            destination=DESTINATION_BEFORE,
            temporary=TEMP_EXACT,
        )

        observed = LabPromotionAppliedTargetInspection(
            component=observed.component,
            schema_version=observed.schema_version,
            transaction_id=observed.transaction_id,
            snapshot_id="f" * 64,
            materials_id=observed.materials_id,
            repository_path=observed.repository_path,
            repository_device=observed.repository_device,
            repository_inode=observed.repository_inode,
            branch=observed.branch,
            head=observed.head,
            files=observed.files,
        )

        with self.assertRaisesRegex(
            LabPromotionRollbackPlanError,
            "snapshot_id mismatch",
        ):
            self.build(
                tx,
                observed,
            )

    def test_plan_identity_is_deterministic(self):
        tx = transaction()

        observed = inspection(
            tx,
            destination=DESTINATION_AFTER,
            temporary=TEMP_ABSENT,
        )

        first = self.build(
            tx,
            observed,
        )

        second = self.build(
            tx,
            observed,
        )

        self.assertEqual(
            first,
            second,
        )

        self.assertEqual(
            len(
                first.plan_id
            ),
            64,
        )

    def test_real_transaction_validator_boundary_accepts_valid_transaction(self):
        tx = real_applying_transaction()

        observed = inspection(
            tx,
            destination=DESTINATION_BEFORE,
            temporary=TEMP_EXACT,
        )

        plan = build_lab_promotion_rollback_plan(
            transaction=tx,
            inspection=observed,
        )

        self.assertEqual(
            self.kinds(
                plan
            ),
            [
                STEP_ENTER_ROLLBACK,
                STEP_REMOVE_EXACT_TEMP,
                STEP_FINISH_ROLLBACK,
            ],
        )

    def test_real_transaction_validator_boundary_refuses_tampered_transaction(self):
        tx = real_applying_transaction()

        observed = inspection(
            tx,
            destination=DESTINATION_BEFORE,
            temporary=TEMP_EXACT,
        )

        tampered = replace(
            tx,
            component="tampered-transaction-component",
        )

        with self.assertRaisesRegex(
            LabPromotionRollbackPlanError,
            "transaction validation failed",
        ):
            build_lab_promotion_rollback_plan(
                transaction=tampered,
                inspection=observed,
            )

    def test_coding_candidate_provenance_does_not_change_rollback_decisions(self):
        cases = (
            ("pending-before", STATE_APPLYING, PROGRESS_PENDING, DESTINATION_BEFORE, TEMP_EXACT),
            ("pending-after", STATE_APPLYING, PROGRESS_PENDING, DESTINATION_AFTER, TEMP_ABSENT),
            ("installed", STATE_APPLYING, PROGRESS_INSTALLED, DESTINATION_AFTER, TEMP_ABSENT),
            ("verifying", STATE_VERIFYING, PROGRESS_VERIFIED, DESTINATION_AFTER, TEMP_ABSENT),
            ("rolling-back", STATE_ROLLING_BACK, PROGRESS_INSTALLED, DESTINATION_BEFORE, TEMP_ABSENT),
            ("recovery-required", STATE_RECOVERY_REQUIRED, PROGRESS_INSTALLED, DESTINATION_AFTER, TEMP_ABSENT),
            ("already-restored", STATE_ROLLING_BACK, PROGRESS_RESTORED, DESTINATION_BEFORE, TEMP_ABSENT),
            ("unexpected-destination", STATE_APPLYING, PROGRESS_PENDING, DESTINATION_OTHER, TEMP_EXACT),
            ("unexpected-temp", STATE_APPLYING, PROGRESS_PENDING, DESTINATION_BEFORE, TEMP_UNEXPECTED),
        )

        def outcome(tx, destination, temporary):
            try:
                return ("plan", self.build(tx, inspection(tx, destination=destination, temporary=temporary)))
            except LabPromotionRollbackPlanError as exc:
                return ("error", type(exc), str(exc))

        for name, state, progress, destination, temporary in cases:
            with self.subTest(case=name):
                legacy = transaction(state=state, progress=progress)
                candidate = replace(
                    legacy,
                    files=(
                        replace(
                            legacy.files[0],
                            source_kind="coding_candidate",
                            source_id=CANDIDATE_ID,
                        ),
                    ),
                )
                self.assertEqual(
                    outcome(legacy, destination, temporary),
                    outcome(candidate, destination, temporary),
                )

    def test_runtime_has_no_mutation_or_durable_store_authority(self):
        source = Path(
            planner.__file__
        ).read_text(
            encoding="utf-8"
        )

        forbidden = (
            "import os",
            "from pathlib",
            "subprocess",
            "os.write",
            "os.replace",
            "os.rename",
            "os.unlink",
            "os.remove",
            "os.mkdir",
            "os.chmod",
            "git add",
            "git commit",
            "git push",
            "load_promotion_recovery",
            "load_promotion_transaction_journal",
            "append_promotion_transaction_snapshot",
            "consume_promotion_approval",
        )

        for token in forbidden:
            self.assertNotIn(
                token,
                source,
            )


if __name__ == "__main__":
    unittest.main()
