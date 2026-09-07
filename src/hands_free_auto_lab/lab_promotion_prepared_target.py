"""READ-ONLY classification of promotion target preparation state.

This module does not prepare, install, remove, restore, or otherwise mutate
promotion targets.

It validates durable recovery evidence and classifies deterministic after-image
temporary files while proving that:

- repository identity, branch, and HEAD still match the transaction,
- approved destination before-states remain intact,
- no tracked/index changes exist,
- no unrelated untracked files exist,
- expected temporary paths are not tracked or ignored,
- existing expected temporaries are securely inspected without following links.

Expected temporary files are classified as:

ABSENT
    The deterministic temporary path does not exist.

EXACT_TEMP
    It is an untracked, ordinary, euid-owned, single-link regular file whose
    bytes, SHA-256, and mode exactly match approved after-state evidence.

UNEXPECTED_TEMP
    The deterministic name exists, but its safe observable properties do not
    exactly match approved evidence.

UNEXPECTED_TEMP is evidence only. It confers no cleanup, overwrite, reuse,
installation, rollback, or recovery authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import PurePosixPath
import stat
import subprocess

from .lab_promotion_proposal import (
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
)
from .lab_promotion_recovery_materials import (
    LabPromotionRecoveryMaterialsError,
    load_promotion_recovery_materials,
)
from .lab_promotion_transaction_state import (
    LabPromotionTransaction,
    PROGRESS_PENDING,
    STATE_PREPARED,
    validate_lab_promotion_transaction,
)


PREPARED_TARGET_COMPONENT = (
    "hands-free-auto-lab-promotion-prepared-target-v1"
)
PREPARED_TARGET_SCHEMA_VERSION = 1

TEMP_ABSENT = "ABSENT"
TEMP_EXACT = "EXACT_TEMP"
TEMP_UNEXPECTED = "UNEXPECTED_TEMP"

TEMP_STATES = frozenset(
    {
        TEMP_ABSENT,
        TEMP_EXACT,
        TEMP_UNEXPECTED,
    }
)

GIT_BINARY = "/usr/bin/git"
GIT_TIMEOUT_SECONDS = 5.0
MAX_TEMP_BYTES = 8 * 1024 * 1024


class LabPromotionPreparedTargetError(
    RuntimeError
):
    """Prepared-target evidence is unsafe, invalid, or inconsistent."""


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionPreparedTemp:
    path: str
    temp_path: str
    status: str
    observed_bytes: int | None
    observed_sha256: str | None
    observed_mode: int | None


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionPreparedTargetInspection:
    component: str
    schema_version: int
    transaction_id: str
    materials_id: str
    repository_path: str
    repository_device: int
    repository_inode: int
    branch: str
    head: str
    temps: tuple[
        LabPromotionPreparedTemp,
        ...,
    ]


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

    if os.stat not in os.supports_dir_fd:
        missing.append(
            "os.stat(dir_fd=...)"
        )

    if missing:
        raise LabPromotionPreparedTargetError(
            "secure prepared-target primitives unavailable: "
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


def _require_repository_path(
    value: object,
) -> str:
    if (
        not isinstance(
            value,
            str,
        )
        or not value
    ):
        raise LabPromotionPreparedTargetError(
            "repository_path must be non-empty text"
        )

    if not os.path.isabs(
        value
    ):
        raise LabPromotionPreparedTargetError(
            "repository_path must be absolute"
        )

    normalized = os.path.normpath(
        value
    )

    if normalized != value:
        raise LabPromotionPreparedTargetError(
            "repository_path must be normalized"
        )

    if os.path.realpath(
        value
    ) != value:
        raise LabPromotionPreparedTargetError(
            "repository_path must not contain symlink components"
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
        or "\\" in value
    ):
        raise LabPromotionPreparedTargetError(
            "promotion path must be canonical relative POSIX text"
        )

    path = PurePosixPath(
        value
    )

    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(
            part in {
                "",
                ".",
                "..",
            }
            for part in path.parts
        )
    ):
        raise LabPromotionPreparedTargetError(
            f"unsafe promotion path {value!r}"
        )

    return value


def _open_repository(
    repository_path: str,
) -> tuple[
    int,
    os.stat_result,
]:
    repository_path = _require_repository_path(
        repository_path
    )

    current_fd = os.open(
        "/",
        _directory_flags(),
    )

    try:
        for component in PurePosixPath(
            repository_path
        ).parts[1:]:
            next_fd = os.open(
                component,
                _directory_flags(),
                dir_fd=current_fd,
            )

            os.close(
                current_fd
            )
            current_fd = next_fd

        state = os.fstat(
            current_fd
        )

        if not stat.S_ISDIR(
            state.st_mode
        ):
            raise LabPromotionPreparedTargetError(
                "promotion repository is not a directory"
            )

        if state.st_uid != os.geteuid():
            raise LabPromotionPreparedTargetError(
                "promotion repository is not owned by current euid"
            )

        return (
            current_fd,
            state,
        )

    except Exception:
        os.close(
            current_fd
        )
        raise


def _validate_git_directory(
    repository_fd: int,
) -> None:
    try:
        state = os.stat(
            ".git",
            dir_fd=repository_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LabPromotionPreparedTargetError(
            f"cannot inspect .git directory: {exc}"
        ) from exc

    if not stat.S_ISDIR(
        state.st_mode
    ):
        raise LabPromotionPreparedTargetError(
            ".git must be a real directory"
        )

    if state.st_uid != os.geteuid():
        raise LabPromotionPreparedTargetError(
            ".git is not owned by current euid"
        )


def _open_parent_directory(
    repository_fd: int,
    path: str,
) -> tuple[
    int,
    str,
]:
    path = _require_relative_path(
        path
    )

    parts = PurePosixPath(
        path
    ).parts

    parent_fd = os.dup(
        repository_fd
    )

    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                _directory_flags(),
                dir_fd=parent_fd,
            )

            state = os.fstat(
                next_fd
            )

            if (
                not stat.S_ISDIR(
                    state.st_mode
                )
                or state.st_uid
                != os.geteuid()
            ):
                os.close(
                    next_fd
                )
                raise LabPromotionPreparedTargetError(
                    f"unsafe parent directory for {path!r}"
                )

            os.close(
                parent_fd
            )
            parent_fd = next_fd

        return (
            parent_fd,
            parts[-1],
        )

    except Exception:
        os.close(
            parent_fd
        )
        raise


def _git_environment() -> dict[
    str,
    str,
]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _run_git(
    repository_path: str,
    *arguments: str,
    allowed_returncodes: tuple[
        int,
        ...,
    ] = (
        0,
    ),
) -> subprocess.CompletedProcess[
    bytes
]:
    command = [
        GIT_BINARY,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-C",
        repository_path,
        *arguments,
    ]

    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_git_environment(),
        )
    except (
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        raise LabPromotionPreparedTargetError(
            f"read-only Git inspection failed: {exc}"
        ) from exc

    if (
        completed.returncode
        not in allowed_returncodes
    ):
        stderr = completed.stderr.decode(
            "utf-8",
            errors="replace",
        ).strip()

        raise LabPromotionPreparedTargetError(
            "read-only Git inspection command failed"
            + (
                f": {stderr}"
                if stderr
                else ""
            )
        )

    return completed


def _single_git_line(
    raw: bytes,
    *,
    name: str,
) -> str:
    try:
        text = raw.decode(
            "utf-8"
        )
    except UnicodeDecodeError as exc:
        raise LabPromotionPreparedTargetError(
            f"{name} is not UTF-8"
        ) from exc

    lines = text.splitlines()

    if (
        len(
            lines
        )
        != 1
        or not lines[0]
    ):
        raise LabPromotionPreparedTargetError(
            f"{name} must contain one non-empty line"
        )

    return lines[0]


def _git_snapshot(
    transaction: LabPromotionTransaction,
) -> tuple[
    str,
    str,
    bytes,
]:
    repository_path = transaction.repository_path

    top_level = _single_git_line(
        _run_git(
            repository_path,
            "rev-parse",
            "--show-toplevel",
        ).stdout,
        name="Git top-level path",
    )

    if os.path.realpath(
        top_level
    ) != repository_path:
        raise LabPromotionPreparedTargetError(
            "Git top-level path does not match transaction repository"
        )

    branch_result = _run_git(
        repository_path,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        allowed_returncodes=(
            0,
            1,
        ),
    )

    if branch_result.returncode != 0:
        raise LabPromotionPreparedTargetError(
            "promotion requires an attached Git branch"
        )

    branch = _single_git_line(
        branch_result.stdout,
        name="Git branch",
    )

    head = _single_git_line(
        _run_git(
            repository_path,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ).stdout,
        name="Git HEAD",
    )

    status = _run_git(
        repository_path,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout

    if branch != transaction.branch:
        raise LabPromotionPreparedTargetError(
            "repository branch does not match transaction"
        )

    if head != transaction.head:
        raise LabPromotionPreparedTargetError(
            "repository HEAD does not match transaction"
        )

    return (
        branch,
        head,
        status,
    )


def _temp_relative_path(
    destination_path: str,
    temp_name: str,
) -> str:
    destination = PurePosixPath(
        _require_relative_path(
            destination_path
        )
    )

    if (
        not isinstance(
            temp_name,
            str,
        )
        or not temp_name
        or "/" in temp_name
        or "\\" in temp_name
        or temp_name in {
            ".",
            "..",
        }
    ):
        raise LabPromotionPreparedTargetError(
            "unsafe deterministic temporary basename"
        )

    parent = destination.parent

    if parent == PurePosixPath(
        "."
    ):
        return temp_name

    return (
        parent
        / temp_name
    ).as_posix()


def _expected_temp_paths(
    materials,
) -> tuple[
    str,
    ...,
]:
    values = tuple(
        _temp_relative_path(
            item.path,
            item.after_temp_name,
        )
        for item in materials.files
    )

    if len(
        values
    ) != len(
        set(
            values
        )
    ):
        raise LabPromotionPreparedTargetError(
            "duplicate deterministic temporary paths"
        )

    return values


def _validate_status(
    raw: bytes,
    *,
    expected_temp_paths: tuple[
        str,
        ...,
    ],
) -> frozenset[
    str
]:
    expected_bytes = {
        path.encode(
            "utf-8"
        )
        for path in expected_temp_paths
    }

    observed: set[
        bytes
    ] = set()

    for record in raw.split(
        b"\0"
    ):
        if not record:
            continue

        if not record.startswith(
            b"?? "
        ):
            raise LabPromotionPreparedTargetError(
                "promotion repository has tracked/index changes"
            )

        raw_path = record[
            3:
        ]

        if (
            not raw_path
            or raw_path not in expected_bytes
        ):
            raise LabPromotionPreparedTargetError(
                "promotion repository contains unrelated untracked state"
            )

        if raw_path in observed:
            raise LabPromotionPreparedTargetError(
                "duplicate Git status path"
            )

        observed.add(
            raw_path
        )

    try:
        return frozenset(
            value.decode(
                "utf-8"
            )
            for value in observed
        )
    except UnicodeDecodeError as exc:
        raise LabPromotionPreparedTargetError(
            "Git status path is not UTF-8"
        ) from exc


def _validate_temp_git_path(
    repository_path: str,
    temp_path: str,
) -> None:
    tracked = _run_git(
        repository_path,
        "ls-files",
        "--",
        temp_path,
    ).stdout

    if tracked:
        raise LabPromotionPreparedTargetError(
            f"deterministic temporary path is tracked: {temp_path!r}"
        )

    ignored = _run_git(
        repository_path,
        "check-ignore",
        "--quiet",
        "--no-index",
        "--",
        temp_path,
        allowed_returncodes=(
            0,
            1,
        ),
    )

    if ignored.returncode == 0:
        raise LabPromotionPreparedTargetError(
            f"deterministic temporary path is ignored: {temp_path!r}"
        )


def _read_exact_before(
    repository_fd: int,
    *,
    path: str,
    expected_bytes: int,
    expected_sha256: str,
    expected_mode: int,
) -> None:
    parent_fd, leaf = _open_parent_directory(
        repository_fd,
        path,
    )

    try:
        try:
            initial = os.stat(
                leaf,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LabPromotionPreparedTargetError(
                f"cannot inspect MODIFY destination {path!r}: {exc}"
            ) from exc

        if (
            not stat.S_ISREG(
                initial.st_mode
            )
            or stat.S_ISLNK(
                initial.st_mode
            )
        ):
            raise LabPromotionPreparedTargetError(
                f"MODIFY destination is not a regular file: {path!r}"
            )

        if initial.st_uid != os.geteuid():
            raise LabPromotionPreparedTargetError(
                f"MODIFY destination ownership mismatch: {path!r}"
            )

        if initial.st_nlink != 1:
            raise LabPromotionPreparedTargetError(
                f"MODIFY destination has unexpected hard links: {path!r}"
            )

        try:
            fd = os.open(
                leaf,
                _read_flags(),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise LabPromotionPreparedTargetError(
                f"cannot securely open MODIFY destination {path!r}: {exc}"
            ) from exc

        try:
            before = os.fstat(
                fd
            )

            if (
                before.st_dev
                != initial.st_dev
                or before.st_ino
                != initial.st_ino
                or before.st_mode
                != initial.st_mode
            ):
                raise LabPromotionPreparedTargetError(
                    f"MODIFY destination changed during open: {path!r}"
                )

            if stat.S_IMODE(
                before.st_mode
            ) != expected_mode:
                raise LabPromotionPreparedTargetError(
                    f"MODIFY before mode mismatch: {path!r}"
                )

            digest = hashlib.sha256()
            total = 0

            while True:
                raw = os.read(
                    fd,
                    1024 * 1024,
                )

                if not raw:
                    break

                total += len(
                    raw
                )

                if total > MAX_TEMP_BYTES:
                    raise LabPromotionPreparedTargetError(
                        f"MODIFY destination exceeds inspection limit: {path!r}"
                    )

                digest.update(
                    raw
                )

            after = os.fstat(
                fd
            )

            identity_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_uid",
                "st_nlink",
            )

            if any(
                getattr(
                    before,
                    field,
                )
                != getattr(
                    after,
                    field,
                )
                for field in identity_fields
            ):
                raise LabPromotionPreparedTargetError(
                    f"MODIFY destination changed while reading: {path!r}"
                )

            if total != expected_bytes:
                raise LabPromotionPreparedTargetError(
                    f"MODIFY before byte count mismatch: {path!r}"
                )

            if digest.hexdigest() != expected_sha256:
                raise LabPromotionPreparedTargetError(
                    f"MODIFY before SHA-256 mismatch: {path!r}"
                )

        finally:
            os.close(
                fd
            )

        final_path = os.stat(
            leaf,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )

        if (
            final_path.st_dev
            != initial.st_dev
            or final_path.st_ino
            != initial.st_ino
            or final_path.st_mode
            != initial.st_mode
            or final_path.st_size
            != initial.st_size
            or final_path.st_mtime_ns
            != initial.st_mtime_ns
            or final_path.st_ctime_ns
            != initial.st_ctime_ns
        ):
            raise LabPromotionPreparedTargetError(
                f"MODIFY destination changed after read: {path!r}"
            )

    finally:
        os.close(
            parent_fd
        )


def _require_add_absent(
    repository_fd: int,
    *,
    path: str,
) -> None:
    parent_fd, leaf = _open_parent_directory(
        repository_fd,
        path,
    )

    try:
        try:
            os.stat(
                leaf,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LabPromotionPreparedTargetError(
                f"cannot inspect ADD destination {path!r}: {exc}"
            ) from exc

        raise LabPromotionPreparedTargetError(
            f"ADD destination is no longer absent: {path!r}"
        )

    finally:
        os.close(
            parent_fd
        )


def _inspect_temp(
    repository_fd: int,
    *,
    destination_path: str,
    temp_path: str,
    expected_bytes: int,
    expected_sha256: str,
    expected_mode: int,
) -> LabPromotionPreparedTemp:
    parent_fd, leaf = _open_parent_directory(
        repository_fd,
        temp_path,
    )

    try:
        try:
            initial = os.stat(
                leaf,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return LabPromotionPreparedTemp(
                path=destination_path,
                temp_path=temp_path,
                status=TEMP_ABSENT,
                observed_bytes=None,
                observed_sha256=None,
                observed_mode=None,
            )
        except OSError as exc:
            raise LabPromotionPreparedTargetError(
                f"cannot inspect deterministic temporary {temp_path!r}: {exc}"
            ) from exc

        mode = stat.S_IMODE(
            initial.st_mode
        )

        if (
            stat.S_ISLNK(
                initial.st_mode
            )
            or not stat.S_ISREG(
                initial.st_mode
            )
            or initial.st_uid
            != os.geteuid()
            or initial.st_nlink
            != 1
        ):
            return LabPromotionPreparedTemp(
                path=destination_path,
                temp_path=temp_path,
                status=TEMP_UNEXPECTED,
                observed_bytes=initial.st_size,
                observed_sha256=None,
                observed_mode=mode,
            )

        try:
            fd = os.open(
                leaf,
                _read_flags(),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise LabPromotionPreparedTargetError(
                f"cannot securely open deterministic temporary {temp_path!r}: "
                f"{exc}"
            ) from exc

        try:
            before = os.fstat(
                fd
            )

            if (
                before.st_dev
                != initial.st_dev
                or before.st_ino
                != initial.st_ino
                or before.st_mode
                != initial.st_mode
            ):
                raise LabPromotionPreparedTargetError(
                    f"temporary changed during open: {temp_path!r}"
                )

            digest = hashlib.sha256()
            total = 0

            while True:
                raw = os.read(
                    fd,
                    1024 * 1024,
                )

                if not raw:
                    break

                total += len(
                    raw
                )

                if total > MAX_TEMP_BYTES:
                    return LabPromotionPreparedTemp(
                        path=destination_path,
                        temp_path=temp_path,
                        status=TEMP_UNEXPECTED,
                        observed_bytes=total,
                        observed_sha256=None,
                        observed_mode=mode,
                    )

                digest.update(
                    raw
                )

            after = os.fstat(
                fd
            )

            identity_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_uid",
                "st_nlink",
            )

            if any(
                getattr(
                    before,
                    field,
                )
                != getattr(
                    after,
                    field,
                )
                for field in identity_fields
            ):
                raise LabPromotionPreparedTargetError(
                    f"temporary changed while reading: {temp_path!r}"
                )

            observed_sha256 = digest.hexdigest()

        finally:
            os.close(
                fd
            )

        final_path = os.stat(
            leaf,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )

        if (
            final_path.st_dev
            != initial.st_dev
            or final_path.st_ino
            != initial.st_ino
            or final_path.st_mode
            != initial.st_mode
            or final_path.st_size
            != initial.st_size
            or final_path.st_mtime_ns
            != initial.st_mtime_ns
            or final_path.st_ctime_ns
            != initial.st_ctime_ns
        ):
            raise LabPromotionPreparedTargetError(
                f"temporary changed after read: {temp_path!r}"
            )

        status = (
            TEMP_EXACT
            if (
                total == expected_bytes
                and observed_sha256 == expected_sha256
                and mode == expected_mode
            )
            else TEMP_UNEXPECTED
        )

        return LabPromotionPreparedTemp(
            path=destination_path,
            temp_path=temp_path,
            status=status,
            observed_bytes=total,
            observed_sha256=observed_sha256,
            observed_mode=mode,
        )

    finally:
        os.close(
            parent_fd
        )


def inspect_promotion_prepared_target(
    *,
    recovery_root: str | os.PathLike[str],
    transaction: LabPromotionTransaction,
) -> LabPromotionPreparedTargetInspection:
    """Classify exact target preparation state without modifying the target."""
    try:
        transaction = validate_lab_promotion_transaction(
            transaction
        )
    except Exception as exc:
        raise LabPromotionPreparedTargetError(
            f"transaction validation failed: {exc}"
        ) from exc

    if transaction.state != STATE_PREPARED:
        raise LabPromotionPreparedTargetError(
            "prepared-target inspection requires PREPARED transaction"
        )

    if any(
        item.progress != PROGRESS_PENDING
        for item in transaction.files
    ):
        raise LabPromotionPreparedTargetError(
            "prepared-target inspection requires every file PENDING"
        )

    try:
        materials = load_promotion_recovery_materials(
            recovery_root,
            transaction=transaction,
        )
    except LabPromotionRecoveryMaterialsError as exc:
        raise LabPromotionPreparedTargetError(
            f"durable recovery evidence failed validation: {exc}"
        ) from exc

    repository_path = _require_repository_path(
        transaction.repository_path
    )

    repository_fd, initial_repository_state = _open_repository(
        repository_path
    )

    try:
        if (
            initial_repository_state.st_dev
            != transaction.repository_device
            or initial_repository_state.st_ino
            != transaction.repository_inode
        ):
            raise LabPromotionPreparedTargetError(
                "repository device/inode does not match transaction"
            )

        _validate_git_directory(
            repository_fd
        )

        expected_temp_paths = _expected_temp_paths(
            materials
        )

        for temp_path in expected_temp_paths:
            _validate_temp_git_path(
                repository_path,
                temp_path,
            )

        branch, head, initial_status = _git_snapshot(
            transaction
        )

        observed_untracked = _validate_status(
            initial_status,
            expected_temp_paths=expected_temp_paths,
        )

        temps: list[
            LabPromotionPreparedTemp
        ] = []

        for (
            transaction_file,
            material_file,
            temp_path,
        ) in zip(
            transaction.files,
            materials.files,
            expected_temp_paths,
            strict=True,
        ):
            if transaction_file.operation == PROMOTION_OPERATION_MODIFY:
                if (
                    transaction_file.before_bytes is None
                    or transaction_file.before_sha256 is None
                    or transaction_file.before_mode is None
                ):
                    raise LabPromotionPreparedTargetError(
                        f"MODIFY before-state missing for "
                        f"{transaction_file.path!r}"
                    )

                _read_exact_before(
                    repository_fd,
                    path=transaction_file.path,
                    expected_bytes=transaction_file.before_bytes,
                    expected_sha256=transaction_file.before_sha256,
                    expected_mode=transaction_file.before_mode,
                )

            elif transaction_file.operation == PROMOTION_OPERATION_ADD:
                _require_add_absent(
                    repository_fd,
                    path=transaction_file.path,
                )

            else:
                raise LabPromotionPreparedTargetError(
                    f"unsupported promotion operation "
                    f"{transaction_file.operation!r}"
                )

            result = _inspect_temp(
                repository_fd,
                destination_path=transaction_file.path,
                temp_path=temp_path,
                expected_bytes=material_file.after_bytes,
                expected_sha256=material_file.after_sha256,
                expected_mode=material_file.after_mode,
            )

            if (
                result.status == TEMP_ABSENT
                and temp_path in observed_untracked
            ):
                raise LabPromotionPreparedTargetError(
                    f"Git reports absent temporary as untracked: {temp_path!r}"
                )

            if (
                result.status != TEMP_ABSENT
                and temp_path not in observed_untracked
            ):
                raise LabPromotionPreparedTargetError(
                    f"existing temporary is not exact visible Git dirt: "
                    f"{temp_path!r}"
                )

            temps.append(
                result
            )

        final_branch, final_head, final_status = _git_snapshot(
            transaction
        )

        if (
            final_branch != branch
            or final_head != head
            or final_status != initial_status
        ):
            raise LabPromotionPreparedTargetError(
                "repository Git state changed during prepared-target inspection"
            )

        final_repository_state = os.fstat(
            repository_fd
        )

        if (
            final_repository_state.st_dev
            != initial_repository_state.st_dev
            or final_repository_state.st_ino
            != initial_repository_state.st_ino
            or final_repository_state.st_uid
            != initial_repository_state.st_uid
        ):
            raise LabPromotionPreparedTargetError(
                "repository identity changed during inspection"
            )

        final_path_state = os.stat(
            repository_path,
            follow_symlinks=False,
        )

        if (
            final_path_state.st_dev
            != final_repository_state.st_dev
            or final_path_state.st_ino
            != final_repository_state.st_ino
        ):
            raise LabPromotionPreparedTargetError(
                "repository path no longer names inspected repository"
            )

        return LabPromotionPreparedTargetInspection(
            component=PREPARED_TARGET_COMPONENT,
            schema_version=PREPARED_TARGET_SCHEMA_VERSION,
            transaction_id=transaction.transaction_id,
            materials_id=materials.materials_id,
            repository_path=repository_path,
            repository_device=final_repository_state.st_dev,
            repository_inode=final_repository_state.st_ino,
            branch=branch,
            head=head,
            temps=tuple(
                temps
            ),
        )

    finally:
        os.close(
            repository_fd
        )
