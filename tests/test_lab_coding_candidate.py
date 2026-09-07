from dataclasses import is_dataclass, replace
import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hands_free_auto_lab.lab_coding_candidate import (
    LabCodingCandidate, LabCodingCandidateError, LabCodingCandidateFile,
    build_lab_coding_candidate, validate_lab_coding_candidate,
    lab_coding_candidate_from_bytes, lab_coding_candidate_from_wire,
    lab_coding_candidate_to_bytes, lab_coding_candidate_to_wire,
)
from hands_free_auto_lab.lab_coding_job import (
    CODING_JOB_COMPONENT, CODING_JOB_SCHEMA_VERSION, LabCodingJobRecord,
)
from hands_free_auto_lab.lab_worker import (
    CAPABILITY_RUN_PROCESS, CAPABILITY_WORKSPACE_READ, CAPABILITY_WORKSPACE_WRITE,
    STATUS_COMPLETED, STATUS_FAILED, build_lab_worker_request,
    build_lab_worker_result,
)
from hands_free_auto_lab.lab_worker_job import run_lab_worker_job
from hands_free_auto_lab.lab_worker_change_policy import (
    LabWorkerChangePolicyError,
    LabWorkerChangeTarget,
    OPERATION_ADD,
    OPERATION_MODIFY,
    build_lab_worker_change_policy,
)
from hands_free_auto_lab.lab_workspace import create_lab_workspace
from hands_free_auto_lab.lab_workspace_snapshot import (
    CHANGE_ADDED,
    CHANGE_MODIFIED,
    ENTRY_FILE,
)
from hands_free_auto_lab.lab_workspace_seed import seed_lab_workspace

CAPABILITIES = tuple(sorted((
    CAPABILITY_RUN_PROCESS, CAPABILITY_WORKSPACE_READ, CAPABILITY_WORKSPACE_WRITE,
)))


def write_safe_file(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


class MutatingWorker:
    def __init__(self, mutation, *, status=STATUS_COMPLETED, reported=()):
        self.mutation = mutation
        self.status = status
        self.reported = reported

    def run(self, request):
        self.mutation(Path(request.workspace.path))
        return build_lab_worker_result(
            request=request, status=self.status, summary="test worker",
            reported_changed_paths=self.reported, log="test log",
        )


def _bind_candidate_test_policy(worker_job):
    """
    Bind deterministic policy evidence to a legacy low-level test record.

    Real pre-worker policy enforcement is independently covered by the
    worker-job tests. This helper keeps candidate adversarial fixtures
    focused on candidate validation.
    """
    try:
        targets = []

        for change in worker_job.physical_diff.entries:
            before = change.before
            after = change.after

            if (
                change.change == CHANGE_ADDED
                and before is None
                and after is not None
                and after.kind == ENTRY_FILE
            ):
                targets.append(
                    LabWorkerChangeTarget(
                        operation=OPERATION_ADD,
                        path=change.path,
                        max_final_bytes=max(
                            1,
                            after.bytes,
                        ),
                        final_mode=after.mode,
                        before_bytes=None,
                        before_sha256=None,
                        before_mode=None,
                    )
                )

            elif (
                change.change == CHANGE_MODIFIED
                and before is not None
                and after is not None
                and before.kind == ENTRY_FILE
                and after.kind == ENTRY_FILE
            ):
                targets.append(
                    LabWorkerChangeTarget(
                        operation=OPERATION_MODIFY,
                        path=change.path,
                        max_final_bytes=max(
                            1,
                            after.bytes,
                        ),
                        final_mode=after.mode,
                        before_bytes=before.bytes,
                        before_sha256=before.sha256,
                        before_mode=before.mode,
                    )
                )

            else:
                raise ValueError(
                    "physical evidence cannot form a valid test policy"
                )

        if not targets:
            raise ValueError(
                "empty physical diff cannot form a valid test policy"
            )

        policy = build_lab_worker_change_policy(
            targets=tuple(targets)
        )

    except (
        AttributeError,
        LabWorkerChangePolicyError,
        ValueError,
    ):
        policy = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path="__b2b_policy_mismatch__.txt",
                    max_final_bytes=1,
                    final_mode=0o600,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                ),
            )
        )

    old_request = worker_job.request
    old_result = worker_job.result

    request = build_lab_worker_request(
        worker=old_request.worker,
        goal=old_request.goal,
        workspace=old_request.workspace,
        change_policy_id=policy.policy_id,
        capabilities=old_request.constraints.capabilities,
        max_runtime_seconds=(
            old_request.constraints.max_runtime_seconds
        ),
        max_log_bytes=old_request.constraints.max_log_bytes,
    )

    result = build_lab_worker_result(
        request=request,
        status=old_result.status,
        summary=old_result.summary,
        reported_changed_paths=(
            old_result.reported_changed_paths
        ),
        log=old_result.log,
    )

    return replace(
        worker_job,
        request=request,
        result=result,
        change_policy=policy,
    )


class LabCodingCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.parent.chmod(0o700)
        self.counter = 0

    def tearDown(self):
        self.temporary.cleanup()

    def make_record(self, mutation, *, status=STATUS_COMPLETED, reported=(), setup=None):
        self.counter += 1
        workspace = create_lab_workspace(
            str(self.parent), session_id=f"{self.counter:064x}",
        )
        root = Path(workspace.path)
        if setup is not None:
            setup(root)
        worker_job = run_lab_worker_job(
            goal="candidate evidence test", worker_name="test-worker",
            worker=MutatingWorker(mutation, status=status, reported=reported),
            workspace_parent=str(self.parent), workspace=workspace,
            capabilities=CAPABILITIES, max_runtime_seconds=30, max_log_bytes=4096,
        )
        worker_job = _bind_candidate_test_policy(worker_job)
        return LabCodingJobRecord(
            component=CODING_JOB_COMPONENT, schema_version=CODING_JOB_SCHEMA_VERSION,
            workspace=workspace, seed=None, worker_job=worker_job,
        )

    def build(self, record):
        return build_lab_coding_candidate(record, workspace_parent=str(self.parent))

    def assert_refused(self, record):
        with self.assertRaises(LabCodingCandidateError):
            self.build(record)

    def test_unbound_worker_job_record_is_refused(self):
        record = self.make_record(
            lambda root: write_safe_file(
                root / "policy-bound.txt",
                "policy bound\n",
            )
        )

        self.assert_refused(
            replace(
                record,
                worker_job=replace(
                    record.worker_job,
                    change_policy=None,
                ),
            )
        )

    def test_substituted_or_tampered_worker_policy_is_refused(self):
        record = self.make_record(
            lambda root: write_safe_file(
                root / "policy-bound.txt",
                "policy bound\n",
            )
        )

        substituted = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path="different.txt",
                    max_final_bytes=4096,
                    final_mode=0o600,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                ),
            )
        )

        self.assert_refused(
            replace(
                record,
                worker_job=replace(
                    record.worker_job,
                    change_policy=substituted,
                ),
            )
        )

        tampered = replace(
            record.worker_job.change_policy,
            policy_id="0" * 64,
        )

        self.assert_refused(
            replace(
                record,
                worker_job=replace(
                    record.worker_job,
                    change_policy=tampered,
                ),
            )
        )

    def test_seeded_modified_candidate_uses_source_mode_and_binds_workspace_mode_via_snapshot(self):
        source = self.parent / "source"
        source.mkdir(mode=0o700)
        source_file = source / "existing.txt"
        source_file.write_text("before\n", encoding="utf-8")
        source_file.chmod(0o644)
        self.counter += 1
        workspace = create_lab_workspace(
            str(self.parent), session_id=f"{self.counter:064x}",
        )
        seed = seed_lab_workspace(
            source_root=str(source), relative_paths=("existing.txt",),
            workspace_parent=str(self.parent), workspace=workspace,
            max_files=2, max_total_bytes=1024,
        )
        worker_job = run_lab_worker_job(
            goal="seed mode provenance", worker_name="test-worker",
            worker=MutatingWorker(lambda root: write_safe_file(
                root / "existing.txt", "after\n")),
            workspace_parent=str(self.parent), workspace=workspace,
            capabilities=CAPABILITIES, max_runtime_seconds=30, max_log_bytes=4096,
        )
        worker_job = _bind_candidate_test_policy(worker_job)
        record = LabCodingJobRecord(
            component=CODING_JOB_COMPONENT, schema_version=CODING_JOB_SCHEMA_VERSION,
            workspace=workspace, seed=seed, worker_job=worker_job,
        )

        candidate = self.build(record)
        item = candidate.files[0]
        before_entry = next(
            entry for entry in worker_job.before_snapshot.entries
            if entry.path == "existing.txt"
        )
        self.assertEqual(source_file.stat().st_mode & 0o777, 0o644)
        self.assertEqual(seed.entries[0].mode, 0o600)
        self.assertEqual(item.before_mode, 0o644)
        self.assertEqual(item.final_mode, 0o600)
        self.assertEqual(before_entry.mode, 0o600)

    def test_successful_added_candidate(self):
        record = self.make_record(lambda root: write_safe_file(root / "new.txt", "new\n"))
        candidate = self.build(record)
        self.assertEqual([(f.path, f.operation, f.content) for f in candidate.files],
                         [("new.txt", "ADDED", "new\n")])
        self.assertEqual(len(candidate.candidate_id), 64)
        self.assertTrue(is_dataclass(LabCodingCandidate))
        self.assertTrue(LabCodingCandidate.__dataclass_params__.frozen)
        self.assertTrue(hasattr(LabCodingCandidate, "__slots__"))
        self.assertTrue(LabCodingCandidateFile.__dataclass_params__.frozen)
        self.assertTrue(hasattr(LabCodingCandidateFile, "__slots__"))

    def test_public_validator_accepts_added_and_returns_same_object(self):
        candidate = self.build(self.make_record(
            lambda root: write_safe_file(root / "new.txt", "new\n")))
        self.assertIs(validate_lab_coding_candidate(candidate), candidate)

    def test_public_validator_accepts_modified(self):
        candidate = self.build(self.make_record(
            lambda root: write_safe_file(root / "existing.txt", "after\n"),
            setup=lambda root: write_safe_file(root / "existing.txt", "before\n")))
        self.assertIs(validate_lab_coding_candidate(candidate), candidate)

    def assert_validation_refused(self, candidate):
        with self.assertRaises(LabCodingCandidateError):
            validate_lab_coding_candidate(candidate)

    def test_validator_refuses_wrong_top_level_metadata_and_identity(self):
        candidate = self.build(self.make_record(
            lambda root: write_safe_file(root / "x.txt", "x\n")))
        for changed in (
            object(), replace(candidate, component="wrong"),
            replace(candidate, schema_version=2),
            replace(candidate, candidate_id="0" * 64),
            replace(candidate, workspace_session_id="A" * 64),
            replace(candidate, workspace_device=True),
            replace(candidate, workspace_inode=-1),
            replace(candidate, before_snapshot_id="bad"),
            replace(candidate, after_snapshot_id="F" * 64),
            replace(candidate, physical_diff_id="0" * 63),
            replace(candidate, files=()),
        ):
            with self.subTest(changed=changed):
                self.assert_validation_refused(changed)

    def test_validator_refuses_file_structure_tampering(self):
        candidate = self.build(self.make_record(lambda root: (
            write_safe_file(root / "a.txt", "a\n"),
            write_safe_file(root / "b.txt", "b\n"))))
        a, b = candidate.files
        cases = (
            replace(candidate, files=(object(),)),
            replace(candidate, files=(a, a)),
            replace(candidate, files=(b, a)),
            replace(candidate, files=(replace(a, path="../a.txt"), b)),
            replace(candidate, files=(replace(a, operation="DELETED"), b)),
            replace(candidate, files=(replace(a, before_bytes=0), b)),
        )
        for changed in cases:
            with self.subTest(files=changed.files):
                self.assert_validation_refused(changed)

    def test_validator_refuses_incomplete_modified_evidence(self):
        candidate = self.build(self.make_record(
            lambda root: write_safe_file(root / "x.txt", "after\n"),
            setup=lambda root: write_safe_file(root / "x.txt", "before\n")))
        item = candidate.files[0]
        for field in ("before_bytes", "before_sha256", "before_mode"):
            self.assert_validation_refused(replace(
                candidate, files=(replace(item, **{field: None}),)))

    def test_validator_refuses_final_evidence_and_content_tampering(self):
        candidate = self.build(self.make_record(
            lambda root: write_safe_file(root / "x.txt", "safe\n")))
        item = candidate.files[0]
        for changed_item in (
            replace(item, final_bytes=item.final_bytes + 1),
            replace(item, final_sha256="0" * 64),
            replace(item, content="other\n"),
            replace(item, final_mode=0o700),
            replace(item, final_mode=0o620),
            replace(item, final_bytes=True),
            replace(item, content=b"safe\n"),
        ):
            self.assert_validation_refused(replace(candidate, files=(changed_item,)))

    def test_validator_does_not_reread_workspace_or_filesystem(self):
        candidate = self.build(self.make_record(
            lambda root: write_safe_file(root / "x.txt", "safe\n")))
        with patch("hands_free_auto_lab.lab_coding_candidate.execute_lab_read_file",
                   side_effect=AssertionError("READ_FILE called")), patch(
             "hands_free_auto_lab.lab_coding_candidate.capture_lab_workspace_snapshot",
             side_effect=AssertionError("workspace recaptured")), patch(
             "builtins.open", side_effect=AssertionError("filesystem opened")):
            self.assertIs(validate_lab_coding_candidate(candidate), candidate)

    def codec_candidates(self):
        added = self.build(self.make_record(
            lambda root: write_safe_file(root / "new.txt", "snowman \u2603\n")))
        modified = self.build(self.make_record(
            lambda root: write_safe_file(root / "existing.txt", "after \u00e9\n"),
            setup=lambda root: write_safe_file(root / "existing.txt", "before\n")))
        return added, modified

    def test_wire_round_trip_added_and_modified(self):
        for candidate in self.codec_candidates():
            with self.subTest(operation=candidate.files[0].operation):
                wire = lab_coding_candidate_to_wire(candidate)
                decoded = lab_coding_candidate_from_wire(wire)
                self.assertEqual(decoded, candidate)
                self.assertEqual(decoded.candidate_id, candidate.candidate_id)
                self.assertIs(type(wire), dict)
                self.assertIs(type(wire["files"]), list)
                self.assertIs(type(wire["files"][0]), dict)

    def test_canonical_bytes_round_trip_is_deterministic_and_unicode_safe(self):
        for candidate in self.codec_candidates():
            with self.subTest(operation=candidate.files[0].operation):
                encoded = lab_coding_candidate_to_bytes(candidate)
                self.assertEqual(encoded, lab_coding_candidate_to_bytes(candidate))
                self.assertTrue(encoded.endswith(b"\n"))
                self.assertFalse(encoded.endswith(b"\n\n"))
                decoded = lab_coding_candidate_from_bytes(encoded)
                self.assertEqual(decoded, candidate)
                self.assertEqual(decoded.candidate_id, candidate.candidate_id)
                self.assertEqual(decoded.files[0].content, candidate.files[0].content)
                unicode_text = "snowman \u2603" if candidate.files[0].operation == "ADDED" else "after \u00e9"
                self.assertIn(unicode_text.encode("utf-8"), encoded)

    def test_from_wire_refuses_wrong_and_inexact_structure(self):
        candidate = self.codec_candidates()[0]
        wire = lab_coding_candidate_to_wire(candidate)
        cases = [None, [], "wire"]
        extra = dict(wire); extra["extra"] = None; cases.append(extra)
        missing = dict(wire); missing.pop("candidate_id"); cases.append(missing)
        wrong_files = dict(wire); wrong_files["files"] = tuple(wire["files"]); cases.append(wrong_files)
        empty_files = dict(wire); empty_files["files"] = []; cases.append(empty_files)
        wrong_nested = dict(wire); wrong_nested["files"] = [object()]; cases.append(wrong_nested)
        extra_nested = dict(wire); extra_nested["files"] = [dict(wire["files"][0], extra=None)]; cases.append(extra_nested)
        missing_item = dict(wire["files"][0]); missing_item.pop("content")
        missing_nested = dict(wire); missing_nested["files"] = [missing_item]; cases.append(missing_nested)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(LabCodingCandidateError):
                    lab_coding_candidate_from_wire(value)

    def test_codec_refuses_tampered_identity_and_content_evidence(self):
        candidate = self.codec_candidates()[0]
        wire = lab_coding_candidate_to_wire(candidate)
        bad_id = dict(wire); bad_id["candidate_id"] = "0" * 64
        bad_content = dict(wire); bad_content["files"] = [dict(wire["files"][0], content="tampered\n")]
        bad_hash = dict(wire); bad_hash["files"] = [dict(wire["files"][0], final_sha256="0" * 64)]
        for value in (bad_id, bad_content, bad_hash):
            encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False) + "\n").encode("utf-8")
            with self.assertRaises(LabCodingCandidateError):
                lab_coding_candidate_from_bytes(encoded)

    def test_from_bytes_refuses_wrong_type_invalid_data_and_non_object(self):
        for value in ("{}\n", bytearray(b"{}\n"), memoryview(b"{}\n"), None):
            with self.subTest(value=type(value)):
                with self.assertRaises(LabCodingCandidateError):
                    lab_coding_candidate_from_bytes(value)
        for value in (b"\xff\n", b"not-json\n", b"[]\n", b"null\n"):
            with self.subTest(value=value):
                with self.assertRaises(LabCodingCandidateError):
                    lab_coding_candidate_from_bytes(value)

    def test_from_bytes_refuses_noncanonical_encodings(self):
        candidate = self.codec_candidates()[0]
        canonical = lab_coding_candidate_to_bytes(candidate)
        wire = lab_coding_candidate_to_wire(candidate)
        alternate_order = json.dumps(wire, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        duplicate = canonical.replace(b'{"after_snapshot_id":', b'{"after_snapshot_id":"duplicate","after_snapshot_id":', 1)
        cases = (
            b" " + canonical, canonical[:-1], canonical + b"\n",
            json.dumps(wire, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n",
            alternate_order, duplicate,
        )
        for value in cases:
            with self.subTest(value=value[:40]):
                self.assertNotEqual(value, canonical)
                with self.assertRaises(LabCodingCandidateError):
                    lab_coding_candidate_from_bytes(value)

    def test_codec_does_not_reread_workspace_or_filesystem(self):
        candidate = self.codec_candidates()[0]
        encoded = lab_coding_candidate_to_bytes(candidate)
        with patch("hands_free_auto_lab.lab_coding_candidate.execute_lab_read_file",
                   side_effect=AssertionError("READ_FILE called")), patch(
             "hands_free_auto_lab.lab_coding_candidate.capture_lab_workspace_snapshot",
             side_effect=AssertionError("workspace recaptured")), patch(
             "builtins.open", side_effect=AssertionError("filesystem opened")):
            self.assertEqual(lab_coding_candidate_from_bytes(encoded), candidate)
            self.assertEqual(lab_coding_candidate_to_bytes(candidate), encoded)

    def test_worker_reported_paths_do_not_determine_candidate_paths(self):
        record = self.make_record(
            lambda root: write_safe_file(root / "physical.txt", "truth\n"),
            reported=("fiction.txt",),
        )
        self.assertEqual([f.path for f in self.build(record).files], ["physical.txt"])

    def test_successful_modified_regular_file_candidate(self):
        record = self.make_record(
            lambda root: write_safe_file(root / "existing.txt", "after\n"),
            setup=lambda root: write_safe_file(root / "existing.txt", "before\n"),
        )
        item = self.build(record).files[0]
        self.assertEqual((item.path, item.operation, item.content),
                         ("existing.txt", "MODIFIED", "after\n"))
        self.assertIsNotNone(item.before_sha256)

    def test_candidate_identity_changes_with_authoritative_evidence(self):
        first = self.make_record(lambda root: write_safe_file(root / "x.txt", "one\n"))
        second = self.make_record(lambda root: write_safe_file(root / "x.txt", "two\n"))
        self.assertNotEqual(self.build(first).candidate_id, self.build(second).candidate_id)

    def test_unsuccessful_worker_refused(self):
        self.assert_refused(self.make_record(
            lambda root: write_safe_file(root / "partial.txt", "partial\n"),
            status=STATUS_FAILED,
        ))

    def test_deletion_refused(self):
        self.assert_refused(self.make_record(
            lambda root: (root / "old.txt").unlink(),
            setup=lambda root: write_safe_file(root / "old.txt", "old\n"),
        ))

    def test_changed_directory_refused(self):
        self.assert_refused(self.make_record(lambda root: (root / "directory").mkdir()))

    def test_empty_directory_to_file_same_path_replacement_refused(self):
        def replace_directory(root):
            (root / "node").rmdir()
            write_safe_file(root / "node", "file\n")
        self.assert_refused(self.make_record(
            replace_directory, setup=lambda root: (root / "node").mkdir(),
        ))

    def test_executable_final_file_refused(self):
        def mutation(root):
            path = root / "run.txt"
            path.write_text("run\n")
            path.chmod(0o700)
        self.assert_refused(self.make_record(mutation))

    def test_group_or_other_writable_final_file_refused(self):
        for mode in (0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                def mutation(root, mode=mode):
                    path = root / "unsafe.txt"
                    path.write_text("unsafe\n")
                    path.chmod(mode)
                self.assert_refused(self.make_record(mutation))

    def test_changed_final_content_refused(self):
        record = self.make_record(lambda root: write_safe_file(root / "changed.txt", "recorded\n"))
        write_safe_file(Path(record.workspace.path) / "changed.txt", "changed later\n")
        self.assert_refused(record)

    def test_tampered_recorded_physical_diff_refused(self):
        record = self.make_record(lambda root: write_safe_file(root / "real.txt", "real\n"))
        diff = replace(record.worker_job.physical_diff, entries=())
        self.assert_refused(replace(record, worker_job=replace(record.worker_job, physical_diff=diff)))

    def test_tampered_snapshot_refused(self):
        record = self.make_record(lambda root: write_safe_file(root / "real.txt", "real\n"))
        entry = record.worker_job.after_snapshot.entries[0]
        bad_entry = replace(entry, sha256="0" * 64)
        snapshot = replace(record.worker_job.after_snapshot, entries=(bad_entry,))
        self.assert_refused(replace(record, worker_job=replace(record.worker_job, after_snapshot=snapshot)))

    def test_empty_physical_diff_refused(self):
        self.assert_refused(self.make_record(lambda root: None))

    def test_import_boundary_has_no_authority_modules(self):
        source_path = Path(__file__).parents[1] / "src/hands_free_auto_lab/lab_coding_candidate.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        forbidden = ("subprocess", "socket", "urllib", "http", "git", "promotion", "approval")
        self.assertFalse([name for name in imports if any(word in name.lower() for word in forbidden)])


if __name__ == "__main__":
    unittest.main()
