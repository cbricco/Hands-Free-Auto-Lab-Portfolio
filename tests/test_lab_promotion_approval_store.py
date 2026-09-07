from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import hands_free_auto_lab.lab_promotion_approval_store as store

from hands_free_auto_lab.lab_promotion_approval_state import (
    validate_decision_record,
)
from hands_free_auto_lab.lab_promotion_approval_store import (
    APPROVALS_DIRECTORY,
    APPROVAL_FILENAME,
    CHALLENGES_DIRECTORY,
    CHALLENGE_FILENAME,
    DECISION_FILENAME,
    LOCK_FILENAME,
    TERMINAL_FILENAME,
    LabPromotionApprovalStoreError,
    consume_promotion_approval,
    create_promotion_challenge,
    decide_promotion_challenge,
    initialize_promotion_approval_state_root,
    query_promotion_approval,
)


NOW = datetime(
    2026,
    8,
    19,
    2,
    30,
    0,
    tzinfo=timezone.utc,
)

PROPOSAL_ID = "a" * 64
OTHER_PROPOSAL_ID = "b" * 64
RUN_ID = "c" * 64


class LabPromotionApprovalStoreTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-promotion-approval-store-"
            )
        )

        os.chmod(
            self.root,
            0o700,
        )

        initialize_promotion_approval_state_root(
            self.root
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    @contextmanager
    def clock(
        self,
        value: datetime = NOW,
    ):
        with patch(
            "hands_free_auto_lab.lab_promotion_approval_store._utcnow",
            return_value=value,
        ):
            yield

    def make_approved(self):
        with self.clock():
            challenge = create_promotion_challenge(
                self.root,
                proposal_id=PROPOSAL_ID,
            )

            result = decide_promotion_challenge(
                self.root,
                challenge_id=challenge["challenge_id"],
                proposal_id=PROPOSAL_ID,
                decision="approve",
            )

        return (
            challenge,
            result["approval"],
        )

    def test_initialize_creates_private_fixed_layout(self):
        self.assertEqual(
            {
                path.name
                for path in self.root.iterdir()
            },
            {
                LOCK_FILENAME,
                CHALLENGES_DIRECTORY,
                APPROVALS_DIRECTORY,
            },
        )

        self.assertEqual(
            self.root.stat().st_mode
            & 0o777,
            0o700,
        )

        self.assertEqual(
            (
                self.root
                / LOCK_FILENAME
            ).stat().st_mode
            & 0o777,
            0o600,
        )

        for name in (
            CHALLENGES_DIRECTORY,
            APPROVALS_DIRECTORY,
        ):
            self.assertEqual(
                (
                    self.root
                    / name
                ).stat().st_mode
                & 0o777,
                0o700,
            )

    def test_initialization_rejects_unexpected_root_entry(self):
        extra = (
            self.root
            / "unexpected"
        )

        extra.write_text(
            "x",
            encoding="utf-8",
        )

        extra.chmod(
            0o600
        )

        with self.assertRaises(
            LabPromotionApprovalStoreError
        ):
            initialize_promotion_approval_state_root(
                self.root
            )

    def test_create_challenge_binds_exact_proposal_and_refuses_second_pending(self):
        with self.clock():
            challenge = create_promotion_challenge(
                self.root,
                proposal_id=PROPOSAL_ID,
            )

            with self.assertRaisesRegex(
                LabPromotionApprovalStoreError,
                "already pending",
            ):
                create_promotion_challenge(
                    self.root,
                    proposal_id=OTHER_PROPOSAL_ID,
                )

        self.assertEqual(
            challenge["proposal_id"],
            PROPOSAL_ID,
        )

        path = (
            self.root
            / CHALLENGES_DIRECTORY
            / challenge["challenge_id"]
            / CHALLENGE_FILENAME
        )

        self.assertTrue(
            path.is_file()
        )

        self.assertEqual(
            path.stat().st_mode
            & 0o777,
            0o600,
        )

    def test_wrong_proposal_cannot_decide_challenge(self):
        with self.clock():
            challenge = create_promotion_challenge(
                self.root,
                proposal_id=PROPOSAL_ID,
            )

            with self.assertRaisesRegex(
                LabPromotionApprovalStoreError,
                "proposal binding",
            ):
                decide_promotion_challenge(
                    self.root,
                    challenge_id=challenge["challenge_id"],
                    proposal_id=OTHER_PROPOSAL_ID,
                    decision="approve",
                )

    def test_approve_creates_short_lived_bound_authority(self):
        challenge, approval = self.make_approved()

        self.assertEqual(
            approval["proposal_id"],
            PROPOSAL_ID,
        )

        self.assertEqual(
            approval["challenge_id"],
            challenge["challenge_id"],
        )

        with self.clock():
            queried = query_promotion_approval(
                self.root,
                approval_id=approval["approval_id"],
                proposal_id=PROPOSAL_ID,
            )

        self.assertEqual(
            queried["status"],
            "approved",
        )

    def test_rejection_is_irreversible_and_creates_no_approval(self):
        with self.clock():
            challenge = create_promotion_challenge(
                self.root,
                proposal_id=PROPOSAL_ID,
            )

            rejected = decide_promotion_challenge(
                self.root,
                challenge_id=challenge["challenge_id"],
                proposal_id=PROPOSAL_ID,
                decision="reject",
            )

            with self.assertRaisesRegex(
                LabPromotionApprovalStoreError,
                "irreversible decision",
            ):
                decide_promotion_challenge(
                    self.root,
                    challenge_id=challenge["challenge_id"],
                    proposal_id=PROPOSAL_ID,
                    decision="approve",
                )

        self.assertEqual(
            rejected["status"],
            "rejected",
        )

        self.assertEqual(
            list(
                (
                    self.root
                    / APPROVALS_DIRECTORY
                ).iterdir()
            ),
            [],
        )

    def test_expired_challenge_gets_durable_expired_decision(self):
        with self.clock():
            challenge = create_promotion_challenge(
                self.root,
                proposal_id=PROPOSAL_ID,
            )

        with self.clock(
            NOW
            + timedelta(
                hours=3,
                seconds=1,
            )
        ):
            with self.assertRaisesRegex(
                LabPromotionApprovalStoreError,
                "challenge expired",
            ):
                decide_promotion_challenge(
                    self.root,
                    challenge_id=challenge["challenge_id"],
                    proposal_id=PROPOSAL_ID,
                    decision="approve",
                )

        raw = (
            self.root
            / CHALLENGES_DIRECTORY
            / challenge["challenge_id"]
            / DECISION_FILENAME
        ).read_bytes()

        decision = validate_decision_record(
            raw
        )

        self.assertEqual(
            decision["decision"],
            "expired",
        )

    def test_normal_consumption_binds_exact_run_id(self):
        _, approval = self.make_approved()

        with self.clock(
            NOW
            + timedelta(
                seconds=1
            )
        ):
            consumed = consume_promotion_approval(
                self.root,
                approval_id=approval["approval_id"],
                proposal_id=PROPOSAL_ID,
                run_id=RUN_ID,
            )

            queried = query_promotion_approval(
                self.root,
                approval_id=approval["approval_id"],
                proposal_id=PROPOSAL_ID,
            )

        self.assertEqual(
            consumed["status"],
            "consumed",
        )

        self.assertEqual(
            queried["status"],
            "consumed",
        )

        self.assertEqual(
            consumed["terminal"]["run_id"],
            RUN_ID,
        )

    def test_duplicate_consumption_is_refused(self):
        _, approval = self.make_approved()

        with self.clock(
            NOW
            + timedelta(
                seconds=1
            )
        ):
            first = consume_promotion_approval(
                self.root,
                approval_id=approval["approval_id"],
                proposal_id=PROPOSAL_ID,
                run_id="1" * 64,
            )

            with self.assertRaisesRegex(
                LabPromotionApprovalStoreError,
                "terminal state",
            ):
                consume_promotion_approval(
                    self.root,
                    approval_id=approval["approval_id"],
                    proposal_id=PROPOSAL_ID,
                    run_id="2" * 64,
                )

        self.assertEqual(
            first["terminal"]["run_id"],
            "1" * 64,
        )

    def test_concurrent_consumption_yields_one_terminal_winner(self):
        _, approval = self.make_approved()

        barrier = threading.Barrier(
            2
        )

        successes = []
        failures = []

        def worker(
            run_id: str,
        ):
            barrier.wait()

            try:
                result = consume_promotion_approval(
                    self.root,
                    approval_id=approval["approval_id"],
                    proposal_id=PROPOSAL_ID,
                    run_id=run_id,
                )

                successes.append(
                    result
                )

            except LabPromotionApprovalStoreError as exc:
                failures.append(
                    str(
                        exc
                    )
                )

        with self.clock(
            NOW
            + timedelta(
                seconds=1
            )
        ):
            threads = [
                threading.Thread(
                    target=worker,
                    args=(
                        "3" * 64,
                    ),
                ),
                threading.Thread(
                    target=worker,
                    args=(
                        "4" * 64,
                    ),
                ),
            ]

            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join(
                    timeout=5
                )

                self.assertFalse(
                    thread.is_alive()
                )

        self.assertEqual(
            len(
                successes
            ),
            1,
        )

        self.assertEqual(
            len(
                failures
            ),
            1,
        )

        self.assertIn(
            successes[0]["terminal"]["run_id"],
            {
                "3" * 64,
                "4" * 64,
            },
        )

    def test_crash_after_terminal_claim_never_restores_authority(self):
        _, approval = self.make_approved()

        with self.clock(
            NOW
            + timedelta(
                seconds=1
            )
        ):
            with patch(
                "hands_free_auto_lab.lab_promotion_approval_store._write_all",
                side_effect=LabPromotionApprovalStoreError(
                    "simulated crash after claim"
                ),
            ):
                with self.assertRaisesRegex(
                    LabPromotionApprovalStoreError,
                    "simulated crash",
                ):
                    consume_promotion_approval(
                        self.root,
                        approval_id=approval["approval_id"],
                        proposal_id=PROPOSAL_ID,
                        run_id="5" * 64,
                    )

        terminal = (
            self.root
            / APPROVALS_DIRECTORY
            / approval["approval_id"]
            / TERMINAL_FILENAME
        )

        self.assertTrue(
            terminal.exists()
        )

        with self.clock(
            NOW
            + timedelta(
                seconds=2
            )
        ):
            with self.assertRaises(
                LabPromotionApprovalStoreError
            ):
                consume_promotion_approval(
                    self.root,
                    approval_id=approval["approval_id"],
                    proposal_id=PROPOSAL_ID,
                    run_id="6" * 64,
                )

            with self.assertRaises(
                LabPromotionApprovalStoreError
            ):
                query_promotion_approval(
                    self.root,
                    approval_id=approval["approval_id"],
                    proposal_id=PROPOSAL_ID,
                )

    def test_terminal_claim_fsyncs_file_before_directory(self):
        _, approval = self.make_approved()

        real_fsync = os.fsync
        observed = []

        def recording_fsync(
            fd: int,
        ):
            mode = os.fstat(
                fd
            ).st_mode

            if stat.S_ISREG(
                mode
            ):
                observed.append(
                    "file"
                )

            elif stat.S_ISDIR(
                mode
            ):
                observed.append(
                    "directory"
                )

            else:
                observed.append(
                    "other"
                )

            real_fsync(
                fd
            )

        with self.clock(
            NOW
            + timedelta(
                seconds=1
            )
        ):
            with patch(
                "hands_free_auto_lab.lab_promotion_approval_store.os.fsync",
                side_effect=recording_fsync,
            ):
                result = consume_promotion_approval(
                    self.root,
                    approval_id=approval["approval_id"],
                    proposal_id=PROPOSAL_ID,
                    run_id="7" * 64,
                )

        self.assertEqual(
            result["status"],
            "consumed",
        )

        self.assertGreaterEqual(
            len(
                observed
            ),
            2,
        )

        self.assertEqual(
            observed[:2],
            [
                "file",
                "directory",
            ],
        )

    def test_consumed_replay_fails_after_store_reopen(self):
        _, approval = self.make_approved()

        with self.clock(
            NOW
            + timedelta(
                seconds=1
            )
        ):
            first = consume_promotion_approval(
                self.root,
                approval_id=approval["approval_id"],
                proposal_id=PROPOSAL_ID,
                run_id="8" * 64,
            )

        self.assertEqual(
            first["status"],
            "consumed",
        )

        initialize_promotion_approval_state_root(
            self.root
        )

        with self.clock(
            NOW
            + timedelta(
                seconds=2
            )
        ):
            queried = query_promotion_approval(
                self.root,
                approval_id=approval["approval_id"],
                proposal_id=PROPOSAL_ID,
            )

            with self.assertRaises(
                LabPromotionApprovalStoreError
            ):
                consume_promotion_approval(
                    self.root,
                    approval_id=approval["approval_id"],
                    proposal_id=PROPOSAL_ID,
                    run_id="9" * 64,
                )

        self.assertEqual(
            queried["status"],
            "consumed",
        )

        self.assertEqual(
            queried["terminal"]["run_id"],
            "8" * 64,
        )

    def test_hardlinked_approval_record_fails_closed(self):
        _, approval = self.make_approved()

        approval_file = (
            self.root
            / APPROVALS_DIRECTORY
            / approval["approval_id"]
            / APPROVAL_FILENAME
        )

        outside = (
            self.root.parent
            / (
                "auto-lab-approval-hardlink-"
                + approval["approval_id"]
            )
        )

        try:
            os.link(
                approval_file,
                outside,
            )

            with self.clock():
                with self.assertRaisesRegex(
                    LabPromotionApprovalStoreError,
                    "hard link",
                ):
                    query_promotion_approval(
                        self.root,
                        approval_id=approval["approval_id"],
                        proposal_id=PROPOSAL_ID,
                    )

        finally:
            if outside.exists():
                outside.unlink()

    def test_symlinked_approval_record_fails_closed(self):
        _, approval = self.make_approved()

        approval_file = (
            self.root
            / APPROVALS_DIRECTORY
            / approval["approval_id"]
            / APPROVAL_FILENAME
        )

        raw = approval_file.read_bytes()

        approval_file.unlink()

        outside = (
            self.root.parent
            / (
                "auto-lab-approval-outside-"
                + approval["approval_id"]
            )
        )

        outside.write_bytes(
            raw
        )

        outside.chmod(
            0o600
        )

        try:
            approval_file.symlink_to(
                outside
            )

            with self.clock():
                with self.assertRaises(
                    LabPromotionApprovalStoreError
                ):
                    query_promotion_approval(
                        self.root,
                        approval_id=approval["approval_id"],
                        proposal_id=PROPOSAL_ID,
                    )

        finally:
            if outside.exists():
                outside.unlink()

    def test_unsafe_lock_permissions_fail_closed(self):
        (
            self.root
            / LOCK_FILENAME
        ).chmod(
            0o644
        )

        with self.clock():
            with self.assertRaisesRegex(
                LabPromotionApprovalStoreError,
                "mode 0600",
            ):
                create_promotion_challenge(
                    self.root,
                    proposal_id=PROPOSAL_ID,
                )

    def test_expired_approval_is_terminalized_and_never_reusable(self):
        _, approval = self.make_approved()

        with self.clock(
            NOW
            + timedelta(
                seconds=31
            )
        ):
            with self.assertRaisesRegex(
                LabPromotionApprovalStoreError,
                "expired before consumption",
            ):
                consume_promotion_approval(
                    self.root,
                    approval_id=approval["approval_id"],
                    proposal_id=PROPOSAL_ID,
                    run_id=RUN_ID,
                )

            queried = query_promotion_approval(
                self.root,
                approval_id=approval["approval_id"],
                proposal_id=PROPOSAL_ID,
            )

            with self.assertRaises(
                LabPromotionApprovalStoreError
            ):
                consume_promotion_approval(
                    self.root,
                    approval_id=approval["approval_id"],
                    proposal_id=PROPOSAL_ID,
                    run_id="d" * 64,
                )

        self.assertEqual(
            queried["status"],
            "expired",
        )

        self.assertIsNotNone(
            queried["terminal"]
        )


if __name__ == "__main__":
    unittest.main()
