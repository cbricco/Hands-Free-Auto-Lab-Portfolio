"""Thin environment-configured front door for the coding runtime."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import BinaryIO, TextIO

from .lab_codex_worker import build_codex_worker_config
from .lab_coding_runtime_intake import (
    LabCodingRuntime,
    LabCodingRuntimeConfig,
    validate_lab_coding_runtime_config,
)


class LabCodingCliError(ValueError):
    """Raised when local CLI configuration or stream data is invalid."""


ENV_CODEX_EXECUTABLE = "HF_AUTO_LAB_CODEX_EXECUTABLE"
ENV_CODEX_HOME = "HF_AUTO_LAB_CODEX_HOME"
ENV_CODEX_RELEASE_ROOT = "HF_AUTO_LAB_CODEX_RELEASE_ROOT"
ENV_RUNTIME_TEMP = "HF_AUTO_LAB_RUNTIME_TEMP"
ENV_WORKSPACE_PARENT = "HF_AUTO_LAB_WORKSPACE_PARENT"
ENV_CANDIDATE_STORE_ROOT = "HF_AUTO_LAB_CANDIDATE_STORE_ROOT"
ENV_LIFECYCLE_JOURNAL_ROOT = "HF_AUTO_LAB_LIFECYCLE_JOURNAL_ROOT"
ENV_MAX_RUNTIME_SECONDS = "HF_AUTO_LAB_MAX_RUNTIME_SECONDS"
ENV_MAX_OUTPUT_BYTES = "HF_AUTO_LAB_MAX_OUTPUT_BYTES"
ENV_MAX_SEED_FILES = "HF_AUTO_LAB_MAX_SEED_FILES"
ENV_MAX_SEED_BYTES = "HF_AUTO_LAB_MAX_SEED_BYTES"
ENV_TRUSTED_CLIENT_PROXY = "HF_AUTO_LAB_TRUSTED_CLIENT_PROXY"

_REQUIRED_ENVIRONMENT_NAMES = frozenset(
    {
        ENV_CODEX_EXECUTABLE,
        ENV_CODEX_HOME,
        ENV_CODEX_RELEASE_ROOT,
        ENV_RUNTIME_TEMP,
        ENV_WORKSPACE_PARENT,
        ENV_CANDIDATE_STORE_ROOT,
        ENV_LIFECYCLE_JOURNAL_ROOT,
        ENV_MAX_RUNTIME_SECONDS,
        ENV_MAX_OUTPUT_BYTES,
        ENV_MAX_SEED_FILES,
        ENV_MAX_SEED_BYTES,
    }
)

_OPTIONAL_ENVIRONMENT_NAMES = frozenset(
    {
        ENV_TRUSTED_CLIENT_PROXY,
    }
)

_ALLOWED_ENVIRONMENT_NAMES = (
    _REQUIRED_ENVIRONMENT_NAMES | _OPTIONAL_ENVIRONMENT_NAMES
)

_ENVIRONMENT_PREFIX = "HF_AUTO_LAB_"
_ERROR_EXIT_STATUS = 2


def _reject_unknown_auto_lab_environment(
    environ: Mapping[str, str],
) -> None:
    unknown = sorted(
        name
        for name in environ
        if name.startswith(_ENVIRONMENT_PREFIX)
        and name not in _ALLOWED_ENVIRONMENT_NAMES
    )
    if unknown:
        raise LabCodingCliError(
            "unknown Auto Lab environment variable(s): "
            + ", ".join(unknown)
        )


def _required_environment_text(
    environ: Mapping[str, str],
    name: str,
) -> str:
    if name not in environ:
        raise LabCodingCliError(
            f"required environment variable is missing: {name}"
        )

    value = environ[name]

    if type(value) is not str:
        raise LabCodingCliError(
            f"{name} must be text"
        )

    if value == "":
        raise LabCodingCliError(
            f"{name} must not be empty"
        )

    return value


def _required_environment_positive_integer(
    environ: Mapping[str, str],
    name: str,
) -> int:
    raw = _required_environment_text(
        environ,
        name,
    )

    if not all("0" <= character <= "9" for character in raw):
        raise LabCodingCliError(
            f"{name} must be a canonical positive decimal integer"
        )

    value = int(raw, 10)

    if value <= 0 or raw != str(value):
        raise LabCodingCliError(
            f"{name} must be a canonical positive decimal integer"
        )

    return value


def _optional_proxy(
    environ: Mapping[str, str],
) -> str | None:
    if ENV_TRUSTED_CLIENT_PROXY not in environ:
        return None

    value = environ[ENV_TRUSTED_CLIENT_PROXY]

    if type(value) is not str or value == "":
        raise LabCodingCliError(
            f"{ENV_TRUSTED_CLIENT_PROXY} must be non-empty text when supplied"
        )

    return value


def build_lab_coding_runtime_config_from_environment(
    environ: Mapping[str, str],
) -> LabCodingRuntimeConfig:
    """Build validated local runtime configuration from explicit environment."""
    if not isinstance(environ, Mapping):
        raise LabCodingCliError(
            "environment must be a mapping"
        )

    _reject_unknown_auto_lab_environment(environ)

    worker_config = build_codex_worker_config(
        codex_executable=_required_environment_text(
            environ,
            ENV_CODEX_EXECUTABLE,
        ),
        codex_home=_required_environment_text(
            environ,
            ENV_CODEX_HOME,
        ),
        codex_release_root=_required_environment_text(
            environ,
            ENV_CODEX_RELEASE_ROOT,
        ),
        runtime_temp=_required_environment_text(
            environ,
            ENV_RUNTIME_TEMP,
        ),
        max_runtime_seconds=_required_environment_positive_integer(
            environ,
            ENV_MAX_RUNTIME_SECONDS,
        ),
        max_output_bytes=_required_environment_positive_integer(
            environ,
            ENV_MAX_OUTPUT_BYTES,
        ),
        trusted_client_proxy=_optional_proxy(environ),
    )

    config = LabCodingRuntimeConfig(
        codex_worker_config=worker_config,
        workspace_parent=_required_environment_text(
            environ,
            ENV_WORKSPACE_PARENT,
        ),
        candidate_store_root=_required_environment_text(
            environ,
            ENV_CANDIDATE_STORE_ROOT,
        ),
        lifecycle_journal_root=_required_environment_text(
            environ,
            ENV_LIFECYCLE_JOURNAL_ROOT,
        ),
        max_seed_files=_required_environment_positive_integer(
            environ,
            ENV_MAX_SEED_FILES,
        ),
        max_seed_bytes=_required_environment_positive_integer(
            environ,
            ENV_MAX_SEED_BYTES,
        ),
    )

    return validate_lab_coding_runtime_config(config)


def run_cli(
    *,
    environ: Mapping[str, str],
    stdin: BinaryIO,
    stdout: BinaryIO,
    runtime_factory: Callable[
        [LabCodingRuntimeConfig],
        LabCodingRuntime,
    ] = LabCodingRuntime,
) -> int:
    """Run exactly one canonical coding request through the existing runtime."""
    config = build_lab_coding_runtime_config_from_environment(
        environ
    )

    record_bytes = stdin.read()

    if type(record_bytes) is not bytes:
        raise LabCodingCliError(
            "stdin must provide bytes"
        )

    runtime = runtime_factory(config)

    result_bytes = runtime.run_request_bytes(
        record_bytes
    )

    if type(result_bytes) is not bytes:
        raise LabCodingCliError(
            "coding runtime must return bytes"
        )

    stdout.write(result_bytes)
    stdout.flush()

    return 0


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
    """Process one stdin request and return a deterministic process status."""
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
        return run_cli(
            environ=effective_environ,
            stdin=effective_stdin,
            stdout=effective_stdout,
            runtime_factory=runtime_factory,
        )
    except Exception as exc:
        effective_stderr.write(
            "lab_coding_cli: "
            f"{type(exc).__name__}: {exc}\n"
        )
        effective_stderr.flush()
        return _ERROR_EXIT_STATUS


if __name__ == "__main__":
    raise SystemExit(main())
