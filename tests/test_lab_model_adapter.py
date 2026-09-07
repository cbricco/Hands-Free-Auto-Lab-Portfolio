from __future__ import annotations

import json
import unittest
from urllib.error import URLError

from hands_free_auto_lab.lab_model_adapter import (
    LabModelAdapterError,
    MAX_MODEL_REQUEST_BYTES,
    OLLAMA_GENERATE_URL,
    build_model_request,
    parse_model_decision,
    request_model_decision,
)


def ollama_response(
    decision,
    *,
    done=True,
):
    if isinstance(
        decision,
        str,
    ):
        text = decision
    else:
        text = json.dumps(
            decision,
            separators=(",", ":"),
        )

    return json.dumps(
        {
            "model": "test-model",
            "response": text,
            "done": done,
        },
        separators=(",", ":"),
    ).encode(
        "utf-8"
    )


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
    ):
        self.payload = payload

    def __enter__(
        self,
    ):
        return self

    def __exit__(
        self,
        exc_type,
        exc,
        traceback,
    ):
        return None

    def read(
        self,
        amount=-1,
    ):
        if amount < 0:
            return self.payload

        return self.payload[
            :amount
        ]


class CapturingOpener:
    def __init__(
        self,
        payload: bytes,
    ):
        self.payload = payload
        self.calls = []

    def __call__(
        self,
        request,
        *,
        timeout,
    ):
        self.calls.append(
            (
                request,
                timeout,
            )
        )

        return FakeResponse(
            self.payload
        )


class LabModelAdapterTests(
    unittest.TestCase
):
    def test_request_uses_fixed_loopback_endpoint(self):
        opener = CapturingOpener(
            ollama_response(
                {
                    "decision": "done",
                    "summary": "goal complete",
                }
            )
        )

        turn = request_model_decision(
            goal="Build a small program.",
            context={
                "iteration": 1,
                "evidence": {
                    "stdout": "untrusted text",
                },
            },
            model=" qwen2.5:3b ",
            timeout_seconds=47,
            opener=opener,
        )

        self.assertEqual(
            len(opener.calls),
            1,
        )

        request, timeout = (
            opener.calls[0]
        )

        self.assertEqual(
            request.full_url,
            OLLAMA_GENERATE_URL,
        )

        self.assertEqual(
            OLLAMA_GENERATE_URL,
            "http://127.0.0.1:11434/api/generate",
        )

        self.assertEqual(
            request.get_method(),
            "POST",
        )

        self.assertEqual(
            timeout,
            47,
        )

        body = json.loads(
            request.data.decode(
                "utf-8"
            )
        )

        self.assertEqual(
            body["model"],
            "qwen2.5:3b",
        )

        self.assertIs(
            body["stream"],
            False,
        )

        self.assertEqual(
            body["format"],
            "json",
        )

        self.assertEqual(
            body["options"],
            {
                "temperature": 0,
            },
        )

        self.assertIn(
            "Human goal",
            body["prompt"],
        )

        self.assertIn(
            "untrusted",
            body["prompt"].lower(),
        )

        self.assertTrue(
            turn.decision.is_done
        )

    def test_read_file_action_is_parsed(self):
        decision = parse_model_decision(
            json.dumps(
                {
                    "decision": "action",
                    "kind": "READ_FILE",
                    "path": "README.md",
                }
            )
        )

        self.assertTrue(
            decision.is_action
        )

        self.assertEqual(
            decision.action.kind,
            "READ_FILE",
        )

        self.assertEqual(
            decision.action.path,
            "README.md",
        )

    def test_write_file_action_is_parsed(self):
        decision = parse_model_decision(
            json.dumps(
                {
                    "decision": "action",
                    "kind": "WRITE_FILE",
                    "path": "example.py",
                    "content": "value = 4\n",
                }
            )
        )

        self.assertTrue(
            decision.is_action
        )

        self.assertEqual(
            decision.action.kind,
            "WRITE_FILE",
        )

        self.assertEqual(
            decision.action.content,
            "value = 4\n",
        )

    def test_run_action_is_parsed(self):
        decision = parse_model_decision(
            json.dumps(
                {
                    "decision": "action",
                    "kind": "RUN",
                    "argv": [
                        "/usr/bin/python3",
                        "-m",
                        "unittest",
                        "-v",
                    ],
                }
            )
        )

        self.assertTrue(
            decision.is_action
        )

        self.assertEqual(
            decision.action.kind,
            "RUN",
        )

        self.assertEqual(
            decision.action.argv,
            (
                "/usr/bin/python3",
                "-m",
                "unittest",
                "-v",
            ),
        )

    def test_done_decision_is_parsed(self):
        decision = parse_model_decision(
            json.dumps(
                {
                    "decision": "done",
                    "summary": "All requested tests pass.",
                }
            )
        )

        self.assertTrue(
            decision.is_done
        )

        self.assertIsNone(
            decision.action
        )

        self.assertEqual(
            decision.summary,
            "All requested tests pass.",
        )

    def test_markdown_fenced_json_is_refused(self):
        with self.assertRaises(
            LabModelAdapterError
        ):
            parse_model_decision(
                '```json\n{"decision":"done","summary":"x"}\n```'
            )

    def test_extra_model_fields_are_refused(self):
        with self.assertRaises(
            LabModelAdapterError
        ):
            parse_model_decision(
                json.dumps(
                    {
                        "decision": "done",
                        "summary": "x",
                        "command": "rm -rf /",
                    }
                )
            )

    def test_absolute_read_path_fails_policy(self):
        with self.assertRaisesRegex(
            LabModelAdapterError,
            "deterministic policy",
        ):
            parse_model_decision(
                json.dumps(
                    {
                        "decision": "action",
                        "kind": "READ_FILE",
                        "path": "/etc/passwd",
                    }
                )
            )

    def test_bash_run_fails_policy(self):
        with self.assertRaisesRegex(
            LabModelAdapterError,
            "deterministic policy",
        ):
            parse_model_decision(
                json.dumps(
                    {
                        "decision": "action",
                        "kind": "RUN",
                        "argv": [
                            "/usr/bin/bash",
                            "-lc",
                            "echo unsafe",
                        ],
                    }
                )
            )

    def test_incomplete_ollama_response_is_refused(self):
        opener = CapturingOpener(
            ollama_response(
                {
                    "decision": "done",
                    "summary": "partial",
                },
                done=False,
            )
        )

        with self.assertRaisesRegex(
            LabModelAdapterError,
            "not complete",
        ):
            request_model_decision(
                goal="test",
                context={},
                model="test-model",
                opener=opener,
            )

        self.assertEqual(
            len(opener.calls),
            1,
        )

    def test_invalid_model_decision_json_is_refused(self):
        opener = CapturingOpener(
            ollama_response(
                "not-json"
            )
        )

        with self.assertRaises(
            LabModelAdapterError
        ):
            request_model_decision(
                goal="test",
                context={},
                model="test-model",
                opener=opener,
            )

        self.assertEqual(
            len(opener.calls),
            1,
        )

    def test_large_request_fails_before_network(self):
        with self.assertRaisesRegex(
            LabModelAdapterError,
            "nothing was sent",
        ):
            build_model_request(
                goal="test",
                context={
                    "large": (
                        "x"
                        * MAX_MODEL_REQUEST_BYTES
                    ),
                },
                model="test-model",
            )

    def test_transport_failure_is_attempted_once(self):
        calls = []

        def failing_opener(
            request,
            *,
            timeout,
        ):
            calls.append(
                (
                    request,
                    timeout,
                )
            )

            raise URLError(
                "expected local failure"
            )

        with self.assertRaisesRegex(
            LabModelAdapterError,
            "no automatic retry",
        ):
            request_model_decision(
                goal="test",
                context={},
                model="test-model",
                opener=failing_opener,
            )

        self.assertEqual(
            len(calls),
            1,
        )

    def test_invalid_input_fails_before_network(self):
        opener = CapturingOpener(
            ollama_response(
                {
                    "decision": "done",
                    "summary": "unused",
                }
            )
        )

        invalid_cases = (
            {
                "goal": "",
                "context": {},
                "model": "test-model",
                "timeout_seconds": 10,
            },
            {
                "goal": "test",
                "context": [],
                "model": "test-model",
                "timeout_seconds": 10,
            },
            {
                "goal": "test",
                "context": {},
                "model": "",
                "timeout_seconds": 10,
            },
            {
                "goal": "test",
                "context": {},
                "model": "test-model",
                "timeout_seconds": True,
            },
            {
                "goal": "test",
                "context": {},
                "model": "test-model",
                "timeout_seconds": 0,
            },
        )

        for case in invalid_cases:
            with self.subTest(
                case=case
            ):
                with self.assertRaises(
                    LabModelAdapterError
                ):
                    request_model_decision(
                        opener=opener,
                        **case,
                    )

        self.assertEqual(
            opener.calls,
            [],
        )


if __name__ == "__main__":
    unittest.main()
