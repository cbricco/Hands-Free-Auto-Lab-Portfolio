"""Execute one exact deterministic promotion rollback plan.

This module owns a deliberately narrow repository-mutation boundary for
rollback of already-active promotion transactions.

It may:
- consume an exact deterministic rollback plan,
- consume existing durable recovery material and preimages,
- remove an exact plan-authorized promotion temporary,
- restore an exact MODIFY preimage through descriptor-bound replacement,
- append exact legal rollback transaction progress.

It does not:
- decide that rollback is authorized or desirable,
- build human approval,
- consume promotion approval,
- create or regenerate recovery material,
- support ADD/DELETE rollback in this initial executor,
- stage, commit, or push Git state,
- invoke shell or network activity,
- retry or clean ambiguous failures automatically.

Any ambiguous failure after rollback execution begins is terminalized as
RECOVERY_REQUIRED when the durable journal still permits that transition.
Existing physical and journal evidence is otherwise preserved.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import PurePosixPath
import stat
from typing import Mapping

from .lab_promotion_applied_target import (
    DESTINATION_AFTER,
    DESTINATION_BEFORE,
    TEMP_EXACT,
    LabPromotionAppliedTargetError,
    inspect_promotion_applied_target,
)
from .lab_promotion_proposal import (
    PROMOTION_OPERATION_MODIFY,
)
from .lab_promotion_recovery_materials import (
    LabPromotionRecoveryMaterialsError,
    load_promotion_recovery_materials,
    load_promotion_recovery_preimages,
)
from .lab_promotion_rollback_plan import (
    STEP_ENTER_ROLLBACK,
    STEP_FINISH_ROLLBACK,
    STEP_MARK_RESTORED,
    STEP_RECONCILE_INSTALLED,
    STEP_REMOVE_ADDED_FILE,
    STEP_REMOVE_EXACT_TEMP,
    STEP_RESTORE_PREIMAGE,
    LabPromotionRollbackPlan,
    LabPromotionRollbackPlanError,
    LabPromotionRollbackStep,
    build_lab_promotion_rollback_plan,
)
from .lab_promotion_transaction_journal import (
    LabPromotionTransactionJournalError,
    append_promotion_transaction_snapshot,
    load_promotion_transaction_journal,
)
from .lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_RESTORED,
    STATE_APPLYING,
    STATE_RECOVERY_REQUIRED,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    STATE_VERIFYING,
    LabPromotionTransaction,
    LabPromotionTransactionStateError,
    transition_lab_promotion_transaction,
    validate_lab_promotion_transaction,
)


_ROLLBACK_TEMP_DOMAIN = (
    b"hands-free-auto-lab-promotion-rollback-temp-v1\x00"
)

_ACTIVE_STATES = frozenset(
    {
        STATE_APPLYING,
        STATE_VERIFYING,
        STATE_ROLLING_BACK,
    }
)


class LabPromotionRollbackExecutorError(
    RuntimeError
):
    """Raised when bounded rollback execution cannot proceed safely."""


def _progress(
    transaction: LabPromotionTransaction,
) -> dict[str, str]:
    return {
        item.path: item.progress
        for item in transaction.files
    }


def _require_secure_platform() -> None:
    missing: list[str] = []

    for name in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
    ):
        if not hasattr(
            os,
            name,
        ):
            missing.append(
                f"os.{name}"
            )

    if not hasattr(
        os,
        "geteuid",
    ):
        missing.append(
            "os.geteuid"
        )

    if os.open not in os.supports_dir_fd:
        missing.append(
            "os.open(dir_fd=...)"
        )

    if os.stat not in os.supports_dir_fd:
        missing.append(
            "os.stat(dir_fd=...)"
        )

    if os.unlink not in os.supports_dir_fd:
        missing.append(
            "os.unlink(dir_fd=...)"
        )

    if os.listdir not in os.supports_fd:
        missing.append(
            "os.listdir(fd)"
        )

    if missing:
        raise LabPromotionRollbackExecutorError(
            "secure rollback primitives unavailable: "
            + ", ".join(
                sorted(
                    missing
                )
            )
        )


def _directory_flags() -> int:
    _require_secure_platform()

    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
    )

    if hasattr(
        os,
        "O_CLOEXEC",
    ):
        flags |= os.O_CLOEXEC

    return flags


def _read_flags() -> int:
    _require_secure_platform()

    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
    )

    if hasattr(
        os,
        "O_CLOEXEC",
    ):
        flags |= os.O_CLOEXEC

    return flags


def _create_flags() -> int:
    _require_secure_platform()

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
    )

    if hasattr(
        os,
        "O_CLOEXEC",
    ):
        flags |= os.O_CLOEXEC

    return flags


def _same_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _validate_safe_directory(
    state: os.stat_result,
    *,
    name: str,
) -> None:
    if not stat.S_ISDIR(
        state.st_mode
    ):
        raise LabPromotionRollbackExecutorError(
            f"{name} is not a directory"
        )

    if (
        state.st_uid
        != os.geteuid()
    ):
        raise LabPromotionRollbackExecutorError(
            f"{name} is not owned by current euid"
        )

    if state.st_mode & 0o022:
        raise LabPromotionRollbackExecutorError(
            f"{name} is group- or other-writable"
        )


def _open_repository(
    transaction: LabPromotionTransaction,
) -> int:
    try:
        fd = os.open(
            transaction.repository_path,
            _directory_flags(),
        )
    except OSError as exc:
        raise LabPromotionRollbackExecutorError(
            f"cannot securely open rollback repository: {exc}"
        ) from exc

    try:
        state = os.fstat(
            fd
        )

        _validate_safe_directory(
            state,
            name="rollback repository",
        )

        if (
            state.st_dev
            != transaction.repository_device
            or state.st_ino
            != transaction.repository_inode
        ):
            raise LabPromotionRollbackExecutorError(
                "rollback repository device/inode mismatch"
            )

        return fd

    except Exception:
        os.close(
            fd
        )
        raise


def _path_components(
    path: str,
) -> tuple[str, ...]:
    raw = PurePosixPath(
        path
    )

    components = raw.parts

    if (
        not components
        or raw.is_absolute()
        or any(
            part in {
                "",
                ".",
                "..",
            }
            for part in components
        )
    ):
        raise LabPromotionRollbackExecutorError(
            f"unsafe rollback path: {path!r}"
        )

    return components


def _open_parent(
    repository_fd: int,
    *,
    path: str,
) -> tuple[int, str]:
    components = _path_components(
        path
    )

    current_fd = os.dup(
        repository_fd
    )

    try:
        _validate_safe_directory(
            os.fstat(
                current_fd
            ),
            name="rollback repository root",
        )

        for component in components[:-1]:
            try:
                child_fd = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise LabPromotionRollbackExecutorError(
                    f"cannot securely traverse rollback parent "
                    f"for {path!r}: {exc}"
                ) from exc

            try:
                _validate_safe_directory(
                    os.fstat(
                        child_fd
                    ),
                    name=(
                        "rollback parent "
                        f"{component!r}"
                    ),
                )
            except Exception:
                os.close(
                    child_fd
                )
                raise

            os.close(
                current_fd
            )
            current_fd = child_fd

        return (
            current_fd,
            components[-1],
        )

    except Exception:
        os.close(
            current_fd
        )
        raise


def _validate_git_entry(
    state: os.stat_result,
    *,
    name: str,
) -> None:
    if (
        state.st_uid
        != os.geteuid()
    ):
        raise LabPromotionRollbackExecutorError(
            f"{name} is not owned by current euid"
        )

    if state.st_mode & 0o022:
        raise LabPromotionRollbackExecutorError(
            f"{name} is group- or other-writable"
        )


def _walk_git_tree(
    directory_fd: int,
    *,
    components: tuple[str, ...],
) -> None:
    try:
        names = sorted(
            os.listdir(
                directory_fd
            )
        )
    except OSError as exc:
        raise LabPromotionRollbackExecutorError(
            "cannot enumerate .git metadata tree: "
            f"{exc}"
        ) from exc

    for name in names:
        if name in {
            ".",
            "..",
        }:
            raise LabPromotionRollbackExecutorError(
                "invalid .git metadata entry"
            )

        if (
            components
            == (
                ".git",
                "objects",
                "info",
            )
            and name == "alternates"
        ):
            raise LabPromotionRollbackExecutorError(
                "Git object alternates are unsupported "
                "by rollback executor"
            )

        try:
            observed = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LabPromotionRollbackExecutorError(
                f"cannot inspect .git metadata "
                f"{'/'.join(components + (name,))!r}: {exc}"
            ) from exc

        label = (
            "Git metadata "
            + "/".join(
                components
                + (
                    name,
                )
            )
        )

        _validate_git_entry(
            observed,
            name=label,
        )

        if stat.S_ISDIR(
            observed.st_mode
        ):
            try:
                child_fd = os.open(
                    name,
                    _directory_flags(),
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise LabPromotionRollbackExecutorError(
                    f"cannot securely open {label}: {exc}"
                ) from exc

            try:
                reopened = os.fstat(
                    child_fd
                )

                if not _same_identity(
                    observed,
                    reopened,
                ):
                    raise LabPromotionRollbackExecutorError(
                        f"{label} changed while opening"
                    )

                _validate_safe_directory(
                    reopened,
                    name=label,
                )

                _walk_git_tree(
                    child_fd,
                    components=(
                        components
                        + (
                            name,
                        )
                    ),
                )

            finally:
                os.close(
                    child_fd
                )

        elif stat.S_ISREG(
            observed.st_mode
        ):
            try:
                file_fd = os.open(
                    name,
                    _read_flags(),
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise LabPromotionRollbackExecutorError(
                    f"cannot securely reopen {label}: {exc}"
                ) from exc

            try:
                reopened = os.fstat(
                    file_fd
                )

                if not _same_identity(
                    observed,
                    reopened,
                ):
                    raise LabPromotionRollbackExecutorError(
                        f"{label} changed while opening"
                    )

                if not stat.S_ISREG(
                    reopened.st_mode
                ):
                    raise LabPromotionRollbackExecutorError(
                        f"{label} is not a regular file"
                    )

                _validate_git_entry(
                    reopened,
                    name=label,
                )

            finally:
                os.close(
                    file_fd
                )

        else:
            raise LabPromotionRollbackExecutorError(
                f"{label} has unsupported file type"
            )


def _validate_git_tree(
    repository_fd: int,
) -> None:
    try:
        observed = os.stat(
            ".git",
            dir_fd=repository_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LabPromotionRollbackExecutorError(
            f"cannot inspect real .git directory: {exc}"
        ) from exc

    if not stat.S_ISDIR(
        observed.st_mode
    ):
        raise LabPromotionRollbackExecutorError(
            ".git must be a real directory"
        )

    _validate_git_entry(
        observed,
        name=".git",
    )

    try:
        git_fd = os.open(
            ".git",
            _directory_flags(),
            dir_fd=repository_fd,
        )
    except OSError as exc:
        raise LabPromotionRollbackExecutorError(
            f"cannot securely open .git: {exc}"
        ) from exc

    try:
        reopened = os.fstat(
            git_fd
        )

        if not _same_identity(
            observed,
            reopened,
        ):
            raise LabPromotionRollbackExecutorError(
                ".git changed while opening"
            )

        _validate_safe_directory(
            reopened,
            name=".git",
        )

        _walk_git_tree(
            git_fd,
            components=(
                ".git",
            ),
        )

    finally:
        os.close(
            git_fd
        )


def _read_exact_regular_file(
    parent_fd: int,
    *,
    name: str,
    path: str,
    expected_bytes: int,
    expected_sha256: str,
    expected_mode: int,
) -> None:
    try:
        before = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LabPromotionRollbackExecutorError(
            f"cannot inspect exact rollback file {path!r}: {exc}"
        ) from exc

    if not stat.S_ISREG(
        before.st_mode
    ):
        raise LabPromotionRollbackExecutorError(
            f"rollback file is not regular: {path!r}"
        )

    if (
        before.st_uid
        != os.geteuid()
    ):
        raise LabPromotionRollbackExecutorError(
            f"rollback file is not current-user-owned: {path!r}"
        )

    if before.st_nlink != 1:
        raise LabPromotionRollbackExecutorError(
            f"rollback file must have one hard link: {path!r}"
        )

    if stat.S_IMODE(
        before.st_mode
    ) != expected_mode:
        raise LabPromotionRollbackExecutorError(
            f"rollback file mode mismatch: {path!r}"
        )

    if before.st_size != expected_bytes:
        raise LabPromotionRollbackExecutorError(
            f"rollback file byte count mismatch: {path!r}"
        )

    try:
        fd = os.open(
            name,
            _read_flags(),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise LabPromotionRollbackExecutorError(
            f"cannot securely open rollback file {path!r}: {exc}"
        ) from exc

    try:
        opened = os.fstat(
            fd
        )

        if not _same_identity(
            before,
            opened,
        ):
            raise LabPromotionRollbackExecutorError(
                f"rollback file changed while opening: {path!r}"
            )

        digest = hashlib.sha256()
        count = 0

        while True:
            chunk = os.read(
                fd,
                64 * 1024,
            )

            if not chunk:
                break

            count += len(
                chunk
            )

            if count > expected_bytes:
                raise LabPromotionRollbackExecutorError(
                    f"rollback file grew while reading: {path!r}"
                )

            digest.update(
                chunk
            )

        if count != expected_bytes:
            raise LabPromotionRollbackExecutorError(
                f"rollback file byte count changed: {path!r}"
            )

        if (
            digest.hexdigest()
            != expected_sha256
        ):
            raise LabPromotionRollbackExecutorError(
                f"rollback file SHA-256 mismatch: {path!r}"
            )

        final_fd_state = os.fstat(
            fd
        )

        if not _same_identity(
            opened,
            final_fd_state,
        ):
            raise LabPromotionRollbackExecutorError(
                f"rollback file identity changed during read: {path!r}"
            )

        try:
            final_path_state = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LabPromotionRollbackExecutorError(
                f"cannot revalidate rollback file {path!r}: {exc}"
            ) from exc

        if not _same_identity(
            final_fd_state,
            final_path_state,
        ):
            raise LabPromotionRollbackExecutorError(
                f"rollback path changed after read: {path!r}"
            )

    finally:
        os.close(
            fd
        )


def _write_all(
    fd: int,
    raw: bytes,
) -> None:
    view = memoryview(
        raw
    )

    while view:
        written = os.write(
            fd,
            view,
        )

        if written <= 0:
            raise LabPromotionRollbackExecutorError(
                "short write while creating rollback preimage temporary"
            )

        view = view[
            written:
        ]


def _transaction_file(
    transaction: LabPromotionTransaction,
    path: str,
):
    matches = [
        item
        for item in transaction.files
        if item.path == path
    ]

    if len(
        matches
    ) != 1:
        raise LabPromotionRollbackExecutorError(
            f"rollback transaction path mismatch: {path!r}"
        )

    return matches[0]


def _material_file(
    materials,
    path: str,
):
    matches = [
        item
        for item in materials.files
        if item.path == path
    ]

    if len(
        matches
    ) != 1:
        raise LabPromotionRollbackExecutorError(
            f"rollback recovery-material path mismatch: {path!r}"
        )

    return matches[0]


def _append_transition(
    *,
    journal_root: str | os.PathLike[str],
    current: LabPromotionTransaction,
    state: str,
    progress_by_path: Mapping[str, str],
) -> LabPromotionTransaction:
    successor = transition_lab_promotion_transaction(
        current,
        state=state,
        progress_by_path=progress_by_path,
    )

    return append_promotion_transaction_snapshot(
        journal_root,
        successor=successor,
        expected_previous_snapshot_id=current.snapshot_id,
    )


def _preflight_repository(
    transaction: LabPromotionTransaction,
) -> None:
    repository_fd = _open_repository(
        transaction
    )

    try:
        _validate_git_tree(
            repository_fd
        )

        for item in transaction.files:
            parent_fd, _ = _open_parent(
                repository_fd,
                path=item.path,
            )

            os.close(
                parent_fd
            )

    finally:
        os.close(
            repository_fd
        )


def _require_exact_plan(
    *,
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
    plan: LabPromotionRollbackPlan,
):
    try:
        transaction = validate_lab_promotion_transaction(
            transaction
        )
    except (
        TypeError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionRollbackExecutorError(
            f"rollback transaction validation failed: {exc}"
        ) from exc

    if transaction.state not in _ACTIVE_STATES:
        raise LabPromotionRollbackExecutorError(
            "rollback executor requires APPLYING, VERIFYING, "
            "or ROLLING_BACK transaction"
        )

    if any(
        item.operation
        != PROMOTION_OPERATION_MODIFY
        for item in transaction.files
    ):
        raise LabPromotionRollbackExecutorError(
            "initial rollback executor is MODIFY-only"
        )

    if type(
        plan
    ) is not LabPromotionRollbackPlan:
        raise LabPromotionRollbackExecutorError(
            "rollback plan must be exact LabPromotionRollbackPlan type"
        )

    try:
        durable = load_promotion_transaction_journal(
            journal_root,
            transaction_id=transaction.transaction_id,
        )
    except LabPromotionTransactionJournalError as exc:
        raise LabPromotionRollbackExecutorError(
            f"durable rollback journal failed validation: {exc}"
        ) from exc

    if durable != transaction:
        raise LabPromotionRollbackExecutorError(
            "durable journal does not match exact rollback snapshot"
        )

    _preflight_repository(
        transaction
    )

    try:
        inspection = inspect_promotion_applied_target(
            journal_root=journal_root,
            recovery_root=recovery_root,
            transaction=transaction,
        )

        expected_plan = build_lab_promotion_rollback_plan(
            transaction=transaction,
            inspection=inspection,
        )
    except (
        LabPromotionAppliedTargetError,
        LabPromotionRollbackPlanError,
    ) as exc:
        raise LabPromotionRollbackExecutorError(
            f"rollback physical-plan validation failed: {exc}"
        ) from exc

    if expected_plan != plan:
        raise LabPromotionRollbackExecutorError(
            "rollback plan does not match exact current physical state"
        )

    try:
        materials = load_promotion_recovery_materials(
            recovery_root,
            transaction=transaction,
        )

        preimages = load_promotion_recovery_preimages(
            recovery_root,
            transaction=transaction,
        )
    except LabPromotionRecoveryMaterialsError as exc:
        raise LabPromotionRollbackExecutorError(
            f"durable rollback recovery evidence failed validation: {exc}"
        ) from exc

    if materials.materials_id != plan.materials_id:
        raise LabPromotionRollbackExecutorError(
            "rollback recovery-material identity mismatch"
        )

    transaction_paths = tuple(
        item.path
        for item in transaction.files
    )

    material_paths = tuple(
        item.path
        for item in materials.files
    )

    if material_paths != transaction_paths:
        raise LabPromotionRollbackExecutorError(
            "rollback recovery-material path set mismatch"
        )

    if set(
        preimages
    ) != set(
        transaction_paths
    ):
        raise LabPromotionRollbackExecutorError(
            "rollback MODIFY preimage set mismatch"
        )

    if not plan.steps:
        raise LabPromotionRollbackExecutorError(
            "rollback plan contains no steps"
        )

    if any(
        step.kind == STEP_REMOVE_ADDED_FILE
        for step in plan.steps
    ):
        raise LabPromotionRollbackExecutorError(
            "initial rollback executor refuses ADD removal"
        )

    return (
        transaction,
        materials,
        preimages,
    )


def _inspect_current(
    *,
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
):
    try:
        return inspect_promotion_applied_target(
            journal_root=journal_root,
            recovery_root=recovery_root,
            transaction=transaction,
        )
    except LabPromotionAppliedTargetError as exc:
        raise LabPromotionRollbackExecutorError(
            f"rollback applied-target reinspection failed: {exc}"
        ) from exc


def _require_step_observation(
    *,
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
    step: LabPromotionRollbackStep,
):
    if step.path is None:
        raise LabPromotionRollbackExecutorError(
            f"{step.kind} requires a path"
        )

    transaction_file = _transaction_file(
        transaction,
        step.path,
    )

    if (
        step.operation is not None
        and transaction_file.operation
        != step.operation
    ):
        raise LabPromotionRollbackExecutorError(
            f"rollback step operation mismatch for {step.path!r}"
        )

    if (
        step.expected_progress is not None
        and transaction_file.progress
        != step.expected_progress
    ):
        raise LabPromotionRollbackExecutorError(
            f"rollback step progress mismatch for {step.path!r}"
        )

    inspection = _inspect_current(
        journal_root=journal_root,
        recovery_root=recovery_root,
        transaction=transaction,
    )

    matches = [
        item
        for item in inspection.files
        if item.path == step.path
    ]

    if len(
        matches
    ) != 1:
        raise LabPromotionRollbackExecutorError(
            f"rollback applied inspection path mismatch: {step.path!r}"
        )

    observed = matches[0]

    if (
        step.expected_destination is not None
        and observed.destination.status
        != step.expected_destination
    ):
        raise LabPromotionRollbackExecutorError(
            f"rollback destination state changed for {step.path!r}"
        )

    if (
        step.expected_temporary is not None
        and observed.temporary.status
        != step.expected_temporary
    ):
        raise LabPromotionRollbackExecutorError(
            f"rollback temporary state changed for {step.path!r}"
        )

    return transaction_file


def _validate_expected_destination(
    parent_fd: int,
    *,
    destination_name: str,
    transaction_file,
    expected_destination: str | None,
) -> None:
    if expected_destination == DESTINATION_AFTER:
        expected_bytes = transaction_file.after_bytes
        expected_sha256 = transaction_file.after_sha256
        expected_mode = transaction_file.after_mode

    elif expected_destination == DESTINATION_BEFORE:
        if (
            transaction_file.before_bytes is None
            or transaction_file.before_sha256 is None
            or transaction_file.before_mode is None
        ):
            raise LabPromotionRollbackExecutorError(
                f"MODIFY before evidence missing for "
                f"{transaction_file.path!r}"
            )

        expected_bytes = transaction_file.before_bytes
        expected_sha256 = transaction_file.before_sha256
        expected_mode = transaction_file.before_mode

    else:
        raise LabPromotionRollbackExecutorError(
            f"unsupported expected rollback destination "
            f"for {transaction_file.path!r}"
        )

    _read_exact_regular_file(
        parent_fd,
        name=destination_name,
        path=transaction_file.path,
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha256,
        expected_mode=expected_mode,
    )


def _remove_exact_promotion_temp(
    *,
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
    materials,
    step: LabPromotionRollbackStep,
) -> None:
    transaction_file = _require_step_observation(
        journal_root=journal_root,
        recovery_root=recovery_root,
        transaction=transaction,
        step=step,
    )

    if step.expected_temporary != TEMP_EXACT:
        raise LabPromotionRollbackExecutorError(
            f"REMOVE_EXACT_TEMP lacks exact temp expectation "
            f"for {transaction_file.path!r}"
        )

    material = _material_file(
        materials,
        transaction_file.path,
    )

    repository_fd = _open_repository(
        transaction
    )

    try:
        _validate_git_tree(
            repository_fd
        )

        parent_fd, destination_name = _open_parent(
            repository_fd,
            path=transaction_file.path,
        )

        try:
            _validate_expected_destination(
                parent_fd,
                destination_name=destination_name,
                transaction_file=transaction_file,
                expected_destination=step.expected_destination,
            )

            _read_exact_regular_file(
                parent_fd,
                name=material.after_temp_name,
                path=(
                    transaction_file.path
                    + " [promotion temporary]"
                ),
                expected_bytes=transaction_file.after_bytes,
                expected_sha256=transaction_file.after_sha256,
                expected_mode=transaction_file.after_mode,
            )

            os.unlink(
                material.after_temp_name,
                dir_fd=parent_fd,
            )

            os.fsync(
                parent_fd
            )

        except OSError as exc:
            raise LabPromotionRollbackExecutorError(
                f"cannot remove exact promotion temporary for "
                f"{transaction_file.path!r}: {exc}"
            ) from exc

        finally:
            os.close(
                parent_fd
            )

    finally:
        os.close(
            repository_fd
        )


def _rollback_temp_name(
    *,
    transaction_id: str,
    path: str,
) -> str:
    digest = hashlib.sha256(
        _ROLLBACK_TEMP_DOMAIN
        + transaction_id.encode(
            "ascii"
        )
        + b"\x00"
        + path.encode(
            "utf-8"
        )
    ).hexdigest()

    return (
        ".hands-free-auto-lab-rollback-"
        + digest
        + ".tmp"
    )


def _restore_preimage(
    *,
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
    materials,
    preimages: Mapping[str, bytes],
    step: LabPromotionRollbackStep,
) -> None:
    transaction_file = _require_step_observation(
        journal_root=journal_root,
        recovery_root=recovery_root,
        transaction=transaction,
        step=step,
    )

    if step.expected_destination != DESTINATION_AFTER:
        raise LabPromotionRollbackExecutorError(
            f"RESTORE_PREIMAGE requires exact after destination "
            f"for {transaction_file.path!r}"
        )

    if (
        transaction_file.before_bytes is None
        or transaction_file.before_sha256 is None
        or transaction_file.before_mode is None
    ):
        raise LabPromotionRollbackExecutorError(
            f"MODIFY preimage evidence missing for "
            f"{transaction_file.path!r}"
        )

    try:
        preimage = preimages[
            transaction_file.path
        ]
    except KeyError as exc:
        raise LabPromotionRollbackExecutorError(
            f"durable preimage missing for {transaction_file.path!r}"
        ) from exc

    if (
        len(
            preimage
        )
        != transaction_file.before_bytes
        or hashlib.sha256(
            preimage
        ).hexdigest()
        != transaction_file.before_sha256
    ):
        raise LabPromotionRollbackExecutorError(
            f"loaded preimage identity mismatch for "
            f"{transaction_file.path!r}"
        )

    material = _material_file(
        materials,
        transaction_file.path,
    )

    temporary_name = _rollback_temp_name(
        transaction_id=transaction.transaction_id,
        path=transaction_file.path,
    )

    if temporary_name == material.after_temp_name:
        raise LabPromotionRollbackExecutorError(
            "rollback temporary collides with promotion temporary"
        )

    repository_fd = _open_repository(
        transaction
    )

    try:
        _validate_git_tree(
            repository_fd
        )

        parent_fd, destination_name = _open_parent(
            repository_fd,
            path=transaction_file.path,
        )

        temporary_created = False

        try:
            try:
                temporary_fd = os.open(
                    temporary_name,
                    _create_flags(),
                    transaction_file.before_mode,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise LabPromotionRollbackExecutorError(
                    f"cannot create rollback preimage temporary for "
                    f"{transaction_file.path!r}: {exc}"
                ) from exc

            temporary_created = True

            try:
                os.fchmod(
                    temporary_fd,
                    transaction_file.before_mode,
                )

                _write_all(
                    temporary_fd,
                    preimage,
                )

                os.fsync(
                    temporary_fd
                )

            except OSError as exc:
                raise LabPromotionRollbackExecutorError(
                    f"cannot persist rollback preimage temporary for "
                    f"{transaction_file.path!r}: {exc}"
                ) from exc

            finally:
                os.close(
                    temporary_fd
                )

            os.fsync(
                parent_fd
            )

            _validate_expected_destination(
                parent_fd,
                destination_name=destination_name,
                transaction_file=transaction_file,
                expected_destination=DESTINATION_AFTER,
            )

            _read_exact_regular_file(
                parent_fd,
                name=temporary_name,
                path=(
                    transaction_file.path
                    + " [rollback preimage temporary]"
                ),
                expected_bytes=transaction_file.before_bytes,
                expected_sha256=transaction_file.before_sha256,
                expected_mode=transaction_file.before_mode,
            )

            try:
                os.replace(
                    temporary_name,
                    destination_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )

                temporary_created = False

                os.fsync(
                    parent_fd
                )

            except OSError as exc:
                raise LabPromotionRollbackExecutorError(
                    f"cannot install rollback preimage for "
                    f"{transaction_file.path!r}: {exc}"
                ) from exc

        finally:
            # Deliberately do not unlink an ambiguous rollback temporary.
            # If it still exists, it remains evidence for human recovery.
            _ = temporary_created

            os.close(
                parent_fd
            )

    finally:
        os.close(
            repository_fd
        )


def _mark_progress(
    *,
    journal_root: str | os.PathLike[str],
    current: LabPromotionTransaction,
    path: str,
    progress: str,
) -> LabPromotionTransaction:
    progress_by_path = _progress(
        current
    )

    progress_by_path[
        path
    ] = progress

    return _append_transition(
        journal_root=journal_root,
        current=current,
        state=STATE_ROLLING_BACK,
        progress_by_path=progress_by_path,
    )


def _require_finish_ready(
    *,
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    current: LabPromotionTransaction,
    step: LabPromotionRollbackStep,
) -> None:
    if (
        step.path is not None
        or step.operation is not None
    ):
        raise LabPromotionRollbackExecutorError(
            "FINISH_ROLLBACK must not carry a file target"
        )

    inspection = _inspect_current(
        journal_root=journal_root,
        recovery_root=recovery_root,
        transaction=current,
    )

    try:
        remaining = build_lab_promotion_rollback_plan(
            transaction=current,
            inspection=inspection,
        )
    except LabPromotionRollbackPlanError as exc:
        raise LabPromotionRollbackExecutorError(
            f"cannot verify rollback completion readiness: {exc}"
        ) from exc

    if (
        len(
            remaining.steps
        ) != 1
        or remaining.steps[0].kind
        != STEP_FINISH_ROLLBACK
    ):
        raise LabPromotionRollbackExecutorError(
            "rollback is not physically ready for ROLLED_BACK"
        )


def _fail_closed(
    *,
    journal_root: str | os.PathLike[str],
    transaction_id: str,
) -> LabPromotionTransaction:
    try:
        durable = load_promotion_transaction_journal(
            journal_root,
            transaction_id=transaction_id,
        )
    except LabPromotionTransactionJournalError as exc:
        raise LabPromotionRollbackExecutorError(
            "rollback failed and durable journal cannot be reopened; "
            "preserve all evidence: "
            f"{exc}"
        ) from exc

    if durable.state in {
        STATE_RECOVERY_REQUIRED,
        STATE_ROLLED_BACK,
    }:
        return durable

    if durable.state not in _ACTIVE_STATES:
        raise LabPromotionRollbackExecutorError(
            "rollback failed from unexpected durable state; "
            "preserve all evidence"
        )

    try:
        return _append_transition(
            journal_root=journal_root,
            current=durable,
            state=STATE_RECOVERY_REQUIRED,
            progress_by_path=_progress(
                durable
            ),
        )
    except (
        LabPromotionTransactionJournalError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionRollbackExecutorError(
            "rollback failed and RECOVERY_REQUIRED could not be "
            "durably recorded; preserve all evidence: "
            f"{exc}"
        ) from exc


def execute_lab_promotion_rollback(
    *,
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
    plan: LabPromotionRollbackPlan,
) -> LabPromotionTransaction:
    """Execute one exact already-built MODIFY-only rollback plan."""

    (
        current,
        materials,
        preimages,
    ) = _require_exact_plan(
        journal_root=journal_root,
        recovery_root=recovery_root,
        transaction=transaction,
        plan=plan,
    )

    try:
        for step in plan.steps:
            if step.kind == STEP_ENTER_ROLLBACK:
                if (
                    step.path is not None
                    or current.state
                    not in {
                        STATE_APPLYING,
                        STATE_VERIFYING,
                    }
                ):
                    raise LabPromotionRollbackExecutorError(
                        "invalid ENTER_ROLLBACK step"
                    )

                current = _append_transition(
                    journal_root=journal_root,
                    current=current,
                    state=STATE_ROLLING_BACK,
                    progress_by_path=_progress(
                        current
                    ),
                )

            elif step.kind == STEP_RECONCILE_INSTALLED:
                transaction_file = _require_step_observation(
                    journal_root=journal_root,
                    recovery_root=recovery_root,
                    transaction=current,
                    step=step,
                )

                current = _mark_progress(
                    journal_root=journal_root,
                    current=current,
                    path=transaction_file.path,
                    progress=PROGRESS_INSTALLED,
                )

            elif step.kind == STEP_REMOVE_EXACT_TEMP:
                _remove_exact_promotion_temp(
                    journal_root=journal_root,
                    recovery_root=recovery_root,
                    transaction=current,
                    materials=materials,
                    step=step,
                )

            elif step.kind == STEP_RESTORE_PREIMAGE:
                _restore_preimage(
                    journal_root=journal_root,
                    recovery_root=recovery_root,
                    transaction=current,
                    materials=materials,
                    preimages=preimages,
                    step=step,
                )

            elif step.kind == STEP_MARK_RESTORED:
                transaction_file = _require_step_observation(
                    journal_root=journal_root,
                    recovery_root=recovery_root,
                    transaction=current,
                    step=step,
                )

                current = _mark_progress(
                    journal_root=journal_root,
                    current=current,
                    path=transaction_file.path,
                    progress=PROGRESS_RESTORED,
                )

            elif step.kind == STEP_FINISH_ROLLBACK:
                _require_finish_ready(
                    journal_root=journal_root,
                    recovery_root=recovery_root,
                    current=current,
                    step=step,
                )

                current = _append_transition(
                    journal_root=journal_root,
                    current=current,
                    state=STATE_ROLLED_BACK,
                    progress_by_path=_progress(
                        current
                    ),
                )

            elif step.kind == STEP_REMOVE_ADDED_FILE:
                raise LabPromotionRollbackExecutorError(
                    "initial rollback executor refuses ADD removal"
                )

            else:
                raise LabPromotionRollbackExecutorError(
                    f"unsupported rollback step kind: {step.kind!r}"
                )

        if current.state != STATE_ROLLED_BACK:
            raise LabPromotionRollbackExecutorError(
                "rollback plan ended without ROLLED_BACK"
            )

        durable = load_promotion_transaction_journal(
            journal_root,
            transaction_id=current.transaction_id,
        )

        if durable != current:
            raise LabPromotionRollbackExecutorError(
                "final rollback journal does not match exact result"
            )

        return current

    except Exception as exc:
        try:
            recovery = _fail_closed(
                journal_root=journal_root,
                transaction_id=current.transaction_id,
            )
        except LabPromotionRollbackExecutorError as recovery_exc:
            raise LabPromotionRollbackExecutorError(
                "rollback execution failed and recovery terminalization "
                "also failed; preserve all evidence"
            ) from recovery_exc

        raise LabPromotionRollbackExecutorError(
            "rollback execution failed; durable state is "
            f"{recovery.state}; no retry or cleanup performed: {exc}"
        ) from exc


__all__ = [
    "LabPromotionRollbackExecutorError",
    "execute_lab_promotion_rollback",
]
