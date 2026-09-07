"""Immutable persistence of one Auto Lab session record.

This module has one narrow host-write authority: create one JSON evidence
file inside an explicitly supplied private evidence directory.

It does not:

- execute model actions,
- modify a lab workspace,
- delete or overwrite audit records,
- create arbitrary caller-selected filenames,
- promote data into a real repository,
- stage, commit, or push Git state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import stat

from .lab_session import (
    LabSessionRecord,
    lab_session_record_to_wire,
)


AUDIT_COMPONENT = "hands-free-auto-lab-audit-v1"
PRIVATE_DIRECTORY_MODE = 0o700
AUDIT_FILE_MODE = 0o600
SESSION_ID_LENGTH = 64


class LabAuditError(RuntimeError):
    """Raised when immutable audit persistence must fail closed."""


@dataclass(frozen=True, slots=True)
class LabAuditWriteResult:
    component: str
    session_id: str
    evidence_directory: str
    directory_device: int
    directory_inode: int
    filename: str
    path: str
    bytes_written: int
    sha256: str


def _validate_session_id(
    value: object,
) -> str:
    if not isinstance(value, str):
        raise LabAuditError(
            "session_id must be a string"
        )

    if len(value) != SESSION_ID_LENGTH:
        raise LabAuditError(
            "session_id must be a 64-character lowercase hex digest"
        )

    if value.lower() != value:
        raise LabAuditError(
            "session_id must use lowercase hexadecimal"
        )

    try:
        decoded = bytes.fromhex(
            value
        )
    except ValueError as exc:
        raise LabAuditError(
            "session_id is not valid hexadecimal"
        ) from exc

    if len(decoded) != SESSION_ID_LENGTH // 2:
        raise LabAuditError(
            "session_id has invalid decoded length"
        )

    return value


def _directory_flags() -> int:
    for name in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
    ):
        if not hasattr(os, name):
            raise LabAuditError(
                "secure directory primitive unavailable: "
                f"os.{name}"
            )

    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
    )

    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    return flags


def _file_create_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise LabAuditError(
            "secure file primitive unavailable: os.O_NOFOLLOW"
        )

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
    )

    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    return flags


def _file_read_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise LabAuditError(
            "secure file primitive unavailable: os.O_NOFOLLOW"
        )

    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
    )

    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    return flags


def _canonical_evidence_directory(
    value: object,
) -> str:
    if not isinstance(value, str) or not value:
        raise LabAuditError(
            "evidence_directory must be a non-empty string"
        )

    if "\x00" in value:
        raise LabAuditError(
            "evidence_directory must not contain NUL"
        )

    if not os.path.isabs(value):
        raise LabAuditError(
            "evidence_directory must be absolute"
        )

    try:
        resolved = os.path.realpath(
            value
        )
    except OSError as exc:
        raise LabAuditError(
            f"cannot resolve evidence_directory: {exc}"
        ) from exc

    if resolved != value:
        raise LabAuditError(
            "evidence_directory must be an exact canonical path "
            "without symlink or textual aliasing"
        )

    return value


def _open_private_evidence_directory(
    evidence_directory: object,
) -> tuple[int, str, os.stat_result]:
    canonical = _canonical_evidence_directory(
        evidence_directory
    )

    try:
        fd = os.open(
            canonical,
            _directory_flags(),
        )
    except OSError as exc:
        raise LabAuditError(
            f"cannot securely open evidence_directory: {exc}"
        ) from exc

    try:
        st = os.fstat(
            fd
        )

        if not stat.S_ISDIR(st.st_mode):
            raise LabAuditError(
                "evidence_directory is not a directory"
            )

        if st.st_uid != os.geteuid():
            raise LabAuditError(
                "evidence_directory is not owned by the effective user"
            )

        if stat.S_IMODE(st.st_mode) != PRIVATE_DIRECTORY_MODE:
            raise LabAuditError(
                "evidence_directory must have mode 0700"
            )

        return (
            fd,
            canonical,
            st,
        )

    except Exception:
        os.close(fd)
        raise


def _write_all(
    fd: int,
    data: bytes,
) -> None:
    view = memoryview(
        data
    )
    offset = 0

    while offset < len(view):
        try:
            written = os.write(
                fd,
                view[offset:],
            )
        except OSError as exc:
            raise LabAuditError(
                f"cannot write audit temporary file: {exc}"
            ) from exc

        if written <= 0:
            raise LabAuditError(
                "audit temporary write made no progress"
            )

        offset += written


def _destination_must_not_exist(
    directory_fd: int,
    filename: str,
) -> None:
    try:
        os.stat(
            filename,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LabAuditError(
            f"cannot inspect audit destination: {exc}"
        ) from exc

    raise LabAuditError(
        "audit destination already exists; refusing overwrite"
    )


def _verify_temporary_identity(
    directory_fd: int,
    temporary_name: str,
    temporary_stat: os.stat_result,
) -> None:
    try:
        path_stat = os.stat(
            temporary_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LabAuditError(
            f"cannot verify audit temporary path: {exc}"
        ) from exc

    if not stat.S_ISREG(path_stat.st_mode):
        raise LabAuditError(
            "audit temporary path is not a regular file"
        )

    if (
        path_stat.st_dev != temporary_stat.st_dev
        or path_stat.st_ino != temporary_stat.st_ino
    ):
        raise LabAuditError(
            "audit temporary path identity mismatch"
        )

    if path_stat.st_uid != os.geteuid():
        raise LabAuditError(
            "audit temporary path has unexpected owner"
        )

    if stat.S_IMODE(path_stat.st_mode) != AUDIT_FILE_MODE:
        raise LabAuditError(
            "audit temporary path must have mode 0600"
        )

    if path_stat.st_nlink != 1:
        raise LabAuditError(
            "audit temporary path has unexpected hard-link count"
        )


def _verify_link_transition(
    directory_fd: int,
    temporary_name: str,
    filename: str,
    temporary_stat: os.stat_result,
) -> None:
    try:
        temporary_path_stat = os.stat(
            temporary_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )

        final_path_stat = os.stat(
            filename,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LabAuditError(
            f"cannot verify audit install link transition: {exc}"
        ) from exc

    for name, st in (
        ("temporary", temporary_path_stat),
        ("final", final_path_stat),
    ):
        if not stat.S_ISREG(st.st_mode):
            raise LabAuditError(
                f"audit {name} install path is not a regular file"
            )

        if st.st_uid != os.geteuid():
            raise LabAuditError(
                f"audit {name} install path has unexpected owner"
            )

        if stat.S_IMODE(st.st_mode) != AUDIT_FILE_MODE:
            raise LabAuditError(
                f"audit {name} install path must have mode 0600"
            )

        if st.st_nlink != 2:
            raise LabAuditError(
                f"audit {name} install path must have "
                "hard-link count 2 during transition"
            )

    expected_identity = (
        temporary_stat.st_dev,
        temporary_stat.st_ino,
    )

    temporary_identity = (
        temporary_path_stat.st_dev,
        temporary_path_stat.st_ino,
    )

    final_identity = (
        final_path_stat.st_dev,
        final_path_stat.st_ino,
    )

    if temporary_identity != expected_identity:
        raise LabAuditError(
            "audit temporary identity changed during install"
        )

    if final_identity != expected_identity:
        raise LabAuditError(
            "audit final path does not identify temporary inode"
        )


def _verify_final_file(
    directory_fd: int,
    filename: str,
    expected: bytes,
) -> str:
    try:
        fd = os.open(
            filename,
            _file_read_flags(),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LabAuditError(
            f"cannot securely open final audit record: {exc}"
        ) from exc

    try:
        st = os.fstat(
            fd
        )

        if not stat.S_ISREG(st.st_mode):
            raise LabAuditError(
                "final audit record is not a regular file"
            )

        if st.st_uid != os.geteuid():
            raise LabAuditError(
                "final audit record has unexpected owner"
            )

        if stat.S_IMODE(st.st_mode) != AUDIT_FILE_MODE:
            raise LabAuditError(
                "final audit record must have mode 0600"
            )

        if st.st_nlink != 1:
            raise LabAuditError(
                "final audit record has unexpected hard-link count"
            )

        chunks: list[bytes] = []

        while True:
            try:
                chunk = os.read(
                    fd,
                    65536,
                )
            except OSError as exc:
                raise LabAuditError(
                    f"cannot read final audit record: {exc}"
                ) from exc

            if not chunk:
                break

            chunks.append(
                chunk
            )

        actual = b"".join(
            chunks
        )

        if actual != expected:
            raise LabAuditError(
                "final audit record content mismatch"
            )

        return hashlib.sha256(
            actual
        ).hexdigest()

    finally:
        os.close(
            fd
        )


def write_lab_session_audit(
    *,
    record: LabSessionRecord,
    evidence_directory: str,
) -> LabAuditWriteResult:
    """Create exactly one immutable JSON audit record."""

    if not isinstance(record, LabSessionRecord):
        raise LabAuditError(
            "record must be a LabSessionRecord"
        )

    session_id = _validate_session_id(
        record.workspace.session_id
    )

    wire = lab_session_record_to_wire(
        record
    )

    try:
        encoded = (
            json.dumps(
                wire,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode(
            "utf-8"
        )
    except (
        TypeError,
        ValueError,
    ) as exc:
        raise LabAuditError(
            f"session record is not JSON serializable: {exc}"
        ) from exc

    filename = (
        f"session-{session_id}.json"
    )

    temporary_name = (
        ".hands-free-auto-lab-audit-"
        f"{session_id}.tmp"
    )

    (
        directory_fd,
        canonical_directory,
        directory_stat,
    ) = _open_private_evidence_directory(
        evidence_directory
    )

    temporary_fd: int | None = None

    try:
        _destination_must_not_exist(
            directory_fd,
            filename,
        )

        try:
            temporary_fd = os.open(
                temporary_name,
                _file_create_flags(),
                AUDIT_FILE_MODE,
                dir_fd=directory_fd,
            )
        except FileExistsError as exc:
            raise LabAuditError(
                "audit temporary path already exists; refusing reuse"
            ) from exc
        except OSError as exc:
            raise LabAuditError(
                f"cannot create audit temporary file: {exc}"
            ) from exc

        try:
            os.fchmod(
                temporary_fd,
                AUDIT_FILE_MODE,
            )
        except OSError as exc:
            raise LabAuditError(
                f"cannot set audit temporary file mode: {exc}"
            ) from exc

        _write_all(
            temporary_fd,
            encoded,
        )

        try:
            os.fsync(
                temporary_fd
            )
        except OSError as exc:
            raise LabAuditError(
                f"cannot fsync audit temporary file: {exc}"
            ) from exc

        temporary_stat = os.fstat(
            temporary_fd
        )

        _verify_temporary_identity(
            directory_fd,
            temporary_name,
            temporary_stat,
        )

        try:
            os.link(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise LabAuditError(
                "audit destination already exists; refusing overwrite"
            ) from exc
        except OSError as exc:
            raise LabAuditError(
                f"cannot atomically install audit record: {exc}"
            ) from exc

        try:
            os.fsync(
                directory_fd
            )
        except OSError as exc:
            raise LabAuditError(
                f"cannot fsync evidence_directory after install: {exc}"
            ) from exc

        _verify_link_transition(
            directory_fd,
            temporary_name,
            filename,
            temporary_stat,
        )

        try:
            os.unlink(
                temporary_name,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise LabAuditError(
                f"cannot remove installed audit temporary link: {exc}"
            ) from exc

        try:
            os.fsync(
                directory_fd
            )
        except OSError as exc:
            raise LabAuditError(
                f"cannot fsync evidence_directory after cleanup: {exc}"
            ) from exc

        digest = _verify_final_file(
            directory_fd,
            filename,
            encoded,
        )

        return LabAuditWriteResult(
            component=AUDIT_COMPONENT,
            session_id=session_id,
            evidence_directory=canonical_directory,
            directory_device=directory_stat.st_dev,
            directory_inode=directory_stat.st_ino,
            filename=filename,
            path=os.path.join(
                canonical_directory,
                filename,
            ),
            bytes_written=len(
                encoded
            ),
            sha256=digest,
        )

    finally:
        if temporary_fd is not None:
            os.close(
                temporary_fd
            )

        os.close(
            directory_fd
        )
