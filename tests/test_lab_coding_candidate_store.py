from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

# This task fixture contains the verified candidate module but omits its
# execution-time collaborators.  Supply import-only stand-ins so these tests
# can exercise the real validator and codec without granting those authorities.
import sys
import types


def _stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules.setdefault(name, module)


_stub("hands_free_auto_lab.lab_action", build_lab_action=lambda *a, **k: None)
_stub("hands_free_auto_lab.lab_read_file", execute_lab_read_file=lambda *a, **k: None)
_stub(
    "hands_free_auto_lab.lab_worker",
    STATUS_COMPLETED="COMPLETED", LabWorkerRequest=object, LabWorkerResult=object,
    validate_lab_worker_request=lambda value: value,
    validate_lab_worker_result=lambda value: value,
)
_stub(
    "hands_free_auto_lab.lab_worker_job",
    WORKER_JOB_COMPONENT="worker", WORKER_JOB_SCHEMA_VERSION=1,
    LabWorkerJobRecord=type("LabWorkerJobRecord", (), {}),
)
_stub("hands_free_auto_lab.lab_workspace", validate_lab_workspace=lambda value: value)
_stub(
    "hands_free_auto_lab.lab_workspace_snapshot",
    CHANGE_ADDED="ADDED", CHANGE_DELETED="DELETED", CHANGE_MODIFIED="MODIFIED",
    ENTRY_FILE="FILE", capture_lab_workspace_snapshot=lambda *a, **k: None,
    diff_lab_workspace_snapshots=lambda *a, **k: None,
    validate_lab_workspace_snapshot=lambda value: value,
)

from hands_free_auto_lab.lab_coding_candidate import (
    CANDIDATE_COMPONENT,
    CANDIDATE_SCHEMA_VERSION,
    LabCodingCandidate,
    LabCodingCandidateFile,
    lab_coding_candidate_to_bytes,
)
from hands_free_auto_lab.lab_coding_candidate_store import (
    MAX_RECORD_BYTES,
    LabCodingCandidateStoreError,
    initialize_lab_coding_candidate_store,
    load_lab_coding_candidate,
    persist_lab_coding_candidate,
)
import hands_free_auto_lab.lab_coding_candidate_store as store


class LabCodingCandidateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "store"
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)

    def candidate(self, content: str = "hello\n") -> LabCodingCandidate:
        raw = content.encode()
        item = LabCodingCandidateFile(
            operation="ADDED", path="answer.txt", before_bytes=None,
            before_sha256=None, before_mode=None, final_bytes=len(raw),
            final_sha256=hashlib.sha256(raw).hexdigest(), final_mode=0o644,
            content=content,
        )
        prototype = LabCodingCandidate(
            component=CANDIDATE_COMPONENT, schema_version=CANDIDATE_SCHEMA_VERSION,
            candidate_id="0" * 64, workspace_session_id="1" * 64,
            workspace_device=1, workspace_inode=2, before_snapshot_id="2" * 64,
            after_snapshot_id="3" * 64, physical_diff_id="4" * 64,
            files=(item,),
        )
        identity = {
            "component": prototype.component, "schema_version": prototype.schema_version,
            "workspace_session_id": prototype.workspace_session_id,
            "workspace_device": prototype.workspace_device,
            "workspace_inode": prototype.workspace_inode,
            "before_snapshot_id": prototype.before_snapshot_id,
            "after_snapshot_id": prototype.after_snapshot_id,
            "physical_diff_id": prototype.physical_diff_id,
            "files": [{
                "operation": item.operation, "path": item.path,
                "before_bytes": item.before_bytes, "before_sha256": item.before_sha256,
                "before_mode": item.before_mode, "final_bytes": item.final_bytes,
                "final_sha256": item.final_sha256, "final_mode": item.final_mode,
                "content": item.content,
            }],
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        identifier = hashlib.sha256(
            b"hands-free-auto-lab-coding-candidate-id-v1\x00" + encoded
        ).hexdigest()
        return replace(prototype, candidate_id=identifier)

    def initialize(self):
        return initialize_lab_coding_candidate_store(self.root)

    def test_initialize_exact_private_fixed_layout_and_existing_valid_layout(self):
        self.assertEqual(self.initialize(), self.root)
        self.assertEqual(set(os.listdir(self.root)), {".lock", "candidates"})
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / ".lock").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "candidates").stat().st_mode), 0o700)
        self.assertEqual(self.initialize(), self.root)

    def test_unexpected_root_entry_refused(self):
        (self.root / "surprise").write_text("x")
        with self.assertRaises(LabCodingCandidateStoreError): self.initialize()

    def test_nonprivate_root_mode_refused(self):
        os.chmod(self.root, 0o755)
        with self.assertRaises(LabCodingCandidateStoreError): self.initialize()

    def test_root_symlink_and_symlink_component_refused(self):
        real = self.base / "real"; real.mkdir(mode=0o700); os.chmod(real, 0o700)
        link = self.base / "link"; link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(LabCodingCandidateStoreError):
            initialize_lab_coding_candidate_store(link)
        parent = self.base / "parent"; parent.mkdir(mode=0o700); os.chmod(parent, 0o700)
        through = self.base / "through"; through.symlink_to(parent, target_is_directory=True)
        child = parent / "child"; child.mkdir(mode=0o700); os.chmod(child, 0o700)
        with self.assertRaises(LabCodingCandidateStoreError):
            initialize_lab_coding_candidate_store(through / "child")

    def test_persist_load_reopen_filename_permissions_and_link_count(self):
        self.initialize(); candidate = self.candidate()
        result = persist_lab_coding_candidate(self.root, candidate)
        self.assertEqual(result.name, candidate.candidate_id + ".json")
        self.assertEqual(load_lab_coding_candidate(self.root, candidate.candidate_id), candidate)
        self.initialize()
        self.assertEqual(load_lab_coding_candidate(self.root, candidate.candidate_id), candidate)
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        self.assertEqual(result.stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE(result.parent.stat().st_mode), 0o700)

    def test_duplicate_refused_and_completed_record_never_overwritten(self):
        self.initialize(); candidate = self.candidate()
        path = persist_lab_coding_candidate(self.root, candidate); before = path.read_bytes()
        with self.assertRaises(LabCodingCandidateStoreError):
            persist_lab_coding_candidate(self.root, candidate)
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_candidate_rejected_before_root_access(self):
        with mock.patch.object(store, "_require_root", side_effect=AssertionError("root accessed")):
            with self.assertRaises(LabCodingCandidateStoreError):
                persist_lab_coding_candidate(self.root, object())

    def test_invalid_candidate_id_rejected_before_root_access(self):
        with mock.patch.object(store, "_require_root", side_effect=AssertionError("root accessed")):
            with self.assertRaises(LabCodingCandidateStoreError):
                load_lab_coding_candidate(self.root, "A" * 64)

    def test_missing_candidate_refused(self):
        self.initialize()
        with self.assertRaises(LabCodingCandidateStoreError):
            load_lab_coding_candidate(self.root, "a" * 64)

    def test_noncanonical_and_tampered_evidence_refused(self):
        self.initialize(); candidate = self.candidate(); path = persist_lab_coding_candidate(self.root, candidate)
        value = json.loads(path.read_bytes()); path.write_text(json.dumps(value, indent=2)); os.chmod(path, 0o600)
        with self.assertRaises(LabCodingCandidateStoreError):
            load_lab_coding_candidate(self.root, candidate.candidate_id)
        path.write_bytes(lab_coding_candidate_to_bytes(candidate).replace(b"hello", b"jello")); os.chmod(path, 0o600)
        with self.assertRaises(LabCodingCandidateStoreError):
            load_lab_coding_candidate(self.root, candidate.candidate_id)

    def test_symlink_hardlink_and_unsafe_permissions_refused(self):
        for kind in ("symlink", "hardlink", "mode"):
            with self.subTest(kind=kind):
                other = self.base / kind; other.mkdir(mode=0o700); os.chmod(other, 0o700)
                initialize_lab_coding_candidate_store(other); candidate = self.candidate(kind)
                path = persist_lab_coding_candidate(other, candidate)
                if kind == "symlink":
                    path.unlink(); path.symlink_to(self.base / "missing")
                elif kind == "hardlink":
                    os.link(path, self.base / "alias")
                else:
                    os.chmod(path, 0o644)
                with self.assertRaises(LabCodingCandidateStoreError):
                    load_lab_coding_candidate(other, candidate.candidate_id)

    def test_unexpected_candidates_entry_and_known_temporary_fail_closed(self):
        self.initialize(); directory = self.root / "candidates"
        (directory / "junk").write_text("x")
        with self.assertRaises(LabCodingCandidateStoreError): self.initialize()
        (directory / "junk").unlink()
        candidate = self.candidate(); temporary = directory / ("." + candidate.candidate_id + ".tmp")
        temporary.write_bytes(b"partial"); os.chmod(temporary, 0o600)
        with self.assertRaises(LabCodingCandidateStoreError): self.initialize()
        with self.assertRaises(LabCodingCandidateStoreError):
            persist_lab_coding_candidate(self.root, candidate)
        self.assertEqual(temporary.read_bytes(), b"partial")

    def test_oversized_record_refused_before_decode(self):
        self.initialize(); identifier = "a" * 64
        path = self.root / "candidates" / (identifier + ".json")
        with path.open("wb") as stream: stream.truncate(MAX_RECORD_BYTES + 1)
        os.chmod(path, 0o600)
        with mock.patch.object(store, "lab_coding_candidate_from_bytes", side_effect=AssertionError("decoded")):
            with self.assertRaises(LabCodingCandidateStoreError):
                load_lab_coding_candidate(self.root, identifier)

    def test_decoded_candidate_id_must_match_requested(self):
        self.initialize(); candidate = self.candidate(); requested = "a" * 64
        path = self.root / "candidates" / (requested + ".json")
        path.write_bytes(lab_coding_candidate_to_bytes(candidate)); os.chmod(path, 0o600)
        with self.assertRaises(LabCodingCandidateStoreError):
            load_lab_coding_candidate(self.root, requested)

    def test_file_fsync_before_link_and_directory_fsync_after_link(self):
        self.initialize(); events = []; original_link = store._install_link
        original_fsync = store.os.fsync
        def recording_fsync(fd):
            mode = os.fstat(fd).st_mode
            events.append("directory-fsync" if stat.S_ISDIR(mode) else "file-fsync")
            return original_fsync(fd)
        def recording_link(*args):
            events.append("link")
            return original_link(*args)
        with mock.patch.object(store, "_install_link", side_effect=recording_link), \
             mock.patch.object(store.os, "fsync", side_effect=recording_fsync):
            persist_lab_coding_candidate(self.root, self.candidate())
        link_index = events.index("link")
        self.assertIn("file-fsync", events[:link_index])
        self.assertIn("directory-fsync", events[link_index + 1:])

    def test_module_imports_no_authority_modules(self):
        source = Path(store.__file__).read_text()
        forbidden = ("subprocess", "socket", "urllib", "requests", "lab_promotion", "lab_worker", "lab_model", "git")
        for name in forbidden:
            self.assertNotIn("import " + name, source.lower())
            self.assertNotIn("from ." + name, source.lower())


if __name__ == "__main__":
    unittest.main()
