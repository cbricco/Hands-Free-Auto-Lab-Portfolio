"""Initial deterministic Auto Lab eligibility policy.

Policy decides eligibility only. It never executes an action.
"""

from __future__ import annotations

import posixpath
from typing import Any

from .lab_action import (
    LabAction,
    LabActionError,
    validate_lab_action,
)


LAB_POLICY = "auto-lab-mvp-v1"
PYTHON = "/usr/bin/python3"

RESERVED_ACCEPTANCE_PATH = "_hands_free_acceptance.py"
RESERVED_ACCEPTANCE_SANDBOX_PATH = (
    "/workspace/_hands_free_acceptance.py"
)


class LabPolicyError(ValueError):
    """Raised when an action is outside the enabled lab subset."""


def _relative_path(
    value: str,
    *,
    name: str,
    allow_dot: bool,
) -> str:
    if not isinstance(value, str) or not value:
        raise LabPolicyError(
            f"{name} must be a non-empty string"
        )

    if "\x00" in value:
        raise LabPolicyError(
            f"{name} must not contain NUL"
        )

    if value.startswith("/"):
        raise LabPolicyError(
            f"{name} must be relative to the lab workspace"
        )

    normalized = posixpath.normpath(value)

    if normalized != value:
        raise LabPolicyError(
            f"{name} must be canonical"
        )

    if normalized == ".":
        if allow_dot:
            return normalized

        raise LabPolicyError(
            f"{name} must identify a workspace entry"
        )

    if normalized == ".." or normalized.startswith("../"):
        raise LabPolicyError(
            f"{name} must remain inside the lab workspace"
        )

    return normalized


def _validate_unittest_run(action: LabAction) -> None:
    if action.argv == (
        PYTHON,
        "-I",
        RESERVED_ACCEPTANCE_SANDBOX_PATH,
    ):
        return

    if action.argv[0] != PYTHON:
        raise LabPolicyError(
            "RUN executable is not enabled"
        )

    if len(action.argv) < 3:
        raise LabPolicyError(
            "initial RUN policy permits only "
            "python3 -m unittest"
        )

    if action.argv[1:3] != ("-m", "unittest"):
        raise LabPolicyError(
            "initial RUN policy permits only "
            "python3 -m unittest"
        )


def evaluate_lab_policy(
    action: LabAction,
) -> dict[str, Any]:
    try:
        validate_lab_action(action)
    except LabActionError as exc:
        raise LabPolicyError(
            f"invalid lab action: {exc}"
        ) from exc

    cwd = _relative_path(
        action.cwd,
        name="cwd",
        allow_dot=True,
    )

    if action.kind in {
        "READ_FILE",
        "WRITE_FILE",
    }:
        assert action.path is not None

        path = _relative_path(
            action.path,
            name="path",
            allow_dot=False,
        )

        if (
            action.kind == "WRITE_FILE"
            and path == RESERVED_ACCEPTANCE_PATH
        ):
            raise LabPolicyError(
                "WRITE_FILE path is reserved for "
                "controller acceptance"
            )

        return {
            "policy": LAB_POLICY,
            "eligible": True,
            "action_id": action.action_id,
            "kind": action.kind,
            "cwd": cwd,
            "path": path,
        }

    if action.kind == "RUN":
        _validate_unittest_run(action)

        return {
            "policy": LAB_POLICY,
            "eligible": True,
            "action_id": action.action_id,
            "kind": action.kind,
            "cwd": cwd,
            "argv": list(action.argv),
        }

    raise LabPolicyError(
        "action kind is not enabled"
    )
