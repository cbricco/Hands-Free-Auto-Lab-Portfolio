from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

import hands_free_auto_lab.lab_promotion_transaction_journal as journal

from hands_free_auto_lab.lab_promotion_proposal import (
    LabPromotionProposal,
    LabPromotionProposalFile,
    PROMOTION_OPERATION_ADD,
    PROMOTION_OPERATION_MODIFY,
    PROMOTION_PROPOSAL_COMPONENT,
    PROMOTION_PROPOSAL_SCHEMA_VERSION,
    _PROPOSAL_ID_DOMAIN,
    _canonical_json_bytes,
    _proposal_identity_object,
)
from hands_free_auto_lab.lab_promotion_transaction_journal import (
    LOCK_FILENAME,
    PLAN_FILENAME,
    SNAPSHOTS_DIRECTORY,
    TRANSACTIONS_DIRECTORY,
    LabPromotionTransactionJournalError,
    append_promotion_transaction_snapshot,
    create_promotion_transaction_journal,
    initialize_promotion_transaction_journal_root,
    load_promotion_transaction_journal,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_PENDING,
    PROGRESS_VERIFIED,
    STATE_APPLYING,
    STATE_VERIFYING,
    build_lab_promotion_transaction,
    transition_lab_promotion_transaction,
)


RUN_ID = "9" * 64
CANDIDATE_ID = "a" * 64
HEAD = "1" * 40


def _sha(
    text: str,
) -> str:
    return hashlib.sha256(
        text.encode(
            "utf-8"
        )
    ).hexdigest()


def _proposal() -> LabPromotionProposal:
    files = (
        LabPromotionProposalFile(
            operation=PROMOTION_OPERATION_MODIFY,
            path="alpha.txt",
            before_exists=True,
            before_mode=0o644,
            after_mode=0o644,
            before_bytes=len(
                b"old alpha\n"
            ),
            before_sha256=_sha(
                "old alpha\n"
            ),
            after_bytes=len(
                b"new alpha\n"
            ),
            after_sha256=_sha(
                "new alpha\n"
            ),
            after_content="new alpha\n",
            source_kind="coding_candidate",
            source_id=CANDIDATE_ID,
        ),
        LabPromotionProposalFile(
            operation=PROMOTION_OPERATION_ADD,
            path="tests/test_alpha.py",
            before_exists=False,
            before_mode=None,
            after_mode=0o644,
            before_bytes=None,
            before_sha256=None,
            after_bytes=len(
                b"assert True\n"
            ),
            after_sha256=_sha(
                "assert True\n"
            ),
            after_content="assert True\n",
            source_kind="coding_candidate",
            source_id=CANDIDATE_ID,
        ),
    )

    identity = _proposal_identity_object(
        candidate_id=CANDIDATE_ID,
        repository_path="/tmp/example-repo",
        repository_device=100,
        repository_inode=200,
        branch="main",
        head=HEAD,
        files=files,
    )

    proposal_id = hashlib.sha256(
        _PROPOSAL_ID_DOMAIN
        + _canonical_json_bytes(
            identity
        )
    ).hexdigest()

    return LabPromotionProposal(
        component=PROMOTION_PROPOSAL_COMPONENT,
        schema_version=PROMOTION_PROPOSAL_SCHEMA_VERSION,
        proposal_id=proposal_id,
        candidate_id=CANDIDATE_ID,
        repository_path="/tmp/example-repo",
        repository_device=100,
        repository_inode=200,
        branch="main",
        head=HEAD,
        files=files,
    )


def _prepared():
    return build_lab_promotion_transaction(
        proposal=_proposal(),
        run_id=RUN_ID,
    )


def _applying(
    prepared,
):
    return transition_lab_promotion_transaction(
        prepared,
        state=STATE_APPLYING,
        progress_by_path={
            "alpha.txt": PROGRESS_INSTALLED,
            "tests/test_alpha.py": PROGRESS_PENDING,
        },
    )


class LabPromotionTransactionJournalTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-promotion-journal-"
            )
        )

        os.chmod(
            self.root,
            0o700,
        )

        initialize_promotion_transaction_journal_root(
            self.root
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def create(self):
        prepared = _prepared()

        persisted = create_promotion_transaction_journal(
            self.root,
            transaction=prepared,
        )

        return prepared, persisted

    def transaction_dir(
        self,
        transaction_id: str,
    ) -> Path:
        return (
            self.root
            / TRANSACTIONS_DIRECTORY
            / transaction_id
        )

    def test_initialize_creates_private_fixed_layout(self):
        self.assertEqual(
            {
                path.name
                for path in self.root.iterdir()
            },
            {
                LOCK_FILENAME,
                TRANSACTIONS_DIRECTORY,
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

        self.assertEqual(
            (
                self.root
                / TRANSACTIONS_DIRECTORY
            ).stat().st_mode
            & 0o777,
            0o700,
        )

    def test_create_round_trip_preserves_exact_prepared_transaction(self):
        prepared, persisted = self.create()

        loaded = load_promotion_transaction_journal(
            self.root,
            transaction_id=prepared.transaction_id,
        )

        self.assertEqual(
            persisted,
            prepared,
        )

        self.assertEqual(
            loaded,
            prepared,
        )

        directory = self.transaction_dir(
            prepared.transaction_id
        )

        self.assertTrue(
            (
                directory
                / PLAN_FILENAME
            ).is_file()
        )

        self.assertTrue(
            (
                directory
                / SNAPSHOTS_DIRECTORY
                / "0000000000000000.json"
            ).is_file()
        )

    def test_v3_plan_round_trip_preserves_exact_provenance_fields(self):
        prepared, _ = self.create()
        plan = (
            self.transaction_dir(prepared.transaction_id)
            / PLAN_FILENAME
        )
        record = json.loads(
            plan.read_text(encoding="utf-8")
        )

        self.assertEqual(
            [
                (item["source_kind"], item["source_id"])
                for item in record["files"]
            ],
            [
                (item.source_kind, item.source_id)
                for item in prepared.files
            ],
        )
        for item in record["files"]:
            self.assertIn("source_kind", item)
            self.assertIn("source_id", item)
            self.assertNotIn("source_write_action_id", item)

        loaded = load_promotion_transaction_journal(
            self.root,
            transaction_id=prepared.transaction_id,
        )
        self.assertEqual(
            [
                (item.source_kind, item.source_id)
                for item in loaded.files
            ],
            [
                (item.source_kind, item.source_id)
                for item in prepared.files
            ],
        )

    def test_v2_style_source_write_action_id_substitution_is_refused(self):
        prepared, _ = self.create()
        plan = (
            self.transaction_dir(prepared.transaction_id)
            / PLAN_FILENAME
        )
        record = json.loads(
            plan.read_text(encoding="utf-8")
        )
        for item in record["files"]:
            item["source_write_action_id"] = item.pop("source_id")
            item.pop("source_kind")

        plan.write_text(
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        plan.chmod(0o600)

        with self.assertRaisesRegex(
            LabPromotionTransactionJournalError,
            "fields do not exactly match schema",
        ):
            load_promotion_transaction_journal(
                self.root,
                transaction_id=prepared.transaction_id,
            )

    def test_coding_candidate_provenance_tampering_is_rejected_after_reload(self):
        prepared, _ = self.create()

        loaded = load_promotion_transaction_journal(
            self.root,
            transaction_id=prepared.transaction_id,
        )
        self.assertEqual(
            {
                (item.source_kind, item.source_id)
                for item in loaded.files
            },
            {("coding_candidate", CANDIDATE_ID)},
        )

        plan = (
            self.transaction_dir(prepared.transaction_id)
            / PLAN_FILENAME
        )
        record = json.loads(
            plan.read_text(encoding="utf-8")
        )
        record["files"][0]["source_id"] = "e" * 64
        plan.write_text(
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        plan.chmod(0o600)

        with self.assertRaisesRegex(
            LabPromotionTransactionJournalError,
            "proposal binding|transaction_id",
        ):
            load_promotion_transaction_journal(
                self.root,
                transaction_id=prepared.transaction_id,
            )

    def test_duplicate_transaction_creation_is_refused(self):
        prepared, _ = self.create()

        with self.assertRaises(
            LabPromotionTransactionJournalError
        ):
            create_promotion_transaction_journal(
                self.root,
                transaction=prepared,
            )

    def test_legal_successor_is_appended_and_survives_reopen(self):
        prepared, _ = self.create()

        applying = _applying(
            prepared
        )

        persisted = append_promotion_transaction_snapshot(
            self.root,
            successor=applying,
            expected_previous_snapshot_id=prepared.snapshot_id,
        )

        initialize_promotion_transaction_journal_root(
            self.root
        )

        loaded = load_promotion_transaction_journal(
            self.root,
            transaction_id=prepared.transaction_id,
        )

        self.assertEqual(
            persisted,
            applying,
        )

        self.assertEqual(
            loaded,
            applying,
        )

    def test_multiple_legal_snapshots_form_valid_chain(self):
        prepared, _ = self.create()

        applying = _applying(
            prepared
        )

        append_promotion_transaction_snapshot(
            self.root,
            successor=applying,
            expected_previous_snapshot_id=prepared.snapshot_id,
        )

        all_installed = transition_lab_promotion_transaction(
            applying,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_INSTALLED
                for item in applying.files
            },
        )

        append_promotion_transaction_snapshot(
            self.root,
            successor=all_installed,
            expected_previous_snapshot_id=applying.snapshot_id,
        )

        verifying = transition_lab_promotion_transaction(
            all_installed,
            state=STATE_VERIFYING,
            progress_by_path={
                item.path: item.progress
                for item in all_installed.files
            },
        )

        append_promotion_transaction_snapshot(
            self.root,
            successor=verifying,
            expected_previous_snapshot_id=all_installed.snapshot_id,
        )

        loaded = load_promotion_transaction_journal(
            self.root,
            transaction_id=prepared.transaction_id,
        )

        self.assertEqual(
            loaded,
            verifying,
        )

    def test_stale_previous_snapshot_is_refused(self):
        prepared, _ = self.create()

        applying = _applying(
            prepared
        )

        append_promotion_transaction_snapshot(
            self.root,
            successor=applying,
            expected_previous_snapshot_id=prepared.snapshot_id,
        )

        all_installed = transition_lab_promotion_transaction(
            applying,
            state=STATE_APPLYING,
            progress_by_path={
                item.path: PROGRESS_INSTALLED
                for item in applying.files
            },
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionJournalError,
            "stale",
        ):
            append_promotion_transaction_snapshot(
                self.root,
                successor=all_installed,
                expected_previous_snapshot_id=prepared.snapshot_id,
            )

    def test_no_op_append_is_refused(self):
        prepared, _ = self.create()

        with self.assertRaisesRegex(
            LabPromotionTransactionJournalError,
            "no-op",
        ):
            append_promotion_transaction_snapshot(
                self.root,
                successor=prepared,
                expected_previous_snapshot_id=prepared.snapshot_id,
            )

    def test_tampered_successor_is_refused_before_append(self):
        prepared, _ = self.create()

        applying = _applying(
            prepared
        )

        tampered = replace(
            applying,
            snapshot_id="f" * 64,
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionJournalError,
            "successor failed validation",
        ):
            append_promotion_transaction_snapshot(
                self.root,
                successor=tampered,
                expected_previous_snapshot_id=prepared.snapshot_id,
            )

    def test_concurrent_append_from_same_snapshot_yields_one_winner(self):
        prepared, _ = self.create()

        first = _applying(
            prepared
        )

        second = transition_lab_promotion_transaction(
            prepared,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_PENDING,
                "tests/test_alpha.py": PROGRESS_INSTALLED,
            },
        )

        barrier = threading.Barrier(
            2
        )

        successes = []
        failures = []

        def worker(
            successor,
        ):
            barrier.wait()

            try:
                result = append_promotion_transaction_snapshot(
                    self.root,
                    successor=successor,
                    expected_previous_snapshot_id=prepared.snapshot_id,
                )

                successes.append(
                    result
                )

            except LabPromotionTransactionJournalError as exc:
                failures.append(
                    str(
                        exc
                    )
                )

        threads = [
            threading.Thread(
                target=worker,
                args=(
                    first,
                ),
            ),
            threading.Thread(
                target=worker,
                args=(
                    second,
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
            "stale",
            failures[0],
        )

    def test_plan_tamper_fails_closed(self):
        prepared, _ = self.create()

        plan = (
            self.transaction_dir(
                prepared.transaction_id
            )
            / PLAN_FILENAME
        )

        parsed = json.loads(
            plan.read_text(
                encoding="utf-8"
            )
        )

        parsed["branch"] = "tampered"

        plan.write_bytes(
            (
                json.dumps(
                    parsed,
                    sort_keys=True,
                    separators=(
                        ",",
                        ":",
                    ),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode(
                "utf-8"
            )
        )

        plan.chmod(
            0o600
        )

        with self.assertRaises(
            LabPromotionTransactionJournalError
        ):
            load_promotion_transaction_journal(
                self.root,
                transaction_id=prepared.transaction_id,
            )

    def test_snapshot_tamper_fails_closed(self):
        prepared, _ = self.create()

        snapshot = (
            self.transaction_dir(
                prepared.transaction_id
            )
            / SNAPSHOTS_DIRECTORY
            / "0000000000000000.json"
        )

        parsed = json.loads(
            snapshot.read_text(
                encoding="utf-8"
            )
        )

        parsed["snapshot_id"] = "e" * 64

        snapshot.write_bytes(
            (
                json.dumps(
                    parsed,
                    sort_keys=True,
                    separators=(
                        ",",
                        ":",
                    ),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode(
                "utf-8"
            )
        )

        snapshot.chmod(
            0o600
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionJournalError,
            "initial snapshot identity",
        ):
            load_promotion_transaction_journal(
                self.root,
                transaction_id=prepared.transaction_id,
            )

    def test_snapshot_gap_or_unexpected_entry_fails_closed(self):
        prepared, _ = self.create()

        snapshots = (
            self.transaction_dir(
                prepared.transaction_id
            )
            / SNAPSHOTS_DIRECTORY
        )

        extra = (
            snapshots
            / "0000000000000002.json"
        )

        extra.write_text(
            "{}\n",
            encoding="utf-8",
        )

        extra.chmod(
            0o600
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionJournalError,
            "non-contiguous",
        ):
            load_promotion_transaction_journal(
                self.root,
                transaction_id=prepared.transaction_id,
            )

    def test_symlinked_plan_fails_closed(self):
        prepared, _ = self.create()

        directory = self.transaction_dir(
            prepared.transaction_id
        )

        plan = (
            directory
            / PLAN_FILENAME
        )

        raw = plan.read_bytes()

        outside = (
            self.root.parent
            / (
                "auto-lab-journal-plan-"
                + prepared.transaction_id
            )
        )

        outside.write_bytes(
            raw
        )

        outside.chmod(
            0o600
        )

        plan.unlink()

        try:
            plan.symlink_to(
                outside
            )

            with self.assertRaises(
                LabPromotionTransactionJournalError
            ):
                load_promotion_transaction_journal(
                    self.root,
                    transaction_id=prepared.transaction_id,
                )

        finally:
            if outside.exists():
                outside.unlink()

    def test_hardlinked_snapshot_fails_closed(self):
        prepared, _ = self.create()

        snapshot = (
            self.transaction_dir(
                prepared.transaction_id
            )
            / SNAPSHOTS_DIRECTORY
            / "0000000000000000.json"
        )

        outside = (
            self.root.parent
            / (
                "auto-lab-journal-snapshot-"
                + prepared.transaction_id
            )
        )

        try:
            os.link(
                snapshot,
                outside,
            )

            with self.assertRaisesRegex(
                LabPromotionTransactionJournalError,
                "hard link",
            ):
                load_promotion_transaction_journal(
                    self.root,
                    transaction_id=prepared.transaction_id,
                )

        finally:
            if outside.exists():
                outside.unlink()

    def test_partial_next_snapshot_fails_closed_after_simulated_write_failure(self):
        prepared, _ = self.create()

        applying = _applying(
            prepared
        )

        real_write_all = journal._write_all
        calls = 0

        def failing_write_all(
            fd,
            data,
        ):
            nonlocal calls
            calls += 1

            if calls == 1:
                raise LabPromotionTransactionJournalError(
                    "simulated snapshot write failure"
                )

            return real_write_all(
                fd,
                data,
            )

        with patch(
            "hands_free_auto_lab.lab_promotion_transaction_journal._write_all",
            side_effect=failing_write_all,
        ):
            with self.assertRaisesRegex(
                LabPromotionTransactionJournalError,
                "simulated",
            ):
                append_promotion_transaction_snapshot(
                    self.root,
                    successor=applying,
                    expected_previous_snapshot_id=prepared.snapshot_id,
                )

        with self.assertRaises(
            LabPromotionTransactionJournalError
        ):
            load_promotion_transaction_journal(
                self.root,
                transaction_id=prepared.transaction_id,
            )

    def test_unsafe_lock_permissions_fail_closed(self):
        (
            self.root
            / LOCK_FILENAME
        ).chmod(
            0o644
        )

        with self.assertRaisesRegex(
            LabPromotionTransactionJournalError,
            "mode 0600",
        ):
            load_promotion_transaction_journal(
                self.root,
                transaction_id="1" * 64,
            )

    def test_module_has_no_repository_or_execution_authority(self):
        source = Path(
            journal.__file__
        ).read_text(
            encoding="utf-8"
        )

        forbidden = (
            "subprocess",
            "/usr/bin/git",
            "os.replace",
            "os.rename",
            "os.unlink",
            "os.remove",
            "os.system",
            "Popen",
            "shell=True",
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
