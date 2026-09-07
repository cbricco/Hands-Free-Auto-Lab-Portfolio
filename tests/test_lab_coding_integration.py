import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from hands_free_auto_lab.lab_coding_candidate_store import (
    initialize_lab_coding_candidate_store,
    load_lab_coding_candidate,
)
from hands_free_auto_lab.lab_coding_integration import (
    LabCodingIntegrationDeployment,
    run_lab_coding_integration_request,
)
from hands_free_auto_lab.lab_coding_integration_contract import (
    build_lab_coding_integration_request,
    lab_coding_integration_result_from_bytes,
    lab_coding_integration_result_to_bytes,
)
from hands_free_auto_lab.lab_worker import (
    STATUS_COMPLETED,
    build_lab_worker_result,
)
from hands_free_auto_lab.lab_worker_change_policy import (
    CHANGE_POLICY_COMPONENT,
    CHANGE_POLICY_SCHEMA_VERSION,
    LabWorkerChangePolicy,
    LabWorkerChangeTarget,
    OPERATION_ADD,
    validate_lab_worker_change_policy,
)


class SyntheticBoundedWorker:
    def __init__(self, content):
        self.content = content
        self.requests = []

    def run(self, request):
        self.requests.append(request)

        target = (
            Path(request.workspace.path)
            / "generated.py"
        )

        target.write_text(
            self.content,
            encoding="utf-8",
        )
        os.chmod(
            target,
            0o600,
        )

        return build_lab_worker_result(
            request=request,
            status=STATUS_COMPLETED,
            summary="Synthetic bounded worker completed.",
            reported_changed_paths=(
                "generated.py",
            ),
            log="synthetic worker\n",
        )


class LabCodingIntegrationTests(unittest.TestCase):
    def _git(
        self,
        repository,
        *arguments,
    ):
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(repository),
                *arguments,
            ),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        return completed.stdout.strip()

    def _create_repository(
        self,
        root,
    ):
        repository = (
            root
            / "source-repository"
        )
        repository.mkdir(
            mode=0o700
        )

        subprocess.run(
            (
                "git",
                "-C",
                str(repository),
                "init",
                "-q",
                "-b",
                "main",
            ),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._git(
            repository,
            "config",
            "user.name",
            "Synthetic Test",
        )
        self._git(
            repository,
            "config",
            "user.email",
            "synthetic@example.invalid",
        )

        source = (
            repository
            / "seed.txt"
        )
        source.write_text(
            "committed seed\n",
            encoding="utf-8",
        )
        os.chmod(
            source,
            0o600,
        )

        self._git(
            repository,
            "add",
            "--",
            "seed.txt",
        )
        self._git(
            repository,
            "commit",
            "-q",
            "-m",
            "Synthetic seed",
        )

        head = self._git(
            repository,
            "rev-parse",
            "HEAD",
        )

        self.assertEqual(
            len(head),
            40,
        )

        self.assertEqual(
            self._git(
                repository,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            "",
        )

        return (
            repository,
            head,
        )

    def _policy(self):
        target = LabWorkerChangeTarget(
            operation=OPERATION_ADD,
            path="generated.py",
            max_final_bytes=4096,
            final_mode=0o600,
            before_bytes=None,
            before_sha256=None,
            before_mode=None,
        )

        identity = {
            "component": CHANGE_POLICY_COMPONENT,
            "schema_version": CHANGE_POLICY_SCHEMA_VERSION,
            "targets": [
                {
                    "operation": target.operation,
                    "path": target.path,
                    "max_final_bytes": target.max_final_bytes,
                    "final_mode": target.final_mode,
                    "before_bytes": target.before_bytes,
                    "before_sha256": target.before_sha256,
                    "before_mode": target.before_mode,
                }
            ],
        }

        encoded = json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode(
            "utf-8"
        )

        policy_id = hashlib.sha256(
            b"hands-free-auto-lab-worker-change-policy-id-v1\x00"
            + encoded
        ).hexdigest()

        return validate_lab_worker_change_policy(
            LabWorkerChangePolicy(
                component=CHANGE_POLICY_COMPONENT,
                schema_version=CHANGE_POLICY_SCHEMA_VERSION,
                policy_id=policy_id,
                targets=(
                    target,
                ),
            )
        )

    def test_external_request_reaches_durable_candidate_without_source_mutation(self):
        with tempfile.TemporaryDirectory(
            prefix="auto-lab-external-proof."
        ) as temporary:
            root = Path(
                temporary
            ).resolve()

            repository, head = self._create_repository(
                root
            )

            workspace_parent = (
                root
                / "workspaces"
            )
            workspace_parent.mkdir(
                mode=0o700
            )

            store = (
                root
                / "candidate-store"
            )
            store.mkdir(
                mode=0o700
            )
            initialize_lab_coding_candidate_store(
                str(store)
            )

            source_before = (
                repository
                / "seed.txt"
            ).read_bytes()

            source_status_before = self._git(
                repository,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            )

            worker = SyntheticBoundedWorker(
                "VALUE = 42\n"
            )

            request = build_lab_coding_integration_request(
                repository_path=str(repository),
                commit_oid=head,
                expected_branch="main",
                relative_paths=(
                    "seed.txt",
                ),
                goal=(
                    "Create generated.py containing the "
                    "bounded synthetic result."
                ),
                change_policy=self._policy(),
                max_runtime_seconds=30,
            )

            deployment = LabCodingIntegrationDeployment(
                worker=worker,
                workspace_parent=str(
                    workspace_parent
                ),
                candidate_store_root=str(
                    store
                ),
                session_id="b" * 64,
                max_seed_files=8,
                max_seed_bytes=64 * 1024,
                max_runtime_seconds=20,
                max_log_bytes=64 * 1024,
            )

            result = run_lab_coding_integration_request(
                request=request,
                deployment=deployment,
            )

            self.assertEqual(
                result.request_id,
                request.request_id,
            )

            self.assertEqual(
                len(worker.requests),
                1,
            )

            self.assertEqual(
                worker.requests[0]
                .constraints
                .max_runtime_seconds,
                20,
            )

            candidate = load_lab_coding_candidate(
                str(store),
                result.candidate_id,
            )

            self.assertEqual(
                candidate.candidate_id,
                result.candidate_id,
            )

            self.assertEqual(
                tuple(
                    item.path
                    for item in candidate.files
                ),
                (
                    "generated.py",
                ),
            )

            self.assertEqual(
                candidate.files[0].content,
                "VALUE = 42\n",
            )

            encoded_result = (
                lab_coding_integration_result_to_bytes(
                    result
                )
            )

            self.assertEqual(
                lab_coding_integration_result_from_bytes(
                    encoded_result
                ),
                result,
            )

            self.assertEqual(
                self._git(
                    repository,
                    "rev-parse",
                    "HEAD",
                ),
                head,
            )

            self.assertEqual(
                self._git(
                    repository,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ),
                source_status_before,
            )

            self.assertEqual(
                (
                    repository
                    / "seed.txt"
                ).read_bytes(),
                source_before,
            )

            self.assertFalse(
                (
                    repository
                    / "generated.py"
                ).exists()
            )

    def test_adapter_source_has_no_promotion_or_git_authority_imports(self):
        import hands_free_auto_lab.lab_coding_integration as integration

        source = Path(
            integration.__file__
        ).read_text(
            encoding="utf-8"
        )

        tree = ast.parse(
            source
        )

        imported_modules = []

        for node in ast.walk(tree):
            if isinstance(
                node,
                ast.Import,
            ):
                imported_modules.extend(
                    alias.name
                    for alias in node.names
                )

            if isinstance(
                node,
                ast.ImportFrom,
            ):
                imported_modules.append(
                    node.module or ""
                )

        forbidden = (
            "promotion",
            "lab_executor",
            "lab_write_file",
            "subprocess",
            "socket",
            "urllib",
        )

        for imported in imported_modules:
            for fragment in forbidden:
                self.assertNotIn(
                    fragment,
                    imported,
                )


if __name__ == "__main__":
    unittest.main()
