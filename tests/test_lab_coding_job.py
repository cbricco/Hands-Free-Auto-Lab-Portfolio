from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile
import unittest

from hands_free_auto_lab.lab_coding_candidate import (
    build_lab_coding_candidate,
)
from hands_free_auto_lab.lab_coding_job import (
    CODING_JOB_COMPONENT,
    CODING_JOB_SCHEMA_VERSION,
    run_codex_coding_job,
    run_codex_coding_job_from_commit,
)
from hands_free_auto_lab.lab_committed_workspace_seed import (
    LabCommittedWorkspaceSeedError,
    LabCommittedWorkspaceSeedRecord,
)
from hands_free_auto_lab.lab_codex_worker import (
    CODEX_WORKER_NAME,
    CodexWorker,
)
from hands_free_auto_lab.lab_worker import (
    CAPABILITY_RUN_PROCESS,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    build_lab_worker_result,
)
from hands_free_auto_lab.lab_worker_change_policy import (
    LabWorkerChangeTarget,
    OPERATION_ADD,
    OPERATION_MODIFY,
    build_lab_worker_change_policy,
)
from hands_free_auto_lab.lab_workspace_seed import (
    LabWorkspaceSeedError,
)
from hands_free_auto_lab.lab_workspace_snapshot import (
    diff_lab_workspace_snapshots,
)


SESSION_ID = "d" * 64

CAPABILITIES = tuple(
    sorted(
        (
            CAPABILITY_RUN_PROCESS,
            CAPABILITY_WORKSPACE_READ,
            CAPABILITY_WORKSPACE_WRITE,
        )
    )
)


class FakeWorker:
    def __init__(
        self,
        *,
        status=STATUS_COMPLETED,
        output_name="worker-output.txt",
        output_bytes=b"worker output\n",
        reported_changed_paths=("claimed.txt",),
    ):
        self.status = status
        self.output_name = output_name
        self.output_bytes = output_bytes
        self.reported_changed_paths = reported_changed_paths
        self.calls = 0
        self.requests = []
        self.observed_seed = None


    def run(
        self,
        request,
    ):
        self.calls += 1
        self.requests.append(
            request
        )

        workspace = Path(
            request.workspace.path
        )

        self.observed_seed = (
            workspace
            / "project.py"
        ).read_bytes()

        if self.output_name is not None:
            output = (
                workspace
                / self.output_name
            )

            output.write_bytes(
                self.output_bytes
            )

            output.chmod(
                0o600
            )

        return build_lab_worker_result(
            request=request,
            status=self.status,
            summary="fake worker finished",
            log="fake worker finished\n",
            reported_changed_paths=self.reported_changed_paths,
        )


class PolicyBindingFakeCodexWorker(
    FakeWorker,
    CodexWorker,
):
    """
    Test-only Codex-typed fake.

    It proves the committed-source job selects the Codex-specific
    policy-binding branch without executing real Codex.
    """

    def bind_change_policy(
        self,
        change_policy,
    ):
        self.bound_change_policy = (
            change_policy
        )
        return self


class LabCodingJobTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-coding-job-tests-"
            )
        )

        self.source = (
            self.root
            / "source"
        )
        self.source.mkdir(
            mode=0o700
        )

        self.workspace_parent = (
            self.root
            / "workspaces"
        )
        self.workspace_parent.mkdir(
            mode=0o700
        )

        self.project = (
            self.source
            / "project.py"
        )
        self.project.write_bytes(
            b"VALUE = 1\n"
        )
        self.project.chmod(
            0o600
        )

        self.private = (
            self.source
            / "private.txt"
        )
        self.private.write_bytes(
            b"not approved for the lab\n"
        )
        self.private.chmod(
            0o600
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def _committed_repo(self):
        repo = self.root / "committed-repo"
        repo.mkdir(mode=0o700)

        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }

        def git(*args):
            result = subprocess.run(
                [
                    "/usr/bin/git",
                    "-c",
                    "user.name=Auto Lab Test",
                    "-c",
                    "user.email=auto-lab@example.invalid",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.untrackedCache=false",
                    "-C",
                    str(repo),
                    *args,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=environment,
            )

            self.assertEqual(
                result.returncode,
                0,
                result.stderr.decode(
                    "utf-8",
                    errors="replace",
                ),
            )

            return result

        git(
            "init",
            "-b",
            "main",
        )

        project = repo / "project.py"
        project.write_bytes(
            b"VALUE = 1\n"
        )
        project.chmod(
            0o644
        )

        git(
            "add",
            "project.py",
        )
        git(
            "commit",
            "-m",
            "initial",
        )

        head = git(
            "rev-parse",
            "HEAD",
        ).stdout.decode(
            "ascii"
        ).strip()

        return repo, project, head

    def _run(
        self,
        worker,
        *,
        relative_paths=("project.py",),
    ):
        change_policy = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path=worker.output_name,
                    max_final_bytes=max(
                        1,
                        len(worker.output_bytes),
                    ),
                    final_mode=0o600,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                ),
            )
        )

        return run_codex_coding_job(
            source_root=str(self.source),
            relative_paths=relative_paths,
            workspace_parent=str(
                self.workspace_parent
            ),
            session_id=SESSION_ID,
            goal="Make one bounded test change.",
            worker=worker,
            change_policy=change_policy,
            max_seed_files=16,
            max_seed_bytes=1024 * 1024,
            max_runtime_seconds=30,
            max_log_bytes=4096,
        )

    def test_composes_seed_worker_job_and_independent_diff(self):
        worker = FakeWorker()

        record = self._run(
            worker
        )

        self.assertEqual(
            worker.calls,
            1,
        )
        self.assertTrue(
            record.succeeded
        )
        self.assertEqual(
            record.component,
            CODING_JOB_COMPONENT,
        )
        self.assertEqual(
            record.schema_version,
            CODING_JOB_SCHEMA_VERSION,
        )

        request = (
            worker.requests[0]
        )


        self.assertEqual(
            request.worker,
            CODEX_WORKER_NAME,
        )
        self.assertEqual(
            request.goal,
            "Make one bounded test change.",
        )
        self.assertEqual(
            request.constraints.capabilities,
            CAPABILITIES,
        )

        self.assertEqual(
            worker.observed_seed,
            b"VALUE = 1\n",
        )

        workspace = Path(
            record.workspace.path
        )

        self.assertEqual(
            (
                workspace
                / "project.py"
            ).read_bytes(),
            b"VALUE = 1\n",
        )
        self.assertEqual(
            (
                workspace
                / "worker-output.txt"
            ).read_bytes(),
            b"worker output\n",
        )
        self.assertFalse(
            (
                workspace
                / "private.txt"
            ).exists()
        )

        self.assertEqual(
            self.project.read_bytes(),
            b"VALUE = 1\n",
        )
        self.assertEqual(
            self.private.read_bytes(),
            b"not approved for the lab\n",
        )

        self.assertEqual(
            tuple(
                entry.relative_path
                for entry in record.seed.entries
            ),
            ("project.py",),
        )

        self.assertEqual(
            record.worker_job.result.reported_changed_paths,
            ("claimed.txt",),
        )

        self.assertNotEqual(
            record.worker_job.before_snapshot,
            record.worker_job.after_snapshot,
        )

        self.assertEqual(
            record.worker_job.physical_diff,
            diff_lab_workspace_snapshots(
                record.worker_job.before_snapshot,
                record.worker_job.after_snapshot,
            ),
        )

    def test_failed_worker_runs_once_and_source_remains_unchanged(self):
        worker = FakeWorker(
            status=STATUS_FAILED,
            output_name="partial.txt",
            output_bytes=b"partial work\n",
            reported_changed_paths=(),
        )

        record = self._run(
            worker
        )

        self.assertEqual(
            worker.calls,
            1,
        )
        self.assertFalse(
            record.succeeded
        )

        self.assertEqual(
            self.project.read_bytes(),
            b"VALUE = 1\n",
        )

        self.assertEqual(
            (
                Path(record.workspace.path)
                / "partial.txt"
            ).read_bytes(),
            b"partial work\n",
        )

        self.assertNotEqual(
            record.worker_job.before_snapshot,
            record.worker_job.after_snapshot,
        )

    def test_invalid_seed_manifest_stops_before_worker(self):
        worker = FakeWorker()

        with self.assertRaisesRegex(
            LabWorkspaceSeedError,
            r"\.git content must never be seeded",
        ):
            self._run(
                worker,
                relative_paths=(
                    ".git/config",
                ),
            )

        self.assertEqual(
            worker.calls,
            0,
        )

        self.assertEqual(
            self.project.read_bytes(),
            b"VALUE = 1\n",
        )
        self.assertEqual(
            self.private.read_bytes(),
            b"not approved for the lab\n",
        )

        created = list(
            self.workspace_parent.iterdir()
        )

        self.assertEqual(
            len(created),
            1,
        )
        self.assertEqual(
            list(
                created[0].iterdir()
            ),
            [],
        )


    def test_committed_source_job_flows_through_candidate(self):
        repo, project, head = self._committed_repo()

        worker = PolicyBindingFakeCodexWorker(
            output_name="project.py",
            output_bytes=b"VALUE = 2\n",
            reported_changed_paths=(
                "worker-claim-is-not-authority.txt",
            ),
        )

        before = b"VALUE = 1\n"

        change_policy = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_MODIFY,
                    path="project.py",
                    max_final_bytes=len(
                        b"VALUE = 2\n"
                    ),
                    final_mode=0o600,
                    before_bytes=len(before),
                    before_sha256=hashlib.sha256(
                        before
                    ).hexdigest(),
                    before_mode=0o600,
                ),
            )
        )

        record = run_codex_coding_job_from_commit(
            repository_path=str(repo),
            commit_oid=head,
            expected_branch="main",
            relative_paths=(
                "project.py",
            ),
            workspace_parent=str(
                self.workspace_parent
            ),
            session_id=SESSION_ID,
            goal="Modify one committed file.",
            worker=worker,
            change_policy=change_policy,
            max_seed_files=16,
            max_seed_bytes=1024 * 1024,
            max_runtime_seconds=30,
            max_log_bytes=4096,
        )

        self.assertEqual(
            worker.calls,
            1,
        )
        self.assertTrue(
            record.succeeded
        )
        self.assertIs(
            type(record.seed),
            LabCommittedWorkspaceSeedRecord,
        )
        self.assertEqual(
            record.seed.repository_path,
            str(repo),
        )
        self.assertEqual(
            record.seed.branch,
            "main",
        )
        self.assertEqual(
            record.seed.commit_oid,
            head,
        )
        self.assertEqual(
            record.seed.object_format,
            "sha1",
        )
        self.assertEqual(
            worker.observed_seed,
            before,
        )

        # Source repository is still the committed source image.
        self.assertEqual(
            project.read_bytes(),
            before,
        )

        candidate = build_lab_coding_candidate(
            record,
            workspace_parent=str(
                self.workspace_parent
            ),
        )

        self.assertEqual(
            len(candidate.files),
            1,
        )

        item = candidate.files[0]

        self.assertEqual(
            item.path,
            "project.py",
        )
        self.assertEqual(
            item.operation,
            "MODIFIED",
        )
        self.assertEqual(
            item.before_bytes,
            len(before),
        )
        self.assertEqual(
            item.before_sha256,
            hashlib.sha256(
                before
            ).hexdigest(),
        )

        # Candidate source provenance uses committed Git source mode,
        # while physical worker workspace mode remains private.
        self.assertEqual(
            record.seed.entries[0].git_mode,
            "100644",
        )
        self.assertEqual(
            record.seed.entries[0].source_mode,
            0o644,
        )
        self.assertEqual(
            record.seed.entries[0].mode,
            0o600,
        )
        self.assertEqual(
            item.before_mode,
            0o644,
        )
        self.assertEqual(
            item.final_mode,
            0o600,
        )
        self.assertEqual(
            item.content,
            "VALUE = 2\n",
        )

        self.assertEqual(
            worker.bound_change_policy.policy_id,
            worker.requests[0].change_policy_id,
        )

    def test_committed_source_job_refuses_dirty_repository_before_worker(self):
        repo, _project, head = self._committed_repo()

        (
            repo
            / "untracked.txt"
        ).write_bytes(
            b"unexpected\n"
        )

        worker = FakeWorker()

        change_policy = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path="worker-output.txt",
                    max_final_bytes=len(
                        b"worker output\n"
                    ),
                    final_mode=0o600,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                ),
            )
        )

        with self.assertRaisesRegex(
            LabCommittedWorkspaceSeedError,
            "clean working tree and index",
        ):
            run_codex_coding_job_from_commit(
                repository_path=str(repo),
                commit_oid=head,
                expected_branch="main",
                relative_paths=(
                    "project.py",
                ),
                workspace_parent=str(
                    self.workspace_parent
                ),
                session_id=SESSION_ID,
                goal="This worker must not run.",
                worker=worker,
                change_policy=change_policy,
                max_seed_files=16,
                max_seed_bytes=1024 * 1024,
                max_runtime_seconds=30,
                max_log_bytes=4096,
            )

        self.assertEqual(
            worker.calls,
            0,
        )


if __name__ == "__main__":
    unittest.main()

# ADDED_TARGET_CODING_JOB_REPAIR_REGRESSIONS_V1

import tempfile as _added_job_tempfile
import unittest as _added_job_unittest


def _added_job_make_git_repo(
    tmp_root,
    files,
):
    import os
    import subprocess

    repo = (
        tmp_root
        / "added-job-source"
    )

    repo.mkdir(
        mode=0o700,
    )

    os.chmod(
        repo,
        0o700,
    )

    subprocess.run(
        [
            "git",
            "init",
            "-b",
            "dev",
            str(repo),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "user.name",
            "Auto Lab Test",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "user.email",
            "auto-lab-test@example.invalid",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    for relative, content in files.items():
        target = repo / relative

        target.write_bytes(
            content
        )

        os.chmod(
            target,
            0o644,
        )

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "add",
            "-A",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "commit",
            "--allow-empty",
            "-m",
            "source",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    commit_oid = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.decode(
        "ascii"
    ).strip()

    return (
        repo.resolve(),
        commit_oid,
    )


def _added_job_workspace_parent(
    tmp_root,
):
    import os

    parent = (
        tmp_root
        / "added-job-workspaces"
    )

    parent.mkdir(
        mode=0o700,
    )

    os.chmod(
        parent,
        0o700,
    )

    return parent.resolve()


class _AddedRepairWorker:

    def __init__(
        self,
        writes,
    ):
        self._writes = dict(
            writes
        )

        self.calls = 0

    def run(
        self,
        request,
    ):
        import os
        from pathlib import Path

        from hands_free_auto_lab.lab_worker import (
            STATUS_COMPLETED,
            build_lab_worker_result,
        )

        self.calls += 1

        for relative, content in self._writes.items():
            target = (
                Path(
                    request.workspace.path
                )
                / relative
            )

            target.write_bytes(
                content
            )

            os.chmod(
                target,
                0o600,
            )

        return build_lab_worker_result(
            request=request,
            status=STATUS_COMPLETED,
            summary="bounded repair test completed",
            reported_changed_paths=tuple(
                sorted(
                    self._writes
                )
            ),
            log="",
        )


class AddedTargetCodingJobRepairTests(
    _added_job_unittest.TestCase
):

    def test_all_added_job_reaches_worker(
        self,
    ):
        from pathlib import Path

        from hands_free_auto_lab.lab_coding_job import (
            run_codex_coding_job_from_commit,
        )

        from hands_free_auto_lab.lab_worker_change_policy import (
            LabWorkerChangeTarget,
            OPERATION_ADD,
            build_lab_worker_change_policy,
        )

        with _added_job_tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            repo, commit_oid = _added_job_make_git_repo(
                root,
                {},
            )

            parent = _added_job_workspace_parent(
                root
            )

            policy = build_lab_worker_change_policy(
                targets=(
                    LabWorkerChangeTarget(
                        operation=OPERATION_ADD,
                        path="new.txt",
                        max_final_bytes=128,
                        final_mode=0o600,
                        before_bytes=None,
                        before_sha256=None,
                        before_mode=None,
                    ),
                ),
            )

            worker = _AddedRepairWorker(
                {
                    "new.txt": b"new\n",
                }
            )

            record = run_codex_coding_job_from_commit(
                repository_path=str(repo),
                commit_oid=commit_oid,
                expected_branch="dev",
                relative_paths=(
                    "new.txt",
                ),
                workspace_parent=str(parent),
                session_id="e" * 64,
                goal="Create new.txt.",
                worker=worker,
                change_policy=policy,
                max_seed_files=4,
                max_seed_bytes=4096,
                max_runtime_seconds=30,
                max_log_bytes=4096,
            )

            self.assertEqual(
                worker.calls,
                1,
            )

            self.assertTrue(
                record.succeeded
            )

            self.assertEqual(
                record.seed.entries,
                (),
            )

    def test_mixed_modified_and_added_job(
        self,
    ):
        import hashlib
        from pathlib import Path

        from hands_free_auto_lab.lab_coding_job import (
            run_codex_coding_job_from_commit,
        )

        from hands_free_auto_lab.lab_worker_change_policy import (
            LabWorkerChangeTarget,
            OPERATION_ADD,
            OPERATION_MODIFY,
            build_lab_worker_change_policy,
        )

        old = b"old\n"

        with _added_job_tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            repo, commit_oid = _added_job_make_git_repo(
                root,
                {
                    "existing.txt": old,
                },
            )

            parent = _added_job_workspace_parent(
                root
            )

            policy = build_lab_worker_change_policy(
                targets=(
                    LabWorkerChangeTarget(
                        operation=OPERATION_MODIFY,
                        path="existing.txt",
                        max_final_bytes=128,
                        final_mode=0o600,
                        before_bytes=len(old),
                        before_sha256=hashlib.sha256(
                            old
                        ).hexdigest(),
                        before_mode=0o600,
                    ),
                    LabWorkerChangeTarget(
                        operation=OPERATION_ADD,
                        path="new.txt",
                        max_final_bytes=128,
                        final_mode=0o600,
                        before_bytes=None,
                        before_sha256=None,
                        before_mode=None,
                    ),
                ),
            )

            worker = _AddedRepairWorker(
                {
                    "existing.txt": b"updated\n",
                    "new.txt": b"new\n",
                }
            )

            record = run_codex_coding_job_from_commit(
                repository_path=str(repo),
                commit_oid=commit_oid,
                expected_branch="dev",
                relative_paths=(
                    "existing.txt",
                    "new.txt",
                ),
                workspace_parent=str(parent),
                session_id="f" * 64,
                goal=(
                    "Modify existing.txt and "
                    "create new.txt."
                ),
                worker=worker,
                change_policy=policy,
                max_seed_files=4,
                max_seed_bytes=4096,
                max_runtime_seconds=30,
                max_log_bytes=4096,
            )

            self.assertEqual(
                worker.calls,
                1,
            )

            self.assertTrue(
                record.succeeded
            )

            self.assertEqual(
                tuple(
                    entry.relative_path
                    for entry in record.seed.entries
                ),
                (
                    "existing.txt",
                ),
            )

    def test_added_existing_at_commit_fails_before_worker(
        self,
    ):
        from pathlib import Path

        from hands_free_auto_lab.lab_coding_job import (
            run_codex_coding_job_from_commit,
        )

        from hands_free_auto_lab.lab_committed_workspace_seed import (
            LabCommittedWorkspaceSeedError,
        )

        from hands_free_auto_lab.lab_worker_change_policy import (
            LabWorkerChangeTarget,
            OPERATION_ADD,
            build_lab_worker_change_policy,
        )

        with _added_job_tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            repo, commit_oid = _added_job_make_git_repo(
                root,
                {
                    "existing.txt": b"already here\n",
                },
            )

            parent = _added_job_workspace_parent(
                root
            )

            policy = build_lab_worker_change_policy(
                targets=(
                    LabWorkerChangeTarget(
                        operation=OPERATION_ADD,
                        path="existing.txt",
                        max_final_bytes=128,
                        final_mode=0o600,
                        before_bytes=None,
                        before_sha256=None,
                        before_mode=None,
                    ),
                ),
            )

            worker = _AddedRepairWorker(
                {
                    "existing.txt": b"must not run\n",
                }
            )

            with self.assertRaisesRegex(
                LabCommittedWorkspaceSeedError,
                "expected-absent committed path exists",
            ):
                run_codex_coding_job_from_commit(
                    repository_path=str(repo),
                    commit_oid=commit_oid,
                    expected_branch="dev",
                    relative_paths=(
                        "existing.txt",
                    ),
                    workspace_parent=str(parent),
                    session_id="1" * 64,
                    goal="Create existing.txt.",
                    worker=worker,
                    change_policy=policy,
                    max_seed_files=4,
                    max_seed_bytes=4096,
                    max_runtime_seconds=30,
                    max_log_bytes=4096,
                )

            self.assertEqual(
                worker.calls,
                0,
            )
