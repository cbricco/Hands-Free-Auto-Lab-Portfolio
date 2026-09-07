"""Read-only inspection of one prospective promotion repository.

This component crosses the lab/repository visibility boundary only for
inspection. It does not have repository-write authority.

It may:

- validate one exact repository identity,
- run fixed read-only Git inspection commands,
- require an attached branch and existing HEAD,
- require a completely clean working tree/index for the initial MVP,
- securely inspect exact candidate destination paths,
- hash existing regular files without following symlinks.

It does not:

- create, replace, rename, remove, or modify files,
- create directories,
- update the Git index,
- stage, commit, push, checkout, merge, reset, or clean,
- consume approval,
- execute model-selected commands,
- promote candidate content.

The initial MVP intentionally requires an ordinary repository with a real
.git directory, an attached branch, an existing commit, and a completely
clean status.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import stat
import subprocess

from .lab_coding_candidate import LabCodingCandidate
from .lab_promotion_candidate import (
    LabPromotionCandidate,
)
from .lab_promotion_proposal import (
    LabPromotionProposalError,
    LabPromotionRepositoryFileState,
    lab_promotion_candidate_identity,
)
from .lab_promotion_source import (
    LabPromotionSourceError,
    normalize_lab_promotion_source,
)


PROMOTION_INSPECTOR_COMPONENT = (
    "hands-free-auto-lab-promotion-inspector-v2"
)
PROMOTION_INSPECTOR_SCHEMA_VERSION = 2

GIT_BINARY = "/usr/bin/git"
GIT_TIMEOUT_SECONDS = 5.0

MAX_PROMOTION_BEFORE_BYTES = (
    8 * 1024 * 1024
)

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)


class LabPromotionInspectorError(RuntimeError):
    """Raised when repository inspection cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class LabPromotionRepositoryInspection:
    component: str
    schema_version: int
    candidate_id: str
    repository_path: str
    repository_device: int
    repository_inode: int
    branch: str
    head: str
    file_states: tuple[
        LabPromotionRepositoryFileState,
        ...
    ]


def _directory_flags() -> int:
    for name in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
    ):
        if not hasattr(
            os,
            name,
        ):
            raise LabPromotionInspectorError(
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


def _file_read_flags() -> int:
    if not hasattr(
        os,
        "O_NOFOLLOW",
    ):
        raise LabPromotionInspectorError(
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


def _require_repository_path(
    repository_path: object,
) -> str:
    if not isinstance(
        repository_path,
        str,
    ):
        raise LabPromotionInspectorError(
            "repository_path must be a string"
        )

    if not repository_path:
        raise LabPromotionInspectorError(
            "repository_path must not be empty"
        )

    if not os.path.isabs(
        repository_path
    ):
        raise LabPromotionInspectorError(
            "repository_path must be absolute"
        )

    if os.path.normpath(
        repository_path
    ) != repository_path:
        raise LabPromotionInspectorError(
            "repository_path must be normalized"
        )

    if os.path.realpath(
        repository_path
    ) != repository_path:
        raise LabPromotionInspectorError(
            "repository_path must be canonical and contain no symlink "
            "components"
        )

    return repository_path


def _open_repository_fd(
    repository_path: str,
) -> tuple[
    int,
    os.stat_result,
]:
    try:
        fd = os.open(
            repository_path,
            _directory_flags(),
        )
    except OSError as exc:
        raise LabPromotionInspectorError(
            f"cannot securely open repository: {exc}"
        ) from exc

    try:
        st = os.fstat(
            fd
        )

        if not stat.S_ISDIR(
            st.st_mode
        ):
            raise LabPromotionInspectorError(
                "repository is not a directory"
            )

        if st.st_uid != os.geteuid():
            raise LabPromotionInspectorError(
                "repository must be owned by the effective user"
            )

        try:
            git_state = os.stat(
                ".git",
                dir_fd=fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise LabPromotionInspectorError(
                "repository does not contain a .git directory"
            ) from exc
        except OSError as exc:
            raise LabPromotionInspectorError(
                f"cannot inspect .git directory: {exc}"
            ) from exc

        if not stat.S_ISDIR(
            git_state.st_mode
        ):
            raise LabPromotionInspectorError(
                "initial promotion MVP requires .git to be a real "
                "directory"
            )

        if git_state.st_uid != os.geteuid():
            raise LabPromotionInspectorError(
                ".git directory must be owned by the effective user"
            )

        return (
            fd,
            st,
        )

    except Exception:
        os.close(
            fd
        )
        raise


def _git_environment() -> dict[
    str,
    str,
]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_git(
    repository_path: str,
    *arguments: str,
    allowed_returncodes: tuple[
        int,
        ...
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
        subprocess.TimeoutExpired,
    ) as exc:
        raise LabPromotionInspectorError(
            f"fixed Git inspection command failed to execute: {exc}"
        ) from exc

    if (
        completed.returncode
        not in allowed_returncodes
    ):
        stderr = completed.stderr.decode(
            "utf-8",
            errors="replace",
        ).strip()

        raise LabPromotionInspectorError(
            "fixed Git inspection command failed"
            + (
                f": {stderr}"
                if stderr
                else ""
            )
        )

    return completed


def _decode_single_line(
    name: str,
    raw: bytes,
) -> str:
    try:
        text = raw.decode(
            "utf-8"
        )
    except UnicodeDecodeError as exc:
        raise LabPromotionInspectorError(
            f"{name} is not valid UTF-8"
        ) from exc

    text = text.rstrip(
        "\n"
    )

    if (
        not text
        or "\n" in text
        or "\r" in text
        or "\x00" in text
    ):
        raise LabPromotionInspectorError(
            f"{name} is malformed"
        )

    return text


def _validate_head(
    head: str,
) -> str:
    if (
        len(
            head
        )
        not in (
            40,
            64,
        )
        or any(
            character not in _HEX_LOWER
            for character in head
        )
    ):
        raise LabPromotionInspectorError(
            "Git HEAD is not a canonical lowercase object ID"
        )

    return head


def _git_snapshot(
    repository_path: str,
) -> tuple[
    str,
    str,
]:
    top_level = _decode_single_line(
        "Git top-level path",
        _run_git(
            repository_path,
            "rev-parse",
            "--show-toplevel",
        ).stdout,
    )

    if top_level != repository_path:
        raise LabPromotionInspectorError(
            "Git top-level path does not match requested repository"
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
        raise LabPromotionInspectorError(
            "promotion requires an attached Git branch"
        )

    branch = _decode_single_line(
        "Git branch",
        branch_result.stdout,
    )

    head = _validate_head(
        _decode_single_line(
            "Git HEAD",
            _run_git(
                repository_path,
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ).stdout,
        )
    )

    status = _run_git(
        repository_path,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout

    if status:
        raise LabPromotionInspectorError(
            "promotion repository must have a completely clean "
            "working tree and index"
        )

    return (
        branch,
        head,
    )


def _open_parent_directory(
    repository_fd: int,
    components: tuple[
        str,
        ...
    ],
) -> int:
    current_fd = os.dup(
        repository_fd
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
                raise LabPromotionInspectorError(
                    "cannot securely open promotion destination parent "
                    f"component {component!r}: {exc}"
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


def _git_path_is_tracked(
    repository_path: str,
    path: str,
) -> bool:
    result = _run_git(
        repository_path,
        "ls-files",
        "--error-unmatch",
        "--",
        path,
        allowed_returncodes=(
            0,
            1,
        ),
    )

    return result.returncode == 0


def _git_path_is_ignored(
    repository_path: str,
    path: str,
) -> bool:
    result = _run_git(
        repository_path,
        "check-ignore",
        "-q",
        "--",
        path,
        allowed_returncodes=(
            0,
            1,
        ),
    )

    return result.returncode == 0


def _existing_file_state(
    parent_fd: int,
    name: str,
    path: str,
    initial_state: os.stat_result,
) -> LabPromotionRepositoryFileState:
    if stat.S_ISLNK(
        initial_state.st_mode
    ):
        raise LabPromotionInspectorError(
            f"promotion destination must not be a symlink: {path!r}"
        )

    if not stat.S_ISREG(
        initial_state.st_mode
    ):
        raise LabPromotionInspectorError(
            f"promotion destination must be a regular file: {path!r}"
        )

    if initial_state.st_nlink != 1:
        raise LabPromotionInspectorError(
            f"promotion destination must not be hard-linked: {path!r}"
        )

    try:
        fd = os.open(
            name,
            _file_read_flags(),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise LabPromotionInspectorError(
            f"cannot securely open promotion destination {path!r}: {exc}"
        ) from exc

    try:
        before = os.fstat(
            fd
        )

        if not stat.S_ISREG(
            before.st_mode
        ):
            raise LabPromotionInspectorError(
                f"opened promotion destination is not regular: {path!r}"
            )

        if before.st_nlink != 1:
            raise LabPromotionInspectorError(
                f"promotion destination must not be hard-linked: {path!r}"
            )

        if (
            before.st_dev
            != initial_state.st_dev
            or before.st_ino
            != initial_state.st_ino
        ):
            raise LabPromotionInspectorError(
                f"promotion destination changed during open: {path!r}"
            )

        digest = hashlib.sha256()
        byte_count = 0

        while True:
            try:
                chunk = os.read(
                    fd,
                    64 * 1024,
                )
            except OSError as exc:
                raise LabPromotionInspectorError(
                    f"cannot read promotion destination {path!r}: {exc}"
                ) from exc

            if not chunk:
                break

            byte_count += len(
                chunk
            )

            if (
                byte_count
                > MAX_PROMOTION_BEFORE_BYTES
            ):
                raise LabPromotionInspectorError(
                    "promotion destination exceeds initial before-state "
                    f"size limit: {path!r}"
                )

            digest.update(
                chunk
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
            raise LabPromotionInspectorError(
                f"promotion destination changed while reading: {path!r}"
            )

        if byte_count != after.st_size:
            raise LabPromotionInspectorError(
                f"promotion destination size changed while reading: {path!r}"
            )

        try:
            final_path_state = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LabPromotionInspectorError(
                "cannot revalidate promotion destination path "
                f"{path!r}: {exc}"
            ) from exc

        if (
            not stat.S_ISREG(
                final_path_state.st_mode
            )
            or final_path_state.st_dev
            != after.st_dev
            or final_path_state.st_ino
            != after.st_ino
            or final_path_state.st_mode
            != after.st_mode
            or final_path_state.st_nlink != 1
            or after.st_nlink != 1
        ):
            raise LabPromotionInspectorError(
                f"promotion destination path changed after read: {path!r}"
            )

        mode = stat.S_IMODE(
            after.st_mode
        )

        if mode & ~0o777:
            raise LabPromotionInspectorError(
                "promotion v2 refuses setuid, setgid, or sticky mode bits: "
                f"{path!r}"
            )

        if mode & 0o022:
            raise LabPromotionInspectorError(
                "promotion destination must not be group- or other-writable: "
                f"{path!r}"
            )

        if mode & 0o111:
            raise LabPromotionInspectorError(
                f"promotion destination must not be executable: {path!r}"
            )

        return LabPromotionRepositoryFileState(
            path=path,
            exists=True,
            bytes=byte_count,
            sha256=digest.hexdigest(),
            mode=mode,
        )

    finally:
        os.close(
            fd
        )


def _inspect_file_state(
    *,
    repository_path: str,
    repository_fd: int,
    path: str,
    source_file: object,
) -> LabPromotionRepositoryFileState:
    components = tuple(
        path.split(
            "/"
        )
    )

    if (
        not components
        or any(
            component in (
                "",
                ".",
                "..",
            )
            for component in components
        )
    ):
        raise LabPromotionInspectorError(
            f"candidate path is not canonical: {path!r}"
        )

    parent_fd = _open_parent_directory(
        repository_fd,
        components[:-1],
    )

    name = components[-1]

    try:
        try:
            initial_state = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            initial_state = None
        except OSError as exc:
            raise LabPromotionInspectorError(
                f"cannot inspect promotion destination {path!r}: {exc}"
            ) from exc

        ignored = _git_path_is_ignored(
            repository_path,
            path,
        )

        if ignored:
            raise LabPromotionInspectorError(
                f"candidate path is ignored by Git: {path!r}"
            )

        tracked = _git_path_is_tracked(
            repository_path,
            path,
        )

        if initial_state is None:
            if source_file.operation == "MODIFIED":
                raise LabPromotionInspectorError(
                    "MODIFIED promotion destination must be present: "
                    f"{path!r}"
                )

            if tracked:
                raise LabPromotionInspectorError(
                    "tracked candidate destination is unexpectedly "
                    f"absent: {path!r}"
                )

            return LabPromotionRepositoryFileState(
                path=path,
                exists=False,
                bytes=None,
                sha256=None,
                mode=None,
            )

        if source_file.operation == "ADDED":
            raise LabPromotionInspectorError(
                "ADDED promotion destination must be absent: "
                f"{path!r}"
            )

        state = _existing_file_state(
            parent_fd,
            name,
            path,
            initial_state,
        )

        if not tracked:
            raise LabPromotionInspectorError(
                "existing candidate destination is not tracked by Git: "
                f"{path!r}"
            )

        if source_file.operation == "MODIFIED" and (
            source_file.before_bytes,
            source_file.before_sha256,
            source_file.before_mode,
        ) != (
            state.bytes,
            state.sha256,
            state.mode,
        ):
            raise LabPromotionInspectorError(
                "MODIFIED coding candidate before evidence does not match "
                f"the inspected destination: {path!r}"
            )

        return state

    finally:
        os.close(
            parent_fd
        )


def inspect_lab_promotion_repository(
    *,
    candidate: LabPromotionCandidate | LabCodingCandidate,
    repository_path: str,
) -> LabPromotionRepositoryInspection:
    """Inspect one exact clean repository without modifying it."""
    try:
        source = normalize_lab_promotion_source(candidate)
        if type(candidate) is LabPromotionCandidate:
            candidate_id = lab_promotion_candidate_identity(candidate)
        elif type(candidate) is LabCodingCandidate:
            candidate_id = candidate.candidate_id
        else:
            raise TypeError("unsupported promotion candidate type")
    except (
        TypeError,
        LabPromotionProposalError,
        LabPromotionSourceError,
    ) as exc:
        raise LabPromotionInspectorError(
            f"candidate validation failed: {exc}"
        ) from exc

    repository_path = _require_repository_path(
        repository_path
    )

    repository_fd, initial_repository_state = (
        _open_repository_fd(
            repository_path
        )
    )

    try:
        branch, head = _git_snapshot(
            repository_path
        )

        file_states = tuple(
            _inspect_file_state(
                repository_path=repository_path,
                repository_fd=repository_fd,
                path=item.path,
                source_file=item,
            )
            for item in source.files
        )

        final_branch, final_head = _git_snapshot(
            repository_path
        )

        if (
            final_branch != branch
            or final_head != head
        ):
            raise LabPromotionInspectorError(
                "repository Git identity changed during inspection"
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
            raise LabPromotionInspectorError(
                "repository identity changed during inspection"
            )

        try:
            path_state = os.stat(
                repository_path,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LabPromotionInspectorError(
                f"cannot revalidate repository path: {exc}"
            ) from exc

        if (
            path_state.st_dev
            != final_repository_state.st_dev
            or path_state.st_ino
            != final_repository_state.st_ino
        ):
            raise LabPromotionInspectorError(
                "repository path no longer names inspected repository"
            )

        return LabPromotionRepositoryInspection(
            component=(
                PROMOTION_INSPECTOR_COMPONENT
            ),
            schema_version=(
                PROMOTION_INSPECTOR_SCHEMA_VERSION
            ),
            candidate_id=candidate_id,
            repository_path=repository_path,
            repository_device=(
                final_repository_state.st_dev
            ),
            repository_inode=(
                final_repository_state.st_ino
            ),
            branch=branch,
            head=head,
            file_states=file_states,
        )

    finally:
        os.close(
            repository_fd
        )
