"""Isolated RUN execution for Hands-Free Auto Lab.

Only deterministic-policy-approved RUN actions are executed here. READ_FILE
and WRITE_FILE execution remain separate future capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import selectors
import signal
import stat
import subprocess
import time

from .lab_action import LabAction
from .lab_policy import LAB_POLICY, LabPolicyError, evaluate_lab_policy
from .lab_workspace import (
    LabWorkspace,
    LabWorkspaceError,
    validate_lab_workspace,
)


EXECUTOR_COMPONENT = "hands-free-auto-lab-run-executor-v1"
BWRAP = "/usr/bin/bwrap"
PRLIMIT = "/usr/bin/prlimit"

MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 30
OUTPUT_LIMIT_BYTES = 1024 * 1024
ADDRESS_SPACE_LIMIT_BYTES = 512 * 1024 * 1024
FILE_SIZE_LIMIT_BYTES = 16 * 1024 * 1024
CPU_LIMIT_SECONDS = 15
PROCESS_LIMIT = 32
OPEN_FILE_LIMIT = 256
PIPE_READ_BYTES = 64 * 1024
KILL_DRAIN_SECONDS = 2.0


class LabExecutorError(RuntimeError):
    """Raised when a RUN action cannot be safely launched."""


@dataclass(frozen=True, slots=True)
class LabExecutionResult:
    component: str
    action_id: str
    policy: str
    workspace_device: int
    workspace_inode: int
    timeout_seconds: int
    output_limit_bytes: int
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    exit_status: int | None
    signal: int | None
    stdout: str
    stderr: str

    @property
    def output_limit_exceeded(self) -> bool:
        return (
            self.stdout_truncated
            or self.stderr_truncated
        )

    @property
    def succeeded(self) -> bool:
        return (
            not self.timed_out
            and not self.output_limit_exceeded
            and self.exit_status == 0
            and self.signal is None
        )


def _validate_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LabExecutorError("timeout_seconds must be an integer")

    if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
        raise LabExecutorError("timeout_seconds is outside the enabled range")

    return value


def _workspace_flags() -> int:
    for name in ("O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, name):
            raise LabExecutorError(
                f"secure directory primitive unavailable: os.{name}"
            )

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    return flags


def _open_validated_workspace_fd(workspace: LabWorkspace) -> int:
    try:
        fd = os.open(
            workspace.path,
            _workspace_flags(),
        )
    except OSError as exc:
        raise LabExecutorError(
            f"cannot securely open validated workspace: {exc}"
        ) from exc

    try:
        st = os.fstat(fd)

        if not stat.S_ISDIR(st.st_mode):
            raise LabExecutorError(
                "opened workspace is not a directory"
            )

        if st.st_dev != workspace.device:
            raise LabExecutorError(
                "opened workspace device identity mismatch"
            )

        if st.st_ino != workspace.inode:
            raise LabExecutorError(
                "opened workspace inode identity mismatch"
            )

        if st.st_uid != workspace.uid:
            raise LabExecutorError(
                "opened workspace owner identity mismatch"
            )

        if stat.S_IMODE(st.st_mode) != workspace.mode:
            raise LabExecutorError(
                "opened workspace mode identity mismatch"
            )

        return fd

    except Exception:
        os.close(fd)
        raise


def _sandbox_command(
    action: LabAction,
    workspace_fd: int,
) -> tuple[str, ...]:
    return (
        BWRAP,
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--disable-userns",
        "--die-with-parent",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin",
        "--setenv", "HOME", "/workspace",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "LC_ALL", "C",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PYTHONHASHSEED", "0",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/lib64", "/lib64",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--ro-bind-fd", str(workspace_fd), "/workspace",
        "--chdir", "/workspace",
        "--",
        PRLIMIT,
        f"--as={ADDRESS_SPACE_LIMIT_BYTES}",
        f"--fsize={FILE_SIZE_LIMIT_BYTES}",
        f"--cpu={CPU_LIMIT_SECONDS}",
        f"--nproc={PROCESS_LIMIT}",
        f"--nofile={OPEN_FILE_LIMIT}",
        "--",
        *action.argv,
    )


def _kill_process_group(
    process: subprocess.Popen[bytes],
) -> None:
    try:
        os.killpg(
            process.pid,
            signal.SIGKILL,
        )
    except ProcessLookupError:
        return
    except OSError as exc:
        raise LabExecutorError(
            f"cannot terminate sandbox process group: {exc}"
        ) from exc


def _append_bounded(
    target: bytearray,
    chunk: bytes,
) -> bool:
    remaining = (
        OUTPUT_LIMIT_BYTES
        - len(target)
    )

    if remaining > 0:
        target.extend(
            chunk[:remaining]
        )

    return len(chunk) > remaining


def _capture_bounded(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: int,
) -> tuple[
    bytes,
    bytes,
    bool,
    bool,
    bool,
]:
    if process.stdout is None or process.stderr is None:
        raise LabExecutorError(
            "sandbox output pipes are unavailable"
        )

    stdout_data = bytearray()
    stderr_data = bytearray()

    stdout_truncated = False
    stderr_truncated = False
    timed_out = False
    termination_started = False
    termination_deadline: float | None = None

    selector = selectors.DefaultSelector()

    streams = (
        (
            process.stdout,
            "stdout",
        ),
        (
            process.stderr,
            "stderr",
        ),
    )

    try:
        for stream, name in streams:
            fd = stream.fileno()
            os.set_blocking(
                fd,
                False,
            )

            selector.register(
                fd,
                selectors.EVENT_READ,
                data=name,
            )

        deadline = (
            time.monotonic()
            + timeout_seconds
        )

        while selector.get_map():
            now = time.monotonic()

            if (
                not termination_started
                and process.poll() is None
                and now >= deadline
            ):
                timed_out = True
                _kill_process_group(process)
                termination_started = True
                termination_deadline = (
                    now
                    + KILL_DRAIN_SECONDS
                )

            if (
                termination_started
                and termination_deadline is not None
                and now >= termination_deadline
            ):
                raise LabExecutorError(
                    "sandbox output pipes did not close "
                    "after termination"
                )

            if termination_started:
                select_timeout = 0.05
            else:
                select_timeout = max(
                    0.0,
                    min(
                        0.05,
                        deadline - now,
                    ),
                )

            events = selector.select(
                select_timeout
            )

            for key, _ in events:
                try:
                    chunk = os.read(
                        key.fd,
                        PIPE_READ_BYTES,
                    )
                except BlockingIOError:
                    continue

                if not chunk:
                    selector.unregister(
                        key.fd
                    )
                    continue

                if key.data == "stdout":
                    exceeded = _append_bounded(
                        stdout_data,
                        chunk,
                    )

                    if exceeded:
                        stdout_truncated = True

                elif key.data == "stderr":
                    exceeded = _append_bounded(
                        stderr_data,
                        chunk,
                    )

                    if exceeded:
                        stderr_truncated = True

                else:
                    raise LabExecutorError(
                        "unknown sandbox output stream"
                    )

                if (
                    exceeded
                    and not termination_started
                ):
                    _kill_process_group(process)
                    termination_started = True
                    termination_deadline = (
                        time.monotonic()
                        + KILL_DRAIN_SECONDS
                    )

        try:
            process.wait(
                timeout=KILL_DRAIN_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            raise LabExecutorError(
                "sandbox process did not terminate "
                "after output pipes closed"
            ) from exc

        return (
            bytes(stdout_data),
            bytes(stderr_data),
            timed_out,
            stdout_truncated,
            stderr_truncated,
        )

    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _build_result(
    *,
    action: LabAction,
    workspace: LabWorkspace,
    timeout_seconds: int,
    timed_out: bool,
    stdout_truncated: bool,
    stderr_truncated: bool,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
) -> LabExecutionResult:
    if returncode < 0:
        exit_status = None
        terminated_by_signal = -returncode
    else:
        exit_status = returncode
        terminated_by_signal = None

    return LabExecutionResult(
        component=EXECUTOR_COMPONENT,
        action_id=action.action_id,
        policy=LAB_POLICY,
        workspace_device=workspace.device,
        workspace_inode=workspace.inode,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=OUTPUT_LIMIT_BYTES,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        exit_status=exit_status,
        signal=terminated_by_signal,
        stdout=stdout.decode(
            "utf-8",
            errors="replace",
        ),
        stderr=stderr.decode(
            "utf-8",
            errors="replace",
        ),
    )


def execute_lab_run(
    action: LabAction,
    *,
    workspace_parent: str,
    workspace: LabWorkspace,
    timeout_seconds: int = 10,
) -> LabExecutionResult:
    """Execute exactly one eligible RUN action inside the lab sandbox."""
    timeout_seconds = _validate_timeout(
        timeout_seconds
    )

    try:
        decision = evaluate_lab_policy(
            action
        )
    except LabPolicyError as exc:
        raise LabExecutorError(
            f"lab policy refused action: {exc}"
        ) from exc

    if decision["kind"] != "RUN":
        raise LabExecutorError(
            "RUN executor refuses non-RUN actions"
        )

    if decision["cwd"] != ".":
        raise LabExecutorError(
            "initial RUN executor requires cwd='.'"
        )

    try:
        validate_lab_workspace(
            workspace_parent,
            workspace,
        )
    except LabWorkspaceError as exc:
        raise LabExecutorError(
            f"workspace validation failed: {exc}"
        ) from exc

    workspace_fd = _open_validated_workspace_fd(
        workspace
    )

    try:
        command = _sandbox_command(
            action,
            workspace_fd,
        )

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env={
                    "PATH": "/usr/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                },
                close_fds=True,
                pass_fds=(workspace_fd,),
                start_new_session=True,
                bufsize=0,
            )
        except OSError as exc:
            raise LabExecutorError(
                f"cannot launch sandbox executor: {exc}"
            ) from exc

        (
            stdout,
            stderr,
            timed_out,
            stdout_truncated,
            stderr_truncated,
        ) = _capture_bounded(
            process,
            timeout_seconds=timeout_seconds,
        )

        assert process.returncode is not None

        return _build_result(
            action=action,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    finally:
        os.close(workspace_fd)
