from __future__ import annotations

import ast
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "hands_free_auto_lab"


def _tree(module: str) -> ast.Module:
    path = PACKAGE_ROOT / f"{module}.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _local_imports(module: str) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                prefix = "hands_free_auto_lab."
                if alias.name.startswith(prefix):
                    imports.add(alias.name[len(prefix):].split(".", 1)[0])
    return imports


def _call_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return f"{function.value.id}.{function.attr}"
    return None


class LabCodingCandidateBoundaryAuditTests(unittest.TestCase):
    def test_model_and_worker_layers_cannot_import_promotion_authority(self):
        model_facing_modules = (
            "lab_model_adapter",
            "lab_worker",
            "lab_worker_job",
            "lab_coding_job",
            "lab_codex_worker",
            "lab_aider_worker",
        )
        forbidden = {
            "lab_promotion_approval_state",
            "lab_promotion_approval_store",
            "lab_promotion_applied_target",
            "lab_promotion_prepared_target",
            "lab_promotion_target_preparer",
            "lab_promotion_transaction_journal",
            "lab_promotion_recovery_materials",
            "lab_promotion_rollback_plan",
            "lab_promotion_executor",
            "lab_promotion_rollback_executor",
        }

        for module in model_facing_modules:
            with self.subTest(module=module):
                self.assertEqual(_local_imports(module) & forbidden, set())

    def test_candidate_source_and_proposal_remain_evidence_only(self):
        evidence_modules = (
            "lab_coding_candidate",
            "lab_coding_candidate_store",
            "lab_promotion_source",
            "lab_promotion_proposal",
        )
        authority_modules = {
            "lab_promotion_approval_state",
            "lab_promotion_approval_store",
            "lab_promotion_applied_target",
            "lab_promotion_prepared_target",
            "lab_promotion_target_preparer",
            "lab_promotion_transaction_state",
            "lab_promotion_transaction_journal",
            "lab_promotion_recovery_materials",
            "lab_promotion_rollback_plan",
            "lab_promotion_executor",
            "lab_promotion_rollback_executor",
        }

        for module in evidence_modules:
            with self.subTest(module=module):
                self.assertEqual(_local_imports(module) & authority_modules, set())

    def test_repository_inspector_has_no_mutation_primitives(self):
        forbidden_calls = {
            "open",
            "os.chmod",
            "os.chown",
            "os.link",
            "os.mkdir",
            "os.makedirs",
            "os.remove",
            "os.rename",
            "os.replace",
            "os.rmdir",
            "os.symlink",
            "os.truncate",
            "os.unlink",
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.move",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "subprocess.Popen",
        }
        observed = {
            name
            for node in ast.walk(_tree("lab_promotion_inspector"))
            if isinstance(node, ast.Call)
            for name in (_call_name(node),)
            if name in forbidden_calls
        }

        self.assertEqual(observed, set())
        self.assertNotIn("lab_promotion_approval_store", _local_imports("lab_promotion_inspector"))
        self.assertNotIn("lab_promotion_target_preparer", _local_imports("lab_promotion_inspector"))
        self.assertNotIn("lab_promotion_applied_target", _local_imports("lab_promotion_inspector"))

    def test_repository_inspector_git_calls_are_fixed_read_only_families(self):
        allowed_commands = {
            "check-ignore",
            "ls-files",
            "rev-parse",
            "status",
            "symbolic-ref",
        }
        observed_commands = set()

        for node in ast.walk(_tree("lab_promotion_inspector")):
            if not isinstance(node, ast.Call) or _call_name(node) != "_run_git":
                continue
            self.assertGreaterEqual(len(node.args), 2)
            command = node.args[1]
            self.assertIsInstance(command, ast.Constant)
            self.assertIsInstance(command.value, str)
            observed_commands.add(command.value)

        self.assertEqual(observed_commands, allowed_commands)

    def test_approval_transaction_and_recovery_controls_stay_separate(self):
        approval_modules = (
            "lab_promotion_approval_state",
            "lab_promotion_approval_store",
        )
        repository_execution_modules = {
            "lab_promotion_applied_target",
            "lab_promotion_prepared_target",
            "lab_promotion_target_preparer",
            "lab_promotion_transaction_journal",
            "lab_promotion_recovery_materials",
            "lab_promotion_rollback_plan",
            "lab_promotion_executor",
            "lab_promotion_rollback_executor",
        }
        for module in approval_modules:
            with self.subTest(boundary="approval", module=module):
                self.assertEqual(
                    _local_imports(module) & repository_execution_modules,
                    set(),
                )

        separately_controlled = {
            "lab_promotion_transaction_state": {
                "lab_promotion_approval_state",
                "lab_promotion_approval_store",
                "lab_promotion_recovery_materials",
                "lab_promotion_executor",
                "lab_promotion_rollback_executor",
            },
            "lab_promotion_recovery_materials": {
                "lab_promotion_approval_state",
                "lab_promotion_approval_store",
                "lab_promotion_applied_target",
                "lab_promotion_target_preparer",
                "lab_promotion_executor",
                "lab_promotion_rollback_executor",
            },
            "lab_promotion_transaction_journal": {
                "lab_promotion_approval_state",
                "lab_promotion_approval_store",
                "lab_promotion_applied_target",
                "lab_promotion_target_preparer",
                "lab_promotion_executor",
                "lab_promotion_rollback_executor",
            },
        }
        for module, forbidden in separately_controlled.items():
            with self.subTest(boundary="transaction-recovery", module=module):
                self.assertEqual(_local_imports(module) & forbidden, set())


if __name__ == "__main__":
    unittest.main()
