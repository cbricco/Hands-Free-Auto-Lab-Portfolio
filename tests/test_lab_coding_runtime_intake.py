#!/usr/bin/env python3

import ast
import inspect
import os
import tempfile
import unittest
from dataclasses import (
    FrozenInstanceError,
    fields,
    replace,
)
from pathlib import Path
from unittest.mock import (
    call,
    patch,
)

import hands_free_auto_lab.lab_coding_runtime_intake as intake
from hands_free_auto_lab.lab_codex_worker import (
    CodexWorker,
    CodexWorkerError,
    build_codex_worker_config,
)
from hands_free_auto_lab.lab_coding_integration import (
    LabCodingIntegrationDeployment,
)
from hands_free_auto_lab.lab_coding_integration_contract import (
    LabCodingIntegrationContractError,
)


SESSION_ID = "a" * 64
RUN_ID = "b" * 64


class LabCodingRuntimeIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="auto-lab-runtime-intake-tests-"
        )
        self.addCleanup(
            self.temporary.cleanup
        )

        self.root = Path(
            self.temporary.name
        )

        self.release = (
            self.root
            / "release"
        )
        self.release.mkdir(
            mode=0o755
        )
        os.chmod(
            self.release,
            0o755,
        )

        self.executable = (
            self.release
            / "codex"
        )
        self.executable.write_text(
            "test placeholder; never executed\n",
            encoding="utf-8",
        )
        os.chmod(
            self.executable,
            0o755,
        )

        self.codex_home = (
            self.root
            / "codex-home"
        )
        self.codex_home.mkdir(
            mode=0o700
        )
        os.chmod(
            self.codex_home,
            0o700,
        )

        self.runtime_temp = (
            self.root
            / "runtime"
        )
        self.runtime_temp.mkdir(
            mode=0o700
        )
        os.chmod(
            self.runtime_temp,
            0o700,
        )

        self.workspace_parent = (
            self.root
            / "workspaces"
        )
        self.workspace_parent.mkdir(
            mode=0o700
        )
        os.chmod(
            self.workspace_parent,
            0o700,
        )

        self.candidate_store_root = (
            self.root
            / "candidate-store"
        )
        self.candidate_store_root.mkdir(
            mode=0o700
        )
        os.chmod(
            self.candidate_store_root,
            0o700,
        )

        self.lifecycle_journal_root = (
            self.root
            / "lifecycle-journal"
        )
        self.lifecycle_journal_root.mkdir(
            mode=0o700
        )
        os.chmod(
            self.lifecycle_journal_root,
            0o700,
        )

        self.worker_config = (
            build_codex_worker_config(
                codex_executable=str(
                    self.executable
                ),
                codex_home=str(
                    self.codex_home
                ),
                codex_release_root=str(
                    self.release
                ),
                runtime_temp=str(
                    self.runtime_temp
                ),
                max_runtime_seconds=300,
                max_output_bytes=65536,
            )
        )

        self.config = (
            intake.LabCodingRuntimeConfig(
                codex_worker_config=(
                    self.worker_config
                ),
                workspace_parent=str(
                    self.workspace_parent
                ),
                candidate_store_root=str(
                    self.candidate_store_root
                ),
                lifecycle_journal_root=str(
                    self.lifecycle_journal_root
                ),
                max_seed_files=64,
                max_seed_bytes=4 * 1024 * 1024,
            )
        )

    def runtime(self):
        return intake.LabCodingRuntime(
            self.config
        )

    def test_runtime_config_is_immutable_local_and_nonoverlapping(
        self,
    ):
        trusted = (
            intake.validate_lab_coding_runtime_config(
                self.config
            )
        )

        self.assertIs(
            trusted,
            self.config,
        )

        self.assertEqual(
            tuple(
                field.name
                for field in fields(
                    intake.LabCodingRuntimeConfig
                )
            ),
            (
                "codex_worker_config",
                "workspace_parent",
                "candidate_store_root",
                "lifecycle_journal_root",
                "max_seed_files",
                "max_seed_bytes",
            ),
        )

        with self.assertRaises(
            FrozenInstanceError
        ):
            self.config.max_seed_files = 1

        with self.assertRaisesRegex(
            intake.LabCodingRuntimeIntakeError,
            "must not overlap",
        ):
            intake.validate_lab_coding_runtime_config(
                replace(
                    self.config,
                    candidate_store_root=(
                        self.config.workspace_parent
                    ),
                )
            )

        with self.assertRaisesRegex(
            intake.LabCodingRuntimeIntakeError,
            "positive exact integer",
        ):
            intake.validate_lab_coding_runtime_config(
                replace(
                    self.config,
                    max_seed_files=True,
                )
            )

    def test_request_method_exposes_only_canonical_record_bytes(
        self,
    ):
        parameters = tuple(
            inspect.signature(
                intake.LabCodingRuntime.run_request_bytes
            ).parameters
        )

        self.assertEqual(
            parameters,
            (
                "self",
                "record_bytes",
            ),
        )

        constructor_parameters = tuple(
            inspect.signature(
                intake.LabCodingRuntime
            ).parameters
        )

        self.assertEqual(
            constructor_parameters,
            (
                "config",
            ),
        )

    def test_runtime_allows_release_strictly_inside_codex_home(
        self,
    ):
        nested_release = (
            self.codex_home
            / "packages"
            / "standalone"
            / "release"
        )
        nested_release.mkdir(
            parents=True,
            mode=0o755,
        )
        os.chmod(
            nested_release,
            0o755,
        )

        nested_executable = (
            nested_release
            / "codex"
        )
        nested_executable.write_text(
            "test placeholder; never executed\n",
            encoding="utf-8",
        )
        os.chmod(
            nested_executable,
            0o755,
        )

        worker_config = build_codex_worker_config(
            codex_executable=str(
                nested_executable
            ),
            codex_home=str(
                self.codex_home
            ),
            codex_release_root=str(
                nested_release
            ),
            runtime_temp=str(
                self.runtime_temp
            ),
            max_runtime_seconds=300,
            max_output_bytes=65536,
        )

        candidate = replace(
            self.config,
            codex_worker_config=worker_config,
        )

        self.assertIs(
            intake.validate_lab_coding_runtime_config(
                candidate
            ),
            candidate,
        )

    def test_runtime_rejects_codex_home_inside_release(
        self,
    ):
        nested_home = (
            self.release
            / "private-home"
        )
        nested_home.mkdir(
            mode=0o700,
        )
        os.chmod(
            nested_home,
            0o700,
        )

        worker_config = build_codex_worker_config(
            codex_executable=str(
                self.executable
            ),
            codex_home=str(
                nested_home
            ),
            codex_release_root=str(
                self.release
            ),
            runtime_temp=str(
                self.runtime_temp
            ),
            max_runtime_seconds=300,
            max_output_bytes=65536,
        )

        with self.assertRaisesRegex(
            intake.LabCodingRuntimeIntakeError,
            "codex_home must not be inside codex_release_root",
        ):
            intake.validate_lab_coding_runtime_config(
                replace(
                    self.config,
                    codex_worker_config=worker_config,
                )
            )

    def test_runtime_rejects_mutable_overlap_with_codex_home(
        self,
    ):
        candidate_under_home = (
            self.codex_home
            / "candidate-store"
        )
        candidate_under_home.mkdir(
            mode=0o700,
        )
        os.chmod(
            candidate_under_home,
            0o700,
        )

        with self.assertRaisesRegex(
            intake.LabCodingRuntimeIntakeError,
            "must not overlap",
        ):
            intake.validate_lab_coding_runtime_config(
                replace(
                    self.config,
                    candidate_store_root=str(
                        candidate_under_home
                    ),
                )
            )

        candidate_parent = (
            self.root
            / "candidate-home-parent"
        )
        candidate_parent.mkdir(
            mode=0o700,
        )
        os.chmod(
            candidate_parent,
            0o700,
        )

        nested_home = (
            candidate_parent
            / "codex-home"
        )
        nested_home.mkdir(
            mode=0o700,
        )
        os.chmod(
            nested_home,
            0o700,
        )

        worker_config = build_codex_worker_config(
            codex_executable=str(
                self.executable
            ),
            codex_home=str(
                nested_home
            ),
            codex_release_root=str(
                self.release
            ),
            runtime_temp=str(
                self.runtime_temp
            ),
            max_runtime_seconds=300,
            max_output_bytes=65536,
        )

        with self.assertRaisesRegex(
            intake.LabCodingRuntimeIntakeError,
            "must not overlap",
        ):
            intake.validate_lab_coding_runtime_config(
                replace(
                    self.config,
                    codex_worker_config=worker_config,
                    candidate_store_root=str(
                        candidate_parent
                    ),
                )
            )

    def test_runtime_rejects_mutable_overlap_with_codex_release(
        self,
    ):
        candidate_under_release = (
            self.release
            / "candidate-store"
        )
        candidate_under_release.mkdir(
            mode=0o700,
        )
        os.chmod(
            candidate_under_release,
            0o700,
        )

        with self.assertRaisesRegex(
            intake.LabCodingRuntimeIntakeError,
            "must not overlap",
        ):
            intake.validate_lab_coding_runtime_config(
                replace(
                    self.config,
                    candidate_store_root=str(
                        candidate_under_release
                    ),
                )
            )

        candidate_parent = (
            self.root
            / "candidate-release-parent"
        )
        candidate_parent.mkdir(
            mode=0o700,
        )
        os.chmod(
            candidate_parent,
            0o700,
        )

        nested_release = (
            candidate_parent
            / "release"
        )
        nested_release.mkdir(
            mode=0o755,
        )
        os.chmod(
            nested_release,
            0o755,
        )

        nested_executable = (
            nested_release
            / "codex"
        )
        nested_executable.write_text(
            "test placeholder; never executed\n",
            encoding="utf-8",
        )
        os.chmod(
            nested_executable,
            0o755,
        )

        worker_config = build_codex_worker_config(
            codex_executable=str(
                nested_executable
            ),
            codex_home=str(
                self.codex_home
            ),
            codex_release_root=str(
                nested_release
            ),
            runtime_temp=str(
                self.runtime_temp
            ),
            max_runtime_seconds=300,
            max_output_bytes=65536,
        )

        with self.assertRaisesRegex(
            intake.LabCodingRuntimeIntakeError,
            "must not overlap",
        ):
            intake.validate_lab_coding_runtime_config(
                replace(
                    self.config,
                    codex_worker_config=worker_config,
                    candidate_store_root=str(
                        candidate_parent
                    ),
                )
            )

    def test_valid_request_uses_only_local_runtime_and_returns_result_bytes(
        self,
    ):
        runtime = self.runtime()

        request_bytes = (
            b'{"canonical":"request"}\n'
        )
        result_bytes = (
            b'{"canonical":"result"}\n'
        )

        request = object()
        result = object()

        events = []

        def decode(value):
            events.append(
                (
                    "decode",
                    value,
                )
            )
            return request

        def initialize_candidate(root):
            events.append(
                (
                    "candidate-store",
                    root,
                )
            )

        def initialize_journal(root):
            events.append(
                (
                    "lifecycle-journal",
                    root,
                )
            )

        def execute(**kwargs):
            events.append(
                (
                    "execute",
                    kwargs,
                )
            )
            return result

        def encode(value):
            events.append(
                (
                    "encode",
                    value,
                )
            )
            return result_bytes

        with (
            patch.object(
                intake,
                "lab_coding_integration_request_from_bytes",
                side_effect=decode,
            ) as decoder,
            patch.object(
                intake,
                "initialize_lab_coding_candidate_store",
                side_effect=initialize_candidate,
            ) as candidate_initializer,
            patch.object(
                intake,
                "initialize_lab_coding_job_lifecycle_journal_root",
                side_effect=initialize_journal,
            ) as journal_initializer,
            patch.object(
                intake.secrets,
                "token_hex",
                side_effect=(
                    SESSION_ID,
                    RUN_ID,
                ),
            ) as token_hex,
            patch.object(
                intake,
                "run_lab_coding_integration_request_with_lifecycle",
                side_effect=execute,
            ) as runner,
            patch.object(
                intake,
                "lab_coding_integration_result_to_bytes",
                side_effect=encode,
            ) as encoder,
        ):
            returned = runtime.run_request_bytes(
                request_bytes
            )

        self.assertEqual(
            returned,
            result_bytes,
        )

        decoder.assert_called_once_with(
            request_bytes
        )

        candidate_initializer.assert_called_once_with(
            str(
                self.candidate_store_root
            )
        )

        journal_initializer.assert_called_once_with(
            str(
                self.lifecycle_journal_root
            )
        )

        self.assertEqual(
            token_hex.call_args_list,
            [
                call(32),
                call(32),
            ],
        )

        runner.assert_called_once()
        encoder.assert_called_once_with(
            result
        )

        kwargs = (
            runner.call_args.kwargs
        )

        self.assertIs(
            kwargs["request"],
            request,
        )

        self.assertEqual(
            kwargs["run_id"],
            RUN_ID,
        )

        self.assertEqual(
            kwargs["lifecycle_journal_root"],
            str(
                self.lifecycle_journal_root
            ),
        )

        deployment = (
            kwargs["deployment"]
        )

        self.assertIsInstance(
            deployment,
            LabCodingIntegrationDeployment,
        )

        self.assertIsInstance(
            deployment.worker,
            CodexWorker,
        )

        self.assertEqual(
            deployment.workspace_parent,
            str(
                self.workspace_parent
            ),
        )

        self.assertEqual(
            deployment.candidate_store_root,
            str(
                self.candidate_store_root
            ),
        )

        self.assertEqual(
            deployment.session_id,
            SESSION_ID,
        )

        self.assertEqual(
            deployment.max_seed_files,
            64,
        )

        self.assertEqual(
            deployment.max_seed_bytes,
            4 * 1024 * 1024,
        )

        self.assertEqual(
            deployment.max_runtime_seconds,
            300,
        )

        self.assertEqual(
            deployment.max_log_bytes,
            65536,
        )

        self.assertEqual(
            [
                event[0]
                for event in events
            ],
            [
                "decode",
                "candidate-store",
                "lifecycle-journal",
                "execute",
                "encode",
            ],
        )

    def test_malformed_wire_data_fails_before_local_state_or_ids(
        self,
    ):
        runtime = self.runtime()

        with (
            patch.object(
                intake,
                "initialize_lab_coding_candidate_store",
            ) as candidate_initializer,
            patch.object(
                intake,
                "initialize_lab_coding_job_lifecycle_journal_root",
            ) as journal_initializer,
            patch.object(
                intake.secrets,
                "token_hex",
            ) as token_hex,
            patch.object(
                intake,
                "run_lab_coding_integration_request_with_lifecycle",
            ) as runner,
        ):
            with self.assertRaises(
                LabCodingIntegrationContractError
            ):
                runtime.run_request_bytes(
                    b"{}\n"
                )

        candidate_initializer.assert_not_called()
        journal_initializer.assert_not_called()
        token_hex.assert_not_called()
        runner.assert_not_called()

    def test_unsafe_private_runtime_root_fails_before_store_mutation_or_ids(
        self,
    ):
        runtime = self.runtime()
        request = object()

        roots = (
            (
                "workspace_parent",
                self.workspace_parent,
            ),
            (
                "candidate_store_root",
                self.candidate_store_root,
            ),
            (
                "lifecycle_journal_root",
                self.lifecycle_journal_root,
            ),
        )

        for name, path in roots:
            with self.subTest(
                name=name
            ):
                os.chmod(
                    path,
                    0o755,
                )

                try:
                    with (
                        patch.object(
                            intake,
                            "lab_coding_integration_request_from_bytes",
                            return_value=request,
                        ),
                        patch.object(
                            intake,
                            "initialize_lab_coding_candidate_store",
                        ) as candidate_initializer,
                        patch.object(
                            intake,
                            "initialize_lab_coding_job_lifecycle_journal_root",
                        ) as journal_initializer,
                        patch.object(
                            intake.secrets,
                            "token_hex",
                        ) as token_hex,
                        patch.object(
                            intake,
                            "run_lab_coding_integration_request_with_lifecycle",
                        ) as runner,
                    ):
                        with self.assertRaisesRegex(
                            intake.LabCodingRuntimeIntakeError,
                            name,
                        ):
                            runtime.run_request_bytes(
                                b"request\n"
                            )

                    candidate_initializer.assert_not_called()
                    journal_initializer.assert_not_called()
                    token_hex.assert_not_called()
                    runner.assert_not_called()

                finally:
                    os.chmod(
                        path,
                        0o700,
                    )

    def test_unsafe_codex_physical_context_fails_before_store_mutation_or_ids(
        self,
    ):
        runtime = self.runtime()
        request = object()

        os.chmod(
            self.runtime_temp,
            0o755,
        )

        try:
            with (
                patch.object(
                    intake,
                    "lab_coding_integration_request_from_bytes",
                    return_value=request,
                ),
                patch.object(
                    intake,
                    "initialize_lab_coding_candidate_store",
                ) as candidate_initializer,
                patch.object(
                    intake,
                    "initialize_lab_coding_job_lifecycle_journal_root",
                ) as journal_initializer,
                patch.object(
                    intake.secrets,
                    "token_hex",
                ) as token_hex,
                patch.object(
                    intake,
                    "run_lab_coding_integration_request_with_lifecycle",
                ) as runner,
            ):
                with self.assertRaisesRegex(
                    CodexWorkerError,
                    "unsafe permissions",
                ):
                    runtime.run_request_bytes(
                        b"request\n"
                    )

            candidate_initializer.assert_not_called()
            journal_initializer.assert_not_called()
            token_hex.assert_not_called()
            runner.assert_not_called()

        finally:
            os.chmod(
                self.runtime_temp,
                0o700,
            )

    def test_store_failure_prevents_identity_generation_and_execution(
        self,
    ):
        runtime = self.runtime()
        request = object()

        with (
            patch.object(
                intake,
                "lab_coding_integration_request_from_bytes",
                return_value=request,
            ),
            patch.object(
                intake,
                "initialize_lab_coding_candidate_store",
                side_effect=RuntimeError(
                    "candidate store unsafe"
                ),
            ) as candidate_initializer,
            patch.object(
                intake,
                "initialize_lab_coding_job_lifecycle_journal_root",
            ) as journal_initializer,
            patch.object(
                intake.secrets,
                "token_hex",
            ) as token_hex,
            patch.object(
                intake,
                "run_lab_coding_integration_request_with_lifecycle",
            ) as runner,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "candidate store unsafe",
            ):
                runtime.run_request_bytes(
                    b"request\n"
                )

        candidate_initializer.assert_called_once()
        journal_initializer.assert_not_called()
        token_hex.assert_not_called()
        runner.assert_not_called()

    def test_module_excludes_external_authority_and_direct_host_execution(
        self,
    ):
        source = Path(
            intake.__file__
        ).read_text(
            encoding="utf-8"
        )

        tree = ast.parse(
            source
        )

        imported = set()

        for node in ast.walk(
            tree
        ):
            if (
                isinstance(
                    node,
                    ast.ImportFrom,
                )
                and node.module
            ):
                imported.add(
                    node.module
                )

            elif isinstance(
                node,
                ast.Import,
            ):
                imported.update(
                    alias.name
                    for alias in node.names
                )

        for forbidden in (
            "promotion",
            "approval",
            "controller",
            "audit",
            "subprocess",
            "socket",
            "requests",
            "urllib",
        ):
            self.assertFalse(
                any(
                    forbidden
                    in name.lower()
                    for name in imported
                ),
                forbidden,
            )

        for forbidden_text in (
            "/home/",
            "shell=True",
            "os.system",
            "subprocess.",
            "os.mkdir(",
            ".mkdir(",
            ".unlink(",
            "rmtree(",
            "git add",
            "git commit",
            "git push",
        ):
            self.assertNotIn(
                forbidden_text,
                source,
            )


if __name__ == "__main__":
    unittest.main()
