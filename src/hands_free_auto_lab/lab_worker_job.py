from dataclasses import dataclass

from .lab_worker import (
    STATUS_COMPLETED,
    AutoLabWorker,
    LabWorkerRequest,
    LabWorkerResult,
    build_lab_worker_request,
    validate_lab_worker_result,
)

from .lab_worker_change_policy import (
    LabWorkerChangePolicy,
    validate_lab_worker_change_policy,
    validate_lab_worker_change_policy_before,
    validate_lab_worker_change_policy_diff,
)

from .lab_workspace import LabWorkspace

from .lab_workspace_snapshot import (
    LabWorkspaceDiff,
    LabWorkspaceSnapshot,
    capture_lab_workspace_snapshot,
    diff_lab_workspace_snapshots,
)


WORKER_JOB_COMPONENT = "hands-free-auto-lab-worker-job-v1"
WORKER_JOB_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LabWorkerJobRecord:
    component: str
    schema_version: int
    request: LabWorkerRequest
    result: LabWorkerResult
    before_snapshot: LabWorkspaceSnapshot
    after_snapshot: LabWorkspaceSnapshot
    physical_diff: LabWorkspaceDiff
    change_policy: LabWorkerChangePolicy | None = None

    @property
    def succeeded(self) -> bool:
        return self.result.status == STATUS_COMPLETED


def run_lab_worker_job(
    *,
    goal: object,
    worker_name: object,
    worker: AutoLabWorker,
    workspace_parent: str,
    workspace: LabWorkspace,
    capabilities: tuple[str, ...],
    max_runtime_seconds: int,
    max_log_bytes: int,
    change_policy: LabWorkerChangePolicy | None = None,
) -> LabWorkerJobRecord:
    """
    Execute one worker exactly once and derive authoritative physical evidence.

    When a change policy is supplied:

    - its identity is validated before request construction;
    - policy_id is cryptographically bound into the worker request;
    - its before-state requirements are validated before worker execution;
    - physical after-state is captured before worker narrative is trusted;
    - every observed physical change must remain inside policy;
    - COMPLETED requires the physical diff to satisfy every policy target.

    FAILED or TIMED_OUT results may preserve a safe in-policy subset.

    Worker-reported changed paths remain evidence only.
    """
    trusted_policy = (
        None
        if change_policy is None
        else validate_lab_worker_change_policy(
            change_policy
        )
    )

    request = build_lab_worker_request(
        worker=worker_name,
        goal=goal,
        workspace=workspace,
        change_policy_id=(
            None
            if trusted_policy is None
            else trusted_policy.policy_id
        ),
        capabilities=capabilities,
        max_runtime_seconds=max_runtime_seconds,
        max_log_bytes=max_log_bytes,
    )

    before_snapshot = capture_lab_workspace_snapshot(
        workspace_parent=workspace_parent,
        workspace=workspace,
    )

    if trusted_policy is not None:
        validate_lab_worker_change_policy_before(
            trusted_policy,
            before_snapshot=before_snapshot,
        )

    raw_result = worker.run(
        request
    )

    if trusted_policy is None:
        # Preserve the original generic worker-job behavior exactly.
        result = validate_lab_worker_result(
            raw_result,
            request=request,
        )

        after_snapshot = capture_lab_workspace_snapshot(
            workspace_parent=workspace_parent,
            workspace=workspace,
        )

        physical_diff = diff_lab_workspace_snapshots(
            before_snapshot,
            after_snapshot,
        )

    else:
        # Physical truth is authoritative and is captured before trusting
        # worker-reported result metadata.
        after_snapshot = capture_lab_workspace_snapshot(
            workspace_parent=workspace_parent,
            workspace=workspace,
        )

        physical_diff = diff_lab_workspace_snapshots(
            before_snapshot,
            after_snapshot,
        )

        # Any unsafe physical mutation fails even if the worker result itself
        # is malformed, failed, or timed out.
        validate_lab_worker_change_policy_diff(
            trusted_policy,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            physical_diff=physical_diff,
            require_complete=False,
        )

        result = validate_lab_worker_result(
            raw_result,
            request=request,
        )

        if result.status == STATUS_COMPLETED:
            validate_lab_worker_change_policy_diff(
                trusted_policy,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                physical_diff=physical_diff,
                require_complete=True,
            )

    return LabWorkerJobRecord(
        component=WORKER_JOB_COMPONENT,
        schema_version=WORKER_JOB_SCHEMA_VERSION,
        request=request,
        result=result,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        physical_diff=physical_diff,
        change_policy=trusted_policy,
    )
