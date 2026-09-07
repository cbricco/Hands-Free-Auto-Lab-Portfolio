# Hands-Free Auto Lab Architecture

## Document role

This document records permanent architecture and trust-boundary requirements,
not current implementation status. Current milestones, gaps, and validation
results belong in `PROJECT_STATUS.md`.

Auto Lab is a separate project. It must not weaken or bypass the approval
semantics of the original Hands-Free repository. Model output is never
authority, and any unresolved failure remains a blocker.

## Requirements

### Trust boundaries

The reasoning model is untrusted with respect to host authority.

The model may choose actions only inside an explicitly created disposable
lab workspace.

Deterministic software defines what resources the lab can access.

Model output is never permission to access resources outside the lab.

### Lab workspace

The lab workspace is writable and disposable.

The host outside the workspace is not a writable development target. Real
repositories are never autonomous worker targets.

The MVP must not expose:

- the user's normal home directory,
- SSH keys,
- Git credentials,
- authentication tokens,
- Tailscale credentials,
- host package-management authority,
- system services,
- privileged devices,
- unrelated repositories.

### Network

The MVP has no sandbox network access.

Networked dependency installation is outside the initial MVP.

### Major components

1. Goal/session state
2. Lab action representation
3. Deterministic lab policy
4. Disposable workspace manager
5. Sandbox executor
6. Execution evidence/audit record
7. Reasoning adapter
8. Bounded autonomous loop
9. Final result/diff reporter
10. Human promotion boundary

### Action lifecycle

Human provides goal.

The reasoning layer proposes an exact lab action.

Deterministic policy decides whether that action belongs to the permitted
lab subset.

If eligible, the sandbox executor executes exactly that action.

The executor captures complete evidence and does not invent repairs.

The reasoning layer receives the evidence and may propose another lab action.

The loop terminates on success, explicit failure, policy refusal, resource
limit, timeout, or iteration limit.

### Executor responsibility

The executor:

- executes exactly one eligible lab action,
- does not interpret authorization,
- does not improvise,
- captures stdout and stderr separately,
- records exit status,
- records timeout/interruption state,
- enforces resource limits,
- remains confined to the lab workspace.

### Audit record

Each iteration should eventually preserve at least:

- session ID,
- iteration number,
- exact action,
- policy decision,
- workspace identity,
- timestamp,
- executor identity/profile,
- exit status,
- stdout,
- stderr,
- timeout/interruption state,
- reasoning result or next-action decision.

### Failure behavior

Failures stop the current action.

There is no hidden executor retry.

The reasoning layer may request another lab action only after receiving the
record of the failed action.

Policy failures do not become execution attempts.

### Promotion boundary

Auto Lab never directly modifies the real destination repository.

Completed work is surfaced as reviewable output such as:

- diff,
- patch,
- file set,
- test evidence,
- build artifact.

Moving that result into a real repository is outside autonomous lab authority
and always requires a separate, explicit human promotion decision bound to the
exact proposal.

A `coding_candidate` crosses into promotion code only through a neutral,
validated promotion-source representation. Its genuine candidate ID records
provenance; it is not an approval, authorization, legacy `WRITE_FILE` action
ID, or substitute for missing legacy provenance. Candidate identity, physical
diff evidence, repository identity, destination preimages, derived risk, and
the exact human-approved proposal remain independently validated. Transaction
journaling, recovery, verification, and rollback are separate fail-closed
authority boundaries.

## Initial MVP

The first useful proof is deliberately small:

Given a disposable copy of a tiny Python project with failing tests and a
human goal such as "make all tests pass":

1. inspect files,
2. make a bounded edit inside the lab,
3. run tests,
4. capture evidence,
5. reason over the result,
6. make another bounded edit if necessary,
7. stop when tests pass or the iteration limit is reached,
8. present the final diff and evidence.

No phone UI, voice interface, SSH automation, package installation, Git push,
host modification, or promotion automation belongs in this proof.

## Implementation direction

Reuse concepts from Hands-Free rather than bypassing its approval machinery.

Useful existing patterns include:

- Bubblewrap isolation,
- fixed executable identity,
- cleared environment,
- process/resource limits,
- timeout termination,
- separate stdout/stderr capture,
- strict evidence validation,
- reasoning transport separation.

Approval-specific run authority, approval stores, approval claims, and the
browser approval UI are not part of the Auto Lab execution authority model.
