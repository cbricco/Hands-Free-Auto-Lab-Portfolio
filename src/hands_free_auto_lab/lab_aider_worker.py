"""Pinned Aider coding-worker adapter for Hands-Free Auto Lab.

This adapter is an execution mechanism beneath the backend-neutral worker
contract.  It is not an authority boundary.

The adapter:

* accepts one already-built LabWorkerRequest,
* requires the exact V1 workspace capabilities,
* revalidates the physical disposable workspace,
* requires an explicit bounded set of Aider context paths,
* requires a private pre-existing Unix-socket Ollama relay,
* launches the pinned Aider image with Docker network mode "none",
* mounts only the disposable workspace and relay directory,
* uses a read-only container root filesystem,
* drops all Linux capabilities,
* enables no-new-privileges,
* bounds worker runtime and captured output,
* deterministically cleans up only its exact request-labelled container,
* returns LabWorkerResult worker-reported evidence.

It deliberately does not:

* create or authorize promotion,
* modify a real repository,
* grant arbitrary IP networking,
* expose the Docker socket to the worker,
* expose the host home directory,
* start or configure Ollama,
* create the host Ollama allow-list relay,
* treat Aider's output as physical workspace truth,
* infer trustworthy changed paths from Aider output.

The independent workspace snapshot/diff layer remains authoritative for
physical change evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import PurePosixPath
import signal
import socket
import stat
import subprocess
import threading
from typing import Callable

from .lab_worker import (
    CAPABILITY_RUN_PROCESS,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    LabWorkerRequest,
    LabWorkerResult,
    build_lab_worker_result,
    validate_lab_worker_request,
)
from .lab_workspace_snapshot import (
    ENTRY_FILE,
    LabWorkspaceSnapshotError,
    capture_lab_workspace_snapshot,
)


AIDER_WORKER_NAME = "aider-qwen2.5-7b-v1"

AIDER_IMAGE_REPOSITORY = "docker.io/paulgauthier/aider"

AIDER_IMAGE_DIGEST = (
    "sha256:"
    "764924922f1f9a47e1185ebaa72e6435027af657deaff3febf0f20a176403c1e"
)

AIDER_IMAGE = (
    f"{AIDER_IMAGE_REPOSITORY}@{AIDER_IMAGE_DIGEST}"
)

AIDER_MODEL = "ollama_chat/qwen2.5:7b"

DOCKER_BINARY = "/usr/bin/docker"

RELAY_CONTAINER_DIRECTORY = "/relay"
RELAY_SOCKET_NAME = "ollama.sock"
RELAY_CONTAINER_SOCKET = (
    f"{RELAY_CONTAINER_DIRECTORY}/{RELAY_SOCKET_NAME}"
)

WORKSPACE_CONTAINER_DIRECTORY = "/workspace"

MAX_TEST_COMMAND_BYTES = 64 * 1024
MAX_CONTEXT_PATHS = 512
MAX_CONTEXT_PATH_BYTES = 4096

DOCKER_CONTROL_TIMEOUT_SECONDS = 15
DOCKER_CONTROL_LOG_BYTES = 64 * 1024

_CONTAINER_NAME_PREFIX = (
    "hands-free-auto-lab-aider-"
)

_REQUEST_LABEL = (
    "hands-free-auto-lab.request_id"
)

_REQUIRED_CAPABILITIES = tuple(
    sorted(
        (
            CAPABILITY_RUN_PROCESS,
            CAPABILITY_WORKSPACE_READ,
            CAPABILITY_WORKSPACE_WRITE,
        )
    )
)


class AiderWorkerError(
    RuntimeError
):
    """The pinned Aider worker could not run inside its exact boundary."""


@dataclass(
    frozen=True,
    slots=True,
)
class AiderWorkerConfig:
    """Per-run context selected by trusted Auto Lab orchestration."""

    relay_directory: str
    editable_paths: tuple[
        str,
        ...,
    ]
    read_only_paths: tuple[
        str,
        ...,
    ]
    test_command: str | None


@dataclass(
    frozen=True,
    slots=True,
)
class AiderCommandResult:
    """Bounded result of one trusted host-side Docker control command."""

    returncode: int
    log: str
    timed_out: bool


CommandRunner = Callable[
    [
        tuple[
            str,
            ...,
        ],
        int,
        int,
    ],
    AiderCommandResult,
]


_CONTAINER_LAUNCHER = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import threading


def pump(
    source: socket.socket,
    destination: socket.socket,
) -> None:
    try:
        while True:
            data = source.recv(
                65536
            )

            if not data:
                break

            destination.sendall(
                data
            )

    except OSError:
        pass

    finally:
        try:
            destination.shutdown(
                socket.SHUT_WR
            )
        except OSError:
            pass


def bridge(
    client: socket.socket,
    unix_socket: str,
) -> None:
    upstream = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )

    try:
        upstream.connect(
            unix_socket
        )

        first = threading.Thread(
            target=pump,
            args=(
                client,
                upstream,
            ),
            daemon=True,
        )

        second = threading.Thread(
            target=pump,
            args=(
                upstream,
                client,
            ),
            daemon=True,
        )

        first.start()
        second.start()

        first.join()
        second.join()

    finally:
        try:
            upstream.close()
        finally:
            client.close()


def serve_proxy(
    unix_socket: str,
    ready: threading.Event,
) -> None:
    server = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    )

    server.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1,
    )

    server.bind(
        (
            "127.0.0.1",
            11434,
        )
    )

    server.listen(
        16
    )

    ready.set()

    while True:
        client, _ = server.accept()

        threading.Thread(
            target=bridge,
            args=(
                client,
                unix_socket,
            ),
            daemon=True,
        ).start()


def main() -> int:
    goal = os.environ[
        "AUTO_LAB_AIDER_GOAL"
    ]

    editable = json.loads(
        os.environ[
            "AUTO_LAB_AIDER_EDITABLE"
        ]
    )

    read_only = json.loads(
        os.environ[
            "AUTO_LAB_AIDER_READ_ONLY"
        ]
    )

    test_command = os.environ.get(
        "AUTO_LAB_AIDER_TEST_COMMAND",
        "",
    )

    unix_socket = os.environ[
        "AUTO_LAB_OLLAMA_SOCKET"
    ]

    model = os.environ[
        "AUTO_LAB_AIDER_MODEL"
    ]

    home = Path(
        "/tmp/aider-home"
    )

    home.mkdir(
        mode=0o700,
        parents=True,
        exist_ok=False,
    )

    ready = threading.Event()

    threading.Thread(
        target=serve_proxy,
        args=(
            unix_socket,
            ready,
        ),
        daemon=True,
    ).start()

    if not ready.wait(
        timeout=3
    ):
        raise RuntimeError(
            "container-local Ollama proxy did not become ready"
        )

    environment = os.environ.copy()

    environment[
        "HOME"
    ] = str(
        home
    )

    environment[
        "OLLAMA_API_BASE"
    ] = "http://127.0.0.1:11434"

    environment[
        "PYTHONDONTWRITEBYTECODE"
    ] = "1"

    command = [
        "/venv/bin/aider",
        "--model",
        model,
    ]

    for relative in editable:
        command.extend(
            [
                "--file",
                "/workspace/"
                + relative,
            ]
        )

    for relative in read_only:
        command.extend(
            [
                "--read",
                "/workspace/"
                + relative,
            ]
        )

    command.extend(
        [
            "--message",
            goal,
        ]
    )

    if test_command:
        command.extend(
            [
                "--test-cmd",
                test_command,
                "--auto-test",
            ]
        )

    command.extend(
        [
            "--no-auto-lint",
            "--no-git",
            "--no-gitignore",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--map-tokens",
            "0",
            "--no-analytics",
            "--no-check-update",
            "--no-show-release-notes",
            "--no-show-model-warnings",
            "--no-check-model-accepts-settings",
            "--no-suggest-shell-commands",
            "--no-detect-urls",
            "--no-fancy-input",
            "--no-pretty",
            "--no-stream",
            "--disable-playwright",
            "--timeout",
            "120",
            "--input-history-file",
            "/tmp/aider-home/input.history",
            "--chat-history-file",
            "/tmp/aider-home/chat.history.md",
            "--llm-history-file",
            "/tmp/aider-home/llm.history",
        ]
    )

    completed = subprocess.run(
        command,
        cwd="/workspace",
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
'''


def _require_text(
    value: object,
    *,
    name: str,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise AiderWorkerError(
            f"{name} must be a string"
        )

    if (
        not allow_empty
        and not value.strip()
    ):
        raise AiderWorkerError(
            f"{name} must not be empty"
        )

    if "\x00" in value:
        raise AiderWorkerError(
            f"{name} must not contain NUL"
        )

    try:
        encoded = value.encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise AiderWorkerError(
            f"{name} must be UTF-8 encodable"
        ) from exc

    if len(
        encoded
    ) > maximum_bytes:
        raise AiderWorkerError(
            f"{name} exceeds maximum size"
        )

    return value


def _canonical_context_path(
    value: object,
) -> str:
    value = _require_text(
        value,
        name="Aider context path",
        maximum_bytes=MAX_CONTEXT_PATH_BYTES,
    )

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
        raise AiderWorkerError(
            "Aider context paths must be canonical relative paths"
        )

    if "," in value:
        raise AiderWorkerError(
            "Aider context paths must not contain commas"
        )

    return value


def _canonical_host_directory(
    value: object,
) -> str:
    value = _require_text(
        value,
        name="relay_directory",
        maximum_bytes=4096,
    )

    if not os.path.isabs(
        value
    ):
        raise AiderWorkerError(
            "relay_directory must be absolute"
        )

    if os.path.normpath(
        value
    ) != value:
        raise AiderWorkerError(
            "relay_directory must be canonical"
        )

    if "," in value:
        raise AiderWorkerError(
            "relay_directory must not contain a comma"
        )

    return value


def validate_aider_worker_config(
    config: object,
) -> AiderWorkerConfig:
    if not isinstance(
        config,
        AiderWorkerConfig,
    ):
        raise AiderWorkerError(
            "config must be an AiderWorkerConfig"
        )

    relay_directory = _canonical_host_directory(
        config.relay_directory
    )

    if not isinstance(
        config.editable_paths,
        tuple,
    ):
        raise AiderWorkerError(
            "editable_paths must be a tuple"
        )

    if not isinstance(
        config.read_only_paths,
        tuple,
    ):
        raise AiderWorkerError(
            "read_only_paths must be a tuple"
        )

    editable = tuple(
        _canonical_context_path(
            item
        )
        for item in config.editable_paths
    )

    read_only = tuple(
        _canonical_context_path(
            item
        )
        for item in config.read_only_paths
    )

    if not editable:
        raise AiderWorkerError(
            "at least one editable Aider context path is required"
        )

    if (
        len(
            editable
        )
        + len(
            read_only
        )
        > MAX_CONTEXT_PATHS
    ):
        raise AiderWorkerError(
            "too many Aider context paths"
        )

    if tuple(
        sorted(
            editable
        )
    ) != editable:
        raise AiderWorkerError(
            "editable_paths must be in canonical sorted order"
        )

    if tuple(
        sorted(
            read_only
        )
    ) != read_only:
        raise AiderWorkerError(
            "read_only_paths must be in canonical sorted order"
        )

    if len(
        set(
            editable
        )
    ) != len(
        editable
    ):
        raise AiderWorkerError(
            "editable_paths must not contain duplicates"
        )

    if len(
        set(
            read_only
        )
    ) != len(
        read_only
    ):
        raise AiderWorkerError(
            "read_only_paths must not contain duplicates"
        )

    overlap = (
        set(
            editable
        )
        & set(
            read_only
        )
    )

    if overlap:
        raise AiderWorkerError(
            "editable and read-only context paths must not overlap"
        )

    test_command: str | None

    if config.test_command is None:
        test_command = None

    else:
        test_command = _require_text(
            config.test_command,
            name="test_command",
            maximum_bytes=MAX_TEST_COMMAND_BYTES,
        )

    if relay_directory != config.relay_directory:
        raise AiderWorkerError(
            "relay_directory is not canonical"
        )

    if editable != config.editable_paths:
        raise AiderWorkerError(
            "editable_paths are not canonical"
        )

    if read_only != config.read_only_paths:
        raise AiderWorkerError(
            "read_only_paths are not canonical"
        )

    if test_command != config.test_command:
        raise AiderWorkerError(
            "test_command is not canonical"
        )

    return config


def build_aider_worker_config(
    *,
    relay_directory: str,
    editable_paths: tuple[
        str,
        ...,
    ],
    read_only_paths: tuple[
        str,
        ...,
    ] = (),
    test_command: str | None = None,
) -> AiderWorkerConfig:
    config = AiderWorkerConfig(
        relay_directory=_canonical_host_directory(
            relay_directory
        ),
        editable_paths=tuple(
            sorted(
                _canonical_context_path(
                    item
                )
                for item in editable_paths
            )
        ),
        read_only_paths=tuple(
            sorted(
                _canonical_context_path(
                    item
                )
                for item in read_only_paths
            )
        ),
        test_command=(
            None
            if test_command is None
            else _require_text(
                test_command,
                name="test_command",
                maximum_bytes=MAX_TEST_COMMAND_BYTES,
            )
        ),
    )

    return validate_aider_worker_config(
        config
    )


def _validate_request(
    request: LabWorkerRequest,
) -> LabWorkerRequest:
    request = validate_lab_worker_request(
        request
    )

    if request.worker != AIDER_WORKER_NAME:
        raise AiderWorkerError(
            "worker request is not bound to this Aider adapter"
        )

    if (
        request.constraints.capabilities
        != _REQUIRED_CAPABILITIES
    ):
        raise AiderWorkerError(
            "Aider V1 requires exactly the three bounded workspace capabilities"
        )

    if "," in request.workspace.path:
        raise AiderWorkerError(
            "workspace path must not contain a comma"
        )

    return request


def _validate_context_against_snapshot(
    *,
    request: LabWorkerRequest,
    config: AiderWorkerConfig,
) -> None:
    try:
        snapshot = capture_lab_workspace_snapshot(
            workspace_parent=request.workspace.parent,
            workspace=request.workspace,
        )
    except LabWorkspaceSnapshotError as exc:
        raise AiderWorkerError(
            f"cannot establish safe Aider input workspace: {exc}"
        ) from exc

    file_paths = {
        entry.path
        for entry in snapshot.entries
        if entry.kind == ENTRY_FILE
    }

    requested = (
        config.editable_paths
        + config.read_only_paths
    )

    missing = tuple(
        path
        for path in requested
        if path not in file_paths
    )

    if missing:
        raise AiderWorkerError(
            "Aider context path is not a safely observed regular file: "
            + ", ".join(
                missing
            )
        )


def _relay_directory_flags() -> int:
    for name in (
        "O_DIRECTORY",
        "O_NOFOLLOW",
    ):
        if not hasattr(
            os,
            name,
        ):
            raise AiderWorkerError(
                f"secure relay primitive unavailable: os.{name}"
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


def _validate_relay_runtime(
    config: AiderWorkerConfig,
) -> None:
    expected_uid = os.getuid()

    try:
        directory_fd = os.open(
            config.relay_directory,
            _relay_directory_flags(),
        )
    except OSError as exc:
        raise AiderWorkerError(
            f"cannot securely open relay directory: {exc}"
        ) from exc

    try:
        directory_stat = os.fstat(
            directory_fd
        )

        if not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            raise AiderWorkerError(
                "relay path is not a directory"
            )

        if directory_stat.st_uid != expected_uid:
            raise AiderWorkerError(
                "relay directory is not owned by current user"
            )

        if stat.S_IMODE(
            directory_stat.st_mode
        ) != 0o700:
            raise AiderWorkerError(
                "relay directory must have mode 0700"
            )

        try:
            entries = os.listdir(
                directory_fd
            )
        except OSError as exc:
            raise AiderWorkerError(
                f"cannot enumerate relay directory: {exc}"
            ) from exc

        if entries != [
            RELAY_SOCKET_NAME
        ]:
            raise AiderWorkerError(
                "relay directory must contain only the expected Ollama socket"
            )

        try:
            socket_stat = os.stat(
                RELAY_SOCKET_NAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise AiderWorkerError(
                f"cannot inspect Ollama relay socket: {exc}"
            ) from exc

        if not stat.S_ISSOCK(
            socket_stat.st_mode
        ):
            raise AiderWorkerError(
                "Ollama relay endpoint is not a Unix socket"
            )

        if socket_stat.st_uid != expected_uid:
            raise AiderWorkerError(
                "Ollama relay socket is not owned by current user"
            )

        if stat.S_IMODE(
            socket_stat.st_mode
        ) != 0o600:
            raise AiderWorkerError(
                "Ollama relay socket must have mode 0600"
            )

        if socket_stat.st_nlink != 1:
            raise AiderWorkerError(
                "Ollama relay socket has unexpected link count"
            )

    finally:
        os.close(
            directory_fd
        )


def _container_name(
    request: LabWorkerRequest,
) -> str:
    return (
        _CONTAINER_NAME_PREFIX
        + request.request_id[
            :24
        ]
    )


def build_aider_docker_command(
    *,
    request: LabWorkerRequest,
    config: AiderWorkerConfig,
) -> tuple[
    str,
    ...,
]:
    """Build the exact pinned Docker invocation without executing it."""
    request = _validate_request(
        request
    )

    config = validate_aider_worker_config(
        config
    )

    container_name = _container_name(
        request
    )

    editable_json = json.dumps(
        list(
            config.editable_paths
        ),
        separators=(
            ",",
            ":",
        ),
        ensure_ascii=False,
    )

    read_only_json = json.dumps(
        list(
            config.read_only_paths
        ),
        separators=(
            ",",
            ":",
        ),
        ensure_ascii=False,
    )

    read_only_mounts: list[
        str
    ] = []

    for relative in config.read_only_paths:
        host_path = os.path.join(
            request.workspace.path,
            *PurePosixPath(
                relative
            ).parts,
        )

        container_path = (
            WORKSPACE_CONTAINER_DIRECTORY
            + "/"
            + relative
        )

        read_only_mounts.extend(
            [
                "--mount",
                (
                    "type=bind,"
                    f"src={host_path},"
                    f"dst={container_path},"
                    "readonly"
                ),
            ]
        )

    command = [
        DOCKER_BINARY,
        "run",
        "--name",
        container_name,
        "--label",
        (
            f"{_REQUEST_LABEL}="
            f"{request.request_id}"
        ),
        "--pull=never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        "3g",
        "--cpus",
        "2",
        "--tmpfs",
        (
            "/tmp:"
            "rw,nosuid,nodev,noexec,"
            "size=512m,mode=1777"
        ),
        "--mount",
        (
            "type=bind,"
            f"src={request.workspace.path},"
            "dst=/workspace"
        ),
        *read_only_mounts,
        "--mount",
        (
            "type=bind,"
            f"src={config.relay_directory},"
            "dst=/relay,"
            "readonly"
        ),
        "--env",
        "HOME=/tmp/aider-home",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        (
            "AUTO_LAB_AIDER_GOAL="
            + request.goal
        ),
        "--env",
        (
            "AUTO_LAB_AIDER_EDITABLE="
            + editable_json
        ),
        "--env",
        (
            "AUTO_LAB_AIDER_READ_ONLY="
            + read_only_json
        ),
        "--env",
        (
            "AUTO_LAB_AIDER_TEST_COMMAND="
            + (
                config.test_command
                or ""
            )
        ),
        "--env",
        (
            "AUTO_LAB_OLLAMA_SOCKET="
            + RELAY_CONTAINER_SOCKET
        ),
        "--env",
        (
            "AUTO_LAB_AIDER_MODEL="
            + AIDER_MODEL
        ),
        "--entrypoint",
        "/usr/local/bin/python3",
        AIDER_IMAGE,
        "-c",
        _CONTAINER_LAUNCHER,
    ]

    return tuple(
        command
    )


def _bounded_log(
    data: bytearray,
    *,
    truncated: bool,
    maximum_bytes: int,
) -> str:
    marker = (
        b"\n[output truncated by Auto Lab]\n"
    )

    raw = bytes(
        data
    )

    if truncated:
        available = max(
            0,
            maximum_bytes
            - len(
                marker
            ),
        )

        raw = (
            raw[
                :available
            ]
            + marker[
                :maximum_bytes
            ]
        )

    raw = raw[
        :maximum_bytes
    ]

    return raw.decode(
        "utf-8",
        errors="replace",
    )


def _terminate_process_group(
    process: subprocess.Popen[
        bytes
    ],
) -> None:
    try:
        os.killpg(
            process.pid,
            signal.SIGTERM,
        )
    except ProcessLookupError:
        return

    try:
        process.wait(
            timeout=2
        )
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(
            process.pid,
            signal.SIGKILL,
        )
    except ProcessLookupError:
        pass

    try:
        process.wait(
            timeout=2
        )
    except subprocess.TimeoutExpired as exc:
        raise AiderWorkerError(
            "host Docker control process could not be terminated"
        ) from exc


def _default_command_runner(
    argv: tuple[
        str,
        ...,
    ],
    timeout_seconds: int,
    max_log_bytes: int,
) -> AiderCommandResult:
    if (
        not isinstance(
            argv,
            tuple,
        )
        or not argv
    ):
        raise AiderWorkerError(
            "command runner requires a non-empty argv tuple"
        )

    if (
        not isinstance(
            timeout_seconds,
            int,
        )
        or isinstance(
            timeout_seconds,
            bool,
        )
        or timeout_seconds < 1
    ):
        raise AiderWorkerError(
            "command runner timeout is invalid"
        )

    if (
        not isinstance(
            max_log_bytes,
            int,
        )
        or isinstance(
            max_log_bytes,
            bool,
        )
        or max_log_bytes < 1
    ):
        raise AiderWorkerError(
            "command runner log limit is invalid"
        )

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise AiderWorkerError(
            f"cannot start trusted Docker control command: {exc}"
        ) from exc

    if process.stdout is None:
        _terminate_process_group(
            process
        )

        raise AiderWorkerError(
            "Docker control process stdout pipe was not created"
        )

    captured = bytearray()
    truncated = [
        False
    ]
    reader_error: list[
        BaseException
    ] = []

    def drain() -> None:
        try:
            while True:
                chunk = os.read(
                    process.stdout.fileno(),
                    64 * 1024,
                )

                if not chunk:
                    return

                remaining = (
                    max_log_bytes
                    - len(
                        captured
                    )
                )

                if remaining > 0:
                    captured.extend(
                        chunk[
                            :remaining
                        ]
                    )

                if len(
                    chunk
                ) > remaining:
                    truncated[
                        0
                    ] = True

        except BaseException as exc:
            reader_error.append(
                exc
            )

    reader = threading.Thread(
        target=drain,
        daemon=True,
    )

    reader.start()

    timed_out = False

    try:
        process.wait(
            timeout=timeout_seconds
        )

    except subprocess.TimeoutExpired:
        timed_out = True

        _terminate_process_group(
            process
        )

    reader.join(
        timeout=5
    )

    if reader.is_alive():
        try:
            process.stdout.close()
        except OSError:
            pass

        reader.join(
            timeout=2
        )

    if reader.is_alive():
        raise AiderWorkerError(
            "Docker control output reader did not terminate"
        )

    if reader_error:
        raise AiderWorkerError(
            f"Docker control output capture failed: {reader_error[0]}"
        )

    returncode = process.returncode

    if returncode is None:
        raise AiderWorkerError(
            "Docker control process has no terminal return code"
        )

    return AiderCommandResult(
        returncode=returncode,
        log=_bounded_log(
            captured,
            truncated=truncated[
                0
            ],
            maximum_bytes=max_log_bytes,
        ),
        timed_out=timed_out,
    )


@dataclass(
    frozen=True,
    slots=True,
)
class _ContainerState:
    request_id: str
    running: bool


def _container_inspect_command(
    name: str,
) -> tuple[
    str,
    ...,
]:
    return (
        DOCKER_BINARY,
        "container",
        "inspect",
        "--format",
        (
            '{{index .Config.Labels '
            f'"{_REQUEST_LABEL}"}}'
            '|{{.State.Running}}'
        ),
        name,
    )


def _container_state(
    *,
    name: str,
    runner: CommandRunner,
) -> _ContainerState | None:
    result = runner(
        _container_inspect_command(
            name
        ),
        DOCKER_CONTROL_TIMEOUT_SECONDS,
        DOCKER_CONTROL_LOG_BYTES,
    )

    if result.timed_out:
        raise AiderWorkerError(
            "Docker container inspection timed out"
        )

    if result.returncode != 0:
        lowered = result.log.lower()

        if (
            "no such object" in lowered
            or "no such container" in lowered
        ):
            return None

        raise AiderWorkerError(
            "Docker container inspection failed closed: "
            + result.log.strip()
        )

    line = result.log.strip()

    parts = line.split(
        "|"
    )

    if len(
        parts
    ) != 2:
        raise AiderWorkerError(
            "Docker container inspection returned unexpected structure"
        )

    request_id, running_text = parts

    if running_text == "true":
        running = True

    elif running_text == "false":
        running = False

    else:
        raise AiderWorkerError(
            "Docker container inspection returned invalid running state"
        )

    return _ContainerState(
        request_id=request_id,
        running=running,
    )


def _remove_bound_container(
    *,
    name: str,
    request_id: str,
    runner: CommandRunner,
) -> None:
    state = _container_state(
        name=name,
        runner=runner,
    )

    if state is None:
        return

    if state.request_id != request_id:
        raise AiderWorkerError(
            "refusing to remove container with mismatched request label"
        )

    command = [
        DOCKER_BINARY,
        "rm",
    ]

    if state.running:
        command.append(
            "--force"
        )

    command.append(
        name
    )

    result = runner(
        tuple(
            command
        ),
        DOCKER_CONTROL_TIMEOUT_SECONDS,
        DOCKER_CONTROL_LOG_BYTES,
    )

    if (
        result.timed_out
        or result.returncode != 0
    ):
        raise AiderWorkerError(
            "could not remove exact request-bound Aider container"
        )

    if _container_state(
        name=name,
        runner=runner,
    ) is not None:
        raise AiderWorkerError(
            "request-bound Aider container remained after cleanup"
        )


class AiderWorker:
    """Pinned Aider implementation of the AutoLabWorker protocol."""

    def __init__(
        self,
        config: AiderWorkerConfig,
        *,
        command_runner: CommandRunner | None = None,
    ):
        self._config = validate_aider_worker_config(
            config
        )

        self._runner = (
            _default_command_runner
            if command_runner is None
            else command_runner
        )

    def run(
        self,
        request: LabWorkerRequest,
    ) -> LabWorkerResult:
        request = _validate_request(
            request
        )

        config = validate_aider_worker_config(
            self._config
        )

        _validate_context_against_snapshot(
            request=request,
            config=config,
        )

        _validate_relay_runtime(
            config
        )

        container_name = _container_name(
            request
        )

        existing = _container_state(
            name=container_name,
            runner=self._runner,
        )

        if existing is not None:
            raise AiderWorkerError(
                "request-bound Aider container name already exists; "
                "refusing to delete or reuse it"
            )

        command = build_aider_docker_command(
            request=request,
            config=config,
        )

        run_result = self._runner(
            command,
            request.constraints.max_runtime_seconds,
            request.constraints.max_log_bytes,
        )

        state = _container_state(
            name=container_name,
            runner=self._runner,
        )

        if state is not None:
            if state.request_id != request.request_id:
                raise AiderWorkerError(
                    "Aider container request label changed unexpectedly"
                )

            if (
                not run_result.timed_out
                and state.running
            ):
                _remove_bound_container(
                    name=container_name,
                    request_id=request.request_id,
                    runner=self._runner,
                )

                raise AiderWorkerError(
                    "Aider container remained running after Docker command returned"
                )

            _remove_bound_container(
                name=container_name,
                request_id=request.request_id,
                runner=self._runner,
            )

        elif (
            run_result.returncode == 0
            and not run_result.timed_out
        ):
            raise AiderWorkerError(
                "successful Docker run returned without its expected "
                "non-auto-removed request-bound container"
            )

        if run_result.timed_out:
            status = STATUS_TIMED_OUT
            summary = (
                "Aider worker exceeded the bounded runtime; "
                "independent physical workspace inspection is required."
            )

        elif run_result.returncode == 0:
            status = STATUS_COMPLETED
            summary = (
                "Aider worker exited successfully; "
                "physical workspace diff remains the authoritative "
                "change evidence."
            )

        else:
            status = STATUS_FAILED
            summary = (
                "Aider worker exited unsuccessfully; "
                "physical workspace inspection remains required."
            )

        return build_lab_worker_result(
            request=request,
            status=status,
            summary=summary,
            reported_changed_paths=(),
            log=run_result.log,
        )
