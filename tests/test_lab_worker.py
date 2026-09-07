from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
import unittest

import hands_free_auto_lab.lab_worker as worker_module

from hands_free_auto_lab.lab_worker import (
    CAPABILITY_RUN_PROCESS,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    STATUS_COMPLETED,
    AutoLabWorker,
    LabWorkerContractError,
    build_lab_worker_request,
    build_lab_worker_result,
    validate_lab_worker_request,
    validate_lab_worker_result,
)
from hands_free_auto_lab.lab_workspace import (
    WORKSPACE_COMPONENT,
    LabWorkspace,
)


SESSION_ID = "1" * 64


def workspace() -> LabWorkspace:
    return LabWorkspace(
        component=WORKSPACE_COMPONENT,
        parent="/tmp/auto-lab",
        path=f"/tmp/auto-lab/{SESSION_ID}",
        session_id=SESSION_ID,
        device=11,
        inode=22,
        uid=1000,
        mode=0o700,
    )


def request():
    return build_lab_worker_request(
        worker="test-worker",
        goal="Implement and test one tiny feature.",
        workspace=workspace(),
        capabilities=(
            CAPABILITY_WORKSPACE_WRITE,
            CAPABILITY_WORKSPACE_READ,
            CAPABILITY_RUN_PROCESS,
        ),
        max_runtime_seconds=300,
        max_log_bytes=4096,
    )


class DummyWorker:
    def run(
        self,
        worker_request,
    ):
        return build_lab_worker_result(
            request=worker_request,
            status=STATUS_COMPLETED,
            summary="Feature completed in disposable workspace.",
            reported_changed_paths=(
                "src/example.py",
                "tests/test_example.py",
            ),
            log="worker transcript",
        )


class LabWorkerContractTests(
    unittest.TestCase
):
    def test_request_is_deterministic_and_canonical(self):
        first = request()
        second = request()

        self.assertEqual(
            first,
            second,
        )

        self.assertEqual(
            first.constraints.capabilities,
            (
                CAPABILITY_RUN_PROCESS,
                CAPABILITY_WORKSPACE_READ,
                CAPABILITY_WORKSPACE_WRITE,
            ),
        )

        self.assertEqual(
            len(
                first.request_id
            ),
            64,
        )

    def test_request_identity_binds_workspace(self):
        original = request()

        changed_workspace = replace(
            original.workspace,
            inode=999,
        )

        changed = build_lab_worker_request(
            worker=original.worker,
            goal=original.goal,
            workspace=changed_workspace,
            capabilities=original.constraints.capabilities,
            max_runtime_seconds=(
                original.constraints.max_runtime_seconds
            ),
            max_log_bytes=(
                original.constraints.max_log_bytes
            ),
        )

        self.assertNotEqual(
            original.request_id,
            changed.request_id,
        )

    def test_request_tamper_is_refused(self):
        original = request()

        tampered = replace(
            original,
            goal="different goal",
        )

        with self.assertRaisesRegex(
            LabWorkerContractError,
            "identity mismatch",
        ):
            validate_lab_worker_request(
                tampered
            )

    def test_unknown_capability_is_refused(self):
        with self.assertRaisesRegex(
            LabWorkerContractError,
            "unsupported worker capability",
        ):
            build_lab_worker_request(
                worker="test-worker",
                goal="test",
                workspace=workspace(),
                capabilities=(
                    "PROMOTE_REAL_REPOSITORY",
                ),
            )

    def test_worker_result_binds_exact_request_and_workspace(self):
        worker_request = request()

        result = build_lab_worker_result(
            request=worker_request,
            status=STATUS_COMPLETED,
            summary="done",
            reported_changed_paths=(
                "tests/test_alpha.py",
                "alpha.py",
            ),
            log="complete transcript",
        )

        self.assertEqual(
            result.request_id,
            worker_request.request_id,
        )

        self.assertEqual(
            result.workspace_session_id,
            worker_request.workspace.session_id,
        )

        self.assertEqual(
            result.reported_changed_paths,
            (
                "alpha.py",
                "tests/test_alpha.py",
            ),
        )

        validate_lab_worker_result(
            result,
            request=worker_request,
        )

    def test_worker_result_tamper_is_refused(self):
        worker_request = request()

        result = build_lab_worker_result(
            request=worker_request,
            status=STATUS_COMPLETED,
            summary="done",
            log="original log",
        )

        tampered = replace(
            result,
            log="changed log",
        )

        with self.assertRaisesRegex(
            LabWorkerContractError,
            "identity mismatch",
        ):
            validate_lab_worker_result(
                tampered,
                request=worker_request,
            )

    def test_noncanonical_reported_path_is_refused(self):
        worker_request = request()

        with self.assertRaisesRegex(
            LabWorkerContractError,
            "canonical and relative",
        ):
            build_lab_worker_result(
                request=worker_request,
                status=STATUS_COMPLETED,
                summary="done",
                reported_changed_paths=(
                    "../outside.txt",
                ),
            )

    def test_result_log_limit_is_enforced(self):
        worker_request = build_lab_worker_request(
            worker="test-worker",
            goal="test",
            workspace=workspace(),
            max_log_bytes=8,
        )

        with self.assertRaisesRegex(
            LabWorkerContractError,
            "log exceeds request limit",
        ):
            build_lab_worker_result(
                request=worker_request,
                status=STATUS_COMPLETED,
                summary="done",
                log="123456789",
            )

    def test_protocol_supports_backend_adapter_shape(self):
        worker_request = request()

        adapter: AutoLabWorker = DummyWorker()

        result = adapter.run(
            worker_request
        )

        validate_lab_worker_result(
            result,
            request=worker_request,
        )

        self.assertEqual(
            result.status,
            STATUS_COMPLETED,
        )

    def test_contract_module_has_no_execution_or_authority_imports(self):
        source = Path(
            worker_module.__file__
        ).read_text(
            encoding="utf-8"
        )

        tree = ast.parse(
            source
        )

        imported_roots = set()

        for node in ast.walk(
            tree
        ):
            if isinstance(
                node,
                ast.Import,
            ):
                for alias in node.names:
                    imported_roots.add(
                        alias.name.split(
                            "."
                        )[0]
                    )

            elif isinstance(
                node,
                ast.ImportFrom,
            ):
                if node.module:
                    imported_roots.add(
                        node.module
                    )

        allowed = {
            "__future__",
            "dataclasses",
            "hashlib",
            "json",
            "pathlib",
            "typing",
            "lab_workspace",
        }

        unexpected = {
            item
            for item in imported_roots
            if item not in allowed
        }

        self.assertEqual(
            unexpected,
            set(),
        )

        forbidden_runtime_tokens = (
            "subprocess.",
            "os.system",
            "os.exec",
            "os.spawn",
            "Popen(",
            "urlopen(",
            "socket.",
            "execute_lab_",
            "append_promotion_transaction_snapshot(",
            "consume_promotion_approval(",
        )

        for token in forbidden_runtime_tokens:
            self.assertNotIn(
                token,
                source,
            )


if __name__ == "__main__":
    unittest.main()
