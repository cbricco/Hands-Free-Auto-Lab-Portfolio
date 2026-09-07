from dataclasses import is_dataclass, replace
import ast
import hashlib
import json
from pathlib import Path
import unittest

import hands_free_auto_lab.lab_coding_integration_contract as contract
from hands_free_auto_lab.lab_worker_change_policy import (
    CHANGE_POLICY_COMPONENT,
    CHANGE_POLICY_SCHEMA_VERSION,
    LabWorkerChangePolicy,
    LabWorkerChangeTarget,
    OPERATION_ADD,
    validate_lab_worker_change_policy,
)


class LabCodingIntegrationContractTests(unittest.TestCase):
    def _policy(self):
        target = LabWorkerChangeTarget(
            operation=OPERATION_ADD,
            path="project.py",
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
        ).encode("utf-8")

        policy_id = hashlib.sha256(
            b"hands-free-auto-lab-worker-change-policy-id-v1\x00"
            + encoded
        ).hexdigest()

        return validate_lab_worker_change_policy(
            LabWorkerChangePolicy(
                component=CHANGE_POLICY_COMPONENT,
                schema_version=CHANGE_POLICY_SCHEMA_VERSION,
                policy_id=policy_id,
                targets=(target,),
            )
        )

    def _request(self):
        return contract.build_lab_coding_integration_request(
            repository_path="/synthetic/project",
            commit_oid="a" * 40,
            expected_branch="main",
            relative_paths=("project.py",),
            goal="Make one bounded synthetic change.",
            change_policy=self._policy(),
            max_runtime_seconds=30,
        )

    def test_request_is_immutable_versioned_and_canonical(self):
        request = self._request()

        self.assertTrue(
            is_dataclass(
                contract.LabCodingIntegrationRequest
            )
        )
        self.assertTrue(
            contract.LabCodingIntegrationRequest
            .__dataclass_params__
            .frozen
        )
        self.assertTrue(
            hasattr(
                contract.LabCodingIntegrationRequest,
                "__slots__",
            )
        )

        wire = contract.lab_coding_integration_request_to_wire(
            request
        )
        decoded = contract.lab_coding_integration_request_from_wire(
            wire
        )

        self.assertEqual(
            decoded,
            request,
        )

        record = contract.lab_coding_integration_request_to_bytes(
            request
        )

        self.assertEqual(
            contract.lab_coding_integration_request_from_bytes(
                record
            ),
            request,
        )

        self.assertTrue(
            record.endswith(b"\n")
        )

    def test_request_identity_binds_authoritative_fields(self):
        request = self._request()

        changed = contract.build_lab_coding_integration_request(
            repository_path=request.repository_path,
            commit_oid=request.commit_oid,
            expected_branch=request.expected_branch,
            relative_paths=request.relative_paths,
            goal="Different bounded goal.",
            change_policy=request.change_policy,
            max_runtime_seconds=request.max_runtime_seconds,
        )

        self.assertNotEqual(
            request.request_id,
            changed.request_id,
        )

        with self.assertRaises(
            contract.LabCodingIntegrationContractError
        ):
            contract.validate_lab_coding_integration_request(
                replace(
                    request,
                    goal="tampered",
                )
            )

    def test_request_wire_fails_closed_on_inexact_structure(self):
        request = self._request()
        wire = contract.lab_coding_integration_request_to_wire(
            request
        )

        bad_values = []

        extra = dict(wire)
        extra["unexpected"] = True
        bad_values.append(extra)

        missing = dict(wire)
        del missing["goal"]
        bad_values.append(missing)

        wrong_paths = dict(wire)
        wrong_paths["relative_paths"] = [
            "../escape.py"
        ]
        bad_values.append(wrong_paths)

        wrong_version = dict(wire)
        wrong_version["schema_version"] = 999
        bad_values.append(wrong_version)

        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(
                    contract.LabCodingIntegrationContractError
                ):
                    contract.lab_coding_integration_request_from_wire(
                        value
                    )

    def test_request_rejects_non_normalized_repository_path(self):
        with self.assertRaises(
            contract.LabCodingIntegrationContractError
        ):
            contract.build_lab_coding_integration_request(
                repository_path="/synthetic/../escape",
                commit_oid="a" * 40,
                expected_branch="main",
                relative_paths=("project.py",),
                goal="Make one bounded synthetic change.",
                change_policy=self._policy(),
                max_runtime_seconds=30,
            )

    def test_request_rejects_deployment_authority_fields(self):
        request = self._request()
        wire = contract.lab_coding_integration_request_to_wire(
            request
        )

        forbidden = {
            "worker",
            "worker_name",
            "workspace_parent",
            "candidate_store_root",
            "max_seed_files",
            "max_seed_bytes",
            "max_log_bytes",
            "promotion",
            "approval",
        }

        self.assertTrue(
            forbidden.isdisjoint(
                wire
            )
        )

    def test_result_binds_request_and_candidate(self):
        request = self._request()

        result = contract.build_lab_coding_integration_result(
            request=request,
            candidate_id="c" * 64,
        )

        self.assertEqual(
            result.request_id,
            request.request_id,
        )
        self.assertEqual(
            result.candidate_id,
            "c" * 64,
        )

        record = contract.lab_coding_integration_result_to_bytes(
            result
        )

        self.assertEqual(
            contract.lab_coding_integration_result_from_bytes(
                record
            ),
            result,
        )

        with self.assertRaises(
            contract.LabCodingIntegrationContractError
        ):
            contract.validate_lab_coding_integration_result(
                replace(
                    result,
                    candidate_id="d" * 64,
                )
            )

    def test_noncanonical_bytes_are_refused(self):
        request = self._request()

        wire = contract.lab_coding_integration_request_to_wire(
            request
        )

        noncanonical = json.dumps(
            wire,
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8")

        with self.assertRaises(
            contract.LabCodingIntegrationContractError
        ):
            contract.lab_coding_integration_request_from_bytes(
                noncanonical
            )

    def test_contract_import_boundary_has_no_execution_or_promotion_authority(self):
        source = Path(
            contract.__file__
        ).read_text(
            encoding="utf-8"
        )

        tree = ast.parse(source)

        imported_modules = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(
                    alias.name
                    for alias in node.names
                )

            if isinstance(node, ast.ImportFrom):
                imported_modules.append(
                    node.module or ""
                )

        forbidden_fragments = (
            "lab_coding_workflow",
            "lab_codex_worker",
            "promotion",
            "lab_executor",
            "lab_write_file",
            "subprocess",
            "socket",
            "urllib",
        )

        for imported in imported_modules:
            for forbidden in forbidden_fragments:
                self.assertNotIn(
                    forbidden,
                    imported,
                )


if __name__ == "__main__":
    unittest.main()
