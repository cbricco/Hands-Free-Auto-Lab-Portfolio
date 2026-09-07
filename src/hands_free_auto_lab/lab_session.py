"""Record-only wrapper around one bounded Auto Lab controller session.

This module adds session-level evidence only.

It does not:

- grant new model authority,
- write audit records to persistent host storage,
- delete disposable workspaces,
- promote lab data into a real repository,
- stage, commit, or push Git state,
- start or configure model services.

The controller remains authoritative for lab actions and results.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .lab_controller import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_RUN_TIMEOUT_SECONDS,
    LabControllerResult,
    ModelRequester,
    run_auto_lab,
)
from .lab_model_adapter import request_model_decision
from .lab_workspace import LabWorkspace


SESSION_COMPONENT = "hands-free-auto-lab-session-v1"
SESSION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LabSessionRecord:
    """Complete record of one returned controller session."""

    component: str
    schema_version: int
    started_at_utc: str
    finished_at_utc: str
    workspace: LabWorkspace
    controller: LabControllerResult

    @property
    def succeeded(self) -> bool:
        return self.controller.succeeded


def _utc_timestamp() -> str:
    return (
        datetime.now(
            timezone.utc
        )
        .isoformat(
            timespec="microseconds"
        )
        .replace(
            "+00:00",
            "Z",
        )
    )


def _json_native(
    value: Any,
) -> Any:
    """Normalize dataclass output to JSON-native container types."""

    if isinstance(
        value,
        dict,
    ):
        return {
            key: _json_native(
                item
            )
            for key, item in value.items()
        }

    if isinstance(
        value,
        (
            tuple,
            list,
        ),
    ):
        return [
            _json_native(
                item
            )
            for item in value
        ]

    return value


def lab_session_record_to_wire(
    record: LabSessionRecord,
) -> dict[str, Any]:
    """Return a JSON-native structural representation."""

    if not isinstance(
        record,
        LabSessionRecord,
    ):
        raise TypeError(
            "record must be a LabSessionRecord"
        )

    wire = _json_native(
        asdict(
            record
        )
    )

    if not isinstance(
        wire,
        dict,
    ):
        raise AssertionError(
            "session wire representation must be a dictionary"
        )

    return wire


def run_lab_session(
    *,
    goal: object,
    model: object,
    workspace_parent: str,
    workspace: LabWorkspace,
    acceptance_test: object | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    run_timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS,
    model_timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS,
    requester: ModelRequester = request_model_decision,
) -> LabSessionRecord:
    """Run one controller session and retain its full returned evidence."""

    started_at_utc = _utc_timestamp()

    controller = run_auto_lab(
        goal=goal,
        model=model,
        workspace_parent=workspace_parent,
        workspace=workspace,
        acceptance_test=acceptance_test,
        max_iterations=max_iterations,
        run_timeout_seconds=run_timeout_seconds,
        model_timeout_seconds=model_timeout_seconds,
        requester=requester,
    )

    finished_at_utc = _utc_timestamp()

    return LabSessionRecord(
        component=SESSION_COMPONENT,
        schema_version=SESSION_SCHEMA_VERSION,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        workspace=workspace,
        controller=controller,
    )
