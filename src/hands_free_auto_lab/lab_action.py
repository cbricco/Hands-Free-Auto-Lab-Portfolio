"""Deterministic structured actions for Hands-Free Auto Lab.

This module describes actions only. It never executes them.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


ACTION_SCHEMA_VERSION = 1

ACTION_KINDS = frozenset({
    "READ_FILE",
    "WRITE_FILE",
    "RUN",
})

MAX_CONTENT_BYTES = 256 * 1024
MAX_ARGUMENTS = 64
MAX_ARGUMENT_BYTES = 4096

_DOMAIN_SEPARATOR = b"hands-free-auto-lab-action-v1\x00"


class LabActionError(ValueError):
    """Raised when a lab action is structurally invalid."""


@dataclass(frozen=True, slots=True)
class LabAction:
    schema_version: int
    kind: str
    path: str | None
    cwd: str
    argv: tuple[str, ...]
    content: str | None
    action_id: str


def _validate_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise LabActionError(
            f"{name} must be a non-empty string"
        )

    if "\x00" in value:
        raise LabActionError(
            f"{name} must not contain NUL"
        )

    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LabActionError(
            f"{name} must be UTF-8 encodable"
        ) from exc

    if len(encoded) > MAX_ARGUMENT_BYTES:
        raise LabActionError(
            f"{name} exceeds maximum size"
        )

    return value


def _validate_content(value: object) -> str:
    if not isinstance(value, str):
        raise LabActionError(
            "content must be a string"
        )

    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LabActionError(
            "content must be UTF-8 encodable"
        ) from exc

    if len(encoded) > MAX_CONTENT_BYTES:
        raise LabActionError(
            "content exceeds maximum size"
        )

    return value


def _identity_object(action: LabAction) -> dict[str, Any]:
    return {
        "schema_version": action.schema_version,
        "kind": action.kind,
        "path": action.path,
        "cwd": action.cwd,
        "argv": list(action.argv),
        "content": action.content,
    }


def _action_id(action: LabAction) -> str:
    encoded = json.dumps(
        _identity_object(action),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(
        _DOMAIN_SEPARATOR + encoded
    ).hexdigest()


def validate_lab_action(action: LabAction) -> None:
    if not isinstance(action, LabAction):
        raise LabActionError(
            "action must be a LabAction"
        )

    if action.schema_version != ACTION_SCHEMA_VERSION:
        raise LabActionError(
            "unsupported action schema version"
        )

    if action.kind not in ACTION_KINDS:
        raise LabActionError(
            f"unsupported action kind: {action.kind!r}"
        )

    _validate_text("cwd", action.cwd)

    if not isinstance(action.argv, tuple):
        raise LabActionError(
            "argv must be a tuple"
        )

    if len(action.argv) > MAX_ARGUMENTS:
        raise LabActionError(
            "argv contains too many arguments"
        )

    for index, argument in enumerate(action.argv):
        _validate_text(
            f"argv[{index}]",
            argument,
        )

    if action.kind == "READ_FILE":
        _validate_text("path", action.path)

        if action.argv:
            raise LabActionError(
                "READ_FILE must not contain argv"
            )

        if action.content is not None:
            raise LabActionError(
                "READ_FILE must not contain content"
            )

    elif action.kind == "WRITE_FILE":
        _validate_text("path", action.path)

        if action.argv:
            raise LabActionError(
                "WRITE_FILE must not contain argv"
            )

        _validate_content(action.content)

    elif action.kind == "RUN":
        if action.path is not None:
            raise LabActionError(
                "RUN must not contain path"
            )

        if action.content is not None:
            raise LabActionError(
                "RUN must not contain content"
            )

        if not action.argv:
            raise LabActionError(
                "RUN requires argv"
            )

    if (
        not isinstance(action.action_id, str)
        or len(action.action_id) != 64
    ):
        raise LabActionError(
            "action_id must be a SHA-256 hex digest"
        )

    try:
        bytes.fromhex(action.action_id)
    except ValueError as exc:
        raise LabActionError(
            "action_id is not valid hexadecimal"
        ) from exc

    if action.action_id != _action_id(action):
        raise LabActionError(
            "action_id mismatch"
        )


def build_lab_action(
    *,
    kind: str,
    path: str | None = None,
    cwd: str = ".",
    argv: tuple[str, ...] = (),
    content: str | None = None,
) -> LabAction:
    provisional = LabAction(
        schema_version=ACTION_SCHEMA_VERSION,
        kind=kind,
        path=path,
        cwd=cwd,
        argv=argv,
        content=content,
        action_id="0" * 64,
    )

    action = LabAction(
        schema_version=provisional.schema_version,
        kind=provisional.kind,
        path=provisional.path,
        cwd=provisional.cwd,
        argv=provisional.argv,
        content=provisional.content,
        action_id=_action_id(provisional),
    )

    validate_lab_action(action)
    return action
