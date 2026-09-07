"""Bounded Codex coding-worker adapter for Hands-Free Auto Lab.

The adapter is only an execution mechanism.  Its result is worker evidence;
the independent workspace snapshot/diff layer remains physical truth.  This
module has no Git, approval, promotion, controller, audit, or host mutation
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import signal
import stat
import subprocess
import threading
from typing import Callable
from urllib.parse import urlsplit

from .lab_worker import (
    CAPABILITY_RUN_PROCESS,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    MAX_LOG_BYTES,
    MAX_RUNTIME_SECONDS,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    LabWorkerRequest,
    LabWorkerResult,
    build_lab_worker_result,
    validate_lab_worker_request,
)

from .lab_worker_change_policy import (
    LabWorkerChangePolicy,
    validate_lab_worker_change_policy,
)
from .lab_workspace_snapshot import (
    LabWorkspaceSnapshotError,
    capture_lab_workspace_snapshot,
)


CODEX_WORKER_NAME = "codex-v1"

_REQUIRED_CAPABILITIES = tuple(sorted((
    CAPABILITY_RUN_PROCESS,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
)))

_MAX_PATH_BYTES = 4096
_OUTPUT_MARKER = b"\n[output truncated by Auto Lab]\n"

_DIRECT_WORKSPACE_INSTRUCTION = """AUTO LAB MUTATION TRANSPORT: DIRECT_WORKSPACE
Use workspace-local writes directly inside the supplied disposable workspace. Do not use
apply_patch or nested sandbox helpers. Do not weaken networking, sandboxing, permissions,
or containment. Do not use Git as an authority mechanism. Report unexpected generated or
runtime artifacts; do not delete them merely to obtain a passing candidate.

These instructions do not expand authorization or writable scope. Existing Auto Lab policy
and the supplied workspace remain authoritative."""


class CodexWorkerError(RuntimeError):
    """The Codex worker could not be run inside its reviewed boundary."""


@dataclass(frozen=True, slots=True)
class CodexWorkerConfig:
    """Reviewed host paths and hard ceilings for the trusted Codex client."""

    codex_executable: str
    codex_home: str
    codex_release_root: str
    runtime_temp: str
    max_runtime_seconds: int
    max_output_bytes: int
    trusted_client_proxy: str | None = None


@dataclass(frozen=True, slots=True)
class CodexCommandResult:
    returncode: int
    log: str
    timed_out: bool


CommandRunner = Callable[
    [tuple[str, ...], str, dict[str, str], int, int],
    CodexCommandResult,
]


def _text(value: object, *, name: str, maximum: int = _MAX_PATH_BYTES) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CodexWorkerError(f"{name} must be a non-empty string without NUL")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CodexWorkerError(f"{name} must be UTF-8 encodable") from exc
    if len(raw) > maximum:
        raise CodexWorkerError(f"{name} exceeds maximum size")
    return value


def _canonical_absolute(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise CodexWorkerError(f"{name} must be a canonical absolute path")
    if os.path.realpath(value) != value:
        raise CodexWorkerError(f"{name} must not use symlinks or aliases")
    return value


def _positive_limit(value: object, *, name: str, maximum: int) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 1 <= value <= maximum):
        raise CodexWorkerError(f"{name} is outside the supported range")
    return value


def _loopback_proxy(value: object) -> str:
    value = _text(value, name="trusted_client_proxy")
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1")
            or parsed.port is None or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise CodexWorkerError(
            "trusted_client_proxy must be an explicit loopback HTTP URL with port"
        )
    return value


def validate_codex_worker_config(config: object) -> CodexWorkerConfig:
    if not isinstance(config, CodexWorkerConfig):
        raise CodexWorkerError("config must be a CodexWorkerConfig")

    executable = _canonical_absolute(config.codex_executable, name="codex_executable")
    home = _canonical_absolute(config.codex_home, name="codex_home")
    release = _canonical_absolute(config.codex_release_root, name="codex_release_root")
    runtime = _canonical_absolute(config.runtime_temp, name="runtime_temp")
    if os.path.commonpath((executable, release)) != release or executable == release:
        raise CodexWorkerError("codex_executable must be inside codex_release_root")
    if len({home, release, runtime}) != 3:
        raise CodexWorkerError("configured security roots must be distinct")

    runtime_limit = _positive_limit(
        config.max_runtime_seconds, name="max_runtime_seconds", maximum=MAX_RUNTIME_SECONDS
    )
    output_limit = _positive_limit(
        config.max_output_bytes, name="max_output_bytes", maximum=MAX_LOG_BYTES
    )
    proxy = None if config.trusted_client_proxy is None else _loopback_proxy(
        config.trusted_client_proxy
    )
    expected = CodexWorkerConfig(
        executable, home, release, runtime, runtime_limit, output_limit, proxy
    )
    if config != expected:
        raise CodexWorkerError("Codex worker config is not canonical")
    return config


def build_codex_worker_config(
    *, codex_executable: str, codex_home: str, codex_release_root: str,
    runtime_temp: str,
    max_runtime_seconds: int, max_output_bytes: int,
    trusted_client_proxy: str | None = None,
) -> CodexWorkerConfig:
    config = CodexWorkerConfig(
        _canonical_absolute(codex_executable, name="codex_executable"),
        _canonical_absolute(codex_home, name="codex_home"),
        _canonical_absolute(codex_release_root, name="codex_release_root"),
        _canonical_absolute(runtime_temp, name="runtime_temp"),
        max_runtime_seconds,
        max_output_bytes,
        None if trusted_client_proxy is None else _loopback_proxy(trusted_client_proxy),
    )
    return validate_codex_worker_config(config)


def _validate_request(request: LabWorkerRequest) -> LabWorkerRequest:
    request = validate_lab_worker_request(request)
    if request.worker != CODEX_WORKER_NAME:
        raise CodexWorkerError("worker request is not bound to this Codex adapter")
    if request.constraints.capabilities != _REQUIRED_CAPABILITIES:
        raise CodexWorkerError(
            "Codex V1 requires exactly the three bounded workspace capabilities"
        )
    return request


def _validate_directory(
    path: str, *, name: str, private: bool, require_current_user: bool
) -> os.stat_result:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise CodexWorkerError(f"cannot inspect {name}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise CodexWorkerError(f"{name} must be a directory")
    if require_current_user and info.st_uid != os.geteuid():
        raise CodexWorkerError(f"{name} must be owned by the current user")
    mode = stat.S_IMODE(info.st_mode)
    if (private and mode != 0o700) or (not private and mode & 0o022):
        raise CodexWorkerError(f"{name} has unsafe permissions")
    return info


def _validate_physical_context(config: CodexWorkerConfig) -> None:
    release_info = _validate_directory(
        config.codex_release_root, name="codex_release_root", private=False,
        require_current_user=False,
    )
    _validate_directory(
        config.codex_home, name="codex_home", private=True, require_current_user=True
    )
    _validate_directory(
        config.runtime_temp, name="runtime_temp", private=True,
        require_current_user=True,
    )
    try:
        executable = os.stat(config.codex_executable, follow_symlinks=False)
    except OSError as exc:
        raise CodexWorkerError(f"cannot inspect codex_executable: {exc}") from exc
    if (not stat.S_ISREG(executable.st_mode) or executable.st_uid != release_info.st_uid
            or stat.S_IMODE(executable.st_mode) & 0o022
            or not stat.S_IMODE(executable.st_mode) & 0o111):
        raise CodexWorkerError("codex_executable is not a reviewed executable")


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, tuple):
        return "[" + ",".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{_toml_key(key)}={_toml(item)}" for key, item in value.items()
        ) + "}"
    raise TypeError("unsupported TOML value")


def _toml_key(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("TOML table keys must be strings")
    if value and all(character.isascii() and (character.isalnum() or character in "_-")
                     for character in value):
        return value
    return _toml(value)


def _override(name: str, value: object) -> tuple[str, str]:
    return ("-c", f"{name}={_toml(value)}")


def build_codex_effective_goal(
    user_goal: str,
    *,
    change_policy: LabWorkerChangePolicy | None = None,
) -> str:
    """
    Build deterministic Codex-only instructions while preserving the goal.

    Change-policy guidance grants no authority. Independent physical
    workspace validation remains authoritative.
    """
    trusted_policy = (
        None
        if change_policy is None
        else validate_lab_worker_change_policy(
            change_policy
        )
    )

    sections = [
        _DIRECT_WORKSPACE_INSTRUCTION,
    ]

    if trusted_policy is not None:
        guidance = [
            "AUTO LAB CHANGE POLICY GUIDANCE",
            (
                "This describes the already validated deterministic "
                "change policy and grants no authority."
            ),
            (
                "Independent physical workspace validation remains "
                "authoritative."
            ),
            (
                "Make only the listed physical changes and ensure "
                "their final state satisfies every listed constraint."
            ),
        ]

        for target in trusted_policy.targets:
            guidance.append(
                (
                    f"- operation={target.operation} "
                    f"path={target.path!r} "
                    f"max_final_bytes={target.max_final_bytes} "
                    f"final_mode={target.final_mode:04o}"
                )
            )

        guidance.append(
            (
                "Because the worker uses a restrictive umask, "
                "explicitly set each target final mode when needed."
            )
        )

        sections.append(
            "\n".join(guidance)
        )

    sections.append(
        (
            "USER GOAL (preserved exactly as the suffix):\n"
            + user_goal
        )
    )

    return "\n\n".join(sections)


def build_codex_command(
    *,
    request: LabWorkerRequest,
    config: CodexWorkerConfig,
    change_policy: LabWorkerChangePolicy | None = None,
) -> tuple[str, ...]:
    """Build a noninteractive argv-only Codex invocation."""
    request = _validate_request(request)
    config = validate_codex_worker_config(config)

    trusted_policy = (
        None
        if change_policy is None
        else validate_lab_worker_change_policy(
            change_policy
        )
    )

    if trusted_policy is None:
        if request.change_policy_id is not None:
            raise CodexWorkerError(
                "policy-bound Codex request requires the exact change policy"
            )
    elif request.change_policy_id != trusted_policy.policy_id:
        raise CodexWorkerError(
            "Codex change policy binding mismatch"
        )

    generated_home = os.path.join(
        config.runtime_temp,
        "generated-home",
    )
    generated_path = (
        f"{config.codex_release_root}/codex-path:/usr/bin:/bin"
    )

    command = [
        config.codex_executable,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--json",
        "-C",
        request.workspace.path,
        *_override(
            "web_search",
            "disabled",
        ),
        *_override(
            "default_permissions",
            "auto-lab-worker",
        ),
        *_override(
            "permissions.auto-lab-worker.extends",
            ":workspace",
        ),
        *_override(
            "permissions.auto-lab-worker.filesystem",
            {
                ":root": "deny",
                ":minimal": "read",
                ":slash_tmp": "deny",
                ":tmpdir": "write",
                config.codex_release_root: "read",
                ":workspace_roots": {
                    ".": "write",
                },
            },
        ),
        *_override(
            "permissions.auto-lab-worker.network",
            {
                "enabled": False,
            },
        ),
        *_override(
            "shell_environment_policy.inherit",
            "none",
        ),
        *_override(
            (
                "shell_environment_policy."
                "ignore_default_excludes"
            ),
            False,
        ),
        *_override(
            "shell_environment_policy.set.HOME",
            generated_home,
        ),
        *_override(
            "shell_environment_policy.set.TMPDIR",
            config.runtime_temp,
        ),
        *_override(
            "shell_environment_policy.set.PATH",
            generated_path,
        ),
        *_override(
            (
                "shell_environment_policy.set."
                "PYTHONDONTWRITEBYTECODE"
            ),
            "1",
        ),
        *_override(
            (
                "shell_environment_policy.set."
                "PYTHONPYCACHEPREFIX"
            ),
            os.path.join(
                config.runtime_temp,
                "generated-pycache",
            ),
        ),
        "--",
        build_codex_effective_goal(
            request.goal,
            change_policy=trusted_policy,
        ),
    ]

    return tuple(command)


def build_codex_control_environment(config: CodexWorkerConfig) -> dict[str, str]:
    """Return the explicit environment for the trusted client, not its commands."""
    config = validate_codex_worker_config(config)
    environment = {
        "CODEX_HOME": config.codex_home,
        "HOME": config.runtime_temp,
        "TMPDIR": config.runtime_temp,
        "PATH": os.path.dirname(config.codex_executable),
    }
    if config.trusted_client_proxy is not None:
        environment["HTTP_PROXY"] = config.trusted_client_proxy
        environment["HTTPS_PROXY"] = config.trusted_client_proxy
        environment["NO_PROXY"] = "127.0.0.1,::1,localhost"
    return environment


def _bounded_log(data: bytearray, *, truncated: bool, maximum: int) -> str:
    raw = bytes(data)
    if truncated:
        available = max(0, maximum - len(_OUTPUT_MARKER))
        raw = raw[:available] + _OUTPUT_MARKER[:maximum]
    return raw[:maximum].decode("utf-8", errors="replace")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as exc:
        raise CodexWorkerError("Codex process group could not be terminated") from exc


def _default_command_runner(
    argv: tuple[str, ...], cwd: str, environment: dict[str, str],
    timeout_seconds: int, max_output_bytes: int,
) -> CodexCommandResult:
    if not isinstance(argv, tuple) or not argv:
        raise CodexWorkerError("command runner requires a non-empty argv tuple")
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
            umask=0o077,
        )
    except OSError as exc:
        raise CodexWorkerError(f"cannot start trusted Codex control process: {exc}") from exc
    if process.stdout is None:
        _terminate_process_group(process)
        raise CodexWorkerError("Codex output pipe was not created")
    captured = bytearray()
    truncated = [False]
    errors: list[BaseException] = []

    def drain() -> None:
        try:
            while True:
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if not chunk:
                    return
                remaining = max_output_bytes - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    truncated[0] = True
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
    reader.join(timeout=5)
    if reader.is_alive():
        process.stdout.close()
        reader.join(timeout=2)
    process.stdout.close()
    if reader.is_alive() or errors:
        raise CodexWorkerError("Codex output capture did not terminate safely")
    if process.returncode is None:
        raise CodexWorkerError("Codex process has no terminal return code")
    return CodexCommandResult(
        process.returncode,
        _bounded_log(captured, truncated=truncated[0], maximum=max_output_bytes),
        timed_out,
    )


class _PolicyBoundCodexWorker:
    """One-job immutable policy binding around a Codex worker."""

    __slots__ = (
        "_worker",
        "_change_policy",
    )

    def __init__(
        self,
        worker: "CodexWorker",
        change_policy: LabWorkerChangePolicy,
    ) -> None:
        self._worker = worker
        self._change_policy = (
            validate_lab_worker_change_policy(
                change_policy
            )
        )

    def run(
        self,
        request: LabWorkerRequest,
    ) -> LabWorkerResult:
        return self._worker._run(
            request,
            change_policy=self._change_policy,
        )


class CodexWorker:
    """Codex implementation of the AutoLabWorker protocol."""

    def __init__(
        self,
        config: CodexWorkerConfig,
        *,
        command_runner: CommandRunner | None = None,
    ):
        self._config = validate_codex_worker_config(
            config
        )
        self._runner = (
            _default_command_runner
            if command_runner is None
            else command_runner
        )

    def bind_change_policy(
        self,
        change_policy: LabWorkerChangePolicy,
    ) -> _PolicyBoundCodexWorker:
        """
        Bind one validated policy without mutating shared worker state.

        The returned adapter grants no authority. The eventual worker
        request must carry the exact matching policy identity.
        """
        return _PolicyBoundCodexWorker(
            self,
            validate_lab_worker_change_policy(
                change_policy
            ),
        )

    def run(
        self,
        request: LabWorkerRequest,
    ) -> LabWorkerResult:
        return self._run(
            request,
            change_policy=None,
        )

    def _run(
        self,
        request: LabWorkerRequest,
        *,
        change_policy: LabWorkerChangePolicy | None,
    ) -> LabWorkerResult:
        request = _validate_request(request)

        trusted_policy = (
            None
            if change_policy is None
            else validate_lab_worker_change_policy(
                change_policy
            )
        )

        if trusted_policy is None:
            if request.change_policy_id is not None:
                raise CodexWorkerError(
                    "policy-bound Codex request requires the exact change policy"
                )
        elif request.change_policy_id != trusted_policy.policy_id:
            raise CodexWorkerError(
                "Codex change policy binding mismatch"
            )

        config = validate_codex_worker_config(
            self._config
        )
        _validate_physical_context(config)

        try:
            capture_lab_workspace_snapshot(
                workspace_parent=request.workspace.parent,
                workspace=request.workspace,
            )
        except LabWorkspaceSnapshotError as exc:
            raise CodexWorkerError(
                f"cannot establish safe Codex workspace: {exc}"
            ) from exc

        command = build_codex_command(
            request=request,
            config=config,
            change_policy=trusted_policy,
        )

        environment = build_codex_control_environment(
            config
        )

        result = self._runner(
            command,
            request.workspace.path,
            environment,
            min(
                request.constraints.max_runtime_seconds,
                config.max_runtime_seconds,
            ),
            min(
                request.constraints.max_log_bytes,
                config.max_output_bytes,
            ),
        )

        if result.timed_out:
            status = STATUS_TIMED_OUT
            summary = (
                "Codex exceeded the bounded runtime; "
                "physical inspection is required."
            )
        elif result.returncode == 0:
            status = STATUS_COMPLETED
            summary = (
                "Codex exited successfully; physical workspace "
                "diff remains authoritative."
            )
        else:
            status = STATUS_FAILED
            summary = (
                "Codex exited unsuccessfully; physical workspace "
                "inspection is required."
            )

        return build_lab_worker_result(
            request=request,
            status=status,
            summary=summary,
            reported_changed_paths=(),
            log=result.log,
        )
