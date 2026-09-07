"""Durable private recovery material for one promotion transaction.

This module preserves exact MODIFY preimages in a private state root and
derives deterministic future after-image temporary filenames.

It deliberately does not:

- open or modify the target repository,
- create after-image temporary files in the target repository,
- replace, rename, remove, or unlink destination files,
- execute Git or shell commands,
- consume human approval,
- perform rollback or promotion.

Repository mutation remains a separate executor authority boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator

from .lab_promotion_proposal import (
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
)
from .lab_promotion_transaction_state import (
    PROGRESS_PENDING,
    STATE_PREPARED,
    LabPromotionTransaction,
    LabPromotionTransactionStateError,
    validate_lab_promotion_transaction,
)


RECOVERY_MATERIALS_COMPONENT = (
    "hands-free-auto-lab-promotion-recovery-materials-v2"
)
RECOVERY_MATERIALS_SCHEMA_VERSION = 2

LOCK_FILENAME = "promotion-recovery-materials.lock"
TRANSACTIONS_DIRECTORY = "transactions"
MANIFEST_FILENAME = "manifest.json"
PREIMAGES_DIRECTORY = "preimages"

ROOT_ENTRIES = frozenset(
    {
        LOCK_FILENAME,
        TRANSACTIONS_DIRECTORY,
    }
)

TRANSACTION_ENTRIES = frozenset(
    {
        MANIFEST_FILENAME,
        PREIMAGES_DIRECTORY,
    }
)

MAX_PREIMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_PREIMAGE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024

_MATERIALS_ID_DOMAIN = (
    b"hands-free-auto-lab-promotion-recovery-materials-id-v1\x00"
)

_TEMP_NAME_PREFIX = (
    ".hands-free-auto-lab-promote-"
)

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)

_MANIFEST_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "materials_id",
        "transaction_id",
        "proposal_id",
        "run_id",
        "repository_path",
        "repository_device",
        "repository_inode",
        "branch",
        "head",
        "files",
    }
)

_FILE_FIELDS = frozenset(
    {
        "operation",
        "path",
        "before_bytes",
        "before_sha256",
        "before_mode",
        "after_bytes",
        "after_sha256",
        "after_mode",
        "after_temp_name",
        "preimage_name",
    }
)


class LabPromotionRecoveryMaterialsError(
    RuntimeError
):
    """Raised when promotion recovery material is unsafe or inconsistent."""


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionRecoveryFile:
    operation: str
    path: str
    before_bytes: int | None
    before_sha256: str | None
    before_mode: int | None
    after_bytes: int
    after_sha256: str
    after_mode: int
    after_temp_name: str
    preimage_name: str | None


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionRecoveryMaterials:
    component: str
    schema_version: int
    materials_id: str
    transaction_id: str
    proposal_id: str
    run_id: str
    repository_path: str
    repository_device: int
    repository_inode: int
    branch: str
    head: str
    files: tuple[
        LabPromotionRecoveryFile,
        ...,
    ]


def _canonical_json_bytes(
    value: object,
) -> bytes:
    return (
        json.dumps(
            value,
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


def _require_identifier(
    name: str,
    value: object,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionRecoveryMaterialsError(
            f"{name} must be a string"
        )

    if (
        len(value) != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabPromotionRecoveryMaterialsError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _require_valid_transaction(
    transaction: LabPromotionTransaction,
) -> LabPromotionTransaction:
    try:
        return validate_lab_promotion_transaction(
            transaction
        )
    except (
        TypeError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionRecoveryMaterialsError(
            f"transaction failed validation: {exc}"
        ) from exc


def _require_prepared_transaction(
    transaction: LabPromotionTransaction,
) -> LabPromotionTransaction:
    transaction = _require_valid_transaction(
        transaction
    )

    if transaction.state != STATE_PREPARED:
        raise LabPromotionRecoveryMaterialsError(
            "recovery material requires PREPARED transaction"
        )

    if any(
        item.progress != PROGRESS_PENDING
        for item in transaction.files
    ):
        raise LabPromotionRecoveryMaterialsError(
            "recovery material requires every file PENDING"
        )

    return transaction


def _path_digest(
    path: str,
) -> str:
    return hashlib.sha256(
        path.encode(
            "utf-8"
        )
    ).hexdigest()


def _after_temp_name(
    transaction_id: str,
    path: str,
) -> str:
    name = (
        _TEMP_NAME_PREFIX
        + transaction_id
        + "-"
        + _path_digest(
            path
        )
        + ".tmp"
    )

    if (
        "/"
        in name
        or "\\"
        in name
        or len(
            name.encode(
                "utf-8"
            )
        )
        > 255
    ):
        raise LabPromotionRecoveryMaterialsError(
            "derived after-image temporary filename is unsafe"
        )

    return name


def _preimage_name(
    path: str,
) -> str:
    return (
        _path_digest(
            path
        )
        + ".preimage"
    )


def _materials_identity_object(
    *,
    transaction_id: str,
    proposal_id: str,
    run_id: str,
    repository_path: str,
    repository_device: int,
    repository_inode: int,
    branch: str,
    head: str,
    files: tuple[
        LabPromotionRecoveryFile,
        ...,
    ],
) -> dict[str, object]:
    return {
        "component": RECOVERY_MATERIALS_COMPONENT,
        "schema_version": RECOVERY_MATERIALS_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "proposal_id": proposal_id,
        "run_id": run_id,
        "repository_path": repository_path,
        "repository_device": repository_device,
        "repository_inode": repository_inode,
        "branch": branch,
        "head": head,
        "files": [
            {
                "operation": item.operation,
                "path": item.path,
                "before_bytes": item.before_bytes,
                "before_sha256": item.before_sha256,
                "before_mode": item.before_mode,
                "after_bytes": item.after_bytes,
                "after_sha256": item.after_sha256,
                "after_mode": item.after_mode,
                "after_temp_name": item.after_temp_name,
                "preimage_name": item.preimage_name,
            }
            for item in files
        ],
    }


def _materials_id(
    identity: dict[str, object],
) -> str:
    return hashlib.sha256(
        _MATERIALS_ID_DOMAIN
        + _canonical_json_bytes(
            identity
        )
    ).hexdigest()


def build_promotion_recovery_materials(
    *,
    transaction: LabPromotionTransaction,
    preimages: Mapping[
        str,
        bytes,
    ],
) -> tuple[
    LabPromotionRecoveryMaterials,
    dict[
        str,
        bytes,
    ],
]:
    """Build exact private recovery evidence without filesystem access."""
    transaction = _require_prepared_transaction(
        transaction
    )

    if not isinstance(
        preimages,
        Mapping,
    ):
        raise TypeError(
            "preimages must be a mapping"
        )

    expected_modify_paths = {
        item.path
        for item in transaction.files
        if item.operation
        == PROMOTION_OPERATION_MODIFY
    }

    supplied_paths = set(
        preimages
    )

    if supplied_paths != expected_modify_paths:
        missing = sorted(
            expected_modify_paths
            - supplied_paths
        )

        extra = sorted(
            supplied_paths
            - expected_modify_paths
        )

        raise LabPromotionRecoveryMaterialsError(
            "preimage paths must exactly match MODIFY paths; "
            f"missing={missing!r}, extra={extra!r}"
        )

    normalized_preimages: dict[
        str,
        bytes,
    ] = {}

    total_bytes = 0
    files: list[
        LabPromotionRecoveryFile
    ] = []

    for item in transaction.files:
        if item.operation == PROMOTION_OPERATION_MODIFY:
            raw = preimages[
                item.path
            ]

            if not isinstance(
                raw,
                bytes,
            ):
                raise LabPromotionRecoveryMaterialsError(
                    f"preimage must be bytes for {item.path!r}"
                )

            if len(
                raw
            ) > MAX_PREIMAGE_BYTES:
                raise LabPromotionRecoveryMaterialsError(
                    f"preimage exceeds per-file limit for {item.path!r}"
                )

            digest = hashlib.sha256(
                raw
            ).hexdigest()

            if len(
                raw
            ) != item.before_bytes:
                raise LabPromotionRecoveryMaterialsError(
                    f"preimage byte count mismatch for {item.path!r}"
                )

            if digest != item.before_sha256:
                raise LabPromotionRecoveryMaterialsError(
                    f"preimage SHA-256 mismatch for {item.path!r}"
                )

            total_bytes += len(
                raw
            )

            if total_bytes > MAX_TOTAL_PREIMAGE_BYTES:
                raise LabPromotionRecoveryMaterialsError(
                    "total preimage material exceeds limit"
                )

            normalized_preimages[
                item.path
            ] = bytes(
                raw
            )

            preimage_name = _preimage_name(
                item.path
            )

        elif item.operation == PROMOTION_OPERATION_ADD:
            preimage_name = None

        else:
            raise LabPromotionRecoveryMaterialsError(
                f"unsupported operation {item.operation!r}"
            )

        files.append(
            LabPromotionRecoveryFile(
                operation=item.operation,
                path=item.path,
                before_bytes=item.before_bytes,
                before_sha256=item.before_sha256,
                before_mode=item.before_mode,
                after_bytes=item.after_bytes,
                after_sha256=item.after_sha256,
                after_mode=item.after_mode,
                after_temp_name=_after_temp_name(
                    transaction.transaction_id,
                    item.path,
                ),
                preimage_name=preimage_name,
            )
        )

    frozen_files = tuple(
        files
    )

    identity = _materials_identity_object(
        transaction_id=transaction.transaction_id,
        proposal_id=transaction.proposal_id,
        run_id=transaction.run_id,
        repository_path=transaction.repository_path,
        repository_device=transaction.repository_device,
        repository_inode=transaction.repository_inode,
        branch=transaction.branch,
        head=transaction.head,
        files=frozen_files,
    )

    materials = LabPromotionRecoveryMaterials(
        component=RECOVERY_MATERIALS_COMPONENT,
        schema_version=RECOVERY_MATERIALS_SCHEMA_VERSION,
        materials_id=_materials_id(
            identity
        ),
        transaction_id=transaction.transaction_id,
        proposal_id=transaction.proposal_id,
        run_id=transaction.run_id,
        repository_path=transaction.repository_path,
        repository_device=transaction.repository_device,
        repository_inode=transaction.repository_inode,
        branch=transaction.branch,
        head=transaction.head,
        files=frozen_files,
    )

    validate_promotion_recovery_materials(
        materials=materials,
        transaction=transaction,
    )

    return (
        materials,
        normalized_preimages,
    )


def validate_promotion_recovery_materials(
    *,
    materials: LabPromotionRecoveryMaterials,
    transaction: LabPromotionTransaction,
) -> LabPromotionRecoveryMaterials:
    transaction = _require_valid_transaction(
        transaction
    )

    if not isinstance(
        materials,
        LabPromotionRecoveryMaterials,
    ):
        raise TypeError(
            "materials must be LabPromotionRecoveryMaterials"
        )

    if materials.component != RECOVERY_MATERIALS_COMPONENT:
        raise LabPromotionRecoveryMaterialsError(
            "recovery-material component mismatch"
        )

    if (
        materials.schema_version
        != RECOVERY_MATERIALS_SCHEMA_VERSION
    ):
        raise LabPromotionRecoveryMaterialsError(
            "recovery-material schema mismatch"
        )

    _require_identifier(
        "materials_id",
        materials.materials_id,
    )

    expected_scalars = (
        (
            "transaction_id",
            materials.transaction_id,
            transaction.transaction_id,
        ),
        (
            "proposal_id",
            materials.proposal_id,
            transaction.proposal_id,
        ),
        (
            "run_id",
            materials.run_id,
            transaction.run_id,
        ),
        (
            "repository_path",
            materials.repository_path,
            transaction.repository_path,
        ),
        (
            "repository_device",
            materials.repository_device,
            transaction.repository_device,
        ),
        (
            "repository_inode",
            materials.repository_inode,
            transaction.repository_inode,
        ),
        (
            "branch",
            materials.branch,
            transaction.branch,
        ),
        (
            "head",
            materials.head,
            transaction.head,
        ),
    )

    for (
        name,
        observed,
        expected,
    ) in expected_scalars:
        if observed != expected:
            raise LabPromotionRecoveryMaterialsError(
                f"{name} does not match exact transaction"
            )

    if not isinstance(
        materials.files,
        tuple,
    ):
        raise LabPromotionRecoveryMaterialsError(
            "recovery-material files must be a tuple"
        )

    if len(
        materials.files
    ) != len(
        transaction.files
    ):
        raise LabPromotionRecoveryMaterialsError(
            "recovery-material file count mismatch"
        )

    for material_file, transaction_file in zip(
        materials.files,
        transaction.files,
        strict=True,
    ):
        if not isinstance(
            material_file,
            LabPromotionRecoveryFile,
        ):
            raise LabPromotionRecoveryMaterialsError(
                "invalid recovery-material file value"
            )

        if material_file.operation != transaction_file.operation:
            raise LabPromotionRecoveryMaterialsError(
                f"operation mismatch for {transaction_file.path!r}"
            )

        if material_file.path != transaction_file.path:
            raise LabPromotionRecoveryMaterialsError(
                "recovery-material file path mismatch"
            )

        if material_file.before_bytes != transaction_file.before_bytes:
            raise LabPromotionRecoveryMaterialsError(
                f"before byte count mismatch for {transaction_file.path!r}"
            )

        if material_file.before_sha256 != transaction_file.before_sha256:
            raise LabPromotionRecoveryMaterialsError(
                f"before SHA-256 mismatch for {transaction_file.path!r}"
            )

        if material_file.before_mode != transaction_file.before_mode:
            raise LabPromotionRecoveryMaterialsError(
                f"before mode mismatch for {transaction_file.path!r}"
            )

        if material_file.after_bytes != transaction_file.after_bytes:
            raise LabPromotionRecoveryMaterialsError(
                f"after byte count mismatch for {transaction_file.path!r}"
            )

        if material_file.after_sha256 != transaction_file.after_sha256:
            raise LabPromotionRecoveryMaterialsError(
                f"after SHA-256 mismatch for {transaction_file.path!r}"
            )

        if material_file.after_mode != transaction_file.after_mode:
            raise LabPromotionRecoveryMaterialsError(
                f"after mode mismatch for {transaction_file.path!r}"
            )

        expected_temp = _after_temp_name(
            transaction.transaction_id,
            transaction_file.path,
        )

        if material_file.after_temp_name != expected_temp:
            raise LabPromotionRecoveryMaterialsError(
                f"after-image temporary name mismatch for "
                f"{transaction_file.path!r}"
            )

        if transaction_file.operation == PROMOTION_OPERATION_MODIFY:
            expected_preimage = _preimage_name(
                transaction_file.path
            )

            if material_file.preimage_name != expected_preimage:
                raise LabPromotionRecoveryMaterialsError(
                    f"preimage name mismatch for {transaction_file.path!r}"
                )

        else:
            if material_file.preimage_name is not None:
                raise LabPromotionRecoveryMaterialsError(
                    f"ADD must not have preimage for {transaction_file.path!r}"
                )

    identity = _materials_identity_object(
        transaction_id=materials.transaction_id,
        proposal_id=materials.proposal_id,
        run_id=materials.run_id,
        repository_path=materials.repository_path,
        repository_device=materials.repository_device,
        repository_inode=materials.repository_inode,
        branch=materials.branch,
        head=materials.head,
        files=materials.files,
    )

    expected_id = _materials_id(
        identity
    )

    if materials.materials_id != expected_id:
        raise LabPromotionRecoveryMaterialsError(
            "materials_id does not match exact recovery evidence"
        )

    return materials


def _manifest_object(
    materials: LabPromotionRecoveryMaterials,
) -> dict[str, object]:
    return {
        "component": materials.component,
        "schema_version": materials.schema_version,
        "materials_id": materials.materials_id,
        "transaction_id": materials.transaction_id,
        "proposal_id": materials.proposal_id,
        "run_id": materials.run_id,
        "repository_path": materials.repository_path,
        "repository_device": materials.repository_device,
        "repository_inode": materials.repository_inode,
        "branch": materials.branch,
        "head": materials.head,
        "files": [
            {
                "operation": item.operation,
                "path": item.path,
                "before_bytes": item.before_bytes,
                "before_sha256": item.before_sha256,
                "before_mode": item.before_mode,
                "after_bytes": item.after_bytes,
                "after_sha256": item.after_sha256,
                "after_mode": item.after_mode,
                "after_temp_name": item.after_temp_name,
                "preimage_name": item.preimage_name,
            }
            for item in materials.files
        ],
    }


def _require_root_path(
    root: str | os.PathLike[str],
) -> Path:
    path = Path(
        root
    )

    text = os.fspath(
        path
    )

    if not os.path.isabs(
        text
    ):
        raise LabPromotionRecoveryMaterialsError(
            "recovery-material root must be absolute"
        )

    if os.path.normpath(
        text
    ) != text:
        raise LabPromotionRecoveryMaterialsError(
            "recovery-material root must be normalized"
        )

    if os.path.realpath(
        text
    ) != text:
        raise LabPromotionRecoveryMaterialsError(
            "recovery-material root must be canonical and contain "
            "no symlink components"
        )

    return path


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

    if os.mkdir not in os.supports_dir_fd:
        missing.append(
            "os.mkdir(dir_fd=...)"
        )

    if os.listdir not in os.supports_fd:
        missing.append(
            "os.listdir(fd)"
        )

    if missing:
        raise LabPromotionRecoveryMaterialsError(
            "secure recovery-material primitives unavailable: "
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


def _validate_private_stat(
    st: os.stat_result,
    *,
    name: str,
    directory: bool,
) -> None:
    if directory:
        if not stat.S_ISDIR(
            st.st_mode
        ):
            raise LabPromotionRecoveryMaterialsError(
                f"{name} must be a directory"
            )

        if (
            st.st_mode
            & 0o777
        ) != 0o700:
            raise LabPromotionRecoveryMaterialsError(
                f"{name} must have mode 0700"
            )

    else:
        if not stat.S_ISREG(
            st.st_mode
        ):
            raise LabPromotionRecoveryMaterialsError(
                f"{name} must be a regular file"
            )

        if st.st_nlink != 1:
            raise LabPromotionRecoveryMaterialsError(
                f"{name} must have exactly one hard link"
            )

        if (
            st.st_mode
            & 0o777
        ) != 0o600:
            raise LabPromotionRecoveryMaterialsError(
                f"{name} must have mode 0600"
            )

    if st.st_uid != os.geteuid():
        raise LabPromotionRecoveryMaterialsError(
            f"{name} must be owned by the effective user"
        )


def _open_directory_path(
    path: Path,
    *,
    name: str,
) -> int:
    try:
        fd = os.open(
            path,
            _directory_flags(),
        )
    except OSError as exc:
        raise LabPromotionRecoveryMaterialsError(
            f"cannot securely open {name}: {exc}"
        ) from exc

    try:
        _validate_private_stat(
            os.fstat(
                fd
            ),
            name=name,
            directory=True,
        )
    except Exception:
        os.close(
            fd
        )
        raise

    return fd


def _open_child_directory(
    parent_fd: int,
    *,
    filename: str,
    name: str,
) -> int:
    try:
        fd = os.open(
            filename,
            _directory_flags(),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise LabPromotionRecoveryMaterialsError(
            f"cannot securely open {name}: {exc}"
        ) from exc

    try:
        _validate_private_stat(
            os.fstat(
                fd
            ),
            name=name,
            directory=True,
        )
    except Exception:
        os.close(
            fd
        )
        raise

    return fd


def _create_directory(
    parent_fd: int,
    *,
    filename: str,
) -> int:
    try:
        os.mkdir(
            filename,
            mode=0o700,
            dir_fd=parent_fd,
        )
    except FileExistsError as exc:
        raise LabPromotionRecoveryMaterialsError(
            f"refusing existing directory {filename}"
        ) from exc
    except OSError as exc:
        raise LabPromotionRecoveryMaterialsError(
            f"cannot create directory {filename}: {exc}"
        ) from exc

    fd: int | None = None

    try:
        fd = os.open(
            filename,
            _directory_flags(),
            dir_fd=parent_fd,
        )

        os.fchmod(
            fd,
            0o700,
        )

        _validate_private_stat(
            os.fstat(
                fd
            ),
            name=filename,
            directory=True,
        )

        return fd

    except Exception:
        if fd is not None:
            os.close(
                fd
            )

        raise


def _write_all(
    fd: int,
    data: bytes,
) -> None:
    view = memoryview(
        data
    )

    while view:
        try:
            count = os.write(
                fd,
                view,
            )
        except OSError as exc:
            raise LabPromotionRecoveryMaterialsError(
                f"recovery-material write failed: {exc}"
            ) from exc

        if count <= 0:
            raise LabPromotionRecoveryMaterialsError(
                "short recovery-material write"
            )

        view = view[
            count:
        ]


def _create_file(
    directory_fd: int,
    *,
    filename: str,
    data: bytes,
    max_bytes: int,
    allow_empty: bool,
) -> None:
    if (
        (
            not allow_empty
            and not data
        )
        or len(
            data
        )
        > max_bytes
    ):
        raise LabPromotionRecoveryMaterialsError(
            f"{filename} has invalid size"
        )

    try:
        fd = os.open(
            filename,
            _create_flags(),
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LabPromotionRecoveryMaterialsError(
            f"cannot create {filename}: {exc}"
        ) from exc

    try:
        os.fchmod(
            fd,
            0o600,
        )

        _write_all(
            fd,
            data,
        )

        os.fsync(
            fd
        )

    finally:
        os.close(
            fd
        )


def _read_file(
    directory_fd: int,
    *,
    filename: str,
    max_bytes: int,
    allow_empty: bool,
) -> bytes:
    try:
        fd = os.open(
            filename,
            _read_flags(),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LabPromotionRecoveryMaterialsError(
            f"cannot securely open {filename}: {exc}"
        ) from exc

    try:
        st = os.fstat(
            fd
        )

        _validate_private_stat(
            st,
            name=filename,
            directory=False,
        )

        if (
            st.st_size > max_bytes
            or (
                not allow_empty
                and st.st_size <= 0
            )
        ):
            raise LabPromotionRecoveryMaterialsError(
                f"{filename} has invalid size"
            )

        chunks: list[bytes] = []
        total = 0

        while True:
            try:
                chunk = os.read(
                    fd,
                    64 * 1024,
                )
            except OSError as exc:
                raise LabPromotionRecoveryMaterialsError(
                    f"cannot read {filename}: {exc}"
                ) from exc

            if not chunk:
                break

            total += len(
                chunk
            )

            if total > max_bytes:
                raise LabPromotionRecoveryMaterialsError(
                    f"{filename} exceeds maximum size"
                )

            chunks.append(
                chunk
            )

        data = b"".join(
            chunks
        )

        if (
            not allow_empty
            and not data
        ):
            raise LabPromotionRecoveryMaterialsError(
                f"{filename} must not be empty"
            )

        return data

    finally:
        os.close(
            fd
        )


def _list_directory(
    fd: int,
    *,
    name: str,
) -> frozenset[str]:
    try:
        return frozenset(
            os.listdir(
                fd
            )
        )
    except OSError as exc:
        raise LabPromotionRecoveryMaterialsError(
            f"cannot list {name}: {exc}"
        ) from exc


def _create_empty_lock(
    root_fd: int,
) -> None:
    _create_file(
        root_fd,
        filename=LOCK_FILENAME,
        data=b"",
        max_bytes=0,
        allow_empty=True,
    )


def _open_lock(
    root_fd: int,
) -> int:
    try:
        fd = os.open(
            LOCK_FILENAME,
            _read_flags(),
            dir_fd=root_fd,
        )
    except OSError as exc:
        raise LabPromotionRecoveryMaterialsError(
            f"cannot open {LOCK_FILENAME}: {exc}"
        ) from exc

    try:
        _validate_private_stat(
            os.fstat(
                fd
            ),
            name=LOCK_FILENAME,
            directory=False,
        )
    except Exception:
        os.close(
            fd
        )
        raise

    return fd


def _open_transactions(
    root_fd: int,
) -> int:
    return _open_child_directory(
        root_fd,
        filename=TRANSACTIONS_DIRECTORY,
        name=TRANSACTIONS_DIRECTORY,
    )


def _validate_root(
    root_fd: int,
) -> None:
    entries = _list_directory(
        root_fd,
        name="recovery-material root",
    )

    if entries != ROOT_ENTRIES:
        raise LabPromotionRecoveryMaterialsError(
            "recovery-material root entries mismatch: "
            f"{sorted(entries)!r}"
        )

    lock_fd = _open_lock(
        root_fd
    )

    os.close(
        lock_fd
    )

    transactions_fd = _open_transactions(
        root_fd
    )

    try:
        names = _list_directory(
            transactions_fd,
            name="recovery-material transactions",
        )

        for name in names:
            _require_identifier(
                "transaction directory name",
                name,
            )

            transaction_fd = _open_child_directory(
                transactions_fd,
                filename=name,
                name=f"recovery transaction {name}",
            )

            os.close(
                transaction_fd
            )

    finally:
        os.close(
            transactions_fd
        )


def initialize_promotion_recovery_materials_root(
    root: str | os.PathLike[str],
) -> Path:
    root_path = _require_root_path(
        root
    )

    root_fd = _open_directory_path(
        root_path,
        name="recovery-material root",
    )

    try:
        entries = _list_directory(
            root_fd,
            name="recovery-material root",
        )

        if not entries:
            _create_empty_lock(
                root_fd
            )

            transactions_fd = _create_directory(
                root_fd,
                filename=TRANSACTIONS_DIRECTORY,
            )

            os.fsync(
                transactions_fd
            )

            os.close(
                transactions_fd
            )

            os.fsync(
                root_fd
            )

        _validate_root(
            root_fd
        )

    finally:
        os.close(
            root_fd
        )

    return root_path


@contextmanager
def _materials_lock(
    root: str | os.PathLike[str],
    *,
    exclusive: bool,
) -> Iterator[int]:
    root_path = _require_root_path(
        root
    )

    root_fd = _open_directory_path(
        root_path,
        name="recovery-material root",
    )

    lock_fd: int | None = None

    try:
        _validate_root(
            root_fd
        )

        lock_fd = _open_lock(
            root_fd
        )

        fcntl.flock(
            lock_fd,
            (
                fcntl.LOCK_EX
                if exclusive
                else fcntl.LOCK_SH
            ),
        )

        _validate_root(
            root_fd
        )

        yield root_fd

    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(
                    lock_fd,
                    fcntl.LOCK_UN,
                )
            except OSError:
                pass

            os.close(
                lock_fd
            )

        os.close(
            root_fd
        )


def _decode_manifest(
    raw: bytes,
) -> LabPromotionRecoveryMaterials:
    try:
        value = json.loads(
            raw.decode(
                "utf-8"
            )
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise LabPromotionRecoveryMaterialsError(
            "recovery manifest is invalid JSON"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise LabPromotionRecoveryMaterialsError(
            "recovery manifest must be an object"
        )

    if frozenset(
        value
    ) != _MANIFEST_FIELDS:
        raise LabPromotionRecoveryMaterialsError(
            "recovery manifest fields do not exactly match schema"
        )

    if _canonical_json_bytes(
        value
    ) != raw:
        raise LabPromotionRecoveryMaterialsError(
            "recovery manifest is not canonical JSON"
        )

    raw_files = value[
        "files"
    ]

    if (
        not isinstance(
            raw_files,
            list,
        )
        or not raw_files
    ):
        raise LabPromotionRecoveryMaterialsError(
            "recovery manifest files must be a non-empty list"
        )

    files: list[
        LabPromotionRecoveryFile
    ] = []

    for raw_file in raw_files:
        if not isinstance(
            raw_file,
            dict,
        ):
            raise LabPromotionRecoveryMaterialsError(
                "recovery manifest file must be an object"
            )

        if frozenset(
            raw_file
        ) != _FILE_FIELDS:
            raise LabPromotionRecoveryMaterialsError(
                "recovery manifest file fields do not exactly match schema"
            )

        files.append(
            LabPromotionRecoveryFile(
                operation=raw_file["operation"],
                path=raw_file["path"],
                before_bytes=raw_file["before_bytes"],
                before_sha256=raw_file["before_sha256"],
                before_mode=raw_file["before_mode"],
                after_bytes=raw_file["after_bytes"],
                after_sha256=raw_file["after_sha256"],
                after_mode=raw_file["after_mode"],
                after_temp_name=raw_file["after_temp_name"],
                preimage_name=raw_file["preimage_name"],
            )
        )

    return LabPromotionRecoveryMaterials(
        component=value["component"],
        schema_version=value["schema_version"],
        materials_id=value["materials_id"],
        transaction_id=value["transaction_id"],
        proposal_id=value["proposal_id"],
        run_id=value["run_id"],
        repository_path=value["repository_path"],
        repository_device=value["repository_device"],
        repository_inode=value["repository_inode"],
        branch=value["branch"],
        head=value["head"],
        files=tuple(
            files
        ),
    )


def _load_from_transaction_fd(
    transaction_fd: int,
    *,
    transaction: LabPromotionTransaction,
) -> tuple[
    LabPromotionRecoveryMaterials,
    dict[
        str,
        bytes,
    ],
]:
    entries = _list_directory(
        transaction_fd,
        name=f"recovery transaction {transaction.transaction_id}",
    )

    if entries != TRANSACTION_ENTRIES:
        raise LabPromotionRecoveryMaterialsError(
            "recovery transaction entries mismatch: "
            f"{sorted(entries)!r}"
        )

    raw_manifest = _read_file(
        transaction_fd,
        filename=MANIFEST_FILENAME,
        max_bytes=MAX_MANIFEST_BYTES,
        allow_empty=False,
    )

    materials = _decode_manifest(
        raw_manifest
    )

    validate_promotion_recovery_materials(
        materials=materials,
        transaction=transaction,
    )

    preimages_fd = _open_child_directory(
        transaction_fd,
        filename=PREIMAGES_DIRECTORY,
        name="preimages",
    )

    try:
        expected_names = {
            item.preimage_name
            for item in materials.files
            if item.preimage_name is not None
        }

        observed_names = _list_directory(
            preimages_fd,
            name="preimages",
        )

        if observed_names != expected_names:
            raise LabPromotionRecoveryMaterialsError(
                "preimage directory entries mismatch"
            )

        total_bytes = 0

        preimages: dict[
            str,
            bytes,
        ] = {}

        for item in materials.files:
            if item.operation != PROMOTION_OPERATION_MODIFY:
                continue

            if item.preimage_name is None:
                raise LabPromotionRecoveryMaterialsError(
                    f"MODIFY preimage name missing for {item.path!r}"
                )

            raw = _read_file(
                preimages_fd,
                filename=item.preimage_name,
                max_bytes=MAX_PREIMAGE_BYTES,
                allow_empty=True,
            )

            total_bytes += len(
                raw
            )

            if total_bytes > MAX_TOTAL_PREIMAGE_BYTES:
                raise LabPromotionRecoveryMaterialsError(
                    "total persisted preimage material exceeds limit"
                )

            if len(
                raw
            ) != item.before_bytes:
                raise LabPromotionRecoveryMaterialsError(
                    f"persisted preimage byte count mismatch for "
                    f"{item.path!r}"
                )

            digest = hashlib.sha256(
                raw
            ).hexdigest()

            if digest != item.before_sha256:
                raise LabPromotionRecoveryMaterialsError(
                    f"persisted preimage SHA-256 mismatch for "
                    f"{item.path!r}"
                )

            preimages[
                item.path
            ] = raw

        return (
            materials,
            preimages,
        )

    finally:
        os.close(
            preimages_fd
        )


def create_promotion_recovery_materials(
    root: str | os.PathLike[str],
    *,
    transaction: LabPromotionTransaction,
    preimages: Mapping[
        str,
        bytes,
    ],
) -> LabPromotionRecoveryMaterials:
    materials, normalized_preimages = (
        build_promotion_recovery_materials(
            transaction=transaction,
            preimages=preimages,
        )
    )

    with _materials_lock(
        root,
        exclusive=True,
    ) as root_fd:
        transactions_fd = _open_transactions(
            root_fd
        )

        try:
            transaction_fd = _create_directory(
                transactions_fd,
                filename=materials.transaction_id,
            )

            try:
                preimages_fd = _create_directory(
                    transaction_fd,
                    filename=PREIMAGES_DIRECTORY,
                )

                try:
                    for item in materials.files:
                        if item.operation != PROMOTION_OPERATION_MODIFY:
                            continue

                        if item.preimage_name is None:
                            raise LabPromotionRecoveryMaterialsError(
                                "internal MODIFY preimage name missing"
                            )

                        _create_file(
                            preimages_fd,
                            filename=item.preimage_name,
                            data=normalized_preimages[
                                item.path
                            ],
                            max_bytes=MAX_PREIMAGE_BYTES,
                            allow_empty=True,
                        )

                    os.fsync(
                        preimages_fd
                    )

                finally:
                    os.close(
                        preimages_fd
                    )

                manifest = _canonical_json_bytes(
                    _manifest_object(
                        materials
                    )
                )

                _create_file(
                    transaction_fd,
                    filename=MANIFEST_FILENAME,
                    data=manifest,
                    max_bytes=MAX_MANIFEST_BYTES,
                    allow_empty=False,
                )

                os.fsync(
                    transaction_fd
                )

                os.fsync(
                    transactions_fd
                )

                loaded_materials, loaded_preimages = (
                    _load_from_transaction_fd(
                        transaction_fd,
                        transaction=transaction,
                    )
                )

                if loaded_materials != materials:
                    raise LabPromotionRecoveryMaterialsError(
                        "persisted recovery material mismatch"
                    )

                if loaded_preimages != normalized_preimages:
                    raise LabPromotionRecoveryMaterialsError(
                        "persisted recovery preimage mismatch"
                    )

                return loaded_materials

            finally:
                os.close(
                    transaction_fd
                )

        finally:
            os.close(
                transactions_fd
            )


def load_promotion_recovery_materials(
    root: str | os.PathLike[str],
    *,
    transaction: LabPromotionTransaction,
) -> LabPromotionRecoveryMaterials:
    transaction = _require_valid_transaction(
        transaction
    )

    with _materials_lock(
        root,
        exclusive=False,
    ) as root_fd:
        transactions_fd = _open_transactions(
            root_fd
        )

        try:
            transaction_fd = _open_child_directory(
                transactions_fd,
                filename=transaction.transaction_id,
                name=f"recovery transaction {transaction.transaction_id}",
            )

            try:
                materials, _preimages = (
                    _load_from_transaction_fd(
                        transaction_fd,
                        transaction=transaction,
                    )
                )

                return materials

            finally:
                os.close(
                    transaction_fd
                )

        finally:
            os.close(
                transactions_fd
            )


def load_promotion_recovery_preimages(
    root: str | os.PathLike[str],
    *,
    transaction: LabPromotionTransaction,
) -> dict[
    str,
    bytes,
]:
    """Securely reopen exact verified MODIFY preimage bytes."""
    transaction = _require_valid_transaction(
        transaction
    )

    with _materials_lock(
        root,
        exclusive=False,
    ) as root_fd:
        transactions_fd = _open_transactions(
            root_fd
        )

        try:
            transaction_fd = _open_child_directory(
                transactions_fd,
                filename=transaction.transaction_id,
                name=f"recovery transaction {transaction.transaction_id}",
            )

            try:
                _materials, preimages = (
                    _load_from_transaction_fd(
                        transaction_fd,
                        transaction=transaction,
                    )
                )

                return preimages

            finally:
                os.close(
                    transaction_fd
                )

        finally:
            os.close(
                transactions_fd
            )
