"""Durable one-use human approval state for promotion proposals.

Authority in this module is intentionally narrow.

It may write only to an explicitly supplied private approval-state root.
It does not accept a repository path, inspect Git, execute commands, or
modify promotion destination files.

Lifecycle:

    immutable proposal_id
        -> challenge
        -> human approved/rejected decision
        -> short-lived approval
        -> exclusive durable terminal claim
        -> consumed / expired / revoked

The fixed-name terminal claim is the serialization point. Once claimed,
any later failure leaves the approval unusable. Authority is never restored
by cleanup, retry, or replay.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
import secrets
import stat
from typing import Iterator

from .lab_promotion_approval_state import (
    APPROVAL_MAX_LIFETIME,
    CHALLENGE_MAX_LIFETIME,
    LabPromotionApprovalStateError,
    MAX_RECORD_BYTES,
    build_approval_record,
    build_challenge_record,
    build_decision_record,
    build_terminal_record,
    validate_approval_record,
    validate_challenge_record,
    validate_decision_record,
    validate_terminal_record,
)


LOCK_FILENAME = "promotion-approval.lock"

CHALLENGES_DIRECTORY = "challenges"
APPROVALS_DIRECTORY = "approvals"

CHALLENGE_FILENAME = "challenge.json"
DECISION_FILENAME = "decision.json"

APPROVAL_FILENAME = "approval.json"
TERMINAL_FILENAME = "terminal.json"

ROOT_ENTRIES = frozenset(
    {
        LOCK_FILENAME,
        CHALLENGES_DIRECTORY,
        APPROVALS_DIRECTORY,
    }
)

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)


class LabPromotionApprovalStoreError(
    RuntimeError
):
    """Raised when durable promotion approval state is unsafe."""


def _utcnow() -> datetime:
    return datetime.now(
        timezone.utc
    )


def _require_identifier(
    name: str,
    value: object,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionApprovalStoreError(
            f"{name} must be a string"
        )

    if (
        len(value) != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabPromotionApprovalStoreError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _require_root_path(
    root: str | os.PathLike[str],
) -> Path:
    path = Path(root)

    text = os.fspath(
        path
    )

    if not os.path.isabs(
        text
    ):
        raise LabPromotionApprovalStoreError(
            "approval state root must be absolute"
        )

    normalized = os.path.normpath(
        text
    )

    if normalized != text:
        raise LabPromotionApprovalStoreError(
            "approval state root must be normalized"
        )

    if os.path.realpath(
        text
    ) != text:
        raise LabPromotionApprovalStoreError(
            "approval state root must be canonical and contain "
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

    for name, function in (
        (
            "os.open(dir_fd=...)",
            os.open,
        ),
        (
            "os.mkdir(dir_fd=...)",
            os.mkdir,
        ),
    ):
        if function not in os.supports_dir_fd:
            missing.append(
                name
            )

    if os.listdir not in os.supports_fd:
        missing.append(
            "os.listdir(fd)"
        )

    if missing:
        raise LabPromotionApprovalStoreError(
            "secure promotion approval-store primitives unavailable: "
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
    mode = st.st_mode

    if require_directory:
        if not stat.S_ISDIR(
            mode
        ):
            raise LabPromotionApprovalStoreError(
                f"{name} must be a directory"
            )

        if (
            mode
            & 0o777
        ) != 0o700:
            raise LabPromotionApprovalStoreError(
                f"{name} must have mode 0700"
            )

    else:
        if not stat.S_ISREG(
            mode
        ):
            raise LabPromotionApprovalStoreError(
                f"{name} must be a regular file"
            )

        if st.st_nlink != 1:
            raise LabPromotionApprovalStoreError(
                f"{name} must have exactly one hard link"
            )

        if (
            mode
            & 0o777
        ) != 0o600:
            raise LabPromotionApprovalStoreError(
                f"{name} must have mode 0600"
            )

    if st.st_uid != os.geteuid():
        raise LabPromotionApprovalStoreError(
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
        raise LabPromotionApprovalStoreError(
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
        raise LabPromotionApprovalStoreError(
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
        raise LabPromotionApprovalStoreError(
            f"refusing existing directory {filename}"
        ) from exc
    except OSError as exc:
        raise LabPromotionApprovalStoreError(
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
            raise LabPromotionApprovalStoreError(
                f"approval-state write failed: {exc}"
            ) from exc

        if written <= 0:
            raise LabPromotionApprovalStoreError(
                "short write while persisting approval state"
            )

        view = view[
            written:
        ]


def _create_private_file(
    directory_fd: int,
    *,
    filename: str,
    data: bytes,
) -> None:
    if len(
        data
    ) > MAX_RECORD_BYTES:
        raise LabPromotionApprovalStoreError(
            f"{filename} exceeds {MAX_RECORD_BYTES} bytes"
        )

    try:
        fd = os.open(
            filename,
            _create_file_flags(),
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LabPromotionApprovalStoreError(
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
        raise LabPromotionApprovalStoreError(
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
        raise LabPromotionApprovalStoreError(
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

    except OSError as exc:
        raise LabPromotionApprovalStoreError(
            f"cannot persist {filename}: {exc}"
        ) from exc

    finally:
        os.close(
            fd
        )


def _read_private_file(
    directory_fd: int,
    *,
    filename: str,
) -> bytes:
    try:
        fd = os.open(
            filename,
            _read_flags(),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LabPromotionApprovalStoreError(
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

        if st.st_size > MAX_RECORD_BYTES:
            raise LabPromotionApprovalStoreError(
                f"{filename} exceeds {MAX_RECORD_BYTES} bytes"
            )

        chunks: list[bytes] = []
        remaining = (
            MAX_RECORD_BYTES
            + 1
        )

        while remaining > 0:
            try:
                chunk = os.read(
                    fd,
                    min(
                        4096,
                        remaining,
                    ),
                )
            except OSError as exc:
                raise LabPromotionApprovalStoreError(
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

        if len(
            data
        ) > MAX_RECORD_BYTES:
            raise LabPromotionApprovalStoreError(
                f"{filename} exceeds {MAX_RECORD_BYTES} bytes"
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
        raise LabPromotionApprovalStoreError(
            f"cannot list {name}: {exc}"
        ) from exc


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
        raise LabPromotionApprovalStoreError(
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


def _validate_initialized_root_fd(
    root_fd: int,
) -> None:
    entries = _list_directory(
        root_fd,
        name="promotion approval state root",
    )

    if entries != ROOT_ENTRIES:
        missing = sorted(
            ROOT_ENTRIES
            - entries
        )

        extra = sorted(
            entries
            - ROOT_ENTRIES
        )

        raise LabPromotionApprovalStoreError(
            "promotion approval state root entries mismatch: "
            f"missing={missing}, extra={extra}"
        )

    lock_fd = _open_lock_file(
        root_fd
    )

    os.close(
        lock_fd
    )

    for directory in (
        CHALLENGES_DIRECTORY,
        APPROVALS_DIRECTORY,
    ):
        fd = _open_private_child_directory(
            root_fd,
            filename=directory,
            name=directory,
        )

        os.close(
            fd
        )


def initialize_promotion_approval_state_root(
    root: str | os.PathLike[str],
) -> Path:
    """Initialize or strictly validate an existing empty/private root."""
    root_path = _require_root_path(
        root
    )

    root_fd = _open_private_directory_path(
        root_path,
        name="promotion approval state root",
    )

    try:
        entries = _list_directory(
            root_fd,
            name="promotion approval state root",
        )

        if not entries:
            _create_empty_private_file(
                root_fd,
                filename=LOCK_FILENAME,
            )

            for directory in (
                CHALLENGES_DIRECTORY,
                APPROVALS_DIRECTORY,
            ):
                child_fd = _create_private_directory(
                    root_fd,
                    filename=directory,
                )

                os.fsync(
                    child_fd
                )

                os.close(
                    child_fd
                )

            os.fsync(
                root_fd
            )

        _validate_initialized_root_fd(
            root_fd
        )

    except LabPromotionApprovalStoreError:
        raise

    except OSError as exc:
        raise LabPromotionApprovalStoreError(
            "promotion approval state initialization failed; "
            "partial state may remain for inspection: "
            f"{exc}"
        ) from exc

    finally:
        os.close(
            root_fd
        )

    return root_path


@contextmanager
def _state_lock(
    root: str | os.PathLike[str],
    *,
    exclusive: bool,
) -> Iterator[int]:
    root_path = _require_root_path(
        root
    )

    root_fd = _open_private_directory_path(
        root_path,
        name="promotion approval state root",
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
            raise LabPromotionApprovalStoreError(
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


def _open_challenges_directory(
    root_fd: int,
) -> int:
    return _open_private_child_directory(
        root_fd,
        filename=CHALLENGES_DIRECTORY,
        name=CHALLENGES_DIRECTORY,
    )


def _open_approvals_directory(
    root_fd: int,
) -> int:
    return _open_private_child_directory(
        root_fd,
        filename=APPROVALS_DIRECTORY,
        name=APPROVALS_DIRECTORY,
    )


def _open_challenge_directory(
    challenges_fd: int,
    challenge_id: str,
) -> int:
    _require_identifier(
        "challenge_id",
        challenge_id,
    )

    return _open_private_child_directory(
        challenges_fd,
        filename=challenge_id,
        name=f"challenge {challenge_id}",
    )


def _open_approval_directory(
    approvals_fd: int,
    approval_id: str,
) -> int:
    _require_identifier(
        "approval_id",
        approval_id,
    )

    return _open_private_child_directory(
        approvals_fd,
        filename=approval_id,
        name=f"approval {approval_id}",
    )


def _validate_challenge_directory_entries(
    challenge_fd: int,
) -> frozenset[str]:
    entries = _list_directory(
        challenge_fd,
        name="challenge directory",
    )

    allowed = (
        frozenset(
            {
                CHALLENGE_FILENAME,
            }
        ),
        frozenset(
            {
                CHALLENGE_FILENAME,
                DECISION_FILENAME,
            }
        ),
    )

    if entries not in allowed:
        raise LabPromotionApprovalStoreError(
            "challenge directory has unexpected or incomplete state: "
            f"{sorted(entries)!r}"
        )

    return entries


def _validate_approval_directory_entries(
    approval_fd: int,
) -> frozenset[str]:
    entries = _list_directory(
        approval_fd,
        name="approval directory",
    )

    allowed = (
        frozenset(
            {
                APPROVAL_FILENAME,
            }
        ),
        frozenset(
            {
                APPROVAL_FILENAME,
                TERMINAL_FILENAME,
            }
        ),
    )

    if entries not in allowed:
        raise LabPromotionApprovalStoreError(
            "approval directory has unexpected or incomplete state: "
            f"{sorted(entries)!r}"
        )

    return entries


def _load_challenge_from_fd(
    challenge_fd: int,
    *,
    challenge_id: str,
) -> tuple[
    dict[str, object],
    dict[str, object] | None,
]:
    entries = _validate_challenge_directory_entries(
        challenge_fd
    )

    raw = _read_private_file(
        challenge_fd,
        filename=CHALLENGE_FILENAME,
    )

    try:
        challenge = validate_challenge_record(
            raw
        )
    except LabPromotionApprovalStateError as exc:
        raise LabPromotionApprovalStoreError(
            f"invalid challenge record: {exc}"
        ) from exc

    if challenge["challenge_id"] != challenge_id:
        raise LabPromotionApprovalStoreError(
            "challenge identifier binding mismatch"
        )

    decision = None

    if DECISION_FILENAME in entries:
        raw_decision = _read_private_file(
            challenge_fd,
            filename=DECISION_FILENAME,
        )

        try:
            decision = validate_decision_record(
                raw_decision
            )
        except LabPromotionApprovalStateError as exc:
            raise LabPromotionApprovalStoreError(
                f"invalid decision record: {exc}"
            ) from exc

        if decision["challenge_id"] != challenge_id:
            raise LabPromotionApprovalStoreError(
                "decision challenge binding mismatch"
            )

        if (
            decision["proposal_id"]
            != challenge["proposal_id"]
        ):
            raise LabPromotionApprovalStoreError(
                "decision proposal binding mismatch"
            )

    return (
        challenge,
        decision,
    )


def _load_challenge(
    challenges_fd: int,
    challenge_id: str,
) -> tuple[
    dict[str, object],
    dict[str, object] | None,
]:
    challenge_fd = _open_challenge_directory(
        challenges_fd,
        challenge_id,
    )

    try:
        return _load_challenge_from_fd(
            challenge_fd,
            challenge_id=challenge_id,
        )

    finally:
        os.close(
            challenge_fd
        )


def _persist_decision(
    challenge_fd: int,
    challenges_fd: int,
    *,
    record_bytes: bytes,
) -> dict[str, object]:
    _create_private_file(
        challenge_fd,
        filename=DECISION_FILENAME,
        data=record_bytes,
    )

    os.fsync(
        challenge_fd
    )

    os.fsync(
        challenges_fd
    )

    persisted = _read_private_file(
        challenge_fd,
        filename=DECISION_FILENAME,
    )

    try:
        return validate_decision_record(
            persisted
        )
    except LabPromotionApprovalStateError as exc:
        raise LabPromotionApprovalStoreError(
            f"persisted decision failed validation: {exc}"
        ) from exc


def _persist_approval(
    approvals_fd: int,
    *,
    approval_id: str,
    record_bytes: bytes,
) -> dict[str, object]:
    approval_fd = _create_private_directory(
        approvals_fd,
        filename=approval_id,
    )

    try:
        _create_private_file(
            approval_fd,
            filename=APPROVAL_FILENAME,
            data=record_bytes,
        )

        os.fsync(
            approval_fd
        )

        os.fsync(
            approvals_fd
        )

        persisted = _read_private_file(
            approval_fd,
            filename=APPROVAL_FILENAME,
        )

        try:
            approval = validate_approval_record(
                persisted
            )
        except LabPromotionApprovalStateError as exc:
            raise LabPromotionApprovalStoreError(
                f"persisted approval failed validation: {exc}"
            ) from exc

        if approval["approval_id"] != approval_id:
            raise LabPromotionApprovalStoreError(
                "persisted approval identifier mismatch"
            )

        return approval

    finally:
        os.close(
            approval_fd
        )


def _load_approval_from_fd(
    approval_fd: int,
    *,
    approval_id: str,
) -> tuple[
    dict[str, object],
    dict[str, object] | None,
]:
    entries = _validate_approval_directory_entries(
        approval_fd
    )

    raw = _read_private_file(
        approval_fd,
        filename=APPROVAL_FILENAME,
    )

    try:
        approval = validate_approval_record(
            raw
        )
    except LabPromotionApprovalStateError as exc:
        raise LabPromotionApprovalStoreError(
            f"invalid approval record: {exc}"
        ) from exc

    if approval["approval_id"] != approval_id:
        raise LabPromotionApprovalStoreError(
            "approval identifier binding mismatch"
        )

    terminal = None

    if TERMINAL_FILENAME in entries:
        raw_terminal = _read_private_file(
            approval_fd,
            filename=TERMINAL_FILENAME,
        )

        try:
            terminal = validate_terminal_record(
                raw_terminal
            )
        except LabPromotionApprovalStateError as exc:
            raise LabPromotionApprovalStoreError(
                f"invalid terminal state for {approval_id}: {exc}"
            ) from exc

        if terminal["approval_id"] != approval_id:
            raise LabPromotionApprovalStoreError(
                "terminal approval binding mismatch"
            )

        if (
            terminal["proposal_id"]
            != approval["proposal_id"]
        ):
            raise LabPromotionApprovalStoreError(
                "terminal proposal binding mismatch"
            )

    return (
        approval,
        terminal,
    )


def _load_approval(
    approvals_fd: int,
    approval_id: str,
) -> tuple[
    dict[str, object],
    dict[str, object] | None,
]:
    approval_fd = _open_approval_directory(
        approvals_fd,
        approval_id,
    )

    try:
        return _load_approval_from_fd(
            approval_fd,
            approval_id=approval_id,
        )

    finally:
        os.close(
            approval_fd
        )


def _parse_timestamp(
    value: object,
    *,
    name: str,
) -> datetime:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionApprovalStoreError(
            f"{name} timestamp is malformed"
        )

    try:
        parsed = datetime.fromisoformat(
            value
        )
    except ValueError as exc:
        raise LabPromotionApprovalStoreError(
            f"{name} timestamp is malformed"
        ) from exc

    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
    ):
        raise LabPromotionApprovalStoreError(
            f"{name} timestamp is not timezone-aware"
        )

    return parsed


def _expire_old_pending_challenges(
    challenges_fd: int,
    *,
    now: datetime,
) -> list[
    dict[str, object]
]:
    entries = _list_directory(
        challenges_fd,
        name="challenges directory",
    )

    pending: list[
        dict[str, object]
    ] = []

    for challenge_id in sorted(
        entries
    ):
        _require_identifier(
            "challenge directory name",
            challenge_id,
        )

        challenge_fd = _open_challenge_directory(
            challenges_fd,
            challenge_id,
        )

        try:
            challenge, decision = _load_challenge_from_fd(
                challenge_fd,
                challenge_id=challenge_id,
            )

            if decision is not None:
                continue

            created_at = _parse_timestamp(
                challenge["created_at"],
                name="challenge created_at",
            )

            expires_at = _parse_timestamp(
                challenge["expires_at"],
                name="challenge expires_at",
            )

            if now < created_at:
                raise LabPromotionApprovalStoreError(
                    "clock precedes pending challenge creation"
                )

            if now >= expires_at:
                raw = build_decision_record(
                    challenge_id=challenge_id,
                    proposal_id=str(
                        challenge["proposal_id"]
                    ),
                    decision="expired",
                    decided_at=now.isoformat(),
                    approval_id=None,
                )

                _persist_decision(
                    challenge_fd,
                    challenges_fd,
                    record_bytes=raw,
                )

                continue

            pending.append(
                challenge
            )

        finally:
            os.close(
                challenge_fd
            )

    return pending


def create_promotion_challenge(
    root: str | os.PathLike[str],
    *,
    proposal_id: str,
) -> dict[str, object]:
    """Create one pending human challenge bound to exact proposal_id."""
    proposal_id = _require_identifier(
        "proposal_id",
        proposal_id,
    )

    now = _utcnow()

    with _state_lock(
        root,
        exclusive=True,
    ) as root_fd:
        challenges_fd = _open_challenges_directory(
            root_fd
        )

        try:
            pending = _expire_old_pending_challenges(
                challenges_fd,
                now=now,
            )

            if pending:
                raise LabPromotionApprovalStoreError(
                    "a promotion challenge is already pending"
                )

            challenge_id = secrets.token_hex(
                32
            )

            _require_identifier(
                "generated challenge_id",
                challenge_id,
            )

            expires_at = (
                now
                + CHALLENGE_MAX_LIFETIME
            )

            record_bytes = build_challenge_record(
                challenge_id=challenge_id,
                proposal_id=proposal_id,
                created_at=now.isoformat(),
                expires_at=expires_at.isoformat(),
            )

            challenge_fd = _create_private_directory(
                challenges_fd,
                filename=challenge_id,
            )

            try:
                _create_private_file(
                    challenge_fd,
                    filename=CHALLENGE_FILENAME,
                    data=record_bytes,
                )

                os.fsync(
                    challenge_fd
                )

                os.fsync(
                    challenges_fd
                )

                persisted = _read_private_file(
                    challenge_fd,
                    filename=CHALLENGE_FILENAME,
                )

                try:
                    challenge = validate_challenge_record(
                        persisted
                    )
                except LabPromotionApprovalStateError as exc:
                    raise LabPromotionApprovalStoreError(
                        f"persisted challenge failed validation: {exc}"
                    ) from exc

                return challenge

            finally:
                os.close(
                    challenge_fd
                )

        finally:
            os.close(
                challenges_fd
            )


def decide_promotion_challenge(
    root: str | os.PathLike[str],
    *,
    challenge_id: str,
    proposal_id: str,
    decision: str,
) -> dict[str, object]:
    """Persist one irreversible human approve/reject decision."""
    challenge_id = _require_identifier(
        "challenge_id",
        challenge_id,
    )

    proposal_id = _require_identifier(
        "proposal_id",
        proposal_id,
    )

    if decision not in {
        "approve",
        "reject",
    }:
        raise LabPromotionApprovalStoreError(
            "decision must be approve or reject"
        )

    now = _utcnow()

    with _state_lock(
        root,
        exclusive=True,
    ) as root_fd:
        challenges_fd = _open_challenges_directory(
            root_fd
        )

        approvals_fd = _open_approvals_directory(
            root_fd
        )

        try:
            challenge_fd = _open_challenge_directory(
                challenges_fd,
                challenge_id,
            )

            try:
                challenge, existing_decision = (
                    _load_challenge_from_fd(
                        challenge_fd,
                        challenge_id=challenge_id,
                    )
                )

                if (
                    challenge["proposal_id"]
                    != proposal_id
                ):
                    raise LabPromotionApprovalStoreError(
                        "challenge proposal binding mismatch"
                    )

                if existing_decision is not None:
                    raise LabPromotionApprovalStoreError(
                        "challenge already has an irreversible decision"
                    )

                created_at = _parse_timestamp(
                    challenge["created_at"],
                    name="challenge created_at",
                )

                expires_at = _parse_timestamp(
                    challenge["expires_at"],
                    name="challenge expires_at",
                )

                if now < created_at:
                    raise LabPromotionApprovalStoreError(
                        "clock precedes challenge creation"
                    )

                if now >= expires_at:
                    expired_bytes = build_decision_record(
                        challenge_id=challenge_id,
                        proposal_id=proposal_id,
                        decision="expired",
                        decided_at=now.isoformat(),
                        approval_id=None,
                    )

                    _persist_decision(
                        challenge_fd,
                        challenges_fd,
                        record_bytes=expired_bytes,
                    )

                    raise LabPromotionApprovalStoreError(
                        "promotion challenge expired"
                    )

                if decision == "reject":
                    rejected_bytes = build_decision_record(
                        challenge_id=challenge_id,
                        proposal_id=proposal_id,
                        decision="rejected",
                        decided_at=now.isoformat(),
                        approval_id=None,
                    )

                    rejected = _persist_decision(
                        challenge_fd,
                        challenges_fd,
                        record_bytes=rejected_bytes,
                    )

                    return {
                        "status": "rejected",
                        "challenge": challenge,
                        "decision": rejected,
                        "approval": None,
                    }

                approval_id = secrets.token_hex(
                    32
                )

                _require_identifier(
                    "generated approval_id",
                    approval_id,
                )

                approved_at = now
                approval_created_at = now
                approval_expires_at = (
                    approval_created_at
                    + APPROVAL_MAX_LIFETIME
                )

                decision_bytes = build_decision_record(
                    challenge_id=challenge_id,
                    proposal_id=proposal_id,
                    decision="approved",
                    decided_at=approved_at.isoformat(),
                    approval_id=approval_id,
                )

                approval_bytes = build_approval_record(
                    challenge_id=challenge_id,
                    proposal_id=proposal_id,
                    approval_id=approval_id,
                    approved_at=approved_at.isoformat(),
                    created_at=approval_created_at.isoformat(),
                    expires_at=approval_expires_at.isoformat(),
                )

                approved_decision = _persist_decision(
                    challenge_fd,
                    challenges_fd,
                    record_bytes=decision_bytes,
                )

                approval = _persist_approval(
                    approvals_fd,
                    approval_id=approval_id,
                    record_bytes=approval_bytes,
                )

                return {
                    "status": "approved",
                    "challenge": challenge,
                    "decision": approved_decision,
                    "approval": approval,
                }

            finally:
                os.close(
                    challenge_fd
                )

        finally:
            os.close(
                approvals_fd
            )

            os.close(
                challenges_fd
            )


def _verify_approval_authority_chain(
    *,
    challenges_fd: int,
    approval: dict[str, object],
) -> None:
    challenge_id = _require_identifier(
        "approval challenge_id",
        approval["challenge_id"],
    )

    challenge, decision = _load_challenge(
        challenges_fd,
        challenge_id,
    )

    if (
        challenge["proposal_id"]
        != approval["proposal_id"]
    ):
        raise LabPromotionApprovalStoreError(
            "approval/challenge proposal binding mismatch"
        )

    if decision is None:
        raise LabPromotionApprovalStoreError(
            "approval exists without durable human decision"
        )

    if decision["decision"] != "approved":
        raise LabPromotionApprovalStoreError(
            "approval is not backed by approved human decision"
        )

    if (
        decision["approval_id"]
        != approval["approval_id"]
    ):
        raise LabPromotionApprovalStoreError(
            "approval identifier does not match human decision"
        )


def query_promotion_approval(
    root: str | os.PathLike[str],
    *,
    approval_id: str,
    proposal_id: str,
) -> dict[str, object]:
    """Read current durable approval state without changing authority."""
    approval_id = _require_identifier(
        "approval_id",
        approval_id,
    )

    proposal_id = _require_identifier(
        "proposal_id",
        proposal_id,
    )

    now = _utcnow()

    with _state_lock(
        root,
        exclusive=False,
    ) as root_fd:
        challenges_fd = _open_challenges_directory(
            root_fd
        )

        approvals_fd = _open_approvals_directory(
            root_fd
        )

        try:
            approval, terminal = _load_approval(
                approvals_fd,
                approval_id,
            )

            if (
                approval["proposal_id"]
                != proposal_id
            ):
                raise LabPromotionApprovalStoreError(
                    "approval proposal binding mismatch"
                )

            _verify_approval_authority_chain(
                challenges_fd=challenges_fd,
                approval=approval,
            )

            if terminal is not None:
                return {
                    "status": terminal["terminal_state"],
                    "approval": approval,
                    "terminal": terminal,
                }

            created_at = _parse_timestamp(
                approval["created_at"],
                name="approval created_at",
            )

            expires_at = _parse_timestamp(
                approval["expires_at"],
                name="approval expires_at",
            )

            if now < created_at:
                raise LabPromotionApprovalStoreError(
                    "clock precedes approval creation"
                )

            status = (
                "expired"
                if now >= expires_at
                else "approved"
            )

            return {
                "status": status,
                "approval": approval,
                "terminal": None,
            }

        finally:
            os.close(
                approvals_fd
            )

            os.close(
                challenges_fd
            )


def _claim_terminal_file(
    approval_fd: int,
) -> int:
    """Exclusively and durably destroy reusable approval authority."""
    try:
        fd = os.open(
            TERMINAL_FILENAME,
            _create_file_flags(),
            0o600,
            dir_fd=approval_fd,
        )
    except FileExistsError as exc:
        raise LabPromotionApprovalStoreError(
            "approval already has terminal state"
        ) from exc
    except OSError as exc:
        raise LabPromotionApprovalStoreError(
            f"cannot claim {TERMINAL_FILENAME}: {exc}"
        ) from exc

    try:
        os.fchmod(
            fd,
            0o600,
        )

        _validate_private_stat(
            os.fstat(
                fd
            ),
            name=TERMINAL_FILENAME,
            require_directory=False,
        )

        os.fsync(
            fd
        )

        os.fsync(
            approval_fd
        )

        return fd

    except Exception:
        os.close(
            fd
        )
        raise


def _finalize_terminal(
    *,
    terminal_fd: int,
    approval_fd: int,
    approvals_fd: int,
    record_bytes: bytes,
) -> dict[str, object]:
    if len(
        record_bytes
    ) > MAX_RECORD_BYTES:
        raise LabPromotionApprovalStoreError(
            "terminal record exceeds size limit"
        )

    _write_all(
        terminal_fd,
        record_bytes,
    )

    try:
        os.fsync(
            terminal_fd
        )

        os.fsync(
            approval_fd
        )

        os.fsync(
            approvals_fd
        )
    except OSError as exc:
        raise LabPromotionApprovalStoreError(
            f"cannot durably finalize terminal state: {exc}"
        ) from exc

    try:
        return validate_terminal_record(
            record_bytes
        )
    except LabPromotionApprovalStateError as exc:
        raise LabPromotionApprovalStoreError(
            f"terminal record failed validation: {exc}"
        ) from exc


def consume_promotion_approval(
    root: str | os.PathLike[str],
    *,
    approval_id: str,
    proposal_id: str,
    run_id: str,
) -> dict[str, object]:
    """Atomically consume exact approval for one exact promotion run."""
    approval_id = _require_identifier(
        "approval_id",
        approval_id,
    )

    proposal_id = _require_identifier(
        "proposal_id",
        proposal_id,
    )

    run_id = _require_identifier(
        "run_id",
        run_id,
    )

    with _state_lock(
        root,
        exclusive=True,
    ) as root_fd:
        challenges_fd = _open_challenges_directory(
            root_fd
        )

        approvals_fd = _open_approvals_directory(
            root_fd
        )

        try:
            approval_fd = _open_approval_directory(
                approvals_fd,
                approval_id,
            )

            try:
                approval, terminal = _load_approval_from_fd(
                    approval_fd,
                    approval_id=approval_id,
                )

                if (
                    approval["proposal_id"]
                    != proposal_id
                ):
                    raise LabPromotionApprovalStoreError(
                        "approval proposal binding mismatch"
                    )

                _verify_approval_authority_chain(
                    challenges_fd=challenges_fd,
                    approval=approval,
                )

                if terminal is not None:
                    raise LabPromotionApprovalStoreError(
                        "approval already has terminal state"
                    )

                terminal_fd = _claim_terminal_file(
                    approval_fd
                )

                try:
                    now = _utcnow()

                    created_at = _parse_timestamp(
                        approval["created_at"],
                        name="approval created_at",
                    )

                    expires_at = _parse_timestamp(
                        approval["expires_at"],
                        name="approval expires_at",
                    )

                    if now < created_at:
                        terminal_bytes = build_terminal_record(
                            approval_id=approval_id,
                            proposal_id=proposal_id,
                            terminal_state="revoked",
                            terminal_at=now.isoformat(),
                            run_id=None,
                        )

                        terminal_record = _finalize_terminal(
                            terminal_fd=terminal_fd,
                            approval_fd=approval_fd,
                            approvals_fd=approvals_fd,
                            record_bytes=terminal_bytes,
                        )

                        raise LabPromotionApprovalStoreError(
                            "approval revoked because clock precedes "
                            f"creation; terminal={terminal_record['terminal_state']}"
                        )

                    if now >= expires_at:
                        terminal_bytes = build_terminal_record(
                            approval_id=approval_id,
                            proposal_id=proposal_id,
                            terminal_state="expired",
                            terminal_at=now.isoformat(),
                            run_id=None,
                        )

                        terminal_record = _finalize_terminal(
                            terminal_fd=terminal_fd,
                            approval_fd=approval_fd,
                            approvals_fd=approvals_fd,
                            record_bytes=terminal_bytes,
                        )

                        raise LabPromotionApprovalStoreError(
                            "approval expired before consumption; "
                            f"terminal={terminal_record['terminal_state']}"
                        )

                    terminal_bytes = build_terminal_record(
                        approval_id=approval_id,
                        proposal_id=proposal_id,
                        terminal_state="consumed",
                        terminal_at=now.isoformat(),
                        run_id=run_id,
                    )

                    terminal_record = _finalize_terminal(
                        terminal_fd=terminal_fd,
                        approval_fd=approval_fd,
                        approvals_fd=approvals_fd,
                        record_bytes=terminal_bytes,
                    )

                    return {
                        "status": "consumed",
                        "approval": approval,
                        "terminal": terminal_record,
                    }

                finally:
                    os.close(
                        terminal_fd
                    )

            finally:
                os.close(
                    approval_fd
                )

        finally:
            os.close(
                approvals_fd
            )

            os.close(
                challenges_fd
            )
