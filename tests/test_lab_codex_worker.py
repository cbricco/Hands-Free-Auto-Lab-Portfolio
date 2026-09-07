from __future__ import annotations

import ast
from dataclasses import replace
import gc
from pathlib import Path
import os
import shutil
import sys
import tempfile
import unittest
import warnings

import hands_free_auto_lab.lab_codex_worker as codex_module
from hands_free_auto_lab.lab_codex_worker import (
    CODEX_WORKER_NAME,
    CodexCommandResult,
    CodexWorker,
    CodexWorkerError,
    build_codex_command,
    build_codex_control_environment,
    build_codex_worker_config,
)
from hands_free_auto_lab.lab_worker import (
    CAPABILITY_WORKSPACE_READ,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    build_lab_worker_request,
)
from hands_free_auto_lab.lab_worker_change_policy import (
    LabWorkerChangeTarget,
    OPERATION_ADD,
    build_lab_worker_change_policy,
)
from hands_free_auto_lab.lab_workspace import create_lab_workspace


SESSION_ID = "8" * 64


class FakeRunner:
    def __init__(self, returncode=0, log="done\n", timed_out=False):
        self.result = CodexCommandResult(returncode, log, timed_out)
        self.calls = []

    def __call__(self, argv, cwd, environment, timeout, output_limit):
        self.calls.append((argv, cwd, environment, timeout, output_limit))
        return self.result


class CodexWorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="auto-lab-codex-tests-"))
        self.parent = self.root / "workspaces"
        self.parent.mkdir(mode=0o700)
        self.workspace = create_lab_workspace(str(self.parent), session_id=SESSION_ID)
        (Path(self.workspace.path) / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.release = self.root / "release"
        self.release.mkdir(mode=0o755)
        self.executable = self.release / "codex"
        self.executable.write_text("binary placeholder\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.home = self.root / "codex-home"
        self.home.mkdir(mode=0o700)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.config = build_codex_worker_config(
            codex_executable=str(self.executable),
            codex_home=str(self.home),
            codex_release_root=str(self.release),
            runtime_temp=str(self.runtime),
            max_runtime_seconds=30,
            max_output_bytes=8192,
        )
        self.request = build_lab_worker_request(
            worker=CODEX_WORKER_NAME, goal="Implement the requested change.",
            workspace=self.workspace, max_runtime_seconds=60, max_log_bytes=16384,
        )

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_exact_command_shape_and_approval_placement(self):
        command = build_codex_command(request=self.request, config=self.config)
        self.assertEqual(command[:4], (str(self.executable), "--ask-for-approval", "never", "exec"))
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules",
                     "--skip-git-repo-check", "--json"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("-C") + 1], self.workspace.path)
        self.assertNotIn("--sandbox", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(
            command[-2:],
            ("--", codex_module.build_codex_effective_goal(self.request.goal)),
        )

    def test_permission_profile_and_environment_are_minimal(self):
        argv = build_codex_command(request=self.request, config=self.config)
        command = "\n".join(argv)
        overrides = tuple(argv[index + 1] for index, item in enumerate(argv) if item == "-c")
        filesystem_overrides = tuple(
            item for item in overrides
            if item.startswith("permissions.auto-lab-worker.filesystem")
        )
        network_overrides = tuple(
            item for item in overrides
            if item.startswith("permissions.auto-lab-worker.network")
        )
        expected_filesystem = (
            'permissions.auto-lab-worker.filesystem={":root"="deny",'
            '":minimal"="read",":slash_tmp"="deny",":tmpdir"="write",'
            f'"{self.release}"="read",":workspace_roots"={{"."="write"}}}}'
        )
        self.assertEqual(filesystem_overrides, (expected_filesystem,))
        self.assertEqual(
            network_overrides,
            ("permissions.auto-lab-worker.network={enabled=false}",),
        )
        self.assertIn('web_search="disabled"', overrides)
        self.assertFalse(any(item.startswith("features.web_search") for item in overrides))
        for text in (
            'default_permissions="auto-lab-worker"',
            'permissions.auto-lab-worker.extends=":workspace"',
            'shell_environment_policy.inherit="none"',
            f'shell_environment_policy.set.HOME="{self.runtime}/generated-home"',
            f'shell_environment_policy.set.TMPDIR="{self.runtime}"',
            f'shell_environment_policy.set.PATH="{self.release}/codex-path:/usr/bin:/bin"',
        ):
            self.assertIn(text, command)
        self.assertIn(self.workspace.path, command)
        self.assertNotIn("sandbox_workspace_write.", command)
        self.assertNotIn("permissions.filesystem.", command)
        self.assertNotIn("permissions.network", command)
        for old_override in (
            'permissions.auto-lab-worker.filesystem.":root"=',
            'permissions.auto-lab-worker.filesystem.":minimal"=',
            'permissions.auto-lab-worker.filesystem.":slash_tmp"=',
            'permissions.auto-lab-worker.filesystem.":tmpdir"=',
            f'permissions.auto-lab-worker.filesystem."{self.release}"=',
            'permissions.auto-lab-worker.filesystem.":workspace_roots"',
        ):
            self.assertNotIn(old_override, command)
        environment = build_codex_control_environment(self.config)
        self.assertEqual(set(environment), {"CODEX_HOME", "HOME", "TMPDIR", "PATH"})
        self.assertEqual(environment["CODEX_HOME"], str(self.home))
        self.assertNotIn(str(self.home), command)

    def test_effective_goal_is_deterministic_and_preserves_exact_user_goal(self):
        exact_goal = "  Keep leading spaces.\n\nKeep the blank line.  "
        request = build_lab_worker_request(
            worker=CODEX_WORKER_NAME, goal=exact_goal, workspace=self.workspace,
            max_runtime_seconds=60, max_log_bytes=16384,
        )
        first = build_codex_command(request=request, config=self.config)[-1]
        second = build_codex_command(request=request, config=self.config)[-1]
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(exact_goal))
        self.assertLess(first.index("DIRECT_WORKSPACE"), len(first) - len(exact_goal))

    def test_direct_workspace_is_the_default_mutation_transport(self):
        effective_goal = build_codex_command(
            request=self.request, config=self.config
        )[-1]
        self.assertIn("MUTATION TRANSPORT: DIRECT_WORKSPACE", effective_goal)
        self.assertIn("Do not use\napply_patch or nested sandbox helpers", effective_goal)
        self.assertIn(
            "Report unexpected generated or\nruntime artifacts; do not delete them merely "
            "to obtain a passing candidate",
            effective_goal,
        )
        self.assertIn("do not expand authorization or writable scope", effective_goal)

    def test_codex_shell_environment_disables_python_bytecode(self):
        command = "\n".join(build_codex_command(
            request=self.request, config=self.config
        ))
        self.assertIn('shell_environment_policy.inherit="none"', command)
        self.assertIn(
            'shell_environment_policy.set.PYTHONDONTWRITEBYTECODE="1"', command
        )
        self.assertIn(
            f'shell_environment_policy.set.PYTHONPYCACHEPREFIX="{self.runtime}/generated-pycache"',
            command,
        )
        self.assertIn(
            f'shell_environment_policy.set.HOME="{self.runtime}/generated-home"', command
        )
        self.assertIn(
            f'shell_environment_policy.set.TMPDIR="{self.runtime}"', command
        )
        self.assertIn(
            f'shell_environment_policy.set.PATH="{self.release}/codex-path:/usr/bin:/bin"',
            command,
        )
        self.assertIn(
            'permissions.auto-lab-worker.network={enabled=false}', command
        )

    def test_explicit_py_compile_is_redirected_outside_workspace(self):
        import os
        import subprocess
        import sys

        workspace = Path(self.workspace.path)
        source = workspace / "explicit_compile_target.py"

        source.write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )

        cache_prefix = (
            self.runtime
            / "generated-pycache"
        )

        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPYCACHEPREFIX"] = str(
            cache_prefix
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "py_compile",
                str(source),
            ],
            cwd=str(workspace),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stderr.decode(
                "utf-8",
                errors="replace",
            ),
        )

        self.assertFalse(
            (workspace / "__pycache__").exists()
        )

        redirected = tuple(
            cache_prefix.rglob(
                "explicit_compile_target.*.pyc"
            )
        )

        self.assertEqual(
            len(redirected),
            1,
        )


    def test_optional_proxy_is_loopback_and_control_only(self):
        config = build_codex_worker_config(
            codex_executable=str(self.executable), codex_home=str(self.home),
            codex_release_root=str(self.release), runtime_temp=str(self.runtime),
            max_runtime_seconds=30,
            max_output_bytes=8192, trusted_client_proxy="http://127.0.0.1:8123",
        )
        environment = build_codex_control_environment(config)
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:8123")
        self.assertNotIn("HTTP_PROXY", "\n".join(build_codex_command(request=self.request, config=config)))
        with self.assertRaisesRegex(CodexWorkerError, "loopback"):
            replace(config, trusted_client_proxy="http://example.com:8123")
            build_codex_worker_config(
                codex_executable=str(self.executable), codex_home=str(self.home),
                codex_release_root=str(self.release), runtime_temp=str(self.runtime),
                max_runtime_seconds=30,
                max_output_bytes=8192, trusted_client_proxy="http://example.com:8123",
            )

    def test_default_command_runner_sets_deterministic_private_umask(self):
        target = (
            Path(self.workspace.path)
            / "umask_probe.txt"
        )

        previous_umask = os.umask(0o002)

        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter(
                    "always",
                    ResourceWarning,
                )

                result = codex_module._default_command_runner(
                    (
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "Path('umask_probe.txt').write_bytes(b'private\\n')"
                        ),
                    ),
                    self.workspace.path,
                    {
                        "PATH": os.environ.get(
                            "PATH",
                            "/usr/bin:/bin",
                        ),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    10,
                    8192,
                )

                gc.collect()
        finally:
            os.umask(previous_umask)

        resource_warnings = tuple(
            item
            for item in caught
            if issubclass(
                item.category,
                ResourceWarning,
            )
        )

        self.assertEqual(
            resource_warnings,
            (),
        )
        self.assertEqual(
            result.returncode,
            0,
            result.log,
        )
        self.assertFalse(
            result.timed_out,
        )
        self.assertEqual(
            target.read_bytes(),
            b"private\n",
        )
        self.assertEqual(
            target.stat().st_mode & 0o777,
            0o600,
        )

    def test_success_nonzero_and_timeout_mapping(self):
        for runner, expected in (
            (FakeRunner(), STATUS_COMPLETED),
            (FakeRunner(returncode=2), STATUS_FAILED),
            (FakeRunner(returncode=-15, timed_out=True), STATUS_TIMED_OUT),
        ):
            result = CodexWorker(self.config, command_runner=runner).run(self.request)
            self.assertEqual(result.status, expected)
            self.assertEqual(result.reported_changed_paths, ())
            self.assertEqual(runner.calls[0][3:], (30, 8192))

    def test_bound_policy_guidance_preserves_request_identity_and_goal(self):
        exact_goal = "Keep this worker goal exactly."

        policy = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path="guided.txt",
                    max_final_bytes=128,
                    final_mode=0o644,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                ),
            )
        )

        request = build_lab_worker_request(
            worker=CODEX_WORKER_NAME,
            goal=exact_goal,
            workspace=self.workspace,
            change_policy_id=policy.policy_id,
            capabilities=self.request.constraints.capabilities,
            max_runtime_seconds=(
                self.request.constraints.max_runtime_seconds
            ),
            max_log_bytes=(
                self.request.constraints.max_log_bytes
            ),
        )

        original_request_id = request.request_id

        command = build_codex_command(
            request=request,
            config=self.config,
            change_policy=policy,
        )

        self.assertEqual(
            request.goal,
            exact_goal,
        )
        self.assertEqual(
            request.request_id,
            original_request_id,
        )

        effective_goal = command[-1]

        self.assertTrue(
            effective_goal.endswith(
                exact_goal
            )
        )
        self.assertIn(
            "AUTO LAB CHANGE POLICY GUIDANCE",
            effective_goal,
        )
        self.assertIn(
            "operation=ADDED",
            effective_goal,
        )
        self.assertIn(
            "path='guided.txt'",
            effective_goal,
        )
        self.assertIn(
            "max_final_bytes=128",
            effective_goal,
        )
        self.assertIn(
            "final_mode=0644",
            effective_goal,
        )
        self.assertIn(
            "grants no authority",
            effective_goal,
        )

    def test_policy_bound_codex_worker_requires_exact_matching_policy(self):
        first_policy = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path="first.txt",
                    max_final_bytes=128,
                    final_mode=0o644,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                ),
            )
        )

        second_policy = build_lab_worker_change_policy(
            targets=(
                LabWorkerChangeTarget(
                    operation=OPERATION_ADD,
                    path="second.txt",
                    max_final_bytes=128,
                    final_mode=0o644,
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                ),
            )
        )

        request = build_lab_worker_request(
            worker=CODEX_WORKER_NAME,
            goal="Do the exact bounded work.",
            workspace=self.workspace,
            change_policy_id=first_policy.policy_id,
            capabilities=self.request.constraints.capabilities,
            max_runtime_seconds=(
                self.request.constraints.max_runtime_seconds
            ),
            max_log_bytes=(
                self.request.constraints.max_log_bytes
            ),
        )

        runner = FakeRunner()

        worker = CodexWorker(
            self.config,
            command_runner=runner,
        )

        with self.assertRaisesRegex(
            CodexWorkerError,
            "requires the exact change policy",
        ):
            worker.run(request)

        with self.assertRaisesRegex(
            CodexWorkerError,
            "binding mismatch",
        ):
            (
                worker
                .bind_change_policy(
                    second_policy
                )
                .run(request)
            )

        self.assertEqual(
            runner.calls,
            [],
        )

        result = (
            worker
            .bind_change_policy(
                first_policy
            )
            .run(request)
        )

        self.assertEqual(
            result.request_id,
            request.request_id,
        )
        self.assertEqual(
            request.goal,
            "Do the exact bounded work.",
        )
        self.assertEqual(
            len(runner.calls),
            1,
        )

    def test_wrong_binding_and_reduced_capabilities_are_rejected(self):
        runner = FakeRunner()
        wrong = build_lab_worker_request(
            worker="other", goal="Do work.", workspace=self.workspace,
        )
        with self.assertRaisesRegex(CodexWorkerError, "not bound"):
            CodexWorker(self.config, command_runner=runner).run(wrong)
        reduced = build_lab_worker_request(
            worker=CODEX_WORKER_NAME, goal="Do work.", workspace=self.workspace,
            capabilities=(CAPABILITY_WORKSPACE_READ,),
        )
        with self.assertRaisesRegex(CodexWorkerError, "requires exactly"):
            CodexWorker(self.config, command_runner=runner).run(reduced)
        self.assertEqual(runner.calls, [])

    def test_physical_context_rejection_occurs_before_execution(self):
        self.runtime.chmod(0o755)
        runner = FakeRunner()
        with self.assertRaisesRegex(CodexWorkerError, "unsafe permissions"):
            CodexWorker(self.config, command_runner=runner).run(self.request)
        self.assertEqual(runner.calls, [])

    def test_workspace_identity_rejection_occurs_before_execution(self):
        Path(self.workspace.path).chmod(0o755)
        runner = FakeRunner()
        with self.assertRaisesRegex(CodexWorkerError, "safe Codex workspace"):
            CodexWorker(self.config, command_runner=runner).run(self.request)
        self.assertEqual(runner.calls, [])

    def test_module_excludes_authority_imports_and_dangerous_execution(self):
        source = Path(codex_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in ("promotion", "approval", "controller", "audit", "git"):
            self.assertFalse(any(forbidden in name.lower() for name in imported))
        self.assertNotIn("/usr/bin/codex", source)
        self.assertNotIn("dangerously-bypass", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)


if __name__ == "__main__":
    unittest.main()
