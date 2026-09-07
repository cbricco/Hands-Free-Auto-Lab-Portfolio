from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from hands_free_auto_lab.lab_audit import (
    AUDIT_COMPONENT,
    LabAuditError,
    write_lab_session_audit,
)
from hands_free_auto_lab.lab_controller import (
    CONTROLLER_COMPONENT,
    LabControllerResult,
)
from hands_free_auto_lab.lab_session import (
    SESSION_COMPONENT,
    SESSION_SCHEMA_VERSION,
    LabSessionRecord,
    lab_session_record_to_wire,
)
from hands_free_auto_lab.lab_workspace import (
    WORKSPACE_COMPONENT,
    LabWorkspace,
)


SESSION_ID = "b" * 64
FINAL_NAME = f"session-{SESSION_ID}.json"
TEMP_NAME = (
    ".hands-free-auto-lab-audit-"
    f"{SESSION_ID}.tmp"
)


class LabAuditTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-audit-tests-"
            )
        )

        os.chmod(
            self.root,
            0o700,
        )

        self.evidence = (
            self.root
            / "evidence"
        )

        self.evidence.mkdir(
            mode=0o700
        )

        workspace = LabWorkspace(
            component=WORKSPACE_COMPONENT,
            parent="/example/workspaces",
            path=(
                "/example/workspaces/"
                + SESSION_ID
            ),
            session_id=SESSION_ID,
            device=11,
            inode=22,
            uid=os.geteuid(),
            mode=0o700,
        )

        controller = LabControllerResult(
            component=CONTROLLER_COMPONENT,
            status="done",
            goal="Build a tiny example.",
            model="test-model",
            iterations=0,
            workspace_generation=0,
            tested_generation=0,
            acceptance_generation=0,
            summary=None,
            error=None,
            steps=(),
        )

        self.record = LabSessionRecord(
            component=SESSION_COMPONENT,
            schema_version=SESSION_SCHEMA_VERSION,
            started_at_utc=(
                "2026-08-19T01:00:00.000001Z"
            ),
            finished_at_utc=(
                "2026-08-19T01:00:01.000002Z"
            ),
            workspace=workspace,
            controller=controller,
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def expected_bytes(self):
        return (
            json.dumps(
                lab_session_record_to_wire(
                    self.record
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode(
            "utf-8"
        )

    def test_record_is_created_exactly_once(self):
        result = write_lab_session_audit(
            record=self.record,
            evidence_directory=str(
                self.evidence
            ),
        )

        destination = (
            self.evidence
            / FINAL_NAME
        )

        expected = self.expected_bytes()

        self.assertEqual(
            result.component,
            AUDIT_COMPONENT,
        )

        self.assertEqual(
            result.session_id,
            SESSION_ID,
        )

        self.assertEqual(
            result.filename,
            FINAL_NAME,
        )

        self.assertEqual(
            destination.read_bytes(),
            expected,
        )

        self.assertEqual(
            result.bytes_written,
            len(expected),
        )

        self.assertEqual(
            result.sha256,
            hashlib.sha256(
                expected
            ).hexdigest(),
        )

        final_stat = destination.stat()

        self.assertEqual(
            stat.S_IMODE(
                final_stat.st_mode
            ),
            0o600,
        )

        self.assertEqual(
            final_stat.st_nlink,
            1,
        )

        self.assertFalse(
            (
                self.evidence
                / TEMP_NAME
            ).exists()
        )

    def test_install_transition_uses_two_links_before_cleanup(self):
        real_unlink = os.unlink
        observed = []

        def inspecting_unlink(
            path,
            *,
            dir_fd=None,
        ):
            if (
                path == TEMP_NAME
                and dir_fd is not None
            ):
                temporary_stat = os.stat(
                    TEMP_NAME,
                    dir_fd=dir_fd,
                    follow_symlinks=False,
                )

                final_stat = os.stat(
                    FINAL_NAME,
                    dir_fd=dir_fd,
                    follow_symlinks=False,
                )

                observed.append(
                    (
                        temporary_stat.st_dev,
                        temporary_stat.st_ino,
                        temporary_stat.st_nlink,
                        final_stat.st_dev,
                        final_stat.st_ino,
                        final_stat.st_nlink,
                    )
                )

            return real_unlink(
                path,
                dir_fd=dir_fd,
            )

        with patch(
            "hands_free_auto_lab.lab_audit.os.unlink",
            side_effect=inspecting_unlink,
        ):
            write_lab_session_audit(
                record=self.record,
                evidence_directory=str(
                    self.evidence
                ),
            )

        self.assertEqual(
            len(observed),
            1,
        )

        (
            temp_dev,
            temp_ino,
            temp_links,
            final_dev,
            final_ino,
            final_links,
        ) = observed[0]

        self.assertEqual(
            (temp_dev, temp_ino),
            (final_dev, final_ino),
        )

        self.assertEqual(
            temp_links,
            2,
        )

        self.assertEqual(
            final_links,
            2,
        )

        self.assertEqual(
            (
                self.evidence
                / FINAL_NAME
            ).stat().st_nlink,
            1,
        )

    def test_existing_record_is_never_overwritten(self):
        destination = (
            self.evidence
            / FINAL_NAME
        )

        destination.write_text(
            "original\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabAuditError,
            "already exists",
        ):
            write_lab_session_audit(
                record=self.record,
                evidence_directory=str(
                    self.evidence
                ),
            )

        self.assertEqual(
            destination.read_text(
                encoding="utf-8"
            ),
            "original\n",
        )

    def test_symlink_destination_is_never_followed(self):
        outside = (
            self.root
            / "outside.txt"
        )

        outside.write_text(
            "outside\n",
            encoding="utf-8",
        )

        destination = (
            self.evidence
            / FINAL_NAME
        )

        destination.symlink_to(
            outside
        )

        with self.assertRaisesRegex(
            LabAuditError,
            "already exists",
        ):
            write_lab_session_audit(
                record=self.record,
                evidence_directory=str(
                    self.evidence
                ),
            )

        self.assertEqual(
            outside.read_text(
                encoding="utf-8"
            ),
            "outside\n",
        )

        self.assertTrue(
            destination.is_symlink()
        )

    def test_symlink_evidence_directory_is_refused(self):
        alias = (
            self.root
            / "evidence-alias"
        )

        alias.symlink_to(
            self.evidence,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            LabAuditError,
            "canonical",
        ):
            write_lab_session_audit(
                record=self.record,
                evidence_directory=str(
                    alias
                ),
            )

        self.assertEqual(
            list(
                self.evidence.iterdir()
            ),
            [],
        )

    def test_nonprivate_evidence_directory_is_refused(self):
        os.chmod(
            self.evidence,
            0o755,
        )

        with self.assertRaisesRegex(
            LabAuditError,
            "mode 0700",
        ):
            write_lab_session_audit(
                record=self.record,
                evidence_directory=str(
                    self.evidence
                ),
            )

        self.assertEqual(
            list(
                self.evidence.iterdir()
            ),
            [],
        )

    def test_known_temporary_collision_fails_closed(self):
        temporary = (
            self.evidence
            / TEMP_NAME
        )

        temporary.write_text(
            "unexpected temporary state\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabAuditError,
            "temporary path already exists",
        ):
            write_lab_session_audit(
                record=self.record,
                evidence_directory=str(
                    self.evidence
                ),
            )

        self.assertEqual(
            temporary.read_text(
                encoding="utf-8"
            ),
            "unexpected temporary state\n",
        )

        self.assertFalse(
            (
                self.evidence
                / FINAL_NAME
            ).exists()
        )

    def test_invalid_session_id_cannot_select_filename(self):
        bad_workspace = LabWorkspace(
            component=self.record.workspace.component,
            parent=self.record.workspace.parent,
            path=self.record.workspace.path,
            session_id="../escape",
            device=self.record.workspace.device,
            inode=self.record.workspace.inode,
            uid=self.record.workspace.uid,
            mode=self.record.workspace.mode,
        )

        bad_record = LabSessionRecord(
            component=self.record.component,
            schema_version=self.record.schema_version,
            started_at_utc=self.record.started_at_utc,
            finished_at_utc=self.record.finished_at_utc,
            workspace=bad_workspace,
            controller=self.record.controller,
        )

        with self.assertRaises(
            LabAuditError
        ):
            write_lab_session_audit(
                record=bad_record,
                evidence_directory=str(
                    self.evidence
                ),
            )

        self.assertEqual(
            list(
                self.evidence.iterdir()
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
