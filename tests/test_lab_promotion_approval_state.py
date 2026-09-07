from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

import hands_free_auto_lab.lab_promotion_approval_state as approval_state

from hands_free_auto_lab.lab_promotion_approval_state import (
    LabPromotionApprovalStateError,
    build_approval_record,
    build_challenge_record,
    build_decision_record,
    build_terminal_record,
    validate_approval_record,
    validate_challenge_record,
    validate_decision_record,
    validate_terminal_record,
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

CHALLENGE_ID = "a" * 64
PROPOSAL_ID = "b" * 64
APPROVAL_ID = "c" * 64
RUN_ID = "d" * 64


def _time(
    delta: timedelta,
) -> str:
    return (
        NOW
        + delta
    ).isoformat()


class LabPromotionApprovalStateTests(
    unittest.TestCase
):
    def test_challenge_round_trip_accepts_three_hours(self):
        raw = build_challenge_record(
            challenge_id=CHALLENGE_ID,
            proposal_id=PROPOSAL_ID,
            created_at=_time(
                timedelta()
            ),
            expires_at=_time(
                timedelta(
                    hours=3
                )
            ),
        )

        record = validate_challenge_record(
            raw
        )

        self.assertEqual(
            record["challenge_id"],
            CHALLENGE_ID,
        )

        self.assertEqual(
            record["proposal_id"],
            PROPOSAL_ID,
        )

    def test_challenge_lifetime_over_three_hours_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "3 hours",
        ):
            build_challenge_record(
                challenge_id=CHALLENGE_ID,
                proposal_id=PROPOSAL_ID,
                created_at=_time(
                    timedelta()
                ),
                expires_at=_time(
                    timedelta(
                        hours=3,
                        microseconds=1,
                    )
                ),
            )

    def test_invalid_proposal_identifier_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "proposal_id",
        ):
            build_challenge_record(
                challenge_id=CHALLENGE_ID,
                proposal_id="not-a-proposal-id",
                created_at=_time(
                    timedelta()
                ),
                expires_at=_time(
                    timedelta(
                        minutes=1
                    )
                ),
            )

    def test_approved_decision_requires_approval_id(self):
        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "requires approval_id",
        ):
            build_decision_record(
                challenge_id=CHALLENGE_ID,
                proposal_id=PROPOSAL_ID,
                decision="approved",
                decided_at=_time(
                    timedelta()
                ),
                approval_id=None,
            )

    def test_rejected_decision_requires_null_approval_id(self):
        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "null approval_id",
        ):
            build_decision_record(
                challenge_id=CHALLENGE_ID,
                proposal_id=PROPOSAL_ID,
                decision="rejected",
                decided_at=_time(
                    timedelta()
                ),
                approval_id=APPROVAL_ID,
            )

    def test_decision_round_trip_preserves_proposal_binding(self):
        raw = build_decision_record(
            challenge_id=CHALLENGE_ID,
            proposal_id=PROPOSAL_ID,
            decision="approved",
            decided_at=_time(
                timedelta()
            ),
            approval_id=APPROVAL_ID,
        )

        record = validate_decision_record(
            raw
        )

        self.assertEqual(
            record["proposal_id"],
            PROPOSAL_ID,
        )

        self.assertEqual(
            record["approval_id"],
            APPROVAL_ID,
        )

    def test_approval_round_trip_accepts_thirty_seconds(self):
        raw = build_approval_record(
            challenge_id=CHALLENGE_ID,
            proposal_id=PROPOSAL_ID,
            approval_id=APPROVAL_ID,
            approved_at=_time(
                timedelta()
            ),
            created_at=_time(
                timedelta()
            ),
            expires_at=_time(
                timedelta(
                    seconds=30
                )
            ),
        )

        record = validate_approval_record(
            raw
        )

        self.assertEqual(
            record["proposal_id"],
            PROPOSAL_ID,
        )

    def test_approval_lifetime_over_thirty_seconds_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "30 seconds",
        ):
            build_approval_record(
                challenge_id=CHALLENGE_ID,
                proposal_id=PROPOSAL_ID,
                approval_id=APPROVAL_ID,
                approved_at=_time(
                    timedelta()
                ),
                created_at=_time(
                    timedelta()
                ),
                expires_at=_time(
                    timedelta(
                        seconds=30,
                        microseconds=1,
                    )
                ),
            )

    def test_approval_creation_before_human_action_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "earlier than approved_at",
        ):
            build_approval_record(
                challenge_id=CHALLENGE_ID,
                proposal_id=PROPOSAL_ID,
                approval_id=APPROVAL_ID,
                approved_at=_time(
                    timedelta(
                        seconds=1
                    )
                ),
                created_at=_time(
                    timedelta()
                ),
                expires_at=_time(
                    timedelta(
                        seconds=20
                    )
                ),
            )

    def test_consumed_terminal_requires_run_id(self):
        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "requires run_id",
        ):
            build_terminal_record(
                approval_id=APPROVAL_ID,
                proposal_id=PROPOSAL_ID,
                terminal_state="consumed",
                terminal_at=_time(
                    timedelta()
                ),
                run_id=None,
            )

    def test_nonconsumed_terminal_requires_null_run_id(self):
        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "null run_id",
        ):
            build_terminal_record(
                approval_id=APPROVAL_ID,
                proposal_id=PROPOSAL_ID,
                terminal_state="revoked",
                terminal_at=_time(
                    timedelta()
                ),
                run_id=RUN_ID,
            )

    def test_terminal_round_trip_binds_exact_run_and_proposal(self):
        raw = build_terminal_record(
            approval_id=APPROVAL_ID,
            proposal_id=PROPOSAL_ID,
            terminal_state="consumed",
            terminal_at=_time(
                timedelta()
            ),
            run_id=RUN_ID,
        )

        record = validate_terminal_record(
            raw
        )

        self.assertEqual(
            record["proposal_id"],
            PROPOSAL_ID,
        )

        self.assertEqual(
            record["run_id"],
            RUN_ID,
        )

    def test_timezone_naive_timestamp_is_refused(self):
        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "timezone-aware",
        ):
            build_challenge_record(
                challenge_id=CHALLENGE_ID,
                proposal_id=PROPOSAL_ID,
                created_at="2026-08-19T02:30:00",
                expires_at="2026-08-19T02:31:00+00:00",
            )

    def test_noncanonical_or_extra_json_fails_closed(self):
        raw = build_challenge_record(
            challenge_id=CHALLENGE_ID,
            proposal_id=PROPOSAL_ID,
            created_at=_time(
                timedelta()
            ),
            expires_at=_time(
                timedelta(
                    minutes=1
                )
            ),
        )

        parsed = json.loads(
            raw.decode(
                "utf-8"
            )
        )

        noncanonical = json.dumps(
            parsed,
            indent=2,
        ).encode(
            "utf-8"
        )

        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "canonical",
        ):
            validate_challenge_record(
                noncanonical
            )

        parsed["extra"] = True

        extra = (
            json.dumps(
                parsed,
                sort_keys=True,
                separators=(
                    ",",
                    ":",
                ),
            )
            + "\n"
        ).encode(
            "utf-8"
        )

        with self.assertRaisesRegex(
            LabPromotionApprovalStateError,
            "fields",
        ):
            validate_challenge_record(
                extra
            )

    def test_record_module_has_no_execution_or_storage_capability(self):
        source = Path(
            approval_state.__file__
        ).read_text(
            encoding="utf-8"
        )

        forbidden = (
            "subprocess",
            "os.open",
            "os.write",
            "os.replace",
            "os.unlink",
            "os.remove",
            "os.mkdir",
            "os.rmdir",
            "eval(",
            "exec(",
        )

        for token in forbidden:
            with self.subTest(
                token=token
            ):
                self.assertNotIn(
                    token,
                    source,
                )


if __name__ == "__main__":
    unittest.main()
