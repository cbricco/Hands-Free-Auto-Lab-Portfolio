"""Durable append-only evidence journal for coding-job lifecycle snapshots.

Authority is deliberately limited to an explicitly supplied private journal
state root.

This module does not:

- execute workers or models,
- retry or resume coding jobs,
- create or consume approval,
- promote candidates,
- modify repositories,
- execute Git or shell commands,
- stage, commit, or push,
- load coding candidates.

The pure lifecycle model remains authoritative for legal state transitions.
This module only persists and reloads those lifecycle snapshots.

Every append:

- validates the complete existing chain,
- requires the caller's exact expected previous snapshot,
- independently reconstructs the supplied successor through the pure
  lifecycle transition function,
- creates exactly the next snapshot record with O_EXCL,
- fsyncs the record and containing directories,
- reloads the complete chain before reporting success.

Malformed, partial, unsafe, or non-canonical state is never repaired or
removed automatically. It fails closed and remains available for inspection.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator

from .lab_coding_job_lifecycle import (
    STATE_REQUESTED,
    LabCodingJobLifecycle,
    LabCodingJobLifecycleError,
    build_lab_coding_job_lifecycle,
    transition_lab_coding_job_lifecycle,
    validate_lab_coding_job_lifecycle,
)


CODING_JOB_LIFECYCLE_JOURNAL_COMPONENT = (
    "hands-free-auto-lab-coding-job-lifecycle-journal-v1"
)
CODING_JOB_LIFECYCLE_JOURNAL_SCHEMA_VERSION = 1

LOCK_FILENAME = "coding-job-lifecycles.lock"
JOBS_DIRECTORY = "jobs"
SNAPSHOTS_DIRECTORY = "snapshots"

ROOT_ENTRIES = frozenset(
    {
        LOCK_FILENAME,
        JOBS_DIRECTORY,
    }
)

JOB_ENTRIES = frozenset(
    {
        SNAPSHOTS_DIRECTORY,
    }
)

MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_SNAPSHOTS = 100_000

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)

_SNAPSHOT_RECORD_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "job_id",
        "sequence",
        "previous_snapshot_id",
        "lifecycle",
    }
)

_LIFECYCLE_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "job_id",
        "snapshot_id",
        "request_id",
        "run_id",
        "state",
        "session_id",
        "worker_request_id",
        "worker_result_id",
        "candidate_id",
        "integration_result_id",
    }
)


class LabCodingJobLifecycleJournalError(
    RuntimeError
):
    """Durable coding-job lifecycle journal state is unsafe."""


def _error(
    message: str,
    exc: BaseException | None = None,
) -> LabCodingJobLifecycleJournalError:
    error = LabCodingJobLifecycleJournalError(
        message
    )

    if exc is not None:
        error.__cause__ = exc

    return error


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
        "utf-8",
        errors="strict",
    )


def _require_identifier(
    name: str,
    value: object,
) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _require_optional_identifier(
    name: str,
    value: object,
) -> str | None:
    if value is None:
        return None

    return _require_identifier(
        name,
        value,
    )


def _require_root_path(
    root: str | os.PathLike[str],
) -> Path:
    try:
        text = os.fspath(
            root
        )
    except (
        TypeError,
        ValueError,
    ) as exc:
        raise _error(
            "coding-job lifecycle journal root must be a filesystem path",
            exc,
        )

    if type(text) is bytes:
        raise LabCodingJobLifecycleJournalError(
            "coding-job lifecycle journal root must be a text path"
        )

    if not os.path.isabs(
        text
    ):
        raise LabCodingJobLifecycleJournalError(
            "coding-job lifecycle journal root must be absolute"
        )

    if os.path.normpath(
        text
    ) != text:
        raise LabCodingJobLifecycleJournalError(
            "coding-job lifecycle journal root must be normalized"
        )

    if os.path.realpath(
        text
    ) != text:
        raise LabCodingJobLifecycleJournalError(
            "coding-job lifecycle journal root must be canonical and "
            "contain no symlink components"
        )

    return Path(
        text
    )


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

    if not hasattr(
        fcntl,
        "flock",
    ):
        missing.append(
            "fcntl.flock"
        )

    for label, function in (
        (
            "os.open(dir_fd=...)",
            os.open,
        ),
        (
            "os.mkdir(dir_fd=...)",
            os.mkdir,
        ),
        (
            "os.stat(dir_fd=...)",
            os.stat,
        ),
    ):
        if function not in os.supports_dir_fd:
            missing.append(
                label
            )

    if os.stat not in os.supports_follow_symlinks:
        missing.append(
            "os.stat(follow_symlinks=False)"
        )

    if os.listdir not in os.supports_fd:
        missing.append(
            "os.listdir(fd)"
        )

    if missing:
        raise LabCodingJobLifecycleJournalError(
            "secure coding-job lifecycle journal primitives unavailable: "
            + ", ".join(
                sorted(
                    missing
                )
            )
        )


def _flags(
    *values: int,
) -> int:
    result = 0

    for value in values:
        result |= value

    if hasattr(
        os,
        "O_CLOEXEC",
    ):
        result |= os.O_CLOEXEC

    return result


def _directory_flags() -> int:
    _require_secure_platform()

    return _flags(
        os.O_RDONLY,
        os.O_DIRECTORY,
        os.O_NOFOLLOW,
    )


def _read_flags() -> int:
    _require_secure_platform()

    return _flags(
        os.O_RDONLY,
        os.O_NOFOLLOW,
    )


def _create_flags() -> int:
    _require_secure_platform()

    return _flags(
        os.O_WRONLY,
        os.O_CREAT,
        os.O_EXCL,
        os.O_NOFOLLOW,
    )


def _validate_directory(
    st: os.stat_result,
    *,
    name: str,
) -> None:
    if not stat.S_ISDIR(
        st.st_mode
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{name} must be a real directory"
        )

    if stat.S_IMODE(
        st.st_mode
    ) != 0o700:
        raise LabCodingJobLifecycleJournalError(
            f"{name} must have mode 0700"
        )

    if st.st_uid != os.geteuid():
        raise LabCodingJobLifecycleJournalError(
            f"{name} must be owned by the effective user"
        )


def _validate_file(
    st: os.stat_result,
    *,
    name: str,
) -> None:
    if not stat.S_ISREG(
        st.st_mode
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{name} must be a regular file"
        )

    if stat.S_IMODE(
        st.st_mode
    ) != 0o600:
        raise LabCodingJobLifecycleJournalError(
            f"{name} must have mode 0600"
        )

    if st.st_uid != os.geteuid():
        raise LabCodingJobLifecycleJournalError(
            f"{name} must be owned by the effective user"
        )

    if st.st_nlink != 1:
        raise LabCodingJobLifecycleJournalError(
            f"{name} must have exactly one hard link"
        )


def _same_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return (
        left.st_dev,
        left.st_ino,
    ) == (
        right.st_dev,
        right.st_ino,
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
        raise _error(
            f"cannot securely open {name}: {exc}",
            exc,
        )

    try:
        _validate_directory(
            os.fstat(
                fd
            ),
            name=name,
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
        raise _error(
            f"cannot securely open {name}: {exc}",
            exc,
        )

    try:
        _validate_directory(
            os.fstat(
                fd
            ),
            name=name,
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
        raise _error(
            f"refusing existing directory {filename}",
            exc,
        )
    except OSError as exc:
        raise _error(
            f"cannot create directory {filename}: {exc}",
            exc,
        )

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

        _validate_directory(
            os.fstat(
                fd
            ),
            name=filename,
        )

        return fd

    except Exception:
        if fd is not None:
            os.close(
                fd
            )

        raise


def _open_private_file(
    directory_fd: int,
    *,
    filename: str,
) -> int:
    try:
        fd = os.open(
            filename,
            _read_flags(),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise _error(
            f"cannot securely open {filename}: {exc}",
            exc,
        )

    try:
        _validate_file(
            os.fstat(
                fd
            ),
            name=filename,
        )
    except Exception:
        os.close(
            fd
        )
        raise

    return fd


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
            raise _error(
                f"coding-job lifecycle journal write failed: {exc}",
                exc,
            )

        if count <= 0:
            raise LabCodingJobLifecycleJournalError(
                "short write while persisting coding-job lifecycle journal"
            )

        view = view[
            count:
        ]


def _create_private_file(
    directory_fd: int,
    *,
    filename: str,
    data: bytes,
) -> None:
    if (
        not data
        or len(data) > MAX_SNAPSHOT_BYTES
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{filename} has invalid record size"
        )

    try:
        fd = os.open(
            filename,
            _create_flags(),
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise _error(
            f"cannot create {filename}: {exc}",
            exc,
        )

    try:
        os.fchmod(
            fd,
            0o600,
        )

        _write_all(
            fd,
            data,
        )

        _validate_file(
            os.fstat(
                fd
            ),
            name=filename,
        )

        os.fsync(
            fd
        )

    except LabCodingJobLifecycleJournalError:
        raise

    except OSError as exc:
        raise _error(
            f"cannot persist {filename}: {exc}",
            exc,
        )

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
            _create_flags(),
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise _error(
            f"cannot create {filename}: {exc}",
            exc,
        )

    try:
        os.fchmod(
            fd,
            0o600,
        )

        _validate_file(
            os.fstat(
                fd
            ),
            name=filename,
        )

        os.fsync(
            fd
        )

    finally:
        os.close(
            fd
        )


def _read_bounded(
    fd: int,
    *,
    filename: str,
) -> bytes:
    before = os.fstat(
        fd
    )

    _validate_file(
        before,
        name=filename,
    )

    if (
        before.st_size <= 0
        or before.st_size > MAX_SNAPSHOT_BYTES
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{filename} has invalid record size"
        )

    chunks: list[bytes] = []
    remaining = MAX_SNAPSHOT_BYTES + 1

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
            raise _error(
                f"cannot read {filename}: {exc}",
                exc,
            )

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

    after = os.fstat(
        fd
    )

    _validate_file(
        after,
        name=filename,
    )

    if (
        not _same_identity(
            before,
            after,
        )
        or before.st_size != after.st_size
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{filename} changed while being read"
        )

    if (
        len(data) != before.st_size
        or len(data) > MAX_SNAPSHOT_BYTES
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{filename} size evidence does not match read"
        )

    return data


def _entries(
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
        raise _error(
            f"cannot list {name}: {exc}",
            exc,
        )


def _snapshot_filename(
    sequence: int,
) -> str:
    if (
        type(sequence) is not int
        or sequence < 0
        or sequence >= MAX_SNAPSHOTS
    ):
        raise LabCodingJobLifecycleJournalError(
            "snapshot sequence is outside supported range"
        )

    return f"{sequence:016d}.json"


def _snapshot_sequence(
    filename: str,
) -> int | None:
    if (
        len(filename) != 21
        or not filename.endswith(
            ".json"
        )
    ):
        return None

    digits = filename[
        :-5
    ]

    if (
        len(digits) != 16
        or not digits.isascii()
        or not digits.isdigit()
    ):
        return None

    sequence = int(
        digits,
        10,
    )

    if sequence >= MAX_SNAPSHOTS:
        return None

    return sequence


def _lifecycle_to_wire(
    lifecycle: object,
) -> dict[str, object]:
    try:
        trusted = validate_lab_coding_job_lifecycle(
            lifecycle
        )
    except LabCodingJobLifecycleError as exc:
        raise _error(
            f"invalid coding-job lifecycle: {exc}",
            exc,
        )

    return {
        "component": trusted.component,
        "schema_version": trusted.schema_version,
        "job_id": trusted.job_id,
        "snapshot_id": trusted.snapshot_id,
        "request_id": trusted.request_id,
        "run_id": trusted.run_id,
        "state": trusted.state,
        "session_id": trusted.session_id,
        "worker_request_id": trusted.worker_request_id,
        "worker_result_id": trusted.worker_result_id,
        "candidate_id": trusted.candidate_id,
        "integration_result_id": trusted.integration_result_id,
    }


def _lifecycle_from_wire(
    value: object,
) -> LabCodingJobLifecycle:
    if type(value) is not dict:
        raise LabCodingJobLifecycleJournalError(
            "lifecycle record must be a JSON object"
        )

    if frozenset(
        value
    ) != _LIFECYCLE_FIELDS:
        raise LabCodingJobLifecycleJournalError(
            "lifecycle record fields do not exactly match schema"
        )

    try:
        lifecycle = LabCodingJobLifecycle(
            component=value[
                "component"
            ],
            schema_version=value[
                "schema_version"
            ],
            job_id=value[
                "job_id"
            ],
            snapshot_id=value[
                "snapshot_id"
            ],
            request_id=value[
                "request_id"
            ],
            run_id=value[
                "run_id"
            ],
            state=value[
                "state"
            ],
            session_id=value[
                "session_id"
            ],
            worker_request_id=value[
                "worker_request_id"
            ],
            worker_result_id=value[
                "worker_result_id"
            ],
            candidate_id=value[
                "candidate_id"
            ],
            integration_result_id=value[
                "integration_result_id"
            ],
        )

        return validate_lab_coding_job_lifecycle(
            lifecycle
        )

    except (
        LabCodingJobLifecycleError,
        TypeError,
    ) as exc:
        raise _error(
            f"lifecycle record failed validation: {exc}",
            exc,
        )


def _snapshot_record(
    lifecycle: object,
    *,
    sequence: int,
    previous_snapshot_id: object,
) -> dict[str, object]:
    wire = _lifecycle_to_wire(
        lifecycle
    )

    if (
        type(sequence) is not int
        or sequence < 0
        or sequence >= MAX_SNAPSHOTS
    ):
        raise LabCodingJobLifecycleJournalError(
            "snapshot sequence is outside supported range"
        )

    previous = _require_optional_identifier(
        "previous_snapshot_id",
        previous_snapshot_id,
    )

    return {
        "component": CODING_JOB_LIFECYCLE_JOURNAL_COMPONENT,
        "schema_version": CODING_JOB_LIFECYCLE_JOURNAL_SCHEMA_VERSION,
        "job_id": wire[
            "job_id"
        ],
        "sequence": sequence,
        "previous_snapshot_id": previous,
        "lifecycle": wire,
    }


def _encode_snapshot(
    lifecycle: object,
    *,
    sequence: int,
    previous_snapshot_id: object,
) -> bytes:
    return _canonical_json_bytes(
        _snapshot_record(
            lifecycle,
            sequence=sequence,
            previous_snapshot_id=previous_snapshot_id,
        )
    )


def _decode_snapshot(
    raw: bytes,
    *,
    filename: str,
) -> tuple[
    int,
    str | None,
    LabCodingJobLifecycle,
]:
    try:
        text = raw.decode(
            "utf-8",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise _error(
            f"{filename} is not UTF-8",
            exc,
        )

    try:
        value = json.loads(
            text
        )
    except json.JSONDecodeError as exc:
        raise _error(
            f"{filename} is not valid JSON",
            exc,
        )

    if type(value) is not dict:
        raise LabCodingJobLifecycleJournalError(
            f"{filename} must contain a JSON object"
        )

    if frozenset(
        value
    ) != _SNAPSHOT_RECORD_FIELDS:
        raise LabCodingJobLifecycleJournalError(
            f"{filename} fields do not exactly match schema"
        )

    if _canonical_json_bytes(
        value
    ) != raw:
        raise LabCodingJobLifecycleJournalError(
            f"{filename} is not canonical JSON"
        )

    if (
        value[
            "component"
        ]
        != CODING_JOB_LIFECYCLE_JOURNAL_COMPONENT
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{filename} journal component mismatch"
        )

    if (
        type(
            value[
                "schema_version"
            ]
        )
        is not int
        or value[
            "schema_version"
        ]
        != CODING_JOB_LIFECYCLE_JOURNAL_SCHEMA_VERSION
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{filename} unsupported journal schema version"
        )

    job_id = _require_identifier(
        "job_id",
        value[
            "job_id"
        ],
    )

    sequence = value[
        "sequence"
    ]

    if (
        type(sequence) is not int
        or sequence < 0
        or sequence >= MAX_SNAPSHOTS
    ):
        raise LabCodingJobLifecycleJournalError(
            f"{filename} has invalid sequence"
        )

    previous_snapshot_id = _require_optional_identifier(
        "previous_snapshot_id",
        value[
            "previous_snapshot_id"
        ],
    )

    lifecycle = _lifecycle_from_wire(
        value[
            "lifecycle"
        ]
    )

    if lifecycle.job_id != job_id:
        raise LabCodingJobLifecycleJournalError(
            f"{filename} job_id does not match lifecycle job_id"
        )

    return (
        sequence,
        previous_snapshot_id,
        lifecycle,
    )


def _open_lock_file(
    root_fd: int,
) -> int:
    return _open_private_file(
        root_fd,
        filename=LOCK_FILENAME,
    )


def _open_jobs_directory(
    root_fd: int,
) -> int:
    return _open_private_child_directory(
        root_fd,
        filename=JOBS_DIRECTORY,
        name=JOBS_DIRECTORY,
    )


def _open_job_directory(
    jobs_fd: int,
    *,
    job_id: object,
) -> int:
    trusted_job_id = _require_identifier(
        "job_id",
        job_id,
    )

    return _open_private_child_directory(
        jobs_fd,
        filename=trusted_job_id,
        name=f"coding job {trusted_job_id}",
    )


def _open_snapshots_directory(
    job_fd: int,
) -> int:
    return _open_private_child_directory(
        job_fd,
        filename=SNAPSHOTS_DIRECTORY,
        name=SNAPSHOTS_DIRECTORY,
    )


def _validate_initialized_root_fd(
    root_fd: int,
) -> None:
    root_entries = _entries(
        root_fd,
        name="coding-job lifecycle journal root",
    )

    if root_entries != ROOT_ENTRIES:
        raise LabCodingJobLifecycleJournalError(
            "coding-job lifecycle journal root entries mismatch: "
            f"{sorted(root_entries)!r}"
        )

    lock_fd = _open_lock_file(
        root_fd
    )

    os.close(
        lock_fd
    )

    jobs_fd = _open_jobs_directory(
        root_fd
    )

    try:
        for job_id in _entries(
            jobs_fd,
            name="jobs directory",
        ):
            _require_identifier(
                "job directory name",
                job_id,
            )

            job_fd = _open_private_child_directory(
                jobs_fd,
                filename=job_id,
                name=f"coding job {job_id}",
            )

            try:
                job_entries = _entries(
                    job_fd,
                    name=f"coding job {job_id}",
                )

                if job_entries != JOB_ENTRIES:
                    raise LabCodingJobLifecycleJournalError(
                        f"coding job {job_id} entries mismatch: "
                        f"{sorted(job_entries)!r}"
                    )

                snapshots_fd = _open_snapshots_directory(
                    job_fd
                )

                try:
                    for filename in _entries(
                        snapshots_fd,
                        name=f"coding job {job_id} snapshots",
                    ):
                        if _snapshot_sequence(
                            filename
                        ) is None:
                            raise LabCodingJobLifecycleJournalError(
                                "unexpected coding-job lifecycle snapshot "
                                f"entry: {filename}"
                            )

                        snapshot_fd = _open_private_file(
                            snapshots_fd,
                            filename=filename,
                        )

                        os.close(
                            snapshot_fd
                        )

                finally:
                    os.close(
                        snapshots_fd
                    )

            finally:
                os.close(
                    job_fd
                )

    finally:
        os.close(
            jobs_fd
        )


def initialize_lab_coding_job_lifecycle_journal_root(
    root: str | os.PathLike[str],
) -> Path:
    """Initialize an empty private root or strictly validate an existing one."""

    root_path = _require_root_path(
        root
    )

    root_fd = _open_private_directory_path(
        root_path,
        name="coding-job lifecycle journal root",
    )

    try:
        entries = _entries(
            root_fd,
            name="coding-job lifecycle journal root",
        )

        if not entries:
            _create_empty_private_file(
                root_fd,
                filename=LOCK_FILENAME,
            )

            jobs_fd = _create_private_directory(
                root_fd,
                filename=JOBS_DIRECTORY,
            )

            try:
                os.fsync(
                    jobs_fd
                )
            finally:
                os.close(
                    jobs_fd
                )

            os.fsync(
                root_fd
            )

        _validate_initialized_root_fd(
            root_fd
        )

    except LabCodingJobLifecycleJournalError:
        raise

    except OSError as exc:
        raise _error(
            "coding-job lifecycle journal initialization failed; "
            f"partial state may remain for inspection: {exc}",
            exc,
        )

    finally:
        os.close(
            root_fd
        )

    return root_path


@contextmanager
def _locked_root(
    root: str | os.PathLike[str],
    *,
    exclusive: bool,
) -> Iterator[int]:
    root_path = _require_root_path(
        root
    )

    root_fd = _open_private_directory_path(
        root_path,
        name="coding-job lifecycle journal root",
    )

    lock_fd: int | None = None

    try:
        _validate_initialized_root_fd(
            root_fd
        )

        lock_fd = _open_lock_file(
            root_fd
        )

        try:
            fcntl.flock(
                lock_fd,
                (
                    fcntl.LOCK_EX
                    if exclusive
                    else fcntl.LOCK_SH
                ),
            )
        except OSError as exc:
            raise _error(
                f"cannot acquire {LOCK_FILENAME}: {exc}",
                exc,
            )

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


def _reconstruct_successor(
    previous: LabCodingJobLifecycle,
    successor: LabCodingJobLifecycle,
) -> LabCodingJobLifecycle:
    try:
        reconstructed = transition_lab_coding_job_lifecycle(
            previous,
            state=successor.state,
            session_id=successor.session_id,
            worker_request_id=successor.worker_request_id,
            worker_result_id=successor.worker_result_id,
            candidate_id=successor.candidate_id,
            integration_result_id=successor.integration_result_id,
        )
    except LabCodingJobLifecycleError as exc:
        raise _error(
            f"illegal durable lifecycle transition: {exc}",
            exc,
        )

    if reconstructed != successor:
        raise LabCodingJobLifecycleJournalError(
            "successor does not equal independently reconstructed "
            "coding-job lifecycle transition"
        )

    return reconstructed


def _load_chain_from_jobs_fd(
    jobs_fd: int,
    *,
    job_id: object,
) -> tuple[
    LabCodingJobLifecycle,
    int,
]:
    trusted_job_id = _require_identifier(
        "job_id",
        job_id,
    )

    job_fd = _open_job_directory(
        jobs_fd,
        job_id=trusted_job_id,
    )

    try:
        job_entries = _entries(
            job_fd,
            name=f"coding job {trusted_job_id}",
        )

        if job_entries != JOB_ENTRIES:
            raise LabCodingJobLifecycleJournalError(
                f"coding job {trusted_job_id} entries mismatch: "
                f"{sorted(job_entries)!r}"
            )

        snapshots_fd = _open_snapshots_directory(
            job_fd
        )

        try:
            names = _entries(
                snapshots_fd,
                name=f"coding job {trusted_job_id} snapshots",
            )

            if not names:
                raise LabCodingJobLifecycleJournalError(
                    "coding-job lifecycle journal has no snapshots"
                )

            if len(
                names
            ) > MAX_SNAPSHOTS:
                raise LabCodingJobLifecycleJournalError(
                    "coding-job lifecycle journal exceeds snapshot limit"
                )

            sequences: list[int] = []

            for filename in names:
                sequence = _snapshot_sequence(
                    filename
                )

                if sequence is None:
                    raise LabCodingJobLifecycleJournalError(
                        "unexpected coding-job lifecycle snapshot "
                        f"entry: {filename}"
                    )

                sequences.append(
                    sequence
                )

            sequences.sort()

            expected_sequences = list(
                range(
                    len(
                        sequences
                    )
                )
            )

            if sequences != expected_sequences:
                raise LabCodingJobLifecycleJournalError(
                    "coding-job lifecycle snapshot sequence is non-contiguous"
                )

            previous: LabCodingJobLifecycle | None = None

            for sequence in sequences:
                filename = _snapshot_filename(
                    sequence
                )

                snapshot_fd = _open_private_file(
                    snapshots_fd,
                    filename=filename,
                )

                try:
                    raw = _read_bounded(
                        snapshot_fd,
                        filename=filename,
                    )
                finally:
                    os.close(
                        snapshot_fd
                    )

                (
                    record_sequence,
                    previous_snapshot_id,
                    lifecycle,
                ) = _decode_snapshot(
                    raw,
                    filename=filename,
                )

                if record_sequence != sequence:
                    raise LabCodingJobLifecycleJournalError(
                        f"{filename} sequence does not match filename"
                    )

                if lifecycle.job_id != trusted_job_id:
                    raise LabCodingJobLifecycleJournalError(
                        f"{filename} lifecycle job_id mismatch"
                    )

                if previous is None:
                    if sequence != 0:
                        raise LabCodingJobLifecycleJournalError(
                            "initial coding-job lifecycle sequence must be zero"
                        )

                    if previous_snapshot_id is not None:
                        raise LabCodingJobLifecycleJournalError(
                            "initial coding-job lifecycle snapshot cannot "
                            "have a predecessor"
                        )

                    if lifecycle.state != STATE_REQUESTED:
                        raise LabCodingJobLifecycleJournalError(
                            "initial coding-job lifecycle snapshot must "
                            "be REQUESTED"
                        )

                    try:
                        expected_initial = build_lab_coding_job_lifecycle(
                            request_id=lifecycle.request_id,
                            run_id=lifecycle.run_id,
                        )
                    except LabCodingJobLifecycleError as exc:
                        raise _error(
                            f"initial lifecycle failed reconstruction: {exc}",
                            exc,
                        )

                    if lifecycle != expected_initial:
                        raise LabCodingJobLifecycleJournalError(
                            "initial coding-job lifecycle snapshot does not "
                            "equal independently reconstructed REQUESTED state"
                        )

                else:
                    if previous_snapshot_id != previous.snapshot_id:
                        raise LabCodingJobLifecycleJournalError(
                            "coding-job lifecycle predecessor identity mismatch"
                        )

                    if lifecycle.job_id != previous.job_id:
                        raise LabCodingJobLifecycleJournalError(
                            "coding-job lifecycle job identity changed"
                        )

                    if lifecycle.request_id != previous.request_id:
                        raise LabCodingJobLifecycleJournalError(
                            "coding-job lifecycle request identity changed"
                        )

                    if lifecycle.run_id != previous.run_id:
                        raise LabCodingJobLifecycleJournalError(
                            "coding-job lifecycle run identity changed"
                        )

                    _reconstruct_successor(
                        previous,
                        lifecycle,
                    )

                previous = lifecycle

            if previous is None:
                raise LabCodingJobLifecycleJournalError(
                    "coding-job lifecycle journal has no usable snapshots"
                )

            return (
                previous,
                len(
                    sequences
                ),
            )

        finally:
            os.close(
                snapshots_fd
            )

    finally:
        os.close(
            job_fd
        )


def create_lab_coding_job_lifecycle_journal(
    root: str | os.PathLike[str],
    *,
    lifecycle: object,
) -> LabCodingJobLifecycle:
    """Persist exactly one new REQUESTED lifecycle as sequence zero."""

    try:
        trusted = validate_lab_coding_job_lifecycle(
            lifecycle
        )
    except LabCodingJobLifecycleError as exc:
        raise _error(
            f"invalid initial coding-job lifecycle: {exc}",
            exc,
        )

    if trusted.state != STATE_REQUESTED:
        raise LabCodingJobLifecycleJournalError(
            "new coding-job lifecycle journal must start at REQUESTED"
        )

    try:
        reconstructed = build_lab_coding_job_lifecycle(
            request_id=trusted.request_id,
            run_id=trusted.run_id,
        )
    except LabCodingJobLifecycleError as exc:
        raise _error(
            f"initial coding-job lifecycle reconstruction failed: {exc}",
            exc,
        )

    if reconstructed != trusted:
        raise LabCodingJobLifecycleJournalError(
            "initial lifecycle does not equal independently reconstructed "
            "REQUESTED state"
        )

    data = _encode_snapshot(
        trusted,
        sequence=0,
        previous_snapshot_id=None,
    )

    with _locked_root(
        root,
        exclusive=True,
    ) as root_fd:
        jobs_fd = _open_jobs_directory(
            root_fd
        )

        try:
            existing_jobs = _entries(
                jobs_fd,
                name="jobs directory",
            )

            if trusted.job_id in existing_jobs:
                raise LabCodingJobLifecycleJournalError(
                    f"coding-job lifecycle journal already exists: "
                    f"{trusted.job_id}"
                )

            job_fd = _create_private_directory(
                jobs_fd,
                filename=trusted.job_id,
            )

            try:
                snapshots_fd = _create_private_directory(
                    job_fd,
                    filename=SNAPSHOTS_DIRECTORY,
                )

                try:
                    os.fsync(
                        snapshots_fd
                    )
                    os.fsync(
                        job_fd
                    )
                    os.fsync(
                        jobs_fd
                    )

                    _create_private_file(
                        snapshots_fd,
                        filename=_snapshot_filename(
                            0
                        ),
                        data=data,
                    )

                    os.fsync(
                        snapshots_fd
                    )
                    os.fsync(
                        job_fd
                    )
                    os.fsync(
                        jobs_fd
                    )
                    os.fsync(
                        root_fd
                    )

                finally:
                    os.close(
                        snapshots_fd
                    )

            finally:
                os.close(
                    job_fd
                )

            reloaded, count = _load_chain_from_jobs_fd(
                jobs_fd,
                job_id=trusted.job_id,
            )

            if (
                count != 1
                or reloaded != trusted
            ):
                raise LabCodingJobLifecycleJournalError(
                    "new coding-job lifecycle journal did not reload exactly"
                )

        except LabCodingJobLifecycleJournalError:
            raise

        except OSError as exc:
            raise _error(
                "coding-job lifecycle journal creation failed; partial "
                f"state may remain for inspection: {exc}",
                exc,
            )

        finally:
            os.close(
                jobs_fd
            )

    return trusted


def append_lab_coding_job_lifecycle_snapshot(
    root: str | os.PathLike[str],
    *,
    successor: object,
    expected_previous_snapshot_id: object,
) -> LabCodingJobLifecycle:
    """Append one exact legal successor to an existing lifecycle journal."""

    try:
        trusted_successor = validate_lab_coding_job_lifecycle(
            successor
        )
    except LabCodingJobLifecycleError as exc:
        raise _error(
            f"invalid coding-job lifecycle successor: {exc}",
            exc,
        )

    expected_previous = _require_identifier(
        "expected_previous_snapshot_id",
        expected_previous_snapshot_id,
    )

    with _locked_root(
        root,
        exclusive=True,
    ) as root_fd:
        jobs_fd = _open_jobs_directory(
            root_fd
        )

        try:
            previous, count = _load_chain_from_jobs_fd(
                jobs_fd,
                job_id=trusted_successor.job_id,
            )

            if previous.snapshot_id != expected_previous:
                raise LabCodingJobLifecycleJournalError(
                    "stale coding-job lifecycle predecessor"
                )

            if trusted_successor.snapshot_id == previous.snapshot_id:
                raise LabCodingJobLifecycleJournalError(
                    "refusing no-op coding-job lifecycle append"
                )

            _reconstruct_successor(
                previous,
                trusted_successor,
            )

            if count >= MAX_SNAPSHOTS:
                raise LabCodingJobLifecycleJournalError(
                    "coding-job lifecycle journal exceeds snapshot limit"
                )

            job_fd = _open_job_directory(
                jobs_fd,
                job_id=trusted_successor.job_id,
            )

            try:
                snapshots_fd = _open_snapshots_directory(
                    job_fd
                )

                try:
                    filename = _snapshot_filename(
                        count
                    )

                    data = _encode_snapshot(
                        trusted_successor,
                        sequence=count,
                        previous_snapshot_id=previous.snapshot_id,
                    )

                    _create_private_file(
                        snapshots_fd,
                        filename=filename,
                        data=data,
                    )

                    os.fsync(
                        snapshots_fd
                    )
                    os.fsync(
                        job_fd
                    )
                    os.fsync(
                        jobs_fd
                    )
                    os.fsync(
                        root_fd
                    )

                finally:
                    os.close(
                        snapshots_fd
                    )

            finally:
                os.close(
                    job_fd
                )

            reloaded, reloaded_count = _load_chain_from_jobs_fd(
                jobs_fd,
                job_id=trusted_successor.job_id,
            )

            if (
                reloaded_count != count + 1
                or reloaded != trusted_successor
            ):
                raise LabCodingJobLifecycleJournalError(
                    "appended coding-job lifecycle snapshot did not "
                    "reload exactly"
                )

        except LabCodingJobLifecycleJournalError:
            raise

        except OSError as exc:
            raise _error(
                "coding-job lifecycle append failed; partial state may "
                f"remain for inspection: {exc}",
                exc,
            )

        finally:
            os.close(
                jobs_fd
            )

    return trusted_successor


def load_lab_coding_job_lifecycle_journal(
    root: str | os.PathLike[str],
    *,
    job_id: object,
) -> LabCodingJobLifecycle:
    """Securely load and validate the latest snapshot of one coding job."""

    trusted_job_id = _require_identifier(
        "job_id",
        job_id,
    )

    with _locked_root(
        root,
        exclusive=False,
    ) as root_fd:
        jobs_fd = _open_jobs_directory(
            root_fd
        )

        try:
            lifecycle, _ = _load_chain_from_jobs_fd(
                jobs_fd,
                job_id=trusted_job_id,
            )

            return lifecycle

        finally:
            os.close(
                jobs_fd
            )
