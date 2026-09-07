from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

import hands_free_auto_lab.lab_coding_job_lifecycle_journal as journal

from hands_free_auto_lab.lab_coding_job_lifecycle import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_TIMED_OUT,
    build_lab_coding_job_lifecycle,
    transition_lab_coding_job_lifecycle,
)
from hands_free_auto_lab.lab_coding_job_lifecycle_journal import (
    JOBS_DIRECTORY,
    LOCK_FILENAME,
    SNAPSHOTS_DIRECTORY,
    LabCodingJobLifecycleJournalError,
    append_lab_coding_job_lifecycle_snapshot,
    create_lab_coding_job_lifecycle_journal,
    initialize_lab_coding_job_lifecycle_journal_root,
    load_lab_coding_job_lifecycle_journal,
)


REQUEST_ID = "a" * 64
RUN_ID = "b" * 64
SESSION_ID = "c" * 64
WORKER_REQUEST_ID = "d" * 64
WORKER_RESULT_ID = "e" * 64
CANDIDATE_ID = "f" * 64
INTEGRATION_RESULT_ID = "1" * 64


def _requested():
    return build_lab_coding_job_lifecycle(
        request_id=REQUEST_ID,
        run_id=RUN_ID,
    )


def _running():
    return transition_lab_coding_job_lifecycle(
        _requested(),
        state=STATE_RUNNING,
        session_id=SESSION_ID,
        worker_request_id=WORKER_REQUEST_ID,
    )


def _completed():
    return transition_lab_coding_job_lifecycle(
        _running(),
        state=STATE_COMPLETED,
        worker_result_id=WORKER_RESULT_ID,
        candidate_id=CANDIDATE_ID,
        integration_result_id=INTEGRATION_RESULT_ID,
    )


def _failed_from_requested():
    return transition_lab_coding_job_lifecycle(
        _requested(),
        state=STATE_FAILED,
    )


def _failed_from_running():
    return transition_lab_coding_job_lifecycle(
        _running(),
        state=STATE_FAILED,
        worker_result_id=WORKER_RESULT_ID,
    )


def _timed_out():
    return transition_lab_coding_job_lifecycle(
        _running(),
        state=STATE_TIMED_OUT,
        worker_result_id=WORKER_RESULT_ID,
    )


class LabCodingJobLifecycleJournalTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-coding-lifecycle-journal-"
            )
        )

        os.chmod(
            self.root,
            0o700,
        )

        initialize_lab_coding_job_lifecycle_journal_root(
            self.root
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def job_dir(
        self,
        job_id: str,
    ) -> Path:
        return (
            self.root
            / JOBS_DIRECTORY
            / job_id
        )

    def snapshots_dir(
        self,
        job_id: str,
    ) -> Path:
        return (
            self.job_dir(
                job_id
            )
            / SNAPSHOTS_DIRECTORY
        )

    def create(self):
        requested = _requested()

        persisted = create_lab_coding_job_lifecycle_journal(
            self.root,
            lifecycle=requested,
        )

        return (
            requested,
            persisted,
        )

    def test_initialize_creates_exact_private_layout(
        self,
    ):
        self.assertEqual(
            {
                path.name
                for path in self.root.iterdir()
            },
            {
                LOCK_FILENAME,
                JOBS_DIRECTORY,
            },
        )

        self.assertEqual(
            self.root.stat().st_mode
            & 0o777,
            0o700,
        )

        self.assertEqual(
            (
                self.root
                / LOCK_FILENAME
            ).stat().st_mode
            & 0o777,
            0o600,
        )

        self.assertEqual(
            (
                self.root
                / JOBS_DIRECTORY
            ).stat().st_mode
            & 0o777,
            0o700,
        )

    def test_create_round_trip_and_restart_preserve_exact_requested(
        self,
    ):
        requested, persisted = self.create()

        self.assertEqual(
            persisted,
            requested,
        )

        initialize_lab_coding_job_lifecycle_journal_root(
            self.root
        )

        loaded = load_lab_coding_job_lifecycle_journal(
            self.root,
            job_id=requested.job_id,
        )

        self.assertEqual(
            loaded,
            requested,
        )

        job = self.job_dir(
            requested.job_id
        )

        self.assertEqual(
            job.stat().st_mode
            & 0o777,
            0o700,
        )

        snapshots = self.snapshots_dir(
            requested.job_id
        )

        self.assertEqual(
            snapshots.stat().st_mode
            & 0o777,
            0o700,
        )

        initial = (
            snapshots
            / "0000000000000000.json"
        )

        self.assertEqual(
            initial.stat().st_mode
            & 0o777,
            0o600,
        )

    def test_requested_running_completed_chain_survives_reopen(
        self,
    ):
        requested, _ = self.create()
        running = _running()

        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=running,
            expected_previous_snapshot_id=requested.snapshot_id,
        )

        completed = _completed()

        persisted = append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=completed,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        self.assertEqual(
            persisted,
            completed,
        )

        initialize_lab_coding_job_lifecycle_journal_root(
            self.root
        )

        loaded = load_lab_coding_job_lifecycle_journal(
            self.root,
            job_id=requested.job_id,
        )

        self.assertEqual(
            loaded,
            completed,
        )

        self.assertEqual(
            sorted(
                path.name
                for path in self.snapshots_dir(
                    requested.job_id
                ).iterdir()
            ),
            [
                "0000000000000000.json",
                "0000000000000001.json",
                "0000000000000002.json",
            ],
        )

    def test_requested_may_persist_direct_failure(
        self,
    ):
        requested, _ = self.create()
        failed = _failed_from_requested()

        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=failed,
            expected_previous_snapshot_id=requested.snapshot_id,
        )

        loaded = load_lab_coding_job_lifecycle_journal(
            self.root,
            job_id=requested.job_id,
        )

        self.assertEqual(
            loaded,
            failed,
        )

    def test_running_timeout_persists_as_terminal_evidence(
        self,
    ):
        requested, _ = self.create()
        running = _running()

        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=running,
            expected_previous_snapshot_id=requested.snapshot_id,
        )

        timed_out = _timed_out()

        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=timed_out,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        loaded = load_lab_coding_job_lifecycle_journal(
            self.root,
            job_id=requested.job_id,
        )

        self.assertEqual(
            loaded,
            timed_out,
        )

    def test_duplicate_job_creation_is_refused(
        self,
    ):
        requested, _ = self.create()

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "already exists",
        ):
            create_lab_coding_job_lifecycle_journal(
                self.root,
                lifecycle=requested,
            )

    def test_non_requested_initial_lifecycle_is_refused(
        self,
    ):
        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "must start at REQUESTED",
        ):
            create_lab_coding_job_lifecycle_journal(
                self.root,
                lifecycle=_running(),
            )

    def test_stale_previous_snapshot_is_refused(
        self,
    ):
        requested, _ = self.create()
        running = _running()

        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=running,
            expected_previous_snapshot_id=requested.snapshot_id,
        )

        failed = _failed_from_running()

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "stale",
        ):
            append_lab_coding_job_lifecycle_snapshot(
                self.root,
                successor=failed,
                expected_previous_snapshot_id=requested.snapshot_id,
            )

    def test_no_op_append_is_refused(
        self,
    ):
        requested, _ = self.create()

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "no-op",
        ):
            append_lab_coding_job_lifecycle_snapshot(
                self.root,
                successor=requested,
                expected_previous_snapshot_id=requested.snapshot_id,
            )

    def test_illegal_successor_is_refused_before_persistence(
        self,
    ):
        requested, _ = self.create()

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "illegal durable lifecycle transition",
        ):
            append_lab_coding_job_lifecycle_snapshot(
                self.root,
                successor=_completed(),
                expected_previous_snapshot_id=requested.snapshot_id,
            )

        self.assertEqual(
            {
                path.name
                for path in self.snapshots_dir(
                    requested.job_id
                ).iterdir()
            },
            {
                "0000000000000000.json",
            },
        )

    def test_tampered_successor_identity_is_refused(
        self,
    ):
        requested, _ = self.create()
        running = _running()

        tampered = replace(
            running,
            snapshot_id="2" * 64,
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "invalid coding-job lifecycle successor",
        ):
            append_lab_coding_job_lifecycle_snapshot(
                self.root,
                successor=tampered,
                expected_previous_snapshot_id=requested.snapshot_id,
            )

    def test_terminal_lifecycle_has_no_durable_successor(
        self,
    ):
        requested, _ = self.create()
        running = _running()

        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=running,
            expected_previous_snapshot_id=requested.snapshot_id,
        )

        failed = _failed_from_running()

        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=failed,
            expected_previous_snapshot_id=running.snapshot_id,
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "illegal durable lifecycle transition",
        ):
            append_lab_coding_job_lifecycle_snapshot(
                self.root,
                successor=_completed(),
                expected_previous_snapshot_id=failed.snapshot_id,
            )

    def test_concurrent_append_from_same_snapshot_has_one_winner(
        self,
    ):
        requested, _ = self.create()
        running = _running()

        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=running,
            expected_previous_snapshot_id=requested.snapshot_id,
        )

        first = _failed_from_running()
        second = _timed_out()

        barrier = threading.Barrier(
            2
        )

        successes = []
        failures = []

        def worker(
            successor,
        ):
            barrier.wait()

            try:
                result = append_lab_coding_job_lifecycle_snapshot(
                    self.root,
                    successor=successor,
                    expected_previous_snapshot_id=running.snapshot_id,
                )

                successes.append(
                    result
                )

            except LabCodingJobLifecycleJournalError as exc:
                failures.append(
                    str(
                        exc
                    )
                )

        threads = [
            threading.Thread(
                target=worker,
                args=(
                    first,
                ),
            ),
            threading.Thread(
                target=worker,
                args=(
                    second,
                ),
            ),
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join(
                timeout=5
            )

            self.assertFalse(
                thread.is_alive()
            )

        self.assertEqual(
            len(
                successes
            ),
            1,
        )

        self.assertEqual(
            len(
                failures
            ),
            1,
        )

        self.assertIn(
            "stale",
            failures[
                0
            ],
        )

        loaded = load_lab_coding_job_lifecycle_journal(
            self.root,
            job_id=requested.job_id,
        )

        self.assertEqual(
            loaded,
            successes[
                0
            ],
        )

    def test_snapshot_lifecycle_tamper_fails_closed_after_restart(
        self,
    ):
        requested, _ = self.create()

        snapshot = (
            self.snapshots_dir(
                requested.job_id
            )
            / "0000000000000000.json"
        )

        record = json.loads(
            snapshot.read_text(
                encoding="utf-8"
            )
        )

        record[
            "lifecycle"
        ][
            "snapshot_id"
        ] = "3" * 64

        snapshot.write_bytes(
            (
                json.dumps(
                    record,
                    sort_keys=True,
                    separators=(
                        ",",
                        ":",
                    ),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode(
                "utf-8"
            )
        )

        snapshot.chmod(
            0o600
        )

        with self.assertRaises(
            LabCodingJobLifecycleJournalError
        ):
            load_lab_coding_job_lifecycle_journal(
                self.root,
                job_id=requested.job_id,
            )

    def test_noncanonical_snapshot_fails_closed(
        self,
    ):
        requested, _ = self.create()

        snapshot = (
            self.snapshots_dir(
                requested.job_id
            )
            / "0000000000000000.json"
        )

        record = json.loads(
            snapshot.read_text(
                encoding="utf-8"
            )
        )

        snapshot.write_text(
            json.dumps(
                record,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        snapshot.chmod(
            0o600
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "not canonical",
        ):
            load_lab_coding_job_lifecycle_journal(
                self.root,
                job_id=requested.job_id,
            )

    def test_inexact_snapshot_schema_fails_closed(
        self,
    ):
        requested, _ = self.create()

        snapshot = (
            self.snapshots_dir(
                requested.job_id
            )
            / "0000000000000000.json"
        )

        record = json.loads(
            snapshot.read_text(
                encoding="utf-8"
            )
        )

        record[
            "authority"
        ] = True

        snapshot.write_bytes(
            (
                json.dumps(
                    record,
                    sort_keys=True,
                    separators=(
                        ",",
                        ":",
                    ),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode(
                "utf-8"
            )
        )

        snapshot.chmod(
            0o600
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "fields do not exactly match schema",
        ):
            load_lab_coding_job_lifecycle_journal(
                self.root,
                job_id=requested.job_id,
            )

    def test_snapshot_gap_fails_closed(
        self,
    ):
        requested, _ = self.create()
        running = _running()

        append_lab_coding_job_lifecycle_snapshot(
            self.root,
            successor=running,
            expected_previous_snapshot_id=requested.snapshot_id,
        )

        snapshots = self.snapshots_dir(
            requested.job_id
        )

        (
            snapshots
            / "0000000000000001.json"
        ).rename(
            snapshots
            / "0000000000000002.json"
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "non-contiguous",
        ):
            load_lab_coding_job_lifecycle_journal(
                self.root,
                job_id=requested.job_id,
            )

    def test_unexpected_snapshot_entry_fails_closed(
        self,
    ):
        requested, _ = self.create()

        extra = (
            self.snapshots_dir(
                requested.job_id
            )
            / "unexpected"
        )

        extra.write_text(
            "evidence\n",
            encoding="utf-8",
        )

        extra.chmod(
            0o600
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "unexpected",
        ):
            load_lab_coding_job_lifecycle_journal(
                self.root,
                job_id=requested.job_id,
            )

    def test_unexpected_root_entry_fails_closed(
        self,
    ):
        extra = (
            self.root
            / "unexpected"
        )

        extra.write_text(
            "evidence\n",
            encoding="utf-8",
        )

        extra.chmod(
            0o600
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "root entries mismatch",
        ):
            load_lab_coding_job_lifecycle_journal(
                self.root,
                job_id="4" * 64,
            )

    def test_symlinked_snapshot_fails_closed(
        self,
    ):
        requested, _ = self.create()

        snapshot = (
            self.snapshots_dir(
                requested.job_id
            )
            / "0000000000000000.json"
        )

        raw = snapshot.read_bytes()

        outside = (
            self.root.parent
            / (
                "auto-lab-coding-lifecycle-snapshot-"
                + requested.job_id
            )
        )

        outside.write_bytes(
            raw
        )
        outside.chmod(
            0o600
        )

        snapshot.unlink()

        try:
            snapshot.symlink_to(
                outside
            )

            with self.assertRaises(
                LabCodingJobLifecycleJournalError
            ):
                load_lab_coding_job_lifecycle_journal(
                    self.root,
                    job_id=requested.job_id,
                )

        finally:
            if outside.exists():
                outside.unlink()

    def test_hardlinked_snapshot_fails_closed(
        self,
    ):
        requested, _ = self.create()

        snapshot = (
            self.snapshots_dir(
                requested.job_id
            )
            / "0000000000000000.json"
        )

        outside = (
            self.root.parent
            / (
                "auto-lab-coding-lifecycle-hardlink-"
                + requested.job_id
            )
        )

        try:
            os.link(
                snapshot,
                outside,
            )

            with self.assertRaisesRegex(
                LabCodingJobLifecycleJournalError,
                "hard link",
            ):
                load_lab_coding_job_lifecycle_journal(
                    self.root,
                    job_id=requested.job_id,
                )

        finally:
            if outside.exists():
                outside.unlink()

    def test_unsafe_lock_permissions_fail_closed(
        self,
    ):
        (
            self.root
            / LOCK_FILENAME
        ).chmod(
            0o644
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleJournalError,
            "mode 0600",
        ):
            load_lab_coding_job_lifecycle_journal(
                self.root,
                job_id="5" * 64,
            )

    def test_partial_next_snapshot_remains_and_fails_closed(
        self,
    ):
        requested, _ = self.create()
        running = _running()

        real_write_all = journal._write_all
        calls = 0

        def failing_write_all(
            fd,
            data,
        ):
            nonlocal calls
            calls += 1

            if calls == 1:
                os.write(
                    fd,
                    data[
                        :7
                    ],
                )

                raise LabCodingJobLifecycleJournalError(
                    "simulated partial lifecycle snapshot write"
                )

            return real_write_all(
                fd,
                data,
            )

        with patch(
            "hands_free_auto_lab.lab_coding_job_lifecycle_journal._write_all",
            side_effect=failing_write_all,
        ):
            with self.assertRaisesRegex(
                LabCodingJobLifecycleJournalError,
                "simulated partial",
            ):
                append_lab_coding_job_lifecycle_snapshot(
                    self.root,
                    successor=running,
                    expected_previous_snapshot_id=requested.snapshot_id,
                )

        partial = (
            self.snapshots_dir(
                requested.job_id
            )
            / "0000000000000001.json"
        )

        self.assertTrue(
            partial.exists()
        )

        self.assertGreater(
            partial.stat().st_size,
            0,
        )

        with self.assertRaises(
            LabCodingJobLifecycleJournalError
        ):
            load_lab_coding_job_lifecycle_journal(
                self.root,
                job_id=requested.job_id,
            )

    def test_root_and_job_symlink_components_are_refused(
        self,
    ):
        link = (
            self.root.parent
            / (
                self.root.name
                + "-link"
            )
        )

        try:
            link.symlink_to(
                self.root,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                LabCodingJobLifecycleJournalError,
                "canonical",
            ):
                initialize_lab_coding_job_lifecycle_journal_root(
                    link
                )

        finally:
            if link.is_symlink():
                link.unlink()

    def test_module_has_no_execution_repository_or_promotion_authority(
        self,
    ):
        source = Path(
            journal.__file__
        ).read_text(
            encoding="utf-8"
        )

        forbidden = (
            "import subprocess",
            "from subprocess",
            "os.system",
            "os.popen",
            "os.remove",
            "os.unlink",
            "os.rename",
            "os.replace",
            "shell=True",
            "Popen",
            "lab_promotion",
            "lab_executor",
            "lab_coding_candidate",
        )

        for token in forbidden:
            with self.subTest(
                token=token,
            ):
                self.assertNotIn(
                    token,
                    source,
                )


if __name__ == "__main__":
    unittest.main()
