from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from hands_free_auto_lab.lab_worker import (
    CAPABILITY_RUN_PROCESS,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    LabWorkerContractError,
    build_lab_worker_result,
)

from hands_free_auto_lab.lab_worker_job import (
    WORKER_JOB_COMPONENT,
    WORKER_JOB_SCHEMA_VERSION,
    run_lab_worker_job,
)

from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)


CAPABILITIES = (
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    CAPABILITY_RUN_PROCESS,
)


class FakeWorker:
    def __init__(self, status, filename):
        self.status = status
        self.filename = filename
        self.calls = 0

    def run(self, request):
        self.calls += 1

        (
            Path(request.workspace.path)
            / self.filename
        ).write_text(
            "physical truth\n",
            encoding="utf-8",
        )

        return build_lab_worker_result(
            request=request,
            status=self.status,
            summary="fake result",
            reported_changed_paths=(),
            log="fake log",
        )


class TamperedWorker:
    def __init__(self):
        self.calls = 0

    def run(self, request):
        self.calls += 1

        valid = build_lab_worker_result(
            request=request,
            status=STATUS_COMPLETED,
            summary="valid before tamper",
            reported_changed_paths=(),
            log="log",
        )

        return replace(
            valid,
            summary="tampered",
        )


class LabWorkerJobTests(unittest.TestCase):
    def run_job(self, session_id, worker):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            parent.chmod(0o700)
            workspace = create_lab_workspace(
                str(parent),
                session_id=session_id,
            )

            return run_lab_worker_job(
                goal="exercise worker job contract",
                worker_name="fake-worker",
                worker=worker,
                workspace_parent=str(parent),
                workspace=workspace,
                capabilities=CAPABILITIES,
                max_runtime_seconds=30,
                max_log_bytes=4096,
            )

    def test_completed_reports_success_and_independent_physical_diff(self):
        worker = FakeWorker(STATUS_COMPLETED, "completed.txt")

        result = self.run_job("1" * 64, worker)

        self.assertEqual(worker.calls, 1)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.component, WORKER_JOB_COMPONENT)
        self.assertEqual(result.schema_version, WORKER_JOB_SCHEMA_VERSION)
        self.assertEqual(result.result.reported_changed_paths, ())
        self.assertEqual(
            [(entry.path, entry.change) for entry in result.physical_diff.entries],
            [("completed.txt", "ADDED")],
        )

    def test_failed_does_not_retry_and_preserves_partial_physical_diff(self):
        worker = FakeWorker(STATUS_FAILED, "failed.txt")

        result = self.run_job("2" * 64, worker)

        self.assertEqual(worker.calls, 1)
        self.assertFalse(result.succeeded)
        self.assertEqual(
            [(entry.path, entry.change) for entry in result.physical_diff.entries],
            [("failed.txt", "ADDED")],
        )

    def test_timed_out_does_not_retry_and_preserves_partial_physical_diff(self):
        worker = FakeWorker(STATUS_TIMED_OUT, "timed-out.txt")

        result = self.run_job("3" * 64, worker)

        self.assertEqual(worker.calls, 1)
        self.assertFalse(result.succeeded)
        self.assertEqual(
            [(entry.path, entry.change) for entry in result.physical_diff.entries],
            [("timed-out.txt", "ADDED")],
        )

    def test_tampered_result_raises_contract_error_without_retry(self):
        worker = TamperedWorker()

        with self.assertRaises(LabWorkerContractError):
            self.run_job("4" * 64, worker)

        self.assertEqual(worker.calls, 1)


if __name__ == "__main__":
    unittest.main()
