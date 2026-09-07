from __future__ import annotations

import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hands_free_auto_lab.lab_coding_job_status as status

from hands_free_auto_lab.lab_coding_job_lifecycle import (
    LabCodingJobLifecycleError,
    STATE_COMPLETED,
    STATE_RUNNING,
    STATE_TIMED_OUT,
    build_lab_coding_job_lifecycle,
    transition_lab_coding_job_lifecycle,
)
from hands_free_auto_lab.lab_coding_job_lifecycle_journal import (
    LabCodingJobLifecycleJournalError,
    append_lab_coding_job_lifecycle_snapshot,
    create_lab_coding_job_lifecycle_journal,
    initialize_lab_coding_job_lifecycle_journal_root,
)


REQUEST_ID = "1" * 64
RUN_ID = "2" * 64
SESSION_ID = "3" * 64
WORKER_REQUEST_ID = "4" * 64
WORKER_RESULT_ID = "5" * 64
CANDIDATE_ID = "6" * 64
INTEGRATION_RESULT_ID = "7" * 64


class LabCodingJobStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

        self.root = Path(self.temporary.name) / "journal"
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o700)

        initialize_lab_coding_job_lifecycle_journal_root(
            self.root
        )

    def requested(self):
        return build_lab_coding_job_lifecycle(
            request_id=REQUEST_ID,
            run_id=RUN_ID,
        )

    def create_requested(self):
        requested = self.requested()
        create_lab_coding_job_lifecycle_journal(
            self.root,
            lifecycle=requested,
        )
        return requested

    def append_running(self, requested):
        running = transition_lab_coding_job_lifecycle(
            requested,
            state=STATE_RUNNING,
            session_id=SESSION_ID,
        )
        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=running,
            expected_previous_snapshot_id=requested.snapshot_id,
        )
        return running

    def test_requested_status_survives_reopen(self) -> None:
        requested = self.create_requested()

        initialize_lab_coding_job_lifecycle_journal_root(
            self.root
        )

        loaded = status.load_lab_coding_job_status(
            self.root,
            request_id=REQUEST_ID,
            run_id=RUN_ID,
        )

        self.assertEqual(loaded, requested)

    def test_running_status_survives_reopen_without_resume(self) -> None:
        requested = self.create_requested()
        running = self.append_running(requested)

        initialize_lab_coding_job_lifecycle_journal_root(
            self.root
        )

        loaded = status.load_lab_coding_job_status(
            self.root,
            request_id=REQUEST_ID,
            run_id=RUN_ID,
        )

        self.assertEqual(loaded, running)
        self.assertEqual(loaded.state, STATE_RUNNING)

    def test_completed_status_survives_reopen(self) -> None:
        requested = self.create_requested()
        running = self.append_running(requested)

        completed = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_COMPLETED,
            worker_request_id=WORKER_REQUEST_ID,
            worker_result_id=WORKER_RESULT_ID,
            candidate_id=CANDIDATE_ID,
            integration_result_id=INTEGRATION_RESULT_ID,
        )
        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=completed,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        initialize_lab_coding_job_lifecycle_journal_root(
            self.root
        )

        loaded = status.load_lab_coding_job_status(
            self.root,
            request_id=REQUEST_ID,
            run_id=RUN_ID,
        )

        self.assertEqual(loaded, completed)

    def test_timed_out_status_survives_reopen(self) -> None:
        requested = self.create_requested()
        running = self.append_running(requested)

        timed_out = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_TIMED_OUT,
            worker_request_id=WORKER_REQUEST_ID,
            worker_result_id=WORKER_RESULT_ID,
        )
        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=timed_out,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        initialize_lab_coding_job_lifecycle_journal_root(
            self.root
        )

        loaded = status.load_lab_coding_job_status(
            self.root,
            request_id=REQUEST_ID,
            run_id=RUN_ID,
        )

        self.assertEqual(loaded, timed_out)

    def test_exact_request_and_run_derive_existing_job_id(self) -> None:
        requested = self.requested()

        with patch.object(
            status,
            "load_lab_coding_job_lifecycle_journal",
            return_value=requested,
        ) as loader:
            loaded = status.load_lab_coding_job_status(
                self.root,
                request_id=REQUEST_ID,
                run_id=RUN_ID,
            )

        self.assertEqual(loaded, requested)
        loader.assert_called_once_with(
            self.root,
            job_id=requested.job_id,
        )

    def test_invalid_identity_fails_before_journal_load(self) -> None:
        with patch.object(
            status,
            "load_lab_coding_job_lifecycle_journal",
        ) as loader:
            with self.assertRaises(
                LabCodingJobLifecycleError
            ):
                status.load_lab_coding_job_status(
                    self.root,
                    request_id="not-valid",
                    run_id=RUN_ID,
                )

        loader.assert_not_called()

    def test_missing_job_fails_closed(self) -> None:
        with self.assertRaises(
            LabCodingJobLifecycleJournalError
        ):
            status.load_lab_coding_job_status(
                self.root,
                request_id=REQUEST_ID,
                run_id=RUN_ID,
            )

    def test_status_module_has_no_execution_or_authority_imports(self) -> None:
        source = Path(status.__file__).read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        forbidden = {
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "lab_codex_worker",
            "lab_worker_job",
            "lab_coding_job",
            "lab_coding_workflow",
            "lab_coding_integration",
            "lab_promotion",
            "lab_promotion_approval_store",
            "lab_promotion_target_preparer",
        }

        imported = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(
                    alias.name
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    imported.add(node.module)

        for name in imported:
            for blocked in forbidden:
                self.assertFalse(
                    name == blocked
                    or name.endswith("." + blocked),
                    msg=f"forbidden import: {name}",
                )


if __name__ == "__main__":
    unittest.main()
