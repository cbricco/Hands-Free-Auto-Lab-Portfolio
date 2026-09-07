# Project Status

## Current state

Hands-Free Auto Lab is an experimental, separate development system for bounded autonomous work inside disposable, isolated workspaces. Model output is not authority. The system can represent deterministic actions, enforce lab policy, execute eligible work in a sandbox, capture evidence, run bounded controller loops, and produce independently validated coding-candidate evidence.

Overnight results remain candidate evidence until separately reviewed and explicitly human-approved for promotion. Human promotion remains mandatory and must be a separate, explicit decision bound to the exact proposal. Model output is never authority, and `coding_candidate` provenance is evidence only, never approval or authorization. Real repositories are not autonomous worker targets. Phase 1 is now the first completed production-integration exercise: the exact 30-path promotion was separately human-approved and applied to this real working tree, followed by a separately reviewed two-test-file fixture repair. The integration reached verified pre-commit signoff while still unstaged, uncommitted, and unpushed. Any unresolved failure remains a blocker.

## Verified milestones

The implementation and test suite currently cover:

- deterministic action identities, policy eligibility, workspace identity, workspace snapshots, physical diffs, and bounded loop behavior;
- isolated file reads, atomic bounded writes, sandboxed test execution, resource limits, timeout handling, audit evidence, and fail-closed worker adapters;
- independently derived immutable coding candidates whose IDs bind workspace identity, snapshots, physical diff, operations, modes, content, and hashes;
- a neutral promotion-source boundary supporting genuine legacy write-action provenance and coding-candidate provenance without treating either as authority;
- read-only destination inspection binding canonical repository path, device/inode, branch, head, cleanliness, file type, mode, and exact preimage or absence evidence;
- immutable promotion proposals and transaction snapshots, with operation-derived risk classification and provenance preserved in their identities;
- explicit human promotion challenges, decisions, short-lived approvals, single-use consumption, and durable terminal records bound to exact proposal and run identities;
- transaction journaling, target preparation, applied/prepared-state inspection, recovery-material collection, rollback planning, legal state transitions, and fail-closed drift detection.

The full automated suite is the verification authority for this checkpoint; see the validation note below.

## Unresolved work and blockers

Every item in this section remains a blocker for any operation that depends on it; no candidate or model output can waive a blocker.

- Continue hardening and reviewing the end-to-end promotion application, verification, recovery, and rollback orchestration without merging currently separated authority boundaries; the Phase 1 path has now been exercised successfully on the real working tree.
- Demonstrate crash recovery across every mutation boundary with durable integration evidence and human-reviewed operational procedures.
- Define a human-facing review and approval workflow that clearly presents exact repository identity, proposal contents, risk, preimages, and test evidence.
- Continue adversarial testing of filesystem races, repository drift, durable-store corruption, sandbox escape attempts, and resource exhaustion.
- Keep networked dependency installation, host modification, and unrelated persistent state outside the initial autonomous lab authority.

## Proposed first real-task pilot

A documentation-only first pilot has been drafted for human review on 2026-08-22 in `docs/CODING_CANDIDATE_PILOT.md`. It proposes a one-sentence change to one human-selected documentation file, first produced only in a disposable worker workspace. It specifies fail-closed pre-gates, exact clean repository/branch and preimage requirements, an independently derived physical diff, durable genuine `coding_candidate` provenance, a complete human review packet, separate explicit proposal-bound approval, controlled promotion, verification, and deterministic recovery/rollback conditions.

The proposal has not run and grants no authority tonight. It does not select a real repository or final task, approve a candidate, or authorize promotion, arbitrary shell, accounts, messages, purchases, network access, devices, or physical control. If end-to-end promotion orchestration and its environment-specific tests are not ready, the proposed pilot stops after candidate review rather than bypassing a boundary.

## Validation

Phase 1 production signoff was verified against `main` at HEAD `903959ec08f00afdd1d98549b3c6f8f9dd921c88`. After the separately approved host-umask test-fixture repair, the focused gate ran 143 tests with 0 failures and 0 errors. The complete real-host suite then discovered and ran exactly 520 tests with 0 failures, 0 errors, and 0 skipped tests. `git diff --check` passed, and the working-tree path/status set remained exactly 30 throughout verification. The host-umask repair changed only test fixtures to create the affected `alpha.txt` files as mode `0644`; the hardened production destination-inspection rule was not weakened. The earlier contained-environment baseline of 520 discovered tests with the 28 known nested-Bubblewrap namespace failures and 0 errors remains preserved as environmental evidence. The Phase 1 integration and repair reached verified pre-commit signoff while still unstaged, uncommitted, and unpushed; earlier failed and repaired Phase 1 attempts remain preserved separately as audit evidence.

## August 31, 2026 — Bounded MODIFY promotion apply/verify executor

- Added `lab_promotion_executor.py` as the first deterministic production
  application/verification owner for an already-prepared promotion.
- The executor does not create, decide, or consume approval authority. It requires
  exact `LabPromotionTargetPreparationResult` evidence from the existing
  single-use consumed-approval preparation boundary and re-queries that durable
  consumed approval before entering mutation.
- This first application slice supports `MODIFY` only. `ADD` fails closed before
  transaction or repository mutation until an atomic no-clobber installation
  primitive receives separate review.
- The executor durably enters `APPLYING` before the first target mutation, installs
  one exact prepared after-image at a time through same-directory descriptor-bound
  `os.replace()`, fsyncs the parent, inspects the physical result, and journals
  that file `INSTALLED`.
- After all files are installed it enters `VERIFYING`, reuses the existing
  read-only applied-target inspector for exact after-image evidence, journals each
  file `VERIFIED`, and reaches `COMPLETED` only when every file is verified.
- Any ambiguous failure after an active transaction begins is preserved rather
  than retried or cleaned. The executor attempts to durably terminalize the
  transaction as `RECOVERY_REQUIRED` while retaining the current per-file
  progress. It performs no automatic rollback, temporary deletion, staging,
  commit, push, or retry.
- Exact destination and temporary files are re-opened beneath descriptor-bound,
  no-follow repository parents immediately before replacement and must match the
  transaction byte count, SHA-256, mode, owner, regular-file type, and single-link
  expectations. The repository root and every traversed parent directory must be
  current-user-owned and not group- or other-writable before `APPLYING` and are
  checked again immediately at installation time. Before `APPLYING`, the executor
  also recursively validates the real `.git` metadata tree through no-follow
  descriptor-relative inspection: every entry must remain current-user-owned,
  directories and regular files must not be group- or other-writable, symlinks
  and unsupported file types fail closed, and Git object alternates remain
  unsupported by this initial executor.
- The coding-candidate authority-boundary audit now treats
  `lab_promotion_executor` as repository-execution authority so model/worker,
  candidate-evidence, approval, transaction, and recovery layers cannot silently
  acquire or reverse-import the new mutation boundary.
- Focused regression coverage proves two-file nested MODIFY success, explicit ADD
  refusal, the physical-replace-before-journal crash window reaching durable
  `RECOVERY_REQUIRED` without cleanup, and the executor's bounded authority
  surface.
- A bounded physical rollback executor now consumes only an exact,
  deterministic rollback plan for an already-active promotion transaction.
  Its initial mutation authority is restricted to MODIFY rollback: exact
  plan-authorized promotion-temporary removal, descriptor-bound restoration
  of already-preserved preimages, and durable rollback progress transitions.
  ADD/DELETE rollback remains refused.
- The rollback executor independently revalidates the exact durable journal,
  deterministic rollback plan, recovery-material identity, preimage bytes,
  repository identity, safe parent traversal, and real `.git` metadata tree
  before mutation. It has no approval-creation/consumption, worker/model,
  network, shell, staging, commit, push, or forward-promotion authority.
- Ambiguous failure after rollback execution begins is preserved rather than
  retried or cleaned. When legally possible the exact current journal progress
  is durably terminalized as `RECOVERY_REQUIRED`; any unconsumed rollback
  temporary is retained as recovery evidence.
- Focused rollback-executor regression coverage exercises nested two-file
  MODIFY rollback, partial-apply rollback, already-restored reconciliation,
  explicit ADD refusal, stale-plan/drift refusal, unsafe-parent refusal,
  recovery-material plan binding, and the physical-replace-before-journal
  failure window.

- This slice does not invoke the executor against Upgrade Life, does not promote
  the preserved browser candidate, adds no network or shell execution, and does
  not stage, commit, or push any repository state.
