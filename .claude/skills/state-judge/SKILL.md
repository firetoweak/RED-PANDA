---
name: state-judge
description: >-
  Use when designing or reviewing stateful systems (databases, journals,
  workflows, agents): checks correctness under evidenced retry, crash, and concurrency risks
  (idempotency, consistency, atomicity) and data-access efficiency (repeated
  reads/writes, N+1 and slow queries, missing or unsafe caching, unbatched
  writes).
---
# State judge

A proportionate review for persistent systems: protect actual business guarantees and find avoidable storage costs. Start from supported workflows, existing contracts, and demonstrated failure paths, not every failure that can be imagined.

Two parts, one review:

- **Part A: Correctness under relevant failures.** Does state preserve the required business meaning under realistic retry, crash, and overlap?
- **Part B: Data-access efficiency.** Is storage cost reasonable for the actual workload and expected data growth?

Correctness comes first. Never trade Part A for Part B: a cache or batch that breaks idempotency or consistency is a regression, not an optimization.

This is design plus targeted verification, not a requirement to add defensive infrastructure or adversarial tests to every stateful change.

## Scope first: real risks, not hypothetical guarantees

Never add general validation, reconciliation, uniqueness rules, locking, version checks, conflict handling, recovery machinery, or guard tests solely for nonexistent or extremely rare scenarios. Being technically possible, or possible within a supported feature, does not make a scenario representative or justify a general guarantee.

Before recommending a correctness safeguard, identify the existing business requirement it protects, the concrete trigger in the actual workflow, and the consequence of failure. Ground the trigger in code, delivery/retry semantics, observed incidents, expected usage, or an explicit product contract. Do not invent frequency or treat missing evidence as proof that a risk is common. If applicability is unclear, inspect the relevant path or state the uncertainty; uncertainty alone is not a blocker or permission to add a safeguard.

Distinguish business uniqueness, retry idempotency, and concurrent-edit policy. None implies the others. Do not infer that every write must be unique, every repeated submission is the same intent, or every edit needs lost-update protection. Preserve the existing business semantics unless a change is explicitly required.

For example, the hypothetical case of one account editing the same item on two devices and saving at nearly the same time does not justify adding unique constraints, version checks, or conflict UI to all saves. First establish that this is a supported, meaningful workflow and that the existing save behavior violates a required guarantee. Otherwise, leave the rare scenario outside the change.

An explicit business guarantee or a concrete severe consequence (such as duplicate charging or unrecoverable data loss) can justify protection even when the trigger is infrequent. This still requires evidence of a reachable failure path; severity must not be invented to justify machinery. Use the smallest local fix that addresses the established risk. Prefer simplicity, high cohesion, and low coupling; do not expand a local finding into a system-wide hardening project.

## When to run

- Changing persistence, transactions, queues, journals, workflows, or multi-actor control
- Adding or changing code on a hot path: per-request handlers, loops over records, polling, list/dashboard pages, agent step loops
- Bugs that only show under retry, crash mid-write, double-submit, or overlapping workers
- Slowness that grows with data size, request count, or history length
- User says they can judge business meaning but not low-level correctness

## Part A: Correctness questions (only where applicable)

1. **Success boundary**: What durable state and side effects change on success? Which changes must be atomic according to the business contract?
2. **Idempotency**: Can the same intent actually be retried, replayed, or delivered twice on this path? If so, does repeating it violate the required outcome? Require deduplication only when those semantics call for it.
3. **Concurrency**: Which actors actually overlap on this state in supported or observed use? Does the existing conflict policy satisfy the business requirement? Do not require serialization, locks, or versioning merely because simultaneous writes are technically possible.
4. **Partial failure**: Can a reachable interruption leave a materially invalid business state? If so, how is committed state identified and the required recovery achieved? Do not require an in-flight state machine for ordinary writes without such a need.
5. **Proof**: What focused check would falsify the identified risk or verify the fix? Reuse existing tests when sufficient. Add adversarial tests only for established failure paths.

Mark a question not applicable with a brief reason when no relevant trigger or guarantee exists; that is a valid review result.

### Additional concerns (consider only when the workflow supports them)

- **At-least-once delivery**: check the actual queue or retry contract before requiring deduplication.
- **Read-your-writes / stale reads**: after a write, can another actor still see old state?
- **Ordering**: does the business require ordering, and can actual delivery violate it?
- **Poison in-flight**: started work with unknown result is marked unknown, never treated as success or failure.
- **Cross-boundary transactions**: if DB, filesystem, and network effects must agree, assess existing recovery before considering compensation or an outbox.
- **Human plus automation overlap**: manual interrupt, rewind, or edit concurrent with agents.

## Part B: Efficiency questions (for relevant workload costs)

1. **Access count**: For one typical operation (one request, one agent step, one page load), how many reads and writes hit each store? Is that number constant, or does it grow with item count, history length, or user count? Write it down, e.g. "3 queries, constant" or "1 + N queries".
2. **Repeated reads**: Is the same data loaded more than once within one operation (different functions each re-fetching the same row, config, file, or session)? Load once at the boundary and pass it down, or memoize for the scope of that operation.
3. **Query shape**: Any query inside a loop (N+1)? Any filter, sort, or join without a supporting index? Any full scan, `SELECT *` of wide rows, or loading a whole table or history to use a few fields or the last item? Any unbounded result without a limit or pagination?
4. **Write pattern**: Are many small writes done one by one where one batched or bulk write would do? Is a whole object or file rewritten to change one field? Are writes repeated with unchanged values?
5. **Reuse and caching**: Is expensive, stable data (config, schema, compiled templates, permission tables, file metadata) recomputed or re-read on every call? If a cache is added: what is its scope (per operation, per process, shared), what invalidates it, and is any staleness acceptable for this business use? Require only the freshness guarantee the consumer needs.

### Efficiency red flags (name them when seen)

- Query or file read inside a `for` / `map` over records
- The same getter called repeatedly in one request with no reuse
- Polling a store in a tight loop instead of waiting on an event or using backoff
- Cost that grows with total history (re-reading the full log, chat, or journal each step) when only the tail or a summary is needed
- Count or exists checks done by loading every row
- Serial network or DB calls that are independent and could run concurrently or be combined
- A cache keyed so broadly it serves wrong data, or so narrowly it never hits
- Optimization without measurement: complexity added for a path that is not hot

### Scope discipline for Part B

Only flag an efficiency issue when the path is hot (per request, per loop item, per step) or the cost grows with data size. A one-off startup read or an admin script that runs once does not need tuning. Prefer the simplest fix in this order: remove the duplicate access, then pass already-loaded data down, then batch, then index, and only then cache.

## Verification beyond unit tests

Choose from this menu only to verify an established risk or meaningful cost. These are options, not a mandatory checklist:

| Kind | What it catches |
| --- | --- |
| Crash injection mid-write | Half-committed state |
| Duplicate submit / replay | Missing idempotency |
| Concurrent interleaving | Races between workers and actors |
| Restart plus recovery | Incorrect resume from journal or DB |
| Property / invariant checks | Violations of existing business invariants; do not invent uniqueness or single-writer rules |
| Chaos on clocks or network | Timeouts and reordering |
| Query or IO counting (assert at most K queries per operation) | N+1, repeated reads, regressions in access count |
| Scaling check (run with 10, 100, 1000 items and compare counts or time) | Cost that grows with data size |
| Query plan inspection (e.g. EXPLAIN) on the main queries | Missing index, full scans |
| Cache staleness test (change the source, then read through the cache) | Missing or wrong invalidation |

For relevant checks, verify **final durable state, side effects, and storage cost** as needed, not return codes alone. Only add a count or timing test where a real hot path exists; don't add guard tests for internal code paths with no external cost source. There is no minimum number of tests; a review may require no new tests or safeguards.

## How to guide a human who does not read every line

Use these plain-language prompts where relevant:

1. What is the smallest successful transaction?
2. Can this operation actually repeat, and what should that mean for the business?
3. Who actually changes this state concurrently, and does that require special handling?
4. How many times does one operation hit the database or disk, and does that grow with data?
5. Is anything loaded or computed twice that could be reused, and if something is cached, what makes it refresh?

Clarify unanswered material requirements before proposing a fix. Do not redesign a boundary merely to fill every prompt.

## Output format

Keep the review proportional to the change. Report:

1. A short summary of relevant correctness risks and storage costs; say when no material issue is found.
2. Findings with evidence, the business guarantee affected, the real trigger, and the smallest justified fix. Cover applicable Part A and Part B questions; group irrelevant questions with a brief reason instead of filling a ceremonial checklist. State typical access counts only when relevant and derivable; distinguish measured counts from estimates.
3. Focused verification for the identified findings, if needed. Do not manufacture adversarial cases or scaling checks to meet a quota.
4. If a merge decision is requested, pass or fail based on evidenced violations of required behavior. Hypothetical or extremely rare unsupported scenarios are not blockers. Separate material correctness blockers from optional efficiency follow-ups.

Keep project-specific names out of the skill; map both sets of questions onto the project's stores, actors, and hot paths in the answer.
