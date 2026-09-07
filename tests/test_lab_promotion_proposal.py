from __future__ import annotations

from dataclasses import replace
import hashlib
import unittest
from unittest.mock import patch

from hands_free_auto_lab.lab_coding_candidate import (
    CANDIDATE_COMPONENT,
    CANDIDATE_SCHEMA_VERSION,
    LabCodingCandidate,
    LabCodingCandidateFile,
    _candidate_id as coding_candidate_id,
)
from hands_free_auto_lab.lab_promotion_candidate import (
    PROMOTION_CANDIDATE_COMPONENT,
    PROMOTION_CANDIDATE_SCHEMA_VERSION,
    LabPromotionCandidate,
    LabPromotionCandidateFile,
)
from hands_free_auto_lab.lab_promotion_source import normalize_lab_promotion_source
from hands_free_auto_lab.lab_promotion_proposal import (
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
    PROMOTION_PROPOSAL_COMPONENT,
    PROMOTION_PROPOSAL_SCHEMA_VERSION,
    LabPromotionProposalError,
    LabPromotionRepositoryFileState,
    _PROPOSAL_ID_DOMAIN,
    _canonical_json_bytes,
    _proposal_identity_object,
    build_lab_coding_candidate_promotion_proposal,
    build_lab_promotion_proposal,
    lab_promotion_candidate_identity,
)


def _sha(
    content: str,
) -> str:
    return hashlib.sha256(
        content.encode(
            "utf-8"
        )
    ).hexdigest()


class LabPromotionProposalTests(
    unittest.TestCase
):
    def _candidate(
        self,
        *,
        reverse: bool = False,
    ) -> LabPromotionCandidate:
        files = [
            LabPromotionCandidateFile(
                path="alpha.txt",
                bytes=len(
                    "alpha\n".encode(
                        "utf-8"
                    )
                ),
                sha256=_sha(
                    "alpha\n"
                ),
                content="alpha\n",
                latest_write_action_id=(
                    "a" * 64
                ),
            ),
            LabPromotionCandidateFile(
                path="tests/test_alpha.py",
                bytes=len(
                    "test\n".encode(
                        "utf-8"
                    )
                ),
                sha256=_sha(
                    "test\n"
                ),
                content="test\n",
                latest_write_action_id=(
                    "b" * 64
                ),
            ),
        ]

        if reverse:
            files.reverse()

        return LabPromotionCandidate(
            component=(
                PROMOTION_CANDIDATE_COMPONENT
            ),
            schema_version=(
                PROMOTION_CANDIDATE_SCHEMA_VERSION
            ),
            session_id="d" * 64,
            workspace_device=10,
            workspace_inode=20,
            controller_status="done",
            workspace_generation=2,
            tested_generation=2,
            acceptance_generation=None,
            files=tuple(
                files
            ),
        )

    def _states(
        self,
        *,
        reverse: bool = False,
        alpha_sha: str | None = None,
    ):
        if alpha_sha is None:
            alpha_sha = _sha(
                "old alpha\n"
            )

        states = [
            LabPromotionRepositoryFileState(
                path="alpha.txt",
                exists=True,
                mode=0o644,
                bytes=len(
                    "old alpha\n".encode(
                        "utf-8"
                    )
                ),
                sha256=alpha_sha,
            ),
            LabPromotionRepositoryFileState(
                path="tests/test_alpha.py",
                exists=False,
                mode=None,
                bytes=None,
                sha256=None,
            ),
        ]

        if reverse:
            states.reverse()

        return tuple(
            states
        )

    def _proposal(
        self,
        **overrides,
    ):
        arguments = {
            "candidate": self._candidate(),
            "repository_path": (
                "/tmp/example-repo"
            ),
            "repository_device": 100,
            "repository_inode": 200,
            "branch": "main",
            "head": "1" * 40,
            "before_states": self._states(),
        }

        arguments.update(
            overrides
        )

        return build_lab_promotion_proposal(
            **arguments
        )

    def test_proposal_is_deterministic_and_derives_operations(self):
        first = self._proposal()

        second = self._proposal(
            candidate=self._candidate(
                reverse=True
            ),
            before_states=self._states(
                reverse=True
            ),
        )

        self.assertEqual(
            first.component,
            PROMOTION_PROPOSAL_COMPONENT,
        )

        self.assertEqual(
            first.schema_version,
            PROMOTION_PROPOSAL_SCHEMA_VERSION,
        )

        self.assertEqual(
            first.proposal_id,
            second.proposal_id,
        )

        self.assertEqual(
            first.candidate_id,
            second.candidate_id,
        )

        self.assertEqual(
            [
                item.path
                for item in first.files
            ],
            [
                "alpha.txt",
                "tests/test_alpha.py",
            ],
        )

        self.assertEqual(
            [
                item.operation
                for item in first.files
            ],
            [
                PROMOTION_OPERATION_MODIFY,
                PROMOTION_OPERATION_ADD,
            ],
        )

        self.assertEqual(
            len(
                first.proposal_id
            ),
            64,
        )

    def test_legacy_candidate_maps_exact_latest_write_action_provenance(self):
        candidate = self._candidate()
        proposal = self._proposal(
            candidate=candidate
        )

        expected = {
            item.path: item.latest_write_action_id
            for item in candidate.files
        }

        for item in proposal.files:
            with self.subTest(path=item.path):
                self.assertEqual(
                    item.source_kind,
                    "legacy_write_action",
                )
                self.assertEqual(
                    item.source_id,
                    expected[item.path],
                )

    def test_proposal_identity_binds_source_kind_and_source_id(self):
        proposal = self._proposal()

        def identity_for(files):
            identity = _proposal_identity_object(
                candidate_id=proposal.candidate_id,
                repository_path=proposal.repository_path,
                repository_device=proposal.repository_device,
                repository_inode=proposal.repository_inode,
                branch=proposal.branch,
                head=proposal.head,
                files=files,
            )
            return hashlib.sha256(
                _PROPOSAL_ID_DOMAIN
                + _canonical_json_bytes(identity)
            ).hexdigest()

        changed_kind = (
            replace(
                proposal.files[0],
                source_kind="coding_candidate",
            ),
            proposal.files[1],
        )
        changed_id = (
            replace(
                proposal.files[0],
                source_id="e" * 64,
            ),
            proposal.files[1],
        )

        self.assertNotEqual(
            proposal.proposal_id,
            identity_for(changed_kind),
        )
        self.assertNotEqual(
            proposal.proposal_id,
            identity_for(changed_id),
        )

    def test_candidate_identity_changes_with_exact_candidate(self):
        original = self._candidate()

        changed_file = replace(
            original.files[0],
            bytes=len(
                "changed\n".encode(
                    "utf-8"
                )
            ),
            sha256=_sha(
                "changed\n"
            ),
            content="changed\n",
        )

        changed = replace(
            original,
            files=(
                changed_file,
                original.files[1],
            ),
        )

        self.assertNotEqual(
            lab_promotion_candidate_identity(
                original
            ),
            lab_promotion_candidate_identity(
                changed
            ),
        )

        original_proposal = self._proposal(
            candidate=original
        )

        changed_proposal = self._proposal(
            candidate=changed
        )

        self.assertNotEqual(
            original_proposal.proposal_id,
            changed_proposal.proposal_id,
        )

    def test_repository_context_changes_proposal_identity(self):
        baseline = self._proposal()

        variations = (
            {
                "repository_path": (
                    "/tmp/other-repo"
                ),
            },
            {
                "repository_device": 101,
            },
            {
                "repository_inode": 201,
            },
            {
                "branch": "dev",
            },
            {
                "head": "2" * 40,
            },
        )

        for override in variations:
            with self.subTest(
                override=override
            ):
                changed = self._proposal(
                    **override
                )

                self.assertNotEqual(
                    baseline.proposal_id,
                    changed.proposal_id,
                )

    def test_before_state_changes_proposal_identity(self):
        baseline = self._proposal()

        changed = self._proposal(
            before_states=self._states(
                alpha_sha=_sha(
                    "different old alpha\n"
                )
            ),
        )

        self.assertNotEqual(
            baseline.proposal_id,
            changed.proposal_id,
        )

    def test_missing_before_state_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionProposalError,
            "exactly match",
        ):
            self._proposal(
                before_states=(
                    self._states()[0],
                ),
            )

    def test_extra_before_state_is_refused(self):
        extra = (
            *self._states(),
            LabPromotionRepositoryFileState(
                path="extra.txt",
                exists=False,
                mode=None,
                bytes=None,
                sha256=None,
            ),
        )

        with self.assertRaisesRegex(
            LabPromotionProposalError,
            "exactly match",
        ):
            self._proposal(
                before_states=extra
            )

    def test_duplicate_before_state_is_refused(self):
        duplicate = (
            self._states()[0],
            self._states()[0],
            self._states()[1],
        )

        with self.assertRaisesRegex(
            LabPromotionProposalError,
            "duplicate before-state",
        ):
            self._proposal(
                before_states=duplicate
            )

    def test_absent_before_state_with_metadata_is_refused(self):
        states = (
            self._states()[0],
            LabPromotionRepositoryFileState(
                path="tests/test_alpha.py",
                exists=False,
                mode=None,
                bytes=0,
                sha256=_sha(
                    ""
                ),
            ),
        )

        with self.assertRaisesRegex(
            LabPromotionProposalError,
            "absent before-state",
        ):
            self._proposal(
                before_states=states
            )

    def test_tampered_candidate_file_is_refused(self):
        candidate = self._candidate()

        tampered = replace(
            candidate,
            files=(
                replace(
                    candidate.files[0],
                    content="tampered\n",
                ),
                candidate.files[1],
            ),
        )

        with self.assertRaisesRegex(
            LabPromotionProposalError,
            "candidate file",
        ):
            self._proposal(
                candidate=tampered
            )

    def test_noncanonical_repository_or_invalid_head_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionProposalError,
            "normalized",
        ):
            self._proposal(
                repository_path=(
                    "/tmp/example-repo/../example-repo"
                )
            )

        with self.assertRaisesRegex(
            LabPromotionProposalError,
            "Git object ID",
        ):
            self._proposal(
                head="not-a-head"
            )


    def _coding_candidate(
        self,
        *,
        final_mode: int = 0o640,
    ) -> LabCodingCandidate:
        content = "coding final\n"
        item = LabCodingCandidateFile(
            operation="MODIFIED",
            path="alpha.txt",
            before_bytes=len("old alpha\n".encode("utf-8")),
            before_sha256=_sha("old alpha\n"),
            before_mode=0o644,
            final_bytes=len(content.encode("utf-8")),
            final_sha256=_sha(content),
            final_mode=final_mode,
            content=content,
        )
        prototype = LabCodingCandidate(
            component=CANDIDATE_COMPONENT,
            schema_version=CANDIDATE_SCHEMA_VERSION,
            candidate_id="0" * 64,
            workspace_session_id="a" * 64,
            workspace_device=10,
            workspace_inode=20,
            before_snapshot_id="b" * 64,
            after_snapshot_id="c" * 64,
            physical_diff_id="d" * 64,
            files=(item,),
        )
        return replace(
            prototype,
            candidate_id=coding_candidate_id(prototype),
        )

    def _coding_proposal(self, candidate=None, before_states=None):
        if candidate is None:
            candidate = self._coding_candidate()
        if before_states is None:
            before_states = (self._states()[0],)
        return build_lab_coding_candidate_promotion_proposal(
            candidate=candidate,
            repository_path="/tmp/example-repo",
            repository_device=100,
            repository_inode=200,
            branch="main",
            head="1" * 40,
            before_states=before_states,
        )

    def test_coding_candidate_proposal_uses_genuine_provenance_and_mode(self):
        candidate = self._coding_candidate()
        proposal = self._coding_proposal(candidate)
        item = proposal.files[0]
        self.assertEqual(proposal.candidate_id, candidate.candidate_id)
        self.assertEqual(item.source_kind, "coding_candidate")
        self.assertEqual(item.source_id, candidate.candidate_id)
        self.assertEqual(item.operation, PROMOTION_OPERATION_MODIFY)
        self.assertEqual(item.after_mode, 0o644)
        self.assertEqual(item.before_mode, 0o644)
        self.assertEqual(item.before_sha256, _sha("old alpha\n"))

    def test_added_private_0600_mode_maps_to_destination_0644(self):
        content = "new coding file\n"
        item = LabCodingCandidateFile(
            operation="ADDED", path="new.txt", before_bytes=None,
            before_sha256=None, before_mode=None,
            final_bytes=len(content.encode("utf-8")), final_sha256=_sha(content),
            final_mode=0o600, content=content,
        )
        prototype = replace(
            self._coding_candidate(), candidate_id="0" * 64, files=(item,),
        )
        candidate = replace(
            prototype, candidate_id=coding_candidate_id(prototype),
        )
        absent = LabPromotionRepositoryFileState(
            path="new.txt", exists=False, bytes=None, sha256=None, mode=None,
        )

        proposal = self._coding_proposal(candidate, before_states=(absent,))
        proposed = proposal.files[0]
        self.assertEqual(proposed.operation, PROMOTION_OPERATION_ADD)
        self.assertIsNone(proposed.before_mode)
        self.assertEqual(proposed.after_mode, 0o644)

    def test_coding_candidate_final_mode_changes_bound_proposal_identity(self):
        baseline = self._coding_proposal(self._coding_candidate(final_mode=0o640))
        changed = self._coding_proposal(self._coding_candidate(final_mode=0o600))
        self.assertNotEqual(baseline.candidate_id, changed.candidate_id)
        self.assertNotEqual(baseline.proposal_id, changed.proposal_id)


    def test_coding_candidate_worker_0600_mode_is_not_promoted(self):
        proposal = self._coding_proposal(
            self._coding_candidate(final_mode=0o600)
        )
        self.assertEqual(proposal.files[0].before_mode, 0o644)
        self.assertEqual(proposal.files[0].after_mode, 0o644)

    def test_coding_candidate_before_bytes_and_mode_mismatches_are_refused(self):
        for field, value in (("bytes", 999), ("mode", 0o600)):
            with self.subTest(field=field):
                mismatched = replace(self._states()[0], **{field: value})
                with self.assertRaisesRegex(
                    LabPromotionProposalError,
                    "before evidence does not match",
                ):
                    self._coding_proposal(before_states=(mismatched,))

    def test_coding_candidate_executable_and_special_modes_are_refused(self):
        for mode in (0o755, 0o4755):
            with self.subTest(mode=oct(mode)):
                with self.assertRaises(Exception):
                    self._coding_proposal(self._coding_candidate(final_mode=mode))

    def test_invalid_coding_candidate_is_refused(self):
        candidate = replace(
            self._coding_candidate(),
            candidate_id="0" * 64,
        )
        with self.assertRaises(Exception):
            self._coding_proposal(candidate)

    def test_coding_candidate_preimage_mismatch_is_refused(self):
        mismatched = replace(
            self._states()[0],
            sha256=_sha("different old content\n"),
        )
        with self.assertRaisesRegex(
            LabPromotionProposalError,
            "before evidence does not match",
        ):
            self._coding_proposal(before_states=(mismatched,))

    def test_coding_candidate_operation_mismatch_is_refused(self):
        absent = LabPromotionRepositoryFileState(
            path="alpha.txt",
            exists=False,
            bytes=None,
            sha256=None,
            mode=None,
        )
        with self.assertRaisesRegex(
            LabPromotionProposalError,
            "operation does not match",
        ):
            self._coding_proposal(before_states=(absent,))



    def test_coding_candidate_wrong_provenance_kind_or_id_is_refused(self):
        candidate = self._coding_candidate()
        source = normalize_lab_promotion_source(candidate)
        bad_files = (
            replace(
                source.files[0],
                source_kind="legacy_write_action",
            ),
            replace(
                source.files[0],
                source_id="e" * 64,
            ),
        )

        for bad_file in bad_files:
            with self.subTest(
                source_kind=bad_file.source_kind,
                source_id=bad_file.source_id,
            ):
                with patch(
                    "hands_free_auto_lab.lab_promotion_proposal.normalize_lab_promotion_source",
                    return_value=replace(source, files=(bad_file,)),
                ):
                    with self.assertRaisesRegex(
                        LabPromotionProposalError,
                        "coding candidate provenance mismatch",
                    ):
                        self._coding_proposal(candidate)
if __name__ == "__main__":
    unittest.main()
