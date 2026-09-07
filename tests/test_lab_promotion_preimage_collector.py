from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import hands_free_auto_lab.lab_promotion_preimage_collector as collector

from hands_free_auto_lab.lab_promotion_preimage_collector import (
    LabPromotionPreimageCollectorError,
    collect_promotion_preimages,
)
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
from hands_free_auto_lab.lab_promotion_recovery_materials import (
    build_promotion_recovery_materials,
)
from hands_free_auto_lab.lab_promotion_transaction_state import (
    PROGRESS_INSTALLED,
    PROGRESS_PENDING,
    STATE_APPLYING,
    build_lab_promotion_transaction,
    transition_lab_promotion_transaction,
)


GIT = "/usr/bin/git"
RUN_ID = "9" * 64
CANDIDATE_ID = "a" * 64

OLD_ALPHA = b"old alpha\n"


def _sha(
    raw: bytes,
) -> str:
    return hashlib.sha256(
        raw
    ).hexdigest()


class LabPromotionPreimageCollectorTests(
    unittest.TestCase
):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="auto-lab-preimage-collector-"
            )
        )

        self.repo = (
            self.root
            / "target"
        )

        self.repo.mkdir()

        self.git(
            "init",
            "-b",
            "main",
        )

        self.git(
            "config",
            "user.name",
            "Auto Lab Test",
        )

        self.git(
            "config",
            "user.email",
            "auto-lab@example.invalid",
        )

        (
            self.repo
            / "alpha.txt"
        ).write_bytes(
            OLD_ALPHA
        )

        (
            self.repo
            / "alpha.txt"
        ).chmod(
            0o644
        )

        (
            self.repo
            / "tests"
        ).mkdir()

        (
            self.repo
            / "tests"
            / ".keep"
        ).write_text(
            "keep\n",
            encoding="utf-8",
        )

        self.git(
            "add",
            "alpha.txt",
            "tests/.keep",
        )

        self.git(
            "commit",
            "-m",
            "baseline",
        )

    def tearDown(self):
        shutil.rmtree(
            self.root
        )

    def git(
        self,
        *args: str,
    ) -> str:
        completed = subprocess.run(
            [
                GIT,
                *args,
            ],
            cwd=self.repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )

        return completed.stdout.strip()

    def proposal(
        self,
        *,
        repository_path: str | None = None,
        repository_device: int | None = None,
        repository_inode: int | None = None,
        branch: str | None = None,
        head: str | None = None,
    ) -> LabPromotionProposal:
        repository_state = self.repo.stat()

        if repository_path is None:
            repository_path = str(
                self.repo
            )

        if repository_device is None:
            repository_device = repository_state.st_dev

        if repository_inode is None:
            repository_inode = repository_state.st_ino

        if branch is None:
            branch = self.git(
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
            )

        if head is None:
            head = self.git(
                "rev-parse",
                "--verify",
                "HEAD",
            )

        files = (
            LabPromotionProposalFile(
                operation=PROMOTION_OPERATION_MODIFY,
                path="alpha.txt",
                before_exists=True,
                before_mode=0o644,
                after_mode=0o644,
                before_bytes=len(
                    OLD_ALPHA
                ),
                before_sha256=_sha(
                    OLD_ALPHA
                ),
                after_bytes=len(
                    b"new alpha\n"
                ),
                after_sha256=_sha(
                    b"new alpha\n"
                ),
                after_content="new alpha\n",
                source_kind="legacy_write_action",
                source_id="b" * 64,
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
                    b"assert True\n"
                ),
                after_content="assert True\n",
                source_kind="legacy_write_action",
                source_id="c" * 64,
            ),
        )

        identity = _proposal_identity_object(
            candidate_id=CANDIDATE_ID,
            repository_path=repository_path,
            repository_device=repository_device,
            repository_inode=repository_inode,
            branch=branch,
            head=head,
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
            repository_path=repository_path,
            repository_device=repository_device,
            repository_inode=repository_inode,
            branch=branch,
            head=head,
            files=files,
        )

    def transaction(
        self,
        **proposal_overrides,
    ):
        return build_lab_promotion_transaction(
            proposal=self.proposal(
                **proposal_overrides
            ),
            run_id=RUN_ID,
        )

    def test_collects_exact_modify_preimage_and_proves_add_absent(self):
        transaction = self.transaction()

        observed = collect_promotion_preimages(
            transaction=transaction,
        )

        self.assertEqual(
            observed,
            {
                "alpha.txt": OLD_ALPHA,
            },
        )

    def test_collected_bytes_feed_private_recovery_material_builder(self):
        transaction = self.transaction()

        preimages = collect_promotion_preimages(
            transaction=transaction,
        )

        materials, normalized = (
            build_promotion_recovery_materials(
                transaction=transaction,
                preimages=preimages,
            )
        )

        self.assertEqual(
            normalized,
            {
                "alpha.txt": OLD_ALPHA,
            },
        )

        self.assertEqual(
            materials.transaction_id,
            transaction.transaction_id,
        )

    def test_modified_tracked_file_is_refused(self):
        transaction = self.transaction()

        (
            self.repo
            / "alpha.txt"
        ).write_text(
            "changed\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabPromotionPreimageCollectorError,
            "not completely clean",
        ):
            collect_promotion_preimages(
                transaction=transaction,
            )

    def test_untracked_add_destination_is_refused(self):
        transaction = self.transaction()

        (
            self.repo
            / "tests"
            / "test_alpha.py"
        ).write_text(
            "appeared\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LabPromotionPreimageCollectorError,
            "not completely clean|no longer absent",
        ):
            collect_promotion_preimages(
                transaction=transaction,
            )

    def test_changed_head_is_refused(self):
        transaction = self.transaction()

        (
            self.repo
            / "beta.txt"
        ).write_text(
            "beta\n",
            encoding="utf-8",
        )

        self.git(
            "add",
            "beta.txt",
        )

        self.git(
            "commit",
            "-m",
            "move head",
        )

        with self.assertRaisesRegex(
            LabPromotionPreimageCollectorError,
            "HEAD",
        ):
            collect_promotion_preimages(
                transaction=transaction,
            )

    def test_changed_branch_is_refused(self):
        transaction = self.transaction()

        self.git(
            "switch",
            "-c",
            "other",
        )

        with self.assertRaisesRegex(
            LabPromotionPreimageCollectorError,
            "branch",
        ):
            collect_promotion_preimages(
                transaction=transaction,
            )

    def test_detached_head_is_refused(self):
        transaction = self.transaction()

        self.git(
            "checkout",
            "--detach",
            "HEAD",
        )

        with self.assertRaises(
            LabPromotionPreimageCollectorError
        ):
            collect_promotion_preimages(
                transaction=transaction,
            )

    def test_wrong_repository_device_identity_is_refused(self):
        st = self.repo.stat()

        transaction = self.transaction(
            repository_device=st.st_dev + 1,
        )

        with self.assertRaisesRegex(
            LabPromotionPreimageCollectorError,
            "device/inode",
        ):
            collect_promotion_preimages(
                transaction=transaction,
            )

    def test_wrong_repository_inode_identity_is_refused(self):
        st = self.repo.stat()

        transaction = self.transaction(
            repository_inode=st.st_ino + 1,
        )

        with self.assertRaisesRegex(
            LabPromotionPreimageCollectorError,
            "device/inode",
        ):
            collect_promotion_preimages(
                transaction=transaction,
            )

    def test_symlink_repository_path_is_refused(self):
        link = (
            self.root
            / "target-link"
        )

        link.symlink_to(
            self.repo,
            target_is_directory=True,
        )

        transaction = self.transaction(
            repository_path=str(
                link
            ),
        )

        with self.assertRaisesRegex(
            LabPromotionPreimageCollectorError,
            "canonical",
        ):
            collect_promotion_preimages(
                transaction=transaction,
            )

    def test_nonprepared_transaction_is_refused(self):
        prepared = self.transaction()

        applying = transition_lab_promotion_transaction(
            prepared,
            state=STATE_APPLYING,
            progress_by_path={
                "alpha.txt": PROGRESS_INSTALLED,
                "tests/test_alpha.py": PROGRESS_PENDING,
            },
        )

        with self.assertRaisesRegex(
            LabPromotionPreimageCollectorError,
            "PREPARED",
        ):
            collect_promotion_preimages(
                transaction=applying,
            )

    def test_exact_before_hash_mismatch_is_refused_even_if_git_clean(self):
        transaction = self.transaction()

        wrong_file = replace(
            transaction.files[0],
            before_sha256="f" * 64,
        )

        tampered = replace(
            transaction,
            files=(
                wrong_file,
                transaction.files[1],
            ),
        )

        with self.assertRaises(
            LabPromotionPreimageCollectorError
        ):
            collect_promotion_preimages(
                transaction=tampered,
            )

    def test_module_has_no_repository_write_authority(self):
        source = Path(
            collector.__file__
        ).read_text(
            encoding="utf-8"
        )

        forbidden = (
            "O_WRONLY",
            "O_RDWR",
            "O_CREAT",
            "O_EXCL",
            "os.write",
            "os.mkdir",
            "os.replace",
            "os.rename",
            "os.unlink",
            "os.remove",
            "os.rmdir",
            "os.chmod",
            "os.fchmod",
            "git add",
            "git commit",
            "git push",
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
