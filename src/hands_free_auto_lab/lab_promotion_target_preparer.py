"""Human-gated preparation of promotion after-image temporary files.

This is the first promotion runtime component intentionally permitted to write
inside a target Git repository.

Its authority is deliberately limited to creating deterministic same-parent
temporary files that are already bound by an immutable promotion transaction
and durable recovery evidence.

It does NOT:

- install or replace destination files,
- remove or clean temporary files,
- create destination directories,
- transition the promotion transaction,
- append transaction-journal snapshots,
- stage, commit, or push Git state.

Any failure after approval consumption leaves that approval permanently
consumed. Any temporary files already created remain in place for later
read-only classification and explicit human-directed recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import PurePosixPath
import stat

from .lab_promotion_approval_store import (
    LabPromotionApprovalStoreError,
    consume_promotion_approval,
)
from .lab_promotion_prepared_target import (
    LabPromotionPreparedTargetError,
    LabPromotionPreparedTemp,
    TEMP_ABSENT,
    TEMP_EXACT,
    inspect_promotion_prepared_target,
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
    PROGRESS_PENDING,
    STATE_PREPARED,
    validate_lab_promotion_transaction,
)


TARGET_PREPARER_COMPONENT = (
    "hands-free-auto-lab-promotion-target-preparer-v1"
)
TARGET_PREPARER_SCHEMA_VERSION = 1

INITIAL_CREATE_MODE = 0o600


class LabPromotionTargetPreparationError(
    RuntimeError
):
    """Target preparation was refused or could not be proven safe."""


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionTargetPreparationResult:
    component: str
    schema_version: int
    transaction_id: str
    proposal_id: str
    run_id: str
    approval_id: str
    materials_id: str
    repository_path: str
    terminal_at: str
    temps: tuple[
        LabPromotionPreparedTemp,
        ...,
    ]


def _require_secure_platform() -> None:
    missing: list[str] = []

    for name in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
        "O_CREAT",
        "O_EXCL",
    ):
        if not hasattr(
            os,
            name,
        ):
            missing.append(
                f"os.{name}"
            )

    for function_name in (
        "fchmod",
        "fsync",
        "geteuid",
    ):
        if not hasattr(
            os,
            function_name,
        ):
            missing.append(
                f"os.{function_name}"
            )

    if os.open not in os.supports_dir_fd:
        missing.append(
            "os.open(dir_fd=...)"
        )

    if missing:
        raise LabPromotionTargetPreparationError(
            "secure target-preparation primitives unavailable: "
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


def _require_repository_path(
    value: object,
) -> str:
    if (
        not isinstance(
            value,
            str,
        )
        or not value
        or not os.path.isabs(
            value
        )
    ):
        raise LabPromotionTargetPreparationError(
            "repository_path must be non-empty absolute text"
        )

    if os.path.normpath(
        value
    ) != value:
        raise LabPromotionTargetPreparationError(
            "repository_path must be normalized"
        )

    if os.path.realpath(
        value
    ) != value:
        raise LabPromotionTargetPreparationError(
            "repository_path must not contain symlink components"
        )

    return value


def _require_relative_path(
    value: object,
) -> str:
    if (
        not isinstance(
            value,
            str,
        )
        or not value
        or "\\" in value
    ):
        raise LabPromotionTargetPreparationError(
            "promotion path must be canonical relative POSIX text"
        )

    path = PurePosixPath(
        value
    )

    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(
            part in {
                "",
                ".",
                "..",
            }
            for part in path.parts
        )
    ):
        raise LabPromotionTargetPreparationError(
            f"unsafe promotion path {value!r}"
        )

    return value


def _require_temp_basename(
    value: object,
) -> str:
    if (
        not isinstance(
            value,
            str,
        )
        or not value
        or "/" in value
        or "\\" in value
        or value in {
            ".",
            "..",
        }
    ):
        raise LabPromotionTargetPreparationError(
            "unsafe deterministic temporary basename"
        )

    return value


def _open_repository(
    repository_path: str,
    *,
    expected_device: int,
    expected_inode: int,
) -> int:
    repository_path = _require_repository_path(
        repository_path
    )

    current_fd = os.open(
        "/",
        _directory_flags(),
    )

    try:
        for component in PurePosixPath(
            repository_path
        ).parts[1:]:
            next_fd = os.open(
                component,
                _directory_flags(),
                dir_fd=current_fd,
            )

            os.close(
                current_fd
            )

            current_fd = next_fd

        state = os.fstat(
            current_fd
        )

        if not stat.S_ISDIR(
            state.st_mode
        ):
            raise LabPromotionTargetPreparationError(
                "target repository is not a directory"
            )

        if state.st_uid != os.geteuid():
            raise LabPromotionTargetPreparationError(
                "target repository is not owned by current euid"
            )

        if (
            state.st_dev != expected_device
            or state.st_ino != expected_inode
        ):
            raise LabPromotionTargetPreparationError(
                "target repository device/inode changed"
            )

        return current_fd

    except Exception:
        os.close(
            current_fd
        )
        raise


def _open_parent_directory(
    repository_fd: int,
    *,
    destination_path: str,
) -> int:
    destination_path = _require_relative_path(
        destination_path
    )

    parts = PurePosixPath(
        destination_path
    ).parts

    parent_fd = os.dup(
        repository_fd
    )

    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                _directory_flags(),
                dir_fd=parent_fd,
            )

            state = os.fstat(
                next_fd
            )

            if (
                not stat.S_ISDIR(
                    state.st_mode
                )
                or state.st_uid
                != os.geteuid()
            ):
                os.close(
                    next_fd
                )

                raise LabPromotionTargetPreparationError(
                    f"unsafe destination parent for {destination_path!r}"
                )

            os.close(
                parent_fd
            )

            parent_fd = next_fd

        return parent_fd

    except Exception:
        os.close(
            parent_fd
        )
        raise


def _write_all(
    fd: int,
    data: bytes,
) -> None:
    view = memoryview(
        data
    )

    offset = 0

    while offset < len(
        view
    ):
        try:
            written = os.write(
                fd,
                view[
                    offset:
                ],
            )
        except OSError as exc:
            raise LabPromotionTargetPreparationError(
                f"target temporary write failed: {exc}"
            ) from exc

        if written <= 0:
            raise LabPromotionTargetPreparationError(
                "target temporary write made no progress"
            )

        offset += written


def _prepare_one_temp(
    repository_fd: int,
    *,
    destination_path: str,
    temp_name: str,
    data: bytes,
    expected_mode: int,
) -> None:
    temp_name = _require_temp_basename(
        temp_name
    )

    parent_fd = _open_parent_directory(
        repository_fd,
        destination_path=destination_path,
    )

    temp_fd: int | None = None

    try:
        try:
            temp_fd = os.open(
                temp_name,
                _create_flags(),
                INITIAL_CREATE_MODE,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise LabPromotionTargetPreparationError(
                "deterministic target temporary already exists; "
                "refusing overwrite or reuse"
            ) from exc
        except OSError as exc:
            raise LabPromotionTargetPreparationError(
                f"cannot create deterministic target temporary: {exc}"
            ) from exc

        try:
            os.fchmod(
                temp_fd,
                expected_mode,
            )
        except OSError as exc:
            raise LabPromotionTargetPreparationError(
                f"cannot set exact target temporary mode: {exc}"
            ) from exc

        opened = os.fstat(
            temp_fd
        )

        if (
            not stat.S_ISREG(
                opened.st_mode
            )
            or opened.st_uid
            != os.geteuid()
            or opened.st_nlink
            != 1
            or stat.S_IMODE(
                opened.st_mode
            )
            != expected_mode
        ):
            raise LabPromotionTargetPreparationError(
                "new target temporary failed exact file-identity checks"
            )

        _write_all(
            temp_fd,
            data,
        )

        try:
            os.fsync(
                temp_fd
            )
        except OSError as exc:
            raise LabPromotionTargetPreparationError(
                f"cannot fsync target temporary: {exc}"
            ) from exc

        final_file = os.fstat(
            temp_fd
        )

        if (
            final_file.st_dev
            != opened.st_dev
            or final_file.st_ino
            != opened.st_ino
            or not stat.S_ISREG(
                final_file.st_mode
            )
            or final_file.st_uid
            != os.geteuid()
            or final_file.st_nlink
            != 1
            or stat.S_IMODE(
                final_file.st_mode
            )
            != expected_mode
            or final_file.st_size
            != len(
                data
            )
        ):
            raise LabPromotionTargetPreparationError(
                "target temporary changed or mismatched while writing"
            )

        os.close(
            temp_fd
        )

        temp_fd = None

        try:
            os.fsync(
                parent_fd
            )
        except OSError as exc:
            raise LabPromotionTargetPreparationError(
                f"cannot fsync target temporary parent directory: {exc}"
            ) from exc

    finally:
        if temp_fd is not None:
            os.close(
                temp_fd
            )

        os.close(
            parent_fd
        )


def _require_exact_journal(
    journal_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
) -> None:
    try:
        current = load_promotion_transaction_journal(
            journal_root,
            transaction_id=transaction.transaction_id,
        )
    except LabPromotionTransactionJournalError as exc:
        raise LabPromotionTargetPreparationError(
            f"durable transaction journal validation failed: {exc}"
        ) from exc

    if current != transaction:
        raise LabPromotionTargetPreparationError(
            "durable transaction journal does not match exact transaction"
        )

    if (
        current.state != STATE_PREPARED
        or any(
            item.progress
            != PROGRESS_PENDING
            for item in current.files
        )
    ):
        raise LabPromotionTargetPreparationError(
            "target preparation requires exact PREPARED/PENDING journal state"
        )


def _require_all_absent(
    inspection,
) -> None:
    if any(
        item.status != TEMP_ABSENT
        for item in inspection.temps
    ):
        raise LabPromotionTargetPreparationError(
            "target preparation requires all deterministic temporaries ABSENT"
        )


def _require_all_exact(
    inspection,
) -> None:
    if any(
        item.status != TEMP_EXACT
        for item in inspection.temps
    ):
        raise LabPromotionTargetPreparationError(
            "target preparation final verification requires every "
            "temporary EXACT_TEMP"
        )


def _validate_consumption(
    consumption: object,
    *,
    approval_id: str,
    transaction: LabPromotionTransaction,
) -> str:
    if not isinstance(
        consumption,
        dict,
    ):
        raise LabPromotionTargetPreparationError(
            "approval consumption returned invalid result type"
        )

    if consumption.get(
        "status"
    ) != "consumed":
        raise LabPromotionTargetPreparationError(
            "approval consumption did not return consumed status"
        )

    approval = consumption.get(
        "approval"
    )

    terminal = consumption.get(
        "terminal"
    )

    if (
        not isinstance(
            approval,
            dict,
        )
        or not isinstance(
            terminal,
            dict,
        )
    ):
        raise LabPromotionTargetPreparationError(
            "approval consumption returned invalid records"
        )

    expected_approval = {
        "approval_id": approval_id,
        "proposal_id": transaction.proposal_id,
    }

    for key, expected in expected_approval.items():
        if approval.get(
            key
        ) != expected:
            raise LabPromotionTargetPreparationError(
                f"consumed approval {key} mismatch"
            )

    expected_terminal = {
        "approval_id": approval_id,
        "proposal_id": transaction.proposal_id,
        "terminal_state": "consumed",
        "run_id": transaction.run_id,
    }

    for key, expected in expected_terminal.items():
        if terminal.get(
            key
        ) != expected:
            raise LabPromotionTargetPreparationError(
                f"consumed terminal {key} mismatch"
            )

    terminal_at = terminal.get(
        "terminal_at"
    )

    if (
        not isinstance(
            terminal_at,
            str,
        )
        or not terminal_at
    ):
        raise LabPromotionTargetPreparationError(
            "consumed terminal timestamp is invalid"
        )

    return terminal_at


def prepare_promotion_target(
    *,
    approval_root: str | os.PathLike[str],
    journal_root: str | os.PathLike[str],
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
    approval_id: str,
) -> LabPromotionTargetPreparationResult:
    """Consume exact approval and prepare exact after-image temporaries."""
    try:
        transaction = validate_lab_promotion_transaction(
            transaction
        )
    except (
        TypeError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionTargetPreparationError(
            f"transaction validation failed: {exc}"
        ) from exc

    if transaction.state != STATE_PREPARED:
        raise LabPromotionTargetPreparationError(
            "target preparation requires PREPARED transaction"
        )

    if any(
        item.progress != PROGRESS_PENDING
        for item in transaction.files
    ):
        raise LabPromotionTargetPreparationError(
            "target preparation requires every file PENDING"
        )

    _require_exact_journal(
        journal_root,
        transaction,
    )

    try:
        materials = load_promotion_recovery_materials(
            recovery_root,
            transaction=transaction,
        )
    except LabPromotionRecoveryMaterialsError as exc:
        raise LabPromotionTargetPreparationError(
            f"durable recovery evidence validation failed: {exc}"
        ) from exc

    try:
        preflight = inspect_promotion_prepared_target(
            recovery_root=recovery_root,
            transaction=transaction,
        )
    except LabPromotionPreparedTargetError as exc:
        raise LabPromotionTargetPreparationError(
            f"target preflight failed: {exc}"
        ) from exc

    if preflight.materials_id != materials.materials_id:
        raise LabPromotionTargetPreparationError(
            "preflight recovery-material identity mismatch"
        )

    _require_all_absent(
        preflight
    )

    try:
        consumption = consume_promotion_approval(
            approval_root,
            approval_id=approval_id,
            proposal_id=transaction.proposal_id,
            run_id=transaction.run_id,
        )
    except LabPromotionApprovalStoreError as exc:
        raise LabPromotionTargetPreparationError(
            "exact approval consumption failed; do not retry automatically: "
            f"{exc}"
        ) from exc

    terminal_at = _validate_consumption(
        consumption,
        approval_id=approval_id,
        transaction=transaction,
    )

    try:
        _require_exact_journal(
            journal_root,
            transaction,
        )

        materials_after = load_promotion_recovery_materials(
            recovery_root,
            transaction=transaction,
        )

        if materials_after != materials:
            raise LabPromotionTargetPreparationError(
                "durable recovery evidence changed after approval consumption"
            )

        post_approval = inspect_promotion_prepared_target(
            recovery_root=recovery_root,
            transaction=transaction,
        )

        if post_approval.materials_id != materials.materials_id:
            raise LabPromotionTargetPreparationError(
                "post-approval recovery-material identity mismatch"
            )

        _require_all_absent(
            post_approval
        )

        repository_fd = _open_repository(
            transaction.repository_path,
            expected_device=transaction.repository_device,
            expected_inode=transaction.repository_inode,
        )

        try:
            if len(
                transaction.files
            ) != len(
                materials.files
            ):
                raise LabPromotionTargetPreparationError(
                    "transaction/recovery file count mismatch"
                )

            for (
                transaction_file,
                material_file,
            ) in zip(
                transaction.files,
                materials.files,
                strict=True,
            ):
                if transaction_file.path != material_file.path:
                    raise LabPromotionTargetPreparationError(
                        "transaction/recovery path mismatch"
                    )

                if (
                    transaction_file.after_bytes
                    != material_file.after_bytes
                    or transaction_file.after_sha256
                    != material_file.after_sha256
                    or transaction_file.after_mode
                    != material_file.after_mode
                ):
                    raise LabPromotionTargetPreparationError(
                        "transaction/recovery after-state mismatch"
                    )

                try:
                    data = transaction_file.after_content.encode(
                        "utf-8"
                    )
                except UnicodeEncodeError as exc:
                    raise LabPromotionTargetPreparationError(
                        f"after_content is not UTF-8 encodable for "
                        f"{transaction_file.path!r}"
                    ) from exc

                if len(
                    data
                ) != material_file.after_bytes:
                    raise LabPromotionTargetPreparationError(
                        f"after byte count mismatch for "
                        f"{transaction_file.path!r}"
                    )

                _prepare_one_temp(
                    repository_fd,
                    destination_path=transaction_file.path,
                    temp_name=material_file.after_temp_name,
                    data=data,
                    expected_mode=material_file.after_mode,
                )

        finally:
            os.close(
                repository_fd
            )

        final_inspection = inspect_promotion_prepared_target(
            recovery_root=recovery_root,
            transaction=transaction,
        )

        if final_inspection.materials_id != materials.materials_id:
            raise LabPromotionTargetPreparationError(
                "final recovery-material identity mismatch"
            )

        _require_all_exact(
            final_inspection
        )

        _require_exact_journal(
            journal_root,
            transaction,
        )

        return LabPromotionTargetPreparationResult(
            component=TARGET_PREPARER_COMPONENT,
            schema_version=TARGET_PREPARER_SCHEMA_VERSION,
            transaction_id=transaction.transaction_id,
            proposal_id=transaction.proposal_id,
            run_id=transaction.run_id,
            approval_id=approval_id,
            materials_id=materials.materials_id,
            repository_path=transaction.repository_path,
            terminal_at=terminal_at,
            temps=final_inspection.temps,
        )

    except Exception as exc:
        if isinstance(
            exc,
            LabPromotionTargetPreparationError,
        ):
            raise LabPromotionTargetPreparationError(
                "target preparation failed after approval consumption; "
                "approval remains consumed and any created temporaries "
                f"must not be cleaned or reused automatically: {exc}"
            ) from exc

        raise LabPromotionTargetPreparationError(
            "target preparation failed after approval consumption; "
            "approval remains consumed and any created temporaries "
            "must not be cleaned or reused automatically"
        ) from exc
