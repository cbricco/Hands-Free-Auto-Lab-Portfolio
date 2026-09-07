"""Compose one committed-source coding job into durable candidate evidence.

This module is orchestration only. It does not grant approval, promotion,
repository mutation, Git, networking, retry, or model authority beyond the
already-bounded worker job.

The caller must provide an already-initialized private candidate store.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lab_coding_candidate import (
    LabCodingCandidate,
    build_lab_coding_candidate,
)
from .lab_coding_candidate_store import (
    persist_lab_coding_candidate,
)
from .lab_coding_job import (
    LabCodingJobRecord,
    run_codex_coding_job_from_commit,
)
from .lab_codex_worker import CodexWorker
from .lab_worker_change_policy import LabWorkerChangePolicy


@dataclass(frozen=True, slots=True)
class LabCodingWorkflowResult:
    """Composition result; authority remains in its validated component records."""

    coding_job: LabCodingJobRecord
    candidate: LabCodingCandidate
    candidate_record_path: str


def finalize_lab_coding_workflow(
    *,
    coding_job: LabCodingJobRecord,
    workspace_parent: str,
    candidate_store_root: str,
) -> LabCodingWorkflowResult:
    """
    Convert one already-returned coding job into durable candidate evidence.

    This function does not execute a worker. It only reuses the existing
    independent candidate validator and immutable candidate store.

    Unsuccessful, malformed, or physically invalid coding jobs continue to
    fail closed through the existing candidate validation path.
    """
    candidate = build_lab_coding_candidate(
        coding_job,
        workspace_parent=workspace_parent,
    )

    candidate_record_path = persist_lab_coding_candidate(
        candidate_store_root,
        candidate,
    )

    return LabCodingWorkflowResult(
        coding_job=coding_job,
        candidate=candidate,
        candidate_record_path=str(candidate_record_path),
    )


def run_codex_coding_workflow_from_commit(
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
    candidate_store_root: str,
    max_seed_files: int,
    max_seed_bytes: int,
    max_runtime_seconds: int,
    max_log_bytes: int,
) -> LabCodingWorkflowResult:
    """Run, independently validate, and durably persist one coding candidate.

    Execution is exactly one normal committed-source coding job. The resulting
    workspace is converted into a candidate only through the existing physical
    evidence validator. Only that validated candidate is passed to the existing
    immutable candidate store.

    A failure at any stage propagates immediately. This function does not retry,
    initialize stores, build promotion proposals, authorize promotion, apply
    repository changes, stage, commit, or push.
    """
    coding_job = run_codex_coding_job_from_commit(
        repository_path=repository_path,
        commit_oid=commit_oid,
        expected_branch=expected_branch,
        relative_paths=relative_paths,
        workspace_parent=workspace_parent,
        session_id=session_id,
        goal=goal,
        worker=worker,
        change_policy=change_policy,
        max_seed_files=max_seed_files,
        max_seed_bytes=max_seed_bytes,
        max_runtime_seconds=max_runtime_seconds,
        max_log_bytes=max_log_bytes,
    )

    return finalize_lab_coding_workflow(
        coding_job=coding_job,
        workspace_parent=workspace_parent,
        candidate_store_root=candidate_store_root,
    )
