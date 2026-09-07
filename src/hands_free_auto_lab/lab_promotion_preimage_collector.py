"""Secure READ-ONLY collection of exact promotion MODIFY preimages.

The collector operates on the repository identity and before-state already
bound into a PREPARED promotion transaction.

It:

- requires the exact canonical repository path/device/inode,
- requires the exact branch and HEAD,
- requires a completely clean Git working tree including untracked files,
- traverses destination parents without following symlinks,
- rereads each MODIFY destination with O_NOFOLLOW,
- proves exact byte count and SHA-256 against the transaction,
- proves ADD destinations remain absent,
- revalidates repository/Git/file identity after reads.

It deliberately has no repository-write authority.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess

from .lab_promotion_proposal import (
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
)
from .lab_promotion_recovery_materials import (
    MAX_PREIMAGE_BYTES,
)
from .lab_promotion_transaction_state import (
    PROGRESS_PENDING,
    STATE_PREPARED,
    LabPromotionTransaction,
    LabPromotionTransactionStateError,
    validate_lab_promotion_transaction,
)


GIT_BINARY = "/usr/bin/git"
GIT_TIMEOUT_SECONDS = 5.0


class LabPromotionPreimageCollectorError(
    RuntimeError
):
    """Raised when exact repository preimages cannot be proven safely."""


def _require_prepared_transaction(
    transaction: LabPromotionTransaction,
) -> LabPromotionTransaction:
    try:
        transaction = validate_lab_promotion_transaction(
            transaction
        )
    except (
        TypeError,
        LabPromotionTransactionStateError,
    ) as exc:
        raise LabPromotionPreimageCollectorError(
            f"transaction failed validation: {exc}"
        ) from exc

    if transaction.state != STATE_PREPARED:
        raise LabPromotionPreimageCollectorError(
            "preimage collection requires PREPARED transaction"
        )

    if any(
        item.progress != PROGRESS_PENDING
        for item in transaction.files
    ):
        raise LabPromotionPreimageCollectorError(
            "preimage collection requires every file PENDING"
        )

    return transaction


def _directory_flags() -> int:
    for name in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
    ):
        if not hasattr(
            os,
            name,
        ):
            raise LabPromotionPreimageCollectorError(
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
        raise LabPromotionPreimageCollectorError(
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
    path: str,
) -> str:
    if not isinstance(
        path,
        str,
    ):
        raise LabPromotionPreimageCollectorError(
            "repository path must be a string"
        )

    if not os.path.isabs(
        path
    ):
        raise LabPromotionPreimageCollectorError(
            "repository path must be absolute"
        )

    if os.path.normpath(
        path
    ) != path:
        raise LabPromotionPreimageCollectorError(
            "repository path must be normalized"
        )

    if os.path.realpath(
        path
    ) != path:
        raise LabPromotionPreimageCollectorError(
            "repository path must be canonical and contain no symlink "
            "components"
        )

    return path


def _open_repository_fd(
    transaction: LabPromotionTransaction,
) -> tuple[
    int,
    os.stat_result,
]:
    repository_path = _require_repository_path(
        transaction.repository_path
    )

    try:
        fd = os.open(
            repository_path,
            _directory_flags(),
        )
    except OSError as exc:
        raise LabPromotionPreimageCollectorError(
            f"cannot securely open repository: {exc}"
        ) from exc

    try:
        st = os.fstat(
            fd
        )

        if not stat.S_ISDIR(
            st.st_mode
        ):
            raise LabPromotionPreimageCollectorError(
                "repository is not a directory"
            )

        if st.st_uid != os.geteuid():
            raise LabPromotionPreimageCollectorError(
                "repository must be owned by effective user"
            )

        if (
            st.st_dev
            != transaction.repository_device
            or st.st_ino
            != transaction.repository_inode
        ):
            raise LabPromotionPreimageCollectorError(
                "repository device/inode identity mismatch"
            )

        try:
            git_state = os.stat(
                ".git",
                dir_fd=fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LabPromotionPreimageCollectorError(
                f"cannot inspect repository .git directory: {exc}"
            ) from exc

        if not stat.S_ISDIR(
            git_state.st_mode
        ):
            raise LabPromotionPreimageCollectorError(
                "initial promotion MVP requires .git to be a real directory"
            )

        if git_state.st_uid != os.geteuid():
            raise LabPromotionPreimageCollectorError(
                ".git directory must be owned by effective user"
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
    environment = os.environ.copy()

    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )

    return environment


def _run_git(
    repository_path: str,
    *arguments: str,
) -> bytes:
    command = [
        GIT_BINARY,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        *arguments,
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=repository_path,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        raise LabPromotionPreimageCollectorError(
            f"read-only Git inspection failed: {exc}"
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode(
            "utf-8",
            errors="replace",
        ).strip()

        raise LabPromotionPreimageCollectorError(
            "read-only Git inspection command failed"
            + (
                f": {stderr}"
                if stderr
                else ""
            )
        )

    return completed.stdout


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
        raise LabPromotionPreimageCollectorError(
            f"{name} is not UTF-8"
        ) from exc

    lines = text.splitlines()

    if len(
        lines
    ) != 1:
        raise LabPromotionPreimageCollectorError(
            f"{name} must contain exactly one line"
        )

    value = lines[0]

    if not value:
        raise LabPromotionPreimageCollectorError(
            f"{name} must not be empty"
        )

    return value


def _git_snapshot(
    transaction: LabPromotionTransaction,
) -> tuple[
    str,
    str,
]:
    repository_path = transaction.repository_path

    top_level = _single_git_line(
        _run_git(
            repository_path,
            "rev-parse",
            "--show-toplevel",
        ),
        name="Git top-level path",
    )

    if os.path.realpath(
        top_level
    ) != repository_path:
        raise LabPromotionPreimageCollectorError(
            "Git top-level path does not match transaction repository"
        )

    branch = _single_git_line(
        _run_git(
            repository_path,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        ),
        name="Git branch",
    )

    head = _single_git_line(
        _run_git(
            repository_path,
            "rev-parse",
            "--verify",
            "HEAD",
        ),
        name="Git HEAD",
    )

    status = _run_git(
        repository_path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )

    if status:
        raise LabPromotionPreimageCollectorError(
            "promotion repository is not completely clean"
        )

    if branch != transaction.branch:
        raise LabPromotionPreimageCollectorError(
            "repository branch does not match transaction"
        )

    if head != transaction.head:
        raise LabPromotionPreimageCollectorError(
            "repository HEAD does not match transaction"
        )

    return (
        branch,
        head,
    )


def _open_parent_directory(
    repository_fd: int,
    components: tuple[
        str,
        ...,
    ],
) -> int:
    try:
        current_fd = os.dup(
            repository_fd
        )
    except OSError as exc:
        raise LabPromotionPreimageCollectorError(
            f"cannot duplicate repository descriptor: {exc}"
        ) from exc

    try:
        for component in components:
            try:
                next_fd = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise LabPromotionPreimageCollectorError(
                    "cannot securely open promotion destination parent "
                    f"{component!r}: {exc}"
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


def _require_components(
    path: str,
) -> tuple[
    str,
    ...,
]:
    if not isinstance(
        path,
        str,
    ):
        raise LabPromotionPreimageCollectorError(
            "promotion path must be a string"
        )

    if (
        not path
        or path.startswith(
            "/"
        )
        or "\\"
        in path
    ):
        raise LabPromotionPreimageCollectorError(
            f"promotion path is not canonical: {path!r}"
        )

    components = tuple(
        path.split(
            "/"
        )
    )

    if any(
        component in (
            "",
            ".",
            "..",
        )
        for component in components
    ):
        raise LabPromotionPreimageCollectorError(
            f"promotion path is not canonical: {path!r}"
        )

    return components


def _collect_modify_preimage(
    *,
    parent_fd: int,
    name: str,
    path: str,
    expected_bytes: int,
    expected_sha256: str,
    expected_mode: int,
) -> bytes:
    try:
        initial = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LabPromotionPreimageCollectorError(
            f"cannot inspect MODIFY destination {path!r}: {exc}"
        ) from exc

    if stat.S_ISLNK(
        initial.st_mode
    ):
        raise LabPromotionPreimageCollectorError(
            f"MODIFY destination must not be symlink: {path!r}"
        )

    if not stat.S_ISREG(
        initial.st_mode
    ):
        raise LabPromotionPreimageCollectorError(
            f"MODIFY destination must be regular file: {path!r}"
        )

    try:
        fd = os.open(
            name,
            _file_read_flags(),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise LabPromotionPreimageCollectorError(
            f"cannot securely open MODIFY destination {path!r}: {exc}"
        ) from exc

    try:
        before = os.fstat(
            fd
        )

        if not stat.S_ISREG(
            before.st_mode
        ):
            raise LabPromotionPreimageCollectorError(
                f"opened MODIFY destination is not regular: {path!r}"
            )

        if (
            before.st_dev
            != initial.st_dev
            or before.st_ino
            != initial.st_ino
        ):
            raise LabPromotionPreimageCollectorError(
                f"MODIFY destination changed during open: {path!r}"
            )

        observed_mode = stat.S_IMODE(
            before.st_mode
        )

        if observed_mode & ~0o777:
            raise LabPromotionPreimageCollectorError(
                "promotion v2 refuses setuid, setgid, or sticky mode bits: "
                f"{path!r}"
            )

        if observed_mode != expected_mode:
            raise LabPromotionPreimageCollectorError(
                f"MODIFY before mode mismatch: {path!r}"
            )

        digest = hashlib.sha256()
        chunks: list[
            bytes
        ] = []
        byte_count = 0

        while True:
            try:
                chunk = os.read(
                    fd,
                    64 * 1024,
                )
            except OSError as exc:
                raise LabPromotionPreimageCollectorError(
                    f"cannot read MODIFY destination {path!r}: {exc}"
                ) from exc

            if not chunk:
                break

            byte_count += len(
                chunk
            )

            if byte_count > MAX_PREIMAGE_BYTES:
                raise LabPromotionPreimageCollectorError(
                    f"MODIFY preimage exceeds size limit: {path!r}"
                )

            chunks.append(
                chunk
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
            raise LabPromotionPreimageCollectorError(
                f"MODIFY destination changed while reading: {path!r}"
            )

        if byte_count != after.st_size:
            raise LabPromotionPreimageCollectorError(
                f"MODIFY destination size changed while reading: {path!r}"
            )

        try:
            final_path_state = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LabPromotionPreimageCollectorError(
                f"cannot revalidate MODIFY destination {path!r}: {exc}"
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
        ):
            raise LabPromotionPreimageCollectorError(
                f"MODIFY destination path changed after read: {path!r}"
            )

        observed_sha256 = digest.hexdigest()

        if byte_count != expected_bytes:
            raise LabPromotionPreimageCollectorError(
                f"MODIFY before byte count mismatch: {path!r}"
            )

        if observed_sha256 != expected_sha256:
            raise LabPromotionPreimageCollectorError(
                f"MODIFY before SHA-256 mismatch: {path!r}"
            )

        return b"".join(
            chunks
        )

    finally:
        os.close(
            fd
        )


def _require_add_absent(
    *,
    parent_fd: int,
    name: str,
    path: str,
) -> None:
    try:
        os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LabPromotionPreimageCollectorError(
            f"cannot inspect ADD destination {path!r}: {exc}"
        ) from exc

    raise LabPromotionPreimageCollectorError(
        f"ADD destination is no longer absent: {path!r}"
    )


def collect_promotion_preimages(
    *,
    transaction: LabPromotionTransaction,
) -> dict[
    str,
    bytes,
]:
    """Collect exact MODIFY before-bytes without modifying the repository."""
    transaction = _require_prepared_transaction(
        transaction
    )

    repository_fd, initial_repository_state = (
        _open_repository_fd(
            transaction
        )
    )

    try:
        initial_branch, initial_head = _git_snapshot(
            transaction
        )

        preimages: dict[
            str,
            bytes,
        ] = {}

        for item in transaction.files:
            components = _require_components(
                item.path
            )

            parent_fd = _open_parent_directory(
                repository_fd,
                components[:-1],
            )

            try:
                name = components[-1]

                if item.operation == PROMOTION_OPERATION_MODIFY:
                    if (
                        item.before_bytes is None
                        or item.before_sha256 is None
                        or item.before_mode is None
                    ):
                        raise LabPromotionPreimageCollectorError(
                            f"MODIFY before-state missing for {item.path!r}"
                        )

                    preimages[
                        item.path
                    ] = _collect_modify_preimage(
                        parent_fd=parent_fd,
                        name=name,
                        path=item.path,
                        expected_bytes=item.before_bytes,
                        expected_sha256=item.before_sha256,
                        expected_mode=item.before_mode,
                    )

                elif item.operation == PROMOTION_OPERATION_ADD:
                    _require_add_absent(
                        parent_fd=parent_fd,
                        name=name,
                        path=item.path,
                    )

                else:
                    raise LabPromotionPreimageCollectorError(
                        f"unsupported promotion operation {item.operation!r}"
                    )

            finally:
                os.close(
                    parent_fd
                )

        final_branch, final_head = _git_snapshot(
            transaction
        )

        if (
            final_branch != initial_branch
            or final_head != initial_head
        ):
            raise LabPromotionPreimageCollectorError(
                "repository Git identity changed during preimage collection"
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
            raise LabPromotionPreimageCollectorError(
                "repository identity changed during preimage collection"
            )

        try:
            final_path_state = os.stat(
                transaction.repository_path,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LabPromotionPreimageCollectorError(
                f"cannot revalidate repository path: {exc}"
            ) from exc

        if (
            final_path_state.st_dev
            != final_repository_state.st_dev
            or final_path_state.st_ino
            != final_repository_state.st_ino
        ):
            raise LabPromotionPreimageCollectorError(
                "repository path no longer names exact repository"
            )

        return preimages

    finally:
        os.close(
            repository_fd
        )
