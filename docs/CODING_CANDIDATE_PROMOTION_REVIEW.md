# Coding Candidate Promotion Review Checklist

## Status

This is a draft checklist for a later human decision, not a statement of current project status or completed promotion. Overnight work remains unpromoted candidate evidence. Completing the checklist records review only: it does not approve, authorize, stage, commit, push, or promote anything. Model output is never authority, and `coding_candidate` provenance is evidence only, never approval or authorization. Real repositories are not autonomous worker targets. Human promotion approval remains mandatory. Any unresolved failure, or any missing, stale, ambiguous, or mismatched evidence, remains a blocker and requires stopping.

## Candidate and diff

- [ ] Treat the independently derived physical diff between the recorded before and after workspace snapshots as authoritative. Its physical-diff ID, complete file set, operations, paths, bytes, hashes, content, and modes all validate and agree with the candidate.
- [ ] Load the canonical durable candidate by its exact genuine candidate ID and verify its workspace session/device/inode and before/after snapshot identities. Preserve provenance as `coding_candidate` with that candidate ID; do not invent a legacy `WRITE_FILE` action ID or substitute the physical-diff ID, a content hash, or any other identifier.
- [ ] Review every production and test change in full, together and against the stated task. Reject hidden scope, weakened or deleted tests, renamed test IDs, reduced assertions, concealed defects, generated artifacts, or unrelated edits.
- [ ] Confirm the proposed operations independently determine the recorded risk classification. Provenance and a documentation-only label must not lower risk.
- [ ] Confirm file modes are explicit and expected: modifications preserve their approved preimage modes, additions use the required safe mode, and no executable or special bits appear unexpectedly.

## Verification evidence

- [ ] Review the exact full-suite command, tool/environment identity, timestamp, stdout, stderr, exit status, timeout state, and test count. Require a current run against the exact candidate; preserve every failure, error, skip, warning, and unresolved limitation. Do not infer acceptance from a partial or green-looking excerpt.
- [ ] Confirm the authoritative diff contains no unexpected files or operations, and separately account for all workspace artifacts. Reject temporary files, caches, bytecode, secrets, credentials, tokens, private configuration, host state, or unrelated repository content.

## Destination before-state

- [ ] From fresh read-only inspection, verify the destination canonical path, filesystem device/inode, ownership, real `.git` directory, attached branch, exact HEAD, and required clean index/worktree. The inspected repository must be the precise human-selected destination, not the disposable lab workspace.
- [ ] For every modification, verify the current regular-file preimage bytes, size, SHA-256, mode, path traversal, and link expectations exactly match the approved proposal. For every addition, verify the bound absence claim still holds. Refuse symlinks, hard-link ambiguity, special files, unexpected state, or any drift.
- [ ] Confirm the proposal binds the exact candidate, physical changes, repository identity, branch/HEAD, preimages or absences, modes, verification plan, and operation-derived risk. Any changed input requires fresh inspection, a new proposal, and a new decision.

## Approval, recovery, and decision

- [ ] Before any mutation, verify private durable recovery materials bind every exact preimage or absence, hash, mode, repository identity, proposal/transaction, and destination. Review the deterministic rollback plan, append-only transaction journal, legal state transitions, verification steps, stop conditions, and handling for interrupted or ambiguous states.
- [ ] Keep approval separate from candidate evidence. If promotion is later requested, a human must explicitly answer a fresh challenge for the exact proposal; the resulting approval must be unexpired, single-use, and bound to the exact proposal and promotion run. Silence, model output, prior approval, or identifier resemblance is refusal.
- [ ] Record an explicit human decision: **reject**, **defer**, or **approve this exact proposal for a separately initiated promotion run**. Name the reviewer, decision time, proposal ID, candidate ID, repository identity, branch, and HEAD. Until that record exists and all gates are revalidated immediately before mutation, the candidate remains unreviewed or unpromoted evidence.

A favorable review does not authorize staging, committing, pushing, merging, or any later change. Those are separate human-controlled actions.
