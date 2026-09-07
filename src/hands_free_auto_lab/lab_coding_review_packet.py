"""Immutable unified review evidence for completed coding-job queues.

This module is a pure model/serialization boundary. It does not execute jobs,
load journals or stores, inspect repositories, construct promotion proposals,
create or consume approvals, mutate files, or perform Git/network operations.

A review packet binds one exact validated queue, in queue order, to exact
COMPLETED lifecycle evidence, exact integration-result evidence, and exact
independently validated coding candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .lab_coding_candidate import (
    LabCodingCandidate,
    lab_coding_candidate_from_wire,
    lab_coding_candidate_to_wire,
    validate_lab_coding_candidate,
)
from .lab_coding_integration_contract import (
    LabCodingIntegrationResult,
    lab_coding_integration_request_from_wire,
    lab_coding_integration_request_to_wire,
    lab_coding_integration_result_from_wire,
    lab_coding_integration_result_to_wire,
    validate_lab_coding_integration_result,
)
from .lab_coding_job_lifecycle import (
    LabCodingJobLifecycle,
    validate_lab_coding_job_lifecycle,
)
from .lab_coding_job_queue import (
    LabCodingJobQueue,
    LabCodingJobQueueItem,
    validate_lab_coding_job_queue,
)


CODING_REVIEW_PACKET_COMPONENT = (
    "hands-free-auto-lab-coding-review-packet-v1"
)
CODING_REVIEW_PACKET_SCHEMA_VERSION = 1

_PACKET_ID_DOMAIN = (
    b"hands-free-auto-lab-coding-review-packet-id-v1\x00"
)

_PACKET_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "packet_id",
        "queue",
        "items",
    }
)

_REVIEW_ITEM_FIELDS = frozenset(
    {
        "lifecycle",
        "result",
        "candidate",
    }
)

_QUEUE_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "queue_id",
        "items",
    }
)

_QUEUE_ITEM_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "request",
        "run_id",
        "job_id",
    }
)

_LIFECYCLE_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "job_id",
        "snapshot_id",
        "request_id",
        "run_id",
        "state",
        "session_id",
        "worker_request_id",
        "worker_result_id",
        "candidate_id",
        "integration_result_id",
    }
)


class LabCodingReviewPacketError(ValueError):
    """Unified coding review evidence is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class LabCodingReviewItem:
    lifecycle: LabCodingJobLifecycle
    result: LabCodingIntegrationResult
    candidate: LabCodingCandidate


@dataclass(frozen=True, slots=True)
class LabCodingReviewPacket:
    component: str
    schema_version: int
    packet_id: str
    queue: LabCodingJobQueue
    items: tuple[LabCodingReviewItem, ...]


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LabCodingReviewPacketError(
            f"review evidence cannot be canonically encoded: {exc}"
        ) from exc


def _require_identifier(
    value: object,
    *,
    name: str,
) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value
        )
    ):
        raise LabCodingReviewPacketError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _packet_identity(
    *,
    queue: LabCodingJobQueue,
    items: tuple[LabCodingReviewItem, ...],
) -> dict[str, object]:
    return {
        "component": CODING_REVIEW_PACKET_COMPONENT,
        "schema_version": CODING_REVIEW_PACKET_SCHEMA_VERSION,
        "queue_id": queue.queue_id,
        "items": [
            {
                "snapshot_id": item.lifecycle.snapshot_id,
                "result_id": item.result.result_id,
                "candidate_id": item.candidate.candidate_id,
            }
            for item in items
        ],
    }


def _packet_id(
    *,
    queue: LabCodingJobQueue,
    items: tuple[LabCodingReviewItem, ...],
) -> str:
    return hashlib.sha256(
        _PACKET_ID_DOMAIN
        + _canonical_json_bytes(
            _packet_identity(
                queue=queue,
                items=items,
            )
        )
    ).hexdigest()


def validate_lab_coding_review_item(
    item: object,
) -> LabCodingReviewItem:
    if type(item) is not LabCodingReviewItem:
        raise LabCodingReviewPacketError(
            "review item must have the exact LabCodingReviewItem type"
        )

    try:
        lifecycle = validate_lab_coding_job_lifecycle(
            item.lifecycle
        )
        result = validate_lab_coding_integration_result(
            item.result
        )
        candidate = validate_lab_coding_candidate(
            item.candidate
        )
    except Exception as exc:
        raise LabCodingReviewPacketError(
            f"nested review evidence failed validation: {exc}"
        ) from exc

    if lifecycle.state != "COMPLETED":
        raise LabCodingReviewPacketError(
            "review item lifecycle must be COMPLETED"
        )

    if lifecycle.request_id != result.request_id:
        raise LabCodingReviewPacketError(
            "lifecycle/result request_id mismatch"
        )

    if lifecycle.candidate_id != result.candidate_id:
        raise LabCodingReviewPacketError(
            "lifecycle/result candidate_id mismatch"
        )

    if lifecycle.candidate_id != candidate.candidate_id:
        raise LabCodingReviewPacketError(
            "lifecycle/candidate candidate_id mismatch"
        )

    if lifecycle.integration_result_id != result.result_id:
        raise LabCodingReviewPacketError(
            "lifecycle integration_result_id/result_id mismatch"
        )

    if lifecycle.session_id != candidate.workspace_session_id:
        raise LabCodingReviewPacketError(
            "lifecycle session_id/candidate workspace_session_id mismatch"
        )

    return item


def build_lab_coding_review_item(
    *,
    lifecycle: object,
    result: object,
    candidate: object,
) -> LabCodingReviewItem:
    item = LabCodingReviewItem(
        lifecycle=lifecycle,
        result=result,
        candidate=candidate,
    )

    return validate_lab_coding_review_item(
        item
    )


def _validate_review_item_for_queue_item(
    *,
    queue_item: LabCodingJobQueueItem,
    review_item: object,
) -> LabCodingReviewItem:
    trusted = validate_lab_coding_review_item(
        review_item
    )

    if queue_item.request.request_id != trusted.lifecycle.request_id:
        raise LabCodingReviewPacketError(
            "queue/review request_id mismatch"
        )

    if queue_item.run_id != trusted.lifecycle.run_id:
        raise LabCodingReviewPacketError(
            "queue/review run_id mismatch"
        )

    if queue_item.job_id != trusted.lifecycle.job_id:
        raise LabCodingReviewPacketError(
            "queue/review job_id mismatch"
        )

    return trusted


def validate_lab_coding_review_packet(
    packet: object,
) -> LabCodingReviewPacket:
    if type(packet) is not LabCodingReviewPacket:
        raise LabCodingReviewPacketError(
            "packet must have the exact LabCodingReviewPacket type"
        )

    if (
        type(packet.component) is not str
        or packet.component != CODING_REVIEW_PACKET_COMPONENT
    ):
        raise LabCodingReviewPacketError(
            "review packet component mismatch"
        )

    if (
        type(packet.schema_version) is not int
        or packet.schema_version
        != CODING_REVIEW_PACKET_SCHEMA_VERSION
    ):
        raise LabCodingReviewPacketError(
            "unsupported review packet schema version"
        )

    supplied_packet_id = _require_identifier(
        packet.packet_id,
        name="packet_id",
    )

    try:
        queue = validate_lab_coding_job_queue(
            packet.queue
        )
    except Exception as exc:
        raise LabCodingReviewPacketError(
            f"review packet queue failed validation: {exc}"
        ) from exc

    if type(packet.items) is not tuple:
        raise LabCodingReviewPacketError(
            "review packet items must be an exact tuple"
        )

    if len(packet.items) != len(queue.items):
        raise LabCodingReviewPacketError(
            "review item count must exactly match queue item count"
        )

    trusted_items = tuple(
        _validate_review_item_for_queue_item(
            queue_item=queue_item,
            review_item=review_item,
        )
        for queue_item, review_item in zip(
            queue.items,
            packet.items,
        )
    )

    expected_packet_id = _packet_id(
        queue=queue,
        items=trusted_items,
    )

    if supplied_packet_id != expected_packet_id:
        raise LabCodingReviewPacketError(
            "packet_id does not match exact review evidence"
        )

    return packet


def build_lab_coding_review_packet(
    *,
    queue: object,
    items: object,
) -> LabCodingReviewPacket:
    try:
        trusted_queue = validate_lab_coding_job_queue(
            queue
        )
    except Exception as exc:
        raise LabCodingReviewPacketError(
            f"review packet queue failed validation: {exc}"
        ) from exc

    if type(items) is not tuple:
        raise LabCodingReviewPacketError(
            "review packet items must be an exact tuple"
        )

    if len(items) != len(trusted_queue.items):
        raise LabCodingReviewPacketError(
            "review item count must exactly match queue item count"
        )

    trusted_items = tuple(
        _validate_review_item_for_queue_item(
            queue_item=queue_item,
            review_item=review_item,
        )
        for queue_item, review_item in zip(
            trusted_queue.items,
            items,
        )
    )

    packet = LabCodingReviewPacket(
        component=CODING_REVIEW_PACKET_COMPONENT,
        schema_version=CODING_REVIEW_PACKET_SCHEMA_VERSION,
        packet_id=_packet_id(
            queue=trusted_queue,
            items=trusted_items,
        ),
        queue=trusted_queue,
        items=trusted_items,
    )

    return validate_lab_coding_review_packet(
        packet
    )


def _queue_to_wire(
    queue: LabCodingJobQueue,
) -> dict[str, object]:
    trusted = validate_lab_coding_job_queue(
        queue
    )

    return {
        "component": trusted.component,
        "schema_version": trusted.schema_version,
        "queue_id": trusted.queue_id,
        "items": [
            {
                "component": item.component,
                "schema_version": item.schema_version,
                "request": lab_coding_integration_request_to_wire(
                    item.request
                ),
                "run_id": item.run_id,
                "job_id": item.job_id,
            }
            for item in trusted.items
        ],
    }


def _queue_from_wire(
    value: object,
) -> LabCodingJobQueue:
    if type(value) is not dict:
        raise LabCodingReviewPacketError(
            "review queue wire value must be an exact object"
        )

    if set(value) != _QUEUE_FIELDS:
        raise LabCodingReviewPacketError(
            "review queue wire fields do not exactly match schema"
        )

    raw_items = value["items"]

    if type(raw_items) is not list:
        raise LabCodingReviewPacketError(
            "review queue wire items must be an exact list"
        )

    items: list[LabCodingJobQueueItem] = []

    for raw_item in raw_items:
        if type(raw_item) is not dict:
            raise LabCodingReviewPacketError(
                "review queue item wire value must be an exact object"
            )

        if set(raw_item) != _QUEUE_ITEM_FIELDS:
            raise LabCodingReviewPacketError(
                "review queue item wire fields do not exactly match schema"
            )

        try:
            request = lab_coding_integration_request_from_wire(
                raw_item["request"]
            )
        except Exception as exc:
            raise LabCodingReviewPacketError(
                f"review queue request wire value is invalid: {exc}"
            ) from exc

        items.append(
            LabCodingJobQueueItem(
                component=raw_item["component"],
                schema_version=raw_item["schema_version"],
                request=request,
                run_id=raw_item["run_id"],
                job_id=raw_item["job_id"],
            )
        )

    queue = LabCodingJobQueue(
        component=value["component"],
        schema_version=value["schema_version"],
        queue_id=value["queue_id"],
        items=tuple(items),
    )

    try:
        return validate_lab_coding_job_queue(
            queue
        )
    except Exception as exc:
        raise LabCodingReviewPacketError(
            f"review queue wire value failed validation: {exc}"
        ) from exc


def _lifecycle_to_wire(
    lifecycle: LabCodingJobLifecycle,
) -> dict[str, object]:
    trusted = validate_lab_coding_job_lifecycle(
        lifecycle
    )

    return {
        "component": trusted.component,
        "schema_version": trusted.schema_version,
        "job_id": trusted.job_id,
        "snapshot_id": trusted.snapshot_id,
        "request_id": trusted.request_id,
        "run_id": trusted.run_id,
        "state": trusted.state,
        "session_id": trusted.session_id,
        "worker_request_id": trusted.worker_request_id,
        "worker_result_id": trusted.worker_result_id,
        "candidate_id": trusted.candidate_id,
        "integration_result_id": trusted.integration_result_id,
    }


def _lifecycle_from_wire(
    value: object,
) -> LabCodingJobLifecycle:
    if type(value) is not dict:
        raise LabCodingReviewPacketError(
            "review lifecycle wire value must be an exact object"
        )

    if set(value) != _LIFECYCLE_FIELDS:
        raise LabCodingReviewPacketError(
            "review lifecycle wire fields do not exactly match schema"
        )

    lifecycle = LabCodingJobLifecycle(
        component=value["component"],
        schema_version=value["schema_version"],
        job_id=value["job_id"],
        snapshot_id=value["snapshot_id"],
        request_id=value["request_id"],
        run_id=value["run_id"],
        state=value["state"],
        session_id=value["session_id"],
        worker_request_id=value["worker_request_id"],
        worker_result_id=value["worker_result_id"],
        candidate_id=value["candidate_id"],
        integration_result_id=value["integration_result_id"],
    )

    try:
        return validate_lab_coding_job_lifecycle(
            lifecycle
        )
    except Exception as exc:
        raise LabCodingReviewPacketError(
            f"review lifecycle wire value failed validation: {exc}"
        ) from exc


def lab_coding_review_packet_to_wire(
    packet: object,
) -> dict[str, object]:
    trusted = validate_lab_coding_review_packet(
        packet
    )

    return {
        "component": trusted.component,
        "schema_version": trusted.schema_version,
        "packet_id": trusted.packet_id,
        "queue": _queue_to_wire(
            trusted.queue
        ),
        "items": [
            {
                "lifecycle": _lifecycle_to_wire(
                    item.lifecycle
                ),
                "result": lab_coding_integration_result_to_wire(
                    item.result
                ),
                "candidate": lab_coding_candidate_to_wire(
                    item.candidate
                ),
            }
            for item in trusted.items
        ],
    }


def lab_coding_review_packet_from_wire(
    value: object,
) -> LabCodingReviewPacket:
    if type(value) is not dict:
        raise LabCodingReviewPacketError(
            "review packet wire value must be an exact object"
        )

    if set(value) != _PACKET_FIELDS:
        raise LabCodingReviewPacketError(
            "review packet wire fields do not exactly match schema"
        )

    try:
        queue = _queue_from_wire(
            value["queue"]
        )
    except LabCodingReviewPacketError:
        raise
    except Exception as exc:
        raise LabCodingReviewPacketError(
            f"review packet queue wire value is invalid: {exc}"
        ) from exc

    raw_items = value["items"]

    if type(raw_items) is not list:
        raise LabCodingReviewPacketError(
            "review packet wire items must be an exact list"
        )

    items: list[LabCodingReviewItem] = []

    for raw_item in raw_items:
        if type(raw_item) is not dict:
            raise LabCodingReviewPacketError(
                "review item wire value must be an exact object"
            )

        if set(raw_item) != _REVIEW_ITEM_FIELDS:
            raise LabCodingReviewPacketError(
                "review item wire fields do not exactly match schema"
            )

        try:
            lifecycle = _lifecycle_from_wire(
                raw_item["lifecycle"]
            )
            result = lab_coding_integration_result_from_wire(
                raw_item["result"]
            )
            candidate = lab_coding_candidate_from_wire(
                raw_item["candidate"]
            )
        except LabCodingReviewPacketError:
            raise
        except Exception as exc:
            raise LabCodingReviewPacketError(
                f"nested review wire evidence is invalid: {exc}"
            ) from exc

        items.append(
            build_lab_coding_review_item(
                lifecycle=lifecycle,
                result=result,
                candidate=candidate,
            )
        )

    packet = LabCodingReviewPacket(
        component=value["component"],
        schema_version=value["schema_version"],
        packet_id=value["packet_id"],
        queue=queue,
        items=tuple(items),
    )

    return validate_lab_coding_review_packet(
        packet
    )


def lab_coding_review_packet_to_bytes(
    packet: object,
) -> bytes:
    return _canonical_json_bytes(
        lab_coding_review_packet_to_wire(
            packet
        )
    )


def lab_coding_review_packet_from_bytes(
    record_bytes: object,
) -> LabCodingReviewPacket:
    if type(record_bytes) is not bytes:
        raise LabCodingReviewPacketError(
            "review packet record must have the exact bytes type"
        )

    try:
        text = record_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise LabCodingReviewPacketError(
            "review packet record is not valid UTF-8"
        ) from exc

    try:
        value = json.loads(
            text
        )
    except json.JSONDecodeError as exc:
        raise LabCodingReviewPacketError(
            "review packet record is not valid JSON"
        ) from exc

    if type(value) is not dict:
        raise LabCodingReviewPacketError(
            "review packet record must be a JSON object"
        )

    canonical = _canonical_json_bytes(
        value
    )

    if canonical != record_bytes:
        raise LabCodingReviewPacketError(
            "review packet record is not canonical JSON"
        )

    return lab_coding_review_packet_from_wire(
        value
    )
