"""Private disposable workspace directory primitives.

This module creates and validates the host directory that may later be
bind-mounted as /workspace inside the Auto Lab sandbox.

It does not:

- execute commands,
- launch Bubblewrap,
- copy project data,
- access the network,
- delete workspaces,
- promote lab data into a real repository.

Creation is fail-closed. If creation succeeds but later verification fails,
the module does not automatically delete the path. The caller must inspect
that known path explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import stat


WORKSPACE_COMPONENT = "hands-free-auto-lab-workspace-v1"
SESSION_ID_LENGTH = 64
PRIVATE_DIRECTORY_MODE = 0o700


class LabWorkspaceError(RuntimeError):
    """Raised when workspace creation or verification must fail closed."""


@dataclass(frozen=True, slots=True)
class LabWorkspace:
    """Stable identity of one private disposable workspace."""

    component: str
    parent: str
    path: str
    session_id: str
    device: int
    inode: int
    uid: int
    mode: int


def _validate_session_id(value: object) -> str:
    if not isinstance(value, str):
        raise LabWorkspaceError(
            "session_id must be a string"
        )

    if len(value) != SESSION_ID_LENGTH:
        raise LabWorkspaceError(
            "session_id must be a 64-character lowercase hex digest"
        )

    if value.lower() != value:
        raise LabWorkspaceError(
            "session_id must use lowercase hexadecimal"
        )

    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise LabWorkspaceError(
            "session_id is not valid hexadecimal"
        ) from exc

    if len(decoded) != SESSION_ID_LENGTH // 2:
        raise LabWorkspaceError(
            "session_id has invalid decoded length"
        )

    return value


def _directory_flags() -> int:
    required = (
        "O_DIRECTORY",
        "O_NOFOLLOW",
    )

    for name in required:
        if not hasattr(os, name):
            raise LabWorkspaceError(
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


def _canonical_absolute_path(
    value: object,
    *,
    name: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise LabWorkspaceError(
            f"{name} must be a non-empty string"
        )

    if "\x00" in value:
        raise LabWorkspaceError(
            f"{name} must not contain NUL"
        )

    if not os.path.isabs(value):
        raise LabWorkspaceError(
            f"{name} must be absolute"
        )

    try:
        resolved = os.path.realpath(value)
    except OSError as exc:
        raise LabWorkspaceError(
            f"cannot resolve {name}: {exc}"
        ) from exc

    if resolved != value:
        raise LabWorkspaceError(
            f"{name} must be an exact canonical path "
            "without symlink or textual aliasing"
        )

    return value


def _validate_private_directory_stat(
    st: os.stat_result,
    *,
    name: str,
) -> None:
    if not stat.S_ISDIR(st.st_mode):
        raise LabWorkspaceError(
            f"{name} is not a directory"
        )

    expected_uid = os.geteuid()

    if st.st_uid != expected_uid:
        raise LabWorkspaceError(
            f"{name} is not owned by the effective user"
        )

    mode = stat.S_IMODE(st.st_mode)

    if mode != PRIVATE_DIRECTORY_MODE:
        raise LabWorkspaceError(
            f"{name} must have mode 0700"
        )


def _open_private_parent(
    parent: object,
) -> tuple[int, str]:
    canonical = _canonical_absolute_path(
        parent,
        name="workspace parent",
    )

    try:
        fd = os.open(
            canonical,
            _directory_flags(),
        )
    except OSError as exc:
        raise LabWorkspaceError(
            f"cannot securely open workspace parent: {exc}"
        ) from exc

    try:
        st = os.fstat(fd)

        _validate_private_directory_stat(
            st,
            name="workspace parent",
        )

        return fd, canonical

    except Exception:
        os.close(fd)
        raise


def _open_workspace_child(
    parent_fd: int,
    session_id: str,
) -> int:
    try:
        return os.open(
            session_id,
            _directory_flags(),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise LabWorkspaceError(
            f"cannot securely open workspace child: {exc}"
        ) from exc


def _identity_from_stat(
    *,
    parent: str,
    session_id: str,
    st: os.stat_result,
) -> LabWorkspace:
    _validate_private_directory_stat(
        st,
        name="workspace",
    )

    return LabWorkspace(
        component=WORKSPACE_COMPONENT,
        parent=parent,
        path=os.path.join(
            parent,
            session_id,
        ),
        session_id=session_id,
        device=st.st_dev,
        inode=st.st_ino,
        uid=st.st_uid,
        mode=stat.S_IMODE(st.st_mode),
    )


def create_lab_workspace(
    parent: str,
    *,
    session_id: str,
) -> LabWorkspace:
    """Create exactly one new private workspace.

    The supplied parent must already exist, be canonical, be owned by the
    effective user, and have mode 0700.

    Existing child paths are never reused.
    """
    session_id = _validate_session_id(
        session_id
    )

    parent_fd, canonical_parent = (
        _open_private_parent(parent)
    )

    created = False
    child_fd: int | None = None

    try:
        try:
            os.mkdir(
                session_id,
                mode=PRIVATE_DIRECTORY_MODE,
                dir_fd=parent_fd,
            )
            created = True

        except FileExistsError as exc:
            raise LabWorkspaceError(
                "workspace child already exists; refusing reuse"
            ) from exc

        except OSError as exc:
            raise LabWorkspaceError(
                f"cannot create workspace child: {exc}"
            ) from exc

        try:
            child_fd = _open_workspace_child(
                parent_fd,
                session_id,
            )

            child_stat = os.fstat(
                child_fd
            )

            identity = _identity_from_stat(
                parent=canonical_parent,
                session_id=session_id,
                st=child_stat,
            )

            os.fsync(child_fd)
            os.fsync(parent_fd)

            return identity

        except Exception as exc:
            if created:
                raise LabWorkspaceError(
                    "workspace was created but verification failed; "
                    "the created path was not deleted automatically "
                    "and must be inspected"
                ) from exc

            raise

    finally:
        if child_fd is not None:
            os.close(child_fd)

        os.close(parent_fd)


def validate_lab_workspace(
    parent: str,
    workspace: LabWorkspace,
) -> LabWorkspace:
    """Revalidate one workspace against its recorded inode identity."""
    if not isinstance(workspace, LabWorkspace):
        raise LabWorkspaceError(
            "workspace must be a LabWorkspace"
        )

    if workspace.component != WORKSPACE_COMPONENT:
        raise LabWorkspaceError(
            "workspace component mismatch"
        )

    session_id = _validate_session_id(
        workspace.session_id
    )

    parent_fd, canonical_parent = (
        _open_private_parent(parent)
    )

    child_fd: int | None = None

    try:
        if workspace.parent != canonical_parent:
            raise LabWorkspaceError(
                "workspace parent identity mismatch"
            )

        expected_path = os.path.join(
            canonical_parent,
            session_id,
        )

        if workspace.path != expected_path:
            raise LabWorkspaceError(
                "workspace path identity mismatch"
            )

        child_fd = _open_workspace_child(
            parent_fd,
            session_id,
        )

        st = os.fstat(
            child_fd
        )

        _validate_private_directory_stat(
            st,
            name="workspace",
        )

        if st.st_dev != workspace.device:
            raise LabWorkspaceError(
                "workspace device identity mismatch"
            )

        if st.st_ino != workspace.inode:
            raise LabWorkspaceError(
                "workspace inode identity mismatch"
            )

        if st.st_uid != workspace.uid:
            raise LabWorkspaceError(
                "workspace owner identity mismatch"
            )

        if stat.S_IMODE(st.st_mode) != workspace.mode:
            raise LabWorkspaceError(
                "workspace mode identity mismatch"
            )

        return workspace

    finally:
        if child_fd is not None:
            os.close(child_fd)

        os.close(parent_fd)
