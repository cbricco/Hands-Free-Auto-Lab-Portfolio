from __future__ import annotations

from .lab_coding_integration import (
    LabCodingIntegrationDeployment,
    run_lab_coding_integration_request_with_lifecycle,
    validate_lab_coding_integration_deployment,
)
from .lab_coding_integration_contract import (
    LabCodingIntegrationResult,
)
from .lab_coding_job_queue import (
    LabCodingJobQueue,
    validate_lab_coding_job_queue,
)


class LabCodingJobDispatcherError(RuntimeError):
    """Raised when a coding-job queue cannot be dispatched safely."""


def _validate_dispatch_deployments(
    *,
    queue: LabCodingJobQueue,
    deployments: object,
) -> tuple[LabCodingIntegrationDeployment, ...]:
    if type(deployments) is not tuple:
        raise LabCodingJobDispatcherError(
            "deployments must be an exact tuple"
        )

    if len(deployments) != len(queue.items):
        raise LabCodingJobDispatcherError(
            "deployment count must exactly match queue item count"
        )

    trusted_deployments = tuple(
        validate_lab_coding_integration_deployment(deployment)
        for deployment in deployments
    )

    session_ids = tuple(
        deployment.session_id
        for deployment in trusted_deployments
    )

    if len(session_ids) != len(set(session_ids)):
        raise LabCodingJobDispatcherError(
            "deployment session_id values must be unique"
        )

    return trusted_deployments


def run_lab_coding_job_queue(
    *,
    queue: object,
    deployments: object,
    lifecycle_journal_root: object,
) -> tuple[LabCodingIntegrationResult, ...]:
    """
    Dispatch one already-bounded coding-job queue sequentially.

    The complete queue and complete deployment tuple are validated before
    the first execution starts. Each queue item is paired positionally with
    exactly one preconstructed deployment and is executed through the
    existing lifecycle-bound one-job seam.

    The first execution exception stops dispatch immediately and propagates
    unchanged. This function grants no retry, resume, cleanup, promotion,
    staging, commit, push, or deployment/session construction authority.
    """
    trusted_queue = validate_lab_coding_job_queue(
        queue
    )

    trusted_deployments = _validate_dispatch_deployments(
        queue=trusted_queue,
        deployments=deployments,
    )

    results: list[LabCodingIntegrationResult] = []

    for item, deployment in zip(
        trusted_queue.items,
        trusted_deployments,
    ):
        result = run_lab_coding_integration_request_with_lifecycle(
            request=item.request,
            deployment=deployment,
            run_id=item.run_id,
            lifecycle_journal_root=lifecycle_journal_root,
        )

        results.append(
            result
        )

    return tuple(
        results
    )
