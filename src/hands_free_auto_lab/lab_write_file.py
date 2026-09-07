"""Deterministic WRITE_FILE execution inside one validated Auto Lab workspace.

This module writes workspace files only. It does not execute commands, create
directories, follow symlinks, access host paths outside the validated
workspace, or promote data into a real repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import stat

from .lab_action import (
    LabAction,
    MAX_CONTENT_BYTES,
)
from .lab_policy import (
    LAB_POLICY,
    RESERVED_ACCEPTANCE_PATH,
    LabPolicyError,
    evaluate_lab_policy,
)
from .lab_workspace import (
    LabWorkspace,
    LabWorkspaceError,
    validate_lab_workspace,
)


WRITE_COMPONENT = "hands-free-auto-lab-write-file-v1"
ACCEPTANCE_SEED_COMPONENT = (
    "hands-free-auto-lab-acceptance-seed-v1"
)

NEW_FILE_MODE = 0o600

_ACCEPTANCE_SEED_DOMAIN = (
    b"hands-free-auto-lab-acceptance-seed-v1\x00"
)


class LabWriteFileError(RuntimeError):
    """Raised when WRITE_FILE cannot be performed safely."""


@dataclass(frozen=True, slots=True)
class LabWriteFileResult:
    component: str
    action_id: str
    policy: str
    workspace_device: int
    workspace_inode: int
    path: str
    bytes_written: int
    sha256: str
    replaced_existing: bool

    @property
    def succeeded(self) -> bool:
        return True


def _directory_flags() -> int:
    for name in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
    ):
        if not hasattr(os, name):
            raise LabWriteFileError(
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


def _file_read_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise LabWriteFileError(
            "secure file primitive unavailable: os.O_NOFOLLOW"
        )

    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
    )

    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    return flags


def _file_create_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise LabWriteFileError(
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


def _open_validated_workspace_fd(
    workspace: LabWorkspace,
) -> int:
    try:
        fd = os.open(
            workspace.path,
            _directory_flags(),
        )
    except OSError as exc:
        raise LabWriteFileError(
            f"cannot securely open validated workspace: {exc}"
        ) from exc

    try:
        st = os.fstat(fd)

        if not stat.S_ISDIR(st.st_mode):
            raise LabWriteFileError(
                "opened workspace is not a directory"
            )

        if st.st_dev != workspace.device:
            raise LabWriteFileError(
                "opened workspace device identity mismatch"
            )

        if st.st_ino != workspace.inode:
            raise LabWriteFileError(
                "opened workspace inode identity mismatch"
            )

        if st.st_uid != workspace.uid:
            raise LabWriteFileError(
                "opened workspace owner identity mismatch"
            )

        if stat.S_IMODE(st.st_mode) != workspace.mode:
            raise LabWriteFileError(
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
                raise LabWriteFileError(
                    "cannot securely open WRITE_FILE "
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


def _destination_state(
    parent_fd: int,
    name: str,
) -> bool:
    try:
        st = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LabWriteFileError(
            f"cannot inspect WRITE_FILE destination: {exc}"
        ) from exc

    if stat.S_ISLNK(st.st_mode):
        raise LabWriteFileError(
            "WRITE_FILE destination must not be a symlink"
        )

    if not stat.S_ISREG(st.st_mode):
        raise LabWriteFileError(
            "WRITE_FILE destination must be a regular file"
        )

    return True


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
            raise LabWriteFileError(
                f"WRITE_FILE data write failed: {exc}"
            ) from exc

        if written <= 0:
            raise LabWriteFileError(
                "WRITE_FILE data write made no progress"
            )

        offset += written


def _verify_final_file(
    parent_fd: int,
    name: str,
    expected: bytes,
) -> str:
    try:
        fd = os.open(
            name,
            _file_read_flags(),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise LabWriteFileError(
            f"cannot reopen WRITE_FILE result: {exc}"
        ) from exc

    try:
        st = os.fstat(fd)

        if not stat.S_ISREG(st.st_mode):
            raise LabWriteFileError(
                "WRITE_FILE result is not a regular file"
            )

        digest = hashlib.sha256()
        observed = bytearray()

        while True:
            try:
                chunk = os.read(
                    fd,
                    64 * 1024,
                )
            except OSError as exc:
                raise LabWriteFileError(
                    f"cannot verify WRITE_FILE result: {exc}"
                ) from exc

            if not chunk:
                break

            observed.extend(
                chunk
            )
            digest.update(
                chunk
            )

            if len(observed) > len(expected):
                raise LabWriteFileError(
                    "WRITE_FILE verification length mismatch"
                )

        if bytes(observed) != expected:
            raise LabWriteFileError(
                "WRITE_FILE verification content mismatch"
            )

        return digest.hexdigest()

    finally:
        os.close(fd)


def execute_lab_write_file(
    action: LabAction,
    *,
    workspace_parent: str,
    workspace: LabWorkspace,
) -> LabWriteFileResult:
    """Perform exactly one eligible WRITE_FILE inside the workspace."""
    try:
        decision = evaluate_lab_policy(
            action
        )
    except LabPolicyError as exc:
        raise LabWriteFileError(
            f"lab policy refused action: {exc}"
        ) from exc

    if decision["kind"] != "WRITE_FILE":
        raise LabWriteFileError(
            "WRITE_FILE executor refuses non-WRITE_FILE actions"
        )

    if decision["cwd"] != ".":
        raise LabWriteFileError(
            "initial WRITE_FILE executor requires cwd='.'"
        )

    if action.content is None:
        raise LabWriteFileError(
            "WRITE_FILE content is unavailable"
        )

    try:
        validate_lab_workspace(
            workspace_parent,
            workspace,
        )
    except LabWorkspaceError as exc:
        raise LabWriteFileError(
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
        raise LabWriteFileError(
            "WRITE_FILE path components are invalid"
        )

    parent_components = components[:-1]
    destination_name = components[-1]

    workspace_fd = _open_validated_workspace_fd(
        workspace
    )

    parent_fd: int | None = None
    temporary_fd: int | None = None

    temporary_name = (
        ".hands-free-auto-lab-write-"
        f"{action.action_id}.tmp"
    )

    data = action.content.encode(
        "utf-8"
    )

    try:
        parent_fd = _open_parent_directory(
            workspace_fd,
            parent_components,
        )

        replaced_existing = _destination_state(
            parent_fd,
            destination_name,
        )

        try:
            temporary_fd = os.open(
                temporary_name,
                _file_create_flags(),
                NEW_FILE_MODE,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise LabWriteFileError(
                "WRITE_FILE temporary path already exists; "
                "refusing reuse"
            ) from exc
        except OSError as exc:
            raise LabWriteFileError(
                f"cannot create WRITE_FILE temporary file: {exc}"
            ) from exc

        _write_all(
            temporary_fd,
            data,
        )

        try:
            os.fsync(
                temporary_fd
            )
        except OSError as exc:
            raise LabWriteFileError(
                f"cannot fsync WRITE_FILE temporary file: {exc}"
            ) from exc

        os.close(
            temporary_fd
        )
        temporary_fd = None

        try:
            os.replace(
                temporary_name,
                destination_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except OSError as exc:
            raise LabWriteFileError(
                f"cannot atomically install WRITE_FILE result: {exc}"
            ) from exc

        try:
            os.fsync(
                parent_fd
            )
        except OSError as exc:
            raise LabWriteFileError(
                f"cannot fsync WRITE_FILE parent directory: {exc}"
            ) from exc

        digest = _verify_final_file(
            parent_fd,
            destination_name,
            data,
        )

        expected_digest = hashlib.sha256(
            data
        ).hexdigest()

        if digest != expected_digest:
            raise LabWriteFileError(
                "WRITE_FILE result digest mismatch"
            )

        return LabWriteFileResult(
            component=WRITE_COMPONENT,
            action_id=action.action_id,
            policy=LAB_POLICY,
            workspace_device=workspace.device,
            workspace_inode=workspace.inode,
            path=path,
            bytes_written=len(data),
            sha256=digest,
            replaced_existing=replaced_existing,
        )

    finally:
        if temporary_fd is not None:
            os.close(
                temporary_fd
            )

        if parent_fd is not None:
            os.close(
                parent_fd
            )

        os.close(
            workspace_fd
        )


def seed_reserved_acceptance_file(
    *,
    content: str,
    workspace_parent: str,
    workspace: LabWorkspace,
) -> LabWriteFileResult:
    """Create the controller-owned acceptance file exactly once.

    This is a trusted controller operation, not a model WRITE_FILE action.
    The destination must not already exist. The operation uses the same
    validated workspace FD and no-follow file primitives as WRITE_FILE.
    """
    if not isinstance(
        content,
        str,
    ):
        raise LabWriteFileError(
            "acceptance content must be a string"
        )

    try:
        data = content.encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise LabWriteFileError(
            "acceptance content must be UTF-8 encodable"
        ) from exc

    if len(data) > MAX_CONTENT_BYTES:
        raise LabWriteFileError(
            "acceptance content exceeds maximum size"
        )

    try:
        validate_lab_workspace(
            workspace_parent,
            workspace,
        )
    except LabWorkspaceError as exc:
        raise LabWriteFileError(
            f"workspace validation failed: {exc}"
        ) from exc

    workspace_fd = _open_validated_workspace_fd(
        workspace
    )

    file_fd: int | None = None

    seed_id = hashlib.sha256(
        _ACCEPTANCE_SEED_DOMAIN
        + data
    ).hexdigest()

    try:
        if _destination_state(
            workspace_fd,
            RESERVED_ACCEPTANCE_PATH,
        ):
            raise LabWriteFileError(
                "reserved acceptance path already exists; "
                "refusing replacement"
            )

        try:
            file_fd = os.open(
                RESERVED_ACCEPTANCE_PATH,
                _file_create_flags(),
                NEW_FILE_MODE,
                dir_fd=workspace_fd,
            )
        except FileExistsError as exc:
            raise LabWriteFileError(
                "reserved acceptance path appeared "
                "during creation; refusing replacement"
            ) from exc
        except OSError as exc:
            raise LabWriteFileError(
                "cannot securely create reserved "
                f"acceptance file: {exc}"
            ) from exc

        _write_all(
            file_fd,
            data,
        )

        try:
            os.fsync(
                file_fd
            )
        except OSError as exc:
            raise LabWriteFileError(
                "cannot fsync reserved acceptance file: "
                f"{exc}"
            ) from exc

        os.close(
            file_fd
        )
        file_fd = None

        try:
            os.fsync(
                workspace_fd
            )
        except OSError as exc:
            raise LabWriteFileError(
                "cannot fsync workspace after acceptance "
                f"seed: {exc}"
            ) from exc

        digest = _verify_final_file(
            workspace_fd,
            RESERVED_ACCEPTANCE_PATH,
            data,
        )

        expected_digest = hashlib.sha256(
            data
        ).hexdigest()

        if digest != expected_digest:
            raise LabWriteFileError(
                "reserved acceptance seed digest mismatch"
            )

        return LabWriteFileResult(
            component=ACCEPTANCE_SEED_COMPONENT,
            action_id=seed_id,
            policy=LAB_POLICY,
            workspace_device=workspace.device,
            workspace_inode=workspace.inode,
            path=RESERVED_ACCEPTANCE_PATH,
            bytes_written=len(data),
            sha256=digest,
            replaced_existing=False,
        )

    finally:
        if file_fd is not None:
            os.close(
                file_fd
            )

        os.close(
            workspace_fd
        )
