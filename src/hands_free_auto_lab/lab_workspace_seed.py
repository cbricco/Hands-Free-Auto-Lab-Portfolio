from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from .lab_workspace import LabWorkspace, validate_lab_workspace


WORKSPACE_SEED_COMPONENT = "hands-free-auto-lab-workspace-seed-v2"
WORKSPACE_SEED_SCHEMA_VERSION = 2


class LabWorkspaceSeedError(RuntimeError):
    """A reviewed file manifest could not be copied safely."""


@dataclass(frozen=True, slots=True)
class LabWorkspaceSeedEntry:
    relative_path: str
    size: int
    sha256: str
    mode: int
    source_mode: int


@dataclass(frozen=True, slots=True)
class LabWorkspaceSeedRecord:
    component: str
    schema_version: int
    source_root: str
    workspace: LabWorkspace
    entries: tuple[LabWorkspaceSeedEntry, ...]
    total_bytes: int


def _positive_int(
    value: object,
    *,
    name: str,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
    ):
        raise LabWorkspaceSeedError(
            f"{name} must be a positive integer"
        )

    return value


def _source_root(
    value: object,
) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
    ):
        raise LabWorkspaceSeedError(
            "source_root must be a non-empty string without NUL"
        )

    path = Path(value)

    if (
        not path.is_absolute()
        or str(path) != value
    ):
        raise LabWorkspaceSeedError(
            "source_root must be an absolute normalized path"
        )

    try:
        resolved = path.resolve(
            strict=True
        )
        info = path.lstat()

    except OSError as exc:
        raise LabWorkspaceSeedError(
            f"cannot inspect source_root: {exc}"
        ) from exc

    if (
        resolved != path
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise LabWorkspaceSeedError(
            "source_root must be a canonical directory"
        )

    if (
        info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise LabWorkspaceSeedError(
            "source_root has unsafe ownership or permissions"
        )

    return path


def _relative_paths(
    value: object,
    *,
    max_files: int,
) -> tuple[str, ...]:
    if (
        not isinstance(value, tuple)
        or not value
    ):
        raise LabWorkspaceSeedError(
            "relative_paths must be a non-empty tuple"
        )

    if len(value) > max_files:
        raise LabWorkspaceSeedError(
            "relative_paths exceeds max_files"
        )

    paths: list[str] = []

    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or "\x00" in item
        ):
            raise LabWorkspaceSeedError(
                "relative paths must be non-empty strings without NUL"
            )

        parts = item.split("/")

        if (
            item.startswith("/")
            or any(
                part in ("", ".", "..")
                for part in parts
            )
        ):
            raise LabWorkspaceSeedError(
                "relative paths must be normalized and traversal-free"
            )

        if parts[0] == ".git":
            raise LabWorkspaceSeedError(
                ".git content must never be seeded"
            )

        paths.append(
            item
        )

    if len(set(paths)) != len(paths):
        raise LabWorkspaceSeedError(
            "relative_paths must not contain duplicates"
        )

    return tuple(
        sorted(paths)
    )


def _inspect_source(
    source_root: Path,
    relative_path: str,
) -> os.stat_result:
    path = (
        source_root
        / relative_path
    )

    try:
        resolved = path.resolve(
            strict=True
        )
        info = path.lstat()

    except OSError as exc:
        raise LabWorkspaceSeedError(
            f"cannot inspect source file {relative_path}: {exc}"
        ) from exc

    try:
        resolved.relative_to(
            source_root
        )

    except ValueError as exc:
        raise LabWorkspaceSeedError(
            "source file escapes source_root"
        ) from exc

    if resolved != path:
        raise LabWorkspaceSeedError(
            "source file path must not contain symlinks"
        )

    if not stat.S_ISREG(
        info.st_mode
    ):
        raise LabWorkspaceSeedError(
            "seed inputs must be regular files"
        )

    if (
        info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise LabWorkspaceSeedError(
            "seed input has unsafe ownership or permissions"
        )

    return info


def _copy_one(
    source_root: Path,
    workspace_path: Path,
    relative_path: str,
    expected: os.stat_result,
) -> LabWorkspaceSeedEntry:
    source_path = (
        source_root
        / relative_path
    )

    target_path = (
        workspace_path
        / relative_path
    )

    target_path.parent.mkdir(
        mode=0o700,
        parents=True,
        exist_ok=True,
    )

    try:
        source_fd = os.open(
            source_path,
            os.O_RDONLY
            | os.O_NOFOLLOW,
        )

    except OSError as exc:
        raise LabWorkspaceSeedError(
            f"cannot open source file {relative_path}: {exc}"
        ) from exc

    try:
        initial = os.fstat(
            source_fd
        )

        if (
            initial.st_dev != expected.st_dev
            or initial.st_ino != expected.st_ino
            or initial.st_size != expected.st_size
            or initial.st_mtime_ns != expected.st_mtime_ns
        ):
            raise LabWorkspaceSeedError(
                "source file changed after preflight"
            )

        source_mode = stat.S_IMODE(initial.st_mode)
        mode = (
            0o700
            if source_mode & 0o100
            else 0o600
        )

        try:
            target_fd = os.open(
                target_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW,
                mode,
            )

        except OSError as exc:
            raise LabWorkspaceSeedError(
                f"cannot create target file {relative_path}: {exc}"
            ) from exc

        digest = hashlib.sha256()
        copied = 0

        try:
            while True:
                chunk = os.read(
                    source_fd,
                    1024 * 1024,
                )

                if not chunk:
                    break

                digest.update(
                    chunk
                )

                offset = 0

                while offset < len(chunk):
                    written = os.write(
                        target_fd,
                        chunk[offset:],
                    )

                    if written <= 0:
                        raise LabWorkspaceSeedError(
                            "target write made no progress"
                        )

                    offset += written

                copied += len(
                    chunk
                )

            os.fsync(
                target_fd
            )

        finally:
            os.close(
                target_fd
            )

        final = os.fstat(
            source_fd
        )

        if (
            final.st_dev != initial.st_dev
            or final.st_ino != initial.st_ino
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or copied != initial.st_size
        ):
            raise LabWorkspaceSeedError(
                "source changed during copy; "
                "disposable workspace must be discarded"
            )

        return LabWorkspaceSeedEntry(
            relative_path=relative_path,
            size=copied,
            sha256=digest.hexdigest(),
            mode=mode,
            source_mode=source_mode,
        )

    finally:
        os.close(
            source_fd
        )


def seed_lab_workspace(
    *,
    source_root: str,
    relative_paths: tuple[str, ...],
    workspace_parent: str,
    workspace: LabWorkspace,
    max_files: int,
    max_total_bytes: int,
) -> LabWorkspaceSeedRecord:
    """Copy only an explicit reviewed manifest into one empty lab workspace."""
    max_files = _positive_int(
        max_files,
        name="max_files",
    )

    max_total_bytes = _positive_int(
        max_total_bytes,
        name="max_total_bytes",
    )

    paths = _relative_paths(
        relative_paths,
        max_files=max_files,
    )

    source = _source_root(
        source_root
    )

    workspace = validate_lab_workspace(
        workspace_parent,
        workspace,
    )

    workspace_path = Path(
        workspace.path
    )

    if any(
        workspace_path.iterdir()
    ):
        raise LabWorkspaceSeedError(
            "workspace must be empty before seeding"
        )

    inspected: dict[str, os.stat_result] = {}
    total = 0

    for relative_path in paths:
        info = _inspect_source(
            source,
            relative_path,
        )

        total += info.st_size

        if total > max_total_bytes:
            raise LabWorkspaceSeedError(
                "seed inputs exceed max_total_bytes"
            )

        inspected[
            relative_path
        ] = info

    entries = tuple(
        _copy_one(
            source,
            workspace_path,
            relative_path,
            inspected[relative_path],
        )
        for relative_path in paths
    )

    validate_lab_workspace(
        workspace_parent,
        workspace,
    )

    return LabWorkspaceSeedRecord(
        component=WORKSPACE_SEED_COMPONENT,
        schema_version=WORKSPACE_SEED_SCHEMA_VERSION,
        source_root=str(source),
        workspace=workspace,
        entries=entries,
        total_bytes=sum(
            entry.size
            for entry in entries
        ),
    )
