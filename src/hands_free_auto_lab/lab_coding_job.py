from dataclasses import dataclass

from .lab_committed_workspace_seed import (
    LabCommittedWorkspaceSeedRecord,
    seed_lab_workspace_from_commit,
)
from .lab_codex_worker import (
    CODEX_WORKER_NAME,
    CodexWorker,
)
from .lab_worker import (
    CAPABILITY_RUN_PROCESS,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
)
from .lab_worker_job import (
    LabWorkerJobRecord,
    run_lab_worker_job,
)
from .lab_worker_change_policy import (
    LabWorkerChangePolicy,
    validate_lab_worker_change_policy,
    OPERATION_ADD,
)
from .lab_workspace import (
    LabWorkspace,
    create_lab_workspace,
)
from .lab_workspace_seed import (
    LabWorkspaceSeedRecord,
    seed_lab_workspace,
)


CODING_JOB_COMPONENT = "hands-free-auto-lab-coding-job-v1"
CODING_JOB_SCHEMA_VERSION = 1

_CODEX_CAPABILITIES = tuple(
    sorted(
        (
            CAPABILITY_RUN_PROCESS,
            CAPABILITY_WORKSPACE_READ,
            CAPABILITY_WORKSPACE_WRITE,
        )
    )
)


@dataclass(frozen=True, slots=True)
class LabCodingJobRecord:
    component: str
    schema_version: int
    workspace: LabWorkspace
    seed: (
        LabWorkspaceSeedRecord
        | LabCommittedWorkspaceSeedRecord
    )
    worker_job: LabWorkerJobRecord

    @property
    def succeeded(self) -> bool:
        return self.worker_job.succeeded


def run_codex_coding_job(
    *,
    source_root: str,
    relative_paths: tuple[str, ...],
    workspace_parent: str,
    session_id: str,
    goal: str,
    worker: CodexWorker,
    change_policy: LabWorkerChangePolicy,
    max_seed_files: int,
    max_seed_bytes: int,
    max_runtime_seconds: int,
    max_log_bytes: int,
) -> LabCodingJobRecord:
    """
    Run one bounded Codex job in a new disposable workspace.

    The caller supplies an already-selected source root and explicit file
    manifest. This function does not discover projects, mutate the source
    project, invoke Git, authorize promotion, choose arbitrary host paths,
    retry failed work, or grant authority beyond the disposable lab.
    """
    trusted_policy = validate_lab_worker_change_policy(
        change_policy
    )

    workspace = create_lab_workspace(
        workspace_parent,
        session_id=session_id,
    )

    seed = seed_lab_workspace(
        source_root=source_root,
        relative_paths=relative_paths,
        workspace_parent=workspace_parent,
        workspace=workspace,
        max_files=max_seed_files,
        max_total_bytes=max_seed_bytes,
    )

    worker_job = run_lab_worker_job(
        goal=goal,
        worker_name=CODEX_WORKER_NAME,
        worker=worker,
        workspace_parent=workspace_parent,
        workspace=workspace,
        capabilities=_CODEX_CAPABILITIES,
        max_runtime_seconds=max_runtime_seconds,
        max_log_bytes=max_log_bytes,
        change_policy=trusted_policy,
    )

    return LabCodingJobRecord(
        component=CODING_JOB_COMPONENT,
        schema_version=CODING_JOB_SCHEMA_VERSION,
        workspace=workspace,
        seed=seed,
        worker_job=worker_job,
    )

def run_codex_coding_job_from_commit(
    *,
    repository_path: str,
    commit_oid: str,
    expected_branch: str | None,
    relative_paths: tuple[str, ...],
    workspace_parent: str,
    session_id: str,
    goal: str,
    worker: CodexWorker,
    change_policy: LabWorkerChangePolicy,
    max_seed_files: int,
    max_seed_bytes: int,
    max_runtime_seconds: int,
    max_log_bytes: int,
) -> LabCodingJobRecord:
    """
    Run one bounded Codex job from exact verified committed source state.

    The committed-state seeder performs fixed read-only Git inspection and
    materializes only the caller-selected committed files into the disposable
    workspace. The source repository is not used as the worker workspace and
    is not mutated by this function.

    This function does not authorize promotion, retry failed work, stage,
    commit, push, or grant authority outside the disposable lab.
    """
    trusted_policy = validate_lab_worker_change_policy(
        change_policy
    )

    expected_absent_paths = tuple(
        target.path
        for target in trusted_policy.targets
        if target.operation == OPERATION_ADD
    )

    worker_for_job = (
        worker.bind_change_policy(
            trusted_policy
        )
        if isinstance(
            worker,
            CodexWorker,
        )
        else worker
    )

    workspace = create_lab_workspace(
        workspace_parent,
        session_id=session_id,
    )

    seed = seed_lab_workspace_from_commit(
        repository_path=repository_path,
        commit_oid=commit_oid,
        expected_branch=expected_branch,
        relative_paths=relative_paths,
        expected_absent_paths=expected_absent_paths,
        workspace_parent=workspace_parent,
        workspace=workspace,
        max_files=max_seed_files,
        max_total_bytes=max_seed_bytes,
    )

    worker_job = run_lab_worker_job(
        goal=goal,
        worker_name=CODEX_WORKER_NAME,
        worker=worker_for_job,
        workspace_parent=workspace_parent,
        workspace=workspace,
        capabilities=_CODEX_CAPABILITIES,
        max_runtime_seconds=max_runtime_seconds,
        max_log_bytes=max_log_bytes,
        change_policy=trusted_policy,
    )

    return LabCodingJobRecord(
        component=CODING_JOB_COMPONENT,
        schema_version=CODING_JOB_SCHEMA_VERSION,
        workspace=workspace,
        seed=seed,
        worker_job=worker_job,
    )
