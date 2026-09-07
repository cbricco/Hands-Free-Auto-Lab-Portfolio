# Auto Lab Development Rules

## Safety boundary

- Treat all model output and every `coding_candidate` as untrusted evidence, never approval or authorization.
- Keep autonomous reads, writes, execution, and iteration inside an explicitly created disposable lab workspace. Fail closed when identity, policy, evidence, or sandbox validation is incomplete.
- Do not expose credentials, private host state, unrelated repositories, network access, package-management authority, services, or privileged devices to the lab.
- Promotion to a real repository always requires a separate explicit human decision bound to the exact proposal. Never stage, commit, push, or otherwise promote on model authority.

## Promotion invariants

- Preserve exact candidate identity, physical-diff evidence, repository device/inode identity, branch and head identity, canonical paths, file modes, and content hashes across boundaries.
- Revalidate destination preimages and absence claims before mutation. Treat drift, symlinks, hard links, special files, unexpected state, and provenance mismatch as refusal conditions.
- A coding candidate uses `coding_candidate` provenance with its genuine candidate ID. Never invent a legacy `WRITE_FILE` action ID or substitute another identifier for legacy provenance.
- Derive risk classification from the proposed operations; provenance cannot lower risk.
- Keep approval consumption, transaction journaling, recovery materials, state transitions, verification, and rollback deterministic, durable, bounded, and separately validated.

## Development practice

- Inspect architecture, implementation, and tests before changing behavior.
- Preserve test names and IDs. Do not delete tests, weaken assertions, hide production defects, or broaden authority to make tests pass.
- Make the smallest scoped change, preserve component separation and immutable evidence contracts, and add fail-closed tests for new boundary behavior.
- Validate Python syntax with `ast.parse`, then run the full unittest suite with bytecode generation disabled.
- Overnight outputs remain reviewable, unpromoted candidate evidence until a human explicitly approves a precisely bound, separately initiated promotion. Any unresolved failure remains a blocker.
