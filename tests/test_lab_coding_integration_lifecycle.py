from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields
import ast
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import hands_free_auto_lab.lab_coding_integration as integration

from hands_free_auto_lab.lab_coding_integration import (
    LabCodingIntegrationError,
    run_lab_coding_integration_request_with_lifecycle,
)
from hands_free_auto_lab.lab_coding_integration_contract import (
    LabCodingIntegrationRequest,
)
from hands_free_auto_lab.lab_coding_job_lifecycle import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_REQUESTED,
    STATE_RUNNING,
    STATE_TIMED_OUT,
    build_lab_coding_job_lifecycle,
)
from hands_free_auto_lab.lab_coding_job_lifecycle_journal import (
    initialize_lab_coding_job_lifecycle_journal_root,
    load_lab_coding_job_lifecycle_journal,
)
from hands_free_auto_lab.lab_worker import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
)


REQUEST_ID = "a" * 64
RUN_ID = "b" * 64
SESSION_ID = "c" * 64
WORKER_REQUEST_ID = "d" * 64
WORKER_RESULT_ID = "e" * 64
CANDIDATE_ID = "f" * 64
INTEGRATION_RESULT_ID = "1" * 64


class LabCodingIntegrationLifecycleTests(
    unittest.TestCase
):
    def setUp(self):
        self.base = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-integration-lifecycle-"
            )
        )

        self.journal_root = (
            self.base
            / "lifecycle-journal"
        )

        self.journal_root.mkdir(
            mode=0o700
        )

        os.chmod(
            self.journal_root,
            0o700,
        )

        initialize_lab_coding_job_lifecycle_journal_root(
            self.journal_root
        )

    def tearDown(self):
        shutil.rmtree(
            self.base
        )

    def request(self):
        return SimpleNamespace(
            request_id=REQUEST_ID,
            repository_path="/tmp/a3-source-repository",
            commit_oid="2" * 40,
            expected_branch="dev",
            relative_paths=(
                "alpha.py",
            ),
            goal="A3 lifecycle integration test",
            change_policy=object(),
            max_runtime_seconds=120,
        )

    def deployment(self):
        return SimpleNamespace(
            worker=object(),
            workspace_parent="/tmp/a3-workspaces",
            candidate_store_root="/tmp/a3-candidate-store",
            session_id=SESSION_ID,
            max_seed_files=10,
            max_seed_bytes=10000,
            max_runtime_seconds=90,
            max_log_bytes=10000,
        )

    def coding_job(
        self,
        status=STATUS_COMPLETED,
    ):
        return SimpleNamespace(
            worker_job=SimpleNamespace(
                request=SimpleNamespace(
                    request_id=WORKER_REQUEST_ID,
                ),
                result=SimpleNamespace(
                    result_id=WORKER_RESULT_ID,
                    status=status,
                ),
            ),
        )

    def workflow(self):
        return SimpleNamespace(
            candidate=SimpleNamespace(
                candidate_id=CANDIDATE_ID,
            ),
        )

    def result(self):
        return SimpleNamespace(
            result_id=INTEGRATION_RESULT_ID,
            request_id=REQUEST_ID,
            candidate_id=CANDIDATE_ID,
        )

    def current(
        self,
        *,
        run_id=RUN_ID,
    ):
        requested = build_lab_coding_job_lifecycle(
            request_id=REQUEST_ID,
            run_id=run_id,
        )

        return load_lab_coding_job_lifecycle_journal(
            self.journal_root,
            job_id=requested.job_id,
        )

    def call(
        self,
        *,
        run_id=RUN_ID,
    ):
        return run_lab_coding_integration_request_with_lifecycle(
            request=self.request(),
            deployment=self.deployment(),
            run_id=run_id,
            lifecycle_journal_root=self.journal_root,
        )

    @contextmanager
    def patched_execution(
        self,
        *,
        status=STATUS_COMPLETED,
        job_error=None,
        finalizer_error=None,
        result_error=None,
    ):
        request = self.request()
        deployment = self.deployment()
        coding_job = self.coding_job(
            status
        )
        workflow = self.workflow()
        result = self.result()

        with patch.object(
            integration,
            "validate_lab_coding_integration_request",
            return_value=request,
        ), patch.object(
            integration,
            "validate_lab_coding_integration_deployment",
            return_value=deployment,
        ), patch.object(
            integration,
            "run_codex_coding_job_from_commit",
            return_value=coding_job,
        ) as job_call, patch.object(
            integration,
            "finalize_lab_coding_workflow",
            return_value=workflow,
        ) as finalizer_call, patch.object(
            integration,
            "build_lab_coding_integration_result",
            return_value=result,
        ) as result_call:

            if job_error is not None:
                job_call.side_effect = job_error

            if finalizer_error is not None:
                finalizer_call.side_effect = finalizer_error

            if result_error is not None:
                result_call.side_effect = result_error

            yield SimpleNamespace(
                request=request,
                deployment=deployment,
                coding_job=coding_job,
                workflow=workflow,
                result=result,
                job_call=job_call,
                finalizer_call=finalizer_call,
                result_call=result_call,
            )

    def test_success_records_exact_completed_evidence(
        self,
    ):
        with self.patched_execution() as calls:
            result = self.call()

        self.assertIs(
            result,
            calls.result,
        )

        lifecycle = self.current()

        self.assertEqual(
            lifecycle.state,
            STATE_COMPLETED,
        )
        self.assertEqual(
            lifecycle.request_id,
            REQUEST_ID,
        )
        self.assertEqual(
            lifecycle.run_id,
            RUN_ID,
        )
        self.assertEqual(
            lifecycle.session_id,
            SESSION_ID,
        )
        self.assertEqual(
            lifecycle.worker_request_id,
            WORKER_REQUEST_ID,
        )
        self.assertEqual(
            lifecycle.worker_result_id,
            WORKER_RESULT_ID,
        )
        self.assertEqual(
            lifecycle.candidate_id,
            CANDIDATE_ID,
        )
        self.assertEqual(
            lifecycle.integration_result_id,
            INTEGRATION_RESULT_ID,
        )

        self.assertEqual(
            calls.job_call.call_count,
            1,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            1,
        )
        self.assertEqual(
            calls.result_call.call_count,
            1,
        )

    def test_worker_runtime_uses_lower_request_and_local_ceiling(
        self,
    ):
        with self.patched_execution() as calls:
            self.call()

        self.assertEqual(
            calls.job_call.call_args.kwargs[
                "max_runtime_seconds"
            ],
            90,
        )
        self.assertEqual(
            calls.job_call.call_count,
            1,
        )

    def test_failed_worker_records_exact_terminal_ids(
        self,
    ):
        with self.patched_execution(
            status=STATUS_FAILED,
        ) as calls:
            with self.assertRaisesRegex(
                LabCodingIntegrationError,
                "FAILED",
            ):
                self.call()

        lifecycle = self.current()

        self.assertEqual(
            lifecycle.state,
            STATE_FAILED,
        )
        self.assertEqual(
            lifecycle.worker_request_id,
            WORKER_REQUEST_ID,
        )
        self.assertEqual(
            lifecycle.worker_result_id,
            WORKER_RESULT_ID,
        )
        self.assertIsNone(
            lifecycle.candidate_id
        )
        self.assertIsNone(
            lifecycle.integration_result_id
        )

        self.assertEqual(
            calls.job_call.call_count,
            1,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            0,
        )
        self.assertEqual(
            calls.result_call.call_count,
            0,
        )

    def test_timed_out_worker_records_exact_terminal_ids(
        self,
    ):
        with self.patched_execution(
            status=STATUS_TIMED_OUT,
        ) as calls:
            with self.assertRaisesRegex(
                LabCodingIntegrationError,
                "TIMED_OUT",
            ):
                self.call()

        lifecycle = self.current()

        self.assertEqual(
            lifecycle.state,
            STATE_TIMED_OUT,
        )
        self.assertEqual(
            lifecycle.session_id,
            SESSION_ID,
        )
        self.assertEqual(
            lifecycle.worker_request_id,
            WORKER_REQUEST_ID,
        )
        self.assertEqual(
            lifecycle.worker_result_id,
            WORKER_RESULT_ID,
        )
        self.assertIsNone(
            lifecycle.candidate_id
        )
        self.assertIsNone(
            lifecycle.integration_result_id
        )

        self.assertEqual(
            calls.job_call.call_count,
            1,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            0,
        )

    def test_execution_exception_records_failed_without_invented_worker_ids(
        self,
    ):
        with self.patched_execution(
            job_error=RuntimeError(
                "execution failed before a record returned"
            ),
        ) as calls:
            with self.assertRaisesRegex(
                RuntimeError,
                "before a record returned",
            ):
                self.call()

        lifecycle = self.current()

        self.assertEqual(
            lifecycle.state,
            STATE_FAILED,
        )
        self.assertEqual(
            lifecycle.session_id,
            SESSION_ID,
        )
        self.assertIsNone(
            lifecycle.worker_request_id
        )
        self.assertIsNone(
            lifecycle.worker_result_id
        )
        self.assertIsNone(
            lifecycle.candidate_id
        )

        self.assertEqual(
            calls.job_call.call_count,
            1,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            0,
        )

    def test_candidate_finalization_failure_records_failed_worker_evidence(
        self,
    ):
        with self.patched_execution(
            finalizer_error=RuntimeError(
                "candidate persistence failed"
            ),
        ) as calls:
            with self.assertRaisesRegex(
                RuntimeError,
                "candidate persistence failed",
            ):
                self.call()

        lifecycle = self.current()

        self.assertEqual(
            lifecycle.state,
            STATE_FAILED,
        )
        self.assertEqual(
            lifecycle.worker_request_id,
            WORKER_REQUEST_ID,
        )
        self.assertEqual(
            lifecycle.worker_result_id,
            WORKER_RESULT_ID,
        )
        self.assertIsNone(
            lifecycle.candidate_id
        )
        self.assertIsNone(
            lifecycle.integration_result_id
        )

        self.assertEqual(
            calls.job_call.call_count,
            1,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            1,
        )
        self.assertEqual(
            calls.result_call.call_count,
            0,
        )

    def test_result_construction_failure_records_failed_worker_evidence(
        self,
    ):
        with self.patched_execution(
            result_error=RuntimeError(
                "integration result failed"
            ),
        ) as calls:
            with self.assertRaisesRegex(
                RuntimeError,
                "integration result failed",
            ):
                self.call()

        lifecycle = self.current()

        self.assertEqual(
            lifecycle.state,
            STATE_FAILED,
        )
        self.assertEqual(
            lifecycle.worker_request_id,
            WORKER_REQUEST_ID,
        )
        self.assertEqual(
            lifecycle.worker_result_id,
            WORKER_RESULT_ID,
        )
        self.assertIsNone(
            lifecycle.candidate_id
        )
        self.assertIsNone(
            lifecycle.integration_result_id
        )

        self.assertEqual(
            calls.job_call.call_count,
            1,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            1,
        )
        self.assertEqual(
            calls.result_call.call_count,
            1,
        )

    def test_requested_journal_failure_prevents_worker(
        self,
    ):
        with self.patched_execution() as calls, patch.object(
            integration,
            "create_lab_coding_job_lifecycle_journal",
            side_effect=RuntimeError(
                "journal create failed"
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "journal create failed",
            ):
                self.call()

        self.assertEqual(
            calls.job_call.call_count,
            0,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            0,
        )

    def test_running_journal_failure_prevents_worker(
        self,
    ):
        with self.patched_execution() as calls, patch.object(
            integration,
            "append_lab_coding_job_lifecycle_snapshot",
            side_effect=RuntimeError(
                "running append failed"
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "running append failed",
            ):
                self.call()

        lifecycle = self.current()

        self.assertEqual(
            lifecycle.state,
            STATE_REQUESTED,
        )
        self.assertEqual(
            calls.job_call.call_count,
            0,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            0,
        )

    def test_terminal_journal_failure_never_returns_success_or_retries(
        self,
    ):
        original_append = (
            integration
            .append_lab_coding_job_lifecycle_snapshot
        )
        append_count = 0

        def append_with_terminal_failure(
            *args,
            **kwargs,
        ):
            nonlocal append_count
            append_count += 1

            if append_count == 2:
                raise RuntimeError(
                    "terminal append failed"
                )

            return original_append(
                *args,
                **kwargs,
            )

        with self.patched_execution() as calls, patch.object(
            integration,
            "append_lab_coding_job_lifecycle_snapshot",
            side_effect=append_with_terminal_failure,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "terminal append failed",
            ):
                self.call()

        lifecycle = self.current()

        self.assertEqual(
            lifecycle.state,
            STATE_RUNNING,
        )
        self.assertEqual(
            calls.job_call.call_count,
            1,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            1,
        )
        self.assertEqual(
            calls.result_call.call_count,
            1,
        )
        self.assertEqual(
            append_count,
            2,
        )

    def test_duplicate_request_and_run_fails_before_second_worker(
        self,
    ):
        with self.patched_execution() as calls:
            self.call()

            with self.assertRaises(
                Exception
            ):
                self.call()

        self.assertEqual(
            calls.job_call.call_count,
            1,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            1,
        )

    def test_invalid_run_id_fails_before_worker(
        self,
    ):
        with self.patched_execution() as calls:
            with self.assertRaises(
                Exception
            ):
                self.call(
                    run_id="not-a-valid-run-id"
                )

        self.assertEqual(
            calls.job_call.call_count,
            0,
        )
        self.assertEqual(
            calls.finalizer_call.call_count,
            0,
        )

    def test_external_v1_request_contract_has_no_lifecycle_authority_fields(
        self,
    ):
        self.assertEqual(
            tuple(
                field.name
                for field in fields(
                    LabCodingIntegrationRequest
                )
            ),
            (
                "component",
                "schema_version",
                "request_id",
                "repository_path",
                "commit_oid",
                "expected_branch",
                "relative_paths",
                "goal",
                "change_policy",
                "max_runtime_seconds",
            ),
        )

    def test_production_orchestration_imports_no_promotion_or_git_authority(
        self,
    ):
        root = (
            Path(__file__).parents[1]
            / "src"
            / "hands_free_auto_lab"
        )

        forbidden = (
            "promotion",
            "approval",
            "executor",
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "paramiko",
            "asyncssh",
        )

        for filename in (
            "lab_coding_integration.py",
            "lab_coding_workflow.py",
        ):
            with self.subTest(
                filename=filename
            ):
                tree = ast.parse(
                    (
                        root
                        / filename
                    ).read_text(
                        encoding="utf-8"
                    )
                )

                imports = []

                for node in ast.walk(
                    tree
                ):
                    if isinstance(
                        node,
                        ast.Import,
                    ):
                        imports.extend(
                            alias.name
                            for alias in node.names
                        )

                    elif isinstance(
                        node,
                        ast.ImportFrom,
                    ):
                        imports.append(
                            node.module
                            or ""
                        )

                self.assertFalse(
                    [
                        name
                        for name in imports
                        if any(
                            token in name.lower()
                            for token in forbidden
                        )
                    ]
                )


    def test_real_all_added_job_reaches_completed_lifecycle(
        self,
    ):
        import subprocess

        from hands_free_auto_lab.lab_coding_candidate_store import (
            initialize_lab_coding_candidate_store,
            load_lab_coding_candidate,
        )
        from hands_free_auto_lab.lab_coding_integration import (
            LabCodingIntegrationDeployment,
        )
        from hands_free_auto_lab.lab_coding_integration_contract import (
            build_lab_coding_integration_request,
        )
        from hands_free_auto_lab.lab_worker import (
            STATUS_COMPLETED,
            build_lab_worker_result,
        )
        from hands_free_auto_lab.lab_worker_change_policy import (
            LabWorkerChangeTarget,
            OPERATION_ADD,
            build_lab_worker_change_policy,
        )

        target_name = "AutoLabVoiceProof.txt"
        target_content = "great life voice proof\n"

        repository = (
            self.base
            / "added-path-source-repository"
        )
        repository.mkdir(
            mode=0o700
        )

        git_environment = {
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

        def git(*arguments):
            completed = subprocess.run(
                (
                    "/usr/bin/git",
                    "-C",
                    str(repository),
                    *arguments,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=git_environment,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode(
                    "utf-8",
                    errors="replace",
                ),
            )

            return completed.stdout.decode(
                "utf-8",
                errors="strict",
            ).strip()

        git(
            "init",
            "-q",
            "-b",
            "main",
        )

        baseline = (
            repository
            / "baseline.txt"
        )
        baseline.write_text(
            "committed baseline\n",
            encoding="utf-8",
        )
        os.chmod(
            baseline,
            0o600,
        )

        git(
            "add",
            "--",
            "baseline.txt",
        )

        git(
            "-c",
            "user.name=Lifecycle Repair Test",
            "-c",
            "user.email=lifecycle-repair@example.invalid",
            "commit",
            "-q",
            "-m",
            "Committed baseline",
        )

        head = git(
            "rev-parse",
            "HEAD",
        )

        self.assertEqual(
            len(head),
            40,
        )

        source_status_before = git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )

        self.assertEqual(
            source_status_before,
            "",
        )

        self.assertFalse(
            (
                repository
                / target_name
            ).exists()
        )

        workspace_parent = (
            self.base
            / "added-path-workspaces"
        )
        workspace_parent.mkdir(
            mode=0o700
        )

        candidate_store = (
            self.base
            / "added-path-candidate-store"
        )
        candidate_store.mkdir(
            mode=0o700
        )

        initialize_lab_coding_candidate_store(
            str(candidate_store)
        )

        class SyntheticAddedWorker:
            def __init__(self):
                self.requests = []
                self.observed_target_absent = None

            def run(self, request):
                self.requests.append(
                    request
                )

                target = (
                    Path(request.workspace.path)
                    / target_name
                )

                self.observed_target_absent = (
                    not target.exists()
                )

                target.write_text(
                    target_content,
                    encoding="utf-8",
                )
                os.chmod(
                    target,
                    0o600,
                )

                return build_lab_worker_result(
                    request=request,
                    status=STATUS_COMPLETED,
                    summary=(
                        "Synthetic all-added lifecycle "
                        "worker completed."
                    ),
                    reported_changed_paths=(
                        target_name,
                    ),
                    log=(
                        "synthetic all-added lifecycle "
                        "worker\n"
                    ),
                )

        worker = SyntheticAddedWorker()

        policy = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path=target_name,
                    max_final_bytes=len(
                        target_content.encode(
                            "utf-8"
                        )
                    ),
                    final_mode=0o600,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                ),
            )
        )

        request = build_lab_coding_integration_request(
            repository_path=str(
                repository
            ),
            commit_oid=head,
            expected_branch="main",
            relative_paths=(
                target_name,
            ),
            goal=(
                "Create AutoLabVoiceProof.txt "
                "as one bounded added file."
            ),
            change_policy=policy,
            max_runtime_seconds=30,
        )

        deployment = LabCodingIntegrationDeployment(
            worker=worker,
            workspace_parent=str(
                workspace_parent
            ),
            candidate_store_root=str(
                candidate_store
            ),
            session_id="8" * 64,
            max_seed_files=8,
            max_seed_bytes=64 * 1024,
            max_runtime_seconds=20,
            max_log_bytes=64 * 1024,
        )

        run_id = "9" * 64

        result = (
            run_lab_coding_integration_request_with_lifecycle(
                request=request,
                deployment=deployment,
                run_id=run_id,
                lifecycle_journal_root=(
                    self.journal_root
                ),
            )
        )

        self.assertEqual(
            len(worker.requests),
            1,
        )

        self.assertTrue(
            worker.observed_target_absent
        )

        self.assertEqual(
            result.request_id,
            request.request_id,
        )

        candidate = load_lab_coding_candidate(
            str(candidate_store),
            result.candidate_id,
        )

        self.assertEqual(
            tuple(
                item.path
                for item in candidate.files
            ),
            (
                target_name,
            ),
        )

        self.assertEqual(
            candidate.files[0].operation,
            "ADDED",
        )

        self.assertEqual(
            candidate.files[0].content,
            target_content,
        )

        requested = build_lab_coding_job_lifecycle(
            request_id=request.request_id,
            run_id=run_id,
        )

        lifecycle = (
            load_lab_coding_job_lifecycle_journal(
                self.journal_root,
                job_id=requested.job_id,
            )
        )

        self.assertEqual(
            lifecycle.state,
            STATE_COMPLETED,
        )

        self.assertEqual(
            lifecycle.request_id,
            request.request_id,
        )

        self.assertEqual(
            lifecycle.session_id,
            deployment.session_id,
        )

        self.assertIsNotNone(
            lifecycle.worker_request_id
        )

        self.assertIsNotNone(
            lifecycle.worker_result_id
        )

        self.assertEqual(
            lifecycle.candidate_id,
            result.candidate_id,
        )

        self.assertEqual(
            lifecycle.integration_result_id,
            result.result_id,
        )

        self.assertEqual(
            git(
                "rev-parse",
                "HEAD",
            ),
            head,
        )

        self.assertEqual(
            git(
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            source_status_before,
        )

        self.assertFalse(
            (
                repository
                / target_name
            ).exists()
        )



if __name__ == "__main__":
    unittest.main()
