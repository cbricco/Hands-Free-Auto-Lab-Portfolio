from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from hands_free_auto_lab.lab_coding_candidate import (
    CANDIDATE_COMPONENT,
    CANDIDATE_SCHEMA_VERSION,
    LabCodingCandidate,
    LabCodingCandidateFile,
    _candidate_id,
    build_lab_coding_candidate,
    lab_coding_candidate_to_bytes,
    validate_lab_coding_candidate,
)
from hands_free_auto_lab.lab_coding_job import (
    CODING_JOB_COMPONENT, CODING_JOB_SCHEMA_VERSION, LabCodingJobRecord,
)
from hands_free_auto_lab.lab_worker import (
    CAPABILITY_RUN_PROCESS, CAPABILITY_WORKSPACE_READ, CAPABILITY_WORKSPACE_WRITE,
    STATUS_COMPLETED, build_lab_worker_result,
)
from hands_free_auto_lab.lab_worker_job import run_lab_worker_job
from hands_free_auto_lab.lab_worker_change_policy import (
    LabWorkerChangeTarget,
    OPERATION_ADD,
    OPERATION_MODIFY,
    build_lab_worker_change_policy,
)
from hands_free_auto_lab.lab_workspace import create_lab_workspace
from hands_free_auto_lab.lab_workspace_seed import seed_lab_workspace
from hands_free_auto_lab.lab_coding_candidate_store import (
    LabCodingCandidateStoreError,
    initialize_lab_coding_candidate_store,
    load_lab_coding_candidate,
    persist_lab_coding_candidate,
)
from hands_free_auto_lab.lab_promotion_inspector import (
    inspect_lab_promotion_repository,
)
from hands_free_auto_lab.lab_promotion_proposal import (
    build_lab_coding_candidate_promotion_proposal,
)
from hands_free_auto_lab.lab_promotion_source import (
    SOURCE_KIND_CODING_CANDIDATE,
    normalize_lab_promotion_source,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_PENDING,
    RISK_DESTRUCTIVE_HIGH,
    RISK_WRITE,
    validate_lab_promotion_transaction,
    STATE_PREPARED,
    build_lab_promotion_transaction,
)


GIT = "/usr/bin/git"
RUN_ID = "9" * 64


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        [GIT, "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
        },
    )


def _repository_snapshot(repository: Path) -> tuple[tuple[str, str, int], ...]:
    snapshot = []
    for root, directories, filenames in os.walk(repository):
        directories.sort()
        for filename in sorted(filenames):
            path = Path(root) / filename
            relative = path.relative_to(repository).as_posix()
            raw = path.read_bytes()
            snapshot.append((relative, hashlib.sha256(raw).hexdigest(), path.stat().st_mode))
    return tuple(snapshot)


class _RealSeedWorker:
    def run(self, request):
        root = Path(request.workspace.path)
        (root / "alpha.txt").write_text("new alpha\n", encoding="utf-8")
        (root / "alpha.txt").chmod(0o600)
        (root / "new.txt").write_text("new file\n", encoding="utf-8")
        (root / "new.txt").chmod(0o600)
        return build_lab_worker_result(
            request=request, status=STATUS_COMPLETED, summary="bounded test worker",
            reported_changed_paths=("alpha.txt", "new.txt"), log="test log",
        )


class LabCodingCandidatePromotionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="auto-lab-coding-promotion-"))
        os.chmod(self.root, 0o700)
        self.repository = self.root / "repo"
        self.repository.mkdir(mode=0o700)
        _run_git(self.repository, "init", "-b", "main")
        (self.repository / "alpha.txt").write_text("old alpha\n", encoding="utf-8")
        os.chmod(self.repository / "alpha.txt", 0o644)
        _run_git(self.repository, "add", "alpha.txt")
        _run_git(
            self.repository,
            "-c", "user.name=Auto Lab Test",
            "-c", "user.email=auto-lab@example.invalid",
            "commit", "-m", "Initial sample state",
        )

    def tearDown(self):
        shutil.rmtree(self.root)

    def _candidate(self) -> LabCodingCandidate:
        files = (
            LabCodingCandidateFile(
                operation="MODIFIED",
                path="alpha.txt",
                before_bytes=len(b"old alpha\n"),
                before_sha256=_sha("old alpha\n"),
                before_mode=(self.repository / "alpha.txt").stat().st_mode & 0o777,
                final_bytes=len(b"new alpha\n"),
                final_sha256=_sha("new alpha\n"),
                final_mode=0o644,
                content="new alpha\n",
            ),
            LabCodingCandidateFile(
                operation="ADDED",
                path="new.txt",
                before_bytes=None,
                before_sha256=None,
                before_mode=None,
                final_bytes=len(b"new file\n"),
                final_sha256=_sha("new file\n"),
                final_mode=0o644,
                content="new file\n",
            ),
        )
        prototype = LabCodingCandidate(
            component=CANDIDATE_COMPONENT,
            schema_version=CANDIDATE_SCHEMA_VERSION,
            candidate_id="0" * 64,
            workspace_session_id="a" * 64,
            workspace_device=1,
            workspace_inode=2,
            before_snapshot_id="b" * 64,
            after_snapshot_id="c" * 64,
            physical_diff_id="d" * 64,
            files=files,
        )
        return replace(prototype, candidate_id=_candidate_id(prototype))

    def test_real_seed_0600_workspace_promotes_against_0644_repository_without_mode_drift(self):
        workspace_parent = self.root / "workspaces"
        workspace_parent.mkdir(mode=0o700)
        workspace = create_lab_workspace(
            str(workspace_parent), session_id="7" * 64,
        )
        seed = seed_lab_workspace(
            source_root=str(self.repository.resolve()),
            relative_paths=("alpha.txt",),
            workspace_parent=str(workspace_parent), workspace=workspace,
            max_files=2, max_total_bytes=1024,
        )
        self.assertEqual((Path(workspace.path) / "alpha.txt").stat().st_mode & 0o777, 0o600)
        capabilities = tuple(sorted((
            CAPABILITY_RUN_PROCESS, CAPABILITY_WORKSPACE_READ,
            CAPABILITY_WORKSPACE_WRITE,
        )))

        seed_entry = next(
            entry
            for entry in seed.entries
            if entry.relative_path == "alpha.txt"
        )

        change_policy = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_MODIFY,
                    path="alpha.txt",
                    max_final_bytes=4096,
                    final_mode=0o600,
                    before_bytes=seed_entry.size,
                    before_sha256=seed_entry.sha256,
                    before_mode=seed_entry.mode,
                ),
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path="new.txt",
                    max_final_bytes=4096,
                    final_mode=0o600,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                ),
            )
        )

        worker_job = run_lab_worker_job(
            goal="real seed promotion regression", worker_name="bounded-test-worker",
            worker=_RealSeedWorker(), workspace_parent=str(workspace_parent),
            workspace=workspace, capabilities=capabilities,
            max_runtime_seconds=30, max_log_bytes=4096,
            change_policy=change_policy,
        )
        record = LabCodingJobRecord(
            component=CODING_JOB_COMPONENT, schema_version=CODING_JOB_SCHEMA_VERSION,
            workspace=workspace, seed=seed, worker_job=worker_job,
        )
        candidate = build_lab_coding_candidate(
            record, workspace_parent=str(workspace_parent),
        )
        candidate_files = {item.path: item for item in candidate.files}
        self.assertEqual(candidate_files["alpha.txt"].before_mode, 0o644)
        self.assertEqual(candidate_files["alpha.txt"].final_mode, 0o600)
        self.assertIsNone(candidate_files["new.txt"].before_mode)
        self.assertEqual(candidate_files["new.txt"].final_mode, 0o600)

        unchanged_repository = _repository_snapshot(self.repository)
        inspection = inspect_lab_promotion_repository(
            candidate=candidate, repository_path=str(self.repository.resolve()),
        )
        proposal = build_lab_coding_candidate_promotion_proposal(
            candidate=candidate, repository_path=inspection.repository_path,
            repository_device=inspection.repository_device,
            repository_inode=inspection.repository_inode, branch=inspection.branch,
            head=inspection.head, before_states=inspection.file_states,
        )
        proposal_files = {item.path: item for item in proposal.files}
        self.assertEqual(proposal_files["alpha.txt"].before_mode, 0o644)
        self.assertEqual(proposal_files["alpha.txt"].after_mode, 0o644)
        self.assertIsNone(proposal_files["new.txt"].before_mode)
        self.assertEqual(proposal_files["new.txt"].after_mode, 0o644)
        self.assertEqual(_repository_snapshot(self.repository), unchanged_repository)
        self.assertEqual((self.repository / "alpha.txt").read_text(encoding="utf-8"), "old alpha\n")
        self.assertFalse((self.repository / "new.txt").exists())

    def test_validated_candidate_to_prepared_transaction_is_evidence_only(self):
        candidate = validate_lab_coding_candidate(self._candidate())
        unchanged_repository = _repository_snapshot(self.repository)

        source = normalize_lab_promotion_source(candidate)
        self.assertEqual(
            {(item.source_kind, item.source_id) for item in source.files},
            {(SOURCE_KIND_CODING_CANDIDATE, candidate.candidate_id)},
        )

        inspection = inspect_lab_promotion_repository(
            candidate=candidate,
            repository_path=str(self.repository.resolve()),
        )
        self.assertEqual(inspection.candidate_id, candidate.candidate_id)
        self.assertEqual(_repository_snapshot(self.repository), unchanged_repository)

        proposal = build_lab_coding_candidate_promotion_proposal(
            candidate=candidate,
            repository_path=inspection.repository_path,
            repository_device=inspection.repository_device,
            repository_inode=inspection.repository_inode,
            branch=inspection.branch,
            head=inspection.head,
            before_states=inspection.file_states,
        )
        self.assertEqual(proposal.candidate_id, candidate.candidate_id)
        self.assertEqual(
            {(item.source_kind, item.source_id) for item in proposal.files},
            {(SOURCE_KIND_CODING_CANDIDATE, candidate.candidate_id)},
        )
        with self.assertRaises(FrozenInstanceError):
            proposal.proposal_id = "0" * 64
        self.assertEqual(_repository_snapshot(self.repository), unchanged_repository)

        transaction = build_lab_promotion_transaction(proposal=proposal, run_id=RUN_ID)
        self.assertEqual(transaction.state, STATE_PREPARED)
        self.assertEqual(transaction.risk_classification, RISK_DESTRUCTIVE_HIGH)
        self.assertTrue(all(item.progress == PROGRESS_PENDING for item in transaction.files))
        self.assertEqual(
            {(item.source_kind, item.source_id) for item in transaction.files},
            {(SOURCE_KIND_CODING_CANDIDATE, candidate.candidate_id)},
        )
        for evidence in (source, inspection, proposal, transaction):
            for authority_name in (
                "approval", "approval_id", "authorization", "authorized",
                "target_temp", "target_temp_name", "executed",
            ):
                self.assertFalse(hasattr(evidence, authority_name))
        self.assertFalse((self.repository / "new.txt").exists())
        self.assertEqual((self.repository / "alpha.txt").read_text(encoding="utf-8"), "old alpha\n")
        self.assertEqual(_repository_snapshot(self.repository), unchanged_repository)



    def test_persisted_candidate_retains_exact_truthful_provenance_identity(self):
        store_root = self.root / "candidate-store"
        store_root.mkdir(mode=0o700)
        initialize_lab_coding_candidate_store(store_root)
        candidate = self._candidate()
        persist_lab_coding_candidate(store_root, candidate)

        loaded = load_lab_coding_candidate(store_root, candidate.candidate_id)
        source = normalize_lab_promotion_source(loaded)

        self.assertEqual(loaded, candidate)
        self.assertIsNot(loaded, candidate)
        self.assertEqual(
            {(item.source_kind, item.source_id) for item in source.files},
            {(SOURCE_KIND_CODING_CANDIDATE, candidate.candidate_id)},
        )
        self.assertNotEqual(candidate.candidate_id, candidate.physical_diff_id)
        self.assertNotEqual(candidate.candidate_id, candidate.files[0].final_sha256)
        self.assertTrue(all(item.operation != "WRITE_FILE" for item in source.files))

    def test_unsafe_persisted_candidates_do_not_reach_promotion_source(self):
        candidate = self._candidate()

        def make_store(label):
            root = self.root / ("candidate-store-" + label)
            root.mkdir(mode=0o700)
            initialize_lab_coding_candidate_store(root)
            return root

        cases = []

        wrong_root = make_store("wrong-request")
        wrong_id = "e" * 64
        wrong_path = wrong_root / "candidates" / (wrong_id + ".json")
        wrong_path.write_bytes(lab_coding_candidate_to_bytes(candidate))
        os.chmod(wrong_path, 0o600)
        cases.append(("wrong requested candidate id", wrong_root, wrong_id))

        noncanonical_root = make_store("noncanonical")
        noncanonical_path = persist_lab_coding_candidate(noncanonical_root, candidate)
        record = json.loads(noncanonical_path.read_bytes())
        noncanonical_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.chmod(noncanonical_path, 0o600)
        cases.append(("tampered canonical record", noncanonical_root, candidate.candidate_id))

        final_root = make_store("changed-final")
        final_path = persist_lab_coding_candidate(final_root, candidate)
        changed = json.loads(final_path.read_bytes())
        changed["files"][0]["content"] = "changed final evidence\n"
        final_path.write_text(
            json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(final_path, 0o600)
        cases.append(("changed final evidence", final_root, candidate.candidate_id))

        identity_root = make_store("malformed-identity")
        identity_path = persist_lab_coding_candidate(identity_root, candidate)
        malformed = json.loads(identity_path.read_bytes())
        malformed["workspace_session_id"] = "A" * 64
        identity_path.write_text(
            json.dumps(malformed, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(identity_path, 0o600)
        cases.append(("malformed source identity", identity_root, candidate.candidate_id))

        symlink_root = make_store("symlink")
        symlink_record = symlink_root / "candidates" / (candidate.candidate_id + ".json")
        symlink_record.symlink_to(self.root / "missing-candidate-record")
        cases.append(("record replacement symlink", symlink_root, candidate.candidate_id))

        hardlink_root = make_store("hardlink")
        hardlink_path = persist_lab_coding_candidate(hardlink_root, candidate)
        os.link(hardlink_path, self.root / "candidate-record-alias")
        cases.append(("record replacement hard link", hardlink_root, candidate.candidate_id))

        for label, store_root, requested_id in cases:
            with self.subTest(case=label):
                source = None
                with self.assertRaises(LabCodingCandidateStoreError):
                    loaded = load_lab_coding_candidate(store_root, requested_id)
                    source = normalize_lab_promotion_source(loaded)
                self.assertIsNone(source)

    def _proposal(self, candidate: LabCodingCandidate):
        inspection = inspect_lab_promotion_repository(
            candidate=candidate,
            repository_path=str(self.repository.resolve()),
        )
        proposal = build_lab_coding_candidate_promotion_proposal(
            candidate=candidate,
            repository_path=inspection.repository_path,
            repository_device=inspection.repository_device,
            repository_inode=inspection.repository_inode,
            branch=inspection.branch,
            head=inspection.head,
            before_states=inspection.file_states,
        )
        return inspection, proposal

    def test_proposal_does_not_survive_changed_branch(self):
        candidate = self._candidate()
        original_inspection, original = self._proposal(candidate)

        _run_git(self.repository, "checkout", "-b", "changed")
        changed_inspection, changed = self._proposal(candidate)

        self.assertEqual(changed_inspection.head, original_inspection.head)
        self.assertNotEqual(changed_inspection.branch, original_inspection.branch)
        self.assertNotEqual(changed.proposal_id, original.proposal_id)

    def test_proposal_does_not_survive_changed_head(self):
        candidate = self._candidate()
        original_inspection, original = self._proposal(candidate)
        (self.repository / "context.txt").write_text("context\n", encoding="utf-8")
        _run_git(self.repository, "add", "context.txt")
        _run_git(
            self.repository,
            "-c", "user.name=Auto Lab Test",
            "-c", "user.email=auto-lab@example.invalid",
            "commit", "-m", "Change inspected HEAD",
        )

        changed_inspection, changed = self._proposal(candidate)

        self.assertEqual(changed_inspection.branch, original_inspection.branch)
        self.assertNotEqual(changed_inspection.head, original_inspection.head)
        self.assertNotEqual(changed.proposal_id, original.proposal_id)

    def test_proposal_does_not_survive_dirty_staged_or_untracked_state(self):
        mutations = (
            ("dirty tracked file", lambda: (self.repository / "alpha.txt").write_text(
                "dirty\n", encoding="utf-8"
            )),
            ("staged state", self._stage_repository_drift),
            ("untracked state", lambda: (self.repository / "unknown.txt").write_text(
                "unknown\n", encoding="utf-8"
            )),
        )
        for label, mutate in mutations:
            with self.subTest(state=label):
                candidate = self._candidate()
                self._proposal(candidate)
                mutate()
                with self.assertRaisesRegex(Exception, "completely clean"):
                    inspect_lab_promotion_repository(
                        candidate=candidate,
                        repository_path=str(self.repository.resolve()),
                    )
                self.tearDown()
                self.setUp()

    def _stage_repository_drift(self):
        (self.repository / "alpha.txt").write_text("staged\n", encoding="utf-8")
        _run_git(self.repository, "add", "alpha.txt")

    def test_proposal_does_not_survive_replaced_repository_identity(self):
        candidate = self._candidate()
        original_inspection, original = self._proposal(candidate)
        displaced = self.root / "displaced-repo"
        self.repository.rename(displaced)
        self.repository.mkdir(mode=0o700)
        _run_git(self.repository, "init", "-b", "main")
        (self.repository / "alpha.txt").write_text("old alpha\n", encoding="utf-8")
        os.chmod(self.repository / "alpha.txt", 0o644)
        _run_git(self.repository, "add", "alpha.txt")
        _run_git(
            self.repository,
            "-c", "user.name=Auto Lab Test",
            "-c", "user.email=auto-lab@example.invalid",
            "commit", "-m", "Replacement repository",
        )

        changed_inspection, changed = self._proposal(candidate)

        self.assertNotEqual(
            (changed_inspection.repository_device, changed_inspection.repository_inode),
            (original_inspection.repository_device, original_inspection.repository_inode),
        )
        self.assertNotEqual(changed.proposal_id, original.proposal_id)

    def test_proposal_does_not_survive_stale_modify_destination(self):
        candidate = self._candidate()
        self._proposal(candidate)
        (self.repository / "alpha.txt").write_text("stale destination\n", encoding="utf-8")

        with self.assertRaisesRegex(Exception, "completely clean"):
            inspect_lab_promotion_repository(
                candidate=candidate,
                repository_path=str(self.repository.resolve()),
            )

    def test_proposal_does_not_survive_stale_destination_mode(self):
        candidate = self._candidate()
        self._proposal(candidate)
        os.chmod(self.repository / "alpha.txt", 0o640)

        with self.assertRaisesRegex(Exception, "before evidence does not match"):
            inspect_lab_promotion_repository(
                candidate=candidate,
                repository_path=str(self.repository.resolve()),
            )

    def test_modify_uses_exact_inspected_destination_mode(self):
        os.chmod(self.repository / "alpha.txt", 0o640)
        candidate = self._candidate()
        modified = replace(candidate.files[0], before_mode=0o640, final_mode=0o640)
        prototype = replace(
            candidate, candidate_id="0" * 64,
            files=(modified, candidate.files[1]),
        )
        candidate = replace(prototype, candidate_id=_candidate_id(prototype))

        _, proposal = self._proposal(candidate)
        transaction = build_lab_promotion_transaction(proposal=proposal, run_id=RUN_ID)

        self.assertEqual(proposal.files[0].before_mode, 0o640)
        self.assertEqual(proposal.files[0].after_mode, 0o640)
        self.assertEqual(transaction.files[0].after_mode, 0o640)
        self.assertEqual(proposal.files[1].after_mode, 0o644)

    def test_worker_0600_modify_mode_does_not_become_destination_mode(self):
        os.chmod(self.repository / "alpha.txt", 0o644)
        candidate = self._candidate()
        modified = replace(candidate.files[0], final_mode=0o600)
        prototype = replace(
            candidate, candidate_id="0" * 64,
            files=(modified, candidate.files[1]),
        )
        candidate = replace(prototype, candidate_id=_candidate_id(prototype))

        _, proposal = self._proposal(candidate)
        transaction = build_lab_promotion_transaction(proposal=proposal, run_id=RUN_ID)

        self.assertEqual(candidate.files[0].final_mode, 0o600)
        self.assertEqual(proposal.files[0].before_mode, 0o644)
        self.assertEqual(proposal.files[0].after_mode, 0o644)
        self.assertEqual(transaction.files[0].after_mode, 0o644)

    def test_proposal_does_not_survive_unexpected_add_destination(self):
        candidate = self._candidate()
        self._proposal(candidate)
        (self.repository / "new.txt").write_text("occupied\n", encoding="utf-8")

        with self.assertRaisesRegex(Exception, "completely clean"):
            inspect_lab_promotion_repository(
                candidate=candidate,
                repository_path=str(self.repository.resolve()),
            )

    def test_candidate_identity_and_physical_diff_tampering_fail_closed(self):
        candidate = self._candidate()
        tampered_candidates = (
            replace(candidate, candidate_id="0" * 64),
            replace(candidate, physical_diff_id="e" * 64),
        )

        for tampered in tampered_candidates:
            with self.subTest(candidate=tampered):
                with self.assertRaises(Exception):
                    inspect_lab_promotion_repository(
                        candidate=tampered,
                        repository_path=str(self.repository.resolve()),
                    )

    def test_candidate_file_evidence_tampering_fails_before_promotion(self):
        candidate = self._candidate()
        original = candidate.files[0]
        tampered_files = (
            replace(original, final_bytes=original.final_bytes + 1),
            replace(original, final_sha256=_sha("forged final\n")),
            replace(original, content="forged final\n"),
            replace(original, before_bytes=original.before_bytes + 1),
            replace(original, before_sha256=_sha("forged before\n")),
            replace(original, before_mode=original.before_mode ^ 0o040),
        )

        for tampered_file in tampered_files:
            with self.subTest(tampered_file=tampered_file):
                tampered = replace(
                    candidate,
                    files=(tampered_file, candidate.files[1]),
                )
                with self.assertRaises(Exception):
                    inspect_lab_promotion_repository(
                        candidate=tampered,
                        repository_path=str(self.repository.resolve()),
                    )

    def test_provenance_cannot_lower_operation_derived_risk(self):
        candidate = self._candidate()
        inspection = inspect_lab_promotion_repository(
            candidate=candidate,
            repository_path=str(self.repository.resolve()),
        )
        proposal = build_lab_coding_candidate_promotion_proposal(
            candidate=candidate,
            repository_path=inspection.repository_path,
            repository_device=inspection.repository_device,
            repository_inode=inspection.repository_inode,
            branch=inspection.branch,
            head=inspection.head,
            before_states=inspection.file_states,
        )
        transaction = build_lab_promotion_transaction(
            proposal=proposal,
            run_id=RUN_ID,
        )
        forged = replace(
            transaction,
            risk_classification=RISK_WRITE,
            files=tuple(
                replace(
                    item,
                    source_kind="legacy_write_action",
                    source_id="f" * 64,
                )
                for item in transaction.files
            ),
        )

        with self.assertRaisesRegex(Exception, "risk classification"):
            validate_lab_promotion_transaction(forged)
if __name__ == "__main__":
    unittest.main()
