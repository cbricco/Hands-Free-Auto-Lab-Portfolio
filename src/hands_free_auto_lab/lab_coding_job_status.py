"""Read-only status lookup for durable coding-job lifecycles.

This module adds no lifecycle state and performs no recovery, retry, resume,
worker execution, candidate promotion, repository mutation, or authority
decision. It only derives the existing deterministic job identity from an
exact request_id/run_id pair and asks the durable lifecycle journal for the
latest validated snapshot.
"""

from __future__ import annotations

import os

from .lab_coding_job_lifecycle import (
    LabCodingJobLifecycle,
    build_lab_coding_job_lifecycle,
)
from .lab_coding_job_lifecycle_journal import (
    load_lab_coding_job_lifecycle_journal,
)


def load_lab_coding_job_status(
    root: str | os.PathLike[str],
    *,
    request_id: object,
    run_id: object,
) -> LabCodingJobLifecycle:
    """Return the latest validated lifecycle snapshot for one execution attempt."""

    requested = build_lab_coding_job_lifecycle(
        request_id=request_id,
        run_id=run_id,
    )

    return load_lab_coding_job_lifecycle_journal(
        root,
        job_id=requested.job_id,
    )
