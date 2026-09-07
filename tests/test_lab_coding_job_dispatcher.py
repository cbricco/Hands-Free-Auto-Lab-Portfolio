from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import call
from unittest.mock import patch

import hands_free_auto_lab.lab_coding_job_dispatcher as dispatcher
from hands_free_auto_lab.lab_coding_integration import (
    LabCodingIntegrationDeployment,
)


class DispatcherSentinelError(RuntimeError):
    pass


class DummyWorker:
    def run(self, request):
        raise AssertionError(
            "DummyWorker.run must not execute in dispatcher unit tests"
        )


class LabCodingJobDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.worker = DummyWorker()

    def item(self, number):
        return SimpleNamespace(
            request=object(),
            run_id=f"{number:064x}",
            job_id=f"{number + 100:064x}",
        )

    def queue(self, count):
        return SimpleNamespace(
            queue_id="f" * 64,
            items=tuple(
                self.item(number)
                for number in range(1, count + 1)
            ),
        )

    def deployment(
        self,
        session_digit,
        *,
        worker=None,
    ):
        return LabCodingIntegrationDeployment(
            worker=(
                self.worker
                if worker is None
                else worker
            ),
            workspace_parent=(
                "/tmp/hands-free-auto-lab-a5b-workspaces"
            ),
            candidate_store_root=(
                "/tmp/hands-free-auto-lab-a5b-candidates"
            ),
            session_id=session_digit * 64,
            max_seed_files=8,
            max_seed_bytes=64 * 1024,
            max_runtime_seconds=30,
            max_log_bytes=64 * 1024,
        )

    def test_success_dispatches_strictly_in_queue_order(self):
        queue = self.queue(3)
        deployments = (
            self.deployment("1"),
            self.deployment("2"),
            self.deployment("3"),
        )
        expected_results = (
            object(),
            object(),
            object(),
        )
        observed = []

        def execute(**kwargs):
            observed.append(
                (
                    kwargs["request"],
                    kwargs["run_id"],
                    kwargs["deployment"],
                    kwargs["lifecycle_journal_root"],
                )
            )
            return expected_results[len(observed) - 1]

        journal_root = object()

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
                side_effect=lambda value: value,
            ),
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
                side_effect=execute,
            ),
        ):
            results = dispatcher.run_lab_coding_job_queue(
                queue=object(),
                deployments=deployments,
                lifecycle_journal_root=journal_root,
            )

        self.assertIs(type(results), tuple)
        self.assertEqual(
            results,
            expected_results,
        )
        self.assertEqual(
            observed,
            [
                (
                    queue.items[index].request,
                    queue.items[index].run_id,
                    deployments[index],
                    journal_root,
                )
                for index in range(3)
            ],
        )

    def test_deployments_must_be_exact_tuple(self):
        queue = self.queue(1)

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
            ) as validate_deployment,
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
            ) as execute,
        ):
            with self.assertRaisesRegex(
                dispatcher.LabCodingJobDispatcherError,
                "exact tuple",
            ):
                dispatcher.run_lab_coding_job_queue(
                    queue=object(),
                    deployments=[
                        self.deployment("1"),
                    ],
                    lifecycle_journal_root=object(),
                )

        validate_deployment.assert_not_called()
        execute.assert_not_called()

    def test_deployment_count_must_match_before_validation(self):
        queue = self.queue(2)

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
            ) as validate_deployment,
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
            ) as execute,
        ):
            with self.assertRaisesRegex(
                dispatcher.LabCodingJobDispatcherError,
                "exactly match",
            ):
                dispatcher.run_lab_coding_job_queue(
                    queue=object(),
                    deployments=(
                        self.deployment("1"),
                    ),
                    lifecycle_journal_root=object(),
                )

        validate_deployment.assert_not_called()
        execute.assert_not_called()

    def test_all_deployments_validate_before_first_execution(self):
        queue = self.queue(2)
        deployments = (
            self.deployment("1"),
            self.deployment("2"),
        )
        events = []

        def validate(value):
            events.append(
                f"validate:{value.session_id}"
            )
            return value

        def execute(**kwargs):
            events.append(
                f"execute:{kwargs['deployment'].session_id}"
            )
            return object()

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
                side_effect=validate,
            ),
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
                side_effect=execute,
            ),
        ):
            dispatcher.run_lab_coding_job_queue(
                queue=object(),
                deployments=deployments,
                lifecycle_journal_root=object(),
            )

        self.assertEqual(
            events,
            [
                f"validate:{deployments[0].session_id}",
                f"validate:{deployments[1].session_id}",
                f"execute:{deployments[0].session_id}",
                f"execute:{deployments[1].session_id}",
            ],
        )

    def test_invalid_deployment_stops_before_any_execution(self):
        queue = self.queue(3)
        first = self.deployment("1")
        invalid = object()
        third = self.deployment("3")
        failure = DispatcherSentinelError(
            "invalid deployment"
        )
        validated = []

        def validate(value):
            validated.append(value)

            if value is invalid:
                raise failure

            return value

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
                side_effect=validate,
            ),
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
            ) as execute,
        ):
            with self.assertRaises(
                DispatcherSentinelError
            ) as raised:
                dispatcher.run_lab_coding_job_queue(
                    queue=object(),
                    deployments=(
                        first,
                        invalid,
                        third,
                    ),
                    lifecycle_journal_root=object(),
                )

        self.assertIs(
            raised.exception,
            failure,
        )
        self.assertEqual(
            validated,
            [
                first,
                invalid,
            ],
        )
        execute.assert_not_called()

    def test_duplicate_session_ids_rejected_before_execution(self):
        queue = self.queue(2)
        deployments = (
            self.deployment("1"),
            self.deployment("1"),
        )

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
                side_effect=lambda value: value,
            ) as validate_deployment,
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
            ) as execute,
        ):
            with self.assertRaisesRegex(
                dispatcher.LabCodingJobDispatcherError,
                "session_id values must be unique",
            ):
                dispatcher.run_lab_coding_job_queue(
                    queue=object(),
                    deployments=deployments,
                    lifecycle_journal_root=object(),
                )

        self.assertEqual(
            validate_deployment.call_count,
            2,
        )
        execute.assert_not_called()

    def test_same_worker_is_allowed_with_unique_sessions(self):
        queue = self.queue(2)
        shared_worker = DummyWorker()
        deployments = (
            self.deployment(
                "1",
                worker=shared_worker,
            ),
            self.deployment(
                "2",
                worker=shared_worker,
            ),
        )

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
                side_effect=lambda value: value,
            ),
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
                side_effect=(
                    object(),
                    object(),
                ),
            ) as execute,
        ):
            dispatcher.run_lab_coding_job_queue(
                queue=object(),
                deployments=deployments,
                lifecycle_journal_root=object(),
            )

        self.assertIs(
            deployments[0].worker,
            deployments[1].worker,
        )
        self.assertEqual(
            execute.call_count,
            2,
        )

    def test_invalid_queue_prevents_deployment_validation(self):
        failure = DispatcherSentinelError(
            "invalid queue"
        )

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                side_effect=failure,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
            ) as validate_deployment,
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
            ) as execute,
        ):
            with self.assertRaises(
                DispatcherSentinelError
            ) as raised:
                dispatcher.run_lab_coding_job_queue(
                    queue=object(),
                    deployments=(),
                    lifecycle_journal_root=object(),
                )

        self.assertIs(
            raised.exception,
            failure,
        )
        validate_deployment.assert_not_called()
        execute.assert_not_called()

    def test_first_execution_failure_propagates_unchanged_and_stops(self):
        queue = self.queue(3)
        deployments = (
            self.deployment("1"),
            self.deployment("2"),
            self.deployment("3"),
        )
        failure = DispatcherSentinelError(
            "worker failure"
        )

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
                side_effect=lambda value: value,
            ),
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
                side_effect=failure,
            ) as execute,
        ):
            with self.assertRaises(
                DispatcherSentinelError
            ) as raised:
                dispatcher.run_lab_coding_job_queue(
                    queue=object(),
                    deployments=deployments,
                    lifecycle_journal_root=object(),
                )

        self.assertIs(
            raised.exception,
            failure,
        )
        self.assertEqual(
            execute.call_count,
            1,
        )
        self.assertIs(
            execute.call_args.kwargs["request"],
            queue.items[0].request,
        )

    def test_completed_earlier_item_is_not_retried_after_later_failure(self):
        queue = self.queue(3)
        deployments = (
            self.deployment("1"),
            self.deployment("2"),
            self.deployment("3"),
        )
        first_result = object()
        failure = DispatcherSentinelError(
            "second failed"
        )

        def execute(**kwargs):
            if kwargs["request"] is queue.items[0].request:
                return first_result

            if kwargs["request"] is queue.items[1].request:
                raise failure

            raise AssertionError(
                "third queue item must never execute"
            )

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
                side_effect=lambda value: value,
            ),
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
                side_effect=execute,
            ) as run_seam,
        ):
            with self.assertRaises(
                DispatcherSentinelError
            ) as raised:
                dispatcher.run_lab_coding_job_queue(
                    queue=object(),
                    deployments=deployments,
                    lifecycle_journal_root=object(),
                )

        self.assertIs(
            raised.exception,
            failure,
        )
        self.assertEqual(
            run_seam.call_count,
            2,
        )
        self.assertEqual(
            [
                entry.kwargs["request"]
                for entry in run_seam.call_args_list
            ],
            [
                queue.items[0].request,
                queue.items[1].request,
            ],
        )

    def test_lifecycle_journal_root_is_forwarded_unchanged(self):
        queue = self.queue(1)
        deployment = self.deployment("1")
        journal_root = object()

        with (
            patch.object(
                dispatcher,
                "validate_lab_coding_job_queue",
                return_value=queue,
            ),
            patch.object(
                dispatcher,
                "validate_lab_coding_integration_deployment",
                return_value=deployment,
            ),
            patch.object(
                dispatcher,
                "run_lab_coding_integration_request_with_lifecycle",
                return_value=object(),
            ) as execute,
        ):
            dispatcher.run_lab_coding_job_queue(
                queue=object(),
                deployments=(
                    deployment,
                ),
                lifecycle_journal_root=journal_root,
            )

        self.assertIs(
            execute.call_args.kwargs["lifecycle_journal_root"],
            journal_root,
        )
        self.assertEqual(
            execute.call_args_list,
            [
                call(
                    request=queue.items[0].request,
                    deployment=deployment,
                    run_id=queue.items[0].run_id,
                    lifecycle_journal_root=journal_root,
                )
            ],
        )

    def test_dispatcher_has_no_lower_execution_or_identity_authority(self):
        source = inspect.getsource(
            dispatcher
        )
        tree = ast.parse(
            source
        )

        imported_modules = set()

        for node in tree.body:
            if isinstance(node, ast.Import):
                imported_modules.update(
                    alias.name
                    for alias in node.names
                )

            elif isinstance(node, ast.ImportFrom):
                imported_modules.add(
                    node.module or ""
                )

        self.assertEqual(
            imported_modules,
            {
                "__future__",
                "lab_coding_integration",
                "lab_coding_integration_contract",
                "lab_coding_job_queue",
            },
        )

        calls = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            if isinstance(node.func, ast.Name):
                calls.append(
                    node.func.id
                )
            elif isinstance(node.func, ast.Attribute):
                calls.append(
                    node.func.attr
                )

        forbidden_calls = {
            "create_lab_workspace",
            "run_codex_coding_job_from_commit",
            "run_lab_worker_job",
            "build_lab_coding_job_lifecycle",
            "create_lab_coding_job_lifecycle_journal",
            "append_lab_coding_job_lifecycle_snapshot",
            "uuid4",
            "token_hex",
            "open",
            "system",
            "Popen",
        }

        self.assertFalse(
            forbidden_calls.intersection(
                calls
            )
        )

        self.assertEqual(
            calls.count(
                "run_lab_coding_integration_request_with_lifecycle"
            ),
            1,
        )

        run_functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_lab_coding_job_queue"
        ]

        self.assertEqual(
            len(run_functions),
            1,
        )

        self.assertFalse(
            any(
                isinstance(node, ast.Try)
                for node in ast.walk(
                    run_functions[0]
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
