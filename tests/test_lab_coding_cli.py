from __future__ import annotations

import ast
import io
import sys
import unittest
from pathlib import Path


SRC_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
)
PACKAGE_DIR = (
    SRC_ROOT
    / "hands_free_auto_lab"
)

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hands_free_auto_lab import lab_coding_cli as cli


class LabCodingCliTests(unittest.TestCase):
    def _environment(self) -> dict[str, str]:
        root = "/tmp/hf-auto-lab-cli-test"

        return {
            cli.ENV_CODEX_EXECUTABLE: (
                f"{root}/release/bin/codex"
            ),
            cli.ENV_CODEX_HOME: (
                f"{root}/codex-home"
            ),
            cli.ENV_CODEX_RELEASE_ROOT: (
                f"{root}/release"
            ),
            cli.ENV_RUNTIME_TEMP: (
                f"{root}/runtime-temp"
            ),
            cli.ENV_WORKSPACE_PARENT: (
                f"{root}/workspaces"
            ),
            cli.ENV_CANDIDATE_STORE_ROOT: (
                f"{root}/candidate-store"
            ),
            cli.ENV_LIFECYCLE_JOURNAL_ROOT: (
                f"{root}/lifecycle-journal"
            ),
            cli.ENV_MAX_RUNTIME_SECONDS: "300",
            cli.ENV_MAX_OUTPUT_BYTES: "65536",
            cli.ENV_MAX_SEED_FILES: "64",
            cli.ENV_MAX_SEED_BYTES: "4194304",
        }

    def test_environment_contract_is_explicit_and_proxy_only_optional(
        self,
    ) -> None:
        expected_required = {
            "HF_AUTO_LAB_CODEX_EXECUTABLE",
            "HF_AUTO_LAB_CODEX_HOME",
            "HF_AUTO_LAB_CODEX_RELEASE_ROOT",
            "HF_AUTO_LAB_RUNTIME_TEMP",
            "HF_AUTO_LAB_WORKSPACE_PARENT",
            "HF_AUTO_LAB_CANDIDATE_STORE_ROOT",
            "HF_AUTO_LAB_LIFECYCLE_JOURNAL_ROOT",
            "HF_AUTO_LAB_MAX_RUNTIME_SECONDS",
            "HF_AUTO_LAB_MAX_OUTPUT_BYTES",
            "HF_AUTO_LAB_MAX_SEED_FILES",
            "HF_AUTO_LAB_MAX_SEED_BYTES",
        }

        self.assertEqual(
            cli._REQUIRED_ENVIRONMENT_NAMES,
            frozenset(expected_required),
        )
        self.assertEqual(
            cli._OPTIONAL_ENVIRONMENT_NAMES,
            frozenset(
                {
                    "HF_AUTO_LAB_TRUSTED_CLIENT_PROXY",
                }
            ),
        )

    def test_builds_validated_runtime_config_from_explicit_environment(
        self,
    ) -> None:
        environ = self._environment()

        config = (
            cli.build_lab_coding_runtime_config_from_environment(
                environ
            )
        )

        self.assertEqual(
            config.codex_worker_config.codex_executable,
            environ[cli.ENV_CODEX_EXECUTABLE],
        )
        self.assertEqual(
            config.codex_worker_config.codex_home,
            environ[cli.ENV_CODEX_HOME],
        )
        self.assertEqual(
            config.codex_worker_config.codex_release_root,
            environ[cli.ENV_CODEX_RELEASE_ROOT],
        )
        self.assertEqual(
            config.codex_worker_config.runtime_temp,
            environ[cli.ENV_RUNTIME_TEMP],
        )
        self.assertEqual(
            config.codex_worker_config.max_runtime_seconds,
            300,
        )
        self.assertEqual(
            config.codex_worker_config.max_output_bytes,
            65536,
        )
        self.assertIsNone(
            config.codex_worker_config.trusted_client_proxy
        )
        self.assertEqual(
            config.workspace_parent,
            environ[cli.ENV_WORKSPACE_PARENT],
        )
        self.assertEqual(
            config.candidate_store_root,
            environ[cli.ENV_CANDIDATE_STORE_ROOT],
        )
        self.assertEqual(
            config.lifecycle_journal_root,
            environ[cli.ENV_LIFECYCLE_JOURNAL_ROOT],
        )
        self.assertEqual(
            config.max_seed_files,
            64,
        )
        self.assertEqual(
            config.max_seed_bytes,
            4194304,
        )

    def test_optional_proxy_is_passed_through_existing_validator(
        self,
    ) -> None:
        environ = self._environment()
        environ[
            cli.ENV_TRUSTED_CLIENT_PROXY
        ] = "http://127.0.0.1:8123"

        config = (
            cli.build_lab_coding_runtime_config_from_environment(
                environ
            )
        )

        self.assertEqual(
            config.codex_worker_config.trusted_client_proxy,
            "http://127.0.0.1:8123",
        )

    def test_missing_required_configuration_fails_closed(
        self,
    ) -> None:
        for missing_name in sorted(
            cli._REQUIRED_ENVIRONMENT_NAMES
        ):
            with self.subTest(missing_name=missing_name):
                environ = self._environment()
                del environ[missing_name]

                with self.assertRaises(
                    cli.LabCodingCliError
                ):
                    (
                        cli.build_lab_coding_runtime_config_from_environment(
                            environ
                        )
                    )

    def test_noncanonical_integer_configuration_fails_closed(
        self,
    ) -> None:
        bad_values = (
            "",
            "0",
            "-1",
            "+1",
            " 300",
            "300 ",
            "0300",
            "3.0",
            "True",
            "１２",
        )

        integer_names = (
            cli.ENV_MAX_RUNTIME_SECONDS,
            cli.ENV_MAX_OUTPUT_BYTES,
            cli.ENV_MAX_SEED_FILES,
            cli.ENV_MAX_SEED_BYTES,
        )

        for name in integer_names:
            for bad_value in bad_values:
                with self.subTest(
                    name=name,
                    bad_value=bad_value,
                ):
                    environ = self._environment()
                    environ[name] = bad_value

                    with self.assertRaises(
                        Exception
                    ):
                        (
                            cli.build_lab_coding_runtime_config_from_environment(
                                environ
                            )
                        )

    def test_unknown_auto_lab_environment_variable_fails_closed(
        self,
    ) -> None:
        environ = self._environment()
        environ["HF_AUTO_LAB_AUTO_APPROVE"] = "1"

        with self.assertRaises(
            cli.LabCodingCliError
        ):
            (
                cli.build_lab_coding_runtime_config_from_environment(
                    environ
                )
            )

    def test_run_cli_forwards_request_once_and_preserves_result_bytes(
        self,
    ) -> None:
        environ = self._environment()
        request_bytes = b'{"request":"canonical"}'
        expected_result = b'{"result":"canonical"}'
        calls = []

        class FakeRuntime:
            def __init__(self, config):
                self.config = config
                calls.append(("init", config))

            def run_request_bytes(self, record_bytes):
                calls.append(
                    ("run_request_bytes", record_bytes)
                )
                return expected_result

        stdin = io.BytesIO(request_bytes)
        stdout = io.BytesIO()

        status = cli.run_cli(
            environ=environ,
            stdin=stdin,
            stdout=stdout,
            runtime_factory=FakeRuntime,
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            stdout.getvalue(),
            expected_result,
        )
        self.assertEqual(
            [name for name, _ in calls],
            [
                "init",
                "run_request_bytes",
            ],
        )
        self.assertEqual(
            calls[1][1],
            request_bytes,
        )

    def test_runtime_failure_propagates_without_retry(
        self,
    ) -> None:
        environ = self._environment()
        calls = []

        class FailingRuntime:
            def __init__(self, config):
                calls.append("init")

            def run_request_bytes(self, record_bytes):
                calls.append("run")
                raise RuntimeError("synthetic failure")

        stdout = io.BytesIO()

        with self.assertRaisesRegex(
            RuntimeError,
            "synthetic failure",
        ):
            cli.run_cli(
                environ=environ,
                stdin=io.BytesIO(b"request"),
                stdout=stdout,
                runtime_factory=FailingRuntime,
            )

        self.assertEqual(
            calls,
            [
                "init",
                "run",
            ],
        )
        self.assertEqual(
            stdout.getvalue(),
            b"",
        )

    def test_main_keeps_failure_diagnostic_off_stdout(
        self,
    ) -> None:
        environ = self._environment()

        class FailingRuntime:
            def __init__(self, config):
                pass

            def run_request_bytes(self, record_bytes):
                raise RuntimeError("synthetic failure")

        stdout = io.BytesIO()
        stderr = io.StringIO()

        status = cli.main(
            environ=environ,
            stdin=io.BytesIO(b"request"),
            stdout=stdout,
            stderr=stderr,
            runtime_factory=FailingRuntime,
        )

        self.assertEqual(status, 2)
        self.assertEqual(
            stdout.getvalue(),
            b"",
        )
        self.assertIn(
            "RuntimeError: synthetic failure",
            stderr.getvalue(),
        )

    def test_main_success_writes_only_exact_result_bytes(
        self,
    ) -> None:
        environ = self._environment()
        expected = b'{"result_id":"exact"}'

        class FakeRuntime:
            def __init__(self, config):
                pass

            def run_request_bytes(self, record_bytes):
                return expected

        stdout = io.BytesIO()
        stderr = io.StringIO()

        status = cli.main(
            environ=environ,
            stdin=io.BytesIO(b"request"),
            stdout=stdout,
            stderr=stderr,
            runtime_factory=FakeRuntime,
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            stdout.getvalue(),
            expected,
        )
        self.assertEqual(
            stderr.getvalue(),
            "",
        )

    def test_source_has_no_added_execution_or_repository_authority(
        self,
    ) -> None:
        source_path = (
            PACKAGE_DIR
            / "lab_coding_cli.py"
        )
        source = source_path.read_text(
            encoding="utf-8"
        )
        tree = ast.parse(
            source,
            filename=str(source_path),
        )

        forbidden_import_roots = {
            "subprocess",
            "socket",
            "requests",
            "urllib",
        }

        imported_roots = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(
                        alias.name.split(".", 1)[0]
                    )

            if isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_roots.add(
                        node.module.split(".", 1)[0]
                    )

        self.assertTrue(
            forbidden_import_roots.isdisjoint(
                imported_roots
            )
        )

        lowered = source.lower()

        for forbidden_text in (
            "/home/testuser",
            "git add",
            "git commit",
            "git push",
            "os.system",
            "shell=true",
            "auto_approve",
            "allow_write",
        ):
            with self.subTest(
                forbidden_text=forbidden_text
            ):
                self.assertNotIn(
                    forbidden_text,
                    lowered,
                )

        for environment_name in (
            cli._REQUIRED_ENVIRONMENT_NAMES
            | cli._OPTIONAL_ENVIRONMENT_NAMES
        ):
            lowered_name = environment_name.lower()

            for forbidden_fragment in (
                "approve",
                "approval",
                "authorize",
                "authorization",
                "promote",
                "promotion",
                "commit",
                "push",
            ):
                with self.subTest(
                    environment_name=environment_name,
                    forbidden_fragment=forbidden_fragment,
                ):
                    self.assertNotIn(
                        forbidden_fragment,
                        lowered_name,
                    )


if __name__ == "__main__":
    unittest.main()
