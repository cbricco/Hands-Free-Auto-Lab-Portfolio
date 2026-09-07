from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SRC_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
)

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hands_free_auto_lab import lab_coding_cli as cli
from hands_free_auto_lab import lab_coding_local_front_door as front


class LabCodingLocalFrontDoorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="auto-lab-local-front-door-"
        )
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)

        self.dot_config = self.home / ".config"
        self.dot_config.mkdir(mode=0o700)
        self.dot_config.chmod(0o700)

        self.config_directory = (
            self.dot_config
            / front.CONFIG_DIRECTORY_NAME
        )
        self.config_directory.mkdir(mode=0o700)
        self.config_directory.chmod(0o700)

        self.config_file = (
            self.config_directory
            / front.CONFIG_FILE_NAME
        )

        self.passwd_patch = mock.patch.object(
            front.pwd,
            "getpwuid",
            return_value=types.SimpleNamespace(
                pw_dir=str(self.home),
            ),
        )
        self.passwd_patch.start()

        self.write_config(
            self.environment()
        )

    def tearDown(self) -> None:
        self.passwd_patch.stop()
        self.temp.cleanup()

    def environment(self) -> dict[str, str]:
        root = "/private/auto-lab"

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

    def write_config(
        self,
        value: object,
    ) -> None:
        self.config_file.write_text(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        self.config_file.chmod(0o600)

    def write_raw_config(
        self,
        value: bytes,
    ) -> None:
        self.config_file.write_bytes(value)
        self.config_file.chmod(0o600)

    def test_fixed_private_config_delegates_exact_streams_and_mapping(
        self,
    ) -> None:
        request = io.BytesIO(
            b'{"canonical":"request"}\n'
        )
        stdout = io.BytesIO()
        expected = b'{"canonical":"result"}\n'
        calls = []

        class FakeRuntime:
            pass

        def fake_run_cli(**kwargs):
            calls.append(kwargs)
            self.assertIs(
                kwargs["stdin"],
                request,
            )
            self.assertIs(
                kwargs["stdout"],
                stdout,
            )
            self.assertIs(
                kwargs["runtime_factory"],
                FakeRuntime,
            )
            self.assertEqual(
                kwargs["environ"],
                self.environment(),
            )

            record = kwargs["stdin"].read()
            self.assertEqual(
                record,
                b'{"canonical":"request"}\n',
            )

            kwargs["stdout"].write(expected)
            return 0

        with mock.patch.object(
            front.cli,
            "run_cli",
            side_effect=fake_run_cli,
        ):
            status = front.run_local_front_door(
                environ={
                    "HOME": "/attacker/home",
                    "XDG_CONFIG_HOME": "/attacker/config",
                },
                stdin=request,
                stdout=stdout,
                runtime_factory=FakeRuntime,
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            stdout.getvalue(),
            expected,
        )
        self.assertEqual(
            len(calls),
            1,
        )

    def test_caller_auto_lab_configuration_is_refused(
        self,
    ) -> None:
        with mock.patch.object(
            front.cli,
            "run_cli",
        ) as runner:
            with self.assertRaisesRegex(
                front.LabCodingLocalFrontDoorError,
                "caller must not supply",
            ):
                front.run_local_front_door(
                    environ={
                        cli.ENV_CODEX_HOME: "/caller/value",
                    },
                    stdin=io.BytesIO(b"request"),
                    stdout=io.BytesIO(),
                )

        runner.assert_not_called()

    def test_duplicate_unknown_missing_and_non_string_config_fail_closed(
        self,
    ) -> None:
        environment = self.environment()
        first_name = sorted(environment)[0]

        duplicate = (
            "{"
            + json.dumps(first_name)
            + ":"
            + json.dumps(environment[first_name])
            + ","
            + json.dumps(first_name)
            + ":"
            + json.dumps(environment[first_name])
            + "}"
        ).encode("utf-8")

        cases = []

        cases.append(
            (
                duplicate,
                "duplicate Auto Lab configuration key",
            )
        )

        unknown = dict(environment)
        unknown["HF_AUTO_LAB_AUTO_APPROVE"] = "1"
        cases.append(
            (
                (
                    json.dumps(unknown)
                    + "\n"
                ).encode("utf-8"),
                "unknown Auto Lab configuration key",
            )
        )

        missing = dict(environment)
        del missing[first_name]
        cases.append(
            (
                (
                    json.dumps(missing)
                    + "\n"
                ).encode("utf-8"),
                "missing Auto Lab configuration key",
            )
        )

        non_string = dict(environment)
        non_string[
            cli.ENV_MAX_SEED_FILES
        ] = 64
        cases.append(
            (
                (
                    json.dumps(non_string)
                    + "\n"
                ).encode("utf-8"),
                "configuration value must be text",
            )
        )

        cases.append(
            (
                b"[]\n",
                "must be one JSON object",
            )
        )

        for record, expected_error in cases:
            with self.subTest(
                expected_error=expected_error,
            ):
                self.write_raw_config(record)

                with self.assertRaisesRegex(
                    front.LabCodingLocalFrontDoorError,
                    expected_error,
                ):
                    front.load_fixed_runtime_environment()

    def test_values_are_not_expanded_or_interpreted(
        self,
    ) -> None:
        environment = self.environment()
        environment[
            cli.ENV_RUNTIME_TEMP
        ] = "$HOME/runtime/${USER}"

        self.write_config(environment)

        loaded = (
            front.load_fixed_runtime_environment()
        )

        self.assertEqual(
            loaded[cli.ENV_RUNTIME_TEMP],
            "$HOME/runtime/${USER}",
        )

    def test_private_directory_and_file_modes_are_required(
        self,
    ) -> None:
        self.config_directory.chmod(0o755)

        with self.assertRaisesRegex(
            front.LabCodingLocalFrontDoorError,
            "directory must have mode 0700",
        ):
            front.load_fixed_runtime_environment()

        self.config_directory.chmod(0o700)
        self.config_file.chmod(0o644)

        with self.assertRaisesRegex(
            front.LabCodingLocalFrontDoorError,
            "file must have mode 0600",
        ):
            front.load_fixed_runtime_environment()

    def test_config_file_symlink_is_refused(
        self,
    ) -> None:
        target = (
            self.config_directory
            / "real-runtime.json"
        )
        target.write_bytes(
            self.config_file.read_bytes()
        )
        target.chmod(0o600)

        self.config_file.unlink()
        self.config_file.symlink_to(target.name)

        with self.assertRaisesRegex(
            front.LabCodingLocalFrontDoorError,
            "symlink alias",
        ):
            front.load_fixed_runtime_environment()

    def test_config_directory_symlink_is_refused(
        self,
    ) -> None:
        real_directory = (
            self.dot_config
            / "real-hands-free-auto-lab"
        )

        self.config_directory.rename(
            real_directory
        )
        self.config_directory.symlink_to(
            real_directory.name,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            front.LabCodingLocalFrontDoorError,
            "symlink alias",
        ):
            front.load_fixed_runtime_environment()

    def test_main_keeps_failures_off_stdout(
        self,
    ) -> None:
        stdout = io.BytesIO()
        stderr = io.StringIO()

        status = front.main(
            environ={
                cli.ENV_CODEX_HOME: "/caller/value",
            },
            stdin=io.BytesIO(b"request"),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(status, 2)
        self.assertEqual(
            stdout.getvalue(),
            b"",
        )
        self.assertIn(
            "LabCodingLocalFrontDoorError",
            stderr.getvalue(),
        )

    def test_source_has_no_new_external_authority_or_config_path_parameter(
        self,
    ) -> None:
        source_path = (
            SRC_ROOT
            / "hands_free_auto_lab"
            / "lab_coding_local_front_door.py"
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

            if isinstance(node, ast.FunctionDef):
                if node.name in {
                    "run_local_front_door",
                    "main",
                }:
                    parameter_names = {
                        item.arg
                        for item in (
                            list(node.args.args)
                            + list(node.args.kwonlyargs)
                        )
                    }
                    self.assertNotIn(
                        "config_path",
                        parameter_names,
                    )
                    self.assertNotIn(
                        "configuration_path",
                        parameter_names,
                    )

        self.assertTrue(
            forbidden_import_roots.isdisjoint(
                imported_roots
            )
        )

        lowered = source.lower()

        for forbidden in (
            "subprocess",
            "socket",
            "requests",
            "urllib",
            "os.system",
            "shell=true",
            "expandvars",
            "expanduser",
            "git add",
            "git commit",
            "git push",
            "auto_approve",
            "allow_write",
            "promotion",
            "tier 1",
        ):
            with self.subTest(
                forbidden=forbidden,
            ):
                self.assertNotIn(
                    forbidden,
                    lowered,
                )


if __name__ == "__main__":
    unittest.main()
