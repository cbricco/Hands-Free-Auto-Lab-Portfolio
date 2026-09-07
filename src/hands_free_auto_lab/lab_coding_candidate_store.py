"""Durable, immutable local evidence storage for validated coding candidates.

This store is evidence only.  It grants no promotion, approval, Git,
repository mutation, execution, worker, model, or networking authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator

from .lab_coding_candidate import (
    LabCodingCandidate,
    lab_coding_candidate_from_bytes,
    lab_coding_candidate_to_bytes,
    validate_lab_coding_candidate,
)

_LOCK = ".lock"
_CANDIDATES = "candidates"
_ROOT_ENTRIES = frozenset({_LOCK, _CANDIDATES})
_HEX = frozenset("0123456789abcdef")
MAX_RECORD_BYTES = 4 * 1024 * 1024


class LabCodingCandidateStoreError(RuntimeError):
    """The candidate evidence store or requested record is unsafe."""


def _error(message: str, exc: BaseException | None = None) -> LabCodingCandidateStoreError:
    error = LabCodingCandidateStoreError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _require_id(value: object) -> str:
    if type(value) is not str or len(value) != 64 or any(c not in _HEX for c in value):
        raise LabCodingCandidateStoreError(
            "candidate_id must be a lowercase 64-character hex identifier"
        )
    return value


def _require_root(root: str | os.PathLike[str]) -> Path:
    try:
        text = os.fspath(root)
    except (TypeError, ValueError) as exc:
        raise _error("candidate store root must be a filesystem path", exc)
    if type(text) is bytes:
        raise LabCodingCandidateStoreError("candidate store root must be a text path")
    if not os.path.isabs(text):
        raise LabCodingCandidateStoreError("candidate store root must be absolute")
    if os.path.normpath(text) != text:
        raise LabCodingCandidateStoreError("candidate store root must be normalized")
    if os.path.realpath(text) != text:
        raise LabCodingCandidateStoreError(
            "candidate store root must be canonical and contain no symlink components"
        )
    return Path(text)


def _require_platform() -> None:
    missing: list[str] = []
    for name in ("O_NOFOLLOW", "O_DIRECTORY"):
        if not hasattr(os, name):
            missing.append("os." + name)
    if not hasattr(os, "geteuid"):
        missing.append("os.geteuid")
    if not hasattr(fcntl, "flock"):
        missing.append("fcntl.flock")
    for label, function in (
        ("os.open(dir_fd=...)", os.open),
        ("os.mkdir(dir_fd=...)", os.mkdir),
        ("os.stat(dir_fd=...)", os.stat),
        ("os.link(dir_fd=...)", os.link),
        ("os.unlink(dir_fd=...)", os.unlink),
    ):
        if function not in os.supports_dir_fd:
            missing.append(label)
    if os.stat not in os.supports_follow_symlinks:
        missing.append("os.stat(follow_symlinks=False)")
    if os.link not in os.supports_follow_symlinks:
        missing.append("os.link(follow_symlinks=False)")
    if os.listdir not in os.supports_fd:
        missing.append("os.listdir(fd)")
    if missing:
        raise LabCodingCandidateStoreError(
            "secure candidate-store primitives unavailable: " + ", ".join(sorted(missing))
        )


def _flags(*values: int) -> int:
    result = 0
    for value in values:
        result |= value
    if hasattr(os, "O_CLOEXEC"):
        result |= os.O_CLOEXEC
    return result


def _directory_flags() -> int:
    _require_platform()
    return _flags(os.O_RDONLY, os.O_DIRECTORY, os.O_NOFOLLOW)


def _read_flags() -> int:
    _require_platform()
    return _flags(os.O_RDONLY, os.O_NOFOLLOW)


def _create_flags() -> int:
    _require_platform()
    return _flags(os.O_WRONLY, os.O_CREAT, os.O_EXCL, os.O_NOFOLLOW)


def _validate_directory(st: os.stat_result, name: str) -> None:
    if not stat.S_ISDIR(st.st_mode):
        raise LabCodingCandidateStoreError(f"{name} must be a real directory")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise LabCodingCandidateStoreError(f"{name} must have mode 0700")
    if st.st_uid != os.geteuid():
        raise LabCodingCandidateStoreError(f"{name} must be owned by the effective user")


def _validate_file(st: os.stat_result, name: str, *, links: int = 1) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise LabCodingCandidateStoreError(f"{name} must be a regular file")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise LabCodingCandidateStoreError(f"{name} must have mode 0600")
    if st.st_uid != os.geteuid():
        raise LabCodingCandidateStoreError(f"{name} must be owned by the effective user")
    if st.st_nlink != links:
        raise LabCodingCandidateStoreError(f"{name} must have exactly {links} hard link(s)")


def _open_root(path: Path) -> int:
    try:
        fd = os.open(path, _directory_flags())
    except OSError as exc:
        raise _error(f"cannot securely open candidate store root: {exc}", exc)
    try:
        _validate_directory(os.fstat(fd), "candidate store root")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_directory(parent_fd: int, filename: str) -> int:
    try:
        fd = os.open(filename, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise _error(f"cannot securely open {filename}: {exc}", exc)
    try:
        _validate_directory(os.fstat(fd), filename)
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_file(directory_fd: int, filename: str, *, links: int = 1) -> int:
    try:
        fd = os.open(filename, _read_flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise _error(f"cannot securely open {filename}: {exc}", exc)
    try:
        _validate_file(os.fstat(fd), filename, links=links)
        return fd
    except Exception:
        os.close(fd)
        raise


def _entries(fd: int, name: str) -> frozenset[str]:
    try:
        return frozenset(os.listdir(fd))
    except OSError as exc:
        raise _error(f"cannot list {name}: {exc}", exc)


def _record_id(name: str) -> str | None:
    if len(name) != 69 or not name.endswith(".json"):
        return None
    value = name[:-5]
    return value if len(value) == 64 and all(c in _HEX for c in value) else None


def _temporary_id(name: str) -> str | None:
    if len(name) != 69 or not name.startswith(".") or not name.endswith(".tmp"):
        return None
    value = name[1:-4]
    return value if len(value) == 64 and all(c in _HEX for c in value) else None


def _validate_layout(root_fd: int) -> None:
    entries = _entries(root_fd, "candidate store root")
    if entries != _ROOT_ENTRIES:
        raise LabCodingCandidateStoreError(
            f"candidate store root entries mismatch: {sorted(entries)!r}"
        )
    lock_fd = _open_file(root_fd, _LOCK)
    os.close(lock_fd)
    candidates_fd = _open_directory(root_fd, _CANDIDATES)
    try:
        for name in _entries(candidates_fd, "candidates directory"):
            if _temporary_id(name) is not None:
                raise LabCodingCandidateStoreError(
                    f"partial candidate installation remains: {name}"
                )
            if _record_id(name) is None:
                raise LabCodingCandidateStoreError(
                    f"unexpected candidates-directory entry: {name}"
                )
            fd = _open_file(candidates_fd, name)
            os.close(fd)
    finally:
        os.close(candidates_fd)


def _create_empty_file(root_fd: int, name: str) -> None:
    try:
        fd = os.open(name, _create_flags(), 0o600, dir_fd=root_fd)
    except OSError as exc:
        raise _error(f"cannot create {name}: {exc}", exc)
    try:
        os.fchmod(fd, 0o600)
        _validate_file(os.fstat(fd), name)
        os.fsync(fd)
    finally:
        os.close(fd)


def initialize_lab_coding_candidate_store(root: str | os.PathLike[str]) -> Path:
    """Initialize an empty private root, or strictly validate an existing store."""
    path = _require_root(root)
    root_fd = _open_root(path)
    try:
        if not _entries(root_fd, "candidate store root"):
            _create_empty_file(root_fd, _LOCK)
            try:
                os.mkdir(_CANDIDATES, mode=0o700, dir_fd=root_fd)
            except OSError as exc:
                raise _error(f"cannot create {_CANDIDATES}: {exc}", exc)
            try:
                candidates_fd = os.open(
                    _CANDIDATES, _directory_flags(), dir_fd=root_fd
                )
            except OSError as exc:
                raise _error(f"cannot securely open {_CANDIDATES}: {exc}", exc)
            try:
                os.fchmod(candidates_fd, 0o700)
                _validate_directory(os.fstat(candidates_fd), _CANDIDATES)
                os.fsync(candidates_fd)
            finally:
                os.close(candidates_fd)
            os.fsync(root_fd)
        _validate_layout(root_fd)
    except LabCodingCandidateStoreError:
        raise
    except OSError as exc:
        raise _error(
            f"candidate store initialization failed; partial state may remain for inspection: {exc}",
            exc,
        )
    finally:
        os.close(root_fd)
    return path


@contextmanager
def _locked_root(root: str | os.PathLike[str], *, exclusive: bool) -> Iterator[int]:
    path = _require_root(root)
    root_fd = _open_root(path)
    lock_fd: int | None = None
    try:
        _validate_layout(root_fd)
        lock_fd = _open_file(root_fd, _LOCK)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError as exc:
            raise _error(f"cannot acquire {_LOCK}: {exc}", exc)
        _validate_layout(root_fd)
        yield root_fd
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
        os.close(root_fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            count = os.write(fd, view)
        except OSError as exc:
            raise _error(f"candidate record write failed: {exc}", exc)
        if count <= 0:
            raise LabCodingCandidateStoreError("short write while persisting candidate")
        view = view[count:]


def _install_link(directory_fd: int, temporary_name: str, final_name: str) -> None:
    """Install a no-overwrite descriptor-relative hard link (test seam)."""
    os.link(
        temporary_name,
        final_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _read_bounded(fd: int, filename: str) -> bytes:
    before = os.fstat(fd)
    _validate_file(before, filename)
    if before.st_size > MAX_RECORD_BYTES:
        raise LabCodingCandidateStoreError(f"{filename} exceeds {MAX_RECORD_BYTES} bytes")
    chunks: list[bytes] = []
    remaining = MAX_RECORD_BYTES + 1
    while remaining:
        try:
            chunk = os.read(fd, min(65536, remaining))
        except OSError as exc:
            raise _error(f"cannot read {filename}: {exc}", exc)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    after = os.fstat(fd)
    _validate_file(after, filename)
    if len(data) > MAX_RECORD_BYTES:
        raise LabCodingCandidateStoreError(f"{filename} exceeds {MAX_RECORD_BYTES} bytes")
    if not _same_identity(before, after) or before.st_size != after.st_size:
        raise LabCodingCandidateStoreError(f"{filename} changed while being read")
    if len(data) != before.st_size:
        raise LabCodingCandidateStoreError(f"{filename} size evidence does not match read")
    return data


def persist_lab_coding_candidate(
    root: str | os.PathLike[str], candidate: object
) -> Path:
    """Create one immutable canonical candidate record without replacement."""
    try:
        trusted = validate_lab_coding_candidate(candidate)
        data = lab_coding_candidate_to_bytes(trusted)
    except Exception as exc:
        raise _error(f"invalid coding candidate: {exc}", exc)
    if len(data) > MAX_RECORD_BYTES:
        raise LabCodingCandidateStoreError(
            f"candidate record exceeds {MAX_RECORD_BYTES} bytes"
        )
    candidate_id = trusted.candidate_id
    final_name = candidate_id + ".json"
    temporary_name = "." + candidate_id + ".tmp"
    path = _require_root(root)
    with _locked_root(path, exclusive=True) as root_fd:
        candidates_fd = _open_directory(root_fd, _CANDIDATES)
        temporary_fd: int | None = None
        try:
            names = _entries(candidates_fd, "candidates directory")
            if final_name in names:
                raise LabCodingCandidateStoreError(f"candidate already exists: {candidate_id}")
            if temporary_name in names:
                raise LabCodingCandidateStoreError(
                    f"partial candidate installation remains: {temporary_name}"
                )
            try:
                temporary_fd = os.open(
                    temporary_name, _create_flags(), 0o600, dir_fd=candidates_fd
                )
            except OSError as exc:
                raise _error(f"cannot create {temporary_name}: {exc}", exc)
            os.fchmod(temporary_fd, 0o600)
            _write_all(temporary_fd, data)
            os.fsync(temporary_fd)
            temporary_stat = os.fstat(temporary_fd)
            _validate_file(temporary_stat, temporary_name)
            try:
                _install_link(candidates_fd, temporary_name, final_name)
            except OSError as exc:
                raise _error(f"cannot install {final_name}: {exc}", exc)
            os.fsync(candidates_fd)
            temporary_after = os.stat(
                temporary_name, dir_fd=candidates_fd, follow_symlinks=False
            )
            final_transition = os.stat(
                final_name, dir_fd=candidates_fd, follow_symlinks=False
            )
            _validate_file(temporary_after, temporary_name, links=2)
            _validate_file(final_transition, final_name, links=2)
            if not _same_identity(temporary_stat, temporary_after) or not _same_identity(
                temporary_after, final_transition
            ):
                raise LabCodingCandidateStoreError("candidate link transition identity mismatch")
            os.unlink(temporary_name, dir_fd=candidates_fd)
            os.fsync(candidates_fd)
            final_stat = os.stat(final_name, dir_fd=candidates_fd, follow_symlinks=False)
            _validate_file(final_stat, final_name)
            if not _same_identity(temporary_stat, final_stat):
                raise LabCodingCandidateStoreError("installed candidate identity mismatch")
            final_fd = _open_file(candidates_fd, final_name)
            try:
                reopened = _read_bounded(final_fd, final_name)
            finally:
                os.close(final_fd)
            try:
                decoded = lab_coding_candidate_from_bytes(reopened)
            except Exception as exc:
                raise _error(f"installed candidate cannot be decoded: {exc}", exc)
            if decoded != trusted:
                raise LabCodingCandidateStoreError(
                    "installed candidate does not equal supplied candidate"
                )
        except LabCodingCandidateStoreError:
            raise
        except OSError as exc:
            raise _error(
                f"candidate persistence failed; partial state may remain for inspection: {exc}",
                exc,
            )
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            os.close(candidates_fd)
    return path / _CANDIDATES / final_name


def load_lab_coding_candidate(
    root: str | os.PathLike[str], candidate_id: object
) -> LabCodingCandidate:
    """Securely load and validate one canonical candidate evidence record."""
    requested = _require_id(candidate_id)
    final_name = requested + ".json"
    with _locked_root(root, exclusive=False) as root_fd:
        candidates_fd = _open_directory(root_fd, _CANDIDATES)
        try:
            fd = _open_file(candidates_fd, final_name)
            try:
                data = _read_bounded(fd, final_name)
            finally:
                os.close(fd)
        finally:
            os.close(candidates_fd)
    try:
        candidate = lab_coding_candidate_from_bytes(data)
    except Exception as exc:
        raise _error(f"candidate record is invalid: {exc}", exc)
    if candidate.candidate_id != requested:
        raise LabCodingCandidateStoreError(
            "decoded candidate_id does not match requested candidate_id"
        )
    return candidate
