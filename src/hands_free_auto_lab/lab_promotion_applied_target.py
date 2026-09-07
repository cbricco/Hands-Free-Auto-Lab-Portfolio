"""READ-ONLY classification of partially applied promotion targets.

This module exists so recovery can determine physical repository state after a
crash without guessing from journal progress alone.

For each transaction path it classifies:

destination:
    BEFORE
    AFTER
    OTHER

temporary:
    ABSENT
    EXACT_TEMP
    UNEXPECTED_TEMP

The journal and filesystem are intentionally treated as separate evidence.
For example, APPLYING/PENDING plus destination AFTER and temp ABSENT is a valid
observable crash window: replacement may have completed before the journal
recorded INSTALLED.

This module never installs, removes, restores, replaces, creates, chmods, stages,
commits, or pushes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import stat

from .lab_promotion_prepared_target import (
    LabPromotionPreparedTargetError,
    LabPromotionPreparedTemp,
    TEMP_ABSENT,
    TEMP_EXACT,
    TEMP_UNEXPECTED,
    _expected_temp_paths,
    _git_snapshot,
    _inspect_temp,
    _open_parent_directory,
    _open_repository,
    _read_flags,
    _run_git,
    _validate_git_directory,
    _validate_temp_git_path,
)
from .lab_promotion_proposal import (
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
)
from .lab_promotion_recovery_materials import (
    LabPromotionRecoveryMaterialsError,
    load_promotion_recovery_materials,
)
from .lab_promotion_transaction_journal import (
    LabPromotionTransactionJournalError,
    load_promotion_transaction_journal,
)
from .lab_promotion_transaction_state import (
    LabPromotionTransaction,
    LabPromotionTransactionStateError,
    STATE_APPLYING,
    STATE_VERIFYING,
    STATE_ROLLING_BACK,
    validate_lab_promotion_transaction,
)


APPLIED_TARGET_COMPONENT = (
    "hands-free-auto-lab-promotion-applied-target-v1"
)
APPLIED_TARGET_SCHEMA_VERSION = 1

DESTINATION_BEFORE = "BEFORE"
DESTINATION_AFTER = "AFTER"
DESTINATION_OTHER = "OTHER"

DESTINATION_STATES = frozenset(
    {
        DESTINATION_BEFORE,
        DESTINATION_AFTER,
        DESTINATION_OTHER,
    }
)


class LabPromotionAppliedTargetError(
    RuntimeError
):
    """Applied-target evidence is unsafe, invalid, or inconsistent."""


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionAppliedDestination:
    path: str
    operation: str
    journal_progress: str
    status: str
    observed_bytes: int | None
    observed_sha256: str | None
    observed_mode: int | None


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionAppliedFile:
    path: str
    operation: str
    journal_progress: str
    destination: LabPromotionAppliedDestination
    temporary: LabPromotionPreparedTemp


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionAppliedTargetInspection:
    component: str
    schema_version: int
    transaction_id: str
    snapshot_id: str
    materials_id: str
    repository_path: str
    repository_device: int
    repository_inode: int
    branch: str
    head: str
    files: tuple[
        LabPromotionAppliedFile,
        ...,
    ]


@dataclass(
    frozen=True,
    slots=True,
)
class _ObservedPath:
    exists: bool
    safe_regular: bool
    observed_bytes: int | None
    observed_sha256: str | None
    observed_mode: int | None


def _same_stat_identity(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_uid",
        "st_nlink",
    )

    return all(
        getattr(
            first,
            field,
        )
        == getattr(
            second,
            field,
        )
        for field in fields
    )


def _observe_path(
    repository_fd: int,
    *,
    path: str,
    expected_sizes: frozenset[
        int,
    ],
) -> _ObservedPath:
    parent_fd, leaf = _open_parent_directory(
        repository_fd,
        path,
    )

    try:
        try:
            initial = os.stat(
                leaf,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return _ObservedPath(
                exists=False,
                safe_regular=False,
                observed_bytes=None,
                observed_sha256=None,
                observed_mode=None,
            )
        except OSError as exc:
            raise LabPromotionAppliedTargetError(
                f"cannot inspect destination {path!r}: {exc}"
            ) from exc

        mode = stat.S_IMODE(
            initial.st_mode
        )

        safe_regular = (
            stat.S_ISREG(
                initial.st_mode
            )
            and not stat.S_ISLNK(
                initial.st_mode
            )
            and initial.st_uid
            == os.geteuid()
            and initial.st_nlink
            == 1
        )

        if (
            not safe_regular
            or initial.st_size
            not in expected_sizes
        ):
            try:
                final = os.stat(
                    leaf,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise LabPromotionAppliedTargetError(
                    f"destination changed during inspection {path!r}: {exc}"
                ) from exc

            if not _same_stat_identity(
                initial,
                final,
            ):
                raise LabPromotionAppliedTargetError(
                    f"destination changed during inspection: {path!r}"
                )

            return _ObservedPath(
                exists=True,
                safe_regular=safe_regular,
                observed_bytes=initial.st_size,
                observed_sha256=None,
                observed_mode=mode,
            )

        try:
            fd = os.open(
                leaf,
                _read_flags(),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise LabPromotionAppliedTargetError(
                f"cannot securely open destination {path!r}: {exc}"
            ) from exc

        try:
            opened = os.fstat(
                fd
            )

            if not _same_stat_identity(
                initial,
                opened,
            ):
                raise LabPromotionAppliedTargetError(
                    f"destination changed during open: {path!r}"
                )

            digest = hashlib.sha256()
            total = 0

            while True:
                raw = os.read(
                    fd,
                    1024 * 1024,
                )

                if not raw:
                    break

                total += len(
                    raw
                )

                if total > initial.st_size:
                    raise LabPromotionAppliedTargetError(
                        f"destination grew while reading: {path!r}"
                    )

                digest.update(
                    raw
                )

            after_read = os.fstat(
                fd
            )

            if not _same_stat_identity(
                opened,
                after_read,
            ):
                raise LabPromotionAppliedTargetError(
                    f"destination changed while reading: {path!r}"
                )

            if total != initial.st_size:
                raise LabPromotionAppliedTargetError(
                    f"destination byte count changed while reading: {path!r}"
                )

            observed_sha256 = digest.hexdigest()

        finally:
            os.close(
                fd
            )

        final = os.stat(
            leaf,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )

        if not _same_stat_identity(
            initial,
            final,
        ):
            raise LabPromotionAppliedTargetError(
                f"destination changed after read: {path!r}"
            )

        return _ObservedPath(
            exists=True,
            safe_regular=True,
            observed_bytes=total,
            observed_sha256=observed_sha256,
            observed_mode=mode,
        )

    finally:
        os.close(
            parent_fd
        )


def _matches(
    observed: _ObservedPath,
    *,
    expected_bytes: int,
    expected_sha256: str,
    expected_mode: int,
) -> bool:
    return (
        observed.exists
        and observed.safe_regular
        and observed.observed_bytes
        == expected_bytes
        and observed.observed_sha256
        == expected_sha256
        and observed.observed_mode
        == expected_mode
    )


def _inspect_destination(
    repository_fd: int,
    *,
    transaction_file,
) -> LabPromotionAppliedDestination:
    if transaction_file.operation == PROMOTION_OPERATION_ADD:
        observed = _observe_path(
            repository_fd,
            path=transaction_file.path,
            expected_sizes=frozenset(
                {
                    transaction_file.after_bytes,
                }
            ),
        )

        if not observed.exists:
            status = DESTINATION_BEFORE

        elif _matches(
            observed,
            expected_bytes=transaction_file.after_bytes,
            expected_sha256=transaction_file.after_sha256,
            expected_mode=transaction_file.after_mode,
        ):
            status = DESTINATION_AFTER

        else:
            status = DESTINATION_OTHER

    elif transaction_file.operation == PROMOTION_OPERATION_MODIFY:
        if (
            transaction_file.before_bytes is None
            or transaction_file.before_sha256 is None
            or transaction_file.before_mode is None
        ):
            raise LabPromotionAppliedTargetError(
                f"MODIFY before-state missing for {transaction_file.path!r}"
            )

        before_identity = (
            transaction_file.before_bytes,
            transaction_file.before_sha256,
            transaction_file.before_mode,
        )

        after_identity = (
            transaction_file.after_bytes,
            transaction_file.after_sha256,
            transaction_file.after_mode,
        )

        if before_identity == after_identity:
            raise LabPromotionAppliedTargetError(
                f"MODIFY before/after states are physically indistinguishable "
                f"for {transaction_file.path!r}"
            )

        observed = _observe_path(
            repository_fd,
            path=transaction_file.path,
            expected_sizes=frozenset(
                {
                    transaction_file.before_bytes,
                    transaction_file.after_bytes,
                }
            ),
        )

        if _matches(
            observed,
            expected_bytes=transaction_file.before_bytes,
            expected_sha256=transaction_file.before_sha256,
            expected_mode=transaction_file.before_mode,
        ):
            status = DESTINATION_BEFORE

        elif _matches(
            observed,
            expected_bytes=transaction_file.after_bytes,
            expected_sha256=transaction_file.after_sha256,
            expected_mode=transaction_file.after_mode,
        ):
            status = DESTINATION_AFTER

        else:
            status = DESTINATION_OTHER

    else:
        raise LabPromotionAppliedTargetError(
            f"unsupported promotion operation "
            f"{transaction_file.operation!r}"
        )

    return LabPromotionAppliedDestination(
        path=transaction_file.path,
        operation=transaction_file.operation,
        journal_progress=transaction_file.progress,
        status=status,
        observed_bytes=observed.observed_bytes,
        observed_sha256=observed.observed_sha256,
        observed_mode=observed.observed_mode,
    )


def _validate_untracked_path(
    repository_path: str,
    path: str,
    *,
    name: str,
) -> None:
    tracked = _run_git(
        repository_path,
        "ls-files",
        "--",
        path,
    ).stdout

    if tracked:
        raise LabPromotionAppliedTargetError(
            f"{name} unexpectedly tracked: {path!r}"
        )

    ignored = _run_git(
        repository_path,
        "check-ignore",
        "--quiet",
        "--no-index",
        "--",
        path,
        allowed_returncodes=(
            0,
            1,
        ),
    )

    if ignored.returncode == 0:
        raise LabPromotionAppliedTargetError(
            f"{name} unexpectedly ignored: {path!r}"
        )


def _validate_modify_tracked(
    repository_path: str,
    path: str,
) -> None:
    result = _run_git(
        repository_path,
        "ls-files",
        "--error-unmatch",
        "--",
        path,
        allowed_returncodes=(
            0,
            1,
        ),
    )

    if result.returncode != 0:
        raise LabPromotionAppliedTargetError(
            f"MODIFY destination is no longer tracked: {path!r}"
        )


def _validate_git_status(
    raw: bytes,
    *,
    modify_paths: frozenset[
        bytes,
    ],
    add_paths: frozenset[
        bytes,
    ],
    temp_paths: frozenset[
        bytes,
    ],
) -> frozenset[
    bytes
]:
    observed: set[
        bytes
    ] = set()

    for record in raw.split(
        b"\0"
    ):
        if not record:
            continue

        if len(
            record
        ) < 4:
            raise LabPromotionAppliedTargetError(
                "malformed Git status record"
            )

        xy = record[
            :2
        ]

        if record[
            2:3
        ] != b" ":
            raise LabPromotionAppliedTargetError(
                "malformed Git status path separator"
            )

        path = record[
            3:
        ]

        if (
            not path
            or path in observed
        ):
            raise LabPromotionAppliedTargetError(
                "invalid or duplicate Git status path"
            )

        observed.add(
            path
        )

        if xy == b"??":
            if (
                path not in add_paths
                and path not in temp_paths
            ):
                raise LabPromotionAppliedTargetError(
                    "promotion repository contains unrelated untracked state"
                )

            continue

        index_status = xy[
            0:1
        ]

        worktree_status = xy[
            1:2
        ]

        if index_status != b" ":
            raise LabPromotionAppliedTargetError(
                "promotion repository contains staged/index changes"
            )

        if path not in modify_paths:
            raise LabPromotionAppliedTargetError(
                "promotion repository contains unrelated tracked changes"
            )

        if worktree_status not in {
            b"M",
            b"D",
            b"T",
        }:
            raise LabPromotionAppliedTargetError(
                "promotion repository contains unsupported tracked state"
            )

    return frozenset(
        observed
    )


def inspect_promotion_applied_target(
    *,
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
) -> LabPromotionAppliedTargetInspection:
    """Classify physical active promotion state without mutating the target."""
    try:
        transaction = validate_lab_promotion_transaction(
            transaction
        )
    except (
        TypeError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionAppliedTargetError(
            f"transaction validation failed: {exc}"
        ) from exc

    if transaction.state not in {
        STATE_APPLYING,
        STATE_VERIFYING,
        STATE_ROLLING_BACK,
    }:
        raise LabPromotionAppliedTargetError(
            "applied-target inspection requires APPLYING, "
            "VERIFYING, or ROLLING_BACK transaction"
        )

    try:
        durable = load_promotion_transaction_journal(
            journal_root,
            transaction_id=transaction.transaction_id,
        )
    except LabPromotionTransactionJournalError as exc:
        raise LabPromotionAppliedTargetError(
            f"durable transaction journal failed validation: {exc}"
        ) from exc

    if durable != transaction:
        raise LabPromotionAppliedTargetError(
            "durable journal does not match exact APPLYING snapshot"
        )

    try:
        materials = load_promotion_recovery_materials(
            recovery_root,
            transaction=transaction,
        )
    except LabPromotionRecoveryMaterialsError as exc:
        raise LabPromotionAppliedTargetError(
            f"durable recovery evidence failed validation: {exc}"
        ) from exc

    repository_fd: int | None = None

    try:
        repository_fd, initial_repository_state = _open_repository(
            transaction.repository_path
        )

        if (
            initial_repository_state.st_dev
            != transaction.repository_device
            or initial_repository_state.st_ino
            != transaction.repository_inode
        ):
            raise LabPromotionAppliedTargetError(
                "repository device/inode does not match transaction"
            )

        _validate_git_directory(
            repository_fd
        )

        expected_temp_paths = _expected_temp_paths(
            materials
        )

        modify_paths: set[
            str
        ] = set()

        add_paths: set[
            str
        ] = set()

        for item in transaction.files:
            if item.operation == PROMOTION_OPERATION_MODIFY:
                modify_paths.add(
                    item.path
                )

                _validate_modify_tracked(
                    transaction.repository_path,
                    item.path,
                )

            elif item.operation == PROMOTION_OPERATION_ADD:
                add_paths.add(
                    item.path
                )

                _validate_untracked_path(
                    transaction.repository_path,
                    item.path,
                    name="ADD destination",
                )

            else:
                raise LabPromotionAppliedTargetError(
                    f"unsupported operation {item.operation!r}"
                )

        for temp_path in expected_temp_paths:
            try:
                _validate_temp_git_path(
                    transaction.repository_path,
                    temp_path,
                )
            except LabPromotionPreparedTargetError as exc:
                raise LabPromotionAppliedTargetError(
                    f"temporary Git-path validation failed: {exc}"
                ) from exc

        try:
            branch, head, initial_status = _git_snapshot(
                transaction
            )
        except LabPromotionPreparedTargetError as exc:
            raise LabPromotionAppliedTargetError(
                f"Git snapshot failed: {exc}"
            ) from exc

        modify_bytes = frozenset(
            path.encode(
                "utf-8"
            )
            for path in modify_paths
        )

        add_bytes = frozenset(
            path.encode(
                "utf-8"
            )
            for path in add_paths
        )

        temp_bytes = frozenset(
            path.encode(
                "utf-8"
            )
            for path in expected_temp_paths
        )

        observed_status = _validate_git_status(
            initial_status,
            modify_paths=modify_bytes,
            add_paths=add_bytes,
            temp_paths=temp_bytes,
        )

        files: list[
            LabPromotionAppliedFile
        ] = []

        for (
            transaction_file,
            material_file,
            temp_path,
        ) in zip(
            transaction.files,
            materials.files,
            expected_temp_paths,
            strict=True,
        ):
            if transaction_file.path != material_file.path:
                raise LabPromotionAppliedTargetError(
                    "transaction/recovery path mismatch"
                )

            destination = _inspect_destination(
                repository_fd,
                transaction_file=transaction_file,
            )

            try:
                temporary = _inspect_temp(
                    repository_fd,
                    destination_path=transaction_file.path,
                    temp_path=temp_path,
                    expected_bytes=material_file.after_bytes,
                    expected_sha256=material_file.after_sha256,
                    expected_mode=material_file.after_mode,
                )
            except LabPromotionPreparedTargetError as exc:
                raise LabPromotionAppliedTargetError(
                    f"temporary inspection failed: {exc}"
                ) from exc

            temp_path_bytes = temp_path.encode(
                "utf-8"
            )

            destination_path_bytes = transaction_file.path.encode(
                "utf-8"
            )

            if (
                temporary.status == TEMP_ABSENT
                and temp_path_bytes in observed_status
            ):
                raise LabPromotionAppliedTargetError(
                    f"Git reports absent temporary as untracked: {temp_path!r}"
                )

            if (
                temporary.status != TEMP_ABSENT
                and temp_path_bytes not in observed_status
            ):
                raise LabPromotionAppliedTargetError(
                    f"existing temporary is not visible as exact untracked "
                    f"Git state: {temp_path!r}"
                )

            if transaction_file.operation == PROMOTION_OPERATION_ADD:
                if (
                    destination.status == DESTINATION_BEFORE
                    and destination_path_bytes in observed_status
                ):
                    raise LabPromotionAppliedTargetError(
                        f"Git reports absent ADD destination: "
                        f"{transaction_file.path!r}"
                    )

                if (
                    destination.status != DESTINATION_BEFORE
                    and destination_path_bytes not in observed_status
                ):
                    raise LabPromotionAppliedTargetError(
                        f"existing ADD destination is not visible as "
                        f"untracked Git state: {transaction_file.path!r}"
                    )

            files.append(
                LabPromotionAppliedFile(
                    path=transaction_file.path,
                    operation=transaction_file.operation,
                    journal_progress=transaction_file.progress,
                    destination=destination,
                    temporary=temporary,
                )
            )

        try:
            final_branch, final_head, final_status = _git_snapshot(
                transaction
            )
        except LabPromotionPreparedTargetError as exc:
            raise LabPromotionAppliedTargetError(
                f"final Git snapshot failed: {exc}"
            ) from exc

        if (
            final_branch != branch
            or final_head != head
            or final_status != initial_status
        ):
            raise LabPromotionAppliedTargetError(
                "repository Git state changed during inspection"
            )

        final_repository_state = os.fstat(
            repository_fd
        )

        if (
            final_repository_state.st_dev
            != initial_repository_state.st_dev
            or final_repository_state.st_ino
            != initial_repository_state.st_ino
            or final_repository_state.st_uid
            != initial_repository_state.st_uid
        ):
            raise LabPromotionAppliedTargetError(
                "repository identity changed during inspection"
            )

        final_path_state = os.stat(
            transaction.repository_path,
            follow_symlinks=False,
        )

        if (
            final_path_state.st_dev
            != final_repository_state.st_dev
            or final_path_state.st_ino
            != final_repository_state.st_ino
        ):
            raise LabPromotionAppliedTargetError(
                "repository path no longer names inspected repository"
            )

        return LabPromotionAppliedTargetInspection(
            component=APPLIED_TARGET_COMPONENT,
            schema_version=APPLIED_TARGET_SCHEMA_VERSION,
            transaction_id=transaction.transaction_id,
            snapshot_id=transaction.snapshot_id,
            materials_id=materials.materials_id,
            repository_path=transaction.repository_path,
            repository_device=final_repository_state.st_dev,
            repository_inode=final_repository_state.st_ino,
            branch=branch,
            head=head,
            files=tuple(
                files
            ),
        )

    finally:
        if repository_fd is not None:
            os.close(
                repository_fd
            )
