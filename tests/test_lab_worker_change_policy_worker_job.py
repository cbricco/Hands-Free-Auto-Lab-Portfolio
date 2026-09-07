from pathlib import Path
import tempfile
import unittest

from hands_free_auto_lab.lab_worker import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    build_lab_worker_result,
)

from hands_free_auto_lab.lab_worker_change_policy import (
    LabWorkerChangePolicyError,
    LabWorkerChangeTarget,
    OPERATION_ADD,
    build_lab_worker_change_policy,
)

from hands_free_auto_lab.lab_worker_job import (
    run_lab_worker_job,
)

from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)


class WritingWorker:
    def __init__(
        self,
        *,
        writes,
        status=STATUS_COMPLETED,
        reported_changed_paths=("worker-claim.txt",),
    ):
        self.writes = tuple(
            writes
        )

        self.status = status
        self.reported_changed_paths = tuple(
            reported_changed_paths
        )

        self.calls = 0
        self.requests = []

    def run(
        self,
        request,
    ):
        self.calls += 1

        self.requests.append(
            request
        )

        root = Path(
            request.workspace.path
        )

        for relative, content, mode in self.writes:
            target = (
                root
                / relative
            )

            target.write_bytes(
                content
            )

            target.chmod(
                mode
            )

        return build_lab_worker_result(
            request=request,
            status=self.status,
            summary="worker result",
            reported_changed_paths=self.reported_changed_paths,
            log="worker log\n",
        )


class LabWorkerChangePolicyWorkerJobTests(
    unittest.TestCase
):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

        self.parent = Path(
            self.temp.name
        )

        self.parent.chmod(
            0o700
        )

        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def workspace(self):
        self.counter += 1

        return create_lab_workspace(
            str(
                self.parent
            ),
            session_id=f"{self.counter:064x}",
        )

    def policy(
        self,
        *paths,
    ):
        return build_lab_worker_change_policy(
            targets=tuple(
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path=path,
                    max_final_bytes=4096,
                    final_mode=0o600,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                )
                for path in paths
            )
        )

    def run_job(
        self,
        *,
        workspace,
        worker,
        policy,
    ):
        return run_lab_worker_job(
            goal="bounded worker-job policy test",
            worker_name="fake-worker",
            worker=worker,
            workspace_parent=str(
                self.parent
            ),
            workspace=workspace,
            capabilities=(),
            max_runtime_seconds=30,
            max_log_bytes=4096,
            change_policy=policy,
        )

    def test_policy_id_is_bound_and_policy_is_retained(self):
        workspace = self.workspace()

        policy = self.policy(
            "actual.txt"
        )

        worker = WritingWorker(
            writes=(
                (
                    "actual.txt",
                    b"physical truth\n",
                    0o600,
                ),
            ),
            reported_changed_paths=(
                "claimed.txt",
            ),
        )

        record = self.run_job(
            workspace=workspace,
            worker=worker,
            policy=policy,
        )

        self.assertTrue(
            record.succeeded
        )

        self.assertIs(
            record.change_policy,
            policy,
        )

        self.assertEqual(
            record.request.change_policy_id,
            policy.policy_id,
        )

        self.assertEqual(
            tuple(
                item.path
                for item in record.physical_diff.entries
            ),
            (
                "actual.txt",
            ),
        )

        self.assertEqual(
            record.result.reported_changed_paths,
            (
                "claimed.txt",
            ),
        )

    def test_add_precondition_stops_before_worker_execution(self):
        workspace = self.workspace()

        existing = (
            Path(
                workspace.path
            )
            / "new.txt"
        )

        existing.write_bytes(
            b"already exists\n"
        )

        existing.chmod(
            0o600
        )

        worker = WritingWorker(
            writes=()
        )

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "already exists",
        ):
            self.run_job(
                workspace=workspace,
                worker=worker,
                policy=self.policy(
                    "new.txt"
                ),
            )

        self.assertEqual(
            worker.calls,
            0,
        )

    def test_out_of_policy_physical_path_fails_closed(self):
        workspace = self.workspace()

        worker = WritingWorker(
            writes=(
                (
                    "escape.txt",
                    b"outside policy\n",
                    0o600,
                ),
            )
        )

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "outside policy",
        ):
            self.run_job(
                workspace=workspace,
                worker=worker,
                policy=self.policy(
                    "allowed.txt"
                ),
            )

        self.assertEqual(
            worker.calls,
            1,
        )

    def test_completed_result_requires_full_exact_policy(self):
        workspace = self.workspace()

        worker = WritingWorker(
            writes=(
                (
                    "a.txt",
                    b"a\n",
                    0o600,
                ),
            ),
            status=STATUS_COMPLETED,
        )

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "does not exactly match",
        ):
            self.run_job(
                workspace=workspace,
                worker=worker,
                policy=self.policy(
                    "a.txt",
                    "b.txt",
                ),
            )

    def test_failed_result_may_preserve_safe_policy_subset(self):
        workspace = self.workspace()

        policy = self.policy(
            "a.txt",
            "b.txt",
        )

        worker = WritingWorker(
            writes=(
                (
                    "a.txt",
                    b"partial\n",
                    0o600,
                ),
            ),
            status=STATUS_FAILED,
        )

        record = self.run_job(
            workspace=workspace,
            worker=worker,
            policy=policy,
        )

        self.assertFalse(
            record.succeeded
        )

        self.assertEqual(
            record.request.change_policy_id,
            policy.policy_id,
        )

        self.assertEqual(
            tuple(
                item.path
                for item in record.physical_diff.entries
            ),
            (
                "a.txt",
            ),
        )


if __name__ == "__main__":
    unittest.main()
