from __future__ import annotations

from dataclasses import replace
import unittest

from hands_free_auto_lab.lab_coding_job_lifecycle import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_REQUESTED,
    STATE_RUNNING,
    STATE_TIMED_OUT,
    LabCodingJobLifecycleError,
    build_lab_coding_job_lifecycle,
    transition_lab_coding_job_lifecycle,
    validate_lab_coding_job_lifecycle,
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


def _running(
    *,
    worker_request: bool = True,
):
    kwargs = {
        "state": STATE_RUNNING,
        "session_id": SESSION_ID,
    }

    if worker_request:
        kwargs[
            "worker_request_id"
        ] = WORKER_REQUEST_ID

    return transition_lab_coding_job_lifecycle(
        _requested(),
        **kwargs,
    )


class LabCodingJobLifecycleTests(
    unittest.TestCase
):
    def test_build_is_deterministic_and_binds_request_and_run(
        self,
    ):
        first = _requested()
        second = _requested()

        self.assertEqual(
            first,
            second,
        )

        self.assertEqual(
            first.state,
            STATE_REQUESTED,
        )

        self.assertEqual(
            first.request_id,
            REQUEST_ID,
        )

        self.assertEqual(
            first.run_id,
            RUN_ID,
        )

        self.assertIsNone(
            first.session_id
        )

        self.assertIsNone(
            first.worker_request_id
        )

        self.assertIsNone(
            first.worker_result_id
        )

        self.assertIsNone(
            first.candidate_id
        )

        self.assertIsNone(
            first.integration_result_id
        )

    def test_different_run_changes_job_and_snapshot_identity(
        self,
    ):
        first = _requested()

        second = build_lab_coding_job_lifecycle(
            request_id=REQUEST_ID,
            run_id="2" * 64,
        )

        self.assertNotEqual(
            first.job_id,
            second.job_id,
        )

        self.assertNotEqual(
            first.snapshot_id,
            second.snapshot_id,
        )

    def test_request_and_run_identifiers_fail_closed(
        self,
    ):
        bad_values = (
            "",
            "a" * 63,
            "A" * 64,
            "g" * 64,
            123,
            True,
        )

        for value in bad_values:
            with self.subTest(
                value=value,
            ):
                with self.assertRaises(
                    LabCodingJobLifecycleError
                ):
                    build_lab_coding_job_lifecycle(
                        request_id=value,
                        run_id=RUN_ID,
                    )

                with self.assertRaises(
                    LabCodingJobLifecycleError
                ):
                    build_lab_coding_job_lifecycle(
                        request_id=REQUEST_ID,
                        run_id=value,
                    )

    def test_requested_to_running_records_session_and_optional_worker_request(
        self,
    ):
        running_without_worker_request = _running(
            worker_request=False
        )

        self.assertEqual(
            running_without_worker_request.state,
            STATE_RUNNING,
        )

        self.assertEqual(
            running_without_worker_request.session_id,
            SESSION_ID,
        )

        self.assertIsNone(
            running_without_worker_request.worker_request_id
        )

        running_with_worker_request = _running()

        self.assertEqual(
            running_with_worker_request.worker_request_id,
            WORKER_REQUEST_ID,
        )

    def test_running_requires_session_id(
        self,
    ):
        with self.assertRaisesRegex(
            LabCodingJobLifecycleError,
            "RUNNING requires session_id",
        ):
            transition_lab_coding_job_lifecycle(
                _requested(),
                state=STATE_RUNNING,
            )

    def test_requested_may_fail_before_worker_start(
        self,
    ):
        failed = transition_lab_coding_job_lifecycle(
            _requested(),
            state=STATE_FAILED,
        )

        self.assertEqual(
            failed.state,
            STATE_FAILED,
        )

        self.assertIsNone(
            failed.session_id
        )

        self.assertIsNone(
            failed.worker_request_id
        )

        self.assertIsNone(
            failed.worker_result_id
        )

    def test_requested_cannot_skip_directly_to_completed_or_timed_out(
        self,
    ):
        for state in (
            STATE_COMPLETED,
            STATE_TIMED_OUT,
        ):
            with self.subTest(
                state=state,
            ):
                with self.assertRaisesRegex(
                    LabCodingJobLifecycleError,
                    "illegal coding-job lifecycle transition",
                ):
                    transition_lab_coding_job_lifecycle(
                        _requested(),
                        state=state,
                    )

    def test_running_to_completed_requires_all_success_evidence(
        self,
    ):
        running = _running()

        with self.assertRaisesRegex(
            LabCodingJobLifecycleError,
            "COMPLETED requires exact terminal evidence",
        ):
            transition_lab_coding_job_lifecycle(
                running,
                state=STATE_COMPLETED,
                worker_result_id=WORKER_RESULT_ID,
                candidate_id=CANDIDATE_ID,
            )

        completed = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_COMPLETED,
            worker_result_id=WORKER_RESULT_ID,
            candidate_id=CANDIDATE_ID,
            integration_result_id=INTEGRATION_RESULT_ID,
        )

        self.assertEqual(
            completed.state,
            STATE_COMPLETED,
        )

        self.assertEqual(
            completed.session_id,
            SESSION_ID,
        )

        self.assertEqual(
            completed.worker_request_id,
            WORKER_REQUEST_ID,
        )

        self.assertEqual(
            completed.worker_result_id,
            WORKER_RESULT_ID,
        )

        self.assertEqual(
            completed.candidate_id,
            CANDIDATE_ID,
        )

        self.assertEqual(
            completed.integration_result_id,
            INTEGRATION_RESULT_ID,
        )

    def test_running_to_failed_preserves_available_worker_evidence(
        self,
    ):
        running = _running()

        failed = transition_lab_coding_job_lifecycle(
            running,
            state=STATE_FAILED,
            worker_result_id=WORKER_RESULT_ID,
        )

        self.assertEqual(
            failed.state,
            STATE_FAILED,
        )

        self.assertEqual(
            failed.session_id,
            SESSION_ID,
        )

        self.assertEqual(
            failed.worker_request_id,
            WORKER_REQUEST_ID,
        )

        self.assertEqual(
            failed.worker_result_id,
            WORKER_RESULT_ID,
        )

        self.assertIsNone(
            failed.candidate_id
        )

        self.assertIsNone(
            failed.integration_result_id
        )

    def test_running_to_timed_out_is_terminal_evidence(
        self,
    ):
        timed_out = transition_lab_coding_job_lifecycle(
            _running(),
            state=STATE_TIMED_OUT,
            worker_result_id=WORKER_RESULT_ID,
        )

        self.assertEqual(
            timed_out.state,
            STATE_TIMED_OUT,
        )

        self.assertEqual(
            timed_out.session_id,
            SESSION_ID,
        )

        self.assertEqual(
            timed_out.worker_result_id,
            WORKER_RESULT_ID,
        )

    def test_failure_and_timeout_reject_success_evidence(
        self,
    ):
        for state in (
            STATE_FAILED,
            STATE_TIMED_OUT,
        ):
            with self.subTest(
                state=state,
            ):
                with self.assertRaisesRegex(
                    LabCodingJobLifecycleError,
                    "successful candidate/result evidence",
                ):
                    transition_lab_coding_job_lifecycle(
                        _running(),
                        state=state,
                        candidate_id=CANDIDATE_ID,
                    )

    def test_worker_request_requires_session_and_result_requires_request(
        self,
    ):
        requested = _requested()

        with self.assertRaisesRegex(
            LabCodingJobLifecycleError,
            "worker_request_id requires session_id",
        ):
            transition_lab_coding_job_lifecycle(
                requested,
                state=STATE_FAILED,
                worker_request_id=WORKER_REQUEST_ID,
            )

        running = _running(
            worker_request=False
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleError,
            "worker_result_id requires worker_request_id",
        ):
            transition_lab_coding_job_lifecycle(
                running,
                state=STATE_FAILED,
                worker_result_id=WORKER_RESULT_ID,
            )

    def test_terminal_states_have_no_successors(
        self,
    ):
        terminal = (
            transition_lab_coding_job_lifecycle(
                _requested(),
                state=STATE_FAILED,
            ),
            transition_lab_coding_job_lifecycle(
                _running(),
                state=STATE_TIMED_OUT,
                worker_result_id=WORKER_RESULT_ID,
            ),
            transition_lab_coding_job_lifecycle(
                _running(),
                state=STATE_COMPLETED,
                worker_result_id=WORKER_RESULT_ID,
                candidate_id=CANDIDATE_ID,
                integration_result_id=INTEGRATION_RESULT_ID,
            ),
        )

        for lifecycle in terminal:
            with self.subTest(
                state=lifecycle.state,
            ):
                with self.assertRaisesRegex(
                    LabCodingJobLifecycleError,
                    "illegal coding-job lifecycle transition",
                ):
                    transition_lab_coding_job_lifecycle(
                        lifecycle,
                        state=STATE_RUNNING,
                        session_id=SESSION_ID,
                    )

    def test_retry_resume_and_unknown_states_do_not_exist(
        self,
    ):
        for state in (
            "RETRY",
            "RESUME",
            "QUEUED",
            "PROMOTED",
        ):
            with self.subTest(
                state=state,
            ):
                with self.assertRaisesRegex(
                    LabCodingJobLifecycleError,
                    "unknown successor state",
                ):
                    transition_lab_coding_job_lifecycle(
                        _requested(),
                        state=state,
                    )

    def test_recorded_identifiers_cannot_change(
        self,
    ):
        running = _running()

        with self.assertRaisesRegex(
            LabCodingJobLifecycleError,
            "session_id cannot change once recorded",
        ):
            transition_lab_coding_job_lifecycle(
                running,
                state=STATE_FAILED,
                session_id="2" * 64,
            )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleError,
            "worker_request_id cannot change once recorded",
        ):
            transition_lab_coding_job_lifecycle(
                running,
                state=STATE_FAILED,
                worker_request_id="3" * 64,
            )

    def test_tampered_job_identity_is_rejected(
        self,
    ):
        tampered = replace(
            _requested(),
            job_id="4" * 64,
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleError,
            "job_id",
        ):
            validate_lab_coding_job_lifecycle(
                tampered
            )

    def test_tampered_snapshot_identity_is_rejected(
        self,
    ):
        tampered = replace(
            _requested(),
            snapshot_id="5" * 64,
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleError,
            "snapshot_id",
        ):
            validate_lab_coding_job_lifecycle(
                tampered
            )

    def test_requested_snapshot_cannot_carry_execution_evidence(
        self,
    ):
        requested = _requested()

        tampered = replace(
            requested,
            session_id=SESSION_ID,
        )

        with self.assertRaisesRegex(
            LabCodingJobLifecycleError,
            "REQUESTED cannot contain",
        ):
            validate_lab_coding_job_lifecycle(
                replace(
                    tampered,
                    snapshot_id=requested.snapshot_id,
                )
            )

    def test_lifecycle_is_descriptive_evidence_not_authority(
        self,
    ):
        lifecycle = _requested()

        for attribute in (
            "approval",
            "authorization",
            "permission",
            "execution_authority",
            "promotion_authority",
            "retry_authority",
            "resume_authority",
            "commit_authority",
            "push_authority",
        ):
            with self.subTest(
                attribute=attribute,
            ):
                self.assertFalse(
                    hasattr(
                        lifecycle,
                        attribute,
                    )
                )


if __name__ == "__main__":
    unittest.main()
