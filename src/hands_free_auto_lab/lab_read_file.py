"""Deterministic READ_FILE evidence from one validated Auto Lab workspace."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import stat

from .lab_action import LabAction
from .lab_policy import (
    LAB_POLICY,
    LabPolicyError,
    evaluate_lab_policy,
)
from .lab_workspace import (
    LabWorkspace,
    LabWorkspaceError,
    validate_lab_workspace,
)


READ_COMPONENT = "hands-free-auto-lab-read-file-v1"
READ_LIMIT_BYTES = 256 * 1024
READ_CHUNK_BYTES = 64 * 1024


class LabReadFileError(RuntimeError):
    """Raised when READ_FILE cannot produce trustworthy evidence."""


@dataclass(frozen=True, slots=True)
class LabReadFileResult:
    component: str
    action_id: str
    policy: str
    workspace_device: int
    workspace_inode: int
    path: str
    bytes_read: int
    sha256: str
    content: str

    @property
    def succeeded(self) -> bool:
        return True


def _directory_flags() -> int:
    for name in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
    ):
        if not hasattr(os, name):
            raise LabReadFileError(
                f"secure directory primitive unavailable: os.{name}"
            )

    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
    )

    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    return flags


def _file_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise LabReadFileError(
            "secure file primitive unavailable: os.O_NOFOLLOW"
        )

    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
    )

    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    return flags


def _open_validated_workspace_fd(
    workspace: LabWorkspace,
) -> int:
    try:
        fd = os.open(
            workspace.path,
            _directory_flags(),
        )
    except OSError as exc:
        raise LabReadFileError(
            f"cannot securely open validated workspace: {exc}"
        ) from exc

    try:
        st = os.fstat(fd)

        if not stat.S_ISDIR(st.st_mode):
            raise LabReadFileError(
                "opened workspace is not a directory"
            )

        if st.st_dev != workspace.device:
            raise LabReadFileError(
                "opened workspace device identity mismatch"
            )

        if st.st_ino != workspace.inode:
            raise LabReadFileError(
                "opened workspace inode identity mismatch"
            )

        if st.st_uid != workspace.uid:
            raise LabReadFileError(
                "opened workspace owner identity mismatch"
            )

        if stat.S_IMODE(st.st_mode) != workspace.mode:
            raise LabReadFileError(
                "opened workspace mode identity mismatch"
            )

        return fd

    except Exception:
        os.close(fd)
        raise


def _open_parent_directory(
    workspace_fd: int,
    components: tuple[str, ...],
) -> int:
    current_fd = os.dup(
        workspace_fd
    )

    try:
        for component in components:
            try:
                next_fd = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise LabReadFileError(
                    "cannot securely open READ_FILE "
                    f"parent component {component!r}: {exc}"
                ) from exc

            os.close(
                current_fd
            )
            current_fd = next_fd

        return current_fd

    except Exception:
        os.close(
            current_fd
        )
        raise


def _file_identity(
    st: os.stat_result,
) -> tuple[int, ...]:
    return (
        st.st_dev,
        st.st_ino,
        st.st_mode,
        st.st_uid,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _read_open_file(
    fd: int,
) -> tuple[bytes, str]:
    before = os.fstat(
        fd
    )

    if not stat.S_ISREG(before.st_mode):
        raise LabReadFileError(
            "READ_FILE target must be a regular file"
        )

    if before.st_size > READ_LIMIT_BYTES:
        raise LabReadFileError(
            "READ_FILE target exceeds maximum size"
        )

    before_identity = _file_identity(
        before
    )

    data = bytearray()

    while True:
        try:
            chunk = os.read(
                fd,
                READ_CHUNK_BYTES,
            )
        except OSError as exc:
            raise LabReadFileError(
                f"READ_FILE data read failed: {exc}"
            ) from exc

        if not chunk:
            break

        data.extend(
            chunk
        )

        if len(data) > READ_LIMIT_BYTES:
            raise LabReadFileError(
                "READ_FILE target exceeds maximum size"
            )

    after = os.fstat(
        fd
    )

    if _file_identity(after) != before_identity:
        raise LabReadFileError(
            "READ_FILE target changed while being read"
        )

    if len(data) != after.st_size:
        raise LabReadFileError(
            "READ_FILE observed length mismatch"
        )

    raw = bytes(
        data
    )

    try:
        content = raw.decode(
            "utf-8",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise LabReadFileError(
            "READ_FILE target is not valid UTF-8 text"
        ) from exc

    return (
        raw,
        content,
    )


def execute_lab_read_file(
    action: LabAction,
    *,
    workspace_parent: str,
    workspace: LabWorkspace,
) -> LabReadFileResult:
    """Read exactly one eligible regular UTF-8 file from the workspace."""
    try:
        decision = evaluate_lab_policy(
            action
        )
    except LabPolicyError as exc:
        raise LabReadFileError(
            f"lab policy refused action: {exc}"
        ) from exc

    if decision["kind"] != "READ_FILE":
        raise LabReadFileError(
            "READ_FILE executor refuses non-READ_FILE actions"
        )

    if decision["cwd"] != ".":
        raise LabReadFileError(
            "initial READ_FILE executor requires cwd='.'"
        )

    try:
        validate_lab_workspace(
            workspace_parent,
            workspace,
        )
    except LabWorkspaceError as exc:
        raise LabReadFileError(
            f"workspace validation failed: {exc}"
        ) from exc

    path = decision["path"]

    components = tuple(
        path.split("/")
    )

    if (
        not components
        or any(
            not component
            for component in components
        )
    ):
        raise LabReadFileError(
            "READ_FILE path components are invalid"
        )

    workspace_fd = _open_validated_workspace_fd(
        workspace
    )

    parent_fd: int | None = None
    file_fd: int | None = None

    try:
        parent_fd = _open_parent_directory(
            workspace_fd,
            components[:-1],
        )

        try:
            file_fd = os.open(
                components[-1],
                _file_flags(),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise LabReadFileError(
                f"cannot securely open READ_FILE target: {exc}"
            ) from exc

        raw, content = _read_open_file(
            file_fd
        )

        return LabReadFileResult(
            component=READ_COMPONENT,
            action_id=action.action_id,
            policy=LAB_POLICY,
            workspace_device=workspace.device,
            workspace_inode=workspace.inode,
            path=path,
            bytes_read=len(raw),
            sha256=hashlib.sha256(
                raw
            ).hexdigest(),
            content=content,
        )

    finally:
        if file_fd is not None:
            os.close(
                file_fd
            )

        if parent_fd is not None:
            os.close(
                parent_fd
            )

        os.close(
            workspace_fd
        )
