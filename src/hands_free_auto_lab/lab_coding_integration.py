"""Execute one validated external coding request through local Auto Lab policy.

The versioned request supplies task data, never deployment authority.

Auto Lab-local deployment configuration supplies the worker, workspace,
candidate store, session identity, and local resource ceilings.

This module does not initialize stores, retry work, construct promotion
proposals, approve promotion, mutate a source repository, stage, commit,
push, or grant networking/system authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

from .lab_coding_integration_contract import (
    LabCodingIntegrationRequest,
    LabCodingIntegrationResult,
    build_lab_coding_integration_result,
    validate_lab_coding_integration_request,
)
from .lab_coding_job import (
    run_codex_coding_job_from_commit,
)
from .lab_coding_job_lifecycle import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_TIMED_OUT,
    build_lab_coding_job_lifecycle,
    transition_lab_coding_job_lifecycle,
)
from .lab_coding_job_lifecycle_journal import (
    append_lab_coding_job_lifecycle_snapshot,
    create_lab_coding_job_lifecycle_journal,
)
from .lab_coding_workflow import (
    finalize_lab_coding_workflow,
    run_codex_coding_workflow_from_commit,
)
from .lab_codex_worker import CodexWorker
from .lab_worker import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
)


_HEX_LOWER = frozenset("0123456789abcdef")


class LabCodingIntegrationError(ValueError):
    """Local deployment data for integration execution failed closed."""


@dataclass(frozen=True, slots=True)
class LabCodingIntegrationDeployment:
    """Auto Lab-local settings that are deliberately absent from the wire request."""

    worker: CodexWorker
    workspace_parent: str
    candidate_store_root: str
    session_id: str
    max_seed_files: int
    max_seed_bytes: int
    max_runtime_seconds: int
    max_log_bytes: int


def _canonical_absolute_path(
    value: object,
    *,
    name: str,
) -> str:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
    ):
        raise LabCodingIntegrationError(
            f"{name} must be a non-empty string without NUL"
        )

    try:
        value.encode(
            "utf-8",
            errors="strict",
        )
    except UnicodeEncodeError as exc:
        raise LabCodingIntegrationError(
            f"{name} must be UTF-8 encodable"
        ) from exc

    if (
        not os.path.isabs(value)
        or os.path.normpath(value) != value
        or os.path.realpath(value) != value
    ):
        raise LabCodingIntegrationError(
            f"{name} must be a canonical absolute path without aliases"
        )

    return value


def _positive_integer(
    value: object,
    *,
    name: str,
) -> int:
    if (
        type(value) is not int
        or value <= 0
    ):
        raise LabCodingIntegrationError(
            f"{name} must be an exact positive integer"
        )

    return value


def _session_identifier(
    value: object,
) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabCodingIntegrationError(
            "session_id must be a lowercase 64-character hex identifier"
        )

    return value


def validate_lab_coding_integration_deployment(
    deployment: object,
) -> LabCodingIntegrationDeployment:
    if type(deployment) is not LabCodingIntegrationDeployment:
        raise LabCodingIntegrationError(
            "deployment must have the exact LabCodingIntegrationDeployment type"
        )

    if not callable(
        getattr(
            deployment.worker,
            "run",
            None,
        )
    ):
        raise LabCodingIntegrationError(
            "deployment worker must provide a callable run method"
        )

    _canonical_absolute_path(
        deployment.workspace_parent,
        name="workspace_parent",
    )

    _canonical_absolute_path(
        deployment.candidate_store_root,
        name="candidate_store_root",
    )

    _session_identifier(
        deployment.session_id
    )

    _positive_integer(
        deployment.max_seed_files,
        name="max_seed_files",
    )
    _positive_integer(
        deployment.max_seed_bytes,
        name="max_seed_bytes",
    )
    _positive_integer(
        deployment.max_runtime_seconds,
        name="max_runtime_seconds",
    )
    _positive_integer(
        deployment.max_log_bytes,
        name="max_log_bytes",
    )

    return deployment


def run_lab_coding_integration_request(
    *,
    request: object,
    deployment: object,
) -> LabCodingIntegrationResult:
    """Execute one request exactly once and return only versioned result evidence."""
    trusted_request = validate_lab_coding_integration_request(
        request
    )
    trusted_deployment = validate_lab_coding_integration_deployment(
        deployment
    )

    workflow = run_codex_coding_workflow_from_commit(
        repository_path=trusted_request.repository_path,
        commit_oid=trusted_request.commit_oid,
        expected_branch=trusted_request.expected_branch,
        relative_paths=trusted_request.relative_paths,
        workspace_parent=trusted_deployment.workspace_parent,
        session_id=trusted_deployment.session_id,
        goal=trusted_request.goal,
        worker=trusted_deployment.worker,
        change_policy=trusted_request.change_policy,
        candidate_store_root=trusted_deployment.candidate_store_root,
        max_seed_files=trusted_deployment.max_seed_files,
        max_seed_bytes=trusted_deployment.max_seed_bytes,
        max_runtime_seconds=min(
            trusted_request.max_runtime_seconds,
            trusted_deployment.max_runtime_seconds,
        ),
        max_log_bytes=trusted_deployment.max_log_bytes,
    )

    return build_lab_coding_integration_result(
        request=trusted_request,
        candidate_id=workflow.candidate.candidate_id,
    )


def run_lab_coding_integration_request_with_lifecycle(
    *,
    request: object,
    deployment: object,
    run_id: object,
    lifecycle_journal_root: str | os.PathLike[str],
) -> LabCodingIntegrationResult:
    """
    Execute one validated request while durably recording its lifecycle.

    run_id and lifecycle_journal_root are Auto Lab-local runtime inputs.
    They are deliberately absent from the external V1 request contract.

    Lifecycle state is descriptive evidence only. It grants no retry,
    resume, promotion, staging, commit, push, or other authority.

    A failure to durably create REQUESTED or RUNNING state prevents worker
    execution. No failure path retries work or invents missing evidence.
    """
    trusted_request = validate_lab_coding_integration_request(
        request
    )
    trusted_deployment = validate_lab_coding_integration_deployment(
        deployment
    )

    requested = build_lab_coding_job_lifecycle(
        request_id=trusted_request.request_id,
        run_id=run_id,
    )

    create_lab_coding_job_lifecycle_journal(
        lifecycle_journal_root,
        lifecycle=requested,
    )

    running = transition_lab_coding_job_lifecycle(
        requested,
        state=STATE_RUNNING,
        session_id=trusted_deployment.session_id,
    )

    running = append_lab_coding_job_lifecycle_snapshot(
        lifecycle_journal_root,
        successor=running,
        expected_previous_snapshot_id=requested.snapshot_id,
    )

    try:
        coding_job = run_codex_coding_job_from_commit(
            repository_path=trusted_request.repository_path,
            commit_oid=trusted_request.commit_oid,
            expected_branch=trusted_request.expected_branch,
            relative_paths=trusted_request.relative_paths,
            workspace_parent=trusted_deployment.workspace_parent,
            session_id=trusted_deployment.session_id,
            goal=trusted_request.goal,
            worker=trusted_deployment.worker,
            change_policy=trusted_request.change_policy,
            max_seed_files=trusted_deployment.max_seed_files,
            max_seed_bytes=trusted_deployment.max_seed_bytes,
            max_runtime_seconds=min(
                trusted_request.max_runtime_seconds,
                trusted_deployment.max_runtime_seconds,
            ),
            max_log_bytes=trusted_deployment.max_log_bytes,
        )
    except Exception:
        failed = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_FAILED,
        )

        append_lab_coding_job_lifecycle_snapshot(
            lifecycle_journal_root,
            successor=failed,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        raise

    try:
        worker_request_id = (
            coding_job.worker_job.request.request_id
        )
        worker_result_id = (
            coding_job.worker_job.result.result_id
        )
        worker_status = (
            coding_job.worker_job.result.status
        )
    except Exception:
        failed = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_FAILED,
        )

        append_lab_coding_job_lifecycle_snapshot(
            lifecycle_journal_root,
            successor=failed,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        raise

    if worker_status == STATUS_FAILED:
        failed = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_FAILED,
            worker_request_id=worker_request_id,
            worker_result_id=worker_result_id,
        )

        append_lab_coding_job_lifecycle_snapshot(
            lifecycle_journal_root,
            successor=failed,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        raise LabCodingIntegrationError(
            "coding worker returned FAILED"
        )

    if worker_status == STATUS_TIMED_OUT:
        timed_out = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_TIMED_OUT,
            worker_request_id=worker_request_id,
            worker_result_id=worker_result_id,
        )

        append_lab_coding_job_lifecycle_snapshot(
            lifecycle_journal_root,
            successor=timed_out,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        raise LabCodingIntegrationError(
            "coding worker returned TIMED_OUT"
        )

    if worker_status != STATUS_COMPLETED:
        failed = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_FAILED,
            worker_request_id=worker_request_id,
            worker_result_id=worker_result_id,
        )

        append_lab_coding_job_lifecycle_snapshot(
            lifecycle_journal_root,
            successor=failed,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        raise LabCodingIntegrationError(
            "coding worker returned unsupported terminal status "
            f"{worker_status!r}"
        )

    try:
        workflow = finalize_lab_coding_workflow(
            coding_job=coding_job,
            workspace_parent=trusted_deployment.workspace_parent,
            candidate_store_root=(
                trusted_deployment.candidate_store_root
            ),
        )

        result = build_lab_coding_integration_result(
            request=trusted_request,
            candidate_id=workflow.candidate.candidate_id,
        )

        candidate_id = workflow.candidate.candidate_id
        integration_result_id = result.result_id

    except Exception:
        failed = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_FAILED,
            worker_request_id=worker_request_id,
            worker_result_id=worker_result_id,
        )

        append_lab_coding_job_lifecycle_snapshot(
            lifecycle_journal_root,
            successor=failed,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        raise

    completed = transition_lab_coding_job_lifecycle(
        running,
        state=STATE_COMPLETED,
        worker_request_id=worker_request_id,
        worker_result_id=worker_result_id,
        candidate_id=candidate_id,
        integration_result_id=integration_result_id,
    )

    append_lab_coding_job_lifecycle_snapshot(
        lifecycle_journal_root,
        successor=completed,
        expected_previous_snapshot_id=running.snapshot_id,
    )

    return result
