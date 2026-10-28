# Part B: Data-access efficiency

Only flag an efficiency issue when the path is hot (per request, per loop item, per step) or the cost grows with data size. A one-off startup read or an admin script that runs once does not need tuning. Prefer the simplest fix in this order: remove the duplicate access, then pass already-loaded data down, then batch, then index, and only then cache.

1. **Access count**: For one typical operation (one request, one agent step, one page load), how many reads and writes hit each store? Is that number constant, or does it grow with item count, history length, or user count? Write it down, e.g. "3 queries, constant" or "1 + N queries".
2. **Repeated reads**: Is the same data loaded more than once within one operation (different functions each re-fetching the same row, config, file, or session)? Load once at the boundary and pass it down, or memoize for the scope of that operation.
3. **Query shape**: Any query inside a loop (N+1)? Any filter, sort, or join without a supporting index? Any full scan, `SELECT *` of wide rows, or loading a whole table or history to use a few fields or the last item? Any unbounded result without a limit or pagination?
4. **Write pattern**: Are many small writes done one by one where one batched or bulk write would do? Is a whole object or file rewritten to change one field? Are writes repeated with unchanged values?
5. **Reuse and caching**: Is expensive, stable data (config, schema, compiled templates, permission tables, file metadata) recomputed or re-read on every call? If a cache is added: what is its scope (per operation, per process, shared), what invalidates it, and is any staleness acceptable for this business use? Require only the freshness guarantee the consumer needs.

## Red flags (name them when seen)

- Query or file read inside a `for` / `map` over records
- The same getter called repeatedly in one request with no reuse
- Polling a store in a tight loop instead of waiting on an event or using backoff
- Cost that grows with total history (re-reading the full log, chat, or journal each step) when only the tail or a summary is needed
- Count or exists checks done by loading every row
- Serial network or DB calls that are independent and could run concurrently or be combined
- A cache keyed so broadly it serves wrong data, or so narrowly it never hits
- Optimization without measurement: complexity added for a path that is not hot

## Verification options

Choose only to verify a meaningful cost; add a count or timing test only where a real hot path exists.

| Kind | What it catches |
| --- | --- |
| Query or IO counting (assert at most K queries per operation) | N+1, repeated reads, regressions in access count |
| Scaling check (run with 10, 100, 1000 items and compare counts or time) | Cost that grows with data size |
| Query plan inspection (e.g. EXPLAIN) on the main queries | Missing index, full scans |
| Cache staleness test (change the source, then read through the cache) | Missing or wrong invalidation |

## Plain-language prompts

1. How many times does one operation hit the database or disk, and does that grow with data?
2. Is anything loaded or computed twice that could be reused, and if something is cached, what makes it refresh?
