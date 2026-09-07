from __future__ import annotations

import ast
from pathlib import Path
import shutil
import socket
import tempfile
import unittest

import hands_free_auto_lab.lab_aider_worker as aider_module

from hands_free_auto_lab.lab_aider_worker import (
    AIDER_IMAGE,
    AIDER_MODEL,
    AIDER_WORKER_NAME,
    AiderCommandResult,
    AiderWorker,
    AiderWorkerError,
    build_aider_docker_command,
    build_aider_worker_config,
)
from hands_free_auto_lab.lab_worker import (
    CAPABILITY_WORKSPACE_READ,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    build_lab_worker_request,
)
from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)


SESSION_ID = "7" * 64


class FakeDockerRunner:
    def __init__(
        self,
        *,
        run_returncode: int = 0,
        run_log: str = "Aider completed\n",
        run_timed_out: bool = False,
    ):
        self.run_returncode = run_returncode
        self.run_log = run_log
        self.run_timed_out = run_timed_out

        self.container_exists = False
        self.container_running = False
        self.request_id = None

        self.calls = []

    def __call__(
        self,
        argv,
        timeout_seconds,
        max_log_bytes,
    ):
        self.calls.append(
            (
                argv,
                timeout_seconds,
                max_log_bytes,
            )
        )

        if (
            len(
                argv
            )
            >= 3
            and argv[
                1:3
            ]
            == (
                "container",
                "inspect",
            )
        ):
            if not self.container_exists:
                return AiderCommandResult(
                    returncode=1,
                    log=(
                        "Error: No such object: "
                        + argv[
                            -1
                        ]
                    ),
                    timed_out=False,
                )

            return AiderCommandResult(
                returncode=0,
                log=(
                    f"{self.request_id}|"
                    f"{str(self.container_running).lower()}\n"
                ),
                timed_out=False,
            )

        if (
            len(
                argv
            )
            >= 2
            and argv[
                1
            ]
            == "run"
        ):
            label_index = argv.index(
                "--label"
            )

            label = argv[
                label_index
                + 1
            ]

            self.request_id = label.split(
                "=",
                1,
            )[
                1
            ]

            self.container_exists = True
            self.container_running = (
                self.run_timed_out
            )

            return AiderCommandResult(
                returncode=self.run_returncode,
                log=self.run_log,
                timed_out=self.run_timed_out,
            )

        if (
            len(
                argv
            )
            >= 2
            and argv[
                1
            ]
            == "rm"
        ):
            self.container_exists = False
            self.container_running = False

            return AiderCommandResult(
                returncode=0,
                log="",
                timed_out=False,
            )

        raise AssertionError(
            f"unexpected fake Docker command: {argv!r}"
        )


class AiderWorkerTests(
    unittest.TestCase
):
    def setUp(
        self,
    ):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-aider-tests-"
            )
        )

        os_mode = 0o700

        self.parent = (
            self.root
            / "workspaces"
        )

        self.parent.mkdir(
            mode=os_mode
        )

        self.workspace = create_lab_workspace(
            str(
                self.parent
            ),
            session_id=SESSION_ID,
        )

        workspace_root = Path(
            self.workspace.path
        )

        (
            workspace_root
            / "src"
        ).mkdir()

        (
            workspace_root
            / "tests"
        ).mkdir()

        (
            workspace_root
            / "src"
            / "example.py"
        ).write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )

        (
            workspace_root
            / "tests"
            / "test_example.py"
        ).write_text(
            "assert True\n",
            encoding="utf-8",
        )

        self.relay = (
            self.root
            / "relay"
        )

        self.relay.mkdir(
            mode=0o700
        )

        self.socket_path = (
            self.relay
            / "ollama.sock"
        )

        self.socket = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )

        self.socket.bind(
            str(
                self.socket_path
            )
        )

        self.socket_path.chmod(
            0o600
        )

        self.request = build_lab_worker_request(
            worker=AIDER_WORKER_NAME,
            goal="Implement the requested example change.",
            workspace=self.workspace,
            max_runtime_seconds=60,
            max_log_bytes=32 * 1024,
        )

        self.config = build_aider_worker_config(
            relay_directory=str(
                self.relay
            ),
            editable_paths=(
                "src/example.py",
            ),
            read_only_paths=(
                "tests/test_example.py",
            ),
            test_command=(
                "PYTHONDONTWRITEBYTECODE=1 "
                "PYTHONPATH=/workspace/src "
                "python3 /workspace/tests/test_example.py"
            ),
        )

    def tearDown(
        self,
    ):
        self.socket.close()

        shutil.rmtree(
            self.root
        )

    def test_command_contains_exact_containment_boundary(
        self,
    ):
        command = build_aider_docker_command(
            request=self.request,
            config=self.config,
        )

        self.assertEqual(
            command[
                0
            ],
            "/usr/bin/docker",
        )

        self.assertEqual(
            command[
                1
            ],
            "run",
        )

        self.assertIn(
            AIDER_IMAGE,
            command,
        )

        network_index = command.index(
            "--network"
        )

        self.assertEqual(
            command[
                network_index
                + 1
            ],
            "none",
        )

        self.assertIn(
            "--read-only",
            command,
        )

        cap_index = command.index(
            "--cap-drop"
        )

        self.assertEqual(
            command[
                cap_index
                + 1
            ],
            "ALL",
        )

        security_index = command.index(
            "--security-opt"
        )

        self.assertEqual(
            command[
                security_index
                + 1
            ],
            "no-new-privileges:true",
        )

        text = "\n".join(
            command
        )

        self.assertIn(
            "dst=/workspace",
            text,
        )

        self.assertIn(
            (
                f"src={self.workspace.path}/tests/test_example.py,"
                "dst=/workspace/tests/test_example.py,"
                "readonly"
            ),
            text,
        )

        self.assertIn(
            "dst=/relay,readonly",
            text,
        )

        self.assertNotIn(
            "/var/run/docker.sock",
            text,
        )

        self.assertNotIn(
            "--network=host",
            text,
        )

        self.assertIn(
            AIDER_MODEL,
            text,
        )

        launcher = command[
            -1
        ]

        for required in (
            "--no-git",
            "--no-gitignore",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--no-analytics",
            "--no-check-update",
            "--no-suggest-shell-commands",
            "--disable-playwright",
        ):
            self.assertIn(
                required,
                launcher,
            )

    def test_successful_run_returns_worker_evidence_only(
        self,
    ):
        runner = FakeDockerRunner(
            run_returncode=0,
            run_log="worker output\n",
        )

        worker = AiderWorker(
            self.config,
            command_runner=runner,
        )

        result = worker.run(
            self.request
        )

        self.assertEqual(
            result.status,
            STATUS_COMPLETED,
        )

        self.assertEqual(
            result.log,
            "worker output\n",
        )

        self.assertEqual(
            result.reported_changed_paths,
            (),
        )

        self.assertFalse(
            runner.container_exists
        )

    def test_nonzero_aider_exit_maps_to_failed_result(
        self,
    ):
        runner = FakeDockerRunner(
            run_returncode=2,
            run_log="Aider failed\n",
        )

        result = AiderWorker(
            self.config,
            command_runner=runner,
        ).run(
            self.request
        )

        self.assertEqual(
            result.status,
            STATUS_FAILED,
        )

        self.assertFalse(
            runner.container_exists
        )

    def test_timeout_maps_to_timed_out_and_forces_exact_cleanup(
        self,
    ):
        runner = FakeDockerRunner(
            run_returncode=-15,
            run_log="partial output\n",
            run_timed_out=True,
        )

        result = AiderWorker(
            self.config,
            command_runner=runner,
        ).run(
            self.request
        )

        self.assertEqual(
            result.status,
            STATUS_TIMED_OUT,
        )

        self.assertFalse(
            runner.container_exists
        )

        rm_commands = [
            call[
                0
            ]
            for call in runner.calls
            if (
                len(
                    call[
                        0
                    ]
                )
                >= 2
                and call[
                    0
                ][
                    1
                ]
                == "rm"
            )
        ]

        self.assertEqual(
            len(
                rm_commands
            ),
            1,
        )

        self.assertIn(
            "--force",
            rm_commands[
                0
            ],
        )

    def test_existing_request_container_is_never_deleted_or_reused(
        self,
    ):
        runner = FakeDockerRunner()

        runner.container_exists = True
        runner.container_running = False
        runner.request_id = self.request.request_id

        worker = AiderWorker(
            self.config,
            command_runner=runner,
        )

        with self.assertRaisesRegex(
            AiderWorkerError,
            "already exists",
        ):
            worker.run(
                self.request
            )

        self.assertTrue(
            runner.container_exists
        )

        self.assertFalse(
            any(
                call[
                    0
                ][
                    1
                ]
                == "rm"
                for call in runner.calls
                if len(
                    call[
                        0
                    ]
                )
                >= 2
            )
        )

    def test_wrong_worker_binding_is_refused_before_execution(
        self,
    ):
        request = build_lab_worker_request(
            worker="not-aider",
            goal="Do something.",
            workspace=self.workspace,
        )

        runner = FakeDockerRunner()

        worker = AiderWorker(
            self.config,
            command_runner=runner,
        )

        with self.assertRaisesRegex(
            AiderWorkerError,
            "not bound",
        ):
            worker.run(
                request
            )

        self.assertEqual(
            runner.calls,
            [],
        )

    def test_reduced_capabilities_are_refused(
        self,
    ):
        request = build_lab_worker_request(
            worker=AIDER_WORKER_NAME,
            goal="Do something.",
            workspace=self.workspace,
            capabilities=(
                CAPABILITY_WORKSPACE_READ,
            ),
        )

        runner = FakeDockerRunner()

        worker = AiderWorker(
            self.config,
            command_runner=runner,
        )

        with self.assertRaisesRegex(
            AiderWorkerError,
            "requires exactly",
        ):
            worker.run(
                request
            )

        self.assertEqual(
            runner.calls,
            [],
        )

    def test_missing_context_path_is_refused_by_physical_snapshot(
        self,
    ):
        config = build_aider_worker_config(
            relay_directory=str(
                self.relay
            ),
            editable_paths=(
                "src/missing.py",
            ),
        )

        runner = FakeDockerRunner()

        worker = AiderWorker(
            config,
            command_runner=runner,
        )

        with self.assertRaisesRegex(
            AiderWorkerError,
            "safely observed regular file",
        ):
            worker.run(
                self.request
            )

        self.assertEqual(
            runner.calls,
            [],
        )

    def test_relay_directory_with_extra_entry_is_refused(
        self,
    ):
        extra = (
            self.relay
            / "unexpected.txt"
        )

        extra.write_text(
            "unexpected\n",
            encoding="utf-8",
        )

        runner = FakeDockerRunner()

        worker = AiderWorker(
            self.config,
            command_runner=runner,
        )

        with self.assertRaisesRegex(
            AiderWorkerError,
            "contain only",
        ):
            worker.run(
                self.request
            )

        self.assertEqual(
            runner.calls,
            [],
        )

    def test_context_path_traversal_and_overlap_are_refused(
        self,
    ):
        with self.assertRaisesRegex(
            AiderWorkerError,
            "canonical relative",
        ):
            build_aider_worker_config(
                relay_directory=str(
                    self.relay
                ),
                editable_paths=(
                    "../escape.py",
                ),
            )

        with self.assertRaisesRegex(
            AiderWorkerError,
            "must not overlap",
        ):
            build_aider_worker_config(
                relay_directory=str(
                    self.relay
                ),
                editable_paths=(
                    "src/example.py",
                ),
                read_only_paths=(
                    "src/example.py",
                ),
            )

        with self.assertRaisesRegex(
            AiderWorkerError,
            "must not contain commas",
        ):
            build_aider_worker_config(
                relay_directory=str(
                    self.relay
                ),
                editable_paths=(
                    "src/example.py",
                ),
                read_only_paths=(
                    "tests/bad,name.py",
                ),
            )

    def test_module_has_no_promotion_or_approval_authority_imports(
        self,
    ):
        source = Path(
            aider_module.__file__
        ).read_text(
            encoding="utf-8"
        )

        tree = ast.parse(
            source
        )

        imported_modules = set()

        for node in ast.walk(
            tree
        ):
            if isinstance(
                node,
                ast.ImportFrom,
            ):
                if node.module:
                    imported_modules.add(
                        node.module
                    )

            elif isinstance(
                node,
                ast.Import,
            ):
                for alias in node.names:
                    imported_modules.add(
                        alias.name
                    )

        for forbidden in (
            "lab_promotion",
            "approval",
            "recovery",
            "transaction",
        ):
            self.assertFalse(
                any(
                    forbidden in item
                    for item in imported_modules
                )
            )

        for forbidden_text in (
            "docker.sock",
            "--network host",
            "--network=host",
            "git push",
            "git commit",
            "consume_promotion_approval",
            "build_lab_promotion_candidate",
        ):
            self.assertNotIn(
                forbidden_text,
                source,
            )


if __name__ == "__main__":
    unittest.main()
