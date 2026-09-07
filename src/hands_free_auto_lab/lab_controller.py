"""Bounded autonomous control loop for Hands-Free Auto Lab.

The model proposes.
Deterministic policy validates.
Dedicated deterministic executors act.
Evidence returns to the next model turn.

This controller grants no authority outside the disposable lab workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Callable

from .lab_action import LabAction
from .lab_acceptance import (
    LabAcceptanceError,
    LabAcceptanceResult,
    execute_lab_acceptance,
    seed_lab_acceptance_test,
)
from .lab_executor import (
    LabExecutionResult,
    LabExecutorError,
    execute_lab_run,
)
from .lab_model_adapter import (
    LabModelAdapterError,
    LabModelTurn,
    request_model_decision,
)
from .lab_read_file import (
    LabReadFileError,
    LabReadFileResult,
    execute_lab_read_file,
)
from .lab_workspace import LabWorkspace
from .lab_write_file import (
    LabWriteFileError,
    LabWriteFileResult,
    execute_lab_write_file,
)


CONTROLLER_COMPONENT = "hands-free-auto-lab-controller-v1"

DEFAULT_MAX_ITERATIONS = 20
MAX_MAX_ITERATIONS = 20

DEFAULT_RUN_TIMEOUT_SECONDS = 10
DEFAULT_MODEL_TIMEOUT_SECONDS = 120

CONTEXT_HISTORY_LIMIT = 3
CONTEXT_TEXT_PREVIEW_BYTES = 4 * 1024
CURRENT_FILE_PREVIEW_BUDGET_BYTES = 4 * 1024


class LabControllerError(RuntimeError):
    """Raised when controller configuration itself is invalid."""


@dataclass(frozen=True, slots=True)
class LabControllerStep:
    iteration: int
    model_text: str
    decision: str
    action: LabAction | None
    result: (
        LabReadFileResult
        | LabWriteFileResult
        | LabExecutionResult
        | None
    )
    error: str | None
    completion_refused_reason: str | None
    acceptance: LabAcceptanceResult | None = None


@dataclass(frozen=True, slots=True)
class LabControllerResult:
    component: str
    status: str
    goal: str
    model: str
    iterations: int
    workspace_generation: int
    tested_generation: int | None
    acceptance_generation: int | None
    summary: str | None
    error: str | None
    steps: tuple[LabControllerStep, ...]

    @property
    def succeeded(self) -> bool:
        return self.status == "done"


ModelRequester = Callable[..., LabModelTurn]


def _validate_positive_integer(
    value: object,
    *,
    field: str,
    maximum: int | None = None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
    ):
        raise LabControllerError(
            f"{field} must be a positive integer"
        )

    if (
        maximum is not None
        and value > maximum
    ):
        raise LabControllerError(
            f"{field} exceeds the enabled maximum"
        )

    return value


def _require_text(
    value: object,
    *,
    field: str,
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
    ):
        raise LabControllerError(
            f"{field} must be a non-empty string"
        )

    return value


def _preview_text(
    value: str,
) -> dict[str, object]:
    raw = value.encode(
        "utf-8"
    )

    if len(raw) <= CONTEXT_TEXT_PREVIEW_BYTES:
        return {
            "byte_count": len(raw),
            "preview_truncated": False,
            "text": value,
        }

    preview_raw = raw[
        :CONTEXT_TEXT_PREVIEW_BYTES
    ]

    return {
        "byte_count": len(raw),
        "preview_truncated": True,
        "text": preview_raw.decode(
            "utf-8",
            errors="replace",
        ),
    }


def _current_file_previews(
    *,
    known_write_hashes: dict[str, str],
    known_write_contents: dict[str, str],
) -> dict[str, dict[str, object]]:
    """Return deterministic bounded previews of controller-written files."""
    remaining = CURRENT_FILE_PREVIEW_BUDGET_BYTES

    previews: dict[
        str,
        dict[str, object],
    ] = {}

    for path in sorted(
        known_write_hashes
    ):
        content = known_write_contents.get(
            path
        )

        if content is None:
            continue

        raw = content.encode(
            "utf-8"
        )

        preview_raw = raw[
            :remaining
        ]

        preview_text = preview_raw.decode(
            "utf-8",
            errors="ignore",
        )

        preview_bytes = len(
            preview_text.encode(
                "utf-8"
            )
        )

        previews[path] = {
            "sha256": known_write_hashes[
                path
            ],
            "byte_count": len(raw),
            "preview_truncated": (
                preview_bytes
                < len(raw)
            ),
            "text": preview_text,
        }

        remaining = max(
            0,
            remaining - preview_bytes,
        )

    return previews


def _action_context(
    action: LabAction | None,
) -> dict[str, object] | None:
    if action is None:
        return None

    if action.kind == "READ_FILE":
        return {
            "kind": action.kind,
            "path": action.path,
        }

    if action.kind == "WRITE_FILE":
        content = (
            action.content
            if action.content is not None
            else ""
        )

        raw = content.encode(
            "utf-8"
        )

        return {
            "kind": action.kind,
            "path": action.path,
            "content_byte_count": len(raw),
            "content_sha256": hashlib.sha256(
                raw
            ).hexdigest(),
        }

    return {
        "kind": action.kind,
        "argv": list(
            action.argv
        ),
    }


def _result_context(
    result: (
        LabReadFileResult
        | LabWriteFileResult
        | LabExecutionResult
        | None
    ),
) -> dict[str, object] | None:
    if result is None:
        return None

    if isinstance(
        result,
        LabReadFileResult,
    ):
        return {
            "kind": "READ_FILE",
            "succeeded": result.succeeded,
            "path": result.path,
            "bytes_read": result.bytes_read,
            "sha256": result.sha256,
            "content": _preview_text(
                result.content
            ),
        }

    if isinstance(
        result,
        LabWriteFileResult,
    ):
        return {
            "kind": "WRITE_FILE",
            "succeeded": result.succeeded,
            "path": result.path,
            "bytes_written": result.bytes_written,
            "sha256": result.sha256,
            "replaced_existing": result.replaced_existing,
        }

    return {
        "kind": "RUN",
        "succeeded": result.succeeded,
        "timeout_seconds": result.timeout_seconds,
        "timed_out": result.timed_out,
        "output_limit_exceeded": (
            result.output_limit_exceeded
        ),
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "exit_status": result.exit_status,
        "signal": result.signal,
        "stdout": _preview_text(
            result.stdout
        ),
        "stderr": _preview_text(
            result.stderr
        ),
    }


def _acceptance_context(
    result: LabAcceptanceResult,
) -> dict[str, object]:
    return {
        "kind": "ACCEPTANCE",
        "succeeded": result.succeeded,
        "path": result.path,
        "expected_sha256": result.expected_sha256,
        "observed_sha256": result.observed_sha256,
        "run": _result_context(
            result.run_result
        ),
    }


def _step_context(
    step: LabControllerStep,
) -> dict[str, object]:
    context = {
        "iteration": step.iteration,
        "decision": step.decision,
        "action": _action_context(
            step.action
        ),
        "result": _result_context(
            step.result
        ),
        "error": step.error,
        "completion_refused_reason": (
            step.completion_refused_reason
        ),
    }

    return context


def _build_context(
    *,
    iteration: int,
    max_iterations: int,
    workspace_generation: int,
    tested_generation: int | None,
    acceptance_enabled: bool,
    acceptance_generation: int | None,
    acceptance_sha256: str | None,
    acceptance_evidence_generation: int | None,
    acceptance_evidence_result: LabAcceptanceResult | None,
    known_write_hashes: dict[str, str],
    known_write_contents: dict[str, str],
    steps: list[LabControllerStep],
) -> dict[str, object]:
    context = {
        "iteration": iteration,
        "max_iterations": max_iterations,
        "workspace_generation": workspace_generation,
        "tested_generation": tested_generation,
        "current_generation_has_successful_run": (
            tested_generation
            == workspace_generation
        ),
        "controller_known_written_files": dict(
            sorted(
                known_write_hashes.items()
            )
        ),
        "controller_current_file_previews": (
            _current_file_previews(
                known_write_hashes=known_write_hashes,
                known_write_contents=known_write_contents,
            )
        ),
        "recent_steps": [
            _step_context(
                step
            )
            for step in steps[
                -CONTEXT_HISTORY_LIMIT:
            ]
        ],
    }

    if acceptance_enabled:
        context.update(
            {
                "acceptance_enabled": True,
                "acceptance_generation": acceptance_generation,
                "acceptance_test_sha256": acceptance_sha256,
                "current_generation_has_successful_acceptance": (
                    acceptance_generation
                    == workspace_generation
                ),
                "latest_acceptance_evidence": (
                    None
                    if acceptance_evidence_result is None
                    else {
                        "workspace_generation": (
                            acceptance_evidence_generation
                        ),
                        "result": _acceptance_context(
                            acceptance_evidence_result
                        ),
                    }
                ),
            }
        )

    return context


def _error_result(
    *,
    goal: str,
    model: str,
    workspace_generation: int,
    tested_generation: int | None,
    steps: list[LabControllerStep],
    error: str,
    acceptance_generation: int | None = None,
) -> LabControllerResult:
    return LabControllerResult(
        component=CONTROLLER_COMPONENT,
        status="error",
        goal=goal,
        model=model,
        iterations=len(steps),
        workspace_generation=workspace_generation,
        tested_generation=tested_generation,
        acceptance_generation=acceptance_generation,
        summary=None,
        error=error,
        steps=tuple(
            steps
        ),
    )


def run_auto_lab(
    *,
    goal: object,
    model: object,
    workspace_parent: str,
    workspace: LabWorkspace,
    acceptance_test: object | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    run_timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS,
    model_timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS,
    requester: ModelRequester = request_model_decision,
) -> LabControllerResult:
    """Run one bounded autonomous Auto Lab session."""
    goal_text = _require_text(
        goal,
        field="goal",
    )

    model_text = _require_text(
        model,
        field="model",
    ).strip()

    max_iterations = _validate_positive_integer(
        max_iterations,
        field="max_iterations",
        maximum=MAX_MAX_ITERATIONS,
    )

    run_timeout_seconds = _validate_positive_integer(
        run_timeout_seconds,
        field="run_timeout_seconds",
        maximum=30,
    )

    model_timeout_seconds = _validate_positive_integer(
        model_timeout_seconds,
        field="model_timeout_seconds",
    )

    acceptance_text: str | None = None

    if acceptance_test is not None:
        acceptance_text = _require_text(
            acceptance_test,
            field="acceptance_test",
        )

    steps: list[LabControllerStep] = []

    workspace_generation = 0
    tested_generation: int | None = None
    tested_action_id: str | None = None
    failed_run_action_ids: set[str] = set()
    acceptance_generation: int | None = None
    acceptance_sha256: str | None = None

    acceptance_evidence_generation: int | None = None
    acceptance_evidence_result: LabAcceptanceResult | None = None

    known_write_hashes: dict[str, str] = {}
    known_write_contents: dict[str, str] = {}

    if acceptance_text is not None:
        try:
            acceptance_seed = seed_lab_acceptance_test(
                content=acceptance_text,
                workspace_parent=workspace_parent,
                workspace=workspace,
            )
        except LabAcceptanceError as exc:
            return _error_result(
                goal=goal_text,
                model=model_text,
                workspace_generation=workspace_generation,
                tested_generation=tested_generation,
                steps=steps,
                acceptance_generation=acceptance_generation,
                error=(
                    "acceptance setup failed closed: "
                    f"{exc}"
                ),
            )

        acceptance_sha256 = acceptance_seed.sha256

    for iteration in range(
        1,
        max_iterations + 1,
    ):
        context = _build_context(
            iteration=iteration,
            max_iterations=max_iterations,
            workspace_generation=workspace_generation,
            tested_generation=tested_generation,
            acceptance_enabled=(
                acceptance_text is not None
            ),
            acceptance_generation=acceptance_generation,
            acceptance_sha256=acceptance_sha256,
            acceptance_evidence_generation=(
                acceptance_evidence_generation
            ),
            acceptance_evidence_result=(
                acceptance_evidence_result
            ),
            known_write_hashes=known_write_hashes,
            known_write_contents=known_write_contents,
            steps=steps,
        )

        try:
            turn = requester(
                goal=goal_text,
                context=context,
                model=model_text,
                timeout_seconds=model_timeout_seconds,
            )

        except LabModelAdapterError as exc:
            return _error_result(
                goal=goal_text,
                model=model_text,
                workspace_generation=workspace_generation,
                tested_generation=tested_generation,
                steps=steps,
                acceptance_generation=acceptance_generation,
                error=(
                    "model decision failed closed: "
                    f"{exc}"
                ),
            )

        decision = turn.decision

        if decision.is_done:
            if (
                tested_generation
                != workspace_generation
            ):
                reason = (
                    "DONE refused: the current workspace "
                    "generation has no successful RUN evidence"
                )

                steps.append(
                    LabControllerStep(
                        iteration=iteration,
                        model_text=turn.text,
                        decision="done_refused",
                        action=None,
                        result=None,
                        error=None,
                        completion_refused_reason=reason,
                    )
                )

                continue

            if (
                acceptance_text is not None
                and acceptance_generation
                != workspace_generation
            ):
                reason = (
                    "DONE refused: the current workspace "
                    "generation has no successful controller "
                    "acceptance evidence"
                )

                steps.append(
                    LabControllerStep(
                        iteration=iteration,
                        model_text=turn.text,
                        decision="done_refused",
                        action=None,
                        result=None,
                        error=None,
                        completion_refused_reason=reason,
                    )
                )

                continue

            steps.append(
                LabControllerStep(
                    iteration=iteration,
                    model_text=turn.text,
                    decision="done",
                    action=None,
                    result=None,
                    error=None,
                    completion_refused_reason=None,
                )
            )

            return LabControllerResult(
                component=CONTROLLER_COMPONENT,
                status="done",
                goal=goal_text,
                model=model_text,
                iterations=len(steps),
                workspace_generation=workspace_generation,
                tested_generation=tested_generation,
                acceptance_generation=acceptance_generation,
                summary=decision.summary,
                error=None,
                steps=tuple(
                    steps
                ),
            )

        action = decision.action

        if action is None:
            return _error_result(
                goal=goal_text,
                model=model_text,
                workspace_generation=workspace_generation,
                tested_generation=tested_generation,
                steps=steps,
                acceptance_generation=acceptance_generation,
                error=(
                    "model produced an action decision "
                    "without an action"
                ),
            )

        acceptance_result: LabAcceptanceResult | None = None

        try:
            if action.kind == "READ_FILE":
                result = execute_lab_read_file(
                    action,
                    workspace_parent=workspace_parent,
                    workspace=workspace,
                )

            elif action.kind == "WRITE_FILE":
                write_content = (
                    action.content
                    if action.content is not None
                    else ""
                )

                proposed_sha256 = hashlib.sha256(
                    write_content.encode(
                        "utf-8"
                    )
                ).hexdigest()

                if (
                    action.path is not None
                    and known_write_hashes.get(
                        action.path
                    )
                    == proposed_sha256
                ):
                    reason = (
                        "WRITE_FILE refused as no-op: "
                        "controller evidence says this exact "
                        "content is already current for the path"
                    )

                    if (
                        acceptance_text is not None
                        and acceptance_generation
                        != workspace_generation
                    ):
                        assert acceptance_sha256 is not None

                        if (
                            acceptance_evidence_generation
                            == workspace_generation
                            and acceptance_evidence_result
                            is not None
                        ):
                            acceptance_result = (
                                acceptance_evidence_result
                            )

                        else:
                            try:
                                acceptance_result = (
                                    execute_lab_acceptance(
                                        expected_sha256=(
                                            acceptance_sha256
                                        ),
                                        workspace_parent=(
                                            workspace_parent
                                        ),
                                        workspace=workspace,
                                        timeout_seconds=(
                                            run_timeout_seconds
                                        ),
                                    )
                                )
                            except LabAcceptanceError as exc:
                                steps.append(
                                    LabControllerStep(
                                        iteration=iteration,
                                        model_text=turn.text,
                                        decision="acceptance_error",
                                        action=action,
                                        result=None,
                                        error=str(exc),
                                        completion_refused_reason=None,
                                    )
                                )

                                return _error_result(
                                    goal=goal_text,
                                    model=model_text,
                                    workspace_generation=(
                                        workspace_generation
                                    ),
                                    tested_generation=(
                                        tested_generation
                                    ),
                                    steps=steps,
                                    acceptance_generation=(
                                        acceptance_generation
                                    ),
                                    error=(
                                        "diagnostic controller "
                                        "acceptance failed closed: "
                                        f"{exc}"
                                    ),
                                )

                            acceptance_evidence_generation = (
                                workspace_generation
                            )

                            acceptance_evidence_result = (
                                acceptance_result
                            )

                    steps.append(
                        LabControllerStep(
                            iteration=iteration,
                            model_text=turn.text,
                            decision="action_refused",
                            action=action,
                            result=None,
                            error=reason,
                            completion_refused_reason=None,
                            acceptance=acceptance_result,
                        )
                    )

                    continue

                result = execute_lab_write_file(
                    action,
                    workspace_parent=workspace_parent,
                    workspace=workspace,
                )

                known_write_hashes[
                    result.path
                ] = result.sha256

                known_write_contents[
                    result.path
                ] = write_content

                workspace_generation += 1

                failed_run_action_ids.clear()
                tested_action_id = None
                acceptance_evidence_generation = None
                acceptance_evidence_result = None

            elif action.kind == "RUN":
                if (
                    tested_generation
                    == workspace_generation
                    and tested_action_id
                    == action.action_id
                ):
                    reason = (
                        "RUN refused as redundant: this exact "
                        "RUN already succeeded for the current "
                        "unchanged workspace generation"
                    )

                    if (
                        acceptance_evidence_generation
                        == workspace_generation
                        and acceptance_evidence_result
                        is not None
                    ):
                        acceptance_result = (
                            acceptance_evidence_result
                        )

                    steps.append(
                        LabControllerStep(
                            iteration=iteration,
                            model_text=turn.text,
                            decision="action_refused",
                            action=action,
                            result=None,
                            error=reason,
                            completion_refused_reason=None,
                            acceptance=acceptance_result,
                        )
                    )

                    continue

                if action.action_id in failed_run_action_ids:
                    reason = (
                        "RUN refused as unchanged failed retry: "
                        "this exact RUN already failed for the "
                        "current unchanged workspace generation; "
                        "modify the workspace or choose a materially "
                        "different diagnostic action before retrying"
                    )

                    steps.append(
                        LabControllerStep(
                            iteration=iteration,
                            model_text=turn.text,
                            decision="action_refused",
                            action=action,
                            result=None,
                            error=reason,
                            completion_refused_reason=None,
                            acceptance=acceptance_result,
                        )
                    )

                    continue

                result = execute_lab_run(
                    action,
                    workspace_parent=workspace_parent,
                    workspace=workspace,
                    timeout_seconds=run_timeout_seconds,
                )

                if not result.succeeded:
                    failed_run_action_ids.add(
                        action.action_id
                    )

                if result.succeeded:
                    tested_generation = (
                        workspace_generation
                    )

                    tested_action_id = (
                        action.action_id
                    )

                    if (
                        acceptance_text is not None
                        and acceptance_generation
                        != workspace_generation
                    ):
                        assert acceptance_sha256 is not None

                        try:
                            acceptance_result = execute_lab_acceptance(
                                expected_sha256=acceptance_sha256,
                                workspace_parent=workspace_parent,
                                workspace=workspace,
                                timeout_seconds=run_timeout_seconds,
                            )
                        except LabAcceptanceError as exc:
                            steps.append(
                                LabControllerStep(
                                    iteration=iteration,
                                    model_text=turn.text,
                                    decision="acceptance_error",
                                    action=action,
                                    result=result,
                                    error=str(exc),
                                    completion_refused_reason=None,
                                )
                            )

                            return _error_result(
                                goal=goal_text,
                                model=model_text,
                                workspace_generation=workspace_generation,
                                tested_generation=tested_generation,
                                steps=steps,
                                acceptance_generation=acceptance_generation,
                                error=(
                                    "controller acceptance "
                                    "failed closed: "
                                    f"{exc}"
                                ),
                            )

                        acceptance_evidence_generation = (
                            workspace_generation
                        )

                        acceptance_evidence_result = (
                            acceptance_result
                        )

                        if acceptance_result.succeeded:
                            acceptance_generation = (
                                workspace_generation
                            )

            else:
                return _error_result(
                    goal=goal_text,
                    model=model_text,
                    workspace_generation=workspace_generation,
                    tested_generation=tested_generation,
                    steps=steps,
                    acceptance_generation=acceptance_generation,
                    error=(
                        "controller received unsupported "
                        f"action kind: {action.kind}"
                    ),
                )

        except (
            LabReadFileError,
            LabWriteFileError,
            LabExecutorError,
        ) as exc:
            steps.append(
                LabControllerStep(
                    iteration=iteration,
                    model_text=turn.text,
                    decision="action_error",
                    action=action,
                    result=None,
                    error=str(
                        exc
                    ),
                    completion_refused_reason=None,
                )
            )

            return _error_result(
                goal=goal_text,
                model=model_text,
                workspace_generation=workspace_generation,
                tested_generation=tested_generation,
                steps=steps,
                acceptance_generation=acceptance_generation,
                error=(
                    f"{action.kind} failed closed: {exc}"
                ),
            )

        steps.append(
            LabControllerStep(
                iteration=iteration,
                model_text=turn.text,
                decision="action",
                action=action,
                result=result,
                error=None,
                completion_refused_reason=None,
                acceptance=acceptance_result,
            )
        )

        if (
            action.kind == "RUN"
            and acceptance_text is not None
            and tested_generation == workspace_generation
            and acceptance_generation == workspace_generation
        ):
            return LabControllerResult(
                component=CONTROLLER_COMPONENT,
                status="done",
                goal=goal_text,
                model=model_text,
                iterations=len(steps),
                workspace_generation=workspace_generation,
                tested_generation=tested_generation,
                acceptance_generation=acceptance_generation,
                summary=None,
                error=None,
                steps=tuple(
                    steps
                ),
            )

    return LabControllerResult(
        component=CONTROLLER_COMPONENT,
        status="iteration_limit",
        goal=goal_text,
        model=model_text,
        iterations=len(steps),
        workspace_generation=workspace_generation,
        tested_generation=tested_generation,
        acceptance_generation=acceptance_generation,
        summary=None,
        error="maximum model-decision count reached",
        steps=tuple(
            steps
        ),
    )
