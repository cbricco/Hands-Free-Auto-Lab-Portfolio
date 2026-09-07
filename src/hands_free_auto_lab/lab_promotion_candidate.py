"""Build a reviewable promotion candidate from proven Auto Lab evidence.

This module has read-only authority over one validated disposable workspace.

It does not:

- write outside the lab workspace,
- accept a destination repository,
- modify a real repository,
- apply patches,
- stage, commit, or push Git state,
- delete a workspace,
- execute model-selected commands,
- authorize promotion.

A promotion candidate is evidence for later human review, not permission to
cross the lab boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .lab_action import build_lab_action
from .lab_read_file import (
    LabReadFileError,
    execute_lab_read_file,
)
from .lab_session import LabSessionRecord
from .lab_write_file import LabWriteFileResult


PROMOTION_CANDIDATE_COMPONENT = (
    "hands-free-auto-lab-promotion-candidate-v1"
)
PROMOTION_CANDIDATE_SCHEMA_VERSION = 1


class LabPromotionCandidateError(RuntimeError):
    """Raised when trustworthy promotion evidence cannot be constructed."""


@dataclass(frozen=True, slots=True)
class LabPromotionCandidateFile:
    path: str
    bytes: int
    sha256: str
    content: str
    latest_write_action_id: str


@dataclass(frozen=True, slots=True)
class LabPromotionCandidate:
    component: str
    schema_version: int
    session_id: str
    workspace_device: int
    workspace_inode: int
    controller_status: str
    workspace_generation: int
    tested_generation: int
    acceptance_generation: int | None
    files: tuple[LabPromotionCandidateFile, ...]


def _latest_successful_writes(
    record: LabSessionRecord,
) -> dict[str, LabWriteFileResult]:
    latest: dict[
        str,
        LabWriteFileResult,
    ] = {}

    workspace = record.workspace

    for step in record.controller.steps:
        result = step.result

        if not isinstance(
            result,
            LabWriteFileResult,
        ):
            continue

        action = step.action

        if (
            step.decision != "action"
            or action is None
            or action.kind != "WRITE_FILE"
        ):
            raise LabPromotionCandidateError(
                "WRITE_FILE result is not attached to "
                "a successful WRITE_FILE action"
            )

        if result.action_id != action.action_id:
            raise LabPromotionCandidateError(
                "WRITE_FILE result action identity mismatch"
            )

        if (
            action.path is None
            or result.path != action.path
        ):
            raise LabPromotionCandidateError(
                "WRITE_FILE result path identity mismatch"
            )

        if (
            result.workspace_device
            != workspace.device
            or result.workspace_inode
            != workspace.inode
        ):
            raise LabPromotionCandidateError(
                "WRITE_FILE result workspace identity mismatch"
            )

        if action.content is None:
            raise LabPromotionCandidateError(
                "WRITE_FILE action content is unavailable"
            )

        raw = action.content.encode(
            "utf-8"
        )

        if result.bytes_written != len(raw):
            raise LabPromotionCandidateError(
                "WRITE_FILE result byte count does not match action"
            )

        digest = hashlib.sha256(
            raw
        ).hexdigest()

        if result.sha256 != digest:
            raise LabPromotionCandidateError(
                "WRITE_FILE result digest does not match action"
            )

        latest[
            result.path
        ] = result

    return latest


def build_lab_promotion_candidate(
    *,
    record: LabSessionRecord,
    workspace_parent: str,
) -> LabPromotionCandidate:
    """Securely reread final controller-written files for human review."""

    if not isinstance(
        record,
        LabSessionRecord,
    ):
        raise TypeError(
            "record must be a LabSessionRecord"
        )

    controller = record.controller

    if controller.status != "done":
        raise LabPromotionCandidateError(
            "session is not complete"
        )

    if (
        controller.tested_generation
        != controller.workspace_generation
    ):
        raise LabPromotionCandidateError(
            "completed session does not have a successful "
            "worker RUN for the current workspace generation"
        )

    if (
        controller.acceptance_generation
        is not None
        and controller.acceptance_generation
        != controller.workspace_generation
    ):
        raise LabPromotionCandidateError(
            "acceptance evidence is stale for the current "
            "workspace generation"
        )

    latest = _latest_successful_writes(
        record
    )

    if not latest:
        raise LabPromotionCandidateError(
            "completed session contains no files to promote"
        )

    files: list[
        LabPromotionCandidateFile
    ] = []

    for path in sorted(
        latest
    ):
        write_result = latest[
            path
        ]

        read_action = build_lab_action(
            kind="READ_FILE",
            path=path,
        )

        try:
            read_result = execute_lab_read_file(
                read_action,
                workspace_parent=workspace_parent,
                workspace=record.workspace,
            )
        except LabReadFileError as exc:
            raise LabPromotionCandidateError(
                "cannot securely reread final promotion "
                f"candidate file {path!r}: {exc}"
            ) from exc

        if (
            read_result.workspace_device
            != record.workspace.device
            or read_result.workspace_inode
            != record.workspace.inode
        ):
            raise LabPromotionCandidateError(
                "final file reread workspace identity mismatch"
            )

        if (
            read_result.bytes_read
            != write_result.bytes_written
        ):
            raise LabPromotionCandidateError(
                "final file byte count does not match "
                f"latest successful write for {path!r}"
            )

        if (
            read_result.sha256
            != write_result.sha256
        ):
            raise LabPromotionCandidateError(
                "final file digest does not match "
                f"latest successful write for {path!r}"
            )

        files.append(
            LabPromotionCandidateFile(
                path=path,
                bytes=read_result.bytes_read,
                sha256=read_result.sha256,
                content=read_result.content,
                latest_write_action_id=(
                    write_result.action_id
                ),
            )
        )

    tested_generation = (
        controller.tested_generation
    )

    assert tested_generation is not None

    return LabPromotionCandidate(
        component=PROMOTION_CANDIDATE_COMPONENT,
        schema_version=(
            PROMOTION_CANDIDATE_SCHEMA_VERSION
        ),
        session_id=record.workspace.session_id,
        workspace_device=record.workspace.device,
        workspace_inode=record.workspace.inode,
        controller_status=controller.status,
        workspace_generation=(
            controller.workspace_generation
        ),
        tested_generation=tested_generation,
        acceptance_generation=(
            controller.acceptance_generation
        ),
        files=tuple(
            files
        ),
    )
