"""Pure rollback planning from validated promotion evidence.

This module converts one exact active promotion snapshot plus one exact
READ-ONLY applied-target inspection into a deterministic rollback plan.

It deliberately has no filesystem, Git, approval-store, recovery-store,
journal, promotion, rollback-execution, or shell authority.

The planner never guesses through ambiguous physical state. In particular,
DESTINATION_OTHER and TEMP_UNEXPECTED are terminal planning errors requiring
manual recovery handling.

Physical repository mutation and durable journal mutation belong to later,
separate authority boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .lab_promotion_applied_target import (
    APPLIED_TARGET_COMPONENT,
    APPLIED_TARGET_SCHEMA_VERSION,
    DESTINATION_AFTER,
    DESTINATION_BEFORE,
    DESTINATION_OTHER,
    TEMP_ABSENT,
    TEMP_EXACT,
    TEMP_UNEXPECTED,
    LabPromotionAppliedTargetInspection,
)
from .lab_promotion_proposal import (
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
)
from .lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_PENDING,
    PROGRESS_RESTORED,
    PROGRESS_VERIFIED,
    STATE_APPLYING,
    STATE_ROLLING_BACK,
    STATE_VERIFYING,
    LabPromotionTransaction,
    LabPromotionTransactionStateError,
    validate_lab_promotion_transaction,
)


ROLLBACK_PLAN_COMPONENT = (
    "hands-free-auto-lab-promotion-rollback-plan-v1"
)
ROLLBACK_PLAN_SCHEMA_VERSION = 1

STEP_ENTER_ROLLBACK = "ENTER_ROLLBACK"
STEP_RECONCILE_INSTALLED = "RECONCILE_INSTALLED"
STEP_REMOVE_EXACT_TEMP = "REMOVE_EXACT_TEMP"
STEP_RESTORE_PREIMAGE = "RESTORE_PREIMAGE"
STEP_REMOVE_ADDED_FILE = "REMOVE_ADDED_FILE"
STEP_MARK_RESTORED = "MARK_RESTORED"
STEP_FINISH_ROLLBACK = "FINISH_ROLLBACK"

ROLLBACK_STEP_KINDS = frozenset(
    {
        STEP_ENTER_ROLLBACK,
        STEP_RECONCILE_INSTALLED,
        STEP_REMOVE_EXACT_TEMP,
        STEP_RESTORE_PREIMAGE,
        STEP_REMOVE_ADDED_FILE,
        STEP_MARK_RESTORED,
        STEP_FINISH_ROLLBACK,
    }
)

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)

_PLAN_ID_DOMAIN = (
    b"hands-free-auto-lab-promotion-rollback-plan-id-v1\x00"
)


class LabPromotionRollbackPlanError(
    RuntimeError
):
    """Rollback evidence is unsafe, inconsistent, or not automatically planable."""


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionRollbackStep:
    kind: str
    path: str | None
    operation: str | None
    expected_progress: str | None
    expected_destination: str | None
    expected_temporary: str | None


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionRollbackPlan:
    component: str
    schema_version: int
    plan_id: str
    transaction_id: str
    snapshot_id: str
    materials_id: str
    repository_path: str
    source_state: str
    steps: tuple[
        LabPromotionRollbackStep,
        ...,
    ]


def _canonical_json_bytes(
    value: object,
) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(
            ",",
            ":",
        ),
        ensure_ascii=False,
    ).encode(
        "utf-8"
    )


def _require_identifier(
    name: str,
    value: object,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionRollbackPlanError(
            f"{name} must be a string"
        )

    if (
        len(value) != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabPromotionRollbackPlanError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _step_object(
    step: LabPromotionRollbackStep,
) -> dict[
    str,
    object,
]:
    return {
        "kind": step.kind,
        "path": step.path,
        "operation": step.operation,
        "expected_progress": step.expected_progress,
        "expected_destination": step.expected_destination,
        "expected_temporary": step.expected_temporary,
    }


def _plan_identity_object(
    *,
    transaction_id: str,
    snapshot_id: str,
    materials_id: str,
    repository_path: str,
    source_state: str,
    steps: tuple[
        LabPromotionRollbackStep,
        ...,
    ],
) -> dict[
    str,
    object,
]:
    return {
        "component": ROLLBACK_PLAN_COMPONENT,
        "schema_version": ROLLBACK_PLAN_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "snapshot_id": snapshot_id,
        "materials_id": materials_id,
        "repository_path": repository_path,
        "source_state": source_state,
        "steps": [
            _step_object(
                step
            )
            for step in steps
        ],
    }


def _make_step(
    kind: str,
    *,
    path: str | None = None,
    operation: str | None = None,
    expected_progress: str | None = None,
    expected_destination: str | None = None,
    expected_temporary: str | None = None,
) -> LabPromotionRollbackStep:
    if kind not in ROLLBACK_STEP_KINDS:
        raise LabPromotionRollbackPlanError(
            f"unknown rollback step {kind!r}"
        )

    return LabPromotionRollbackStep(
        kind=kind,
        path=path,
        operation=operation,
        expected_progress=expected_progress,
        expected_destination=expected_destination,
        expected_temporary=expected_temporary,
    )


def _restore_step(
    *,
    path: str,
    operation: str,
    progress: str,
) -> LabPromotionRollbackStep:
    if operation == PROMOTION_OPERATION_MODIFY:
        kind = STEP_RESTORE_PREIMAGE
    elif operation == PROMOTION_OPERATION_ADD:
        kind = STEP_REMOVE_ADDED_FILE
    else:
        raise LabPromotionRollbackPlanError(
            f"unsupported promotion operation {operation!r}"
        )

    return _make_step(
        kind,
        path=path,
        operation=operation,
        expected_progress=progress,
        expected_destination=DESTINATION_AFTER,
        expected_temporary=TEMP_ABSENT,
    )


def _plan_file(
    *,
    source_state: str,
    path: str,
    operation: str,
    progress: str,
    destination: str,
    temporary: str,
) -> tuple[
    LabPromotionRollbackStep,
    ...,
]:
    if destination == DESTINATION_OTHER:
        raise LabPromotionRollbackPlanError(
            f"destination state is ambiguous for {path!r}"
        )

    if temporary == TEMP_UNEXPECTED:
        raise LabPromotionRollbackPlanError(
            f"temporary state is unsafe for {path!r}"
        )

    if progress == PROGRESS_PENDING:
        if destination == DESTINATION_BEFORE:
            if temporary == TEMP_EXACT:
                return (
                    _make_step(
                        STEP_REMOVE_EXACT_TEMP,
                        path=path,
                        operation=operation,
                        expected_progress=PROGRESS_PENDING,
                        expected_destination=DESTINATION_BEFORE,
                        expected_temporary=TEMP_EXACT,
                    ),
                )

            if temporary == TEMP_ABSENT:
                return ()

            raise LabPromotionRollbackPlanError(
                f"unsupported pending temporary state for {path!r}"
            )

        if (
            destination == DESTINATION_AFTER
            and temporary == TEMP_ABSENT
        ):
            return (
                _make_step(
                    STEP_RECONCILE_INSTALLED,
                    path=path,
                    operation=operation,
                    expected_progress=PROGRESS_PENDING,
                    expected_destination=DESTINATION_AFTER,
                    expected_temporary=TEMP_ABSENT,
                ),
                _restore_step(
                    path=path,
                    operation=operation,
                    progress=PROGRESS_INSTALLED,
                ),
                _make_step(
                    STEP_MARK_RESTORED,
                    path=path,
                    operation=operation,
                    expected_progress=PROGRESS_INSTALLED,
                    expected_destination=DESTINATION_BEFORE,
                    expected_temporary=TEMP_ABSENT,
                ),
            )

        raise LabPromotionRollbackPlanError(
            f"unsupported PENDING physical state for {path!r}"
        )

    if progress in {
        PROGRESS_INSTALLED,
        PROGRESS_VERIFIED,
    }:
        if (
            destination == DESTINATION_AFTER
            and temporary == TEMP_ABSENT
        ):
            return (
                _restore_step(
                    path=path,
                    operation=operation,
                    progress=progress,
                ),
                _make_step(
                    STEP_MARK_RESTORED,
                    path=path,
                    operation=operation,
                    expected_progress=progress,
                    expected_destination=DESTINATION_BEFORE,
                    expected_temporary=TEMP_ABSENT,
                ),
            )

        if (
            source_state == STATE_ROLLING_BACK
            and destination == DESTINATION_BEFORE
            and temporary == TEMP_ABSENT
        ):
            return (
                _make_step(
                    STEP_MARK_RESTORED,
                    path=path,
                    operation=operation,
                    expected_progress=progress,
                    expected_destination=DESTINATION_BEFORE,
                    expected_temporary=TEMP_ABSENT,
                ),
            )

        raise LabPromotionRollbackPlanError(
            f"unsupported changed-file physical state for {path!r}"
        )

    if progress == PROGRESS_RESTORED:
        if source_state != STATE_ROLLING_BACK:
            raise LabPromotionRollbackPlanError(
                f"RESTORED progress outside ROLLING_BACK for {path!r}"
            )

        if (
            destination != DESTINATION_BEFORE
            or temporary != TEMP_ABSENT
        ):
            raise LabPromotionRollbackPlanError(
                f"RESTORED evidence is not physically restored for {path!r}"
            )

        return ()

    raise LabPromotionRollbackPlanError(
        f"unsupported journal progress {progress!r} for {path!r}"
    )


def build_lab_promotion_rollback_plan(
    *,
    transaction: LabPromotionTransaction,
    inspection: LabPromotionAppliedTargetInspection,
) -> LabPromotionRollbackPlan:
    """Build an immutable rollback plan without external access."""
    try:
        transaction = validate_lab_promotion_transaction(
            transaction
        )
    except (
        TypeError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionRollbackPlanError(
            f"transaction validation failed: {exc}"
        ) from exc

    if transaction.state not in {
        STATE_APPLYING,
        STATE_VERIFYING,
        STATE_ROLLING_BACK,
    }:
        raise LabPromotionRollbackPlanError(
            "rollback planning requires APPLYING, VERIFYING, "
            "or ROLLING_BACK transaction"
        )

    if not isinstance(
        inspection,
        LabPromotionAppliedTargetInspection,
    ):
        raise TypeError(
            "inspection must be a LabPromotionAppliedTargetInspection"
        )

    if inspection.component != APPLIED_TARGET_COMPONENT:
        raise LabPromotionRollbackPlanError(
            "applied-target component mismatch"
        )

    if (
        inspection.schema_version
        != APPLIED_TARGET_SCHEMA_VERSION
    ):
        raise LabPromotionRollbackPlanError(
            "applied-target schema version mismatch"
        )

    materials_id = _require_identifier(
        "materials_id",
        inspection.materials_id,
    )

    identity_pairs = (
        (
            "transaction_id",
            inspection.transaction_id,
            transaction.transaction_id,
        ),
        (
            "snapshot_id",
            inspection.snapshot_id,
            transaction.snapshot_id,
        ),
        (
            "repository_path",
            inspection.repository_path,
            transaction.repository_path,
        ),
        (
            "repository_device",
            inspection.repository_device,
            transaction.repository_device,
        ),
        (
            "repository_inode",
            inspection.repository_inode,
            transaction.repository_inode,
        ),
        (
            "branch",
            inspection.branch,
            transaction.branch,
        ),
        (
            "head",
            inspection.head,
            transaction.head,
        ),
    )

    for (
        name,
        observed,
        expected,
    ) in identity_pairs:
        if observed != expected:
            raise LabPromotionRollbackPlanError(
                f"applied-target {name} mismatch"
            )

    if len(
        inspection.files
    ) != len(
        transaction.files
    ):
        raise LabPromotionRollbackPlanError(
            "transaction/applied-target file count mismatch"
        )

    steps: list[
        LabPromotionRollbackStep
    ] = []

    if transaction.state in {
        STATE_APPLYING,
        STATE_VERIFYING,
    }:
        steps.append(
            _make_step(
                STEP_ENTER_ROLLBACK,
            )
        )

    for (
        transaction_file,
        applied_file,
    ) in zip(
        transaction.files,
        inspection.files,
        strict=True,
    ):
        if applied_file.path != transaction_file.path:
            raise LabPromotionRollbackPlanError(
                "transaction/applied-target path mismatch"
            )

        if applied_file.operation != transaction_file.operation:
            raise LabPromotionRollbackPlanError(
                f"operation mismatch for {transaction_file.path!r}"
            )

        if (
            applied_file.journal_progress
            != transaction_file.progress
        ):
            raise LabPromotionRollbackPlanError(
                f"journal progress mismatch for {transaction_file.path!r}"
            )

        if applied_file.destination.path != transaction_file.path:
            raise LabPromotionRollbackPlanError(
                f"destination path mismatch for {transaction_file.path!r}"
            )

        if (
            applied_file.destination.operation
            != transaction_file.operation
        ):
            raise LabPromotionRollbackPlanError(
                f"destination operation mismatch for {transaction_file.path!r}"
            )

        if (
            applied_file.destination.journal_progress
            != transaction_file.progress
        ):
            raise LabPromotionRollbackPlanError(
                f"destination progress mismatch for {transaction_file.path!r}"
            )

        if applied_file.temporary.path != transaction_file.path:
            raise LabPromotionRollbackPlanError(
                f"temporary destination path mismatch for {transaction_file.path!r}"
            )

        steps.extend(
            _plan_file(
                source_state=transaction.state,
                path=transaction_file.path,
                operation=transaction_file.operation,
                progress=transaction_file.progress,
                destination=applied_file.destination.status,
                temporary=applied_file.temporary.status,
            )
        )

    steps.append(
        _make_step(
            STEP_FINISH_ROLLBACK,
        )
    )

    frozen_steps = tuple(
        steps
    )

    identity_object = _plan_identity_object(
        transaction_id=transaction.transaction_id,
        snapshot_id=transaction.snapshot_id,
        materials_id=materials_id,
        repository_path=transaction.repository_path,
        source_state=transaction.state,
        steps=frozen_steps,
    )

    plan_id = hashlib.sha256(
        _PLAN_ID_DOMAIN
        + _canonical_json_bytes(
            identity_object
        )
    ).hexdigest()

    return LabPromotionRollbackPlan(
        component=ROLLBACK_PLAN_COMPONENT,
        schema_version=ROLLBACK_PLAN_SCHEMA_VERSION,
        plan_id=plan_id,
        transaction_id=transaction.transaction_id,
        snapshot_id=transaction.snapshot_id,
        materials_id=materials_id,
        repository_path=transaction.repository_path,
        source_state=transaction.state,
        steps=frozen_steps,
    )
