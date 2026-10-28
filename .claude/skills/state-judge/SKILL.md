---
name: state-judge
description: Review stateful changes (persistence, journals, workflows, concurrent actors) for correctness under evidenced retry, crash, or overlap, and for data-access cost on hot paths.
---
# State judge

A proportionate review for persistent systems: protect actual business guarantees and find avoidable storage costs. Start from supported workflows, existing contracts, and demonstrated failure paths, not every failure that can be imagined.

Two parts, one review:

- **Part A: Correctness under relevant failures.** Does state preserve the required business meaning under realistic retry, crash, and overlap? Questions, additional concerns, and verification options: `references/correctness.md`.
- **Part B: Data-access efficiency.** Is storage cost reasonable for the actual workload and expected data growth? Questions, red flags, and verification options: `references/efficiency.md`.

Decide which parts the change touches, then read only those references. Correctness comes first. Never trade Part A for Part B: a cache or batch that breaks idempotency or consistency is a regression, not an optimization.

This is design plus targeted verification, not a requirement to add defensive infrastructure or adversarial tests to every stateful change.

## Scope first: real risks, not hypothetical guarantees

Never add general validation, reconciliation, uniqueness rules, locking, version checks, conflict handling, recovery machinery, or guard tests solely for nonexistent or extremely rare scenarios. Being technically possible, or possible within a supported feature, does not make a scenario representative or justify a general guarantee.

Before recommending a correctness safeguard, identify the existing business requirement it protects, the concrete trigger in the actual workflow, and the consequence of failure. Ground the trigger in code, delivery/retry semantics, observed incidents, expected usage, or an explicit product contract. Do not invent frequency or treat missing evidence as proof that a risk is common. If applicability is unclear, inspect the relevant path or state the uncertainty; uncertainty alone is not a blocker or permission to add a safeguard.

Distinguish business uniqueness, retry idempotency, and concurrent-edit policy. None implies the others. Do not infer that every write must be unique, every repeated submission is the same intent, or every edit needs lost-update protection. Preserve the existing business semantics unless a change is explicitly required.

For example, the hypothetical case of one account editing the same item on two devices and saving at nearly the same time does not justify adding unique constraints, version checks, or conflict UI to all saves. First establish that this is a supported, meaningful workflow and that the existing save behavior violates a required guarantee. Otherwise, leave the rare scenario outside the change.

An explicit business guarantee or a concrete severe consequence (such as duplicate charging or unrecoverable data loss) can justify protection even when the trigger is infrequent. This still requires evidence of a reachable failure path; severity must not be invented to justify machinery. Use the smallest local fix that addresses the established risk. Prefer simplicity, high cohesion, and low coupling; do not expand a local finding into a system-wide hardening project.

## When to run

- Changing persistence, transactions, queues, journals, workflows, or multi-actor control (Part A)
- Bugs that only show under retry, crash mid-write, double-submit, or overlapping workers (Part A)
- Adding or changing code on a hot path: per-request handlers, loops over records, polling, list/dashboard pages, agent step loops (Part B)
- Slowness that grows with data size, request count, or history length (Part B)
- User says they can judge business meaning but not low-level correctness

## Verification

Verify only an established risk or meaningful cost. For relevant checks, verify final durable state, side effects, and storage cost as needed, not return codes alone. Don't add guard tests for internal code paths with no external cost source. There is no minimum number of tests; a review may require no new tests or safeguards.

When guiding a human who does not read every line, use the plain-language prompts in each reference where relevant. Clarify unanswered material requirements before proposing a fix. Do not redesign a boundary merely to fill every prompt.

## Output format

Keep the review proportional to the change. Report:

1. A short summary of relevant correctness risks and storage costs; say when no material issue is found.
2. Findings with evidence, the business guarantee affected, the real trigger, and the smallest justified fix. Cover applicable Part A and Part B questions; group irrelevant questions with a brief reason instead of filling a ceremonial checklist. State typical access counts only when relevant and derivable; distinguish measured counts from estimates.
3. Focused verification for the identified findings, if needed. Do not manufacture adversarial cases or scaling checks to meet a quota.
4. If a merge decision is requested, pass or fail based on evidenced violations of required behavior. Hypothetical or extremely rare unsupported scenarios are not blockers. Separate material correctness blockers from optional efficiency follow-ups.

Keep project-specific names out of the skill; map both sets of questions onto the project's stores, actors, and hot paths in the answer.
