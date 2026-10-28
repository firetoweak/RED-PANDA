# Part A: Correctness under relevant failures

Ask only where applicable. Mark a question not applicable with a brief reason when no relevant trigger or guarantee exists; that is a valid review result.

1. **Success boundary**: What durable state and side effects change on success? Which changes must be atomic according to the business contract?
2. **Idempotency**: Can the same intent actually be retried, replayed, or delivered twice on this path? If so, does repeating it violate the required outcome? Require deduplication only when those semantics call for it.
3. **Concurrency**: Which actors actually overlap on this state in supported or observed use? Does the existing conflict policy satisfy the business requirement? Do not require serialization, locks, or versioning merely because simultaneous writes are technically possible.
4. **Partial failure**: Can a reachable interruption leave a materially invalid business state? If so, how is committed state identified and the required recovery achieved? Do not require an in-flight state machine for ordinary writes without such a need.
5. **Proof**: What focused check would falsify the identified risk or verify the fix? Reuse existing tests when sufficient. Add adversarial tests only for established failure paths.

## Additional concerns (consider only when the workflow supports them)

- **At-least-once delivery**: check the actual queue or retry contract before requiring deduplication.
- **Read-your-writes / stale reads**: after a write, can another actor still see old state?
- **Ordering**: does the business require ordering, and can actual delivery violate it?
- **Poison in-flight**: started work with unknown result is marked unknown, never treated as success or failure.
- **Cross-boundary transactions**: if DB, filesystem, and network effects must agree, assess existing recovery before considering compensation or an outbox.
- **Human plus automation overlap**: manual interrupt, rewind, or edit concurrent with agents.

## Verification options

Choose only to verify an established risk; these are options, not a mandatory checklist.

| Kind | What it catches |
| --- | --- |
| Crash injection mid-write | Half-committed state |
| Duplicate submit / replay | Missing idempotency |
| Concurrent interleaving | Races between workers and actors |
| Restart plus recovery | Incorrect resume from journal or DB |
| Property / invariant checks | Violations of existing business invariants; do not invent uniqueness or single-writer rules |
| Chaos on clocks or network | Timeouts and reordering |

## Plain-language prompts

1. What is the smallest successful transaction?
2. Can this operation actually repeat, and what should that mean for the business?
3. Who actually changes this state concurrently, and does that require special handling?
