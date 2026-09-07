"""Independent physical truth for one Auto Lab disposable workspace.

A worker may report which paths it believes it changed.  This module does not
trust that report.  It securely enumerates the physical workspace before and
after a worker run and derives a deterministic diff from those observations.

The snapshot contains metadata and content digests, not file contents.

Security properties:

* validates the exact recorded workspace identity,
* walks directories using file descriptors,
* refuses symlink traversal,
* accepts only regular files and real directories,
* refuses multiply-linked regular files,
* requires workspace ownership for every observed entry,
* detects files or directories changing while they are inspected,
* bounds entry count, file size, total file bytes, and recursion depth,
* accepts binary file contents and hashes raw bytes,
* performs no execution, networking, Git, approval, or promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import PurePosixPath
import stat

from .lab_workspace import (
    LabWorkspace,
    LabWorkspaceError,
    validate_lab_workspace,
)


SNAPSHOT_COMPONENT = "hands-free-auto-lab-workspace-snapshot-v1"
SNAPSHOT_SCHEMA_VERSION = 1

DIFF_COMPONENT = "hands-free-auto-lab-workspace-diff-v1"
DIFF_SCHEMA_VERSION = 1

ENTRY_FILE = "FILE"
ENTRY_DIRECTORY = "DIRECTORY"

ENTRY_KINDS = frozenset(
    {
        ENTRY_FILE,
        ENTRY_DIRECTORY,
    }
)

CHANGE_ADDED = "ADDED"
CHANGE_MODIFIED = "MODIFIED"
CHANGE_DELETED = "DELETED"

CHANGE_KINDS = frozenset(
    {
        CHANGE_ADDED,
        CHANGE_MODIFIED,
        CHANGE_DELETED,
    }
)

MAX_WORKSPACE_ENTRIES = 4096
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 64 * 1024 * 1024
MAX_DIRECTORY_DEPTH = 64

READ_CHUNK_BYTES = 64 * 1024

_SNAPSHOT_ID_DOMAIN = (
    b"hands-free-auto-lab-workspace-snapshot-id-v1\x00"
)

_DIFF_ID_DOMAIN = (
    b"hands-free-auto-lab-workspace-diff-id-v1\x00"
)

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)


class LabWorkspaceSnapshotError(
    RuntimeError
):
    """Physical workspace evidence could not be established safely."""


@dataclass(
    frozen=True,
    slots=True,
)
class LabWorkspaceSnapshotEntry:
    path: str
    kind: str
    mode: int
    bytes: int | None
    sha256: str | None


@dataclass(
    frozen=True,
    slots=True,
)
class LabWorkspaceSnapshot:
    component: str
    schema_version: int
    snapshot_id: str
    workspace_session_id: str
    workspace_device: int
    workspace_inode: int
    entries: tuple[
        LabWorkspaceSnapshotEntry,
        ...,
    ]


@dataclass(
    frozen=True,
    slots=True,
)
class LabWorkspaceDiffEntry:
    path: str
    change: str
    before: LabWorkspaceSnapshotEntry | None
    after: LabWorkspaceSnapshotEntry | None


@dataclass(
    frozen=True,
    slots=True,
)
class LabWorkspaceDiff:
    component: str
    schema_version: int
    diff_id: str
    workspace_session_id: str
    workspace_device: int
    workspace_inode: int
    before_snapshot_id: str
    after_snapshot_id: str
    entries: tuple[
        LabWorkspaceDiffEntry,
        ...,
    ]


def _canonical_json_bytes(
    value: object,
) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(
            ",",
            ":",
        ),
        ensure_ascii=False,
    ).encode(
        "utf-8"
    )


def _require_positive_limit(
    value: object,
    *,
    name: str,
    maximum: int,
) -> int:
    if (
        not isinstance(
            value,
            int,
        )
        or isinstance(
            value,
            bool,
        )
        or not (
            1
            <= value
            <= maximum
        )
    ):
        raise LabWorkspaceSnapshotError(
            f"{name} is outside the supported range"
        )

    return value


def _require_identifier(
    value: object,
    *,
    name: str,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabWorkspaceSnapshotError(
            f"{name} must be a string"
        )

    if (
        len(
            value
        )
        != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabWorkspaceSnapshotError(
            f"{name} must be a lowercase 64-character hex identifier"
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
        or "\x00" in value
    ):
        raise LabWorkspaceSnapshotError(
            "snapshot path must be a non-empty string without NUL"
        )

    try:
        value.encode(
            "utf-8",
            errors="strict",
        )
    except UnicodeEncodeError as exc:
        raise LabWorkspaceSnapshotError(
            "snapshot path must be UTF-8 encodable"
        ) from exc

    path = PurePosixPath(
        value
    )

    if (
        path.is_absolute()
        or value == "."
        or ".." in path.parts
        or str(
            path
        )
        != value
    ):
        raise LabWorkspaceSnapshotError(
            "snapshot path must be canonical and relative"
        )

    return value


def _require_entry_name(
    name: object,
) -> str:
    if (
        not isinstance(
            name,
            str,
        )
        or not name
        or name in (
            ".",
            "..",
        )
        or "/" in name
        or "\x00" in name
    ):
        raise LabWorkspaceSnapshotError(
            "workspace entry name is invalid"
        )

    try:
        name.encode(
            "utf-8",
            errors="strict",
        )
    except UnicodeEncodeError as exc:
        raise LabWorkspaceSnapshotError(
            "workspace entry name is not UTF-8 encodable"
        ) from exc

    return name


def _directory_flags() -> int:
    for name in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
    ):
        if not hasattr(
            os,
            name,
        ):
            raise LabWorkspaceSnapshotError(
                f"secure directory primitive unavailable: os.{name}"
            )

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


def _file_flags() -> int:
    if not hasattr(
        os,
        "O_NOFOLLOW",
    ):
        raise LabWorkspaceSnapshotError(
            "secure file primitive unavailable: os.O_NOFOLLOW"
        )

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


def _stat_identity(
    value: os.stat_result,
) -> tuple[
    int,
    ...,
]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_validated_workspace_fd(
    workspace: LabWorkspace,
) -> int:
    try:
        fd = os.open(
            workspace.path,
            _directory_flags(),
        )
    except OSError as exc:
        raise LabWorkspaceSnapshotError(
            f"cannot securely open validated workspace: {exc}"
        ) from exc

    try:
        observed = os.fstat(
            fd
        )

        if not stat.S_ISDIR(
            observed.st_mode
        ):
            raise LabWorkspaceSnapshotError(
                "opened workspace is not a directory"
            )

        if observed.st_dev != workspace.device:
            raise LabWorkspaceSnapshotError(
                "opened workspace device identity mismatch"
            )

        if observed.st_ino != workspace.inode:
            raise LabWorkspaceSnapshotError(
                "opened workspace inode identity mismatch"
            )

        if observed.st_uid != workspace.uid:
            raise LabWorkspaceSnapshotError(
                "opened workspace owner identity mismatch"
            )

        if (
            stat.S_IMODE(
                observed.st_mode
            )
            != workspace.mode
        ):
            raise LabWorkspaceSnapshotError(
                "opened workspace mode identity mismatch"
            )

        return fd

    except Exception:
        os.close(
            fd
        )
        raise


def _read_regular_file(
    parent_fd: int,
    name: str,
    *,
    observed: os.stat_result,
    workspace_uid: int,
    max_file_bytes: int,
) -> tuple[
    int,
    str,
]:
    if not stat.S_ISREG(
        observed.st_mode
    ):
        raise LabWorkspaceSnapshotError(
            "workspace snapshot target is not a regular file"
        )

    if observed.st_uid != workspace_uid:
        raise LabWorkspaceSnapshotError(
            "workspace file is not owned by workspace owner"
        )

    if observed.st_nlink != 1:
        raise LabWorkspaceSnapshotError(
            "workspace regular file must have exactly one hard link"
        )

    if observed.st_size > max_file_bytes:
        raise LabWorkspaceSnapshotError(
            "workspace file exceeds snapshot file-size limit"
        )

    try:
        fd = os.open(
            name,
            _file_flags(),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise LabWorkspaceSnapshotError(
            f"cannot securely open workspace file {name!r}: {exc}"
        ) from exc

    try:
        before = os.fstat(
            fd
        )

        if not stat.S_ISREG(
            before.st_mode
        ):
            raise LabWorkspaceSnapshotError(
                "opened workspace entry is not a regular file"
            )

        if _stat_identity(
            before
        ) != _stat_identity(
            observed
        ):
            raise LabWorkspaceSnapshotError(
                "workspace file changed while being opened"
            )

        if before.st_nlink != 1:
            raise LabWorkspaceSnapshotError(
                "opened workspace file has unexpected hard links"
            )

        digest = hashlib.sha256()
        bytes_read = 0

        while True:
            try:
                chunk = os.read(
                    fd,
                    READ_CHUNK_BYTES,
                )
            except OSError as exc:
                raise LabWorkspaceSnapshotError(
                    f"workspace file read failed: {exc}"
                ) from exc

            if not chunk:
                break

            bytes_read += len(
                chunk
            )

            if bytes_read > max_file_bytes:
                raise LabWorkspaceSnapshotError(
                    "workspace file exceeds snapshot file-size limit"
                )

            digest.update(
                chunk
            )

        after = os.fstat(
            fd
        )

        if _stat_identity(
            after
        ) != _stat_identity(
            before
        ):
            raise LabWorkspaceSnapshotError(
                "workspace file changed while being hashed"
            )

        if bytes_read != after.st_size:
            raise LabWorkspaceSnapshotError(
                "workspace file observed length mismatch"
            )

        return (
            bytes_read,
            digest.hexdigest(),
        )

    finally:
        os.close(
            fd
        )


def _append_entry(
    entries: list[
        LabWorkspaceSnapshotEntry
    ],
    entry: LabWorkspaceSnapshotEntry,
    *,
    max_entries: int,
) -> None:
    if len(
        entries
    ) >= max_entries:
        raise LabWorkspaceSnapshotError(
            "workspace exceeds snapshot entry-count limit"
        )

    entries.append(
        entry
    )


def _walk_directory(
    directory_fd: int,
    *,
    components: tuple[
        str,
        ...,
    ],
    depth: int,
    workspace_uid: int,
    entries: list[
        LabWorkspaceSnapshotEntry
    ],
    total_file_bytes: list[
        int
    ],
    max_entries: int,
    max_file_bytes: int,
    max_total_file_bytes: int,
    max_depth: int,
) -> None:
    if depth > max_depth:
        raise LabWorkspaceSnapshotError(
            "workspace exceeds snapshot directory-depth limit"
        )

    before_directory = os.fstat(
        directory_fd
    )

    if not stat.S_ISDIR(
        before_directory.st_mode
    ):
        raise LabWorkspaceSnapshotError(
            "snapshot traversal target is not a directory"
        )

    if before_directory.st_uid != workspace_uid:
        raise LabWorkspaceSnapshotError(
            "workspace directory is not owned by workspace owner"
        )

    try:
        raw_names = os.listdir(
            directory_fd
        )
    except OSError as exc:
        raise LabWorkspaceSnapshotError(
            f"cannot enumerate workspace directory: {exc}"
        ) from exc

    names = sorted(
        _require_entry_name(
            name
        )
        for name in raw_names
    )

    for name in names:
        try:
            observed = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LabWorkspaceSnapshotError(
                f"cannot inspect workspace entry {name!r}: {exc}"
            ) from exc

        if observed.st_uid != workspace_uid:
            raise LabWorkspaceSnapshotError(
                f"workspace entry {name!r} is not owned by workspace owner"
            )

        path_components = (
            *components,
            name,
        )

        relative_path = "/".join(
            path_components
        )

        _require_relative_path(
            relative_path
        )

        if stat.S_ISDIR(
            observed.st_mode
        ):
            _append_entry(
                entries,
                LabWorkspaceSnapshotEntry(
                    path=relative_path,
                    kind=ENTRY_DIRECTORY,
                    mode=stat.S_IMODE(
                        observed.st_mode
                    ),
                    bytes=None,
                    sha256=None,
                ),
                max_entries=max_entries,
            )

            try:
                child_fd = os.open(
                    name,
                    _directory_flags(),
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise LabWorkspaceSnapshotError(
                    f"cannot securely open workspace directory {relative_path!r}: {exc}"
                ) from exc

            try:
                opened = os.fstat(
                    child_fd
                )

                if _stat_identity(
                    opened
                ) != _stat_identity(
                    observed
                ):
                    raise LabWorkspaceSnapshotError(
                        "workspace directory changed while being opened"
                    )

                _walk_directory(
                    child_fd,
                    components=path_components,
                    depth=depth + 1,
                    workspace_uid=workspace_uid,
                    entries=entries,
                    total_file_bytes=total_file_bytes,
                    max_entries=max_entries,
                    max_file_bytes=max_file_bytes,
                    max_total_file_bytes=max_total_file_bytes,
                    max_depth=max_depth,
                )

            finally:
                os.close(
                    child_fd
                )

        elif stat.S_ISREG(
            observed.st_mode
        ):
            bytes_read, digest = _read_regular_file(
                directory_fd,
                name,
                observed=observed,
                workspace_uid=workspace_uid,
                max_file_bytes=max_file_bytes,
            )

            if (
                total_file_bytes[0]
                + bytes_read
                > max_total_file_bytes
            ):
                raise LabWorkspaceSnapshotError(
                    "workspace exceeds snapshot total-file-byte limit"
                )

            total_file_bytes[0] += bytes_read

            _append_entry(
                entries,
                LabWorkspaceSnapshotEntry(
                    path=relative_path,
                    kind=ENTRY_FILE,
                    mode=stat.S_IMODE(
                        observed.st_mode
                    ),
                    bytes=bytes_read,
                    sha256=digest,
                ),
                max_entries=max_entries,
            )

        else:
            raise LabWorkspaceSnapshotError(
                f"workspace contains unsupported entry type at {relative_path!r}"
            )

    after_directory = os.fstat(
        directory_fd
    )

    if _stat_identity(
        after_directory
    ) != _stat_identity(
        before_directory
    ):
        raise LabWorkspaceSnapshotError(
            "workspace directory changed while being enumerated"
        )


def _entry_identity_object(
    entry: LabWorkspaceSnapshotEntry,
) -> dict[
    str,
    object,
]:
    return {
        "path": entry.path,
        "kind": entry.kind,
        "mode": entry.mode,
        "bytes": entry.bytes,
        "sha256": entry.sha256,
    }


def _snapshot_identity_object(
    *,
    workspace_session_id: str,
    workspace_device: int,
    workspace_inode: int,
    entries: tuple[
        LabWorkspaceSnapshotEntry,
        ...,
    ],
) -> dict[
    str,
    object,
]:
    return {
        "component": SNAPSHOT_COMPONENT,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "workspace_session_id": workspace_session_id,
        "workspace_device": workspace_device,
        "workspace_inode": workspace_inode,
        "entries": [
            _entry_identity_object(
                entry
            )
            for entry in entries
        ],
    }


def _snapshot_id(
    identity: dict[
        str,
        object,
    ],
) -> str:
    return hashlib.sha256(
        _SNAPSHOT_ID_DOMAIN
        + _canonical_json_bytes(
            identity
        )
    ).hexdigest()


def _validate_entry(
    entry: object,
) -> LabWorkspaceSnapshotEntry:
    if not isinstance(
        entry,
        LabWorkspaceSnapshotEntry,
    ):
        raise LabWorkspaceSnapshotError(
            "snapshot entry has invalid type"
        )

    path = _require_relative_path(
        entry.path
    )

    if entry.kind not in ENTRY_KINDS:
        raise LabWorkspaceSnapshotError(
            f"unsupported snapshot entry kind {entry.kind!r}"
        )

    if (
        not isinstance(
            entry.mode,
            int,
        )
        or isinstance(
            entry.mode,
            bool,
        )
        or not (
            0
            <= entry.mode
            <= 0o7777
        )
    ):
        raise LabWorkspaceSnapshotError(
            f"snapshot entry mode is invalid for {path!r}"
        )

    if entry.kind == ENTRY_DIRECTORY:
        if (
            entry.bytes is not None
            or entry.sha256 is not None
        ):
            raise LabWorkspaceSnapshotError(
                "directory snapshot entry must not contain file evidence"
            )

    else:
        if (
            not isinstance(
                entry.bytes,
                int,
            )
            or isinstance(
                entry.bytes,
                bool,
            )
            or entry.bytes < 0
        ):
            raise LabWorkspaceSnapshotError(
                "file snapshot entry byte count is invalid"
            )

        _require_identifier(
            entry.sha256,
            name="file snapshot sha256",
        )

    return entry


def validate_lab_workspace_snapshot(
    snapshot: object,
) -> LabWorkspaceSnapshot:
    """Validate deterministic snapshot structure and identity."""
    if not isinstance(
        snapshot,
        LabWorkspaceSnapshot,
    ):
        raise LabWorkspaceSnapshotError(
            "snapshot must be a LabWorkspaceSnapshot"
        )

    if snapshot.component != SNAPSHOT_COMPONENT:
        raise LabWorkspaceSnapshotError(
            "workspace snapshot component mismatch"
        )

    if snapshot.schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise LabWorkspaceSnapshotError(
            "unsupported workspace snapshot schema version"
        )

    supplied_id = _require_identifier(
        snapshot.snapshot_id,
        name="snapshot_id",
    )

    session_id = _require_identifier(
        snapshot.workspace_session_id,
        name="workspace_session_id",
    )

    for name, value in (
        (
            "workspace_device",
            snapshot.workspace_device,
        ),
        (
            "workspace_inode",
            snapshot.workspace_inode,
        ),
    ):
        if (
            not isinstance(
                value,
                int,
            )
            or isinstance(
                value,
                bool,
            )
            or value < 0
        ):
            raise LabWorkspaceSnapshotError(
                f"{name} must be a non-negative integer"
            )

    if not isinstance(
        snapshot.entries,
        tuple,
    ):
        raise LabWorkspaceSnapshotError(
            "snapshot entries must be a tuple"
        )

    entries = tuple(
        _validate_entry(
            entry
        )
        for entry in snapshot.entries
    )

    paths = tuple(
        entry.path
        for entry in entries
    )

    if paths != tuple(
        sorted(
            paths
        )
    ):
        raise LabWorkspaceSnapshotError(
            "snapshot entries are not in canonical path order"
        )

    if len(
        paths
    ) != len(
        set(
            paths
        )
    ):
        raise LabWorkspaceSnapshotError(
            "snapshot contains duplicate paths"
        )

    expected_id = _snapshot_id(
        _snapshot_identity_object(
            workspace_session_id=session_id,
            workspace_device=snapshot.workspace_device,
            workspace_inode=snapshot.workspace_inode,
            entries=entries,
        )
    )

    if supplied_id != expected_id:
        raise LabWorkspaceSnapshotError(
            "workspace snapshot identity mismatch"
        )

    return snapshot


def capture_lab_workspace_snapshot(
    *,
    workspace_parent: str,
    workspace: LabWorkspace,
    max_entries: int = MAX_WORKSPACE_ENTRIES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_file_bytes: int = MAX_TOTAL_FILE_BYTES,
    max_depth: int = MAX_DIRECTORY_DEPTH,
) -> LabWorkspaceSnapshot:
    """Securely capture one complete physical workspace snapshot."""
    max_entries = _require_positive_limit(
        max_entries,
        name="max_entries",
        maximum=MAX_WORKSPACE_ENTRIES,
    )

    max_file_bytes = _require_positive_limit(
        max_file_bytes,
        name="max_file_bytes",
        maximum=MAX_FILE_BYTES,
    )

    max_total_file_bytes = _require_positive_limit(
        max_total_file_bytes,
        name="max_total_file_bytes",
        maximum=MAX_TOTAL_FILE_BYTES,
    )

    max_depth = _require_positive_limit(
        max_depth,
        name="max_depth",
        maximum=MAX_DIRECTORY_DEPTH,
    )

    try:
        validate_lab_workspace(
            workspace_parent,
            workspace,
        )
    except LabWorkspaceError as exc:
        raise LabWorkspaceSnapshotError(
            f"workspace validation failed: {exc}"
        ) from exc

    workspace_fd = _open_validated_workspace_fd(
        workspace
    )

    entries: list[
        LabWorkspaceSnapshotEntry
    ] = []

    total_file_bytes = [
        0
    ]

    try:
        _walk_directory(
            workspace_fd,
            components=(),
            depth=0,
            workspace_uid=workspace.uid,
            entries=entries,
            total_file_bytes=total_file_bytes,
            max_entries=max_entries,
            max_file_bytes=max_file_bytes,
            max_total_file_bytes=max_total_file_bytes,
            max_depth=max_depth,
        )

    finally:
        os.close(
            workspace_fd
        )

    try:
        validate_lab_workspace(
            workspace_parent,
            workspace,
        )
    except LabWorkspaceError as exc:
        raise LabWorkspaceSnapshotError(
            f"workspace changed after snapshot: {exc}"
        ) from exc

    canonical_entries = tuple(
        sorted(
            entries,
            key=lambda item: item.path,
        )
    )

    identity = _snapshot_identity_object(
        workspace_session_id=workspace.session_id,
        workspace_device=workspace.device,
        workspace_inode=workspace.inode,
        entries=canonical_entries,
    )

    snapshot = LabWorkspaceSnapshot(
        component=SNAPSHOT_COMPONENT,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=_snapshot_id(
            identity
        ),
        workspace_session_id=workspace.session_id,
        workspace_device=workspace.device,
        workspace_inode=workspace.inode,
        entries=canonical_entries,
    )

    validate_lab_workspace_snapshot(
        snapshot
    )

    return snapshot


def _diff_entry_identity_object(
    item: LabWorkspaceDiffEntry,
) -> dict[
    str,
    object,
]:
    return {
        "path": item.path,
        "change": item.change,
        "before": (
            None
            if item.before is None
            else _entry_identity_object(
                item.before
            )
        ),
        "after": (
            None
            if item.after is None
            else _entry_identity_object(
                item.after
            )
        ),
    }


def _diff_identity_object(
    *,
    before: LabWorkspaceSnapshot,
    after: LabWorkspaceSnapshot,
    entries: tuple[
        LabWorkspaceDiffEntry,
        ...,
    ],
) -> dict[
    str,
    object,
]:
    return {
        "component": DIFF_COMPONENT,
        "schema_version": DIFF_SCHEMA_VERSION,
        "workspace_session_id": before.workspace_session_id,
        "workspace_device": before.workspace_device,
        "workspace_inode": before.workspace_inode,
        "before_snapshot_id": before.snapshot_id,
        "after_snapshot_id": after.snapshot_id,
        "entries": [
            _diff_entry_identity_object(
                item
            )
            for item in entries
        ],
    }


def _diff_id(
    identity: dict[
        str,
        object,
    ],
) -> str:
    return hashlib.sha256(
        _DIFF_ID_DOMAIN
        + _canonical_json_bytes(
            identity
        )
    ).hexdigest()


def diff_lab_workspace_snapshots(
    before: LabWorkspaceSnapshot,
    after: LabWorkspaceSnapshot,
) -> LabWorkspaceDiff:
    """Derive physical ADDED/MODIFIED/DELETED evidence from two snapshots."""
    before = validate_lab_workspace_snapshot(
        before
    )

    after = validate_lab_workspace_snapshot(
        after
    )

    before_identity = (
        before.workspace_session_id,
        before.workspace_device,
        before.workspace_inode,
    )

    after_identity = (
        after.workspace_session_id,
        after.workspace_device,
        after.workspace_inode,
    )

    if before_identity != after_identity:
        raise LabWorkspaceSnapshotError(
            "workspace snapshots refer to different physical workspaces"
        )

    before_by_path = {
        item.path: item
        for item in before.entries
    }

    after_by_path = {
        item.path: item
        for item in after.entries
    }

    changes: list[
        LabWorkspaceDiffEntry
    ] = []

    for path in sorted(
        set(
            before_by_path
        )
        | set(
            after_by_path
        )
    ):
        before_entry = before_by_path.get(
            path
        )

        after_entry = after_by_path.get(
            path
        )

        if before_entry is None:
            change = CHANGE_ADDED

        elif after_entry is None:
            change = CHANGE_DELETED

        elif before_entry != after_entry:
            change = CHANGE_MODIFIED

        else:
            continue

        changes.append(
            LabWorkspaceDiffEntry(
                path=path,
                change=change,
                before=before_entry,
                after=after_entry,
            )
        )

    canonical_changes = tuple(
        changes
    )

    identity = _diff_identity_object(
        before=before,
        after=after,
        entries=canonical_changes,
    )

    return LabWorkspaceDiff(
        component=DIFF_COMPONENT,
        schema_version=DIFF_SCHEMA_VERSION,
        diff_id=_diff_id(
            identity
        ),
        workspace_session_id=before.workspace_session_id,
        workspace_device=before.workspace_device,
        workspace_inode=before.workspace_inode,
        before_snapshot_id=before.snapshot_id,
        after_snapshot_id=after.snapshot_id,
        entries=canonical_changes,
    )
