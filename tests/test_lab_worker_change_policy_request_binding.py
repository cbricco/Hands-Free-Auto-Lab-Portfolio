from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from hands_free_auto_lab.lab_worker import (
    LabWorkerContractError,
    build_lab_worker_request,
    validate_lab_worker_request,
)

from hands_free_auto_lab.lab_workspace import (
    create_lab_workspace,
)


class LabWorkerChangePolicyRequestBindingTests(
    unittest.TestCase
):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

        self.parent = Path(
            self.temp.name
        )

        self.parent.chmod(
            0o700
        )

        self.workspace = create_lab_workspace(
            str(
                self.parent
            ),
            session_id="a" * 64,
        )

    def tearDown(self):
        self.temp.cleanup()

    def request(
        self,
        *,
        policy_id=None,
    ):
        return build_lab_worker_request(
            worker="fake-worker",
            goal="same bounded goal",
            workspace=self.workspace,
            change_policy_id=policy_id,
            max_runtime_seconds=30,
            max_log_bytes=4096,
        )

    def test_policy_id_changes_request_identity(self):
        first = self.request(
            policy_id="1" * 64,
        )

        second = self.request(
            policy_id="2" * 64,
        )

        self.assertNotEqual(
            first.request_id,
            second.request_id,
        )

        self.assertEqual(
            first.change_policy_id,
            "1" * 64,
        )

        self.assertEqual(
            second.change_policy_id,
            "2" * 64,
        )

    def test_policy_id_tampering_breaks_request_identity(self):
        request = self.request(
            policy_id="1" * 64,
        )

        tampered = replace(
            request,
            change_policy_id="2" * 64,
        )

        with self.assertRaisesRegex(
            LabWorkerContractError,
            "identity mismatch",
        ):
            validate_lab_worker_request(
                tampered
            )

    def test_malformed_policy_ids_fail_closed(self):
        malformed = (
            True,
            "",
            "A" * 64,
            "0" * 63,
            "g" * 64,
        )

        for value in malformed:
            with self.subTest(
                value=value
            ):
                with self.assertRaises(
                    LabWorkerContractError
                ):
                    self.request(
                        policy_id=value
                    )

    def test_none_policy_remains_valid_generic_request(self):
        request = self.request()

        self.assertIsNone(
            request.change_policy_id
        )

        self.assertIs(
            validate_lab_worker_request(
                request
            ),
            request,
        )


if __name__ == "__main__":
    unittest.main()
