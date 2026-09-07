"""Pure bounded queue model for independent Auto Lab coding attempts.

This module defines immutable queue evidence only.

It has no filesystem, lifecycle-journal, worker, deployment, execution,
approval, promotion, repository-mutation, Git, retry, resume, networking,
credential, or persistence authority.

Each queue item binds one exact validated coding integration request to one
exact run_id. The existing lifecycle builder remains the sole source of the
derived job_id algorithm.

A queue is ordered, non-empty, bounded, deterministic, and contains no
duplicate request_id or run_id. Re-running a request is therefore not an
operation inside one queue; a later explicit attempt belongs to a separately
constructed queue with a new run_id.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .lab_coding_integration_contract import (
    LabCodingIntegrationRequest,
    validate_lab_coding_integration_request,
)
from .lab_coding_job_lifecycle import (
    build_lab_coding_job_lifecycle,
)


CODING_JOB_QUEUE_ITEM_COMPONENT = (
    "hands-free-auto-lab-coding-job-queue-item-v1"
)
CODING_JOB_QUEUE_ITEM_SCHEMA_VERSION = 1

CODING_JOB_QUEUE_COMPONENT = (
    "hands-free-auto-lab-coding-job-queue-v1"
)
CODING_JOB_QUEUE_SCHEMA_VERSION = 1

MAX_CODING_JOB_QUEUE_ITEMS = 32

_QUEUE_ID_DOMAIN = (
    b"hands-free-auto-lab-coding-job-queue-id-v1\x00"
)


class LabCodingJobQueueError(ValueError):
    """Coding-job queue evidence is invalid."""


@dataclass(frozen=True, slots=True)
class LabCodingJobQueueItem:
    component: str
    schema_version: int
    request: LabCodingIntegrationRequest
    run_id: str
    job_id: str


@dataclass(frozen=True, slots=True)
class LabCodingJobQueue:
    component: str
    schema_version: int
    queue_id: str
    items: tuple[LabCodingJobQueueItem, ...]


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode(
        "utf-8",
        errors="strict",
    )


def _queue_identity(
    items: tuple[LabCodingJobQueueItem, ...],
) -> dict[str, object]:
    return {
        "component": CODING_JOB_QUEUE_COMPONENT,
        "schema_version": CODING_JOB_QUEUE_SCHEMA_VERSION,
        "items": [
            {
                "request_id": item.request.request_id,
                "run_id": item.run_id,
                "job_id": item.job_id,
            }
            for item in items
        ],
    }


def _queue_id(
    items: tuple[LabCodingJobQueueItem, ...],
) -> str:
    return hashlib.sha256(
        _QUEUE_ID_DOMAIN
        + _canonical_json_bytes(
            _queue_identity(items)
        )
    ).hexdigest()


def validate_lab_coding_job_queue_item(
    item: object,
) -> LabCodingJobQueueItem:
    if type(item) is not LabCodingJobQueueItem:
        raise LabCodingJobQueueError(
            "queue item must have the exact LabCodingJobQueueItem type"
        )

    if item.component != CODING_JOB_QUEUE_ITEM_COMPONENT:
        raise LabCodingJobQueueError(
            "coding-job queue item component mismatch"
        )

    if (
        type(item.schema_version) is not int
        or item.schema_version
        != CODING_JOB_QUEUE_ITEM_SCHEMA_VERSION
    ):
        raise LabCodingJobQueueError(
            "unsupported coding-job queue item schema version"
        )

    request = validate_lab_coding_integration_request(
        item.request
    )

    lifecycle = build_lab_coding_job_lifecycle(
        request_id=request.request_id,
        run_id=item.run_id,
    )

    if type(item.job_id) is not str:
        raise LabCodingJobQueueError(
            "job_id must be a string"
        )

    if item.job_id != lifecycle.job_id:
        raise LabCodingJobQueueError(
            "job_id does not match exact request_id/run_id identity"
        )

    return item


def build_lab_coding_job_queue_item(
    *,
    request: object,
    run_id: object,
) -> LabCodingJobQueueItem:
    trusted_request = validate_lab_coding_integration_request(
        request
    )

    lifecycle = build_lab_coding_job_lifecycle(
        request_id=trusted_request.request_id,
        run_id=run_id,
    )

    item = LabCodingJobQueueItem(
        component=CODING_JOB_QUEUE_ITEM_COMPONENT,
        schema_version=CODING_JOB_QUEUE_ITEM_SCHEMA_VERSION,
        request=trusted_request,
        run_id=lifecycle.run_id,
        job_id=lifecycle.job_id,
    )

    return validate_lab_coding_job_queue_item(
        item
    )


def validate_lab_coding_job_queue(
    queue: object,
) -> LabCodingJobQueue:
    if type(queue) is not LabCodingJobQueue:
        raise LabCodingJobQueueError(
            "queue must have the exact LabCodingJobQueue type"
        )

    if queue.component != CODING_JOB_QUEUE_COMPONENT:
        raise LabCodingJobQueueError(
            "coding-job queue component mismatch"
        )

    if (
        type(queue.schema_version) is not int
        or queue.schema_version
        != CODING_JOB_QUEUE_SCHEMA_VERSION
    ):
        raise LabCodingJobQueueError(
            "unsupported coding-job queue schema version"
        )

    if (
        type(queue.items) is not tuple
        or not queue.items
    ):
        raise LabCodingJobQueueError(
            "items must be a non-empty exact tuple"
        )

    if len(queue.items) > MAX_CODING_JOB_QUEUE_ITEMS:
        raise LabCodingJobQueueError(
            "coding-job queue exceeds maximum item count"
        )

    items = tuple(
        validate_lab_coding_job_queue_item(item)
        for item in queue.items
    )

    request_ids = tuple(
        item.request.request_id
        for item in items
    )
    run_ids = tuple(
        item.run_id
        for item in items
    )

    if len(request_ids) != len(set(request_ids)):
        raise LabCodingJobQueueError(
            "queue must not contain duplicate request_id values"
        )

    if len(run_ids) != len(set(run_ids)):
        raise LabCodingJobQueueError(
            "queue must not contain duplicate run_id values"
        )

    if type(queue.queue_id) is not str:
        raise LabCodingJobQueueError(
            "queue_id must be a string"
        )

    if queue.queue_id != _queue_id(items):
        raise LabCodingJobQueueError(
            "queue_id does not match exact ordered queue contents"
        )

    return queue


def build_lab_coding_job_queue(
    *,
    items: object,
) -> LabCodingJobQueue:
    if type(items) is not tuple:
        raise LabCodingJobQueueError(
            "items must be a non-empty exact tuple"
        )

    if not items:
        raise LabCodingJobQueueError(
            "items must be a non-empty exact tuple"
        )

    if len(items) > MAX_CODING_JOB_QUEUE_ITEMS:
        raise LabCodingJobQueueError(
            "coding-job queue exceeds maximum item count"
        )

    trusted_items = tuple(
        validate_lab_coding_job_queue_item(item)
        for item in items
    )

    queue = LabCodingJobQueue(
        component=CODING_JOB_QUEUE_COMPONENT,
        schema_version=CODING_JOB_QUEUE_SCHEMA_VERSION,
        queue_id=_queue_id(trusted_items),
        items=trusted_items,
    )

    return validate_lab_coding_job_queue(
        queue
    )
