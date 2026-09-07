from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from hands_free_auto_lab.lab_coding_candidate import (
    CANDIDATE_COMPONENT,
    CANDIDATE_SCHEMA_VERSION,
    LabCodingCandidate,
    LabCodingCandidateFile,
    _candidate_id,
    validate_lab_coding_candidate,
)
from hands_free_auto_lab.lab_promotion_approval_store import (
    APPROVALS_DIRECTORY,
    CHALLENGES_DIRECTORY,
    DECISION_FILENAME,
    LabPromotionApprovalStoreError,
    consume_promotion_approval,
    create_promotion_challenge,
    decide_promotion_challenge,
    initialize_promotion_approval_state_root,
    query_promotion_approval,
)
from hands_free_auto_lab.lab_promotion_proposal import (
    LabPromotionRepositoryFileState,
    build_lab_coding_candidate_promotion_proposal,
)
from hands_free_auto_lab.lab_promotion_source import (
    SOURCE_KIND_CODING_CANDIDATE,
    normalize_lab_promotion_source,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_PENDING,
    RISK_DESTRUCTIVE_HIGH,
    STATE_PREPARED,
    build_lab_promotion_transaction,
)


NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
RUN_ID = "9" * 64


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LabCodingCandidateAuthorityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="auto-lab-candidate-authority-"))
        os.chmod(self.root, 0o700)
        self.repository = self.root / "target"
        self.repository.mkdir(mode=0o700)
        self.approval_root = self.root / "approval-state"
        self.approval_root.mkdir(mode=0o700)
        initialize_promotion_approval_state_root(self.approval_root)

    def tearDown(self):
        shutil.rmtree(self.root)

    def _evidence_chain(self):
        content = "candidate evidence only\n"
        prototype = LabCodingCandidate(
            component=CANDIDATE_COMPONENT,
            schema_version=CANDIDATE_SCHEMA_VERSION,
            candidate_id="0" * 64,
            workspace_session_id="1" * 64,
            workspace_device=10,
            workspace_inode=20,
            before_snapshot_id="2" * 64,
            after_snapshot_id="3" * 64,
            physical_diff_id="4" * 64,
            files=(
                LabCodingCandidateFile(
                    operation="ADDED",
                    path="authority.txt",
                    before_bytes=None,
                    before_sha256=None,
                    before_mode=None,
                    final_bytes=len(content.encode("utf-8")),
                    final_sha256=_sha(content),
                    final_mode=0o644,
                    content=content,
                ),
            ),
        )
        candidate = validate_lab_coding_candidate(
            replace(prototype, candidate_id=_candidate_id(prototype))
        )
        source = normalize_lab_promotion_source(candidate)
        repository_stat = self.repository.stat()
        proposal = build_lab_coding_candidate_promotion_proposal(
            candidate=candidate,
            repository_path=str(self.repository.resolve()),
            repository_device=repository_stat.st_dev,
            repository_inode=repository_stat.st_ino,
            branch="main",
            head="5" * 40,
            before_states=(
                LabPromotionRepositoryFileState(
                    path="authority.txt",
                    exists=False,
                    bytes=None,
                    sha256=None,
                    mode=None,
                ),
            ),
        )
        transaction = build_lab_promotion_transaction(
            proposal=proposal,
            run_id=RUN_ID,
        )
        return candidate, source, proposal, transaction

    def _approval_entries(self):
        return tuple(sorted(path.name for path in (
            self.approval_root / APPROVALS_DIRECTORY
        ).iterdir()))

    def test_complete_candidate_chain_is_evidence_only_and_has_no_side_effects(self):
        candidate, source, proposal, transaction = self._evidence_chain()

        self.assertEqual(source.files[0].source_kind, SOURCE_KIND_CODING_CANDIDATE)
        self.assertEqual(source.files[0].source_id, candidate.candidate_id)
        self.assertEqual(proposal.candidate_id, candidate.candidate_id)
        self.assertEqual(transaction.proposal_id, proposal.proposal_id)
        self.assertEqual(transaction.state, STATE_PREPARED)
        self.assertEqual(transaction.risk_classification, RISK_DESTRUCTIVE_HIGH)
        self.assertTrue(all(item.progress == PROGRESS_PENDING for item in transaction.files))
        self.assertEqual(self._approval_entries(), ())
        self.assertEqual(tuple((self.approval_root / CHALLENGES_DIRECTORY).iterdir()), ())
        self.assertFalse((self.repository / "authority.txt").exists())

        for evidence in (candidate, source, proposal, transaction):
            for attribute in (
                "approval_id", "approved", "authorization", "authorized",
                "challenge_id", "execution_authority", "run_authority",
            ):
                with self.subTest(type=type(evidence).__name__, attribute=attribute):
                    self.assertFalse(hasattr(evidence, attribute))

    def test_candidate_identifiers_cannot_decide_or_consume_pending_challenge(self):
        candidate, _source, proposal, transaction = self._evidence_chain()
        with patch(
            "hands_free_auto_lab.lab_promotion_approval_store._utcnow",
            return_value=NOW,
        ):
            challenge = create_promotion_challenge(
                self.approval_root,
                proposal_id=proposal.proposal_id,
            )

            with self.assertRaises(LabPromotionApprovalStoreError):
                decide_promotion_challenge(
                    self.approval_root,
                    challenge_id=candidate.candidate_id,
                    proposal_id=proposal.proposal_id,
                    decision="approve",
                )

            with self.assertRaises(LabPromotionApprovalStoreError):
                consume_promotion_approval(
                    self.approval_root,
                    approval_id=candidate.candidate_id,
                    proposal_id=proposal.proposal_id,
                    run_id=transaction.run_id,
                )

        challenge_directory = (
            self.approval_root / CHALLENGES_DIRECTORY / challenge["challenge_id"]
        )
        self.assertFalse((challenge_directory / DECISION_FILENAME).exists())
        self.assertEqual(self._approval_entries(), ())
        self.assertFalse((self.repository / "authority.txt").exists())

    def test_repeated_candidate_evidence_never_recreates_consumed_authority(self):
        candidate, source, proposal, transaction = self._evidence_chain()
        repeated_candidate, repeated_source, repeated_proposal, repeated_transaction = (
            self._evidence_chain()
        )

        self.assertEqual(repeated_candidate, candidate)
        self.assertEqual(repeated_source, source)
        self.assertEqual(repeated_proposal.proposal_id, proposal.proposal_id)
        self.assertEqual(repeated_transaction, transaction)
        self.assertEqual(self._approval_entries(), ())
        self.assertEqual(tuple((self.approval_root / CHALLENGES_DIRECTORY).iterdir()), ())

        with patch(
            "hands_free_auto_lab.lab_promotion_approval_store._utcnow",
            return_value=NOW,
        ):
            challenge = create_promotion_challenge(
                self.approval_root,
                proposal_id=proposal.proposal_id,
            )
            approval = decide_promotion_challenge(
                self.approval_root,
                challenge_id=challenge["challenge_id"],
                proposal_id=proposal.proposal_id,
                decision="approve",
            )["approval"]

            with self.assertRaisesRegex(
                LabPromotionApprovalStoreError,
                "proposal binding",
            ):
                consume_promotion_approval(
                    self.approval_root,
                    approval_id=approval["approval_id"],
                    proposal_id="a" * 64,
                    run_id=transaction.run_id,
                )

        with patch(
            "hands_free_auto_lab.lab_promotion_approval_store._utcnow",
            return_value=NOW + timedelta(seconds=1),
        ):
            consumed = consume_promotion_approval(
                self.approval_root,
                approval_id=approval["approval_id"],
                proposal_id=repeated_proposal.proposal_id,
                run_id=repeated_transaction.run_id,
            )

        self.assertEqual(consumed["status"], "consumed")
        self.assertEqual(consumed["terminal"]["run_id"], transaction.run_id)

        replayed_candidate, _source, replayed_proposal, replayed_transaction = (
            self._evidence_chain()
        )
        self.assertEqual(replayed_candidate.candidate_id, candidate.candidate_id)
        self.assertEqual(replayed_proposal.proposal_id, proposal.proposal_id)

        initialize_promotion_approval_state_root(self.approval_root)
        with patch(
            "hands_free_auto_lab.lab_promotion_approval_store._utcnow",
            return_value=NOW + timedelta(seconds=2),
        ):
            queried = query_promotion_approval(
                self.approval_root,
                approval_id=approval["approval_id"],
                proposal_id=replayed_proposal.proposal_id,
            )
            with self.assertRaisesRegex(
                LabPromotionApprovalStoreError,
                "terminal state",
            ):
                consume_promotion_approval(
                    self.approval_root,
                    approval_id=approval["approval_id"],
                    proposal_id=replayed_proposal.proposal_id,
                    run_id=replayed_transaction.run_id,
                )

        self.assertEqual(queried["status"], "consumed")
        self.assertEqual(queried["terminal"], consumed["terminal"])
        self.assertFalse((self.repository / "authority.txt").exists())

    def test_expiration_lifetime_and_replay_require_fresh_human_authority(self):
        _candidate, _source, proposal, transaction = self._evidence_chain()
        with patch(
            "hands_free_auto_lab.lab_promotion_approval_store._utcnow",
            return_value=NOW,
        ):
            challenge = create_promotion_challenge(
                self.approval_root,
                proposal_id=proposal.proposal_id,
            )
            approved = decide_promotion_challenge(
                self.approval_root,
                challenge_id=challenge["challenge_id"],
                proposal_id=proposal.proposal_id,
                decision="approve",
            )["approval"]

        original_expires_at = approved["expires_at"]
        approved["expires_at"] = (NOW + timedelta(days=1)).isoformat()
        with patch(
            "hands_free_auto_lab.lab_promotion_approval_store._utcnow",
            return_value=NOW + timedelta(seconds=31),
        ):
            queried = query_promotion_approval(
                self.approval_root,
                approval_id=approved["approval_id"],
                proposal_id=proposal.proposal_id,
            )
            self.assertEqual(queried["approval"]["expires_at"], original_expires_at)
            self.assertEqual(queried["status"], "expired")
            with self.assertRaisesRegex(LabPromotionApprovalStoreError, "expired"):
                consume_promotion_approval(
                    self.approval_root,
                    approval_id=approved["approval_id"],
                    proposal_id=proposal.proposal_id,
                    run_id=transaction.run_id,
                )

        self.assertFalse((self.repository / "authority.txt").exists())

        second_root = self.root / "second-approval-state"
        second_root.mkdir(mode=0o700)
        initialize_promotion_approval_state_root(second_root)
        with patch(
            "hands_free_auto_lab.lab_promotion_approval_store._utcnow",
            return_value=NOW,
        ):
            challenge = create_promotion_challenge(
                second_root,
                proposal_id=proposal.proposal_id,
            )
            approval = decide_promotion_challenge(
                second_root,
                challenge_id=challenge["challenge_id"],
                proposal_id=proposal.proposal_id,
                decision="approve",
            )["approval"]
        with patch(
            "hands_free_auto_lab.lab_promotion_approval_store._utcnow",
            return_value=NOW + timedelta(seconds=1),
        ):
            consumed = consume_promotion_approval(
                second_root,
                approval_id=approval["approval_id"],
                proposal_id=proposal.proposal_id,
                run_id=transaction.run_id,
            )
            with self.assertRaisesRegex(LabPromotionApprovalStoreError, "terminal state"):
                consume_promotion_approval(
                    second_root,
                    approval_id=approval["approval_id"],
                    proposal_id=proposal.proposal_id,
                    run_id=transaction.run_id,
                )

        self.assertEqual(consumed["terminal"]["run_id"], transaction.run_id)
        self.assertFalse((self.repository / "authority.txt").exists())
