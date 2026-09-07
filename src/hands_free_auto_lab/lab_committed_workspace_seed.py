"""Seed a private Auto Lab workspace from exact verified Git commit objects."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from .lab_workspace import LabWorkspace, validate_lab_workspace


COMMITTED_WORKSPACE_SEED_COMPONENT = "hands-free-auto-lab-committed-workspace-seed-v1"
COMMITTED_WORKSPACE_SEED_SCHEMA_VERSION = 1
GIT_BINARY = "/usr/bin/git"
GIT_TIMEOUT_SECONDS = 30
_MAX_COMMIT_BYTES = 1024 * 1024
_HEX = frozenset("0123456789abcdef")


class LabCommittedWorkspaceSeedError(RuntimeError):
    """Committed source state could not be proven and materialized safely."""


@dataclass(frozen=True, slots=True)
class LabCommittedWorkspaceSeedEntry:
    relative_path: str
    blob_oid: str
    size: int
    sha256: str
    mode: int
    source_mode: int
    git_mode: str


@dataclass(frozen=True, slots=True)
class LabCommittedWorkspaceSeedRecord:
    component: str
    schema_version: int
    repository_path: str
    repository_device: int
    repository_inode: int
    git_device: int
    git_inode: int
    branch: str
    commit_oid: str
    object_format: str
    workspace: LabWorkspace
    entries: tuple[LabCommittedWorkspaceSeedEntry, ...]
    total_bytes: int


@dataclass(frozen=True, slots=True)
class _Plan:
    path: str
    oid: str
    size: int
    git_mode: str
    source_mode: int
    workspace_mode: int


def _fail(message: str) -> None:
    raise LabCommittedWorkspaceSeedError(message)


def _positive(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"{name} must be a positive integer")
    return value


def _text(value: object, name: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail(f"{name} must be non-empty text without NUL")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LabCommittedWorkspaceSeedError(
            f"{name} must be UTF-8 encodable"
        ) from exc
    if len(raw) > maximum:
        _fail(f"{name} exceeds maximum size")
    return value


def _repository(value: object) -> str:
    value = _text(value, "repository_path")
    if (
        not os.path.isabs(value)
        or os.path.normpath(value) != value
        or os.path.realpath(value) != value
    ):
        _fail("repository_path must be canonical, absolute, and symlink-free")
    return value


def _oid(value: object) -> str:
    value = _text(value, "commit_oid", 40)
    if len(value) != 40 or any(char not in _HEX for char in value):
        _fail("commit_oid must be an exact lowercase SHA-1 object ID")
    return value


def _paths(value: object, max_files: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        _fail("relative_paths must be a non-empty tuple")
    if len(value) > max_files:
        _fail("relative_paths exceeds max_files")
    result: list[str] = []
    for item in value:
        item = _text(item, "relative path")
        parsed = PurePosixPath(item)
        if (
            parsed.is_absolute()
            or str(parsed) != item
            or any(part in ("", ".", "..") for part in item.split("/"))
        ):
            _fail("relative paths must be canonical traversal-free POSIX paths")
        if parsed.parts[0] == ".git":
            _fail(".git content must never be seeded")
        result.append(item)
    if len(set(result)) != len(result):
        _fail("relative_paths must not contain duplicates")
    return tuple(sorted(result))


def _dir_flags() -> int:
    flags = os.O_RDONLY
    for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        flags |= getattr(os, name, 0)
    return flags


@dataclass(frozen=True, slots=True)
class _GitLayout:
    linked_worktree: bool
    marker_state: os.stat_result
    marker_bytes: bytes | None
    git_dir: str
    git_dir_state: os.stat_result
    common_dir: str
    common_dir_state: os.stat_result
    commondir_state: os.stat_result | None
    commondir_bytes: bytes | None
    gitdir_state: os.stat_result | None
    gitdir_bytes: bytes | None


def _state_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        stat.S_IFMT(value.st_mode),
        stat.S_IMODE(value.st_mode),
    )


def _read_owned_regular_at(
    directory_fd: int,
    relative: str,
    label: str,
    *,
    maximum: int = 4096,
) -> tuple[os.stat_result, bytes]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

    try:
        fd = os.open(
            relative,
            flags,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise LabCommittedWorkspaceSeedError(
            f"cannot securely open {label}: {exc}"
        ) from exc

    try:
        info = os.fstat(fd)

        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
        ):
            _fail(
                f"{label} must be a real regular file "
                "owned by current euid"
            )

        if info.st_size > maximum:
            _fail(
                f"{label} exceeds bounded metadata size"
            )

        remaining = info.st_size
        chunks: list[bytes] = []

        while remaining:
            chunk = os.read(
                fd,
                min(65536, remaining),
            )

            if not chunk:
                _fail(
                    f"{label} changed during bounded read"
                )

            chunks.append(chunk)
            remaining -= len(chunk)

        raw = b"".join(chunks)

        if (
            len(raw) != info.st_size
            or os.read(fd, 1)
        ):
            _fail(
                f"{label} changed during bounded read"
            )

        return info, raw

    finally:
        os.close(fd)


def _open_owned_directory(
    path: str,
    label: str,
) -> tuple[int, os.stat_result]:
    if (
        not os.path.isabs(path)
        or os.path.normpath(path) != path
        or os.path.realpath(path) != path
    ):
        _fail(
            f"{label} must be canonical, absolute, and symlink-free"
        )

    try:
        fd = os.open(
            path,
            _dir_flags(),
        )
    except OSError as exc:
        raise LabCommittedWorkspaceSeedError(
            f"cannot securely open {label}: {exc}"
        ) from exc

    try:
        opened = os.fstat(fd)

        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
        ):
            _fail(
                f"{label} must be a real directory "
                "owned by current euid"
            )

        named = os.stat(
            path,
            follow_symlinks=False,
        )

        if _state_identity(named) != _state_identity(opened):
            _fail(
                f"{label} identity changed while opening"
            )

        return fd, opened

    except Exception:
        os.close(fd)
        raise


def _inspect_git_layout(
    path: str,
    repository_fd: int,
) -> _GitLayout:
    try:
        marker = os.stat(
            ".git",
            dir_fd=repository_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LabCommittedWorkspaceSeedError(
            f"cannot inspect repository .git marker: {exc}"
        ) from exc

    if marker.st_uid != os.geteuid():
        _fail(
            ".git must be owned by current euid"
        )

    marker_path = os.path.join(
        path,
        ".git",
    )

    if stat.S_ISDIR(marker.st_mode):
        if os.path.realpath(marker_path) != marker_path:
            _fail(
                ".git directory must be symlink-free"
            )

        return _GitLayout(
            linked_worktree=False,
            marker_state=marker,
            marker_bytes=None,
            git_dir=marker_path,
            git_dir_state=marker,
            common_dir=marker_path,
            common_dir_state=marker,
            commondir_state=None,
            commondir_bytes=None,
            gitdir_state=None,
            gitdir_bytes=None,
        )

    if not stat.S_ISREG(marker.st_mode):
        _fail(
            ".git must be either a real directory or a "
            "validated linked-worktree pointer file owned by current euid"
        )

    marker_read_state, marker_bytes = _read_owned_regular_at(
        repository_fd,
        ".git",
        "linked-worktree .git pointer",
    )

    if (
        _state_identity(marker_read_state)
        != _state_identity(marker)
    ):
        _fail(
            "linked-worktree .git pointer identity changed during read"
        )

    try:
        marker_text = marker_bytes.decode(
            "utf-8",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise LabCommittedWorkspaceSeedError(
            "linked-worktree .git pointer must be strict UTF-8"
        ) from exc

    if (
        not marker_text.startswith("gitdir: ")
        or not marker_text.endswith("\n")
        or marker_text.count("\n") != 1
    ):
        _fail(
            "linked-worktree .git pointer is malformed"
        )

    git_dir = marker_text[
        len("gitdir: ") : -1
    ]

    if (
        not git_dir
        or "\x00" in git_dir
        or not os.path.isabs(git_dir)
        or os.path.normpath(git_dir) != git_dir
        or os.path.realpath(git_dir) != git_dir
    ):
        _fail(
            "linked-worktree Git directory pointer must be "
            "canonical, absolute, and symlink-free"
        )

    git_fd, git_state = _open_owned_directory(
        git_dir,
        "linked-worktree Git directory",
    )

    try:
        commondir_state, commondir_bytes = (
            _read_owned_regular_at(
                git_fd,
                "commondir",
                "linked-worktree commondir metadata",
            )
        )

        gitdir_state, gitdir_bytes = (
            _read_owned_regular_at(
                git_fd,
                "gitdir",
                "linked-worktree gitdir metadata",
            )
        )

    finally:
        os.close(git_fd)

    if commondir_bytes != b"../..\n":
        _fail(
            "linked-worktree commondir metadata must be "
            "the standard ../.. relationship"
        )

    common_dir = os.path.normpath(
        os.path.join(
            git_dir,
            "../..",
        )
    )

    if (
        not os.path.isabs(common_dir)
        or os.path.realpath(common_dir) != common_dir
    ):
        _fail(
            "linked-worktree common Git directory must be "
            "canonical, absolute, and symlink-free"
        )

    if (
        Path(git_dir).parent
        != Path(common_dir) / "worktrees"
    ):
        _fail(
            "linked-worktree Git directory must be directly "
            "inside the common Git worktrees directory"
        )

    common_fd, common_state = _open_owned_directory(
        common_dir,
        "linked-worktree common Git directory",
    )
    os.close(common_fd)

    expected_back_pointer = (
        f"{marker_path}\n"
    ).encode(
        "utf-8",
        errors="strict",
    )

    if gitdir_bytes != expected_back_pointer:
        _fail(
            "linked-worktree gitdir back-pointer does not "
            "match repository .git marker"
        )

    return _GitLayout(
        linked_worktree=True,
        marker_state=marker_read_state,
        marker_bytes=marker_bytes,
        git_dir=git_dir,
        git_dir_state=git_state,
        common_dir=common_dir,
        common_dir_state=common_state,
        commondir_state=commondir_state,
        commondir_bytes=commondir_bytes,
        gitdir_state=gitdir_state,
        gitdir_bytes=gitdir_bytes,
    )


def _normalize_reported_git_path(
    repository_path: str,
    value: str,
    label: str,
) -> str:
    if not os.path.isabs(value):
        value = os.path.normpath(
            os.path.join(
                repository_path,
                value,
            )
        )

    if (
        not os.path.isabs(value)
        or os.path.normpath(value) != value
        or os.path.realpath(value) != value
    ):
        _fail(
            f"{label} reported by Git is not canonical and symlink-free"
        )

    return value


def _layout_identity_matches(
    left: _GitLayout,
    right: _GitLayout,
) -> bool:
    if (
        left.linked_worktree != right.linked_worktree
        or left.git_dir != right.git_dir
        or left.common_dir != right.common_dir
        or left.marker_bytes != right.marker_bytes
        or left.commondir_bytes != right.commondir_bytes
        or left.gitdir_bytes != right.gitdir_bytes
    ):
        return False

    pairs = (
        (
            left.marker_state,
            right.marker_state,
        ),
        (
            left.git_dir_state,
            right.git_dir_state,
        ),
        (
            left.common_dir_state,
            right.common_dir_state,
        ),
    )

    if any(
        _state_identity(first)
        != _state_identity(second)
        for first, second in pairs
    ):
        return False

    optional_pairs = (
        (
            left.commondir_state,
            right.commondir_state,
        ),
        (
            left.gitdir_state,
            right.gitdir_state,
        ),
    )

    for first, second in optional_pairs:
        if (first is None) != (second is None):
            return False

        if (
            first is not None
            and second is not None
            and _state_identity(first)
            != _state_identity(second)
        ):
            return False

    return True


def _open_repository(
    path: str,
) -> tuple[
    int,
    os.stat_result,
    _GitLayout,
]:
    try:
        fd = os.open(
            path,
            _dir_flags(),
        )
    except OSError as exc:
        raise LabCommittedWorkspaceSeedError(
            f"cannot securely open repository: {exc}"
        ) from exc

    try:
        repo = os.fstat(fd)

        if (
            not stat.S_ISDIR(repo.st_mode)
            or repo.st_uid != os.geteuid()
        ):
            _fail(
                "repository must be an ordinary directory "
                "owned by current euid"
            )

        layout = _inspect_git_layout(
            path,
            fd,
        )

        return fd, repo, layout

    except Exception:
        os.close(fd)
        raise


def _git_env() -> dict[str, str]:
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
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_LITERAL_PATHSPECS": "1",
    }


def _git(
    path: str,
    *args: str,
    allowed: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            [
                GIT_BINARY,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                f"core.worktree={path}",
                "-c",
                "core.bare=false",
                "-C",
                path,
                *args,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LabCommittedWorkspaceSeedError(
            f"fixed Git read command failed to execute: {exc}"
        ) from exc
    if result.returncode not in allowed:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        _fail(
            "fixed Git read command failed"
            + (f": {detail}" if detail else "")
        )
    return result


def _line(raw: bytes, name: str) -> str:
    if not raw or b"\x00" in raw:
        _fail(f"{name} is not one canonical line")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LabCommittedWorkspaceSeedError(
            f"{name} is not UTF-8"
        ) from exc
    if text.endswith("\n"):
        text = text[:-1]
    if not text or "\n" in text or "\r" in text:
        _fail(f"{name} is not one canonical line")
    return text


def _reject_indirections(
    path: str,
    layout: _GitLayout,
) -> None:
    common_root = Path(
        layout.common_dir
    )

    def check_directory(
        root: Path,
        relative: str,
    ) -> None:
        candidate = root / relative

        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return

        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
        ):
            _fail(
                f"{candidate} must be a real directory "
                "owned by current euid"
            )

    def check_regular_file(
        root: Path,
        relative: str,
    ) -> None:
        candidate = root / relative

        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return

        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
        ):
            _fail(
                f"{candidate} must be a real regular file "
                "owned by current euid"
            )

    for relative in (
        "objects",
        "objects/info",
        "objects/pack",
        "info",
        "refs",
    ):
        check_directory(
            common_root,
            relative,
        )

    for relative in (
        "HEAD",
        "config",
        "index",
        "packed-refs",
    ):
        check_regular_file(
            common_root,
            relative,
        )

    forbidden = [
        (
            common_root,
            "commondir",
        ),
        (
            common_root,
            "objects/info/alternates",
        ),
        (
            common_root,
            "info/grafts",
        ),
        (
            common_root,
            "shallow",
        ),
    ]

    if layout.linked_worktree:
        worktree_root = Path(
            layout.git_dir
        )

        for relative in (
            "HEAD",
            "index",
            "commondir",
            "gitdir",
        ):
            check_regular_file(
                worktree_root,
                relative,
            )

        refs_root = worktree_root / "refs"

        try:
            refs_state = refs_root.lstat()
        except FileNotFoundError:
            pass
        else:
            if (
                not stat.S_ISDIR(refs_state.st_mode)
                or refs_state.st_uid != os.geteuid()
            ):
                _fail(
                    f"{refs_root} must be a real directory "
                    "owned by current euid"
                )

            allowed_namespaces = {
                "bisect",
                "rewritten",
                "worktree",
            }

            try:
                namespaces = tuple(
                    refs_root.iterdir()
                )
            except OSError as exc:
                raise LabCommittedWorkspaceSeedError(
                    "cannot inspect linked-worktree refs "
                    f"directory: {exc}"
                ) from exc

            for namespace in sorted(
                namespaces,
                key=lambda candidate: candidate.name,
            ):
                if namespace.name not in allowed_namespaces:
                    _fail(
                        "unsupported linked-worktree ref "
                        f"namespace present: {namespace}"
                    )

                try:
                    namespace_state = namespace.lstat()
                except OSError as exc:
                    raise LabCommittedWorkspaceSeedError(
                        "cannot inspect linked-worktree ref "
                        f"namespace {namespace}: {exc}"
                    ) from exc

                if (
                    not stat.S_ISDIR(namespace_state.st_mode)
                    or namespace_state.st_uid != os.geteuid()
                ):
                    _fail(
                        f"{namespace} must be a real directory "
                        "owned by current euid"
                    )

                pending = [namespace]

                while pending:
                    current = pending.pop()

                    try:
                        children = tuple(
                            current.iterdir()
                        )
                    except OSError as exc:
                        raise LabCommittedWorkspaceSeedError(
                            "cannot inspect linked-worktree ref "
                            f"storage {current}: {exc}"
                        ) from exc

                    for child in children:
                        try:
                            child_state = child.lstat()
                        except OSError as exc:
                            raise LabCommittedWorkspaceSeedError(
                                "cannot inspect linked-worktree "
                                f"ref storage {child}: {exc}"
                            ) from exc

                        if child_state.st_uid != os.geteuid():
                            _fail(
                                f"{child} must be owned by "
                                "current euid"
                            )

                        if stat.S_ISDIR(child_state.st_mode):
                            pending.append(child)
                            continue

                        if not stat.S_ISREG(child_state.st_mode):
                            _fail(
                                f"{child} must be a real "
                                "directory or regular file "
                                "owned by current euid"
                            )

        for relative in (
            "objects",
            "config",
            "config.worktree",
            "packed-refs",
        ):
            candidate = (
                worktree_root
                / relative
            )

            try:
                candidate.lstat()
            except FileNotFoundError:
                continue

            _fail(
                "unsupported linked-worktree-local Git storage "
                f"present: {candidate}"
            )

        forbidden.extend(
            (
                (
                    worktree_root,
                    "objects/info/alternates",
                ),
                (
                    worktree_root,
                    "info/grafts",
                ),
                (
                    worktree_root,
                    "shallow",
                ),
            )
        )

    for root, relative in forbidden:
        candidate = root / relative

        try:
            candidate.lstat()
        except FileNotFoundError:
            continue

        _fail(
            "unsupported Git object indirection present: "
            f"{candidate}"
        )

    if _git(
        path,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace",
    ).stdout:
        _fail(
            "Git replace refs are not supported"
        )


def _snapshot(
    path: str,
    *,
    expected_branch: str | None,
    commit_oid: str,
    layout: _GitLayout,
) -> tuple[str, str]:
    _reject_indirections(
        path,
        layout,
    )

    reported_git_dir = (
        _normalize_reported_git_path(
            path,
            _line(
                _git(
                    path,
                    "rev-parse",
                    "--absolute-git-dir",
                ).stdout,
                "Git directory",
            ),
            "Git directory",
        )
    )

    reported_common_dir = (
        _normalize_reported_git_path(
            path,
            _line(
                _git(
                    path,
                    "rev-parse",
                    "--git-common-dir",
                ).stdout,
                "Git common directory",
            ),
            "Git common directory",
        )
    )

    if (
        reported_git_dir != layout.git_dir
        or reported_common_dir != layout.common_dir
    ):
        _fail(
            "Git-reported repository layout does not match "
            "the physically validated .git layout"
        )

    if _line(
        _git(
            path,
            "rev-parse",
            "--show-toplevel",
        ).stdout,
        "Git top-level",
    ) != path:
        _fail(
            "Git top-level path does not match requested repository"
        )

    branch_result = _git(
        path,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        allowed=(0, 1),
    )

    if branch_result.returncode != 0:
        _fail(
            "committed-state seeding requires an attached branch"
        )

    branch = _line(
        branch_result.stdout,
        "Git branch",
    )

    if (
        expected_branch is not None
        and branch != expected_branch
    ):
        _fail(
            "repository branch does not match expected_branch"
        )

    head = _line(
        _git(
            path,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ).stdout,
        "Git HEAD",
    )

    if head != commit_oid:
        _fail(
            "repository HEAD does not match exact commit_oid"
        )

    if _git(
        path,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout:
        _fail(
            "repository must have a completely clean "
            "working tree and index"
        )

    object_format = _line(
        _git(
            path,
            "rev-parse",
            "--show-object-format=storage",
        ).stdout,
        "Git object format",
    )

    if object_format != "sha1":
        _fail(
            "only Git SHA-1 object format is supported by this MVP"
        )

    return branch, object_format


def _git_oid(kind: str, raw: bytes) -> str:
    header = (
        f"{kind} {len(raw)}".encode("ascii")
        + b"\x00"
    )
    return hashlib.sha1(
        header + raw
    ).hexdigest()


def _size(path: str, oid: str) -> int:
    text = _line(
        _git(
            path,
            "cat-file",
            "-s",
            oid,
        ).stdout,
        "Git object size",
    )

    if (
        not text.isascii()
        or not text.isdigit()
    ):
        _fail(
            "Git object size is malformed"
        )

    return int(text)


def _verified_object(
    path: str,
    kind: str,
    oid: str,
    maximum: int,
) -> bytes:
    if _line(
        _git(
            path,
            "cat-file",
            "-t",
            oid,
        ).stdout,
        "Git object type",
    ) != kind:
        _fail(
            f"expected {kind} object"
        )

    size = _size(
        path,
        oid,
    )

    if size > maximum:
        _fail(
            f"{kind} object exceeds bounded size"
        )

    raw = _git(
        path,
        "cat-file",
        kind,
        oid,
    ).stdout

    if len(raw) != size:
        _fail(
            f"{kind} object size changed during read"
        )

    if _git_oid(
        kind,
        raw,
    ) != oid:
        _fail(
            f"{kind} object ID failed independent "
            "SHA-1 verification"
        )

    return raw


def _plan(
    path: str,
    commit_oid: str,
    relative: str,
) -> _Plan:
    raw = _git(
        path,
        "ls-tree",
        "-z",
        "--full-tree",
        commit_oid,
        "--",
        relative,
    ).stdout

    records = raw.split(
        b"\x00"
    )

    if (
        not raw
        or records[-1] != b""
        or len(records) != 2
    ):
        _fail(
            f"exact committed path is missing or ambiguous: "
            f"{relative}"
        )

    try:
        header, raw_path = records[0].split(
            b"\t",
            1,
        )
        mode_raw, kind_raw, oid_raw = header.split(
            b" "
        )
        mode = mode_raw.decode(
            "ascii"
        )
        kind = kind_raw.decode(
            "ascii"
        )
        blob_oid = oid_raw.decode(
            "ascii"
        )
    except (
        ValueError,
        UnicodeDecodeError,
    ) as exc:
        raise LabCommittedWorkspaceSeedError(
            "Git tree entry is malformed"
        ) from exc

    if raw_path != relative.encode(
        "utf-8"
    ):
        _fail(
            "Git tree entry path does not exactly "
            "match requested path"
        )

    if (
        mode not in (
            "100644",
            "100755",
        )
        or kind != "blob"
    ):
        _fail(
            "committed context paths must be ordinary "
            "100644 or 100755 blobs"
        )

    if (
        len(blob_oid) != 40
        or any(
            char not in _HEX
            for char in blob_oid
        )
    ):
        _fail(
            "Git tree blob object ID is malformed"
        )

    executable = (
        mode == "100755"
    )

    return _Plan(
        relative,
        blob_oid,
        _size(
            path,
            blob_oid,
        ),
        mode,
        (
            0o755
            if executable
            else 0o644
        ),
        (
            0o700
            if executable
            else 0o600
        ),
    )


def _write_private(
    root_fd: int,
    plan: _Plan,
    raw: bytes,
) -> LabCommittedWorkspaceSeedEntry:
    parts = PurePosixPath(
        plan.path
    ).parts

    parent_fd = os.dup(
        root_fd
    )

    try:
        for part in parts[:-1]:
            try:
                os.mkdir(
                    part,
                    0o700,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                pass

            next_fd = os.open(
                part,
                _dir_flags(),
                dir_fd=parent_fd,
            )

            info = os.fstat(
                next_fd
            )

            if (
                not stat.S_ISDIR(
                    info.st_mode
                )
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(
                    info.st_mode
                ) != 0o700
            ):
                os.close(
                    next_fd
                )
                _fail(
                    "workspace parent is not an exact "
                    "private directory"
                )

            os.close(
                parent_fd
            )
            parent_fd = next_fd

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(
                os,
                "O_NOFOLLOW",
                0,
            )
            | getattr(
                os,
                "O_CLOEXEC",
                0,
            )
        )

        try:
            fd = os.open(
                parts[-1],
                flags,
                plan.workspace_mode,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise LabCommittedWorkspaceSeedError(
                f"cannot create workspace target "
                f"{plan.path}: {exc}"
            ) from exc

        try:
            os.fchmod(
                fd,
                plan.workspace_mode,
            )

            offset = 0

            while offset < len(raw):
                written = os.write(
                    fd,
                    raw[offset:],
                )

                if written <= 0:
                    _fail(
                        "workspace write made no progress"
                    )

                offset += written

            os.fsync(
                fd
            )

            final = os.fstat(
                fd
            )

            if (
                not stat.S_ISREG(
                    final.st_mode
                )
                or final.st_uid != os.geteuid()
                or final.st_size != len(raw)
                or stat.S_IMODE(
                    final.st_mode
                ) != plan.workspace_mode
            ):
                _fail(
                    "materialized workspace file failed "
                    "physical verification"
                )

        finally:
            os.close(
                fd
            )

    finally:
        os.close(
            parent_fd
        )

    return LabCommittedWorkspaceSeedEntry(
        plan.path,
        plan.oid,
        len(raw),
        hashlib.sha256(
            raw
        ).hexdigest(),
        plan.workspace_mode,
        plan.source_mode,
        plan.git_mode,
    )



def _verify_expected_absent(
    path: str,
    commit_oid: str,
    relative: str,
) -> None:
    raw = _git(
        path,
        "ls-tree",
        "-z",
        "--full-tree",
        commit_oid,
        "--",
        relative,
    ).stdout

    if raw:
        _fail(
            "expected-absent committed path exists: "
            f"{relative}"
        )


def _revalidate_identity(
    path: str,
    repository_fd: int,
    initial_repo: os.stat_result,
    initial_layout: _GitLayout,
) -> None:
    final_repo = os.fstat(
        repository_fd
    )

    named = os.stat(
        path,
        follow_symlinks=False,
    )

    final_layout = _inspect_git_layout(
        path,
        repository_fd,
    )

    if (
        _state_identity(final_repo)
        != _state_identity(initial_repo)
        or (
            named.st_dev,
            named.st_ino,
            named.st_uid,
            named.st_gid,
            stat.S_IFMT(named.st_mode),
            stat.S_IMODE(named.st_mode),
        )
        != _state_identity(final_repo)
        or not _layout_identity_matches(
            initial_layout,
            final_layout,
        )
        or os.path.realpath(path) != path
    ):
        _fail(
            "repository or .git identity changed "
            "during committed-state seeding"
        )


def seed_lab_workspace_from_commit(
    *,
    repository_path: str,
    commit_oid: str,
    expected_branch: str | None,
    relative_paths: tuple[str, ...],
    expected_absent_paths: tuple[str, ...] = (),
    workspace_parent: str,
    workspace: LabWorkspace,
    max_files: int,
    max_total_bytes: int,
) -> LabCommittedWorkspaceSeedRecord:
    """Materialize explicit ordinary blobs from one exact verified SHA-1 commit."""
    max_files = _positive(
        max_files,
        "max_files",
    )

    max_total_bytes = _positive(
        max_total_bytes,
        "max_total_bytes",
    )

    repository_path = _repository(
        repository_path
    )

    commit_oid = _oid(
        commit_oid
    )

    expected_branch = (
        None
        if expected_branch is None
        else _text(
            expected_branch,
            "expected_branch",
            1024,
        )
    )

    relative_paths = _paths(
        relative_paths,
        max_files,
    )

    if type(expected_absent_paths) is not tuple:
        _fail(
            "expected_absent_paths must be an exact tuple"
        )

    if expected_absent_paths:
        expected_absent_paths = _paths(
            expected_absent_paths,
            max_files,
        )

    expected_absent_set = frozenset(
        expected_absent_paths
    )

    materialized_paths = tuple(
        relative
        for relative in relative_paths
        if relative not in expected_absent_set
    )

    workspace = validate_lab_workspace(
        workspace_parent,
        workspace,
    )

    if any(
        Path(
            workspace.path
        ).iterdir()
    ):
        _fail(
            "workspace must be empty before "
            "committed-state seeding"
        )

    repository_fd, repo_state, git_layout = (
        _open_repository(
            repository_path
        )
    )

    try:
        branch, object_format = _snapshot(
            repository_path,
            expected_branch=expected_branch,
            commit_oid=commit_oid,
            layout=git_layout,
        )

        _verified_object(
            repository_path,
            "commit",
            commit_oid,
            _MAX_COMMIT_BYTES,
        )

        for expected_absent in expected_absent_paths:
            _verify_expected_absent(
                repository_path,
                commit_oid,
                expected_absent,
            )

        plans = tuple(
            _plan(
                repository_path,
                commit_oid,
                item,
            )
            for item in materialized_paths
        )

        total = 0

        for plan in plans:
            if plan.size > max_total_bytes:
                _fail(
                    "committed blob exceeds "
                    "max_total_bytes"
                )

            total += plan.size

            if total > max_total_bytes:
                _fail(
                    "committed seed inputs exceed "
                    "max_total_bytes"
                )

        root_fd = os.open(
            workspace.path,
            _dir_flags(),
        )

        try:
            entries = []

            for plan in plans:
                raw = _verified_object(
                    repository_path,
                    "blob",
                    plan.oid,
                    max_total_bytes,
                )

                entries.append(
                    _write_private(
                        root_fd,
                        plan,
                        raw,
                    )
                )

        finally:
            os.close(
                root_fd
            )

        final_branch, final_format = _snapshot(
            repository_path,
            expected_branch=expected_branch,
            commit_oid=commit_oid,
            layout=git_layout,
        )

        if (
            final_branch,
            final_format,
        ) != (
            branch,
            object_format,
        ):
            _fail(
                "repository Git identity changed during "
                "committed-state seeding"
            )

        _verified_object(
            repository_path,
            "commit",
            commit_oid,
            _MAX_COMMIT_BYTES,
        )

        evidence = {
            entry.relative_path: entry
            for entry in entries
        }

        for plan in plans:
            raw = _verified_object(
                repository_path,
                "blob",
                plan.oid,
                max_total_bytes,
            )

            if (
                len(raw) != plan.size
                or hashlib.sha256(
                    raw
                ).hexdigest()
                != evidence[
                    plan.path
                ].sha256
            ):
                _fail(
                    "committed blob changed during "
                    "final revalidation"
                )

        for expected_absent in expected_absent_paths:
            _verify_expected_absent(
                repository_path,
                commit_oid,
                expected_absent,
            )

        _revalidate_identity(
            repository_path,
            repository_fd,
            repo_state,
            git_layout,
        )

        validate_lab_workspace(
            workspace_parent,
            workspace,
        )

        return LabCommittedWorkspaceSeedRecord(
            COMMITTED_WORKSPACE_SEED_COMPONENT,
            COMMITTED_WORKSPACE_SEED_SCHEMA_VERSION,
            repository_path,
            repo_state.st_dev,
            repo_state.st_ino,
            git_layout.marker_state.st_dev,
            git_layout.marker_state.st_ino,
            branch,
            commit_oid,
            object_format,
            workspace,
            tuple(entries),
            total,
        )

    finally:
        os.close(
            repository_fd
        )
