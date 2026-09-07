from dataclasses import replace
import ast
from pathlib import Path
import tempfile
import unittest

from hands_free_auto_lab.lab_worker_change_policy import (
    LabWorkerChangePolicyError,
    LabWorkerChangeTarget,
    OPERATION_ADD,
    OPERATION_MODIFY,
    build_lab_worker_change_policy,
    validate_lab_worker_change_policy,
    validate_lab_worker_change_policy_before,
    validate_lab_worker_change_policy_diff,
)
from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)
from hands_free_auto_lab.lab_workspace_snapshot import (
    capture_lab_workspace_snapshot,
    diff_lab_workspace_snapshots,
)


class LabWorkerChangePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(
            self.temp.name
        )
        self.root.chmod(
            0o700
        )

        self.workspace = create_lab_workspace(
            str(
                self.root
            ),
            session_id="a" * 64,
        )

    def tearDown(self):
        self.temp.cleanup()

    @property
    def workspace_path(self):
        return Path(
            self.workspace.path
        )

    def snapshot(self):
        return capture_lab_workspace_snapshot(
            workspace_parent=str(
                self.root
            ),
            workspace=self.workspace,
        )

    def add_target(
        self,
        *,
        path="new.txt",
        maximum=64,
        mode=0o600,
    ):
        return LabWorkerChangeTarget(
            operation=OPERATION_ADD,
            path=path,
            max_final_bytes=maximum,
            final_mode=mode,
            before_bytes=None,
            before_sha256=None,
            before_mode=None,
        )

    def modify_target(
        self,
        before,
        *,
        path="existing.txt",
        maximum=64,
        mode=0o600,
    ):
        entry = {
            item.path: item
            for item in before.entries
        }[path]

        return LabWorkerChangeTarget(
            operation=OPERATION_MODIFY,
            path=path,
            max_final_bytes=maximum,
            final_mode=mode,
            before_bytes=entry.bytes,
            before_sha256=entry.sha256,
            before_mode=entry.mode,
        )

    def test_policy_identity_is_deterministic_and_authority_relevant(self):
        first = build_lab_worker_change_policy(
            targets=(
                self.add_target(
                    path="b.txt",
                ),
                self.add_target(
                    path="a.txt",
                ),
            )
        )

        second = build_lab_worker_change_policy(
            targets=(
                self.add_target(
                    path="a.txt",
                ),
                self.add_target(
                    path="b.txt",
                ),
            )
        )

        changed = build_lab_worker_change_policy(
            targets=(
                self.add_target(
                    path="a.txt",
                    maximum=65,
                ),
                self.add_target(
                    path="b.txt",
                ),
            )
        )

        self.assertEqual(
            first,
            second,
        )

        self.assertNotEqual(
            first.policy_id,
            changed.policy_id,
        )

        self.assertEqual(
            tuple(
                target.path
                for target in first.targets
            ),
            (
                "a.txt",
                "b.txt",
            ),
        )

    def test_add_policy_accepts_exact_physical_change(self):
        before = self.snapshot()

        policy = build_lab_worker_change_policy(
            targets=(
                self.add_target(),
            )
        )

        validate_lab_worker_change_policy_before(
            policy,
            before_snapshot=before,
        )

        target = (
            self.workspace_path
            / "new.txt"
        )

        target.write_bytes(
            b"safe\n"
        )
        target.chmod(
            0o600
        )

        after = self.snapshot()

        diff = diff_lab_workspace_snapshots(
            before,
            after,
        )

        self.assertIs(
            validate_lab_worker_change_policy_diff(
                policy,
                before_snapshot=before,
                after_snapshot=after,
                physical_diff=diff,
                require_complete=True,
            ),
            policy,
        )

    def test_add_target_must_be_absent_before_worker(self):
        target = (
            self.workspace_path
            / "new.txt"
        )

        target.write_bytes(
            b"already here\n"
        )
        target.chmod(
            0o600
        )

        before = self.snapshot()

        policy = build_lab_worker_change_policy(
            targets=(
                self.add_target(),
            )
        )

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "already exists",
        ):
            validate_lab_worker_change_policy_before(
                policy,
                before_snapshot=before,
            )

    def test_modify_policy_binds_exact_before_provenance(self):
        target = (
            self.workspace_path
            / "existing.txt"
        )

        target.write_bytes(
            b"before\n"
        )
        target.chmod(
            0o600
        )

        before = self.snapshot()

        policy = build_lab_worker_change_policy(
            targets=(
                self.modify_target(
                    before
                ),
            )
        )

        target.write_bytes(
            b"after\n"
        )
        target.chmod(
            0o600
        )

        after = self.snapshot()
        diff = diff_lab_workspace_snapshots(
            before,
            after,
        )

        validate_lab_worker_change_policy_diff(
            policy,
            before_snapshot=before,
            after_snapshot=after,
            physical_diff=diff,
            require_complete=True,
        )

        bad = replace(
            policy.targets[0],
            before_sha256="0" * 64,
        )

        bad_policy = build_lab_worker_change_policy(
            targets=(
                bad,
            )
        )

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "before-state provenance mismatch",
        ):
            validate_lab_worker_change_policy_before(
                bad_policy,
                before_snapshot=before,
            )

    def test_extra_physical_path_is_refused(self):
        before = self.snapshot()

        policy = build_lab_worker_change_policy(
            targets=(
                self.add_target(),
            )
        )

        for name in (
            "new.txt",
            "escape.txt",
        ):
            path = (
                self.workspace_path
                / name
            )
            path.write_bytes(
                b"x\n"
            )
            path.chmod(
                0o600
            )

        after = self.snapshot()
        diff = diff_lab_workspace_snapshots(
            before,
            after,
        )

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "outside policy",
        ):
            validate_lab_worker_change_policy_diff(
                policy,
                before_snapshot=before,
                after_snapshot=after,
                physical_diff=diff,
                require_complete=True,
            )

    def test_byte_ceiling_and_mode_are_enforced(self):
        before = self.snapshot()

        policy = build_lab_worker_change_policy(
            targets=(
                self.add_target(
                    maximum=4,
                    mode=0o600,
                ),
            )
        )

        path = (
            self.workspace_path
            / "new.txt"
        )

        path.write_bytes(
            b"12345"
        )
        path.chmod(
            0o600
        )

        after = self.snapshot()

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "byte count exceeds",
        ):
            validate_lab_worker_change_policy_diff(
                policy,
                before_snapshot=before,
                after_snapshot=after,
                physical_diff=diff_lab_workspace_snapshots(
                    before,
                    after,
                ),
                require_complete=True,
            )

        path.write_bytes(
            b"123"
        )
        path.chmod(
            0o640
        )

        after = self.snapshot()

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "final mode differs",
        ):
            validate_lab_worker_change_policy_diff(
                policy,
                before_snapshot=before,
                after_snapshot=after,
                physical_diff=diff_lab_workspace_snapshots(
                    before,
                    after,
                ),
                require_complete=True,
            )

    def test_deletion_and_operation_substitution_are_refused(self):
        path = (
            self.workspace_path
            / "existing.txt"
        )

        path.write_bytes(
            b"before\n"
        )
        path.chmod(
            0o600
        )

        before = self.snapshot()

        policy = build_lab_worker_change_policy(
            targets=(
                self.modify_target(
                    before
                ),
            )
        )

        path.unlink()

        after = self.snapshot()

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "deletion is forbidden",
        ):
            validate_lab_worker_change_policy_diff(
                policy,
                before_snapshot=before,
                after_snapshot=after,
                physical_diff=diff_lab_workspace_snapshots(
                    before,
                    after,
                ),
                require_complete=True,
            )

    def test_failed_or_timed_out_run_may_preserve_safe_partial_diff(self):
        before = self.snapshot()

        policy = build_lab_worker_change_policy(
            targets=(
                self.add_target(
                    path="a.txt",
                ),
                self.add_target(
                    path="b.txt",
                ),
            )
        )

        path = (
            self.workspace_path
            / "a.txt"
        )

        path.write_bytes(
            b"partial\n"
        )
        path.chmod(
            0o600
        )

        after = self.snapshot()
        diff = diff_lab_workspace_snapshots(
            before,
            after,
        )

        validate_lab_worker_change_policy_diff(
            policy,
            before_snapshot=before,
            after_snapshot=after,
            physical_diff=diff,
            require_complete=False,
        )

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "does not exactly match",
        ):
            validate_lab_worker_change_policy_diff(
                policy,
                before_snapshot=before,
                after_snapshot=after,
                physical_diff=diff,
                require_complete=True,
            )

    def test_malformed_targets_and_policy_tampering_fail_closed(self):
        malformed = (
            replace(
                self.add_target(),
                path="../escape",
            ),
            replace(
                self.add_target(),
                path="/absolute",
            ),
            replace(
                self.add_target(),
                path=".git/config",
            ),
            replace(
                self.add_target(),
                operation="DELETED",
            ),
            replace(
                self.add_target(),
                max_final_bytes=True,
            ),
            replace(
                self.add_target(),
                final_mode=0o700,
            ),
            replace(
                self.add_target(),
                final_mode=0o620,
            ),
            replace(
                self.add_target(),
                before_bytes=0,
            ),
            LabWorkerChangeTarget(
                operation=OPERATION_MODIFY,
                path="existing.txt",
                max_final_bytes=64,
                final_mode=0o600,
                before_bytes=None,
                before_sha256=None,
                before_mode=None,
            ),
        )

        for target in malformed:
            with self.subTest(
                target=target
            ):
                with self.assertRaises(
                    LabWorkerChangePolicyError
                ):
                    build_lab_worker_change_policy(
                        targets=(
                            target,
                        )
                    )

        policy = build_lab_worker_change_policy(
            targets=(
                self.add_target(),
            )
        )

        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "identity mismatch",
        ):
            validate_lab_worker_change_policy(
                replace(
                    policy,
                    policy_id="0" * 64,
                )
            )

    def test_duplicate_paths_fail_closed(self):
        with self.assertRaisesRegex(
            LabWorkerChangePolicyError,
            "duplicate",
        ):
            build_lab_worker_change_policy(
                targets=(
                    self.add_target(),
                    self.add_target(),
                )
            )

    def test_module_has_no_execution_network_git_or_authority_imports(self):
        module_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hands_free_auto_lab"
            / "lab_worker_change_policy.py"
        )

        tree = ast.parse(
            module_path.read_text(
                encoding="utf-8"
            ),
            filename=str(
                module_path
            ),
        )

        forbidden = {
            "subprocess",
            "socket",
            "requests",
            "urllib",
            "http",
            "ftplib",
            "paramiko",
            "os",
            "shutil",
        }

        forbidden_local = {
            "lab_promotion",
            "lab_promotion_approval_store",
            "lab_promotion_target_preparer",
            "lab_write_file",
            "lab_executor",
            "lab_codex_worker",
            "lab_aider_worker",
        }

        for node in ast.walk(
            tree
        ):
            if isinstance(
                node,
                ast.Import,
            ):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name.split(
                            ".",
                            1,
                        )[0],
                        forbidden,
                    )

            if isinstance(
                node,
                ast.ImportFrom,
            ):
                if node.module:
                    root = node.module.lstrip(
                        "."
                    ).split(
                        ".",
                        1,
                    )[0]

                    self.assertNotIn(
                        root,
                        forbidden,
                    )

                    self.assertNotIn(
                        root,
                        forbidden_local,
                    )


if __name__ == "__main__":
    unittest.main()
