"""Versioned data-only boundary for external Auto Lab coding callers.

The records in this module are requests and evidence references only.

They grant no worker selection, execution, filesystem mutation, promotion,
approval, Git, credential, networking, or host-configuration authority.

Auto Lab deployment policy remains responsible for choosing the worker,
workspace location, candidate store, and internal resource ceilings.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath

from .lab_worker_change_policy import (
    CHANGE_POLICY_COMPONENT,
    CHANGE_POLICY_SCHEMA_VERSION,
    LabWorkerChangePolicy,
    LabWorkerChangeTarget,
    validate_lab_worker_change_policy,
)


CODING_INTEGRATION_REQUEST_COMPONENT = (
    "hands-free-auto-lab-coding-integration-request-v1"
)
CODING_INTEGRATION_REQUEST_SCHEMA_VERSION = 1

CODING_INTEGRATION_RESULT_COMPONENT = (
    "hands-free-auto-lab-coding-integration-result-v1"
)
CODING_INTEGRATION_RESULT_SCHEMA_VERSION = 1

MAX_INTEGRATION_GOAL_BYTES = 64 * 1024
MAX_INTEGRATION_RUNTIME_SECONDS = 60 * 60

_REQUEST_ID_DOMAIN = (
    b"hands-free-auto-lab-coding-integration-request-id-v1\x00"
)
_RESULT_ID_DOMAIN = (
    b"hands-free-auto-lab-coding-integration-result-id-v1\x00"
)

_HEX_LOWER = frozenset("0123456789abcdef")


class LabCodingIntegrationContractError(ValueError):
    """External coding request/result contract failed closed."""


@dataclass(frozen=True, slots=True)
class LabCodingIntegrationRequest:
    component: str
    schema_version: int
    request_id: str
    repository_path: str
    commit_oid: str
    expected_branch: str | None
    relative_paths: tuple[str, ...]
    goal: str
    change_policy: LabWorkerChangePolicy
    max_runtime_seconds: int


@dataclass(frozen=True, slots=True)
class LabCodingIntegrationResult:
    component: str
    schema_version: int
    result_id: str
    request_id: str
    candidate_id: str


def _canonical_json_bytes(
    value: object,
    *,
    newline: bool = False,
) -> bytes:
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    if newline:
        text += "\n"

    return text.encode(
        "utf-8",
        errors="strict",
    )


def _require_identifier(
    value: object,
    *,
    name: str,
    length: int,
) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabCodingIntegrationContractError(
            f"{name} must be a lowercase {length}-character hex identifier"
        )

    return value


def _require_text(
    value: object,
    *,
    name: str,
    maximum_bytes: int,
) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or "\x00" in value
    ):
        raise LabCodingIntegrationContractError(
            f"{name} must be a non-empty string without NUL"
        )

    try:
        encoded = value.encode(
            "utf-8",
            errors="strict",
        )
    except UnicodeEncodeError as exc:
        raise LabCodingIntegrationContractError(
            f"{name} must be UTF-8 encodable"
        ) from exc

    if len(encoded) > maximum_bytes:
        raise LabCodingIntegrationContractError(
            f"{name} exceeds maximum size"
        )

    return value


def _require_absolute_path(
    value: object,
    *,
    name: str,
) -> str:
    text = _require_text(
        value,
        name=name,
        maximum_bytes=4096,
    )

    parsed = PurePosixPath(text)

    if (
        not parsed.is_absolute()
        or ".." in parsed.parts
        or str(parsed) != text
    ):
        raise LabCodingIntegrationContractError(
            f"{name} must be an absolute normalized path"
        )

    return text


def _require_relative_path(
    value: object,
) -> str:
    text = _require_text(
        value,
        name="relative path",
        maximum_bytes=4096,
    )

    parsed = PurePosixPath(text)

    if (
        parsed.is_absolute()
        or text == "."
        or ".." in parsed.parts
        or str(parsed) != text
        or text == ".git"
        or text.startswith(".git/")
    ):
        raise LabCodingIntegrationContractError(
            "relative path must be canonical, relative, and outside .git"
        )

    return text


def _policy_to_wire(
    policy: object,
) -> dict[str, object]:
    trusted = validate_lab_worker_change_policy(
        policy
    )

    return {
        "component": trusted.component,
        "schema_version": trusted.schema_version,
        "policy_id": trusted.policy_id,
        "targets": [
            {
                "operation": target.operation,
                "path": target.path,
                "max_final_bytes": target.max_final_bytes,
                "final_mode": target.final_mode,
                "before_bytes": target.before_bytes,
                "before_sha256": target.before_sha256,
                "before_mode": target.before_mode,
            }
            for target in trusted.targets
        ],
    }


_POLICY_WIRE_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "policy_id",
        "targets",
    }
)

_POLICY_TARGET_WIRE_FIELDS = frozenset(
    {
        "operation",
        "path",
        "max_final_bytes",
        "final_mode",
        "before_bytes",
        "before_sha256",
        "before_mode",
    }
)


def _policy_from_wire(
    value: object,
) -> LabWorkerChangePolicy:
    if type(value) is not dict:
        raise LabCodingIntegrationContractError(
            "change policy wire value must be an exact object"
        )

    if frozenset(value) != _POLICY_WIRE_FIELDS:
        raise LabCodingIntegrationContractError(
            "change policy wire fields do not exactly match schema"
        )

    targets_value = value["targets"]

    if (
        type(targets_value) is not list
        or not targets_value
    ):
        raise LabCodingIntegrationContractError(
            "change policy wire targets must be a non-empty exact list"
        )

    targets: list[LabWorkerChangeTarget] = []

    for item in targets_value:
        if type(item) is not dict:
            raise LabCodingIntegrationContractError(
                "change policy wire target must be an exact object"
            )

        if frozenset(item) != _POLICY_TARGET_WIRE_FIELDS:
            raise LabCodingIntegrationContractError(
                "change policy target fields do not exactly match schema"
            )

        targets.append(
            LabWorkerChangeTarget(
                operation=item["operation"],
                path=item["path"],
                max_final_bytes=item["max_final_bytes"],
                final_mode=item["final_mode"],
                before_bytes=item["before_bytes"],
                before_sha256=item["before_sha256"],
                before_mode=item["before_mode"],
            )
        )

    policy = LabWorkerChangePolicy(
        component=value["component"],
        schema_version=value["schema_version"],
        policy_id=value["policy_id"],
        targets=tuple(targets),
    )

    try:
        return validate_lab_worker_change_policy(
            policy
        )
    except Exception as exc:
        raise LabCodingIntegrationContractError(
            f"change policy wire evidence is invalid: {exc}"
        ) from exc


def _request_identity(
    request: LabCodingIntegrationRequest,
) -> dict[str, object]:
    return {
        "component": request.component,
        "schema_version": request.schema_version,
        "repository_path": request.repository_path,
        "commit_oid": request.commit_oid,
        "expected_branch": request.expected_branch,
        "relative_paths": list(request.relative_paths),
        "goal": request.goal,
        "change_policy": _policy_to_wire(
            request.change_policy
        ),
        "max_runtime_seconds": request.max_runtime_seconds,
    }


def _request_id(
    request: LabCodingIntegrationRequest,
) -> str:
    return hashlib.sha256(
        _REQUEST_ID_DOMAIN
        + _canonical_json_bytes(
            _request_identity(request)
        )
    ).hexdigest()


def validate_lab_coding_integration_request(
    request: object,
) -> LabCodingIntegrationRequest:
    if type(request) is not LabCodingIntegrationRequest:
        raise LabCodingIntegrationContractError(
            "request must have the exact LabCodingIntegrationRequest type"
        )

    if request.component != CODING_INTEGRATION_REQUEST_COMPONENT:
        raise LabCodingIntegrationContractError(
            "coding integration request component mismatch"
        )

    if (
        type(request.schema_version) is not int
        or request.schema_version
        != CODING_INTEGRATION_REQUEST_SCHEMA_VERSION
    ):
        raise LabCodingIntegrationContractError(
            "unsupported coding integration request schema version"
        )

    supplied_id = _require_identifier(
        request.request_id,
        name="request_id",
        length=64,
    )

    _require_absolute_path(
        request.repository_path,
        name="repository_path",
    )

    _require_identifier(
        request.commit_oid,
        name="commit_oid",
        length=40,
    )

    if request.expected_branch is not None:
        _require_text(
            request.expected_branch,
            name="expected_branch",
            maximum_bytes=1024,
        )

    if (
        type(request.relative_paths) is not tuple
        or not request.relative_paths
    ):
        raise LabCodingIntegrationContractError(
            "relative_paths must be a non-empty exact tuple"
        )

    paths = tuple(
        _require_relative_path(path)
        for path in request.relative_paths
    )

    if paths != tuple(sorted(paths)):
        raise LabCodingIntegrationContractError(
            "relative_paths must be in canonical path order"
        )

    if len(paths) != len(set(paths)):
        raise LabCodingIntegrationContractError(
            "relative_paths must not contain duplicates"
        )

    _require_text(
        request.goal,
        name="goal",
        maximum_bytes=MAX_INTEGRATION_GOAL_BYTES,
    )

    validate_lab_worker_change_policy(
        request.change_policy
    )

    if (
        type(request.max_runtime_seconds) is not int
        or not (
            1
            <= request.max_runtime_seconds
            <= MAX_INTEGRATION_RUNTIME_SECONDS
        )
    ):
        raise LabCodingIntegrationContractError(
            "max_runtime_seconds is outside the supported range"
        )

    if supplied_id != _request_id(request):
        raise LabCodingIntegrationContractError(
            "request_id does not match request evidence"
        )

    return request


def build_lab_coding_integration_request(
    *,
    repository_path: object,
    commit_oid: object,
    expected_branch: object,
    relative_paths: object,
    goal: object,
    change_policy: object,
    max_runtime_seconds: object,
) -> LabCodingIntegrationRequest:
    trusted_policy = validate_lab_worker_change_policy(
        change_policy
    )

    if type(relative_paths) is not tuple:
        raise LabCodingIntegrationContractError(
            "relative_paths must be a non-empty exact tuple"
        )

    provisional = LabCodingIntegrationRequest(
        component=CODING_INTEGRATION_REQUEST_COMPONENT,
        schema_version=CODING_INTEGRATION_REQUEST_SCHEMA_VERSION,
        request_id="0" * 64,
        repository_path=repository_path,
        commit_oid=commit_oid,
        expected_branch=expected_branch,
        relative_paths=relative_paths,
        goal=goal,
        change_policy=trusted_policy,
        max_runtime_seconds=max_runtime_seconds,
    )

    final = LabCodingIntegrationRequest(
        component=provisional.component,
        schema_version=provisional.schema_version,
        request_id=_request_id(provisional),
        repository_path=provisional.repository_path,
        commit_oid=provisional.commit_oid,
        expected_branch=provisional.expected_branch,
        relative_paths=provisional.relative_paths,
        goal=provisional.goal,
        change_policy=provisional.change_policy,
        max_runtime_seconds=provisional.max_runtime_seconds,
    )

    return validate_lab_coding_integration_request(
        final
    )


_REQUEST_WIRE_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "request_id",
        "repository_path",
        "commit_oid",
        "expected_branch",
        "relative_paths",
        "goal",
        "change_policy",
        "max_runtime_seconds",
    }
)


def lab_coding_integration_request_to_wire(
    request: object,
) -> dict[str, object]:
    trusted = validate_lab_coding_integration_request(
        request
    )

    return {
        "component": trusted.component,
        "schema_version": trusted.schema_version,
        "request_id": trusted.request_id,
        "repository_path": trusted.repository_path,
        "commit_oid": trusted.commit_oid,
        "expected_branch": trusted.expected_branch,
        "relative_paths": list(trusted.relative_paths),
        "goal": trusted.goal,
        "change_policy": _policy_to_wire(
            trusted.change_policy
        ),
        "max_runtime_seconds": trusted.max_runtime_seconds,
    }


def lab_coding_integration_request_from_wire(
    value: object,
) -> LabCodingIntegrationRequest:
    if type(value) is not dict:
        raise LabCodingIntegrationContractError(
            "request wire value must be an exact object"
        )

    if frozenset(value) != _REQUEST_WIRE_FIELDS:
        raise LabCodingIntegrationContractError(
            "request wire fields do not exactly match schema"
        )

    paths = value["relative_paths"]

    if (
        type(paths) is not list
        or not paths
    ):
        raise LabCodingIntegrationContractError(
            "request wire relative_paths must be a non-empty exact list"
        )

    request = LabCodingIntegrationRequest(
        component=value["component"],
        schema_version=value["schema_version"],
        request_id=value["request_id"],
        repository_path=value["repository_path"],
        commit_oid=value["commit_oid"],
        expected_branch=value["expected_branch"],
        relative_paths=tuple(paths),
        goal=value["goal"],
        change_policy=_policy_from_wire(
            value["change_policy"]
        ),
        max_runtime_seconds=value["max_runtime_seconds"],
    )

    return validate_lab_coding_integration_request(
        request
    )


def lab_coding_integration_request_to_bytes(
    request: object,
) -> bytes:
    return _canonical_json_bytes(
        lab_coding_integration_request_to_wire(
            request
        ),
        newline=True,
    )


def lab_coding_integration_request_from_bytes(
    record_bytes: object,
) -> LabCodingIntegrationRequest:
    value = _decode_canonical_record(
        record_bytes,
        name="request",
    )

    return lab_coding_integration_request_from_wire(
        value
    )


def _result_identity(
    result: LabCodingIntegrationResult,
) -> dict[str, object]:
    return {
        "component": result.component,
        "schema_version": result.schema_version,
        "request_id": result.request_id,
        "candidate_id": result.candidate_id,
    }


def _result_id(
    result: LabCodingIntegrationResult,
) -> str:
    return hashlib.sha256(
        _RESULT_ID_DOMAIN
        + _canonical_json_bytes(
            _result_identity(result)
        )
    ).hexdigest()


def validate_lab_coding_integration_result(
    result: object,
) -> LabCodingIntegrationResult:
    if type(result) is not LabCodingIntegrationResult:
        raise LabCodingIntegrationContractError(
            "result must have the exact LabCodingIntegrationResult type"
        )

    if result.component != CODING_INTEGRATION_RESULT_COMPONENT:
        raise LabCodingIntegrationContractError(
            "coding integration result component mismatch"
        )

    if (
        type(result.schema_version) is not int
        or result.schema_version
        != CODING_INTEGRATION_RESULT_SCHEMA_VERSION
    ):
        raise LabCodingIntegrationContractError(
            "unsupported coding integration result schema version"
        )

    supplied_id = _require_identifier(
        result.result_id,
        name="result_id",
        length=64,
    )

    _require_identifier(
        result.request_id,
        name="request_id",
        length=64,
    )

    _require_identifier(
        result.candidate_id,
        name="candidate_id",
        length=64,
    )

    if supplied_id != _result_id(result):
        raise LabCodingIntegrationContractError(
            "result_id does not match result evidence"
        )

    return result


def build_lab_coding_integration_result(
    *,
    request: object,
    candidate_id: object,
) -> LabCodingIntegrationResult:
    trusted_request = validate_lab_coding_integration_request(
        request
    )

    candidate_identifier = _require_identifier(
        candidate_id,
        name="candidate_id",
        length=64,
    )

    provisional = LabCodingIntegrationResult(
        component=CODING_INTEGRATION_RESULT_COMPONENT,
        schema_version=CODING_INTEGRATION_RESULT_SCHEMA_VERSION,
        result_id="0" * 64,
        request_id=trusted_request.request_id,
        candidate_id=candidate_identifier,
    )

    final = LabCodingIntegrationResult(
        component=provisional.component,
        schema_version=provisional.schema_version,
        result_id=_result_id(provisional),
        request_id=provisional.request_id,
        candidate_id=provisional.candidate_id,
    )

    return validate_lab_coding_integration_result(
        final
    )


_RESULT_WIRE_FIELDS = frozenset(
    {
        "component",
        "schema_version",
        "result_id",
        "request_id",
        "candidate_id",
    }
)


def lab_coding_integration_result_to_wire(
    result: object,
) -> dict[str, object]:
    trusted = validate_lab_coding_integration_result(
        result
    )

    return {
        "component": trusted.component,
        "schema_version": trusted.schema_version,
        "result_id": trusted.result_id,
        "request_id": trusted.request_id,
        "candidate_id": trusted.candidate_id,
    }


def lab_coding_integration_result_from_wire(
    value: object,
) -> LabCodingIntegrationResult:
    if type(value) is not dict:
        raise LabCodingIntegrationContractError(
            "result wire value must be an exact object"
        )

    if frozenset(value) != _RESULT_WIRE_FIELDS:
        raise LabCodingIntegrationContractError(
            "result wire fields do not exactly match schema"
        )

    result = LabCodingIntegrationResult(
        component=value["component"],
        schema_version=value["schema_version"],
        result_id=value["result_id"],
        request_id=value["request_id"],
        candidate_id=value["candidate_id"],
    )

    return validate_lab_coding_integration_result(
        result
    )


def lab_coding_integration_result_to_bytes(
    result: object,
) -> bytes:
    return _canonical_json_bytes(
        lab_coding_integration_result_to_wire(
            result
        ),
        newline=True,
    )


def lab_coding_integration_result_from_bytes(
    record_bytes: object,
) -> LabCodingIntegrationResult:
    value = _decode_canonical_record(
        record_bytes,
        name="result",
    )

    return lab_coding_integration_result_from_wire(
        value
    )


def _decode_canonical_record(
    record_bytes: object,
    *,
    name: str,
) -> dict[str, object]:
    if type(record_bytes) is not bytes:
        raise LabCodingIntegrationContractError(
            f"{name} record must have the exact bytes type"
        )

    try:
        text = record_bytes.decode(
            "utf-8",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise LabCodingIntegrationContractError(
            f"{name} record is not valid UTF-8"
        ) from exc

    try:
        value = json.loads(text)
    except (
        json.JSONDecodeError,
        RecursionError,
    ) as exc:
        raise LabCodingIntegrationContractError(
            f"{name} record is not valid JSON"
        ) from exc

    if type(value) is not dict:
        raise LabCodingIntegrationContractError(
            f"{name} record must be a JSON object"
        )

    try:
        canonical = _canonical_json_bytes(
            value,
            newline=True,
        )
    except (
        TypeError,
        ValueError,
        UnicodeEncodeError,
    ) as exc:
        raise LabCodingIntegrationContractError(
            f"{name} record cannot be canonically encoded"
        ) from exc

    if canonical != record_bytes:
        raise LabCodingIntegrationContractError(
            f"{name} record is not canonical JSON"
        )

    return value
