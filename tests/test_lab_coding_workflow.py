from pathlib import Path
import ast
import unittest
from unittest.mock import patch

import hands_free_auto_lab.lab_coding_workflow as workflow


class LabCodingWorkflowTests(unittest.TestCase):
    def _arguments(self):
        return {
            "repository_path": "/synthetic/project",
            "commit_oid": "a" * 40,
            "expected_branch": "main",
            "relative_paths": ("project.py",),
            "workspace_parent": "/synthetic/workspaces",
            "session_id": "b" * 64,
            "goal": "Make one bounded synthetic change.",
            "worker": object(),
            "change_policy": object(),
            "candidate_store_root": "/synthetic/candidate-store",
            "max_seed_files": 16,
            "max_seed_bytes": 1024 * 1024,
            "max_runtime_seconds": 30,
            "max_log_bytes": 4096,
        }

    def test_composes_job_candidate_and_persistence_exactly_once(self):
        coding_job = object()
        candidate = object()
        arguments = self._arguments()
        stored = Path(
            "/synthetic/candidate-store/candidates/"
            + ("c" * 64)
            + ".json"
        )

        with (
            patch.object(
                workflow,
                "run_codex_coding_job_from_commit",
                return_value=coding_job,
            ) as run_job,
            patch.object(
                workflow,
                "build_lab_coding_candidate",
                return_value=candidate,
            ) as build_candidate,
            patch.object(
                workflow,
                "persist_lab_coding_candidate",
                return_value=stored,
            ) as persist_candidate,
        ):
            result = workflow.run_codex_coding_workflow_from_commit(
                **arguments
            )

        run_job.assert_called_once_with(
            repository_path="/synthetic/project",
            commit_oid="a" * 40,
            expected_branch="main",
            relative_paths=("project.py",),
            workspace_parent="/synthetic/workspaces",
            session_id="b" * 64,
            goal="Make one bounded synthetic change.",
            worker=arguments["worker"],
            change_policy=arguments["change_policy"],
            max_seed_files=16,
            max_seed_bytes=1024 * 1024,
            max_runtime_seconds=30,
            max_log_bytes=4096,
        )

        build_candidate.assert_called_once_with(
            coding_job,
            workspace_parent="/synthetic/workspaces",
        )

        persist_candidate.assert_called_once_with(
            "/synthetic/candidate-store",
            candidate,
        )

        self.assertIs(
            result.coding_job,
            coding_job,
        )
        self.assertIs(
            result.candidate,
            candidate,
        )
        self.assertEqual(
            result.candidate_record_path,
            str(stored),
        )

    def test_candidate_failure_stops_before_persistence(self):
        coding_job = object()

        with (
            patch.object(
                workflow,
                "run_codex_coding_job_from_commit",
                return_value=coding_job,
            ) as run_job,
            patch.object(
                workflow,
                "build_lab_coding_candidate",
                side_effect=RuntimeError("candidate refused"),
            ) as build_candidate,
            patch.object(
                workflow,
                "persist_lab_coding_candidate",
            ) as persist_candidate,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "candidate refused",
            ):
                workflow.run_codex_coding_workflow_from_commit(
                    **self._arguments()
                )

        self.assertEqual(
            run_job.call_count,
            1,
        )
        self.assertEqual(
            build_candidate.call_count,
            1,
        )
        persist_candidate.assert_not_called()

    def test_persistence_failure_is_not_retried(self):
        coding_job = object()
        candidate = object()

        with (
            patch.object(
                workflow,
                "run_codex_coding_job_from_commit",
                return_value=coding_job,
            ) as run_job,
            patch.object(
                workflow,
                "build_lab_coding_candidate",
                return_value=candidate,
            ) as build_candidate,
            patch.object(
                workflow,
                "persist_lab_coding_candidate",
                side_effect=RuntimeError("store refused"),
            ) as persist_candidate,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "store refused",
            ):
                workflow.run_codex_coding_workflow_from_commit(
                    **self._arguments()
                )

        self.assertEqual(
            run_job.call_count,
            1,
        )
        self.assertEqual(
            build_candidate.call_count,
            1,
        )
        self.assertEqual(
            persist_candidate.call_count,
            1,
        )

    def test_import_boundary_contains_no_promotion_or_execution_authority(self):
        source = Path(
            workflow.__file__
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
            "promotion",
            "lab_executor",
            "lab_write_file",
            "subprocess",
            "socket",
        )

        for imported in imported_modules:
            for forbidden in forbidden_fragments:
                self.assertNotIn(
                    forbidden,
                    imported,
                )


if __name__ == "__main__":
    unittest.main()
