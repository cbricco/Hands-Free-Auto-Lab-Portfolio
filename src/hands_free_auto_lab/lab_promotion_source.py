"""Pure normalization of validated promotion-candidate evidence.

A normalized source is evidence only.  It grants no approval or authority and
has no repository, filesystem, process, transaction, or recovery capability.
Unavailable legacy evidence remains unavailable rather than being invented.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath

from .lab_coding_candidate import (
    LabCodingCandidate,
    validate_lab_coding_candidate,
)
from .lab_promotion_candidate import (
    PROMOTION_CANDIDATE_COMPONENT,
    PROMOTION_CANDIDATE_SCHEMA_VERSION,
    LabPromotionCandidate,
    LabPromotionCandidateFile,
)


SOURCE_KIND_LEGACY_WRITE_ACTION = "legacy_write_action"
SOURCE_KIND_CODING_CANDIDATE = "coding_candidate"
SOURCE_OPERATION_LEGACY_WRITE_FILE = "WRITE_FILE"

_ALLOWED_SOURCE_KINDS = frozenset({
    SOURCE_KIND_LEGACY_WRITE_ACTION,
    SOURCE_KIND_CODING_CANDIDATE,
})
_ALLOWED_OPERATIONS = frozenset({
    SOURCE_OPERATION_LEGACY_WRITE_FILE,
    "ADDED",
    "MODIFIED",
})
_HEX_LOWER = frozenset("0123456789abcdef")


class LabPromotionSourceError(ValueError):
    """Raised when promotion-source evidence is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class LabPromotionSourceFile:
    operation: str
    path: str
    before_bytes: int | None
    before_sha256: str | None
    before_mode: int | None
    final_bytes: int
    final_sha256: str
    final_mode: int | None
    final_content: str
    source_kind: str
    source_id: str


@dataclass(frozen=True, slots=True)
class LabPromotionSource:
    files: tuple[LabPromotionSourceFile, ...]


def _fail(message: str) -> None:
    raise LabPromotionSourceError(message)


def _identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX_LOWER for character in value)
    ):
        _fail(f"{name} must be a lowercase 64-character hex identifier")
    return value


def _count(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        _fail(f"{name} must be an exact non-negative integer")
    return value


def _mode(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 0o777:
        _fail(f"{name} must be an exact plain mode from 0000 through 0777")
    if value & 0o111:
        _fail(f"{name} must not be executable")
    if value & 0o022:
        _fail(f"{name} must not be group- or other-writable")
    return value


def _path(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        _fail("source path must be a non-empty string without NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise LabPromotionSourceError(
            "source path must be UTF-8 encodable"
        ) from exc
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or value == "."
        or ".." in parsed.parts
        or str(parsed) != value
    ):
        _fail("source path must be canonical and relative")
    return value


def _validate_legacy_candidate(candidate: LabPromotionCandidate) -> None:
    if candidate.component != PROMOTION_CANDIDATE_COMPONENT:
        _fail("legacy candidate component mismatch")
    if candidate.schema_version != PROMOTION_CANDIDATE_SCHEMA_VERSION:
        _fail("legacy candidate schema version mismatch")
    _identifier(candidate.session_id, "legacy session_id")
    _count(candidate.workspace_device, "legacy workspace_device")
    if _count(candidate.workspace_inode, "legacy workspace_inode") < 1:
        _fail("legacy workspace_inode must be positive")
    if candidate.controller_status != "done":
        _fail("legacy candidate controller status is not done")
    generation = _count(candidate.workspace_generation, "legacy workspace_generation")
    if generation < 1:
        _fail("legacy workspace_generation must be positive")
    tested = _count(candidate.tested_generation, "legacy tested_generation")
    if tested != generation:
        _fail("legacy candidate tested generation is stale")
    if candidate.acceptance_generation is not None:
        acceptance = _count(
            candidate.acceptance_generation, "legacy acceptance_generation"
        )
        if acceptance != generation:
            _fail("legacy candidate acceptance generation is stale")
    if type(candidate.files) is not tuple or not candidate.files:
        _fail("legacy candidate files must be a non-empty tuple")
    paths: list[str] = []
    for item in candidate.files:
        if type(item) is not LabPromotionCandidateFile:
            _fail("legacy candidate files must have the exact file type")
        paths.append(_path(item.path))
        _count(item.bytes, "legacy file bytes")
        _identifier(item.sha256, "legacy file sha256")
        _identifier(item.latest_write_action_id, "latest_write_action_id")
        if type(item.content) is not str:
            _fail("legacy file content must be an exact string")
        try:
            raw = item.content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise LabPromotionSourceError(
                "legacy file content must be UTF-8 encodable"
            ) from exc
        if len(raw) != item.bytes:
            _fail(f"legacy content byte count mismatch for {item.path!r}")
        if hashlib.sha256(raw).hexdigest() != item.sha256:
            _fail(f"legacy content digest mismatch for {item.path!r}")
    if len(paths) != len(set(paths)):
        _fail("legacy candidate contains duplicate paths")


def validate_lab_promotion_source(source: object) -> LabPromotionSource:
    """Deterministically validate normalized evidence without external state."""
    try:
        if type(source) is not LabPromotionSource:
            _fail("source must have the exact LabPromotionSource type")
        if type(source.files) is not tuple or not source.files:
            _fail("source files must be a non-empty tuple")

        paths: list[str] = []
        for item in source.files:
            if type(item) is not LabPromotionSourceFile:
                _fail("source file must have the exact LabPromotionSourceFile type")
            if type(item.operation) is not str or item.operation not in _ALLOWED_OPERATIONS:
                _fail("source file operation is unsupported")
            path = _path(item.path)
            paths.append(path)
            if type(item.source_kind) is not str or item.source_kind not in _ALLOWED_SOURCE_KINDS:
                _fail("source file provenance kind is unsupported")
            source_id = _identifier(item.source_id, "source_id")

            if item.source_kind == SOURCE_KIND_LEGACY_WRITE_ACTION:
                if item.operation != SOURCE_OPERATION_LEGACY_WRITE_FILE:
                    _fail("legacy source operation must be WRITE_FILE")
                if (item.before_bytes, item.before_sha256, item.before_mode) != (None, None, None):
                    _fail("legacy source must not claim unavailable before evidence")
                if item.final_mode is not None:
                    _fail("legacy source must not claim an unavailable final mode")
            else:
                if item.operation == SOURCE_OPERATION_LEGACY_WRITE_FILE:
                    _fail("coding source operation must be ADDED or MODIFIED")
                if item.operation == "ADDED":
                    if (item.before_bytes, item.before_sha256, item.before_mode) != (None, None, None):
                        _fail("ADDED source must not contain before evidence")
                    _mode(item.final_mode, "final_mode")
                else:
                    _count(item.before_bytes, "before_bytes")
                    _identifier(item.before_sha256, "before_sha256")
                    _mode(item.before_mode, "before_mode")
                    _mode(item.final_mode, "final_mode")

            _count(item.final_bytes, "final_bytes")
            _identifier(item.final_sha256, "final_sha256")
            if type(item.final_content) is not str:
                _fail("final_content must be an exact string")
            try:
                raw = item.final_content.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise LabPromotionSourceError(
                    "final_content must be UTF-8 encodable"
                ) from exc
            if len(raw) != item.final_bytes:
                _fail(f"final content byte count mismatch for {path!r}")
            if hashlib.sha256(raw).hexdigest() != item.final_sha256:
                _fail(f"final content digest mismatch for {path!r}")
            if not source_id:
                _fail("source_id must not be empty")

        if len(paths) != len(set(paths)):
            _fail("source contains duplicate paths")
        if tuple(paths) != tuple(sorted(paths)):
            _fail("source files are not in canonical path order")
        return source
    except LabPromotionSourceError:
        raise
    except Exception as exc:
        raise LabPromotionSourceError(
            f"promotion source validation failed: {exc}"
        ) from exc


def normalize_lab_promotion_source(candidate: object) -> LabPromotionSource:
    """Normalize validated candidate evidence without creating authority."""
    try:
        files: list[LabPromotionSourceFile]
        if type(candidate) is LabPromotionCandidate:
            _validate_legacy_candidate(candidate)
            files = []
            for item in sorted(candidate.files, key=lambda value: value.path):
                if type(item) is not LabPromotionCandidateFile:
                    _fail("legacy candidate files must have the exact file type")
                files.append(LabPromotionSourceFile(
                    operation=SOURCE_OPERATION_LEGACY_WRITE_FILE,
                    path=item.path,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                    final_bytes=item.bytes,
                    final_sha256=item.sha256,
                    final_mode=None,
                    final_content=item.content,
                    source_kind=SOURCE_KIND_LEGACY_WRITE_ACTION,
                    source_id=item.latest_write_action_id,
                ))
        elif type(candidate) is LabCodingCandidate:
            trusted = validate_lab_coding_candidate(candidate)
            files = [
                LabPromotionSourceFile(
                    operation=item.operation,
                    path=item.path,
                    before_bytes=item.before_bytes,
                    before_sha256=item.before_sha256,
                    before_mode=item.before_mode,
                    final_bytes=item.final_bytes,
                    final_sha256=item.final_sha256,
                    final_mode=item.final_mode,
                    final_content=item.content,
                    source_kind=SOURCE_KIND_CODING_CANDIDATE,
                    source_id=trusted.candidate_id,
                )
                for item in trusted.files
            ]
        else:
            raise TypeError(
                "candidate must have the exact LabPromotionCandidate or "
                "LabCodingCandidate type"
            )
        return validate_lab_promotion_source(LabPromotionSource(files=tuple(files)))
    except (LabPromotionSourceError, TypeError):
        raise
    except Exception as exc:
        raise LabPromotionSourceError(
            f"cannot normalize promotion source: {exc}"
        ) from exc


build_lab_promotion_source = normalize_lab_promotion_source
