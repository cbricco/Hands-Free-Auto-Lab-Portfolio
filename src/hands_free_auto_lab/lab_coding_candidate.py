"""Immutable, independently verified evidence for one coding-job result.

Candidates are evidence only. This module grants no promotion or approval
authority and deliberately has no process, network, Git, or write executor.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import importlib
import json
from pathlib import PurePosixPath

from .lab_action import build_lab_action
from .lab_read_file import execute_lab_read_file
from .lab_worker import (
    STATUS_COMPLETED, LabWorkerRequest, LabWorkerResult,
    validate_lab_worker_request, validate_lab_worker_result,
)
from .lab_worker_job import WORKER_JOB_COMPONENT, WORKER_JOB_SCHEMA_VERSION, LabWorkerJobRecord
from .lab_worker_change_policy import (
    LabWorkerChangePolicy,
    validate_lab_worker_change_policy,
    validate_lab_worker_change_policy_before,
    validate_lab_worker_change_policy_diff,
)
from .lab_workspace import validate_lab_workspace
from .lab_workspace_snapshot import (
    CHANGE_ADDED, CHANGE_DELETED, CHANGE_MODIFIED, ENTRY_FILE,
    capture_lab_workspace_snapshot, diff_lab_workspace_snapshots,
    validate_lab_workspace_snapshot,
)

CANDIDATE_COMPONENT = "hands-free-auto-lab-coding-candidate-v1"
CANDIDATE_SCHEMA_VERSION = 1
LAB_CODING_CANDIDATE_COMPONENT = CANDIDATE_COMPONENT
LAB_CODING_CANDIDATE_SCHEMA_VERSION = CANDIDATE_SCHEMA_VERSION
_CANDIDATE_ID_DOMAIN = b"hands-free-auto-lab-coding-candidate-id-v1\x00"

class LabCodingCandidateError(RuntimeError):
    """Trustworthy candidate evidence could not be established."""

@dataclass(frozen=True, slots=True)
class LabCodingCandidateFile:
    operation: str
    path: str
    before_bytes: int | None
    before_sha256: str | None
    before_mode: int | None
    final_bytes: int
    final_sha256: str
    final_mode: int
    content: str

@dataclass(frozen=True, slots=True)
class LabCodingCandidate:
    component: str
    schema_version: int
    candidate_id: str
    workspace_session_id: str
    workspace_device: int
    workspace_inode: int
    before_snapshot_id: str
    after_snapshot_id: str
    physical_diff_id: str
    files: tuple[LabCodingCandidateFile, ...]

def _exact_coding_record_type() -> type:
    module = importlib.import_module(".lab_coding_job", __package__)
    return module.LabCodingJobRecord

def _candidate_identity(candidate: LabCodingCandidate) -> dict[str, object]:
    return {
        "component": candidate.component, "schema_version": candidate.schema_version,
        "workspace_session_id": candidate.workspace_session_id,
        "workspace_device": candidate.workspace_device, "workspace_inode": candidate.workspace_inode,
        "before_snapshot_id": candidate.before_snapshot_id, "after_snapshot_id": candidate.after_snapshot_id,
        "physical_diff_id": candidate.physical_diff_id,
        "files": [{
            "operation": f.operation, "path": f.path, "before_bytes": f.before_bytes,
            "before_sha256": f.before_sha256, "before_mode": f.before_mode,
            "final_bytes": f.final_bytes, "final_sha256": f.final_sha256,
            "final_mode": f.final_mode, "content": f.content,
        } for f in candidate.files],
    }

def _candidate_id(candidate: LabCodingCandidate) -> str:
    encoded = json.dumps(_candidate_identity(candidate), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(_CANDIDATE_ID_DOMAIN + encoded).hexdigest()

def _workspace_identity(value: object) -> tuple[object, object, object]:
    return (value.workspace_session_id, value.workspace_device, value.workspace_inode)

def _require_identifier(value: object, *, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise LabCodingCandidateError(f"{name} must be a lowercase 64-character hex identifier")
    return value

def _require_exact_nonnegative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise LabCodingCandidateError(f"{name} must be an exact non-negative integer")
    return value

def _require_mode(value: object, *, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 0o7777:
        raise LabCodingCandidateError(f"{name} must be an exact valid mode")
    return value

def _require_relative_path(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise LabCodingCandidateError("candidate file path must be a non-empty string without NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise LabCodingCandidateError("candidate file path must be UTF-8 encodable") from exc
    path = PurePosixPath(value)
    if path.is_absolute() or value == "." or ".." in path.parts or str(path) != value:
        raise LabCodingCandidateError("candidate file path must be canonical and relative")
    return value

def validate_lab_coding_candidate(candidate: object) -> LabCodingCandidate:
    """Validate stored candidate evidence without consulting external state."""
    try:
        if type(candidate) is not LabCodingCandidate:
            raise LabCodingCandidateError("candidate must have the exact LabCodingCandidate type")
        if type(candidate.component) is not str or candidate.component != CANDIDATE_COMPONENT:
            raise LabCodingCandidateError("candidate component mismatch")
        if type(candidate.schema_version) is not int or candidate.schema_version != CANDIDATE_SCHEMA_VERSION:
            raise LabCodingCandidateError("unsupported candidate schema version")
        supplied_id = _require_identifier(candidate.candidate_id, name="candidate_id")
        _require_identifier(candidate.workspace_session_id, name="workspace_session_id")
        _require_exact_nonnegative_int(candidate.workspace_device, name="workspace_device")
        _require_exact_nonnegative_int(candidate.workspace_inode, name="workspace_inode")
        _require_identifier(candidate.before_snapshot_id, name="before_snapshot_id")
        _require_identifier(candidate.after_snapshot_id, name="after_snapshot_id")
        _require_identifier(candidate.physical_diff_id, name="physical_diff_id")
        if type(candidate.files) is not tuple or not candidate.files:
            raise LabCodingCandidateError("candidate files must be a non-empty tuple")
        paths: list[str] = []
        for item in candidate.files:
            if type(item) is not LabCodingCandidateFile:
                raise LabCodingCandidateError("candidate file must have the exact LabCodingCandidateFile type")
            paths.append(_require_relative_path(item.path))
            if type(item.operation) is not str or item.operation not in (CHANGE_ADDED, CHANGE_MODIFIED):
                raise LabCodingCandidateError("candidate file operation must be ADDED or MODIFIED")
            if item.operation == CHANGE_ADDED:
                if (item.before_bytes, item.before_sha256, item.before_mode) != (None, None, None):
                    raise LabCodingCandidateError("ADDED candidate file must not have before evidence")
            else:
                _require_exact_nonnegative_int(item.before_bytes, name="before_bytes")
                _require_identifier(item.before_sha256, name="before_sha256")
                _require_mode(item.before_mode, name="before_mode")
            _require_exact_nonnegative_int(item.final_bytes, name="final_bytes")
            _require_identifier(item.final_sha256, name="final_sha256")
            final_mode = _require_mode(item.final_mode, name="final_mode")
            if final_mode & 0o111:
                raise LabCodingCandidateError("executable final files are refused")
            if final_mode & 0o022:
                raise LabCodingCandidateError("group- or other-writable final files are refused")
            if type(item.content) is not str:
                raise LabCodingCandidateError("candidate file content must be an exact string")
            try:
                content = item.content.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise LabCodingCandidateError("candidate file content must be UTF-8 encodable") from exc
            if len(content) != item.final_bytes:
                raise LabCodingCandidateError("candidate file content byte count mismatch")
            if hashlib.sha256(content).hexdigest() != item.final_sha256:
                raise LabCodingCandidateError("candidate file content SHA-256 mismatch")
        if len(paths) != len(set(paths)):
            raise LabCodingCandidateError("candidate contains duplicate paths")
        if tuple(paths) != tuple(sorted(paths)):
            raise LabCodingCandidateError("candidate files are not in canonical path order")
        if supplied_id != _candidate_id(candidate):
            raise LabCodingCandidateError("candidate_id does not match authoritative evidence")
        return candidate
    except LabCodingCandidateError:
        raise
    except Exception as exc:
        raise LabCodingCandidateError(f"candidate validation failed: {exc}") from exc


_CANDIDATE_WIRE_FIELDS = frozenset({
    "component", "schema_version", "candidate_id", "workspace_session_id",
    "workspace_device", "workspace_inode", "before_snapshot_id",
    "after_snapshot_id", "physical_diff_id", "files",
})
_CANDIDATE_FILE_WIRE_FIELDS = frozenset({
    "operation", "path", "before_bytes", "before_sha256", "before_mode",
    "final_bytes", "final_sha256", "final_mode", "content",
})


def lab_coding_candidate_to_wire(candidate: object) -> dict[str, object]:
    """Return a fresh JSON-native representation of validated candidate evidence."""
    trusted = validate_lab_coding_candidate(candidate)
    return {
        "component": trusted.component,
        "schema_version": trusted.schema_version,
        "candidate_id": trusted.candidate_id,
        "workspace_session_id": trusted.workspace_session_id,
        "workspace_device": trusted.workspace_device,
        "workspace_inode": trusted.workspace_inode,
        "before_snapshot_id": trusted.before_snapshot_id,
        "after_snapshot_id": trusted.after_snapshot_id,
        "physical_diff_id": trusted.physical_diff_id,
        "files": [
            {
                "operation": item.operation,
                "path": item.path,
                "before_bytes": item.before_bytes,
                "before_sha256": item.before_sha256,
                "before_mode": item.before_mode,
                "final_bytes": item.final_bytes,
                "final_sha256": item.final_sha256,
                "final_mode": item.final_mode,
                "content": item.content,
            }
            for item in trusted.files
        ],
    }


def lab_coding_candidate_from_wire(value: object) -> LabCodingCandidate:
    """Reconstruct and validate candidate evidence from its exact wire schema."""
    try:
        if type(value) is not dict:
            raise LabCodingCandidateError("candidate wire value must be an exact object")
        if frozenset(value) != _CANDIDATE_WIRE_FIELDS:
            raise LabCodingCandidateError("candidate wire fields do not exactly match schema")
        files_value = value["files"]
        if type(files_value) is not list or not files_value:
            raise LabCodingCandidateError("candidate wire files must be a non-empty exact list")
        files: list[LabCodingCandidateFile] = []
        for item in files_value:
            if type(item) is not dict:
                raise LabCodingCandidateError("candidate wire file must be an exact object")
            if frozenset(item) != _CANDIDATE_FILE_WIRE_FIELDS:
                raise LabCodingCandidateError("candidate wire file fields do not exactly match schema")
            files.append(LabCodingCandidateFile(
                operation=item["operation"], path=item["path"],
                before_bytes=item["before_bytes"], before_sha256=item["before_sha256"],
                before_mode=item["before_mode"], final_bytes=item["final_bytes"],
                final_sha256=item["final_sha256"], final_mode=item["final_mode"],
                content=item["content"],
            ))
        candidate = LabCodingCandidate(
            component=value["component"], schema_version=value["schema_version"],
            candidate_id=value["candidate_id"],
            workspace_session_id=value["workspace_session_id"],
            workspace_device=value["workspace_device"],
            workspace_inode=value["workspace_inode"],
            before_snapshot_id=value["before_snapshot_id"],
            after_snapshot_id=value["after_snapshot_id"],
            physical_diff_id=value["physical_diff_id"], files=tuple(files),
        )
        return validate_lab_coding_candidate(candidate)
    except LabCodingCandidateError:
        raise
    except Exception as exc:
        raise LabCodingCandidateError(f"candidate wire decoding failed: {exc}") from exc


def lab_coding_candidate_to_bytes(candidate: object) -> bytes:
    """Encode validated candidate evidence as canonical UTF-8 JSON."""
    value = lab_coding_candidate_to_wire(candidate)
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8", errors="strict")


def lab_coding_candidate_from_bytes(record_bytes: object) -> LabCodingCandidate:
    """Decode only the unique canonical UTF-8 JSON representation."""
    if type(record_bytes) is not bytes:
        raise LabCodingCandidateError("candidate record must have the exact bytes type")
    try:
        text = record_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LabCodingCandidateError("candidate record is not valid UTF-8") from exc
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LabCodingCandidateError("candidate record is not valid JSON") from exc
    if type(value) is not dict:
        raise LabCodingCandidateError("candidate record must be a JSON object")
    try:
        canonical = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False) + "\n").encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LabCodingCandidateError("candidate record cannot be canonically encoded") from exc
    if canonical != record_bytes:
        raise LabCodingCandidateError("candidate record is not canonical JSON")
    return lab_coding_candidate_from_wire(value)


def _candidate_seed_entries(record: object) -> dict[str, object] | None:
    """
    Validate supported seed provenance without consulting its source again.

    The worker-job before snapshot remains physical authority. Seed evidence
    may only supply source provenance when its workspace materialization
    exactly matches that authoritative before snapshot.
    """
    seed = record.seed

    if seed is None:
        return None

    legacy_module = importlib.import_module(
        ".lab_workspace_seed",
        __package__,
    )
    committed_module = importlib.import_module(
        ".lab_committed_workspace_seed",
        __package__,
    )

    committed = False

    if type(seed) is legacy_module.LabWorkspaceSeedRecord:
        if (
            seed.component
            != legacy_module.WORKSPACE_SEED_COMPONENT
            or seed.schema_version
            != legacy_module.WORKSPACE_SEED_SCHEMA_VERSION
        ):
            raise LabCodingCandidateError(
                "workspace-seed component or schema mismatch"
            )

        entry_type = legacy_module.LabWorkspaceSeedEntry

    elif type(seed) is committed_module.LabCommittedWorkspaceSeedRecord:
        committed = True

        if (
            seed.component
            != committed_module.COMMITTED_WORKSPACE_SEED_COMPONENT
            or seed.schema_version
            != committed_module.COMMITTED_WORKSPACE_SEED_SCHEMA_VERSION
        ):
            raise LabCodingCandidateError(
                "committed workspace-seed component or schema mismatch"
            )

        if (
            type(seed.repository_path) is not str
            or not seed.repository_path
            or "\x00" in seed.repository_path
        ):
            raise LabCodingCandidateError(
                "committed workspace-seed repository_path is malformed"
            )

        repository_path = PurePosixPath(seed.repository_path)

        if (
            not repository_path.is_absolute()
            or str(repository_path) != seed.repository_path
        ):
            raise LabCodingCandidateError(
                "committed workspace-seed repository_path is not canonical"
            )

        for name, value in (
            ("repository_device", seed.repository_device),
            ("repository_inode", seed.repository_inode),
            ("git_device", seed.git_device),
            ("git_inode", seed.git_inode),
        ):
            _require_exact_nonnegative_int(
                value,
                name=f"committed seed {name}",
            )

        if (
            type(seed.branch) is not str
            or not seed.branch
            or "\x00" in seed.branch
        ):
            raise LabCodingCandidateError(
                "committed workspace-seed branch is malformed"
            )

        if (
            type(seed.commit_oid) is not str
            or len(seed.commit_oid) != 40
            or any(
                character not in "0123456789abcdef"
                for character in seed.commit_oid
            )
        ):
            raise LabCodingCandidateError(
                "committed workspace-seed commit_oid is malformed"
            )

        if seed.object_format != "sha1":
            raise LabCodingCandidateError(
                "committed workspace-seed object format is unsupported"
            )

        entry_type = (
            committed_module.LabCommittedWorkspaceSeedEntry
        )

    else:
        raise LabCodingCandidateError(
            "seed must have an exact supported workspace-seed record type"
        )

    if seed.workspace != record.workspace:
        raise LabCodingCandidateError(
            "workspace-seed workspace does not match coding record workspace"
        )

    seed_entries: dict[str, object] = {}
    total_bytes = 0

    for entry in seed.entries:
        if type(entry) is not entry_type:
            raise LabCodingCandidateError(
                "seed entry does not have the exact expected type"
            )

        path = _require_relative_path(
            entry.relative_path
        )

        if path in seed_entries:
            raise LabCodingCandidateError(
                "workspace seed contains duplicate paths"
            )

        size = _require_exact_nonnegative_int(
            entry.size,
            name="seed size",
        )

        _require_identifier(
            entry.sha256,
            name="seed sha256",
        )
        _require_mode(
            entry.mode,
            name="seed mode",
        )
        _require_mode(
            entry.source_mode,
            name="seed source_mode",
        )

        if committed:
            if (
                type(entry.blob_oid) is not str
                or len(entry.blob_oid) != 40
                or any(
                    character not in "0123456789abcdef"
                    for character in entry.blob_oid
                )
            ):
                raise LabCodingCandidateError(
                    "committed seed blob_oid is malformed"
                )

            if entry.git_mode not in (
                "100644",
                "100755",
            ):
                raise LabCodingCandidateError(
                    "committed seed git_mode is unsupported"
                )

            expected_source_mode = (
                0o755
                if entry.git_mode == "100755"
                else 0o644
            )
            expected_workspace_mode = (
                0o700
                if entry.git_mode == "100755"
                else 0o600
            )

            if entry.source_mode != expected_source_mode:
                raise LabCodingCandidateError(
                    "committed seed source_mode does not match git_mode"
                )

            if entry.mode != expected_workspace_mode:
                raise LabCodingCandidateError(
                    "committed seed workspace mode does not match git_mode"
                )

        total_bytes += size
        seed_entries[path] = entry

    if committed:
        if (
            type(seed.total_bytes) is not int
            or seed.total_bytes < 0
            or seed.total_bytes != total_bytes
        ):
            raise LabCodingCandidateError(
                "committed workspace-seed total_bytes mismatch"
            )

    return seed_entries


def build_lab_coding_candidate(record: object, *, workspace_parent: str) -> LabCodingCandidate:
    """Build evidence from independently checked physical workspace truth."""
    try:
        record_type = _exact_coding_record_type()
        if type(record) is not record_type:
            raise LabCodingCandidateError("record must have the exact LabCodingJobRecord type")
        coding_module = importlib.import_module(".lab_coding_job", __package__)
        if record.component != coding_module.CODING_JOB_COMPONENT:
            raise LabCodingCandidateError("coding-job component mismatch")
        if record.schema_version != coding_module.CODING_JOB_SCHEMA_VERSION:
            raise LabCodingCandidateError("unsupported coding-job schema version")
        if type(workspace_parent) is not str or workspace_parent != record.workspace.parent:
            raise LabCodingCandidateError("workspace_parent must exactly match the record workspace")
        workspace = validate_lab_workspace(workspace_parent, record.workspace)
        seed_entries = _candidate_seed_entries(record)
        job = record.worker_job
        if type(job) is not LabWorkerJobRecord:
            raise LabCodingCandidateError("worker_job must have the exact LabWorkerJobRecord type")
        if job.component != WORKER_JOB_COMPONENT or job.schema_version != WORKER_JOB_SCHEMA_VERSION:
            raise LabCodingCandidateError("worker-job component or schema mismatch")
        if type(job.change_policy) is not LabWorkerChangePolicy:
            raise LabCodingCandidateError("worker-job change policy is required")
        policy = validate_lab_worker_change_policy(
            job.change_policy
        )
        if type(job.request) is not LabWorkerRequest or type(job.result) is not LabWorkerResult:
            raise LabCodingCandidateError("worker request and result must have exact contract types")
        request = validate_lab_worker_request(job.request)
        if request.change_policy_id != policy.policy_id:
            raise LabCodingCandidateError(
                "worker request policy id does not match worker-job policy"
            )
        result = validate_lab_worker_result(job.result, request=request)
        if result.status != STATUS_COMPLETED or not job.succeeded or not record.succeeded:
            raise LabCodingCandidateError("worker did not complete successfully")
        if request.workspace != workspace:
            raise LabCodingCandidateError("worker request workspace does not match record workspace")
        before = validate_lab_workspace_snapshot(job.before_snapshot)
        after = validate_lab_workspace_snapshot(job.after_snapshot)
        validate_lab_worker_change_policy_before(
            policy,
            before_snapshot=before,
        )
        identity = (record.workspace.session_id, record.workspace.device, record.workspace.inode)
        if _workspace_identity(before) != identity or _workspace_identity(after) != identity:
            raise LabCodingCandidateError("snapshot workspace identity mismatch")
        derived = diff_lab_workspace_snapshots(before, after)
        if job.physical_diff != derived:
            raise LabCodingCandidateError("recorded physical diff does not match independently derived diff")
        if _workspace_identity(derived) != identity:
            raise LabCodingCandidateError("physical diff workspace identity mismatch")
        validate_lab_worker_change_policy_diff(
            policy,
            before_snapshot=before,
            after_snapshot=after,
            physical_diff=derived,
            require_complete=True,
        )
        if not derived.entries:
            raise LabCodingCandidateError("empty physical diff is not a candidate")
        files: list[LabCodingCandidateFile] = []
        for change in derived.entries:
            if change.change == CHANGE_DELETED:
                raise LabCodingCandidateError("deleted entries are not supported")
            if change.change not in (CHANGE_ADDED, CHANGE_MODIFIED):
                raise LabCodingCandidateError("unsupported physical change operation")
            if change.after is None or change.after.kind != ENTRY_FILE:
                raise LabCodingCandidateError("final changed entry must be a regular file")
            if change.change == CHANGE_ADDED and change.before is not None:
                raise LabCodingCandidateError("ADDED entry unexpectedly has before evidence")
            if change.change == CHANGE_MODIFIED and (change.before is None or change.before.kind != ENTRY_FILE):
                raise LabCodingCandidateError("MODIFIED requires regular-file before evidence; directory-to-file replacement refused")
            final = change.after
            if final.mode & 0o111:
                raise LabCodingCandidateError("executable final files are refused")
            if final.mode & 0o022:
                raise LabCodingCandidateError("group- or other-writable final files are refused")
            read = execute_lab_read_file(build_lab_action(kind="READ_FILE", path=change.path),
                                         workspace_parent=workspace_parent, workspace=record.workspace)
            if (read.workspace_device, read.workspace_inode) != (after.workspace_device, after.workspace_inode):
                raise LabCodingCandidateError("reread workspace device/inode mismatch")
            if read.bytes_read != final.bytes or read.sha256 != final.sha256:
                raise LabCodingCandidateError("reread bytes or SHA-256 do not match snapshot evidence")
            old = change.before
            seed_entry = None if seed_entries is None else seed_entries.get(change.path)
            if change.change == CHANGE_ADDED and seed_entry is not None:
                raise LabCodingCandidateError("ADDED entry unexpectedly claims workspace-seed provenance")
            before_mode = None if old is None else old.mode
            if change.change == CHANGE_MODIFIED and seed_entry is not None:
                if (seed_entry.size, seed_entry.sha256, seed_entry.mode) != (
                        old.bytes, old.sha256, old.mode):
                    raise LabCodingCandidateError(
                        "workspace-seed evidence does not match physical before snapshot")
                before_mode = seed_entry.source_mode
            files.append(LabCodingCandidateFile(
                operation=change.change, path=change.path,
                before_bytes=None if old is None else old.bytes,
                before_sha256=None if old is None else old.sha256,
                before_mode=before_mode,
                final_bytes=read.bytes_read, final_sha256=read.sha256,
                final_mode=final.mode, content=read.content))
        recaptured = capture_lab_workspace_snapshot(workspace_parent=workspace_parent,
                                                     workspace=record.workspace)
        if recaptured != after:
            raise LabCodingCandidateError("complete final workspace no longer matches recorded after snapshot")
        provisional = LabCodingCandidate(
            component=CANDIDATE_COMPONENT, schema_version=CANDIDATE_SCHEMA_VERSION,
            candidate_id="0" * 64, workspace_session_id=after.workspace_session_id,
            workspace_device=after.workspace_device, workspace_inode=after.workspace_inode,
            before_snapshot_id=before.snapshot_id, after_snapshot_id=after.snapshot_id,
            physical_diff_id=derived.diff_id, files=tuple(files))
        return LabCodingCandidate(
            component=provisional.component, schema_version=provisional.schema_version,
            candidate_id=_candidate_id(provisional), workspace_session_id=provisional.workspace_session_id,
            workspace_device=provisional.workspace_device, workspace_inode=provisional.workspace_inode,
            before_snapshot_id=provisional.before_snapshot_id,
            after_snapshot_id=provisional.after_snapshot_id,
            physical_diff_id=provisional.physical_diff_id, files=provisional.files)
    except LabCodingCandidateError:
        raise
    except Exception as exc:
        raise LabCodingCandidateError(f"candidate validation failed: {exc}") from exc
