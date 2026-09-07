# Proposed First Bounded Real-Task Pilot

## Status and purpose

This document is a draft proposal for human review on 2026-08-22, not a statement of current project status. The pilot has not run. Overnight work remains unpromoted candidate evidence. Nothing in this proposal, including model output or a `coding_candidate`, is approval or authorization to execute it, mutate a repository, or promote a candidate. Human promotion approval remains mandatory, and any unresolved failure remains a blocker.

The first task should be deliberately harmless: improve the wording of one already identified sentence in one documentation file. Before setup, the human reviewer must name the exact real repository, exact attached pilot branch, exact starting HEAD, exact single documentation path, and exact requested replacement text. A suitable example is a one-sentence clarification in `README.md`; the example does not itself select a repository, path, or content.

The task grants no authority for arbitrary shell commands, accounts, credentials, messages, purchases, network access, package installation, services, devices, physical control, staging, commits, pushes, or any file outside the one approved documentation path.

## Pre-gates

All gates are conjunctive and fail closed. A human must record the gate evidence before any disposable worker starts.

1. A human selects the exact repository and creates or selects a dedicated pilot branch outside autonomous authority. The recorded canonical repository path, filesystem device and inode, attached branch name, and HEAD object ID become immutable pilot inputs.
2. The repository is a real, local Git worktree owned by the expected effective user; its root and `.git` are real directories, not symlinks. The repository is not the Auto Lab workspace and is not mounted writable in the worker sandbox.
3. The exact task is a non-executable UTF-8 documentation-only modification to one predeclared regular file. No add, delete, rename, mode change, generated file, dependency, configuration, source, test, submodule, hook, or Git metadata change is permitted.
4. The allowlist contains exactly one canonical repository-relative path, selected by the human. It must have no absolute form, `..`, alternate separator, symlink component, hard-link ambiguity, special-file type, or case/normalization ambiguity. The expected byte count, SHA-256, and mode of its starting preimage are recorded.
5. The worker profile is fixed and reviewed: no network, no credentials, no host home, no unrelated repositories, no package-management or service authority, no privileged devices, fixed resource/time/iteration limits, and only the disposable workspace writable.
6. Candidate persistence, read-only target inspection, proposal construction, operation-derived risk classification, approval consumption, transaction journaling, recovery-material handling, target verification, and rollback behavior have each passed their required tests in the actual execution environment. Any known test failure, incomplete orchestration, corrupted durable state, or unavailable secure primitive blocks the pilot.
7. A human confirms recovery storage is private and durable, has sufficient space, and is on an appropriate failure boundary. A practiced recovery procedure and named human operator are available before promotion is considered.

## Repository and branch cleanliness

At the initial read-only inspection and again immediately before promotion, all of the following must match the recorded inputs:

- the canonical repository path and its device/inode identity;
- a real `.git` directory and expected ownership;
- the exact attached pilot branch (no detached HEAD) and exact starting HEAD;
- an entirely clean index and worktree, with no staged, unstaged, untracked, renamed, deleted, conflicted, ignored-but-relevant, submodule, rebase, merge, cherry-pick, bisect, or other in-progress operation state;
- the exact allowed path is the same regular file with the recorded mode, byte count, SHA-256, link expectations, and safe path traversal;
- every proposed-absence claim remains absent, although this pilot should propose no added files.

The cleanliness check must use fixed read-only inspection and require empty machine-readable status output including all untracked files. It must not clean, stash, reset, checkout, switch branches, or repair the repository. Any mismatch or drift stops the pilot and invalidates the proposal and approval; a fresh inspection and new proposal are required.

## Disposable worker workspace

A deterministic seed copies only the minimum task inputs into a newly created disposable workspace whose session ID and physical device/inode identity are recorded. The seed snapshot binds the selected documentation file's content, hash, mode, and path. The real repository remains read-only and inaccessible to worker actions; real repositories are not autonomous worker targets.

The worker may perform only the bounded file-read and atomic file-write operations needed for the exact allowed path and fixed, non-networked verification commands explicitly admitted by deterministic policy. For this documentation task, verification should be read-only text checks; arbitrary shell is not part of the grant. Limits on actions, output, CPU, memory, elapsed time, and iterations are fixed before work. A policy refusal, timeout, unexpected output, sandbox degradation, or boundary uncertainty terminates the run without promotion.

## Physical diff and durable coding candidate

After the worker stops, trusted code independently captures the final workspace snapshot and derives the physical diff from the recorded before and after snapshots. The diff must contain exactly one `MODIFIED` entry for the human-selected documentation path. It must show unchanged non-executable mode, the exact before and after byte counts and SHA-256 values, and the complete UTF-8 final content. Any other entry, deletion, executable bit, group/other-writable mode, symlink, hard link, special file, or identity mismatch rejects the result.

Only that independently validated diff may become a durable `coding_candidate`. Its genuine candidate ID must bind the workspace session/device/inode, before and after snapshot IDs, physical-diff ID, operation, path, modes, content, and hashes. The canonical candidate record is persisted durably in the private candidate store and loaded back by its exact candidate ID before review.

The candidate is evidence only. It is never approval or authorization. Its candidate ID, physical-diff ID, content hash, or any other identifier must never be invented, relabeled, or mapped into a legacy `WRITE_FILE` action ID. Promotion provenance remains `coding_candidate` with the genuine candidate ID.

## Human review and explicit approval

The system presents, without mutation, a review packet containing:

- the original human task and exact one-path allowlist;
- worker/sandbox profile and termination evidence;
- workspace identity, snapshots, physical diff, candidate ID, and durable-load validation;
- canonical destination repository path/device/inode, attached branch, starting HEAD, and cleanliness evidence;
- exact preimage mode/size/hash and complete before/after text;
- proposal ID, genuine source kind and source ID, derived operation/risk classification, verification plan, recovery materials summary, and rollback/stop plan;
- all test results, including failures, plus every warning or unresolved limitation.

A human compares the complete one-sentence change with the stated task and independently decides. Approval must be an explicit `approve` decision on a fresh challenge bound to the exact proposal ID. It must yield a distinct short-lived, single-use approval ID bound to that proposal and the exact promotion run. Candidate evidence or model output cannot answer the challenge. Silence, partial review, expired approval, replay, identifier mismatch, or an approval for any other proposal is refusal.

## Controlled promotion

Promotion is a separate, human-initiated operation after approval; it is not performed by the autonomous worker. Before the first repository mutation, the controller must:

1. reload and validate the durable candidate, proposal, approval, transaction plan, journal, and recovery records;
2. re-inspect repository identity, branch, HEAD, total cleanliness, safe path, and exact preimage;
3. recompute risk from the proposed operation rather than provenance and require the approved proposal to match it;
4. durably create and fsync recovery material for the exact preimage and initialize the append-only transaction journal;
5. atomically consume the correct unexpired single-use approval for the exact proposal and run;
6. use only fixed file-install primitives for the one allowed path, with no model-selected shell or Git command and no expansion of scope.

If the currently implemented end-to-end application/verification/recovery orchestration has not completed review and environment validation by pilot time, the pilot stops after candidate review. Manual copying is not an acceptable bypass because it would evade the proposal, approval, preimage, journal, verification, and rollback boundaries.

## Verification

Immediately after installation, trusted read-only inspection must prove that repository path/device/inode, branch, and HEAD remain the approved values; no unexpected repository state appeared; the destination is the expected regular file with exact approved final content, byte count, SHA-256, and mode; no temporary artifact remains; and all non-allowed paths are unchanged according to the bounded evidence required by the promotion design.

Run only the predetermined documentation checks in the controlled verifier. Record stdout, stderr, exit status, timeout state, tool identity, and resulting repository cleanliness. Verification is successful only when every content, identity, scope, and check assertion passes. Success means the approved working-tree modification was installed and verified; it does not authorize staging, committing, pushing, merging, or further edits. Those remain separate human actions outside this pilot.

## Rollback and stop conditions

Before mutation, recovery material must bind the exact preimage bytes, hash, mode, repository identity, transaction, and destination. Every transition is appended durably to the journal. On a failed install or verification, the controller stops forward progress, inspects the exact applied state, constructs a deterministic rollback plan from validated transaction and recovery evidence, and restores the preimage using fixed primitives. It then verifies the restored hash/mode, absence of temporary files, original cleanliness, repository identity, branch, and HEAD, and records the terminal result.

Automatic rollback must refuse ambiguous state: unexpected destination content, unexpected temporary files, repository or branch/HEAD drift, missing/corrupt recovery material, journal inconsistency, link/type changes, concurrent modification, or an unplanable state requires containment and manual human recovery. It must not guess, overwrite unknown content, or broaden authority.

Stop without promotion on any failed pre-gate; identity, provenance, hash, mode, preimage, cleanliness, risk, proposal, approval, journal, or recovery mismatch; unexpected physical diff; sandbox or secure-primitive failure; test failure; timeout/resource limit; concurrent change; expired/replayed approval; verification failure; rollback uncertainty; or human rejection/abort. Preserve all evidence for review. Do not claim success unless verification and the durable terminal state both establish it.

## Tomorrow's review decision

The human review should choose one of three outcomes: reject the proposal, revise it and repeat all gates with new bound evidence, or authorize only candidate generation in the disposable lab. Authorization to generate a candidate is not authorization to promote it. A later promotion requires its own exact review and explicit proposal-bound approval after all controlled-promotion readiness gates are satisfied.
