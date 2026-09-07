"""Auto Lab-owned runtime intake for canonical coding-request bytes.

The external caller supplies only the existing canonical integration request
record. Worker selection, local state roots, internal resource ceilings,
session identity, and run identity remain Auto Lab-local runtime concerns.

This module does not create host root directories, discover configuration,
promote candidates, approve work, stage Git state, commit, push, retry failed
work, or provide a network listener.

Configured candidate-store and lifecycle-journal roots must already exist.
Their existing hardened initializers validate them and may initialize only
their fixed internal layouts when a request is actually run.
"""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass

from .lab_codex_worker import (
    CodexWorker,
    CodexWorkerConfig,
    _validate_physical_context as _validate_codex_worker_physical_context,
    validate_codex_worker_config,
)
from .lab_coding_candidate_store import (
    initialize_lab_coding_candidate_store,
)
from .lab_coding_integration import (
    LabCodingIntegrationDeployment,
    run_lab_coding_integration_request_with_lifecycle,
    validate_lab_coding_integration_deployment,
)
from .lab_coding_integration_contract import (
    lab_coding_integration_request_from_bytes,
    lab_coding_integration_result_to_bytes,
)
from .lab_coding_job_lifecycle_journal import (
    initialize_lab_coding_job_lifecycle_journal_root,
)


_ID_BYTES = 32
_PROVISIONAL_SESSION_ID = "0" * 64


class LabCodingRuntimeIntakeError(RuntimeError):
    """The private Auto Lab coding runtime configuration is unsafe."""


@dataclass(frozen=True, slots=True)
class LabCodingRuntimeConfig:
    """Auto Lab-local settings that are never part of request wire data."""

    codex_worker_config: CodexWorkerConfig
    workspace_parent: str
    candidate_store_root: str
    lifecycle_journal_root: str
    max_seed_files: int
    max_seed_bytes: int


def _canonical_absolute_path(
    value: object,
    *,
    name: str,
) -> str:
    if type(value) is not str:
        raise LabCodingRuntimeIntakeError(
            f"{name} must be a string"
        )

    if not value or "\x00" in value:
        raise LabCodingRuntimeIntakeError(
            f"{name} must be a non-empty filesystem path"
        )

    if not os.path.isabs(value):
        raise LabCodingRuntimeIntakeError(
            f"{name} must be absolute"
        )

    if os.path.normpath(value) != value:
        raise LabCodingRuntimeIntakeError(
            f"{name} must be normalized"
        )

    if os.path.realpath(value) != value:
        raise LabCodingRuntimeIntakeError(
            f"{name} must be canonical and contain no symlink aliases"
        )

    if value == os.path.sep:
        raise LabCodingRuntimeIntakeError(
            f"{name} must not be the filesystem root"
        )

    return value


def _positive_integer(
    value: object,
    *,
    name: str,
) -> int:
    if type(value) is not int or value <= 0:
        raise LabCodingRuntimeIntakeError(
            f"{name} must be a positive exact integer"
        )

    return value


def _validate_private_runtime_directory(
    value: str,
    *,
    name: str,
) -> None:
    try:
        info = os.stat(
            value,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LabCodingRuntimeIntakeError(
            f"cannot inspect {name}: {exc}"
        ) from exc

    if not stat.S_ISDIR(
        info.st_mode
    ):
        raise LabCodingRuntimeIntakeError(
            f"{name} must be a directory"
        )

    if info.st_uid != os.geteuid():
        raise LabCodingRuntimeIntakeError(
            f"{name} must be owned by the current user"
        )

    if stat.S_IMODE(
        info.st_mode
    ) != 0o700:
        raise LabCodingRuntimeIntakeError(
            f"{name} must have mode 0700"
        )


def _require_nonoverlapping_roots(
    roots: tuple[tuple[str, str], ...],
) -> None:
    for index, (first_name, first_path) in enumerate(roots):
        for second_name, second_path in roots[index + 1:]:
            try:
                common = os.path.commonpath(
                    (
                        first_path,
                        second_path,
                    )
                )
            except ValueError as exc:
                raise LabCodingRuntimeIntakeError(
                    "runtime security roots cannot be compared safely"
                ) from exc

            if common in {
                first_path,
                second_path,
            }:
                raise LabCodingRuntimeIntakeError(
                    "runtime security roots must not overlap: "
                    f"{first_name} and {second_name}"
                )


def _require_safe_codex_root_relationship(
    *,
    codex_home: str,
    codex_release_root: str,
) -> None:
    try:
        common = os.path.commonpath(
            (
                codex_home,
                codex_release_root,
            )
        )
    except ValueError as exc:
        raise LabCodingRuntimeIntakeError(
            "trusted Codex roots cannot be compared safely"
        ) from exc

    if codex_home == codex_release_root:
        raise LabCodingRuntimeIntakeError(
            "codex_home and codex_release_root must be distinct"
        )

    if common == codex_release_root:
        raise LabCodingRuntimeIntakeError(
            "codex_home must not be inside codex_release_root"
        )


def validate_lab_coding_runtime_config(
    config: object,
) -> LabCodingRuntimeConfig:
    if type(config) is not LabCodingRuntimeConfig:
        raise LabCodingRuntimeIntakeError(
            "config must have the exact LabCodingRuntimeConfig type"
        )

    trusted_worker_config = validate_codex_worker_config(
        config.codex_worker_config
    )

    workspace_parent = _canonical_absolute_path(
        config.workspace_parent,
        name="workspace_parent",
    )

    candidate_store_root = _canonical_absolute_path(
        config.candidate_store_root,
        name="candidate_store_root",
    )

    lifecycle_journal_root = _canonical_absolute_path(
        config.lifecycle_journal_root,
        name="lifecycle_journal_root",
    )

    max_seed_files = _positive_integer(
        config.max_seed_files,
        name="max_seed_files",
    )

    max_seed_bytes = _positive_integer(
        config.max_seed_bytes,
        name="max_seed_bytes",
    )

    mutable_roots = (
        (
            "workspace_parent",
            workspace_parent,
        ),
        (
            "candidate_store_root",
            candidate_store_root,
        ),
        (
            "lifecycle_journal_root",
            lifecycle_journal_root,
        ),
        (
            "runtime_temp",
            trusted_worker_config.runtime_temp,
        ),
    )

    _require_nonoverlapping_roots(
        mutable_roots
        + (
            (
                "codex_home",
                trusted_worker_config.codex_home,
            ),
        )
    )

    _require_nonoverlapping_roots(
        mutable_roots
        + (
            (
                "codex_release_root",
                trusted_worker_config.codex_release_root,
            ),
        )
    )

    _require_safe_codex_root_relationship(
        codex_home=trusted_worker_config.codex_home,
        codex_release_root=(
            trusted_worker_config.codex_release_root
        ),
    )

    expected = LabCodingRuntimeConfig(
        codex_worker_config=trusted_worker_config,
        workspace_parent=workspace_parent,
        candidate_store_root=candidate_store_root,
        lifecycle_journal_root=lifecycle_journal_root,
        max_seed_files=max_seed_files,
        max_seed_bytes=max_seed_bytes,
    )

    if config != expected:
        raise LabCodingRuntimeIntakeError(
            "runtime config is not canonical"
        )

    return config


class LabCodingRuntime:
    """One configured Auto Lab-local runtime behind the wire boundary."""

    __slots__ = (
        "_config",
        "_worker",
    )

    def __init__(
        self,
        config: LabCodingRuntimeConfig,
    ) -> None:
        self._config = validate_lab_coding_runtime_config(
            config
        )

        self._worker = CodexWorker(
            self._config.codex_worker_config
        )

        prototype = LabCodingIntegrationDeployment(
            worker=self._worker,
            workspace_parent=self._config.workspace_parent,
            candidate_store_root=(
                self._config.candidate_store_root
            ),
            session_id=_PROVISIONAL_SESSION_ID,
            max_seed_files=self._config.max_seed_files,
            max_seed_bytes=self._config.max_seed_bytes,
            max_runtime_seconds=(
                self._config
                .codex_worker_config
                .max_runtime_seconds
            ),
            max_log_bytes=(
                self._config
                .codex_worker_config
                .max_output_bytes
            ),
        )

        validate_lab_coding_integration_deployment(
            prototype
        )

    def _deployment_for_session(
        self,
        session_id: str,
    ) -> LabCodingIntegrationDeployment:
        deployment = LabCodingIntegrationDeployment(
            worker=self._worker,
            workspace_parent=self._config.workspace_parent,
            candidate_store_root=(
                self._config.candidate_store_root
            ),
            session_id=session_id,
            max_seed_files=self._config.max_seed_files,
            max_seed_bytes=self._config.max_seed_bytes,
            max_runtime_seconds=(
                self._config
                .codex_worker_config
                .max_runtime_seconds
            ),
            max_log_bytes=(
                self._config
                .codex_worker_config
                .max_output_bytes
            ),
        )

        return validate_lab_coding_integration_deployment(
            deployment
        )

    def run_request_bytes(
        self,
        record_bytes: object,
    ) -> bytes:
        """Run one canonical request through existing lifecycle execution.

        Malformed wire data is rejected before any local store is touched.

        The configured store roots must already exist. Their hardened
        initializers validate or initialize only their fixed internal layouts
        before fresh Auto Lab-local session and run identities are generated.

        Failure is propagated unchanged. There is no retry, automatic cleanup,
        promotion, staging, commit, or push behavior here.
        """

        request = lab_coding_integration_request_from_bytes(
            record_bytes
        )

        _validate_codex_worker_physical_context(
            self._config.codex_worker_config
        )

        for name, path in (
            (
                "workspace_parent",
                self._config.workspace_parent,
            ),
            (
                "candidate_store_root",
                self._config.candidate_store_root,
            ),
            (
                "lifecycle_journal_root",
                self._config.lifecycle_journal_root,
            ),
        ):
            _validate_private_runtime_directory(
                path,
                name=name,
            )

        initialize_lab_coding_candidate_store(
            self._config.candidate_store_root
        )

        initialize_lab_coding_job_lifecycle_journal_root(
            self._config.lifecycle_journal_root
        )

        session_id = secrets.token_hex(
            _ID_BYTES
        )

        run_id = secrets.token_hex(
            _ID_BYTES
        )

        deployment = self._deployment_for_session(
            session_id
        )

        result = (
            run_lab_coding_integration_request_with_lifecycle(
                request=request,
                deployment=deployment,
                run_id=run_id,
                lifecycle_journal_root=(
                    self._config.lifecycle_journal_root
                ),
            )
        )

        return lab_coding_integration_result_to_bytes(
            result
        )
