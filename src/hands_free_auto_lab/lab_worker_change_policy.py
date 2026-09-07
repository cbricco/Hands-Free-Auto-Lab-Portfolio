"""Immutable pre-worker target/change policy for Auto Lab coding workers.

This module grants no execution, approval, promotion, Git, network, or
filesystem-mutation authority.

A policy describes the only physical file changes that may become a
successful worker result. Worker-reported changed paths are never authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath

from .lab_workspace_snapshot import (
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    ENTRY_FILE,
    LabWorkspaceDiff,
    LabWorkspaceSnapshot,
    diff_lab_workspace_snapshots,
    validate_lab_workspace_snapshot,
)


CHANGE_POLICY_COMPONENT = "hands-free-auto-lab-worker-change-policy-v1"
CHANGE_POLICY_SCHEMA_VERSION = 1

OPERATION_ADD = CHANGE_ADDED
OPERATION_MODIFY = CHANGE_MODIFIED

_POLICY_ID_DOMAIN = (
    b"hands-free-auto-lab-worker-change-policy-id-v1\x00"
)

_HEX_LOWER = frozenset("0123456789abcdef")


class LabWorkerChangePolicyError(ValueError):
    """Worker change policy or physical evidence failed closed."""


@dataclass(frozen=True, slots=True)
class LabWorkerChangeTarget:
    operation: str
    path: str
    max_final_bytes: int
    final_mode: int
    before_bytes: int | None
    before_sha256: str | None
    before_mode: int | None


@dataclass(frozen=True, slots=True)
class LabWorkerChangePolicy:
    component: str
    schema_version: int
    policy_id: str
    targets: tuple[LabWorkerChangeTarget, ...]


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode(
        "utf-8",
        errors="strict",
    )


def _require_identifier(
    value: object,
    *,
    name: str,
) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(
            character not in _HEX_LOWER
            for character in value
        )
    ):
        raise LabWorkerChangePolicyError(
            f"{name} must be a lowercase 64-character hex identifier"
        )

    return value


def _require_relative_path(
    value: object,
) -> str:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
    ):
        raise LabWorkerChangePolicyError(
            "change-policy path must be a non-empty string without NUL"
        )

    try:
        value.encode(
            "utf-8",
            errors="strict",
        )
    except UnicodeEncodeError as exc:
        raise LabWorkerChangePolicyError(
            "change-policy path must be UTF-8 encodable"
        ) from exc

    parsed = PurePosixPath(value)

    if (
        parsed.is_absolute()
        or value == "."
        or ".." in parsed.parts
        or str(parsed) != value
        or value.startswith(".git/")
        or value == ".git"
    ):
        raise LabWorkerChangePolicyError(
            "change-policy path must be canonical, relative, and outside .git"
        )

    return value


def _require_nonnegative_int(
    value: object,
    *,
    name: str,
) -> int:
    if (
        type(value) is not int
        or value < 0
    ):
        raise LabWorkerChangePolicyError(
            f"{name} must be an exact non-negative integer"
        )

    return value


def _require_positive_int(
    value: object,
    *,
    name: str,
) -> int:
    if (
        type(value) is not int
        or value <= 0
    ):
        raise LabWorkerChangePolicyError(
            f"{name} must be an exact positive integer"
        )

    return value


def _require_mode(
    value: object,
    *,
    name: str,
) -> int:
    if (
        type(value) is not int
        or not 0 <= value <= 0o7777
    ):
        raise LabWorkerChangePolicyError(
            f"{name} must be an exact valid mode"
        )

    if value & 0o7000:
        raise LabWorkerChangePolicyError(
            f"{name} must not contain special mode bits"
        )

    if value & 0o111:
        raise LabWorkerChangePolicyError(
            f"{name} must not be executable"
        )

    if value & 0o022:
        raise LabWorkerChangePolicyError(
            f"{name} must not be group- or other-writable"
        )

    return value


def _target_identity(
    target: LabWorkerChangeTarget,
) -> dict[str, object]:
    return {
        "operation": target.operation,
        "path": target.path,
        "max_final_bytes": target.max_final_bytes,
        "final_mode": target.final_mode,
        "before_bytes": target.before_bytes,
        "before_sha256": target.before_sha256,
        "before_mode": target.before_mode,
    }


def _policy_identity(
    policy: LabWorkerChangePolicy,
) -> dict[str, object]:
    return {
        "component": policy.component,
        "schema_version": policy.schema_version,
        "targets": [
            _target_identity(target)
            for target in policy.targets
        ],
    }


def _policy_id(
    policy: LabWorkerChangePolicy,
) -> str:
    return hashlib.sha256(
        _POLICY_ID_DOMAIN
        + _canonical_json_bytes(
            _policy_identity(policy)
        )
    ).hexdigest()


def validate_lab_worker_change_target(
    target: object,
) -> LabWorkerChangeTarget:
    if type(target) is not LabWorkerChangeTarget:
        raise LabWorkerChangePolicyError(
            "change target must have the exact LabWorkerChangeTarget type"
        )

    if target.operation not in (
        OPERATION_ADD,
        OPERATION_MODIFY,
    ):
        raise LabWorkerChangePolicyError(
            "change target operation must be ADDED or MODIFIED"
        )

    _require_relative_path(
        target.path
    )

    _require_positive_int(
        target.max_final_bytes,
        name="max_final_bytes",
    )

    _require_mode(
        target.final_mode,
        name="final_mode",
    )

    if target.operation == OPERATION_ADD:
        if (
            target.before_bytes,
            target.before_sha256,
            target.before_mode,
        ) != (
            None,
            None,
            None,
        ):
            raise LabWorkerChangePolicyError(
                "ADDED target must not contain before-state provenance"
            )

    else:
        _require_nonnegative_int(
            target.before_bytes,
            name="before_bytes",
        )

        _require_identifier(
            target.before_sha256,
            name="before_sha256",
        )

        _require_mode(
            target.before_mode,
            name="before_mode",
        )

    return target


def validate_lab_worker_change_policy(
    policy: object,
) -> LabWorkerChangePolicy:
    if type(policy) is not LabWorkerChangePolicy:
        raise LabWorkerChangePolicyError(
            "change policy must have the exact LabWorkerChangePolicy type"
        )

    if policy.component != CHANGE_POLICY_COMPONENT:
        raise LabWorkerChangePolicyError(
            "change-policy component mismatch"
        )

    if (
        type(policy.schema_version) is not int
        or policy.schema_version
        != CHANGE_POLICY_SCHEMA_VERSION
    ):
        raise LabWorkerChangePolicyError(
            "unsupported change-policy schema version"
        )

    supplied_id = _require_identifier(
        policy.policy_id,
        name="policy_id",
    )

    if (
        type(policy.targets) is not tuple
        or not policy.targets
    ):
        raise LabWorkerChangePolicyError(
            "change policy must contain a non-empty target tuple"
        )

    targets = tuple(
        validate_lab_worker_change_target(
            target
        )
        for target in policy.targets
    )

    paths = tuple(
        target.path
        for target in targets
    )

    if paths != tuple(
        sorted(
            paths
        )
    ):
        raise LabWorkerChangePolicyError(
            "change-policy targets must be in canonical path order"
        )

    if len(paths) != len(
        set(
            paths
        )
    ):
        raise LabWorkerChangePolicyError(
            "change-policy targets must not contain duplicate paths"
        )

    expected_id = _policy_id(
        policy
    )

    if supplied_id != expected_id:
        raise LabWorkerChangePolicyError(
            "change-policy identity mismatch"
        )

    return policy


def build_lab_worker_change_policy(
    *,
    targets: tuple[LabWorkerChangeTarget, ...],
) -> LabWorkerChangePolicy:
    if type(targets) is not tuple:
        raise LabWorkerChangePolicyError(
            "targets must be an exact tuple"
        )

    canonical_targets = tuple(
        sorted(
            (
                validate_lab_worker_change_target(
                    target
                )
                for target in targets
            ),
            key=lambda target: target.path,
        )
    )

    provisional = LabWorkerChangePolicy(
        component=CHANGE_POLICY_COMPONENT,
        schema_version=CHANGE_POLICY_SCHEMA_VERSION,
        policy_id="0" * 64,
        targets=canonical_targets,
    )

    policy = LabWorkerChangePolicy(
        component=provisional.component,
        schema_version=provisional.schema_version,
        policy_id=_policy_id(
            provisional
        ),
        targets=provisional.targets,
    )

    return validate_lab_worker_change_policy(
        policy
    )


def validate_lab_worker_change_policy_before(
    policy: object,
    *,
    before_snapshot: object,
) -> LabWorkerChangePolicy:
    trusted_policy = validate_lab_worker_change_policy(
        policy
    )

    before = validate_lab_workspace_snapshot(
        before_snapshot
    )

    before_by_path = {
        entry.path: entry
        for entry in before.entries
    }

    for target in trusted_policy.targets:
        existing = before_by_path.get(
            target.path
        )

        if target.operation == OPERATION_ADD:
            if existing is not None:
                raise LabWorkerChangePolicyError(
                    f"ADDED target already exists before worker: {target.path!r}"
                )

            continue

        if (
            existing is None
            or existing.kind != ENTRY_FILE
        ):
            raise LabWorkerChangePolicyError(
                f"MODIFIED target lacks regular-file before state: {target.path!r}"
            )

        if (
            existing.bytes,
            existing.sha256,
            existing.mode,
        ) != (
            target.before_bytes,
            target.before_sha256,
            target.before_mode,
        ):
            raise LabWorkerChangePolicyError(
                f"MODIFIED before-state provenance mismatch: {target.path!r}"
            )

    return trusted_policy


def validate_lab_worker_change_policy_diff(
    policy: object,
    *,
    before_snapshot: object,
    after_snapshot: object,
    physical_diff: object,
    require_complete: bool,
) -> LabWorkerChangePolicy:
    if type(require_complete) is not bool:
        raise LabWorkerChangePolicyError(
            "require_complete must be an exact bool"
        )

    trusted_policy = validate_lab_worker_change_policy_before(
        policy,
        before_snapshot=before_snapshot,
    )

    before = validate_lab_workspace_snapshot(
        before_snapshot
    )

    after = validate_lab_workspace_snapshot(
        after_snapshot
    )

    if type(physical_diff) is not LabWorkspaceDiff:
        raise LabWorkerChangePolicyError(
            "physical_diff must have the exact LabWorkspaceDiff type"
        )

    derived = diff_lab_workspace_snapshots(
        before,
        after,
    )

    if physical_diff != derived:
        raise LabWorkerChangePolicyError(
            "recorded physical diff differs from independently derived diff"
        )

    targets_by_path = {
        target.path: target
        for target in trusted_policy.targets
    }

    observed_paths: set[str] = set()

    for change in derived.entries:
        target = targets_by_path.get(
            change.path
        )

        if target is None:
            raise LabWorkerChangePolicyError(
                f"physical change is outside policy: {change.path!r}"
            )

        if change.path in observed_paths:
            raise LabWorkerChangePolicyError(
                "physical diff contains duplicate policy path"
            )

        observed_paths.add(
            change.path
        )

        if change.change == CHANGE_DELETED:
            raise LabWorkerChangePolicyError(
                f"deletion is forbidden by change policy: {change.path!r}"
            )

        if change.change != target.operation:
            raise LabWorkerChangePolicyError(
                f"physical operation differs from policy: {change.path!r}"
            )

        if (
            change.after is None
            or change.after.kind != ENTRY_FILE
        ):
            raise LabWorkerChangePolicyError(
                f"final target is not a regular file: {change.path!r}"
            )

        if change.after.mode != target.final_mode:
            raise LabWorkerChangePolicyError(
                f"final mode differs from policy: {change.path!r}"
            )

        if (
            change.after.bytes is None
            or change.after.bytes
            > target.max_final_bytes
        ):
            raise LabWorkerChangePolicyError(
                f"final byte count exceeds policy: {change.path!r}"
            )

        if target.operation == OPERATION_ADD:
            if change.before is not None:
                raise LabWorkerChangePolicyError(
                    f"ADDED physical change unexpectedly has before state: {change.path!r}"
                )

        else:
            if (
                change.before is None
                or change.before.kind != ENTRY_FILE
            ):
                raise LabWorkerChangePolicyError(
                    f"MODIFIED physical change lacks regular before state: {change.path!r}"
                )

            if (
                change.before.bytes,
                change.before.sha256,
                change.before.mode,
            ) != (
                target.before_bytes,
                target.before_sha256,
                target.before_mode,
            ):
                raise LabWorkerChangePolicyError(
                    f"MODIFIED physical before state differs from policy: {change.path!r}"
                )

    if require_complete:
        expected_paths = set(
            targets_by_path
        )

        if observed_paths != expected_paths:
            raise LabWorkerChangePolicyError(
                "successful physical diff does not exactly match policy targets"
            )

    return trusted_policy
