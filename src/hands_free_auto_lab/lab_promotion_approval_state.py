"""Canonical promotion-approval records with no execution capability.

This module defines and validates immutable records for the human promotion
approval boundary.

It does not:

- create directories or files,
- persist approval state,
- inspect or modify repositories,
- execute commands,
- consume approval,
- promote candidate content.

The durable store and promotion executor are separate authority boundaries.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any, Callable


SCHEMA_VERSION = 1
CHANNEL = "promotion"

CHALLENGE_COMPONENT = (
    "hands-free-auto-lab-promotion-challenge-v1"
)
DECISION_COMPONENT = (
    "hands-free-auto-lab-promotion-decision-v1"
)
APPROVAL_COMPONENT = (
    "hands-free-auto-lab-promotion-approval-v1"
)
TERMINAL_COMPONENT = (
    "hands-free-auto-lab-promotion-terminal-v1"
)

CHALLENGE_MAX_LIFETIME = timedelta(
    hours=3
)
APPROVAL_MAX_LIFETIME = timedelta(
    seconds=30
)

MAX_RECORD_BYTES = 4096

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)

CHALLENGE_FIELDS = frozenset(
    {
        "schema_version",
        "challenge_component",
        "channel",
        "challenge_id",
        "proposal_id",
        "created_at",
        "expires_at",
    }
)

DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "decision_component",
        "channel",
        "challenge_id",
        "proposal_id",
        "decision",
        "decided_at",
        "approval_id",
    }
)

APPROVAL_FIELDS = frozenset(
    {
        "schema_version",
        "approval_component",
        "channel",
        "challenge_id",
        "proposal_id",
        "approval_id",
        "approved_at",
        "created_at",
        "expires_at",
    }
)

TERMINAL_FIELDS = frozenset(
    {
        "schema_version",
        "terminal_component",
        "channel",
        "approval_id",
        "proposal_id",
        "terminal_state",
        "terminal_at",
        "run_id",
    }
)


class LabPromotionApprovalStateError(
    ValueError
):
    """Raised when promotion approval evidence is malformed."""


def _canonical_json_bytes(
    value: object,
) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(
                ",",
                ":",
            ),
            ensure_ascii=False,
        )
        + "\n"
    ).encode(
        "utf-8"
    )


def _decode_record(
    record_bytes: bytes,
    *,
    fields: frozenset[str],
    record_name: str,
) -> dict[str, Any]:
    if not isinstance(
        record_bytes,
        bytes,
    ):
        raise LabPromotionApprovalStateError(
            f"{record_name} must be bytes"
        )

    if (
        not record_bytes
        or len(
            record_bytes
        )
        > MAX_RECORD_BYTES
    ):
        raise LabPromotionApprovalStateError(
            f"{record_name} size is invalid"
        )

    try:
        text = record_bytes.decode(
            "utf-8"
        )
    except UnicodeDecodeError as exc:
        raise LabPromotionApprovalStateError(
            f"{record_name} is not UTF-8"
        ) from exc

    try:
        value = json.loads(
            text
        )
    except json.JSONDecodeError as exc:
        raise LabPromotionApprovalStateError(
            f"{record_name} is not valid JSON"
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise LabPromotionApprovalStateError(
            f"{record_name} must be a JSON object"
        )

    if frozenset(
        value
    ) != fields:
        raise LabPromotionApprovalStateError(
            f"{record_name} fields do not exactly match schema"
        )

    if (
        _canonical_json_bytes(
            value
        )
        != record_bytes
    ):
        raise LabPromotionApprovalStateError(
            f"{record_name} is not canonical JSON"
        )

    return value


def _validate_schema(
    value: object,
) -> None:
    if (
        not isinstance(
            value,
            int,
        )
        or isinstance(
            value,
            bool,
        )
        or value != SCHEMA_VERSION
    ):
        raise LabPromotionApprovalStateError(
            "schema_version mismatch"
        )


def _validate_fixed_string(
    name: str,
    value: object,
    expected: str,
) -> None:
    if value != expected:
        raise LabPromotionApprovalStateError(
            f"{name} mismatch"
        )


def _validate_identifier(
    name: str,
    value: object,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionApprovalStateError(
            f"{name} must be a string"
        )

    if (
        len(
            value
        )
        != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabPromotionApprovalStateError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _validate_optional_identifier(
    name: str,
    value: object,
) -> str | None:
    if value is None:
        return None

    return _validate_identifier(
        name,
        value,
    )


def _validate_timestamp(
    name: str,
    value: object,
) -> datetime:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionApprovalStateError(
            f"{name} must be a string"
        )

    try:
        parsed = datetime.fromisoformat(
            value
        )
    except ValueError as exc:
        raise LabPromotionApprovalStateError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc

    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
    ):
        raise LabPromotionApprovalStateError(
            f"{name} must be timezone-aware"
        )

    if parsed.isoformat() != value:
        raise LabPromotionApprovalStateError(
            f"{name} must be canonical ISO-8601"
        )

    return parsed


def _build_record(
    value: dict[str, object],
    validator: Callable[
        [bytes],
        dict[str, Any],
    ],
) -> bytes:
    record_bytes = _canonical_json_bytes(
        value
    )

    validator(
        record_bytes
    )

    return record_bytes


def validate_challenge_record(
    record_bytes: bytes,
) -> dict[str, Any]:
    record = _decode_record(
        record_bytes,
        fields=CHALLENGE_FIELDS,
        record_name="challenge record",
    )

    _validate_schema(
        record["schema_version"]
    )

    _validate_fixed_string(
        "challenge_component",
        record["challenge_component"],
        CHALLENGE_COMPONENT,
    )

    _validate_fixed_string(
        "channel",
        record["channel"],
        CHANNEL,
    )

    _validate_identifier(
        "challenge_id",
        record["challenge_id"],
    )

    _validate_identifier(
        "proposal_id",
        record["proposal_id"],
    )

    created_at = _validate_timestamp(
        "created_at",
        record["created_at"],
    )

    expires_at = _validate_timestamp(
        "expires_at",
        record["expires_at"],
    )

    if expires_at <= created_at:
        raise LabPromotionApprovalStateError(
            "expires_at must be later than created_at"
        )

    if (
        expires_at
        - created_at
        > CHALLENGE_MAX_LIFETIME
    ):
        raise LabPromotionApprovalStateError(
            "challenge lifetime must not exceed 3 hours"
        )

    return record


def build_challenge_record(
    *,
    challenge_id: str,
    proposal_id: str,
    created_at: str,
    expires_at: str,
) -> bytes:
    return _build_record(
        {
            "schema_version": SCHEMA_VERSION,
            "challenge_component": (
                CHALLENGE_COMPONENT
            ),
            "channel": CHANNEL,
            "challenge_id": challenge_id,
            "proposal_id": proposal_id,
            "created_at": created_at,
            "expires_at": expires_at,
        },
        validate_challenge_record,
    )


def validate_decision_record(
    record_bytes: bytes,
) -> dict[str, Any]:
    record = _decode_record(
        record_bytes,
        fields=DECISION_FIELDS,
        record_name="decision record",
    )

    _validate_schema(
        record["schema_version"]
    )

    _validate_fixed_string(
        "decision_component",
        record["decision_component"],
        DECISION_COMPONENT,
    )

    _validate_fixed_string(
        "channel",
        record["channel"],
        CHANNEL,
    )

    _validate_identifier(
        "challenge_id",
        record["challenge_id"],
    )

    _validate_identifier(
        "proposal_id",
        record["proposal_id"],
    )

    decision = record["decision"]

    if decision not in {
        "approved",
        "rejected",
        "expired",
    }:
        raise LabPromotionApprovalStateError(
            "decision must be approved, rejected, or expired"
        )

    _validate_timestamp(
        "decided_at",
        record["decided_at"],
    )

    approval_id = _validate_optional_identifier(
        "approval_id",
        record["approval_id"],
    )

    if decision == "approved":
        if approval_id is None:
            raise LabPromotionApprovalStateError(
                "approved decision requires approval_id"
            )
    elif approval_id is not None:
        raise LabPromotionApprovalStateError(
            "non-approved decision requires null approval_id"
        )

    return record


def build_decision_record(
    *,
    challenge_id: str,
    proposal_id: str,
    decision: str,
    decided_at: str,
    approval_id: str | None,
) -> bytes:
    return _build_record(
        {
            "schema_version": SCHEMA_VERSION,
            "decision_component": (
                DECISION_COMPONENT
            ),
            "channel": CHANNEL,
            "challenge_id": challenge_id,
            "proposal_id": proposal_id,
            "decision": decision,
            "decided_at": decided_at,
            "approval_id": approval_id,
        },
        validate_decision_record,
    )


def validate_approval_record(
    record_bytes: bytes,
) -> dict[str, Any]:
    record = _decode_record(
        record_bytes,
        fields=APPROVAL_FIELDS,
        record_name="approval record",
    )

    _validate_schema(
        record["schema_version"]
    )

    _validate_fixed_string(
        "approval_component",
        record["approval_component"],
        APPROVAL_COMPONENT,
    )

    _validate_fixed_string(
        "channel",
        record["channel"],
        CHANNEL,
    )

    _validate_identifier(
        "challenge_id",
        record["challenge_id"],
    )

    _validate_identifier(
        "proposal_id",
        record["proposal_id"],
    )

    _validate_identifier(
        "approval_id",
        record["approval_id"],
    )

    approved_at = _validate_timestamp(
        "approved_at",
        record["approved_at"],
    )

    created_at = _validate_timestamp(
        "created_at",
        record["created_at"],
    )

    expires_at = _validate_timestamp(
        "expires_at",
        record["expires_at"],
    )

    if created_at < approved_at:
        raise LabPromotionApprovalStateError(
            "created_at must not be earlier than approved_at"
        )

    if expires_at <= created_at:
        raise LabPromotionApprovalStateError(
            "expires_at must be later than created_at"
        )

    if (
        expires_at
        - created_at
        > APPROVAL_MAX_LIFETIME
    ):
        raise LabPromotionApprovalStateError(
            "approval lifetime must not exceed 30 seconds"
        )

    return record


def build_approval_record(
    *,
    challenge_id: str,
    proposal_id: str,
    approval_id: str,
    approved_at: str,
    created_at: str,
    expires_at: str,
) -> bytes:
    return _build_record(
        {
            "schema_version": SCHEMA_VERSION,
            "approval_component": (
                APPROVAL_COMPONENT
            ),
            "channel": CHANNEL,
            "challenge_id": challenge_id,
            "proposal_id": proposal_id,
            "approval_id": approval_id,
            "approved_at": approved_at,
            "created_at": created_at,
            "expires_at": expires_at,
        },
        validate_approval_record,
    )


def validate_terminal_record(
    record_bytes: bytes,
) -> dict[str, Any]:
    record = _decode_record(
        record_bytes,
        fields=TERMINAL_FIELDS,
        record_name="terminal record",
    )

    _validate_schema(
        record["schema_version"]
    )

    _validate_fixed_string(
        "terminal_component",
        record["terminal_component"],
        TERMINAL_COMPONENT,
    )

    _validate_fixed_string(
        "channel",
        record["channel"],
        CHANNEL,
    )

    _validate_identifier(
        "approval_id",
        record["approval_id"],
    )

    _validate_identifier(
        "proposal_id",
        record["proposal_id"],
    )

    terminal_state = record[
        "terminal_state"
    ]

    if terminal_state not in {
        "consumed",
        "revoked",
        "expired",
    }:
        raise LabPromotionApprovalStateError(
            "terminal_state must be consumed, revoked, or expired"
        )

    _validate_timestamp(
        "terminal_at",
        record["terminal_at"],
    )

    run_id = _validate_optional_identifier(
        "run_id",
        record["run_id"],
    )

    if terminal_state == "consumed":
        if run_id is None:
            raise LabPromotionApprovalStateError(
                "consumed terminal requires run_id"
            )
    elif run_id is not None:
        raise LabPromotionApprovalStateError(
            "non-consumed terminal requires null run_id"
        )

    return record


def build_terminal_record(
    *,
    approval_id: str,
    proposal_id: str,
    terminal_state: str,
    terminal_at: str,
    run_id: str | None,
) -> bytes:
    return _build_record(
        {
            "schema_version": SCHEMA_VERSION,
            "terminal_component": (
                TERMINAL_COMPONENT
            ),
            "channel": CHANNEL,
            "approval_id": approval_id,
            "proposal_id": proposal_id,
            "terminal_state": terminal_state,
            "terminal_at": terminal_at,
            "run_id": run_id,
        },
        validate_terminal_record,
    )
