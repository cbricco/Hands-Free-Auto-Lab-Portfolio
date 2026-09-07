"""Pure durable-status model for one externally requested coding job.

This module defines immutable descriptive lifecycle evidence only.

It has no filesystem, worker, execution, approval, promotion, repository,
Git, retry, resume, queue, networking, or credential authority.

A lifecycle is bound to one exact external request_id and one exact run_id.
The derived job_id identifies that requested execution attempt. Immutable
snapshot_id values identify exact lifecycle snapshots.

Legal v1 transitions:

    REQUESTED -> RUNNING
    REQUESTED -> FAILED
    RUNNING   -> COMPLETED
    RUNNING   -> FAILED
    RUNNING   -> TIMED_OUT

Terminal states have no successors. There is deliberately no RETRY or
RESUME transition. A later execution is a new lifecycle with a new run_id.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


CODING_JOB_LIFECYCLE_COMPONENT = (
    "hands-free-auto-lab-coding-job-lifecycle-v1"
)
CODING_JOB_LIFECYCLE_SCHEMA_VERSION = 1

STATE_REQUESTED = "REQUESTED"
STATE_RUNNING = "RUNNING"
STATE_COMPLETED = "COMPLETED"
STATE_FAILED = "FAILED"
STATE_TIMED_OUT = "TIMED_OUT"

CODING_JOB_LIFECYCLE_STATES = frozenset(
    {
        STATE_REQUESTED,
        STATE_RUNNING,
        STATE_COMPLETED,
        STATE_FAILED,
        STATE_TIMED_OUT,
    }
)

TERMINAL_CODING_JOB_LIFECYCLE_STATES = frozenset(
    {
        STATE_COMPLETED,
        STATE_FAILED,
        STATE_TIMED_OUT,
    }
)

_ALLOWED_STATE_TRANSITIONS = {
    STATE_REQUESTED: frozenset(
        {
            STATE_RUNNING,
            STATE_FAILED,
        }
    ),
    STATE_RUNNING: frozenset(
        {
            STATE_COMPLETED,
            STATE_FAILED,
            STATE_TIMED_OUT,
        }
    ),
    STATE_COMPLETED: frozenset(),
    STATE_FAILED: frozenset(),
    STATE_TIMED_OUT: frozenset(),
}

_JOB_ID_DOMAIN = (
    b"hands-free-auto-lab-coding-job-lifecycle-job-id-v1\x00"
)

_SNAPSHOT_ID_DOMAIN = (
    b"hands-free-auto-lab-coding-job-lifecycle-snapshot-id-v1\x00"
)

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)


class LabCodingJobLifecycleError(
    ValueError
):
    """Lifecycle evidence or a requested transition is invalid."""


@dataclass(
    frozen=True,
    slots=True,
)
class LabCodingJobLifecycle:
    component: str
    schema_version: int
    job_id: str
    snapshot_id: str
    request_id: str
    run_id: str
    state: str
    session_id: str | None
    worker_request_id: str | None
    worker_result_id: str | None
    candidate_id: str | None
    integration_result_id: str | None


def _canonical_json_bytes(
    value: object,
) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(
            ",",
            ":",
        ),
        ensure_ascii=False,
    ).encode(
        "utf-8",
        errors="strict",
    )


def _require_identifier(
    name: str,
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
        raise LabCodingJobLifecycleError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _require_optional_identifier(
    name: str,
    value: object,
) -> str | None:
    if value is None:
        return None

    return _require_identifier(
        name,
        value,
    )


def _job_id(
    *,
    request_id: str,
    run_id: str,
) -> str:
    return hashlib.sha256(
        _JOB_ID_DOMAIN
        + _canonical_json_bytes(
            {
                "request_id": request_id,
                "run_id": run_id,
            }
        )
    ).hexdigest()


def _snapshot_identity(
    *,
    job_id: str,
    request_id: str,
    run_id: str,
    state: str,
    session_id: str | None,
    worker_request_id: str | None,
    worker_result_id: str | None,
    candidate_id: str | None,
    integration_result_id: str | None,
) -> dict[str, object]:
    return {
        "component": CODING_JOB_LIFECYCLE_COMPONENT,
        "schema_version": CODING_JOB_LIFECYCLE_SCHEMA_VERSION,
        "job_id": job_id,
        "request_id": request_id,
        "run_id": run_id,
        "state": state,
        "session_id": session_id,
        "worker_request_id": worker_request_id,
        "worker_result_id": worker_result_id,
        "candidate_id": candidate_id,
        "integration_result_id": integration_result_id,
    }


def _snapshot_id(
    **identity: object,
) -> str:
    return hashlib.sha256(
        _SNAPSHOT_ID_DOMAIN
        + _canonical_json_bytes(
            identity
        )
    ).hexdigest()


def _validate_state_invariants(
    *,
    state: str,
    session_id: str | None,
    worker_request_id: str | None,
    worker_result_id: str | None,
    candidate_id: str | None,
    integration_result_id: str | None,
) -> None:
    if state not in CODING_JOB_LIFECYCLE_STATES:
        raise LabCodingJobLifecycleError(
            f"unknown coding-job lifecycle state {state!r}"
        )

    if (
        worker_request_id is not None
        and session_id is None
    ):
        raise LabCodingJobLifecycleError(
            "worker_request_id requires session_id"
        )

    if (
        worker_result_id is not None
        and worker_request_id is None
    ):
        raise LabCodingJobLifecycleError(
            "worker_result_id requires worker_request_id"
        )

    if state == STATE_REQUESTED:
        if any(
            value is not None
            for value in (
                session_id,
                worker_request_id,
                worker_result_id,
                candidate_id,
                integration_result_id,
            )
        ):
            raise LabCodingJobLifecycleError(
                "REQUESTED cannot contain execution or terminal evidence"
            )

    elif state == STATE_RUNNING:
        if session_id is None:
            raise LabCodingJobLifecycleError(
                "RUNNING requires session_id"
            )

        if any(
            value is not None
            for value in (
                worker_result_id,
                candidate_id,
                integration_result_id,
            )
        ):
            raise LabCodingJobLifecycleError(
                "RUNNING cannot contain terminal result evidence"
            )

    elif state == STATE_COMPLETED:
        required = {
            "session_id": session_id,
            "worker_request_id": worker_request_id,
            "worker_result_id": worker_result_id,
            "candidate_id": candidate_id,
            "integration_result_id": integration_result_id,
        }

        missing = sorted(
            name
            for name, value in required.items()
            if value is None
        )

        if missing:
            raise LabCodingJobLifecycleError(
                "COMPLETED requires exact terminal evidence: "
                + ", ".join(
                    missing
                )
            )

    elif state in {
        STATE_FAILED,
        STATE_TIMED_OUT,
    }:
        if (
            candidate_id is not None
            or integration_result_id is not None
        ):
            raise LabCodingJobLifecycleError(
                f"{state} cannot contain successful candidate/result evidence"
            )

        if (
            state == STATE_TIMED_OUT
            and session_id is None
        ):
            raise LabCodingJobLifecycleError(
                "TIMED_OUT requires session_id"
            )


def validate_lab_coding_job_lifecycle(
    lifecycle: object,
) -> LabCodingJobLifecycle:
    if type(lifecycle) is not LabCodingJobLifecycle:
        raise LabCodingJobLifecycleError(
            "lifecycle must have the exact LabCodingJobLifecycle type"
        )

    if (
        lifecycle.component
        != CODING_JOB_LIFECYCLE_COMPONENT
    ):
        raise LabCodingJobLifecycleError(
            "coding-job lifecycle component mismatch"
        )

    if (
        type(lifecycle.schema_version) is not int
        or lifecycle.schema_version
        != CODING_JOB_LIFECYCLE_SCHEMA_VERSION
    ):
        raise LabCodingJobLifecycleError(
            "unsupported coding-job lifecycle schema version"
        )

    job_id = _require_identifier(
        "job_id",
        lifecycle.job_id,
    )

    snapshot_id = _require_identifier(
        "snapshot_id",
        lifecycle.snapshot_id,
    )

    request_id = _require_identifier(
        "request_id",
        lifecycle.request_id,
    )

    run_id = _require_identifier(
        "run_id",
        lifecycle.run_id,
    )

    if type(lifecycle.state) is not str:
        raise LabCodingJobLifecycleError(
            "state must be a string"
        )

    session_id = _require_optional_identifier(
        "session_id",
        lifecycle.session_id,
    )

    worker_request_id = _require_optional_identifier(
        "worker_request_id",
        lifecycle.worker_request_id,
    )

    worker_result_id = _require_optional_identifier(
        "worker_result_id",
        lifecycle.worker_result_id,
    )

    candidate_id = _require_optional_identifier(
        "candidate_id",
        lifecycle.candidate_id,
    )

    integration_result_id = _require_optional_identifier(
        "integration_result_id",
        lifecycle.integration_result_id,
    )

    expected_job_id = _job_id(
        request_id=request_id,
        run_id=run_id,
    )

    if job_id != expected_job_id:
        raise LabCodingJobLifecycleError(
            "job_id does not match exact request_id/run_id identity"
        )

    _validate_state_invariants(
        state=lifecycle.state,
        session_id=session_id,
        worker_request_id=worker_request_id,
        worker_result_id=worker_result_id,
        candidate_id=candidate_id,
        integration_result_id=integration_result_id,
    )

    identity = _snapshot_identity(
        job_id=job_id,
        request_id=request_id,
        run_id=run_id,
        state=lifecycle.state,
        session_id=session_id,
        worker_request_id=worker_request_id,
        worker_result_id=worker_result_id,
        candidate_id=candidate_id,
        integration_result_id=integration_result_id,
    )

    expected_snapshot_id = _snapshot_id(
        **identity
    )

    if snapshot_id != expected_snapshot_id:
        raise LabCodingJobLifecycleError(
            "snapshot_id does not match exact coding-job lifecycle state"
        )

    return lifecycle


def build_lab_coding_job_lifecycle(
    *,
    request_id: object,
    run_id: object,
) -> LabCodingJobLifecycle:
    trusted_request_id = _require_identifier(
        "request_id",
        request_id,
    )

    trusted_run_id = _require_identifier(
        "run_id",
        run_id,
    )

    job_id = _job_id(
        request_id=trusted_request_id,
        run_id=trusted_run_id,
    )

    identity = _snapshot_identity(
        job_id=job_id,
        request_id=trusted_request_id,
        run_id=trusted_run_id,
        state=STATE_REQUESTED,
        session_id=None,
        worker_request_id=None,
        worker_result_id=None,
        candidate_id=None,
        integration_result_id=None,
    )

    lifecycle = LabCodingJobLifecycle(
        component=CODING_JOB_LIFECYCLE_COMPONENT,
        schema_version=CODING_JOB_LIFECYCLE_SCHEMA_VERSION,
        job_id=job_id,
        snapshot_id=_snapshot_id(
            **identity
        ),
        request_id=trusted_request_id,
        run_id=trusted_run_id,
        state=STATE_REQUESTED,
        session_id=None,
        worker_request_id=None,
        worker_result_id=None,
        candidate_id=None,
        integration_result_id=None,
    )

    return validate_lab_coding_job_lifecycle(
        lifecycle
    )


def _advance_identifier(
    *,
    name: str,
    current: str | None,
    supplied: object,
) -> str | None:
    if supplied is None:
        return current

    trusted = _require_identifier(
        name,
        supplied,
    )

    if (
        current is not None
        and trusted != current
    ):
        raise LabCodingJobLifecycleError(
            f"{name} cannot change once recorded"
        )

    return trusted


def transition_lab_coding_job_lifecycle(
    current: object,
    *,
    state: object,
    session_id: object = None,
    worker_request_id: object = None,
    worker_result_id: object = None,
    candidate_id: object = None,
    integration_result_id: object = None,
) -> LabCodingJobLifecycle:
    trusted_current = validate_lab_coding_job_lifecycle(
        current
    )

    if type(state) is not str:
        raise LabCodingJobLifecycleError(
            "state must be a string"
        )

    if state not in CODING_JOB_LIFECYCLE_STATES:
        raise LabCodingJobLifecycleError(
            f"unknown successor state {state!r}"
        )

    if (
        state
        not in _ALLOWED_STATE_TRANSITIONS[
            trusted_current.state
        ]
    ):
        raise LabCodingJobLifecycleError(
            "illegal coding-job lifecycle transition "
            f"{trusted_current.state} -> {state}"
        )

    successor_session_id = _advance_identifier(
        name="session_id",
        current=trusted_current.session_id,
        supplied=session_id,
    )

    successor_worker_request_id = _advance_identifier(
        name="worker_request_id",
        current=trusted_current.worker_request_id,
        supplied=worker_request_id,
    )

    successor_worker_result_id = _advance_identifier(
        name="worker_result_id",
        current=trusted_current.worker_result_id,
        supplied=worker_result_id,
    )

    successor_candidate_id = _advance_identifier(
        name="candidate_id",
        current=trusted_current.candidate_id,
        supplied=candidate_id,
    )

    successor_integration_result_id = _advance_identifier(
        name="integration_result_id",
        current=trusted_current.integration_result_id,
        supplied=integration_result_id,
    )

    _validate_state_invariants(
        state=state,
        session_id=successor_session_id,
        worker_request_id=successor_worker_request_id,
        worker_result_id=successor_worker_result_id,
        candidate_id=successor_candidate_id,
        integration_result_id=successor_integration_result_id,
    )

    identity = _snapshot_identity(
        job_id=trusted_current.job_id,
        request_id=trusted_current.request_id,
        run_id=trusted_current.run_id,
        state=state,
        session_id=successor_session_id,
        worker_request_id=successor_worker_request_id,
        worker_result_id=successor_worker_result_id,
        candidate_id=successor_candidate_id,
        integration_result_id=successor_integration_result_id,
    )

    successor = LabCodingJobLifecycle(
        component=trusted_current.component,
        schema_version=trusted_current.schema_version,
        job_id=trusted_current.job_id,
        snapshot_id=_snapshot_id(
            **identity
        ),
        request_id=trusted_current.request_id,
        run_id=trusted_current.run_id,
        state=state,
        session_id=successor_session_id,
        worker_request_id=successor_worker_request_id,
        worker_result_id=successor_worker_result_id,
        candidate_id=successor_candidate_id,
        integration_result_id=successor_integration_result_id,
    )

    return validate_lab_coding_job_lifecycle(
        successor
    )
