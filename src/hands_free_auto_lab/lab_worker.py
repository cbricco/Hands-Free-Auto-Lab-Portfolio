"""Backend-neutral coding-worker contract for Hands-Free Auto Lab.

This module describes one bounded worker request and the structured result
returned by a worker adapter.

It deliberately contains no execution, filesystem-validation, networking,
approval, promotion, Git, audit-storage, or system-change authority.

A worker result is evidence about what a worker reports. It is never authority
to promote changes. Auto Lab must independently inspect and validate the
physical disposable workspace before constructing promotion evidence.

V1 workspace capabilities deliberately contain only:

- WORKSPACE_READ
- WORKSPACE_WRITE
- RUN_PROCESS

There is no V1 capability for host filesystem access, promotion, approval,
credentials, package installation, system mutation, or network access.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
from typing import Protocol

from .lab_workspace import (
    WORKSPACE_COMPONENT,
    LabWorkspace,
)


WORKER_REQUEST_COMPONENT = "hands-free-auto-lab-worker-request-v1"
WORKER_REQUEST_SCHEMA_VERSION = 1

WORKER_RESULT_COMPONENT = "hands-free-auto-lab-worker-result-v1"
WORKER_RESULT_SCHEMA_VERSION = 1

CAPABILITY_WORKSPACE_READ = "WORKSPACE_READ"
CAPABILITY_WORKSPACE_WRITE = "WORKSPACE_WRITE"
CAPABILITY_RUN_PROCESS = "RUN_PROCESS"

WORKER_CAPABILITIES = frozenset(
    {
        CAPABILITY_WORKSPACE_READ,
        CAPABILITY_WORKSPACE_WRITE,
        CAPABILITY_RUN_PROCESS,
    }
)

STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_TIMED_OUT = "TIMED_OUT"

WORKER_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_TIMED_OUT,
    }
)

MAX_WORKER_NAME_BYTES = 256
MAX_GOAL_BYTES = 64 * 1024
MAX_SUMMARY_BYTES = 64 * 1024

MAX_RUNTIME_SECONDS = 60 * 60
MAX_LOG_BYTES = 8 * 1024 * 1024

_REQUEST_ID_DOMAIN = (
    b"hands-free-auto-lab-worker-request-id-v1\x00"
)

_RESULT_ID_DOMAIN = (
    b"hands-free-auto-lab-worker-result-id-v1\x00"
)

_HEX_LOWER = frozenset(
    "0123456789abcdef"
)


class LabWorkerContractError(
    ValueError
):
    """Worker request/result data is structurally invalid or tampered."""


@dataclass(
    frozen=True,
    slots=True,
)
class LabWorkerConstraints:
    """Capabilities and resource ceilings granted to one worker run."""

    capabilities: tuple[
        str,
        ...,
    ]
    max_runtime_seconds: int
    max_log_bytes: int


@dataclass(
    frozen=True,
    slots=True,
)
class LabWorkerRequest:
    """Immutable backend-neutral request for one coding worker."""

    component: str
    schema_version: int
    request_id: str
    worker: str
    goal: str
    change_policy_id: str | None
    workspace: LabWorkspace
    constraints: LabWorkerConstraints


@dataclass(
    frozen=True,
    slots=True,
)
class LabWorkerResult:
    """Structured worker-reported evidence, not promotion authority."""

    component: str
    schema_version: int
    result_id: str
    request_id: str
    worker: str
    workspace_session_id: str
    workspace_device: int
    workspace_inode: int
    status: str
    summary: str
    reported_changed_paths: tuple[
        str,
        ...,
    ]
    log: str


class AutoLabWorker(
    Protocol
):
    """Narrow adapter interface implemented by pluggable coding workers."""

    def run(
        self,
        request: LabWorkerRequest,
    ) -> LabWorkerResult:
        ...


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
    value: object,
    *,
    name: str,
    maximum_bytes: int,
) -> str:
    if (
        not isinstance(
            value,
            str,
        )
        or not value.strip()
    ):
        raise LabWorkerContractError(
            f"{name} must be a non-empty string"
        )

    if "\x00" in value:
        raise LabWorkerContractError(
            f"{name} must not contain NUL"
        )

    try:
        raw = value.encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise LabWorkerContractError(
            f"{name} must be UTF-8 encodable"
        ) from exc

    if len(
        raw
    ) > maximum_bytes:
        raise LabWorkerContractError(
            f"{name} exceeds maximum size"
        )

    return value


def _require_identifier(
    value: object,
    *,
    name: str,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise LabWorkerContractError(
            f"{name} must be a string"
        )

    if (
        len(value) != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabWorkerContractError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _validate_workspace_identity(
    workspace: object,
) -> LabWorkspace:
    if not isinstance(
        workspace,
        LabWorkspace,
    ):
        raise LabWorkerContractError(
            "workspace must be a LabWorkspace"
        )

    if workspace.component != WORKSPACE_COMPONENT:
        raise LabWorkerContractError(
            "workspace component mismatch"
        )

    _require_identifier(
        workspace.session_id,
        name="workspace session_id",
    )

    for name, value in (
        (
            "workspace parent",
            workspace.parent,
        ),
        (
            "workspace path",
            workspace.path,
        ),
    ):
        _require_text(
            value,
            name=name,
            maximum_bytes=4096,
        )

    for name, value in (
        (
            "workspace device",
            workspace.device,
        ),
        (
            "workspace inode",
            workspace.inode,
        ),
        (
            "workspace uid",
            workspace.uid,
        ),
        (
            "workspace mode",
            workspace.mode,
        ),
    ):
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
        ):
            raise LabWorkerContractError(
                f"{name} must be a non-negative integer"
            )

    return workspace


def _validate_constraints(
    constraints: object,
) -> LabWorkerConstraints:
    if not isinstance(
        constraints,
        LabWorkerConstraints,
    ):
        raise LabWorkerContractError(
            "constraints must be LabWorkerConstraints"
        )

    if not isinstance(
        constraints.capabilities,
        tuple,
    ):
        raise LabWorkerContractError(
            "capabilities must be a tuple"
        )

    if tuple(
        sorted(
            constraints.capabilities
        )
    ) != constraints.capabilities:
        raise LabWorkerContractError(
            "capabilities must be in canonical sorted order"
        )

    if len(
        set(
            constraints.capabilities
        )
    ) != len(
        constraints.capabilities
    ):
        raise LabWorkerContractError(
            "capabilities must not contain duplicates"
        )

    for capability in constraints.capabilities:
        if capability not in WORKER_CAPABILITIES:
            raise LabWorkerContractError(
                f"unsupported worker capability {capability!r}"
            )

    if (
        not isinstance(
            constraints.max_runtime_seconds,
            int,
        )
        or isinstance(
            constraints.max_runtime_seconds,
            bool,
        )
        or not (
            1
            <= constraints.max_runtime_seconds
            <= MAX_RUNTIME_SECONDS
        )
    ):
        raise LabWorkerContractError(
            "max_runtime_seconds is outside the supported range"
        )

    if (
        not isinstance(
            constraints.max_log_bytes,
            int,
        )
        or isinstance(
            constraints.max_log_bytes,
            bool,
        )
        or not (
            1
            <= constraints.max_log_bytes
            <= MAX_LOG_BYTES
        )
    ):
        raise LabWorkerContractError(
            "max_log_bytes is outside the supported range"
        )

    return constraints


def _workspace_identity_object(
    workspace: LabWorkspace,
) -> dict[
    str,
    object,
]:
    return {
        "component": workspace.component,
        "parent": workspace.parent,
        "path": workspace.path,
        "session_id": workspace.session_id,
        "device": workspace.device,
        "inode": workspace.inode,
        "uid": workspace.uid,
        "mode": workspace.mode,
    }


def _constraints_identity_object(
    constraints: LabWorkerConstraints,
) -> dict[
    str,
    object,
]:
    return {
        "capabilities": list(
            constraints.capabilities
        ),
        "max_runtime_seconds": (
            constraints.max_runtime_seconds
        ),
        "max_log_bytes": constraints.max_log_bytes,
    }


def _request_identity_object(
    *,
    worker: str,
    goal: str,
    change_policy_id: str | None,
    workspace: LabWorkspace,
    constraints: LabWorkerConstraints,
) -> dict[
    str,
    object,
]:
    return {
        "component": WORKER_REQUEST_COMPONENT,
        "schema_version": WORKER_REQUEST_SCHEMA_VERSION,
        "worker": worker,
        "goal": goal,
        "change_policy_id": change_policy_id,
        "workspace": _workspace_identity_object(
            workspace
        ),
        "constraints": _constraints_identity_object(
            constraints
        ),
    }


def _request_id(
    identity: dict[
        str,
        object,
    ],
) -> str:
    return hashlib.sha256(
        _REQUEST_ID_DOMAIN
        + _canonical_json_bytes(
            identity
        )
    ).hexdigest()


def build_lab_worker_request(
    *,
    worker: object,
    goal: object,
    workspace: LabWorkspace,
    change_policy_id: object = None,
    capabilities: tuple[
        str,
        ...,
    ] = (
        CAPABILITY_RUN_PROCESS,
        CAPABILITY_WORKSPACE_READ,
        CAPABILITY_WORKSPACE_WRITE,
    ),
    max_runtime_seconds: int = 600,
    max_log_bytes: int = 1024 * 1024,
) -> LabWorkerRequest:
    """Build one immutable request without executing or inspecting the host."""
    worker_text = _require_text(
        worker,
        name="worker",
        maximum_bytes=MAX_WORKER_NAME_BYTES,
    )

    goal_text = _require_text(
        goal,
        name="goal",
        maximum_bytes=MAX_GOAL_BYTES,
    )

    workspace = _validate_workspace_identity(
        workspace
    )

    if change_policy_id is None:
        policy_id = None
    else:
        policy_id = _require_identifier(
            change_policy_id,
            name="change_policy_id",
        )

    if not isinstance(
        capabilities,
        tuple,
    ):
        raise LabWorkerContractError(
            "capabilities must be a tuple"
        )

    constraints = LabWorkerConstraints(
        capabilities=tuple(
            sorted(
                capabilities
            )
        ),
        max_runtime_seconds=max_runtime_seconds,
        max_log_bytes=max_log_bytes,
    )

    constraints = _validate_constraints(
        constraints
    )

    identity = _request_identity_object(
        worker=worker_text,
        goal=goal_text,
        change_policy_id=policy_id,
        workspace=workspace,
        constraints=constraints,
    )

    request = LabWorkerRequest(
        component=WORKER_REQUEST_COMPONENT,
        schema_version=WORKER_REQUEST_SCHEMA_VERSION,
        request_id=_request_id(
            identity
        ),
        worker=worker_text,
        goal=goal_text,
        change_policy_id=policy_id,
        workspace=workspace,
        constraints=constraints,
    )

    validate_lab_worker_request(
        request
    )

    return request


def validate_lab_worker_request(
    request: object,
) -> LabWorkerRequest:
    """Validate immutable request structure and deterministic identity."""
    if not isinstance(
        request,
        LabWorkerRequest,
    ):
        raise LabWorkerContractError(
            "request must be a LabWorkerRequest"
        )

    if request.component != WORKER_REQUEST_COMPONENT:
        raise LabWorkerContractError(
            "worker request component mismatch"
        )

    if (
        request.schema_version
        != WORKER_REQUEST_SCHEMA_VERSION
    ):
        raise LabWorkerContractError(
            "unsupported worker request schema version"
        )

    worker = _require_text(
        request.worker,
        name="worker",
        maximum_bytes=MAX_WORKER_NAME_BYTES,
    )

    goal = _require_text(
        request.goal,
        name="goal",
        maximum_bytes=MAX_GOAL_BYTES,
    )

    workspace = _validate_workspace_identity(
        request.workspace
    )

    if request.change_policy_id is None:
        policy_id = None
    else:
        policy_id = _require_identifier(
            request.change_policy_id,
            name="change_policy_id",
        )

    constraints = _validate_constraints(
        request.constraints
    )

    supplied_id = _require_identifier(
        request.request_id,
        name="request_id",
    )

    expected_id = _request_id(
        _request_identity_object(
            worker=worker,
            goal=goal,
            change_policy_id=policy_id,
            workspace=workspace,
            constraints=constraints,
        )
    )

    if supplied_id != expected_id:
        raise LabWorkerContractError(
            "worker request identity mismatch"
        )

    return request


def _require_reported_path(
    value: object,
) -> str:
    text = _require_text(
        value,
        name="reported changed path",
        maximum_bytes=4096,
    )

    path = PurePosixPath(
        text
    )

    if (
        path.is_absolute()
        or text == "."
        or ".." in path.parts
        or str(path) != text
    ):
        raise LabWorkerContractError(
            "reported changed path must be canonical and relative"
        )

    return text


def _result_identity_object(
    *,
    request_id: str,
    worker: str,
    workspace_session_id: str,
    workspace_device: int,
    workspace_inode: int,
    status: str,
    summary: str,
    reported_changed_paths: tuple[
        str,
        ...,
    ],
    log: str,
) -> dict[
    str,
    object,
]:
    return {
        "component": WORKER_RESULT_COMPONENT,
        "schema_version": WORKER_RESULT_SCHEMA_VERSION,
        "request_id": request_id,
        "worker": worker,
        "workspace_session_id": workspace_session_id,
        "workspace_device": workspace_device,
        "workspace_inode": workspace_inode,
        "status": status,
        "summary": summary,
        "reported_changed_paths": list(
            reported_changed_paths
        ),
        "log": log,
    }


def _result_id(
    identity: dict[
        str,
        object,
    ],
) -> str:
    return hashlib.sha256(
        _RESULT_ID_DOMAIN
        + _canonical_json_bytes(
            identity
        )
    ).hexdigest()


def build_lab_worker_result(
    *,
    request: LabWorkerRequest,
    status: object,
    summary: object,
    reported_changed_paths: tuple[
        str,
        ...,
    ] = (),
    log: object = "",
) -> LabWorkerResult:
    """Build worker-reported evidence bound to one exact worker request."""
    request = validate_lab_worker_request(
        request
    )

    if status not in WORKER_STATUSES:
        raise LabWorkerContractError(
            f"unsupported worker status {status!r}"
        )

    summary_text = _require_text(
        summary,
        name="worker summary",
        maximum_bytes=MAX_SUMMARY_BYTES,
    )

    if not isinstance(
        reported_changed_paths,
        tuple,
    ):
        raise LabWorkerContractError(
            "reported_changed_paths must be a tuple"
        )

    canonical_paths = tuple(
        sorted(
            _require_reported_path(
                value
            )
            for value in reported_changed_paths
        )
    )

    if len(
        set(
            canonical_paths
        )
    ) != len(
        canonical_paths
    ):
        raise LabWorkerContractError(
            "reported_changed_paths must not contain duplicates"
        )

    if not isinstance(
        log,
        str,
    ):
        raise LabWorkerContractError(
            "worker log must be a string"
        )

    if "\x00" in log:
        raise LabWorkerContractError(
            "worker log must not contain NUL"
        )

    try:
        log_bytes = log.encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise LabWorkerContractError(
            "worker log must be UTF-8 encodable"
        ) from exc

    if len(
        log_bytes
    ) > request.constraints.max_log_bytes:
        raise LabWorkerContractError(
            "worker log exceeds request limit"
        )

    identity = _result_identity_object(
        request_id=request.request_id,
        worker=request.worker,
        workspace_session_id=request.workspace.session_id,
        workspace_device=request.workspace.device,
        workspace_inode=request.workspace.inode,
        status=status,
        summary=summary_text,
        reported_changed_paths=canonical_paths,
        log=log,
    )

    result = LabWorkerResult(
        component=WORKER_RESULT_COMPONENT,
        schema_version=WORKER_RESULT_SCHEMA_VERSION,
        result_id=_result_id(
            identity
        ),
        request_id=request.request_id,
        worker=request.worker,
        workspace_session_id=request.workspace.session_id,
        workspace_device=request.workspace.device,
        workspace_inode=request.workspace.inode,
        status=status,
        summary=summary_text,
        reported_changed_paths=canonical_paths,
        log=log,
    )

    validate_lab_worker_result(
        result,
        request=request,
    )

    return result


def validate_lab_worker_result(
    result: object,
    *,
    request: LabWorkerRequest,
) -> LabWorkerResult:
    """Validate one worker result against the exact originating request."""
    request = validate_lab_worker_request(
        request
    )

    if not isinstance(
        result,
        LabWorkerResult,
    ):
        raise LabWorkerContractError(
            "result must be a LabWorkerResult"
        )

    if result.component != WORKER_RESULT_COMPONENT:
        raise LabWorkerContractError(
            "worker result component mismatch"
        )

    if (
        result.schema_version
        != WORKER_RESULT_SCHEMA_VERSION
    ):
        raise LabWorkerContractError(
            "unsupported worker result schema version"
        )

    supplied_id = _require_identifier(
        result.result_id,
        name="result_id",
    )

    if result.request_id != request.request_id:
        raise LabWorkerContractError(
            "worker result request binding mismatch"
        )

    if result.worker != request.worker:
        raise LabWorkerContractError(
            "worker result worker binding mismatch"
        )

    if (
        result.workspace_session_id
        != request.workspace.session_id
        or result.workspace_device
        != request.workspace.device
        or result.workspace_inode
        != request.workspace.inode
    ):
        raise LabWorkerContractError(
            "worker result workspace binding mismatch"
        )

    if result.status not in WORKER_STATUSES:
        raise LabWorkerContractError(
            "worker result status is unsupported"
        )

    summary = _require_text(
        result.summary,
        name="worker summary",
        maximum_bytes=MAX_SUMMARY_BYTES,
    )

    if not isinstance(
        result.reported_changed_paths,
        tuple,
    ):
        raise LabWorkerContractError(
            "reported_changed_paths must be a tuple"
        )

    paths = tuple(
        _require_reported_path(
            value
        )
        for value in result.reported_changed_paths
    )

    if (
        tuple(
            sorted(
                paths
            )
        )
        != paths
        or len(
            set(
                paths
            )
        )
        != len(
            paths
        )
    ):
        raise LabWorkerContractError(
            "reported_changed_paths are not canonical"
        )

    if not isinstance(
        result.log,
        str,
    ):
        raise LabWorkerContractError(
            "worker log must be a string"
        )

    if "\x00" in result.log:
        raise LabWorkerContractError(
            "worker log must not contain NUL"
        )

    try:
        raw_log = result.log.encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise LabWorkerContractError(
            "worker log must be UTF-8 encodable"
        ) from exc

    if len(
        raw_log
    ) > request.constraints.max_log_bytes:
        raise LabWorkerContractError(
            "worker log exceeds request limit"
        )

    expected_id = _result_id(
        _result_identity_object(
            request_id=result.request_id,
            worker=result.worker,
            workspace_session_id=result.workspace_session_id,
            workspace_device=result.workspace_device,
            workspace_inode=result.workspace_inode,
            status=result.status,
            summary=summary,
            reported_changed_paths=paths,
            log=result.log,
        )
    )

    if supplied_id != expected_id:
        raise LabWorkerContractError(
            "worker result identity mismatch"
        )

    return result
