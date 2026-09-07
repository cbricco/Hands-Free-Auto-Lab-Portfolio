"""Fixed private local front door for the Auto Lab coding runtime.

The external caller supplies only canonical coding-request bytes. Machine-local
Auto Lab runtime configuration comes only from the fixed, private Auto Lab
configuration file owned by the current user.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import sys
from collections.abc import Callable, Mapping
from typing import BinaryIO, TextIO

from . import lab_coding_cli as cli
from .lab_coding_runtime_intake import (
    LabCodingRuntime,
    LabCodingRuntimeConfig,
)


CONFIG_DIRECTORY_NAME = "hands-free-auto-lab"
CONFIG_FILE_NAME = "runtime.json"
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_CONFIG_BYTES = 65536
_ERROR_EXIT_STATUS = 2


class LabCodingLocalFrontDoorError(ValueError):
    """The fixed local front-door configuration is unsafe or invalid."""


def _secure_open_flags(*names: str) -> int:
    missing = [
        name
        for name in names
        if not hasattr(os, name)
    ]

    if missing:
        raise LabCodingLocalFrontDoorError(
            "required secure file primitive(s) unavailable: "
            + ", ".join(missing)
        )

    value = 0

    for name in names:
        value |= getattr(os, name)

    return value


def _fixed_config_paths() -> tuple[str, str]:
    try:
        passwd_entry = pwd.getpwuid(os.geteuid())
    except (KeyError, OSError) as exc:
        raise LabCodingLocalFrontDoorError(
            "cannot resolve current-user home directory"
        ) from exc

    home = passwd_entry.pw_dir

    if type(home) is not str or not home:
        raise LabCodingLocalFrontDoorError(
            "current-user home directory is invalid"
        )

    if (
        not os.path.isabs(home)
        or os.path.normpath(home) != home
        or os.path.realpath(home) != home
    ):
        raise LabCodingLocalFrontDoorError(
            "current-user home directory must be canonical"
        )

    config_directory = os.path.join(
        home,
        ".config",
        CONFIG_DIRECTORY_NAME,
    )
    config_file = os.path.join(
        config_directory,
        CONFIG_FILE_NAME,
    )

    if os.path.realpath(config_directory) != config_directory:
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration directory contains a symlink alias"
        )

    if (
        os.path.lexists(config_file)
        and os.path.realpath(config_file) != config_file
    ):
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration file contains a symlink alias"
        )

    return config_directory, config_file


def _stat_signature(value: os.stat_result) -> tuple[int, ...]:
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


def _validate_private_directory(
    value: os.stat_result,
) -> None:
    if not stat.S_ISDIR(value.st_mode):
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration path is not a directory"
        )

    if value.st_uid != os.geteuid():
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration directory is not current-user owned"
        )

    if stat.S_IMODE(value.st_mode) != PRIVATE_DIRECTORY_MODE:
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration directory must have mode 0700"
        )


def _validate_private_file(
    value: os.stat_result,
) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration file is not a regular file"
        )

    if value.st_uid != os.geteuid():
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration file is not current-user owned"
        )

    if stat.S_IMODE(value.st_mode) != PRIVATE_FILE_MODE:
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration file must have mode 0600"
        )

    if value.st_nlink != 1:
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration file must have exactly one hard link"
        )

    if value.st_size > MAX_CONFIG_BYTES:
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration file exceeds size limit"
        )


def _read_all_config_bytes(
    file_descriptor: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0

    while True:
        chunk = os.read(
            file_descriptor,
            min(
                65536,
                MAX_CONFIG_BYTES + 1 - total,
            ),
        )

        if not chunk:
            break

        chunks.append(chunk)
        total += len(chunk)

        if total > MAX_CONFIG_BYTES:
            raise LabCodingLocalFrontDoorError(
                "fixed Auto Lab configuration file exceeds size limit"
            )

    return b"".join(chunks)


def _decode_runtime_environment(
    record_bytes: bytes,
) -> dict[str, str]:
    try:
        text = record_bytes.decode(
            "utf-8",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration must be UTF-8"
        ) from exc

    def object_pairs(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}

        for key, value in pairs:
            if key in result:
                raise LabCodingLocalFrontDoorError(
                    f"duplicate Auto Lab configuration key: {key}"
                )

            result[key] = value

        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=object_pairs,
        )
    except LabCodingLocalFrontDoorError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration is malformed JSON"
        ) from exc

    if type(value) is not dict:
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration must be one JSON object"
        )

    names = set(value)
    allowed = set(cli._ALLOWED_ENVIRONMENT_NAMES)
    required = set(cli._REQUIRED_ENVIRONMENT_NAMES)

    unknown = sorted(names - allowed)

    if unknown:
        raise LabCodingLocalFrontDoorError(
            "unknown Auto Lab configuration key(s): "
            + ", ".join(unknown)
        )

    missing = sorted(required - names)

    if missing:
        raise LabCodingLocalFrontDoorError(
            "missing Auto Lab configuration key(s): "
            + ", ".join(missing)
        )

    environment: dict[str, str] = {}

    for name, item in value.items():
        if type(name) is not str:
            raise LabCodingLocalFrontDoorError(
                "Auto Lab configuration key must be text"
            )

        if type(item) is not str:
            raise LabCodingLocalFrontDoorError(
                f"Auto Lab configuration value must be text: {name}"
            )

        environment[name] = item

    return environment


def load_fixed_runtime_environment() -> dict[str, str]:
    """Read the one fixed private Auto Lab configuration file safely."""
    config_directory, _ = _fixed_config_paths()

    directory_flags = _secure_open_flags(
        "O_RDONLY",
        "O_DIRECTORY",
        "O_NOFOLLOW",
        "O_CLOEXEC",
    )
    file_flags = _secure_open_flags(
        "O_RDONLY",
        "O_NOFOLLOW",
        "O_CLOEXEC",
    )

    directory_fd: int | None = None
    file_fd: int | None = None

    try:
        try:
            directory_fd = os.open(
                config_directory,
                directory_flags,
            )
        except OSError as exc:
            raise LabCodingLocalFrontDoorError(
                "cannot open fixed Auto Lab configuration directory"
            ) from exc

        initial_directory = os.fstat(directory_fd)
        _validate_private_directory(initial_directory)

        try:
            file_fd = os.open(
                CONFIG_FILE_NAME,
                file_flags,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise LabCodingLocalFrontDoorError(
                "cannot open fixed Auto Lab configuration file"
            ) from exc

        initial_file = os.fstat(file_fd)
        _validate_private_file(initial_file)

        record_bytes = _read_all_config_bytes(
            file_fd
        )

        final_file = os.fstat(file_fd)
        named_file = os.stat(
            CONFIG_FILE_NAME,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        final_directory = os.fstat(directory_fd)

        if _stat_signature(initial_file) != _stat_signature(final_file):
            raise LabCodingLocalFrontDoorError(
                "fixed Auto Lab configuration file changed while being read"
            )

        if (
            final_file.st_dev,
            final_file.st_ino,
        ) != (
            named_file.st_dev,
            named_file.st_ino,
        ):
            raise LabCodingLocalFrontDoorError(
                "fixed Auto Lab configuration file identity changed"
            )

        _validate_private_file(named_file)
        _validate_private_directory(final_directory)

        if (
            initial_directory.st_dev,
            initial_directory.st_ino,
        ) != (
            final_directory.st_dev,
            final_directory.st_ino,
        ):
            raise LabCodingLocalFrontDoorError(
                "fixed Auto Lab configuration directory identity changed"
            )

        if len(record_bytes) != final_file.st_size:
            raise LabCodingLocalFrontDoorError(
                "fixed Auto Lab configuration byte count changed"
            )

    except LabCodingLocalFrontDoorError:
        raise
    except OSError as exc:
        raise LabCodingLocalFrontDoorError(
            "fixed Auto Lab configuration inspection failed"
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)

        if directory_fd is not None:
            os.close(directory_fd)

    return _decode_runtime_environment(
        record_bytes
    )


def _reject_caller_auto_lab_configuration(
    environ: Mapping[str, str],
) -> None:
    if not isinstance(environ, Mapping):
        raise LabCodingLocalFrontDoorError(
            "caller environment must be a mapping"
        )

    supplied: list[str] = []

    for name in environ:
        if type(name) is not str:
            raise LabCodingLocalFrontDoorError(
                "caller environment names must be text"
            )

        if name.startswith(cli._ENVIRONMENT_PREFIX):
            supplied.append(name)

    if supplied:
        raise LabCodingLocalFrontDoorError(
            "caller must not supply Auto Lab runtime configuration: "
            + ", ".join(sorted(supplied))
        )


def run_local_front_door(
    *,
    environ: Mapping[str, str],
    stdin: BinaryIO,
    stdout: BinaryIO,
    runtime_factory: Callable[
        [LabCodingRuntimeConfig],
        LabCodingRuntime,
    ] = LabCodingRuntime,
) -> int:
    """Run one canonical request using only fixed Auto Lab-owned config."""
    _reject_caller_auto_lab_configuration(
        environ
    )

    runtime_environment = (
        load_fixed_runtime_environment()
    )

    return cli.run_cli(
        environ=runtime_environment,
        stdin=stdin,
        stdout=stdout,
        runtime_factory=runtime_factory,
    )


def main(
    *,
    environ: Mapping[str, str] | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: TextIO | None = None,
    runtime_factory: Callable[
        [LabCodingRuntimeConfig],
        LabCodingRuntime,
    ] = LabCodingRuntime,
) -> int:
    """Process one request through the fixed local Auto Lab front door."""
    effective_environ = (
        os.environ
        if environ is None
        else environ
    )
    effective_stdin = (
        sys.stdin.buffer
        if stdin is None
        else stdin
    )
    effective_stdout = (
        sys.stdout.buffer
        if stdout is None
        else stdout
    )
    effective_stderr = (
        sys.stderr
        if stderr is None
        else stderr
    )

    try:
        return run_local_front_door(
            environ=effective_environ,
            stdin=effective_stdin,
            stdout=effective_stdout,
            runtime_factory=runtime_factory,
        )
    except Exception as exc:
        effective_stderr.write(
            "lab_coding_local_front_door: "
            f"{type(exc).__name__}: {exc}\n"
        )
        effective_stderr.flush()
        return _ERROR_EXIT_STATUS


if __name__ == "__main__":
    raise SystemExit(main())
