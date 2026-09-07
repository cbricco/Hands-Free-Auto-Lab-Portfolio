"""Pure promotion transaction and recovery-state model.

This module defines immutable transaction snapshots for an exact immutable
promotion proposal.

It deliberately has no filesystem, repository, Git, approval-store, shell,
rollback-execution, or promotion-execution authority.

A transaction is bound to:

- the independently revalidated proposal identity,
- exact repository identity and Git context already present in that proposal,
- one exact run_id,
- deterministic sorted file plans,
- derived risk classification,
- legal transaction-state transitions,
- per-file progress,
- deterministic transaction_id and snapshot_id.

Durable journal persistence and repository mutation are separate authority
boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json

from .lab_promotion_proposal import (
    LabPromotionProposal,
    LabPromotionProposalFile,
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
    PROMOTION_PROPOSAL_COMPONENT,
    PROMOTION_PROPOSAL_SCHEMA_VERSION,
    ALLOWED_SOURCE_KINDS,
    _PROPOSAL_ID_DOMAIN,
    _canonical_json_bytes as _proposal_canonical_json_bytes,
    _proposal_identity_object,
)


TRANSACTION_COMPONENT = (
    "hands-free-auto-lab-promotion-transaction-v3"
)
TRANSACTION_SCHEMA_VERSION = 3

STATE_PREPARED = "PREPARED"
STATE_APPLYING = "APPLYING"
STATE_VERIFYING = "VERIFYING"
STATE_COMPLETED = "COMPLETED"
STATE_ROLLING_BACK = "ROLLING_BACK"
STATE_ROLLED_BACK = "ROLLED_BACK"
STATE_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

TRANSACTION_STATES = frozenset(
    {
        STATE_PREPARED,
        STATE_APPLYING,
        STATE_VERIFYING,
        STATE_COMPLETED,
        STATE_ROLLING_BACK,
        STATE_ROLLED_BACK,
        STATE_RECOVERY_REQUIRED,
    }
)

PROGRESS_PENDING = "PENDING"
PROGRESS_INSTALLED = "INSTALLED"
PROGRESS_VERIFIED = "VERIFIED"
PROGRESS_RESTORED = "RESTORED"

FILE_PROGRESS_STATES = frozenset(
    {
        PROGRESS_PENDING,
        PROGRESS_INSTALLED,
        PROGRESS_VERIFIED,
        PROGRESS_RESTORED,
    }
)

RISK_WRITE = "WRITE"
RISK_DESTRUCTIVE_HIGH = "DESTRUCTIVE / HIGH-RISK"

_TRANSACTION_ID_DOMAIN = (
    b"hands-free-auto-lab-promotion-transaction-id-v3\x00"
)
_SNAPSHOT_ID_DOMAIN = (
    b"hands-free-auto-lab-promotion-transaction-snapshot-id-v3\x00"
)

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)

_ALLOWED_STATE_TRANSITIONS = {
    STATE_PREPARED: frozenset(
        {
            STATE_APPLYING,
            STATE_RECOVERY_REQUIRED,
        }
    ),
    STATE_APPLYING: frozenset(
        {
            STATE_APPLYING,
            STATE_VERIFYING,
            STATE_ROLLING_BACK,
            STATE_RECOVERY_REQUIRED,
        }
    ),
    STATE_VERIFYING: frozenset(
        {
            STATE_VERIFYING,
            STATE_COMPLETED,
            STATE_ROLLING_BACK,
            STATE_RECOVERY_REQUIRED,
        }
    ),
    STATE_ROLLING_BACK: frozenset(
        {
            STATE_ROLLING_BACK,
            STATE_ROLLED_BACK,
            STATE_RECOVERY_REQUIRED,
        }
    ),
    STATE_COMPLETED: frozenset(),
    STATE_ROLLED_BACK: frozenset(),
    STATE_RECOVERY_REQUIRED: frozenset(),
}

_ALLOWED_PROGRESS_TRANSITIONS = frozenset(
    {
        (
            PROGRESS_PENDING,
            PROGRESS_INSTALLED,
        ),
        (
            PROGRESS_INSTALLED,
            PROGRESS_VERIFIED,
        ),
        (
            PROGRESS_INSTALLED,
            PROGRESS_RESTORED,
        ),
        (
            PROGRESS_VERIFIED,
            PROGRESS_RESTORED,
        ),
    }
)


class LabPromotionTransactionStateError(
    ValueError
):
    """Raised when transaction evidence or a transition is invalid."""


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionTransactionFile:
    operation: str
    path: str
    before_exists: bool
    before_bytes: int | None
    before_sha256: str | None
    before_mode: int | None
    after_bytes: int
    after_sha256: str
    after_mode: int
    after_content: str
    source_kind: str
    source_id: str
    progress: str


@dataclass(
    frozen=True,
    slots=True,
)
class LabPromotionTransaction:
    component: str
    schema_version: int
    transaction_id: str
    snapshot_id: str
    proposal_id: str
    candidate_id: str
    run_id: str
    repository_path: str
    repository_device: int
    repository_inode: int
    branch: str
    head: str
    risk_classification: str
    state: str
    files: tuple[
        LabPromotionTransactionFile,
        ...,
    ]


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
        "utf-8"
    )


def _require_text(
    name: str,
    value: object,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionTransactionStateError(
            f"{name} must be a string"
        )

    if (
        not allow_empty
        and not value
    ):
        raise LabPromotionTransactionStateError(
            f"{name} must not be empty"
        )

    if "\x00" in value:
        raise LabPromotionTransactionStateError(
            f"{name} must not contain NUL"
        )

    return value


def _require_integer(
    name: str,
    value: object,
    *,
    minimum: int,
) -> int:
    if (
        not isinstance(
            value,
            int,
        )
        or isinstance(
            value,
            bool,
        )
        or value < minimum
    ):
        raise LabPromotionTransactionStateError(
            f"{name} must be an integer >= {minimum}"
        )

    return value


def _require_hex_identifier(
    name: str,
    value: object,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionTransactionStateError(
            f"{name} must be a string"
        )

    if (
        len(value) != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabPromotionTransactionStateError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _require_plain_mode(
    name: str,
    value: object,
) -> int:
    if (
        not isinstance(
            value,
            int,
        )
        or isinstance(
            value,
            bool,
        )
        or value < 0
        or value > 0o777
    ):
        raise LabPromotionTransactionStateError(
            f"{name} must be an integer from 0000 through 0777"
        )

    return value


def _require_sha256(
    name: str,
    value: object,
) -> str:
    return _require_hex_identifier(
        name,
        value,
    )


def _require_git_head(
    value: object,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionTransactionStateError(
            "head must be a string"
        )

    if (
        len(value)
        not in {
            40,
            64,
        }
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabPromotionTransactionStateError(
            "head must be a lowercase 40- or 64-character Git object ID"
        )

    return value


def _require_repository_path(
    value: object,
) -> str:
    path = _require_text(
        "repository_path",
        value,
    )

    if not path.startswith(
        "/"
    ):
        raise LabPromotionTransactionStateError(
            "repository_path must be absolute"
        )

    components = path.split(
        "/"
    )

    if (
        path != "/"
        and (
            ""
            in components[
                1:
            ]
            or "."
            in components
            or ".."
            in components
        )
    ):
        raise LabPromotionTransactionStateError(
            "repository_path must be normalized"
        )

    return path


def _require_relative_path(
    value: object,
) -> str:
    path = _require_text(
        "file path",
        value,
    )

    if (
        path.startswith(
            "/"
        )
        or "\\"
        in path
    ):
        raise LabPromotionTransactionStateError(
            f"file path is not canonical: {path!r}"
        )

    parts = path.split(
        "/"
    )

    if any(
        part
        in {
            "",
            ".",
            "..",
        }
        for part in parts
    ):
        raise LabPromotionTransactionStateError(
            f"file path is not canonical: {path!r}"
        )

    return path


def _validate_plan_file(
    item: LabPromotionProposalFile,
) -> LabPromotionProposalFile:
    if not isinstance(
        item,
        LabPromotionProposalFile,
    ):
        raise LabPromotionTransactionStateError(
            "proposal files must contain LabPromotionProposalFile values"
        )

    if item.operation not in {
        PROMOTION_OPERATION_ADD,
        PROMOTION_OPERATION_MODIFY,
    }:
        raise LabPromotionTransactionStateError(
            f"unsupported promotion operation {item.operation!r}"
        )

    path = _require_relative_path(
        item.path
    )

    if not isinstance(
        item.before_exists,
        bool,
    ):
        raise LabPromotionTransactionStateError(
            f"before_exists must be boolean for {path!r}"
        )

    if item.operation == PROMOTION_OPERATION_ADD:
        if item.before_exists:
            raise LabPromotionTransactionStateError(
                f"ADD requires absent before-state for {path!r}"
            )

        if (
            item.before_bytes is not None
            or item.before_sha256 is not None
            or item.before_mode is not None
        ):
            raise LabPromotionTransactionStateError(
                f"ADD before-state metadata must be null for {path!r}"
            )

        after_mode = _require_plain_mode(
            "after_mode",
            item.after_mode,
        )

        if after_mode != 0o644:
            raise LabPromotionTransactionStateError(
                f"ADD after_mode must be 0644 for {path!r}"
            )

    else:
        if not item.before_exists:
            raise LabPromotionTransactionStateError(
                f"MODIFY requires existing before-state for {path!r}"
            )

        _require_integer(
            "before_bytes",
            item.before_bytes,
            minimum=0,
        )

        _require_sha256(
            "before_sha256",
            item.before_sha256,
        )

        before_mode = _require_plain_mode(
            "before_mode",
            item.before_mode,
        )

        after_mode = _require_plain_mode(
            "after_mode",
            item.after_mode,
        )

        if after_mode != before_mode:
            raise LabPromotionTransactionStateError(
                f"MODIFY after_mode must preserve before_mode for {path!r}"
            )

    after_bytes = _require_integer(
        "after_bytes",
        item.after_bytes,
        minimum=0,
    )

    after_sha256 = _require_sha256(
        "after_sha256",
        item.after_sha256,
    )

    after_content = _require_text(
        "after_content",
        item.after_content,
        allow_empty=True,
    )

    encoded = after_content.encode(
        "utf-8"
    )

    if len(
        encoded
    ) != after_bytes:
        raise LabPromotionTransactionStateError(
            f"after byte count mismatch for {path!r}"
        )

    actual_after_sha256 = hashlib.sha256(
        encoded
    ).hexdigest()

    if actual_after_sha256 != after_sha256:
        raise LabPromotionTransactionStateError(
            f"after SHA-256 mismatch for {path!r}"
        )

    if (
        not isinstance(item.source_kind, str)
        or item.source_kind not in ALLOWED_SOURCE_KINDS
    ):
        raise LabPromotionTransactionStateError(
            "source_kind must be one of the exact allowed values"
        )

    _require_hex_identifier(
        "source_id",
        item.source_id,
    )

    return item


def _validated_proposal(
    proposal: LabPromotionProposal,
) -> LabPromotionProposal:
    if not isinstance(
        proposal,
        LabPromotionProposal,
    ):
        raise TypeError(
            "proposal must be a LabPromotionProposal"
        )

    if proposal.component != PROMOTION_PROPOSAL_COMPONENT:
        raise LabPromotionTransactionStateError(
            "proposal component mismatch"
        )

    if (
        proposal.schema_version
        != PROMOTION_PROPOSAL_SCHEMA_VERSION
    ):
        raise LabPromotionTransactionStateError(
            "proposal schema version mismatch"
        )

    candidate_id = _require_hex_identifier(
        "candidate_id",
        proposal.candidate_id,
    )

    repository_path = _require_repository_path(
        proposal.repository_path
    )

    repository_device = _require_integer(
        "repository_device",
        proposal.repository_device,
        minimum=0,
    )

    repository_inode = _require_integer(
        "repository_inode",
        proposal.repository_inode,
        minimum=1,
    )

    branch = _require_text(
        "branch",
        proposal.branch,
    )

    head = _require_git_head(
        proposal.head
    )

    proposal_id = _require_hex_identifier(
        "proposal_id",
        proposal.proposal_id,
    )

    if not isinstance(
        proposal.files,
        tuple,
    ):
        raise LabPromotionTransactionStateError(
            "proposal files must be a tuple"
        )

    if not proposal.files:
        raise LabPromotionTransactionStateError(
            "proposal must contain at least one file"
        )

    validated_files = tuple(
        _validate_plan_file(
            item
        )
        for item in proposal.files
    )

    paths = [
        item.path
        for item in validated_files
    ]

    if paths != sorted(
        paths
    ):
        raise LabPromotionTransactionStateError(
            "proposal files must be sorted by canonical path"
        )

    if len(
        paths
    ) != len(
        set(
            paths
        )
    ):
        raise LabPromotionTransactionStateError(
            "proposal contains duplicate paths"
        )

    proposal_object = _proposal_identity_object(
        candidate_id=candidate_id,
        repository_path=repository_path,
        repository_device=repository_device,
        repository_inode=repository_inode,
        branch=branch,
        head=head,
        files=validated_files,
    )

    expected_proposal_id = hashlib.sha256(
        _PROPOSAL_ID_DOMAIN
        + _proposal_canonical_json_bytes(
            proposal_object
        )
    ).hexdigest()

    if expected_proposal_id != proposal_id:
        raise LabPromotionTransactionStateError(
            "proposal identity does not match exact proposal evidence"
        )

    return proposal


def _proposal_file_from_transaction_file(
    item: LabPromotionTransactionFile,
) -> LabPromotionProposalFile:
    return LabPromotionProposalFile(
        operation=item.operation,
        path=item.path,
        before_exists=item.before_exists,
        before_bytes=item.before_bytes,
        before_sha256=item.before_sha256,
        before_mode=item.before_mode,
        after_bytes=item.after_bytes,
        after_sha256=item.after_sha256,
        after_mode=item.after_mode,
        after_content=item.after_content,
        source_kind=item.source_kind,
        source_id=(
            item.source_id
        ),
    )


def _transaction_identity_object(
    *,
    proposal_id: str,
    candidate_id: str,
    run_id: str,
    repository_path: str,
    repository_device: int,
    repository_inode: int,
    branch: str,
    head: str,
    risk_classification: str,
    files: tuple[
        LabPromotionTransactionFile,
        ...,
    ],
) -> dict[str, object]:
    return {
        "component": TRANSACTION_COMPONENT,
        "schema_version": (
            TRANSACTION_SCHEMA_VERSION
        ),
        "proposal_id": proposal_id,
        "candidate_id": candidate_id,
        "run_id": run_id,
        "repository_path": repository_path,
        "repository_device": repository_device,
        "repository_inode": repository_inode,
        "branch": branch,
        "head": head,
        "risk_classification": risk_classification,
        "files": [
            {
                "operation": item.operation,
                "path": item.path,
                "before_exists": item.before_exists,
                "before_bytes": item.before_bytes,
                "before_sha256": item.before_sha256,
                "before_mode": item.before_mode,
                "after_bytes": item.after_bytes,
                "after_sha256": item.after_sha256,
                "after_mode": item.after_mode,
                "after_content": item.after_content,
                "source_kind": item.source_kind,
                "source_id": (
                    item.source_id
                ),
            }
            for item in files
        ],
    }


def _snapshot_identity_object(
    *,
    transaction_id: str,
    state: str,
    files: tuple[
        LabPromotionTransactionFile,
        ...,
    ],
) -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "state": state,
        "file_progress": [
            {
                "path": item.path,
                "progress": item.progress,
            }
            for item in files
        ],
    }


def _derive_risk(
    files: tuple[
        LabPromotionTransactionFile,
        ...,
    ],
) -> str:
    if any(
        item.operation
        == PROMOTION_OPERATION_ADD
        for item in files
    ):
        return RISK_DESTRUCTIVE_HIGH

    return RISK_WRITE


def _validate_state_invariants(
    state: str,
    files: tuple[
        LabPromotionTransactionFile,
        ...,
    ],
) -> None:
    if state not in TRANSACTION_STATES:
        raise LabPromotionTransactionStateError(
            f"unknown transaction state {state!r}"
        )

    progress = tuple(
        item.progress
        for item in files
    )

    for value in progress:
        if value not in FILE_PROGRESS_STATES:
            raise LabPromotionTransactionStateError(
                f"unknown file progress {value!r}"
            )

    if state == STATE_PREPARED:
        if any(
            value != PROGRESS_PENDING
            for value in progress
        ):
            raise LabPromotionTransactionStateError(
                "PREPARED requires every file to be PENDING"
            )

    elif state == STATE_APPLYING:
        if any(
            value
            not in {
                PROGRESS_PENDING,
                PROGRESS_INSTALLED,
            }
            for value in progress
        ):
            raise LabPromotionTransactionStateError(
                "APPLYING permits only PENDING or INSTALLED files"
            )

    elif state == STATE_VERIFYING:
        if any(
            value
            not in {
                PROGRESS_INSTALLED,
                PROGRESS_VERIFIED,
            }
            for value in progress
        ):
            raise LabPromotionTransactionStateError(
                "VERIFYING requires every file to have been installed"
            )

    elif state == STATE_COMPLETED:
        if any(
            value != PROGRESS_VERIFIED
            for value in progress
        ):
            raise LabPromotionTransactionStateError(
                "COMPLETED requires every file to be VERIFIED"
            )

    elif state == STATE_ROLLING_BACK:
        pass

    elif state == STATE_ROLLED_BACK:
        if any(
            value
            not in {
                PROGRESS_PENDING,
                PROGRESS_RESTORED,
            }
            for value in progress
        ):
            raise LabPromotionTransactionStateError(
                "ROLLED_BACK requires changed files RESTORED "
                "and untouched files PENDING"
            )


def _validate_transaction_file(
    item: LabPromotionTransactionFile,
) -> LabPromotionTransactionFile:
    if not isinstance(
        item,
        LabPromotionTransactionFile,
    ):
        raise LabPromotionTransactionStateError(
            "transaction files must contain "
            "LabPromotionTransactionFile values"
        )

    proposal_file = _proposal_file_from_transaction_file(
        item
    )

    _validate_plan_file(
        proposal_file
    )

    if item.progress not in FILE_PROGRESS_STATES:
        raise LabPromotionTransactionStateError(
            f"unknown file progress {item.progress!r}"
        )

    return item


def validate_lab_promotion_transaction(
    transaction: LabPromotionTransaction,
) -> LabPromotionTransaction:
    """Validate an immutable transaction snapshot without external access."""
    if not isinstance(
        transaction,
        LabPromotionTransaction,
    ):
        raise TypeError(
            "transaction must be a LabPromotionTransaction"
        )

    if transaction.component != TRANSACTION_COMPONENT:
        raise LabPromotionTransactionStateError(
            "transaction component mismatch"
        )

    if (
        transaction.schema_version
        != TRANSACTION_SCHEMA_VERSION
    ):
        raise LabPromotionTransactionStateError(
            "transaction schema version mismatch"
        )

    transaction_id = _require_hex_identifier(
        "transaction_id",
        transaction.transaction_id,
    )

    snapshot_id = _require_hex_identifier(
        "snapshot_id",
        transaction.snapshot_id,
    )

    proposal_id = _require_hex_identifier(
        "proposal_id",
        transaction.proposal_id,
    )

    candidate_id = _require_hex_identifier(
        "candidate_id",
        transaction.candidate_id,
    )

    run_id = _require_hex_identifier(
        "run_id",
        transaction.run_id,
    )

    repository_path = _require_repository_path(
        transaction.repository_path
    )

    repository_device = _require_integer(
        "repository_device",
        transaction.repository_device,
        minimum=0,
    )

    repository_inode = _require_integer(
        "repository_inode",
        transaction.repository_inode,
        minimum=1,
    )

    branch = _require_text(
        "branch",
        transaction.branch,
    )

    head = _require_git_head(
        transaction.head
    )

    if not isinstance(
        transaction.files,
        tuple,
    ):
        raise LabPromotionTransactionStateError(
            "transaction files must be a tuple"
        )

    if not transaction.files:
        raise LabPromotionTransactionStateError(
            "transaction must contain at least one file"
        )

    files = tuple(
        _validate_transaction_file(
            item
        )
        for item in transaction.files
    )

    paths = [
        item.path
        for item in files
    ]

    if paths != sorted(
        paths
    ):
        raise LabPromotionTransactionStateError(
            "transaction files must be sorted by canonical path"
        )

    if len(
        paths
    ) != len(
        set(
            paths
        )
    ):
        raise LabPromotionTransactionStateError(
            "transaction contains duplicate paths"
        )

    risk = _derive_risk(
        files
    )

    if transaction.risk_classification != risk:
        raise LabPromotionTransactionStateError(
            "risk classification does not match transaction operations"
        )

    proposal_files = tuple(
        _proposal_file_from_transaction_file(
            item
        )
        for item in files
    )

    proposal_object = _proposal_identity_object(
        candidate_id=candidate_id,
        repository_path=repository_path,
        repository_device=repository_device,
        repository_inode=repository_inode,
        branch=branch,
        head=head,
        files=proposal_files,
    )

    expected_proposal_id = hashlib.sha256(
        _PROPOSAL_ID_DOMAIN
        + _proposal_canonical_json_bytes(
            proposal_object
        )
    ).hexdigest()

    if expected_proposal_id != proposal_id:
        raise LabPromotionTransactionStateError(
            "transaction proposal binding does not match exact file evidence"
        )

    _validate_state_invariants(
        transaction.state,
        files,
    )

    transaction_object = _transaction_identity_object(
        proposal_id=proposal_id,
        candidate_id=candidate_id,
        run_id=run_id,
        repository_path=repository_path,
        repository_device=repository_device,
        repository_inode=repository_inode,
        branch=branch,
        head=head,
        risk_classification=risk,
        files=files,
    )

    expected_transaction_id = hashlib.sha256(
        _TRANSACTION_ID_DOMAIN
        + _canonical_json_bytes(
            transaction_object
        )
    ).hexdigest()

    if expected_transaction_id != transaction_id:
        raise LabPromotionTransactionStateError(
            "transaction_id does not match exact transaction plan"
        )

    snapshot_object = _snapshot_identity_object(
        transaction_id=transaction_id,
        state=transaction.state,
        files=files,
    )

    expected_snapshot_id = hashlib.sha256(
        _SNAPSHOT_ID_DOMAIN
        + _canonical_json_bytes(
            snapshot_object
        )
    ).hexdigest()

    if expected_snapshot_id != snapshot_id:
        raise LabPromotionTransactionStateError(
            "snapshot_id does not match exact transaction state"
        )

    return transaction


def build_lab_promotion_transaction(
    *,
    proposal: LabPromotionProposal,
    run_id: str,
) -> LabPromotionTransaction:
    """Build deterministic PREPARED transaction evidence."""
    proposal = _validated_proposal(
        proposal
    )

    run_id = _require_hex_identifier(
        "run_id",
        run_id,
    )

    files = tuple(
        LabPromotionTransactionFile(
            operation=item.operation,
            path=item.path,
            before_exists=item.before_exists,
            before_bytes=item.before_bytes,
            before_sha256=item.before_sha256,
            before_mode=item.before_mode,
            after_bytes=item.after_bytes,
            after_sha256=item.after_sha256,
            after_mode=item.after_mode,
            after_content=item.after_content,
            source_kind=item.source_kind,
            source_id=(
                item.source_id
            ),
            progress=PROGRESS_PENDING,
        )
        for item in proposal.files
    )

    risk = _derive_risk(
        files
    )

    transaction_object = _transaction_identity_object(
        proposal_id=proposal.proposal_id,
        candidate_id=proposal.candidate_id,
        run_id=run_id,
        repository_path=proposal.repository_path,
        repository_device=proposal.repository_device,
        repository_inode=proposal.repository_inode,
        branch=proposal.branch,
        head=proposal.head,
        risk_classification=risk,
        files=files,
    )

    transaction_id = hashlib.sha256(
        _TRANSACTION_ID_DOMAIN
        + _canonical_json_bytes(
            transaction_object
        )
    ).hexdigest()

    snapshot_object = _snapshot_identity_object(
        transaction_id=transaction_id,
        state=STATE_PREPARED,
        files=files,
    )

    snapshot_id = hashlib.sha256(
        _SNAPSHOT_ID_DOMAIN
        + _canonical_json_bytes(
            snapshot_object
        )
    ).hexdigest()

    transaction = LabPromotionTransaction(
        component=TRANSACTION_COMPONENT,
        schema_version=TRANSACTION_SCHEMA_VERSION,
        transaction_id=transaction_id,
        snapshot_id=snapshot_id,
        proposal_id=proposal.proposal_id,
        candidate_id=proposal.candidate_id,
        run_id=run_id,
        repository_path=proposal.repository_path,
        repository_device=proposal.repository_device,
        repository_inode=proposal.repository_inode,
        branch=proposal.branch,
        head=proposal.head,
        risk_classification=risk,
        state=STATE_PREPARED,
        files=files,
    )

    return validate_lab_promotion_transaction(
        transaction
    )


def transition_lab_promotion_transaction(
    transaction: LabPromotionTransaction,
    *,
    state: str,
    progress_by_path: Mapping[
        str,
        str,
    ],
) -> LabPromotionTransaction:
    """Create one validated immutable successor snapshot."""
    current = validate_lab_promotion_transaction(
        transaction
    )

    if state not in TRANSACTION_STATES:
        raise LabPromotionTransactionStateError(
            f"unknown successor state {state!r}"
        )

    allowed_states = _ALLOWED_STATE_TRANSITIONS[
        current.state
    ]

    if state not in allowed_states:
        raise LabPromotionTransactionStateError(
            f"illegal transaction transition "
            f"{current.state} -> {state}"
        )

    if not isinstance(
        progress_by_path,
        Mapping,
    ):
        raise TypeError(
            "progress_by_path must be a mapping"
        )

    expected_paths = {
        item.path
        for item in current.files
    }

    supplied_paths = set(
        progress_by_path
    )

    if supplied_paths != expected_paths:
        missing = sorted(
            expected_paths
            - supplied_paths
        )

        extra = sorted(
            supplied_paths
            - expected_paths
        )

        raise LabPromotionTransactionStateError(
            "progress paths must exactly match transaction paths; "
            f"missing={missing!r}, extra={extra!r}"
        )

    successor_files: list[
        LabPromotionTransactionFile
    ] = []

    for item in current.files:
        new_progress = progress_by_path[
            item.path
        ]

        if new_progress not in FILE_PROGRESS_STATES:
            raise LabPromotionTransactionStateError(
                f"unknown file progress {new_progress!r}"
            )

        if (
            new_progress != item.progress
            and (
                item.progress,
                new_progress,
            )
            not in _ALLOWED_PROGRESS_TRANSITIONS
        ):
            raise LabPromotionTransactionStateError(
                f"illegal file progress transition for {item.path!r}: "
                f"{item.progress} -> {new_progress}"
            )

        successor_files.append(
            LabPromotionTransactionFile(
                operation=item.operation,
                path=item.path,
                before_exists=item.before_exists,
                before_bytes=item.before_bytes,
                before_sha256=item.before_sha256,
                before_mode=item.before_mode,
                after_bytes=item.after_bytes,
                after_sha256=item.after_sha256,
                after_mode=item.after_mode,
                after_content=item.after_content,
                source_kind=item.source_kind,
                source_id=(
                    item.source_id
                ),
                progress=new_progress,
            )
        )

    files = tuple(
        successor_files
    )

    _validate_state_invariants(
        state,
        files,
    )

    snapshot_object = _snapshot_identity_object(
        transaction_id=current.transaction_id,
        state=state,
        files=files,
    )

    snapshot_id = hashlib.sha256(
        _SNAPSHOT_ID_DOMAIN
        + _canonical_json_bytes(
            snapshot_object
        )
    ).hexdigest()

    successor = LabPromotionTransaction(
        component=current.component,
        schema_version=current.schema_version,
        transaction_id=current.transaction_id,
        snapshot_id=snapshot_id,
        proposal_id=current.proposal_id,
        candidate_id=current.candidate_id,
        run_id=current.run_id,
        repository_path=current.repository_path,
        repository_device=current.repository_device,
        repository_inode=current.repository_inode,
        branch=current.branch,
        head=current.head,
        risk_classification=current.risk_classification,
        state=state,
        files=files,
    )

    return validate_lab_promotion_transaction(
        successor
    )
