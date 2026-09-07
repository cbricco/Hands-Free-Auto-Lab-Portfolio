"""Durable append-only journal for promotion transaction snapshots.

Authority is deliberately limited to an explicitly supplied private journal
state root.

This module does not:

- modify a promotion target repository,
- execute Git or shell commands,
- consume human approval,
- install proposed files,
- restore or delete destination files.

The immutable transaction plan is persisted once. Transaction progress is
persisted as an append-only sequence of canonical snapshot records.

Every append:

- validates the complete existing chain,
- requires the caller's exact expected previous snapshot,
- independently validates the supplied successor transition,
- creates the next record with O_EXCL,
- fsyncs the record and containing directories.

A partial or malformed record is never repaired automatically. It causes the
journal to fail closed for later inspection/recovery.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator

from .lab_promotion_transaction_state import (
    FILE_PROGRESS_STATES,
    PROGRESS_PENDING,
    STATE_PREPARED,
    TRANSACTION_COMPONENT,
    TRANSACTION_SCHEMA_VERSION,
    LabPromotionTransaction,
    LabPromotionTransactionFile,
    LabPromotionTransactionStateError,
    transition_lab_promotion_transaction,
    validate_lab_promotion_transaction,
)


JOURNAL_PLAN_COMPONENT = (
    "hands-free-auto-lab-promotion-transaction-journal-plan-v3"
)
JOURNAL_SNAPSHOT_COMPONENT = (
    "hands-free-auto-lab-promotion-transaction-journal-snapshot-v3"
)
JOURNAL_SCHEMA_VERSION = 3

LOCK_FILENAME = "promotion-transactions.lock"
TRANSACTIONS_DIRECTORY = "transactions"
PLAN_FILENAME = "plan.json"
SNAPSHOTS_DIRECTORY = "snapshots"

ROOT_ENTRIES = frozenset(
    {
        LOCK_FILENAME,
        TRANSACTIONS_DIRECTORY,
    }
)

TRANSACTION_ENTRIES = frozenset(
    {
        PLAN_FILENAME,
        SNAPSHOTS_DIRECTORY,
    }
)

MAX_PLAN_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_SNAPSHOTS = 100_000

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)

_PLAN_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "transaction_component",
        "transaction_schema_version",
        "transaction_id",
        "prepared_snapshot_id",
        "proposal_id",
        "candidate_id",
        "run_id",
        "repository_path",
        "repository_device",
        "repository_inode",
        "branch",
        "head",
        "risk_classification",
        "files",
    }
)

_PLAN_FILE_FIELDS = frozenset(
    {
        "operation",
        "path",
        "before_exists",
        "before_bytes",
        "before_sha256",
        "before_mode",
        "after_bytes",
        "after_sha256",
        "after_mode",
        "after_content",
        "source_kind",
        "source_id",
    }
)

_SNAPSHOT_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "transaction_id",
        "sequence",
        "previous_snapshot_id",
        "snapshot_id",
        "state",
        "file_progress",
    }
)

_PROGRESS_FIELDS = frozenset(
    {
        "path",
        "progress",
    }
)


class LabPromotionTransactionJournalError(
    RuntimeError
):
    """Raised when durable transaction journal state is unsafe."""


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
        raise LabPromotionTransactionJournalError(
            f"{name} must be a string"
        )

    if (
        len(value) != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabPromotionTransactionJournalError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


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
        raise LabPromotionTransactionJournalError(
            "transaction journal root must be absolute"
        )

    if os.path.normpath(
        text
    ) != text:
        raise LabPromotionTransactionJournalError(
            "transaction journal root must be normalized"
        )

    if os.path.realpath(
        text
    ) != text:
        raise LabPromotionTransactionJournalError(
            "transaction journal root must be canonical and contain "
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
        raise LabPromotionTransactionJournalError(
            "secure transaction-journal primitives unavailable: "
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


def _create_file_flags() -> int:
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
    require_directory: bool,
) -> None:
    if require_directory:
        if not stat.S_ISDIR(
            st.st_mode
        ):
            raise LabPromotionTransactionJournalError(
                f"{name} must be a directory"
            )

        if (
            st.st_mode
            & 0o777
        ) != 0o700:
            raise LabPromotionTransactionJournalError(
                f"{name} must have mode 0700"
            )

    else:
        if not stat.S_ISREG(
            st.st_mode
        ):
            raise LabPromotionTransactionJournalError(
                f"{name} must be a regular file"
            )

        if st.st_nlink != 1:
            raise LabPromotionTransactionJournalError(
                f"{name} must have exactly one hard link"
            )

        if (
            st.st_mode
            & 0o777
        ) != 0o600:
            raise LabPromotionTransactionJournalError(
                f"{name} must have mode 0600"
            )

    if st.st_uid != os.geteuid():
        raise LabPromotionTransactionJournalError(
            f"{name} must be owned by the effective user"
        )


def _open_private_directory_path(
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
        raise LabPromotionTransactionJournalError(
            f"cannot securely open {name}: {exc}"
        ) from exc

    try:
        _validate_private_stat(
            os.fstat(
                fd
            ),
            name=name,
            require_directory=True,
        )
    except Exception:
        os.close(
            fd
        )
        raise

    return fd


def _open_private_child_directory(
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
        raise LabPromotionTransactionJournalError(
            f"cannot securely open {name}: {exc}"
        ) from exc

    try:
        _validate_private_stat(
            os.fstat(
                fd
            ),
            name=name,
            require_directory=True,
        )
    except Exception:
        os.close(
            fd
        )
        raise

    return fd


def _create_private_directory(
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
        raise LabPromotionTransactionJournalError(
            f"refusing existing directory {filename}"
        ) from exc
    except OSError as exc:
        raise LabPromotionTransactionJournalError(
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
            require_directory=True,
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
            written = os.write(
                fd,
                view,
            )
        except OSError as exc:
            raise LabPromotionTransactionJournalError(
                f"transaction-journal write failed: {exc}"
            ) from exc

        if written <= 0:
            raise LabPromotionTransactionJournalError(
                "short write while persisting transaction journal"
            )

        view = view[
            written:
        ]


def _create_private_file(
    directory_fd: int,
    *,
    filename: str,
    data: bytes,
    max_bytes: int,
) -> None:
    if (
        not data
        or len(
            data
        )
        > max_bytes
    ):
        raise LabPromotionTransactionJournalError(
            f"{filename} has invalid record size"
        )

    try:
        fd = os.open(
            filename,
            _create_file_flags(),
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LabPromotionTransactionJournalError(
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

    except OSError as exc:
        raise LabPromotionTransactionJournalError(
            f"cannot persist {filename}: {exc}"
        ) from exc

    finally:
        os.close(
            fd
        )


def _create_empty_private_file(
    directory_fd: int,
    *,
    filename: str,
) -> None:
    try:
        fd = os.open(
            filename,
            _create_file_flags(),
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LabPromotionTransactionJournalError(
            f"cannot create {filename}: {exc}"
        ) from exc

    try:
        os.fchmod(
            fd,
            0o600,
        )

        os.fsync(
            fd
        )

    finally:
        os.close(
            fd
        )


def _read_private_file(
    directory_fd: int,
    *,
    filename: str,
    max_bytes: int,
) -> bytes:
    try:
        fd = os.open(
            filename,
            _read_flags(),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LabPromotionTransactionJournalError(
            f"cannot securely open {filename}: {exc}"
        ) from exc

    try:
        st = os.fstat(
            fd
        )

        _validate_private_stat(
            st,
            name=filename,
            require_directory=False,
        )

        if (
            st.st_size <= 0
            or st.st_size > max_bytes
        ):
            raise LabPromotionTransactionJournalError(
                f"{filename} has invalid record size"
            )

        chunks: list[bytes] = []
        remaining = max_bytes + 1

        while remaining > 0:
            try:
                chunk = os.read(
                    fd,
                    min(
                        65536,
                        remaining,
                    ),
                )
            except OSError as exc:
                raise LabPromotionTransactionJournalError(
                    f"cannot read {filename}: {exc}"
                ) from exc

            if not chunk:
                break

            chunks.append(
                chunk
            )

            remaining -= len(
                chunk
            )

        data = b"".join(
            chunks
        )

        if (
            not data
            or len(data) > max_bytes
        ):
            raise LabPromotionTransactionJournalError(
                f"{filename} has invalid record size"
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
        raise LabPromotionTransactionJournalError(
            f"cannot list {name}: {exc}"
        ) from exc


def _decode_canonical_object(
    raw: bytes,
    *,
    fields: frozenset[str],
    name: str,
) -> dict[str, Any]:
    try:
        text = raw.decode(
            "utf-8"
        )
    except UnicodeDecodeError as exc:
        raise LabPromotionTransactionJournalError(
            f"{name} is not UTF-8"
        ) from exc

    try:
        value = json.loads(
            text
        )
    except json.JSONDecodeError as exc:
        raise LabPromotionTransactionJournalError(
            f"{name} is not valid JSON"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise LabPromotionTransactionJournalError(
            f"{name} must be a JSON object"
        )

    if frozenset(
        value
    ) != fields:
        raise LabPromotionTransactionJournalError(
            f"{name} fields do not exactly match schema"
        )

    if _canonical_json_bytes(
        value
    ) != raw:
        raise LabPromotionTransactionJournalError(
            f"{name} is not canonical JSON"
        )

    return value


def _open_lock_file(
    root_fd: int,
) -> int:
    try:
        fd = os.open(
            LOCK_FILENAME,
            _read_flags(),
            dir_fd=root_fd,
        )
    except OSError as exc:
        raise LabPromotionTransactionJournalError(
            f"cannot open {LOCK_FILENAME}: {exc}"
        ) from exc

    try:
        _validate_private_stat(
            os.fstat(
                fd
            ),
            name=LOCK_FILENAME,
            require_directory=False,
        )
    except Exception:
        os.close(
            fd
        )
        raise

    return fd


def _open_transactions_directory(
    root_fd: int,
) -> int:
    return _open_private_child_directory(
        root_fd,
        filename=TRANSACTIONS_DIRECTORY,
        name=TRANSACTIONS_DIRECTORY,
    )


def _validate_initialized_root_fd(
    root_fd: int,
) -> None:
    entries = _list_directory(
        root_fd,
        name="transaction journal root",
    )

    if entries != ROOT_ENTRIES:
        raise LabPromotionTransactionJournalError(
            "transaction journal root entries mismatch: "
            f"{sorted(entries)!r}"
        )

    lock_fd = _open_lock_file(
        root_fd
    )

    os.close(
        lock_fd
    )

    transactions_fd = _open_transactions_directory(
        root_fd
    )

    try:
        transaction_names = _list_directory(
            transactions_fd,
            name="transactions directory",
        )

        for transaction_id in transaction_names:
            _require_identifier(
                "transaction directory name",
                transaction_id,
            )

            transaction_fd = _open_private_child_directory(
                transactions_fd,
                filename=transaction_id,
                name=f"transaction {transaction_id}",
            )

            os.close(
                transaction_fd
            )

    finally:
        os.close(
            transactions_fd
        )


def initialize_promotion_transaction_journal_root(
    root: str | os.PathLike[str],
) -> Path:
    root_path = _require_root_path(
        root
    )

    root_fd = _open_private_directory_path(
        root_path,
        name="transaction journal root",
    )

    try:
        entries = _list_directory(
            root_fd,
            name="transaction journal root",
        )

        if not entries:
            _create_empty_private_file(
                root_fd,
                filename=LOCK_FILENAME,
            )

            transactions_fd = _create_private_directory(
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

        _validate_initialized_root_fd(
            root_fd
        )

    except LabPromotionTransactionJournalError:
        raise

    except OSError as exc:
        raise LabPromotionTransactionJournalError(
            "transaction journal initialization failed; "
            f"partial state may remain for inspection: {exc}"
        ) from exc

    finally:
        os.close(
            root_fd
        )

    return root_path


@contextmanager
def _journal_lock(
    root: str | os.PathLike[str],
    *,
    exclusive: bool,
) -> Iterator[int]:
    root_path = _require_root_path(
        root
    )

    root_fd = _open_private_directory_path(
        root_path,
        name="transaction journal root",
    )

    lock_fd: int | None = None

    try:
        _validate_initialized_root_fd(
            root_fd
        )

        lock_fd = _open_lock_file(
            root_fd
        )

        operation = (
            fcntl.LOCK_EX
            if exclusive
            else fcntl.LOCK_SH
        )

        try:
            fcntl.flock(
                lock_fd,
                operation,
            )
        except OSError as exc:
            raise LabPromotionTransactionJournalError(
                f"cannot acquire {LOCK_FILENAME}: {exc}"
            ) from exc

        _validate_initialized_root_fd(
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


def _plan_object(
    transaction: LabPromotionTransaction,
) -> dict[str, object]:
    transaction = validate_lab_promotion_transaction(
        transaction
    )

    return {
        "component": JOURNAL_PLAN_COMPONENT,
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "transaction_component": transaction.component,
        "transaction_schema_version": transaction.schema_version,
        "transaction_id": transaction.transaction_id,
        "prepared_snapshot_id": transaction.snapshot_id,
        "proposal_id": transaction.proposal_id,
        "candidate_id": transaction.candidate_id,
        "run_id": transaction.run_id,
        "repository_path": transaction.repository_path,
        "repository_device": transaction.repository_device,
        "repository_inode": transaction.repository_inode,
        "branch": transaction.branch,
        "head": transaction.head,
        "risk_classification": transaction.risk_classification,
        "files": [
            {
                "operation": item.operation,
                "path": item.path,
                "before_exists": item.before_exists,
                "before_bytes": item.before_bytes,
                "before_sha256": item.before_sha256,
                "before_mode": item.before_mode,
                "after_bytes": item.after_bytes,
                "after_sha256": item.after_sha256,
                "after_mode": item.after_mode,
                "after_content": item.after_content,
                "source_kind": item.source_kind,
                "source_id": (
                    item.source_id
                ),
            }
            for item in transaction.files
        ],
    }


def _snapshot_object(
    transaction: LabPromotionTransaction,
    *,
    sequence: int,
    previous_snapshot_id: str | None,
) -> dict[str, object]:
    transaction = validate_lab_promotion_transaction(
        transaction
    )

    return {
        "component": JOURNAL_SNAPSHOT_COMPONENT,
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "transaction_id": transaction.transaction_id,
        "sequence": sequence,
        "previous_snapshot_id": previous_snapshot_id,
        "snapshot_id": transaction.snapshot_id,
        "state": transaction.state,
        "file_progress": [
            {
                "path": item.path,
                "progress": item.progress,
            }
            for item in transaction.files
        ],
    }


def _decode_plan(
    raw: bytes,
) -> LabPromotionTransaction:
    record = _decode_canonical_object(
        raw,
        fields=_PLAN_FIELDS,
        name="transaction plan",
    )

    if record["component"] != JOURNAL_PLAN_COMPONENT:
        raise LabPromotionTransactionJournalError(
            "transaction plan component mismatch"
        )

    if record["schema_version"] != JOURNAL_SCHEMA_VERSION:
        raise LabPromotionTransactionJournalError(
            "transaction plan schema mismatch"
        )

    if record["transaction_component"] != TRANSACTION_COMPONENT:
        raise LabPromotionTransactionJournalError(
            "transaction component mismatch"
        )

    if (
        record["transaction_schema_version"]
        != TRANSACTION_SCHEMA_VERSION
    ):
        raise LabPromotionTransactionJournalError(
            "transaction schema mismatch"
        )

    raw_files = record["files"]

    if (
        not isinstance(
            raw_files,
            list,
        )
        or not raw_files
    ):
        raise LabPromotionTransactionJournalError(
            "transaction plan files must be a non-empty list"
        )

    files: list[
        LabPromotionTransactionFile
    ] = []

    for raw_file in raw_files:
        if not isinstance(
            raw_file,
            dict,
        ):
            raise LabPromotionTransactionJournalError(
                "transaction plan file must be an object"
            )

        if frozenset(
            raw_file
        ) != _PLAN_FILE_FIELDS:
            raise LabPromotionTransactionJournalError(
                "transaction plan file fields do not exactly match schema"
            )

        files.append(
            LabPromotionTransactionFile(
                operation=raw_file["operation"],
                path=raw_file["path"],
                before_exists=raw_file["before_exists"],
                before_bytes=raw_file["before_bytes"],
                before_sha256=raw_file["before_sha256"],
                before_mode=raw_file["before_mode"],
                after_bytes=raw_file["after_bytes"],
                after_sha256=raw_file["after_sha256"],
                after_mode=raw_file["after_mode"],
                after_content=raw_file["after_content"],
                source_kind=raw_file["source_kind"],
                source_id=(
                    raw_file["source_id"]
                ),
                progress=PROGRESS_PENDING,
            )
        )

    transaction = LabPromotionTransaction(
        component=record["transaction_component"],
        schema_version=record["transaction_schema_version"],
        transaction_id=record["transaction_id"],
        snapshot_id=record["prepared_snapshot_id"],
        proposal_id=record["proposal_id"],
        candidate_id=record["candidate_id"],
        run_id=record["run_id"],
        repository_path=record["repository_path"],
        repository_device=record["repository_device"],
        repository_inode=record["repository_inode"],
        branch=record["branch"],
        head=record["head"],
        risk_classification=record["risk_classification"],
        state=STATE_PREPARED,
        files=tuple(
            files
        ),
    )

    try:
        return validate_lab_promotion_transaction(
            transaction
        )
    except (
        TypeError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionTransactionJournalError(
            f"transaction plan failed validation: {exc}"
        ) from exc


def _decode_snapshot(
    raw: bytes,
) -> dict[str, Any]:
    record = _decode_canonical_object(
        raw,
        fields=_SNAPSHOT_FIELDS,
        name="transaction snapshot",
    )

    if record["component"] != JOURNAL_SNAPSHOT_COMPONENT:
        raise LabPromotionTransactionJournalError(
            "transaction snapshot component mismatch"
        )

    if record["schema_version"] != JOURNAL_SCHEMA_VERSION:
        raise LabPromotionTransactionJournalError(
            "transaction snapshot schema mismatch"
        )

    _require_identifier(
        "snapshot transaction_id",
        record["transaction_id"],
    )

    _require_identifier(
        "snapshot_id",
        record["snapshot_id"],
    )

    previous = record["previous_snapshot_id"]

    if previous is not None:
        _require_identifier(
            "previous_snapshot_id",
            previous,
        )

    sequence = record["sequence"]

    if (
        not isinstance(
            sequence,
            int,
        )
        or isinstance(
            sequence,
            bool,
        )
        or sequence < 0
    ):
        raise LabPromotionTransactionJournalError(
            "snapshot sequence must be a non-negative integer"
        )

    progress = record["file_progress"]

    if (
        not isinstance(
            progress,
            list,
        )
        or not progress
    ):
        raise LabPromotionTransactionJournalError(
            "snapshot file_progress must be a non-empty list"
        )

    seen: set[str] = set()

    for item in progress:
        if not isinstance(
            item,
            dict,
        ):
            raise LabPromotionTransactionJournalError(
                "snapshot progress entry must be an object"
            )

        if frozenset(
            item
        ) != _PROGRESS_FIELDS:
            raise LabPromotionTransactionJournalError(
                "snapshot progress fields do not exactly match schema"
            )

        path = item["path"]
        value = item["progress"]

        if not isinstance(
            path,
            str,
        ):
            raise LabPromotionTransactionJournalError(
                "snapshot progress path must be a string"
            )

        if path in seen:
            raise LabPromotionTransactionJournalError(
                "snapshot contains duplicate progress path"
            )

        seen.add(
            path
        )

        if value not in FILE_PROGRESS_STATES:
            raise LabPromotionTransactionJournalError(
                f"snapshot contains unknown progress {value!r}"
            )

    return record


def _snapshot_filename(
    sequence: int,
) -> str:
    if (
        not isinstance(
            sequence,
            int,
        )
        or isinstance(
            sequence,
            bool,
        )
        or sequence < 0
        or sequence >= MAX_SNAPSHOTS
    ):
        raise LabPromotionTransactionJournalError(
            "snapshot sequence is outside allowed range"
        )

    return f"{sequence:016d}.json"


def _parse_snapshot_names(
    names: frozenset[str],
) -> list[str]:
    if not names:
        raise LabPromotionTransactionJournalError(
            "transaction journal has no snapshots"
        )

    if len(
        names
    ) > MAX_SNAPSHOTS:
        raise LabPromotionTransactionJournalError(
            "transaction journal exceeds snapshot limit"
        )

    expected = [
        _snapshot_filename(
            index
        )
        for index in range(
            len(
                names
            )
        )
    ]

    if sorted(
        names
    ) != expected:
        raise LabPromotionTransactionJournalError(
            "snapshot sequence is non-contiguous or contains "
            "unexpected entries"
        )

    return expected


def _load_current_from_transaction_fd(
    transaction_fd: int,
    *,
    transaction_id: str,
) -> tuple[
    LabPromotionTransaction,
    int,
]:
    entries = _list_directory(
        transaction_fd,
        name=f"transaction {transaction_id}",
    )

    if entries != TRANSACTION_ENTRIES:
        raise LabPromotionTransactionJournalError(
            "transaction directory entries mismatch: "
            f"{sorted(entries)!r}"
        )

    raw_plan = _read_private_file(
        transaction_fd,
        filename=PLAN_FILENAME,
        max_bytes=MAX_PLAN_BYTES,
    )

    prepared = _decode_plan(
        raw_plan
    )

    if prepared.transaction_id != transaction_id:
        raise LabPromotionTransactionJournalError(
            "transaction directory identity mismatch"
        )

    snapshots_fd = _open_private_child_directory(
        transaction_fd,
        filename=SNAPSHOTS_DIRECTORY,
        name=f"transaction {transaction_id} snapshots",
    )

    try:
        names = _parse_snapshot_names(
            _list_directory(
                snapshots_fd,
                name=f"transaction {transaction_id} snapshots",
            )
        )

        current = prepared
        previous_snapshot_id: str | None = None

        for sequence, filename in enumerate(
            names
        ):
            raw_snapshot = _read_private_file(
                snapshots_fd,
                filename=filename,
                max_bytes=MAX_SNAPSHOT_BYTES,
            )

            snapshot = _decode_snapshot(
                raw_snapshot
            )

            if snapshot["transaction_id"] != transaction_id:
                raise LabPromotionTransactionJournalError(
                    "snapshot transaction binding mismatch"
                )

            if snapshot["sequence"] != sequence:
                raise LabPromotionTransactionJournalError(
                    "snapshot sequence binding mismatch"
                )

            if (
                snapshot["previous_snapshot_id"]
                != previous_snapshot_id
            ):
                raise LabPromotionTransactionJournalError(
                    "snapshot chain predecessor mismatch"
                )

            progress_items = snapshot[
                "file_progress"
            ]

            progress_by_path = {
                item["path"]: item["progress"]
                for item in progress_items
            }

            if sequence == 0:
                if snapshot["state"] != STATE_PREPARED:
                    raise LabPromotionTransactionJournalError(
                        "initial snapshot must be PREPARED"
                    )

                if (
                    snapshot["snapshot_id"]
                    != prepared.snapshot_id
                ):
                    raise LabPromotionTransactionJournalError(
                        "initial snapshot identity mismatch"
                    )

                if progress_by_path != {
                    item.path: PROGRESS_PENDING
                    for item in prepared.files
                }:
                    raise LabPromotionTransactionJournalError(
                        "initial snapshot progress mismatch"
                    )

            else:
                try:
                    successor = transition_lab_promotion_transaction(
                        current,
                        state=snapshot["state"],
                        progress_by_path=progress_by_path,
                    )
                except (
                    TypeError,
                    LabPromotionTransactionStateError,
                ) as exc:
                    raise LabPromotionTransactionJournalError(
                        f"illegal persisted transaction transition: {exc}"
                    ) from exc

                if (
                    successor.snapshot_id
                    != snapshot["snapshot_id"]
                ):
                    raise LabPromotionTransactionJournalError(
                        "persisted snapshot identity mismatch"
                    )

                current = successor

            previous_snapshot_id = snapshot[
                "snapshot_id"
            ]

        return (
            current,
            len(
                names
            ),
        )

    finally:
        os.close(
            snapshots_fd
        )


def _open_transaction_directory(
    transactions_fd: int,
    transaction_id: str,
) -> int:
    _require_identifier(
        "transaction_id",
        transaction_id,
    )

    return _open_private_child_directory(
        transactions_fd,
        filename=transaction_id,
        name=f"transaction {transaction_id}",
    )


def create_promotion_transaction_journal(
    root: str | os.PathLike[str],
    *,
    transaction: LabPromotionTransaction,
) -> LabPromotionTransaction:
    try:
        transaction = validate_lab_promotion_transaction(
            transaction
        )
    except (
        TypeError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionTransactionJournalError(
            f"transaction failed validation: {exc}"
        ) from exc

    if transaction.state != STATE_PREPARED:
        raise LabPromotionTransactionJournalError(
            "new transaction journal requires PREPARED transaction"
        )

    if any(
        item.progress != PROGRESS_PENDING
        for item in transaction.files
    ):
        raise LabPromotionTransactionJournalError(
            "new transaction journal requires all files PENDING"
        )

    transaction_id = transaction.transaction_id

    with _journal_lock(
        root,
        exclusive=True,
    ) as root_fd:
        transactions_fd = _open_transactions_directory(
            root_fd
        )

        try:
            transaction_fd = _create_private_directory(
                transactions_fd,
                filename=transaction_id,
            )

            try:
                snapshots_fd = _create_private_directory(
                    transaction_fd,
                    filename=SNAPSHOTS_DIRECTORY,
                )

                try:
                    plan_bytes = _canonical_json_bytes(
                        _plan_object(
                            transaction
                        )
                    )

                    initial_snapshot_bytes = _canonical_json_bytes(
                        _snapshot_object(
                            transaction,
                            sequence=0,
                            previous_snapshot_id=None,
                        )
                    )

                    _create_private_file(
                        transaction_fd,
                        filename=PLAN_FILENAME,
                        data=plan_bytes,
                        max_bytes=MAX_PLAN_BYTES,
                    )

                    _create_private_file(
                        snapshots_fd,
                        filename=_snapshot_filename(
                            0
                        ),
                        data=initial_snapshot_bytes,
                        max_bytes=MAX_SNAPSHOT_BYTES,
                    )

                    os.fsync(
                        snapshots_fd
                    )

                    os.fsync(
                        transaction_fd
                    )

                    os.fsync(
                        transactions_fd
                    )

                    loaded, count = (
                        _load_current_from_transaction_fd(
                            transaction_fd,
                            transaction_id=transaction_id,
                        )
                    )

                    if count != 1:
                        raise LabPromotionTransactionJournalError(
                            "new journal did not persist exactly one snapshot"
                        )

                    if loaded != transaction:
                        raise LabPromotionTransactionJournalError(
                            "new journal does not match exact transaction"
                        )

                    return loaded

                finally:
                    os.close(
                        snapshots_fd
                    )

            finally:
                os.close(
                    transaction_fd
                )

        finally:
            os.close(
                transactions_fd
            )


def load_promotion_transaction_journal(
    root: str | os.PathLike[str],
    *,
    transaction_id: str,
) -> LabPromotionTransaction:
    transaction_id = _require_identifier(
        "transaction_id",
        transaction_id,
    )

    with _journal_lock(
        root,
        exclusive=False,
    ) as root_fd:
        transactions_fd = _open_transactions_directory(
            root_fd
        )

        try:
            transaction_fd = _open_transaction_directory(
                transactions_fd,
                transaction_id,
            )

            try:
                current, _ = _load_current_from_transaction_fd(
                    transaction_fd,
                    transaction_id=transaction_id,
                )

                return current

            finally:
                os.close(
                    transaction_fd
                )

        finally:
            os.close(
                transactions_fd
            )


def append_promotion_transaction_snapshot(
    root: str | os.PathLike[str],
    *,
    successor: LabPromotionTransaction,
    expected_previous_snapshot_id: str,
) -> LabPromotionTransaction:
    expected_previous_snapshot_id = _require_identifier(
        "expected_previous_snapshot_id",
        expected_previous_snapshot_id,
    )

    try:
        successor = validate_lab_promotion_transaction(
            successor
        )
    except (
        TypeError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionTransactionJournalError(
            f"successor failed validation: {exc}"
        ) from exc

    transaction_id = successor.transaction_id

    with _journal_lock(
        root,
        exclusive=True,
    ) as root_fd:
        transactions_fd = _open_transactions_directory(
            root_fd
        )

        try:
            transaction_fd = _open_transaction_directory(
                transactions_fd,
                transaction_id,
            )

            try:
                current, count = _load_current_from_transaction_fd(
                    transaction_fd,
                    transaction_id=transaction_id,
                )

                if (
                    current.snapshot_id
                    != expected_previous_snapshot_id
                ):
                    raise LabPromotionTransactionJournalError(
                        "expected previous snapshot is stale"
                    )

                if successor.snapshot_id == current.snapshot_id:
                    raise LabPromotionTransactionJournalError(
                        "refusing no-op journal append"
                    )

                progress_by_path = {
                    item.path: item.progress
                    for item in successor.files
                }

                try:
                    expected_successor = (
                        transition_lab_promotion_transaction(
                            current,
                            state=successor.state,
                            progress_by_path=progress_by_path,
                        )
                    )
                except (
                    TypeError,
                    LabPromotionTransactionStateError,
                ) as exc:
                    raise LabPromotionTransactionJournalError(
                        f"illegal successor transition: {exc}"
                    ) from exc

                if expected_successor != successor:
                    raise LabPromotionTransactionJournalError(
                        "successor does not exactly match legal transition"
                    )

                snapshots_fd = _open_private_child_directory(
                    transaction_fd,
                    filename=SNAPSHOTS_DIRECTORY,
                    name=f"transaction {transaction_id} snapshots",
                )

                try:
                    snapshot_bytes = _canonical_json_bytes(
                        _snapshot_object(
                            successor,
                            sequence=count,
                            previous_snapshot_id=(
                                current.snapshot_id
                            ),
                        )
                    )

                    _create_private_file(
                        snapshots_fd,
                        filename=_snapshot_filename(
                            count
                        ),
                        data=snapshot_bytes,
                        max_bytes=MAX_SNAPSHOT_BYTES,
                    )

                    os.fsync(
                        snapshots_fd
                    )

                    os.fsync(
                        transaction_fd
                    )

                    os.fsync(
                        transactions_fd
                    )

                finally:
                    os.close(
                        snapshots_fd
                    )

                loaded, new_count = (
                    _load_current_from_transaction_fd(
                        transaction_fd,
                        transaction_id=transaction_id,
                    )
                )

                if new_count != count + 1:
                    raise LabPromotionTransactionJournalError(
                        "journal append count mismatch"
                    )

                if loaded != successor:
                    raise LabPromotionTransactionJournalError(
                        "persisted successor mismatch"
                    )

                return loaded

            finally:
                os.close(
                    transaction_fd
                )

        finally:
            os.close(
                transactions_fd
            )
