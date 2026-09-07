"""Controller-owned immutable acceptance checks for Auto Lab.

Acceptance code is seeded before model execution, cannot be overwritten by
model WRITE_FILE policy, and executes through the existing read-only RUN
sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lab_action import build_lab_action
from .lab_executor import (
    LabExecutionResult,
    LabExecutorError,
    execute_lab_run,
)
from .lab_policy import (
    PYTHON,
    RESERVED_ACCEPTANCE_PATH,
    RESERVED_ACCEPTANCE_SANDBOX_PATH,
)
from .lab_read_file import (
    LabReadFileError,
    execute_lab_read_file,
)
from .lab_workspace import LabWorkspace
from .lab_write_file import (
    LabWriteFileError,
    LabWriteFileResult,
    seed_reserved_acceptance_file,
)


ACCEPTANCE_COMPONENT = (
    "hands-free-auto-lab-acceptance-v1"
)


class LabAcceptanceError(RuntimeError):
    """Raised when acceptance evidence cannot be trusted or executed."""


@dataclass(frozen=True, slots=True)
class LabAcceptanceResult:
    component: str
    path: str
    expected_sha256: str
    observed_sha256: str
    run_result: LabExecutionResult

    @property
    def succeeded(self) -> bool:
        return self.run_result.succeeded


def _validate_sha256(
    value: object,
) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
    ):
        raise LabAcceptanceError(
            "acceptance SHA-256 identity is invalid"
        )

    try:
        bytes.fromhex(
            value
        )
    except ValueError as exc:
        raise LabAcceptanceError(
            "acceptance SHA-256 identity is invalid"
        ) from exc

    return value


def seed_lab_acceptance_test(
    *,
    content: str,
    workspace_parent: str,
    workspace: LabWorkspace,
) -> LabWriteFileResult:
    """Seed the controller-owned acceptance source exactly once."""
    try:
        return seed_reserved_acceptance_file(
            content=content,
            workspace_parent=workspace_parent,
            workspace=workspace,
        )
    except LabWriteFileError as exc:
        raise LabAcceptanceError(
            f"cannot seed acceptance contract: {exc}"
        ) from exc


def execute_lab_acceptance(
    *,
    expected_sha256: str,
    workspace_parent: str,
    workspace: LabWorkspace,
    timeout_seconds: int,
) -> LabAcceptanceResult:
    """Verify immutable acceptance identity, then run it in the sandbox."""
    expected_sha256 = _validate_sha256(
        expected_sha256
    )

    read_action = build_lab_action(
        kind="READ_FILE",
        path=RESERVED_ACCEPTANCE_PATH,
    )

    try:
        read_result = execute_lab_read_file(
            read_action,
            workspace_parent=workspace_parent,
            workspace=workspace,
        )
    except LabReadFileError as exc:
        raise LabAcceptanceError(
            f"cannot verify acceptance contract: {exc}"
        ) from exc

    if read_result.sha256 != expected_sha256:
        raise LabAcceptanceError(
            "acceptance contract identity changed"
        )

    run_action = build_lab_action(
        kind="RUN",
        argv=(
            PYTHON,
            "-I",
            RESERVED_ACCEPTANCE_SANDBOX_PATH,
        ),
    )

    try:
        run_result = execute_lab_run(
            run_action,
            workspace_parent=workspace_parent,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
        )
    except LabExecutorError as exc:
        raise LabAcceptanceError(
            f"acceptance execution failed closed: {exc}"
        ) from exc

    return LabAcceptanceResult(
        component=ACCEPTANCE_COMPONENT,
        path=RESERVED_ACCEPTANCE_PATH,
        expected_sha256=expected_sha256,
        observed_sha256=read_result.sha256,
        run_result=run_result,
    )
