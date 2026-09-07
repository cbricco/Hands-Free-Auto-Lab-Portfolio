from dataclasses import is_dataclass, replace
import ast
import hashlib
from pathlib import Path
import unittest

import hands_free_auto_lab.lab_promotion_source as promotion_source_module

from hands_free_auto_lab.lab_coding_candidate import (
    CANDIDATE_COMPONENT,
    CANDIDATE_SCHEMA_VERSION,
    LabCodingCandidate,
    LabCodingCandidateFile,
    _candidate_id,
)
from hands_free_auto_lab.lab_promotion_candidate import (
    PROMOTION_CANDIDATE_COMPONENT,
    PROMOTION_CANDIDATE_SCHEMA_VERSION,
    LabPromotionCandidate,
    LabPromotionCandidateFile,
)
from hands_free_auto_lab.lab_promotion_source import (
    LabPromotionSourceError,
    LabPromotionSourceFile,
    SOURCE_KIND_CODING_CANDIDATE,
    SOURCE_KIND_LEGACY_WRITE_ACTION,
    normalize_lab_promotion_source,
    validate_lab_promotion_source,
)


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class LabPromotionSourceTests(unittest.TestCase):
    def legacy(self):
        content = "legacy\n"
        return LabPromotionCandidate(
            component=PROMOTION_CANDIDATE_COMPONENT,
            schema_version=PROMOTION_CANDIDATE_SCHEMA_VERSION,
            session_id="a" * 64,
            workspace_device=1,
            workspace_inode=2,
            controller_status="done",
            workspace_generation=1,
            tested_generation=1,
            acceptance_generation=None,
            files=(LabPromotionCandidateFile(
                path="legacy.txt", bytes=len(content), sha256=digest(content),
                content=content, latest_write_action_id="b" * 64,
            ),),
        )

    def coding(self):
        content = "coding\n"
        item = LabCodingCandidateFile(
            operation="MODIFIED", path="coding.txt", before_bytes=4,
            before_sha256=digest("old\n"), before_mode=0o644,
            final_bytes=len(content), final_sha256=digest(content),
            final_mode=0o640, content=content,
        )
        prototype = LabCodingCandidate(
            component=CANDIDATE_COMPONENT, schema_version=CANDIDATE_SCHEMA_VERSION,
            candidate_id="0" * 64, workspace_session_id="c" * 64,
            workspace_device=3, workspace_inode=4,
            before_snapshot_id="d" * 64, after_snapshot_id="e" * 64,
            physical_diff_id="f" * 64, files=(item,),
        )
        return replace(prototype, candidate_id=_candidate_id(prototype))

    def test_legacy_uses_only_genuine_write_action_provenance(self):
        source = normalize_lab_promotion_source(self.legacy())
        item = source.files[0]
        self.assertEqual((item.source_kind, item.source_id),
                         (SOURCE_KIND_LEGACY_WRITE_ACTION, "b" * 64))
        self.assertEqual(item.operation, "WRITE_FILE")
        self.assertIsNone(item.before_sha256)
        self.assertIsNone(item.final_mode)

    def test_coding_preserves_exact_evidence_and_candidate_provenance(self):
        candidate = self.coding()
        item = normalize_lab_promotion_source(candidate).files[0]
        self.assertEqual((item.source_kind, item.source_id),
                         (SOURCE_KIND_CODING_CANDIDATE, candidate.candidate_id))
        self.assertEqual((item.operation, item.before_sha256, item.final_mode),
                         ("MODIFIED", digest("old\n"), 0o640))


    def test_added_requires_exact_0644_and_no_before_evidence(self):
        base = self.coding()
        added_item = replace(
            base.files[0], operation="ADDED", before_bytes=None,
            before_sha256=None, before_mode=None, final_mode=0o644,
        )
        prototype = replace(base, candidate_id="0" * 64, files=(added_item,))
        added = replace(prototype, candidate_id=_candidate_id(prototype))
        self.assertEqual(
            normalize_lab_promotion_source(added).files[0].final_mode,
            0o644,
        )
        for mode in (0o755, 0o4644):
            with self.subTest(mode=oct(mode)):
                item = replace(added_item, final_mode=mode)
                candidate = replace(base, candidate_id="0" * 64, files=(item,))
                candidate = replace(candidate, candidate_id=_candidate_id(candidate))
                with self.assertRaises(Exception):
                    normalize_lab_promotion_source(candidate)

    def test_added_coding_source_accepts_private_0600_final_mode(self):
        base = self.coding()
        item = replace(
            base.files[0], operation="ADDED", before_bytes=None,
            before_sha256=None, before_mode=None, final_mode=0o600,
        )
        prototype = replace(base, candidate_id="0" * 64, files=(item,))
        candidate = replace(prototype, candidate_id=_candidate_id(prototype))

        source = normalize_lab_promotion_source(candidate)
        self.assertEqual(source.files[0].final_mode, 0o600)
        self.assertEqual(source.files[0].operation, "ADDED")
        self.assertIsNone(source.files[0].before_mode)

    def test_representation_is_immutable_and_publicly_validated(self):
        source = normalize_lab_promotion_source(self.coding())
        self.assertTrue(is_dataclass(type(source)))
        self.assertTrue(type(source).__dataclass_params__.frozen)
        self.assertTrue(LabPromotionSourceFile.__dataclass_params__.frozen)
        self.assertIs(validate_lab_promotion_source(source), source)
        with self.assertRaises(LabPromotionSourceError):
            validate_lab_promotion_source(replace(
                source, files=(replace(source.files[0], final_sha256="a" * 64),)
            ))

    def test_coding_provenance_cannot_be_recast_as_legacy_write_file(self):
        candidate = self.coding()
        source = normalize_lab_promotion_source(candidate)
        item = source.files[0]
        tampered_items = (
            replace(item, source_kind=SOURCE_KIND_LEGACY_WRITE_ACTION),
            replace(item, operation="WRITE_FILE"),
            replace(
                item,
                source_kind=SOURCE_KIND_LEGACY_WRITE_ACTION,
                source_id=candidate.candidate_id,
                operation="WRITE_FILE",
            ),
        )

        for tampered in tampered_items:
            with self.subTest(
                source_kind=tampered.source_kind,
                source_id=tampered.source_id,
                operation=tampered.operation,
            ):
                with self.assertRaises(LabPromotionSourceError):
                    validate_lab_promotion_source(
                        replace(source, files=(tampered,))
                    )

    def test_normalization_module_has_only_evidence_capabilities(self):
        tree = ast.parse(
            Path(promotion_source_module.__file__).read_text(encoding="utf-8")
        )
        imported_modules = set()
        called_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_modules.add(node.module or "")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)

        self.assertEqual(
            imported_modules,
            {
                "__future__",
                "dataclasses",
                "hashlib",
                "pathlib",
                "lab_coding_candidate",
                "lab_promotion_candidate",
            },
        )
        forbidden_calls = {
            "Popen", "call", "check_call", "check_output", "run",
            "system", "popen", "open", "write", "replace", "rename",
            "remove", "unlink", "mkdir", "makedirs", "rmdir",
            "urlopen", "urlretrieve", "create_connection", "socket",
            "consume_promotion_approval", "execute_lab_promotion_transaction",
        }
        self.assertEqual(called_names & forbidden_calls, set())

    def test_invalid_candidates_fail_closed(self):
        with self.assertRaises(Exception):
            normalize_lab_promotion_source(replace(
                self.legacy(), files=(replace(
                    self.legacy().files[0], latest_write_action_id="c" * 63
                ),)
            ))
        with self.assertRaises(Exception):
            normalize_lab_promotion_source(replace(
                self.coding(), candidate_id="0" * 64
            ))
        with self.assertRaises(TypeError):
            normalize_lab_promotion_source(object())


if __name__ == "__main__":
    unittest.main()
