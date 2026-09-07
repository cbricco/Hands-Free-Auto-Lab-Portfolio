"""Construct immutable, review-only promotion proposals.

This module is a pure evidence/model boundary.

It does not:

- open a destination repository,
- inspect destination files,
- write files,
- apply patches,
- execute commands,
- stage, commit, or push Git state,
- consume approval,
- authorize promotion.

Repository and before-state evidence supplied to this module must have been
obtained by a separate trusted READ-ONLY inspection component.

A proposal identity binds the exact candidate, repository context, before
states, and proposed after states. Changing any authority-relevant input
changes the proposal identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from typing import Iterable

from .lab_coding_candidate import LabCodingCandidate
from .lab_promotion_candidate import (
    PROMOTION_CANDIDATE_COMPONENT,
    PROMOTION_CANDIDATE_SCHEMA_VERSION,
    LabPromotionCandidate,
    LabPromotionCandidateFile,
)
from .lab_promotion_source import (
    SOURCE_KIND_CODING_CANDIDATE,
    LabPromotionSource,
    normalize_lab_promotion_source,
)


PROMOTION_PROPOSAL_COMPONENT = (
    "hands-free-auto-lab-promotion-proposal-v3"
)
PROMOTION_PROPOSAL_SCHEMA_VERSION = 3

PROMOTION_OPERATION_ADD = "ADD"
PROMOTION_OPERATION_MODIFY = "MODIFY"

_CANDIDATE_ID_DOMAIN = (
    b"hands-free-auto-lab-promotion-candidate-identity-v1\x00"
)

_PROPOSAL_ID_DOMAIN = (
    b"hands-free-auto-lab-promotion-proposal-identity-v3\x00"
)

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)

ALLOWED_SOURCE_KINDS = frozenset(
    {
        "legacy_write_action",
        "coding_candidate",
    }
)


class LabPromotionProposalError(ValueError):
    """Raised when immutable promotion evidence is malformed."""


@dataclass(frozen=True, slots=True)
class LabPromotionRepositoryFileState:
    path: str
    exists: bool
    bytes: int | None
    sha256: str | None
    mode: int | None


@dataclass(frozen=True, slots=True)
class LabPromotionProposalFile:
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


@dataclass(frozen=True, slots=True)
class LabPromotionProposal:
    component: str
    schema_version: int
    proposal_id: str
    candidate_id: str
    repository_path: str
    repository_device: int
    repository_inode: int
    branch: str
    head: str
    files: tuple[LabPromotionProposalFile, ...]


def _require_text(
    name: str,
    value: object,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionProposalError(
            f"{name} must be a string"
        )

    if not value:
        raise LabPromotionProposalError(
            f"{name} must not be empty"
        )

    try:
        value.encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise LabPromotionProposalError(
            f"{name} must be UTF-8 encodable"
        ) from exc

    if any(
        ord(character) < 32
        or ord(character) == 127
        for character in value
    ):
        raise LabPromotionProposalError(
            f"{name} contains a control character"
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
    ):
        raise LabPromotionProposalError(
            f"{name} must be an integer"
        )

    if value < minimum:
        raise LabPromotionProposalError(
            f"{name} must be >= {minimum}"
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
        raise LabPromotionProposalError(
            f"{name} must be an integer from 0000 through 0777"
        )

    if value & 0o111:
        raise LabPromotionProposalError(
            f"{name} must not be executable"
        )

    return value


def _require_sha256(
    name: str,
    value: object,
) -> str:
    text = _require_text(
        name,
        value,
    )

    if (
        len(text) != 64
        or any(
            character not in _HEX_LOWER
            for character in text
        )
    ):
        raise LabPromotionProposalError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )

    return text


def _require_git_head(
    value: object,
) -> str:
    head = _require_text(
        "head",
        value,
    )

    if (
        len(head) not in (
            40,
            64,
        )
        or any(
            character not in _HEX_LOWER
            for character in head
        )
    ):
        raise LabPromotionProposalError(
            "head must be a lowercase 40- or 64-character "
            "Git object ID"
        )

    return head


def _require_relative_path(
    value: object,
) -> str:
    path = _require_text(
        "path",
        value,
    )

    if path.startswith(
        "/"
    ):
        raise LabPromotionProposalError(
            "path must be relative"
        )

    if "\\" in path:
        raise LabPromotionProposalError(
            "path must use POSIX separators"
        )

    components = path.split(
        "/"
    )

    if any(
        component in (
            "",
            ".",
            "..",
        )
        for component in components
    ):
        raise LabPromotionProposalError(
            "path must be canonical and must not traverse parents"
        )

    return path


def _require_repository_path(
    value: object,
) -> str:
    path = _require_text(
        "repository_path",
        value,
    )

    if not os.path.isabs(
        path
    ):
        raise LabPromotionProposalError(
            "repository_path must be absolute"
        )

    if os.path.normpath(
        path
    ) != path:
        raise LabPromotionProposalError(
            "repository_path must be normalized"
        )

    return path


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


def _candidate_file_object(
    item: LabPromotionCandidateFile,
) -> dict[str, object]:
    if not isinstance(
        item,
        LabPromotionCandidateFile,
    ):
        raise LabPromotionProposalError(
            "candidate files must contain "
            "LabPromotionCandidateFile values"
        )

    path = _require_relative_path(
        item.path
    )

    byte_count = _require_integer(
        "candidate file bytes",
        item.bytes,
        minimum=0,
    )

    sha256 = _require_sha256(
        "candidate file sha256",
        item.sha256,
    )

    content = _require_text_allow_empty(
        "candidate file content",
        item.content,
    )

    source_id = _require_sha256(
        "candidate latest write action id",
        item.latest_write_action_id,
    )

    raw = content.encode(
        "utf-8"
    )

    if len(raw) != byte_count:
        raise LabPromotionProposalError(
            f"candidate file byte count mismatch for {path!r}"
        )

    digest = hashlib.sha256(
        raw
    ).hexdigest()

    if digest != sha256:
        raise LabPromotionProposalError(
            f"candidate file digest mismatch for {path!r}"
        )

    return {
        "path": path,
        "bytes": byte_count,
        "sha256": sha256,
        "content": content,
        "latest_write_action_id": (
            source_id
        ),
    }


def _require_text_allow_empty(
    name: str,
    value: object,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabPromotionProposalError(
            f"{name} must be a string"
        )

    try:
        value.encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise LabPromotionProposalError(
            f"{name} must be UTF-8 encodable"
        ) from exc

    if "\x00" in value:
        raise LabPromotionProposalError(
            f"{name} must not contain NUL"
        )

    return value


def _validated_candidate_object(
    candidate: LabPromotionCandidate,
) -> dict[str, object]:
    if not isinstance(
        candidate,
        LabPromotionCandidate,
    ):
        raise TypeError(
            "candidate must be a LabPromotionCandidate"
        )

    if (
        candidate.component
        != PROMOTION_CANDIDATE_COMPONENT
    ):
        raise LabPromotionProposalError(
            "candidate component mismatch"
        )

    if (
        candidate.schema_version
        != PROMOTION_CANDIDATE_SCHEMA_VERSION
    ):
        raise LabPromotionProposalError(
            "candidate schema version mismatch"
        )

    session_id = _require_sha256(
        "candidate session_id",
        candidate.session_id,
    )

    workspace_device = _require_integer(
        "candidate workspace_device",
        candidate.workspace_device,
        minimum=0,
    )

    workspace_inode = _require_integer(
        "candidate workspace_inode",
        candidate.workspace_inode,
        minimum=1,
    )

    if candidate.controller_status != "done":
        raise LabPromotionProposalError(
            "candidate controller status is not done"
        )

    workspace_generation = _require_integer(
        "candidate workspace_generation",
        candidate.workspace_generation,
        minimum=1,
    )

    tested_generation = _require_integer(
        "candidate tested_generation",
        candidate.tested_generation,
        minimum=1,
    )

    if (
        tested_generation
        != workspace_generation
    ):
        raise LabPromotionProposalError(
            "candidate tested generation is stale"
        )

    acceptance_generation = (
        candidate.acceptance_generation
    )

    if acceptance_generation is not None:
        acceptance_generation = _require_integer(
            "candidate acceptance_generation",
            acceptance_generation,
            minimum=1,
        )

        if (
            acceptance_generation
            != workspace_generation
        ):
            raise LabPromotionProposalError(
                "candidate acceptance generation is stale"
            )

    files_by_path: dict[
        str,
        dict[str, object],
    ] = {}

    for item in candidate.files:
        file_object = _candidate_file_object(
            item
        )

        path = str(
            file_object["path"]
        )

        if path in files_by_path:
            raise LabPromotionProposalError(
                f"duplicate candidate path {path!r}"
            )

        files_by_path[
            path
        ] = file_object

    if not files_by_path:
        raise LabPromotionProposalError(
            "candidate must contain at least one file"
        )

    return {
        "component": candidate.component,
        "schema_version": candidate.schema_version,
        "session_id": session_id,
        "workspace_device": workspace_device,
        "workspace_inode": workspace_inode,
        "controller_status": (
            candidate.controller_status
        ),
        "workspace_generation": (
            workspace_generation
        ),
        "tested_generation": tested_generation,
        "acceptance_generation": (
            acceptance_generation
        ),
        "files": [
            files_by_path[
                path
            ]
            for path in sorted(
                files_by_path
            )
        ],
    }


def lab_promotion_candidate_identity(
    candidate: LabPromotionCandidate,
) -> str:
    """Return a deterministic identity for exact candidate evidence."""
    candidate_object = _validated_candidate_object(
        candidate
    )

    return hashlib.sha256(
        _CANDIDATE_ID_DOMAIN
        + _canonical_json_bytes(
            candidate_object
        )
    ).hexdigest()


def _validated_before_state(
    state: LabPromotionRepositoryFileState,
) -> LabPromotionRepositoryFileState:
    if not isinstance(
        state,
        LabPromotionRepositoryFileState,
    ):
        raise LabPromotionProposalError(
            "before_states must contain "
            "LabPromotionRepositoryFileState values"
        )

    path = _require_relative_path(
        state.path
    )

    if not isinstance(
        state.exists,
        bool,
    ):
        raise LabPromotionProposalError(
            f"before-state exists must be boolean for {path!r}"
        )

    if state.exists:
        byte_count = _require_integer(
            "before-state bytes",
            state.bytes,
            minimum=0,
        )

        sha256 = _require_sha256(
            "before-state sha256",
            state.sha256,
        )

        mode = _require_plain_mode(
            "before-state mode",
            state.mode,
        )

        return LabPromotionRepositoryFileState(
            path=path,
            exists=True,
            bytes=byte_count,
            sha256=sha256,
            mode=mode,
        )

    if (
        state.bytes is not None
        or state.sha256 is not None
        or state.mode is not None
    ):
        raise LabPromotionProposalError(
            "absent before-state must not contain "
            f"bytes, sha256, or mode for {path!r}"
        )

    return LabPromotionRepositoryFileState(
        path=path,
        exists=False,
        bytes=None,
        sha256=None,
        mode=None,
    )


def _proposal_identity_object(
    *,
    candidate_id: str,
    repository_path: str,
    repository_device: int,
    repository_inode: int,
    branch: str,
    head: str,
    files: tuple[LabPromotionProposalFile, ...],
) -> dict[str, object]:
    return {
        "component": PROMOTION_PROPOSAL_COMPONENT,
        "schema_version": (
            PROMOTION_PROPOSAL_SCHEMA_VERSION
        ),
        "candidate_id": candidate_id,
        "repository_path": repository_path,
        "repository_device": repository_device,
        "repository_inode": repository_inode,
        "branch": branch,
        "head": head,
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


def _build_lab_promotion_proposal_from_source(
    *,
    source: LabPromotionSource,
    candidate_id: str,
    repository_path: str,
    repository_device: int,
    repository_inode: int,
    branch: str,
    head: str,
    before_states: Iterable[
        LabPromotionRepositoryFileState
    ],
) -> LabPromotionProposal:
    """Build a proposal from already normalized, validated evidence."""
    candidate_id = _require_sha256(
        "candidate_id",
        candidate_id,
    )

    repository_path = _require_repository_path(
        repository_path
    )
    repository_device = _require_integer(
        "repository_device",
        repository_device,
        minimum=0,
    )
    repository_inode = _require_integer(
        "repository_inode",
        repository_inode,
        minimum=1,
    )
    branch = _require_text(
        "branch",
        branch,
    )
    head = _require_git_head(
        head
    )

    states_by_path: dict[str, LabPromotionRepositoryFileState] = {}
    for raw_state in before_states:
        state = _validated_before_state(
            raw_state
        )
        if state.path in states_by_path:
            raise LabPromotionProposalError(
                f"duplicate before-state path {state.path!r}"
            )
        states_by_path[state.path] = state

    source_files = {item.path: item for item in source.files}
    candidate_paths = set(source_files)
    state_paths = set(states_by_path)
    if state_paths != candidate_paths:
        missing = sorted(candidate_paths - state_paths)
        extra = sorted(state_paths - candidate_paths)
        raise LabPromotionProposalError(
            "before-state paths must exactly match candidate paths; "
            f"missing={missing!r}, extra={extra!r}"
        )

    files: list[LabPromotionProposalFile] = []
    for path in sorted(source_files):
        source_file = source_files[path]
        before = states_by_path[path]
        operation = (
            PROMOTION_OPERATION_MODIFY
            if before.exists
            else PROMOTION_OPERATION_ADD
        )

        if source_file.source_kind == SOURCE_KIND_CODING_CANDIDATE:
            expected_source_operation = (
                "MODIFIED" if before.exists else "ADDED"
            )
            if source_file.operation != expected_source_operation:
                raise LabPromotionProposalError(
                    f"coding candidate operation does not match "
                    f"before-state for {path!r}"
                )
            source_before = (
                source_file.before_bytes,
                source_file.before_sha256,
                source_file.before_mode,
            )
            repository_before = (
                before.bytes,
                before.sha256,
                before.mode,
            )
            if source_before != repository_before:
                raise LabPromotionProposalError(
                    f"coding candidate before evidence does not match "
                    f"repository before-state for {path!r}"
                )
            if before.exists:
                after_mode = before.mode
            else:
                after_mode = 0o644
        else:
            after_mode = before.mode if before.exists else 0o644

        if after_mode is None:
            raise LabPromotionProposalError(
                f"final mode is unavailable for {path!r}"
            )

        files.append(LabPromotionProposalFile(
            operation=operation,
            path=path,
            before_exists=before.exists,
            before_bytes=before.bytes,
            before_sha256=before.sha256,
            before_mode=before.mode,
            after_bytes=source_file.final_bytes,
            after_sha256=source_file.final_sha256,
            after_mode=after_mode,
            after_content=source_file.final_content,
            source_kind=source_file.source_kind,
            source_id=source_file.source_id,
        ))

    frozen_files = tuple(files)
    proposal_object = _proposal_identity_object(
        candidate_id=candidate_id,
        repository_path=repository_path,
        repository_device=repository_device,
        repository_inode=repository_inode,
        branch=branch,
        head=head,
        files=frozen_files,
    )
    proposal_id = hashlib.sha256(
        _PROPOSAL_ID_DOMAIN
        + _canonical_json_bytes(proposal_object)
    ).hexdigest()

    return LabPromotionProposal(
        component=PROMOTION_PROPOSAL_COMPONENT,
        schema_version=PROMOTION_PROPOSAL_SCHEMA_VERSION,
        proposal_id=proposal_id,
        candidate_id=candidate_id,
        repository_path=repository_path,
        repository_device=repository_device,
        repository_inode=repository_inode,
        branch=branch,
        head=head,
        files=frozen_files,
    )


def build_lab_promotion_proposal(
    *,
    candidate: LabPromotionCandidate,
    repository_path: str,
    repository_device: int,
    repository_inode: int,
    branch: str,
    head: str,
    before_states: Iterable[
        LabPromotionRepositoryFileState
    ],
) -> LabPromotionProposal:
    """Build legacy immutable promotion evidence without filesystem access."""
    candidate_object = _validated_candidate_object(
        candidate
    )
    candidate_id = hashlib.sha256(
        _CANDIDATE_ID_DOMAIN
        + _canonical_json_bytes(candidate_object)
    ).hexdigest()
    source = normalize_lab_promotion_source(
        candidate
    )
    return _build_lab_promotion_proposal_from_source(
        source=source,
        candidate_id=candidate_id,
        repository_path=repository_path,
        repository_device=repository_device,
        repository_inode=repository_inode,
        branch=branch,
        head=head,
        before_states=before_states,
    )


def build_lab_coding_candidate_promotion_proposal(
    *,
    candidate: LabCodingCandidate,
    repository_path: str,
    repository_device: int,
    repository_inode: int,
    branch: str,
    head: str,
    before_states: Iterable[
        LabPromotionRepositoryFileState
    ],
) -> LabPromotionProposal:
    """Build review-only proposal evidence from a validated coding candidate."""
    if type(candidate) is not LabCodingCandidate:
        raise TypeError(
            "candidate must be a LabCodingCandidate"
        )
    source = normalize_lab_promotion_source(
        candidate
    )
    if any(
        item.source_kind != SOURCE_KIND_CODING_CANDIDATE
        or item.source_id != candidate.candidate_id
        for item in source.files
    ):
        raise LabPromotionProposalError(
            "coding candidate provenance mismatch"
        )
    return _build_lab_promotion_proposal_from_source(
        source=source,
        candidate_id=candidate.candidate_id,
        repository_path=repository_path,
        repository_device=repository_device,
        repository_inode=repository_inode,
        branch=branch,
        head=head,
        before_states=before_states,
    )
