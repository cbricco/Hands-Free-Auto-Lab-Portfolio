from __future__ import annotations

import hashlib
import os
import stat
from pathlib import PurePosixPath

from .lab_promotion_applied_target import (
    DESTINATION_AFTER,
    DESTINATION_BEFORE,
    inspect_promotion_applied_target,
)
from .lab_promotion_approval_store import query_promotion_approval
from .lab_promotion_prepared_target import (
    TEMP_ABSENT,
    TEMP_EXACT,
    inspect_promotion_prepared_target,
)
from .lab_promotion_proposal import PROMOTION_OPERATION_MODIFY
from .lab_promotion_recovery_materials import load_promotion_recovery_materials
from .lab_promotion_target_preparer import LabPromotionTargetPreparationResult
from .lab_promotion_transaction_journal import (
    append_promotion_transaction_snapshot,
    load_promotion_transaction_journal,
)
from .lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_PENDING,
    PROGRESS_VERIFIED,
    STATE_APPLYING,
    STATE_COMPLETED,
    STATE_PREPARED,
    STATE_RECOVERY_REQUIRED,
    STATE_VERIFYING,
    LabPromotionTransaction,
    transition_lab_promotion_transaction,
    validate_lab_promotion_transaction,
)


class LabPromotionExecutorError(RuntimeError):
    """Raised when bounded promotion application cannot complete safely."""


_ACTIVE_STATES = frozenset({STATE_APPLYING, STATE_VERIFYING})


def _progress(transaction: LabPromotionTransaction) -> dict[str, str]:
    return {item.path: item.progress for item in transaction.files}


def _expected_temp_path(destination_path: str, temp_name: str) -> str:
    destination = PurePosixPath(destination_path)
    if (
        destination.is_absolute()
        or not destination.parts
        or any(part in {"", ".", ".."} for part in destination.parts)
        or "/" in temp_name
        or temp_name in {"", ".", ".."}
    ):
        raise LabPromotionExecutorError(
            "invalid promotion destination or temporary path"
        )
    parent = destination.parent
    return temp_name if str(parent) == "." else f"{parent.as_posix()}/{temp_name}"


def _require_prepared_modify_only(transaction: LabPromotionTransaction) -> None:
    if transaction.state != STATE_PREPARED:
        raise LabPromotionExecutorError(
            "promotion executor requires PREPARED transaction"
        )
    if any(item.progress != PROGRESS_PENDING for item in transaction.files):
        raise LabPromotionExecutorError(
            "promotion executor requires every file PENDING"
        )
    unsupported = [
        item.path
        for item in transaction.files
        if item.operation != PROMOTION_OPERATION_MODIFY
    ]
    if unsupported:
        raise LabPromotionExecutorError(
            "initial promotion executor supports MODIFY only; "
            f"unsupported paths={unsupported!r}"
        )


def _require_exact_preparation(
    *,
    transaction: LabPromotionTransaction,
    preparation: LabPromotionTargetPreparationResult,
    materials,
    inspection,
) -> None:
    if type(preparation) is not LabPromotionTargetPreparationResult:
        raise TypeError(
            "preparation must be LabPromotionTargetPreparationResult"
        )

    pairs = (
        ("transaction_id", preparation.transaction_id, transaction.transaction_id),
        ("proposal_id", preparation.proposal_id, transaction.proposal_id),
        ("run_id", preparation.run_id, transaction.run_id),
        ("materials_id", preparation.materials_id, materials.materials_id),
        ("repository_path", preparation.repository_path, transaction.repository_path),
        (
            "inspection transaction_id",
            inspection.transaction_id,
            transaction.transaction_id,
        ),
        ("inspection materials_id", inspection.materials_id, materials.materials_id),
        (
            "inspection repository_path",
            inspection.repository_path,
            transaction.repository_path,
        ),
        (
            "inspection repository_device",
            inspection.repository_device,
            transaction.repository_device,
        ),
        (
            "inspection repository_inode",
            inspection.repository_inode,
            transaction.repository_inode,
        ),
        ("inspection branch", inspection.branch, transaction.branch),
        ("inspection head", inspection.head, transaction.head),
    )
    for name, observed, expected in pairs:
        if observed != expected:
            raise LabPromotionExecutorError(f"prepared promotion {name} mismatch")

    if tuple(preparation.temps) != tuple(inspection.temps):
        raise LabPromotionExecutorError(
            "preparation temporary evidence does not match fresh inspection"
        )
    if len(materials.files) != len(transaction.files):
        raise LabPromotionExecutorError(
            "transaction/recovery file count mismatch"
        )
    if len(inspection.temps) != len(transaction.files):
        raise LabPromotionExecutorError(
            "transaction/prepared-temp count mismatch"
        )

    for transaction_file, material_file, temp in zip(
        transaction.files,
        materials.files,
        inspection.temps,
        strict=True,
    ):
        if material_file.path != transaction_file.path:
            raise LabPromotionExecutorError(
                "transaction/recovery path mismatch"
            )
        expected_temp_path = _expected_temp_path(
            transaction_file.path,
            material_file.after_temp_name,
        )
        if (
            temp.path != transaction_file.path
            or temp.temp_path != expected_temp_path
            or temp.status != TEMP_EXACT
        ):
            raise LabPromotionExecutorError(
                f"prepared temporary evidence mismatch for "
                f"{transaction_file.path!r}"
            )


def _require_consumed_approval(
    *,
    approval_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
    preparation: LabPromotionTargetPreparationResult,
) -> None:
    if not isinstance(preparation.approval_id, str) or not preparation.approval_id:
        raise LabPromotionExecutorError("preparation approval_id is invalid")

    queried = query_promotion_approval(
        approval_root,
        approval_id=preparation.approval_id,
        proposal_id=transaction.proposal_id,
    )
    if queried.get("status") != "consumed":
        raise LabPromotionExecutorError(
            "promotion approval is not durably consumed"
        )

    approval = queried.get("approval")
    terminal = queried.get("terminal")
    if not isinstance(approval, dict) or not isinstance(terminal, dict):
        raise LabPromotionExecutorError(
            "promotion approval query returned malformed evidence"
        )

    checks = (
        (approval.get("approval_id"), preparation.approval_id, "approval_id"),
        (
            approval.get("proposal_id"),
            transaction.proposal_id,
            "approval proposal_id",
        ),
        (
            terminal.get("approval_id"),
            preparation.approval_id,
            "terminal approval_id",
        ),
        (
            terminal.get("proposal_id"),
            transaction.proposal_id,
            "terminal proposal_id",
        ),
        (terminal.get("terminal_state"), "consumed", "terminal state"),
        (terminal.get("run_id"), transaction.run_id, "terminal run_id"),
        (
            terminal.get("terminal_at"),
            preparation.terminal_at,
            "terminal timestamp",
        ),
    )
    for observed, expected, name in checks:
        if observed != expected:
            raise LabPromotionExecutorError(
                f"consumed approval {name} mismatch"
            )


def _append_transition(
    *,
    journal_root: str | os.PathLike[str],
    current: LabPromotionTransaction,
    state: str,
    progress_by_path: dict[str, str],
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


def _require_safe_directory(
    state: os.stat_result,
    *,
    label: str,
) -> None:
    if state.st_uid != os.geteuid():
        raise LabPromotionExecutorError(
            f"{label} is not owned by current euid"
        )

    mode = stat.S_IMODE(state.st_mode)

    if mode & 0o022:
        raise LabPromotionExecutorError(
            f"{label} must not be group- or other-writable"
        )


def _open_repository(transaction: LabPromotionTransaction) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        repository_fd = os.open(transaction.repository_path, flags)
    except OSError as exc:
        raise LabPromotionExecutorError(
            f"cannot open promotion repository safely: {exc}"
        ) from exc

    try:
        state = os.fstat(repository_fd)
        if (
            state.st_dev != transaction.repository_device
            or state.st_ino != transaction.repository_inode
        ):
            raise LabPromotionExecutorError(
                "repository device/inode changed before install"
            )

        _require_safe_directory(
            state,
            label="promotion repository",
        )

        return repository_fd
    except Exception:
        os.close(repository_fd)
        raise


def _open_parent(repository_fd: int, path: str) -> tuple[int, str]:
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise LabPromotionExecutorError(f"invalid promotion path {path!r}")

    current_fd = os.dup(repository_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        for component in parsed.parts[:-1]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            state = os.fstat(next_fd)

            try:
                _require_safe_directory(
                    state,
                    label=f"promotion parent for {path!r}",
                )
            except Exception:
                os.close(next_fd)
                raise

            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parsed.parts[-1]
    except Exception:
        os.close(current_fd)
        raise


def _require_safe_parent_directories(
    transaction: LabPromotionTransaction,
) -> None:
    repository_fd = _open_repository(transaction)

    try:
        for item in transaction.files:
            parent_fd = None

            try:
                parent_fd, _name = _open_parent(
                    repository_fd,
                    item.path,
                )
            finally:
                if parent_fd is not None:
                    os.close(parent_fd)
    finally:
        os.close(repository_fd)


def _require_safe_git_metadata(
    transaction: LabPromotionTransaction,
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    repository_fd = _open_repository(transaction)
    git_fd = None

    def walk_directory(
        directory_fd: int,
        *,
        label: str,
    ) -> None:
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise LabPromotionExecutorError(
                f"cannot list protected Git metadata {label!r}: {exc}"
            ) from exc

        for name in names:
            if (
                not isinstance(name, str)
                or not name
                or name in {".", ".."}
                or "/" in name
            ):
                raise LabPromotionExecutorError(
                    f"invalid Git metadata entry beneath {label!r}"
                )

            entry_label = f"{label}/{name}"

            try:
                state = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise LabPromotionExecutorError(
                    f"cannot inspect protected Git metadata "
                    f"{entry_label!r}: {exc}"
                ) from exc

            if stat.S_ISLNK(state.st_mode):
                raise LabPromotionExecutorError(
                    f"Git metadata must not contain symlinks: "
                    f"{entry_label!r}"
                )

            if state.st_uid != os.geteuid():
                raise LabPromotionExecutorError(
                    f"Git metadata is not owned by current euid: "
                    f"{entry_label!r}"
                )

            if stat.S_IMODE(state.st_mode) & 0o022:
                raise LabPromotionExecutorError(
                    f"Git metadata must not be group- or other-writable: "
                    f"{entry_label!r}"
                )

            if stat.S_ISDIR(state.st_mode):
                try:
                    child_fd = os.open(
                        name,
                        flags,
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    raise LabPromotionExecutorError(
                        f"cannot securely open Git metadata directory "
                        f"{entry_label!r}: {exc}"
                    ) from exc

                try:
                    opened = os.fstat(child_fd)

                    if (
                        opened.st_dev != state.st_dev
                        or opened.st_ino != state.st_ino
                    ):
                        raise LabPromotionExecutorError(
                            f"Git metadata directory identity changed: "
                            f"{entry_label!r}"
                        )

                    _require_safe_directory(
                        opened,
                        label=f"Git metadata directory {entry_label!r}",
                    )

                    walk_directory(
                        child_fd,
                        label=entry_label,
                    )
                finally:
                    os.close(child_fd)

            elif stat.S_ISREG(state.st_mode):
                if entry_label == ".git/objects/info/alternates":
                    raise LabPromotionExecutorError(
                        "Git object alternates are not supported by the "
                        "initial promotion executor"
                    )

            else:
                raise LabPromotionExecutorError(
                    f"unsupported Git metadata file type: "
                    f"{entry_label!r}"
                )

    try:
        try:
            git_fd = os.open(
                ".git",
                flags,
                dir_fd=repository_fd,
            )
        except OSError as exc:
            raise LabPromotionExecutorError(
                f"cannot securely open promotion .git directory: {exc}"
            ) from exc

        git_state = os.fstat(git_fd)

        _require_safe_directory(
            git_state,
            label="promotion .git directory",
        )

        walk_directory(
            git_fd,
            label=".git",
        )

    finally:
        if git_fd is not None:
            os.close(git_fd)

        os.close(repository_fd)


def _read_exact_regular_file(
    parent_fd: int,
    name: str,
    *,
    expected_bytes: int,
    expected_sha256: str,
    expected_mode: int,
    label: str,
) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise LabPromotionExecutorError(
            f"cannot open exact {label}: {exc}"
        ) from exc

    try:
        state = os.fstat(fd)
        if not stat.S_ISREG(state.st_mode):
            raise LabPromotionExecutorError(f"{label} is not a regular file")
        if state.st_uid != os.geteuid():
            raise LabPromotionExecutorError(
                f"{label} is not owned by current euid"
            )
        if state.st_nlink != 1:
            raise LabPromotionExecutorError(
                f"{label} has unexpected hard links"
            )
        if stat.S_IMODE(state.st_mode) != expected_mode:
            raise LabPromotionExecutorError(f"{label} mode mismatch")
        if state.st_size != expected_bytes:
            raise LabPromotionExecutorError(f"{label} byte-count mismatch")

        digest = hashlib.sha256()
        remaining = expected_bytes
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise LabPromotionExecutorError(
                    f"{label} ended before expected byte count"
                )
            digest.update(chunk)
            remaining -= len(chunk)

        if os.read(fd, 1):
            raise LabPromotionExecutorError(
                f"{label} exceeds expected byte count"
            )

        if digest.hexdigest() != expected_sha256:
            raise LabPromotionExecutorError(f"{label} SHA-256 mismatch")

        path_state = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            path_state.st_dev != state.st_dev
            or path_state.st_ino != state.st_ino
        ):
            raise LabPromotionExecutorError(
                f"{label} path identity changed during validation"
            )
    finally:
        os.close(fd)


def _install_modify(
    *,
    transaction: LabPromotionTransaction,
    transaction_file,
    material_file,
) -> None:
    if (
        transaction_file.before_bytes is None
        or transaction_file.before_sha256 is None
        or transaction_file.before_mode is None
    ):
        raise LabPromotionExecutorError(
            f"MODIFY before-state missing for {transaction_file.path!r}"
        )

    repository_fd = _open_repository(transaction)
    parent_fd: int | None = None

    try:
        parent_fd, destination_name = _open_parent(
            repository_fd,
            transaction_file.path,
        )

        _read_exact_regular_file(
            parent_fd,
            destination_name,
            expected_bytes=transaction_file.before_bytes,
            expected_sha256=transaction_file.before_sha256,
            expected_mode=transaction_file.before_mode,
            label=f"MODIFY destination {transaction_file.path!r}",
        )

        _read_exact_regular_file(
            parent_fd,
            material_file.after_temp_name,
            expected_bytes=transaction_file.after_bytes,
            expected_sha256=transaction_file.after_sha256,
            expected_mode=transaction_file.after_mode,
            label=f"promotion temporary for {transaction_file.path!r}",
        )

        try:
            os.replace(
                material_file.after_temp_name,
                destination_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except OSError as exc:
            raise LabPromotionExecutorError(
                f"cannot durably install {transaction_file.path!r}: {exc}"
            ) from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(repository_fd)


def _require_applied_shape(
    *,
    transaction: LabPromotionTransaction,
    inspection,
    current_path: str | None = None,
    current_pending_after: bool = False,
    require_all_after: bool = False,
) -> None:
    if len(inspection.files) != len(transaction.files):
        raise LabPromotionExecutorError(
            "transaction/applied inspection file count mismatch"
        )

    for transaction_file, observed in zip(
        transaction.files,
        inspection.files,
        strict=True,
    ):
        if (
            observed.path != transaction_file.path
            or observed.operation != transaction_file.operation
            or observed.journal_progress != transaction_file.progress
        ):
            raise LabPromotionExecutorError(
                "applied inspection identity/progress mismatch"
            )

        if current_pending_after and observed.path == current_path:
            if (
                observed.destination.status != DESTINATION_AFTER
                or observed.temporary.status != TEMP_ABSENT
                or transaction_file.progress != PROGRESS_PENDING
            ):
                raise LabPromotionExecutorError(
                    f"installed physical state is not exact for "
                    f"{transaction_file.path!r}"
                )
            continue

        if require_all_after:
            if (
                observed.destination.status != DESTINATION_AFTER
                or observed.temporary.status != TEMP_ABSENT
            ):
                raise LabPromotionExecutorError(
                    f"expected installed after-state for "
                    f"{transaction_file.path!r}"
                )
            continue

        if transaction_file.progress == PROGRESS_PENDING:
            expected = (DESTINATION_BEFORE, TEMP_EXACT)
        elif transaction_file.progress in {
            PROGRESS_INSTALLED,
            PROGRESS_VERIFIED,
        }:
            expected = (DESTINATION_AFTER, TEMP_ABSENT)
        else:
            raise LabPromotionExecutorError(
                f"unsupported active progress for {transaction_file.path!r}"
            )

        observed_pair = (
            observed.destination.status,
            observed.temporary.status,
        )
        if observed_pair != expected:
            raise LabPromotionExecutorError(
                f"physical state mismatch for {transaction_file.path!r}: "
                f"expected={expected!r}, observed={observed_pair!r}"
            )


def _fail_closed_state(
    *,
    journal_root: str | os.PathLike[str],
    initial: LabPromotionTransaction,
) -> LabPromotionTransaction:
    durable = load_promotion_transaction_journal(
        journal_root,
        transaction_id=initial.transaction_id,
    )

    if durable.state == STATE_PREPARED:
        if durable != initial:
            raise LabPromotionExecutorError(
                "promotion failure left unexpected PREPARED journal evidence"
            )
        return durable

    if durable.state == STATE_RECOVERY_REQUIRED:
        return durable

    if durable.state == STATE_COMPLETED:
        return durable

    if durable.state not in _ACTIVE_STATES:
        raise LabPromotionExecutorError(
            f"promotion failure left unsupported durable state "
            f"{durable.state!r}"
        )

    recovery = transition_lab_promotion_transaction(
        durable,
        state=STATE_RECOVERY_REQUIRED,
        progress_by_path=_progress(durable),
    )

    return append_promotion_transaction_snapshot(
        journal_root,
        successor=recovery,
        expected_previous_snapshot_id=durable.snapshot_id,
    )


def execute_lab_promotion_transaction(
    *,
    approval_root: str | os.PathLike[str],
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
    preparation: LabPromotionTargetPreparationResult,
) -> LabPromotionTransaction:
    """
    Apply and verify one already-prepared, already-approved MODIFY promotion.

    This boundary never creates or consumes approval authority. It requires exact
    consumed-approval preparation evidence, journals APPLYING before mutation,
    records each installed and verified file durably, and fails closed after an
    ambiguous active failure. It never stages, commits, pushes, automatically
    rolls back, deletes evidence, or retries.
    """
    try:
        transaction = validate_lab_promotion_transaction(transaction)
    except Exception as exc:
        raise LabPromotionExecutorError(
            f"transaction validation failed: {exc}"
        ) from exc

    _require_prepared_modify_only(transaction)

    durable = load_promotion_transaction_journal(
        journal_root,
        transaction_id=transaction.transaction_id,
    )

    if durable != transaction:
        raise LabPromotionExecutorError(
            "durable transaction journal does not match exact input snapshot"
        )

    materials = load_promotion_recovery_materials(
        recovery_root,
        transaction=transaction,
    )

    prepared_inspection = inspect_promotion_prepared_target(
        recovery_root=recovery_root,
        transaction=transaction,
    )

    _require_exact_preparation(
        transaction=transaction,
        preparation=preparation,
        materials=materials,
        inspection=prepared_inspection,
    )

    _require_consumed_approval(
        approval_root=approval_root,
        transaction=transaction,
        preparation=preparation,
    )

    _require_safe_parent_directories(
        transaction
    )

    _require_safe_git_metadata(
        transaction
    )

    current = transaction

    try:
        current = _append_transition(
            journal_root=journal_root,
            current=current,
            state=STATE_APPLYING,
            progress_by_path=_progress(current),
        )

        for transaction_file, material_file in zip(
            current.files,
            materials.files,
            strict=True,
        ):
            before_install = inspect_promotion_applied_target(
                journal_root=journal_root,
                recovery_root=recovery_root,
                transaction=current,
            )

            _require_applied_shape(
                transaction=current,
                inspection=before_install,
            )

            _install_modify(
                transaction=current,
                transaction_file=transaction_file,
                material_file=material_file,
            )

            after_install = inspect_promotion_applied_target(
                journal_root=journal_root,
                recovery_root=recovery_root,
                transaction=current,
            )

            _require_applied_shape(
                transaction=current,
                inspection=after_install,
                current_path=transaction_file.path,
                current_pending_after=True,
            )

            installed_progress = _progress(current)
            installed_progress[
                transaction_file.path
            ] = PROGRESS_INSTALLED

            current = _append_transition(
                journal_root=journal_root,
                current=current,
                state=STATE_APPLYING,
                progress_by_path=installed_progress,
            )

        current = _append_transition(
            journal_root=journal_root,
            current=current,
            state=STATE_VERIFYING,
            progress_by_path=_progress(current),
        )

        for transaction_file in current.files:
            inspection = inspect_promotion_applied_target(
                journal_root=journal_root,
                recovery_root=recovery_root,
                transaction=current,
            )

            _require_applied_shape(
                transaction=current,
                inspection=inspection,
                require_all_after=True,
            )

            if transaction_file.progress == PROGRESS_VERIFIED:
                continue

            if transaction_file.progress != PROGRESS_INSTALLED:
                raise LabPromotionExecutorError(
                    f"verification requires INSTALLED progress for "
                    f"{transaction_file.path!r}"
                )

            verified_progress = _progress(current)
            verified_progress[
                transaction_file.path
            ] = PROGRESS_VERIFIED

            current = _append_transition(
                journal_root=journal_root,
                current=current,
                state=STATE_VERIFYING,
                progress_by_path=verified_progress,
            )

        final_inspection = inspect_promotion_applied_target(
            journal_root=journal_root,
            recovery_root=recovery_root,
            transaction=current,
        )

        _require_applied_shape(
            transaction=current,
            inspection=final_inspection,
            require_all_after=True,
        )

        if any(
            item.progress != PROGRESS_VERIFIED
            for item in current.files
        ):
            raise LabPromotionExecutorError(
                "promotion cannot complete before every file is VERIFIED"
            )

        return _append_transition(
            journal_root=journal_root,
            current=current,
            state=STATE_COMPLETED,
            progress_by_path=_progress(current),
        )

    except Exception as exc:
        try:
            failure_state = _fail_closed_state(
                journal_root=journal_root,
                initial=transaction,
            )
        except Exception as marker_exc:
            raise LabPromotionExecutorError(
                "promotion failed and durable fail-closed state could not be "
                "established; preserve target, temporary, journal, and recovery "
                "evidence; do not retry, clean up, or roll back automatically: "
                f"original={exc}; marker={marker_exc}"
            ) from marker_exc

        if failure_state.state == STATE_PREPARED:
            raise LabPromotionExecutorError(
                "promotion failed before durable APPLYING; no executor mutation "
                "is authorized for retry without fresh inspection; "
                f"preserve preparation evidence: {exc}"
            ) from exc

        if failure_state.state == STATE_COMPLETED:
            raise LabPromotionExecutorError(
                "promotion error was observed after durable COMPLETED; inspect "
                "the completed evidence manually and do not retry automatically: "
                f"{exc}"
            ) from exc

        raise LabPromotionExecutorError(
            "promotion failed after durable APPLYING; transaction is "
            f"{failure_state.state}; preserve evidence and do not retry, "
            f"clean up, or roll back automatically: {exc}"
        ) from exc
