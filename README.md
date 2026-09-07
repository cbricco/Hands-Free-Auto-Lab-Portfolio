# Hands-Free Auto Lab

Hands-Free Auto Lab is an **AI-assisted personal coding-automation project** exploring how AI can perform useful coding work while deterministic controls and explicit human decisions still limit what it is allowed to change.

I started it to experiment with speeding up development of another personal project, Upgrade Life, without giving an AI unrestricted authority over my normal development environment.

This repository is a **curated employer-facing snapshot** of an evolving personal project. It is not a production system, commercial deployment, or claim of independently authored software.

## What It Is

Auto Lab explores a bounded workflow:

1. A human provides a development goal.
2. Work is performed inside a constrained development workspace.
3. AI reasoning proposes what should happen.
4. Deterministic code checks whether the proposed action is eligible.
5. The allowed action executes under controlled conditions.
6. Evidence is captured.
7. The result becomes something reviewable rather than automatically trusted.
8. Important transitions toward real project state remain separately controlled.

Core principle:

> Model output may propose work, but it is not permission to expand authority or silently promote changes.

## Why I Built It

I wanted to explore whether AI-assisted coding could reduce repetitive development work while still preserving:

- bounded authority;
- isolation from unrelated files and projects;
- explicit review;
- fail-closed behavior;
- testing and verification;
- recovery and rollback;
- separation between development work and stable project state.

## What Exists Today

At the checkpoint represented here, the project contains evolving implementations and tests for:

- bounded coding-job intake;
- durable job lifecycle, queue, and status handling;
- isolated worker and coding-candidate workflows;
- deterministic change-policy validation;
- review-packet construction;
- controlled promotion/application;
- durable transaction state;
- recovery-material handling;
- rollback planning and execution;
- failure and crash-path verification.

These components are still being developed and hardened.

This should **not** be interpreted as a finished autonomous coding platform, production security system, or autonomous production-development service.

## My Role vs AI's Role

This project is heavily AI-assisted.

### My role

I primarily:

- decided why the project should exist;
- defined the desired workflow and user experience;
- specified safety and approval boundaries;
- required sandboxing and isolation;
- required fail-closed behavior;
- decided how stable and development workflows should be separated;
- reviewed test and verification evidence;
- accepted or rejected proposed progress;
- made high-level product and workflow decisions.

### AI's role

AI generated essentially all of:

- implementation code;
- tests;
- debugging work;
- detailed technical architecture;
- many implementation-level design decisions;
- much of the technical documentation.

I do **not** present this as a codebase I independently authored. I am still learning the implementation in depth.

## Model-Driven vs Deterministic Control

The AI may reason about what change would be useful.

It is not intended to decide for itself that it may:

- access unrelated repositories;
- broaden filesystem access;
- use credentials;
- modify host configuration;
- install packages;
- bypass failed checks;
- silently retry with expanded authority;
- stage, commit, push, or publish work solely on model authority.

Those boundaries are intended to be defined and validated by deterministic software and explicit human decisions.

## Failure / Fail-Closed Behavior

A recurring design requirement is:

**uncertain state should stop the workflow rather than cause the executor to improvise.**

Tests cover cases including:

- unexpected changed paths;
- persistence failures;
- repository drift;
- unsafe filesystem state;
- interrupted promotion operations;
- partial rollback writes;
- filesystem synchronization failures;
- tampered rollback evidence;
- ambiguous recovery conditions.

## Recovery / Rollback

The snapshot includes promotion and rollback machinery intended to preserve enough evidence to handle bounded failures.

Recent development work focuses especially on failure boundaries around temporary rollback files, filesystem replacement, synchronization, tampering, and partial writes.

These are evolving mechanisms, not a claim of formally verified or production-grade recovery.

## Testing and Verification

The project contains a large Python `unittest` suite covering both successful behavior and refusal/failure paths.

Representative themes include:

- sandbox and workspace boundaries;
- exact change-policy enforcement;
- durable candidate creation;
- lifecycle persistence;
- promotion inspection;
- repository-state validation;
- rollback and recovery behavior.

Testing is especially important here because many desired properties are about what the automation must **refuse** to do.

## Technologies and Concepts

This project provides practical exposure to:

- Python;
- Linux;
- Bash/shell workflows;
- Git and GitHub concepts;
- automated testing;
- filesystem behavior;
- durable state and journaling concepts;
- isolation and sandboxing concepts;
- error handling;
- rollback and recovery concepts;
- AI-assisted software development.

My Git knowledge is best described as **working knowledge and developing**, not advanced expertise.

## Start Here

For a quick technical tour:

1. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
   Permanent trust-boundary and architecture requirements.

2. [`src/hands_free_auto_lab/lab_coding_integration.py`](src/hands_free_auto_lab/lab_coding_integration.py)
   Part of the bounded coding-request-to-candidate flow.

3. [`src/hands_free_auto_lab/lab_worker_change_policy.py`](src/hands_free_auto_lab/lab_worker_change_policy.py)
   Deterministic validation of worker changes.

4. [`src/hands_free_auto_lab/lab_promotion_executor.py`](src/hands_free_auto_lab/lab_promotion_executor.py)
   Controlled application/promotion logic.

5. [`tests/test_lab_promotion_rollback_executor.py`](tests/test_lab_promotion_rollback_executor.py)
   Representative rollback and recovery tests.

## Current Limitations

Important limitations:

- the project is experimental;
- the polished human review page is unfinished;
- the long-term voice workflow is unfinished;
- this is not autonomous production coding at scale;
- it is not a replacement for professional security review;
- networked dependency installation and broad host modification are intentionally outside the initial autonomous authority model;
- I am still building my own familiarity with the implementation.

## Snapshot Provenance

This employer-facing snapshot was derived from committed development checkpoint:

`1c582f3b8d29b319a67731c7da4958efc6d76bfd`

It intentionally excludes the source repository's Git history and excludes later staged or uncommitted development work.

Two local test-fixture home-directory strings were replaced with a neutral test path for portfolio presentation.

## AI-Assisted Development Disclosure

Hands-Free Auto Lab is a personal AI-assisted development project.

AI generated essentially all implementation code, tests, debugging work, and much of the detailed architecture. My contribution is primarily requirements, product direction, workflow design, safety and approval boundaries, review expectations, evidence review, and decisions about whether work should progress.
