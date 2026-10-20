# Report template

Use when the deliverable is a written schema or performance review. Keep the
order: what was examined → what is wrong → what to do → what it costs. Every
claim carries its evidence; every recommendation carries its risk.

---

# <Database / service> — <schema audit | performance review>

**Engine**: PostgreSQL 16.3 · **Source**: read-only replica `db-r2`, 2026-10-20
**Scope**: `orders`, `payments`, `customers` (the checkout path)
**Evidence**: `pg_snapshot.sql` output, `pg_stat_statements` top 20,
`EXPLAIN (ANALYZE, BUFFERS)` for the three slowest statements

## Summary

Two sentences: the headline problem and the expected effect of fixing it.
Example: "Checkout reads scan `orders` end to end because the composite index
puts the range column first; p95 is 4.8 s. A reordered index plus an `ANALYZE`
on `orders` should bring it under 50 ms."

## Findings

Ranked by impact, not by discovery order. One table row per finding.

| # | Severity | Object | Finding | Evidence |
|---|---|---|---|---|
| 1 | Critical | `orders` | Sequential scan over 5M rows per checkout read | plan node `Seq Scan on orders`, 182k rows returned, 4.8M removed by filter |
| 2 | High | `payments.order_id` | Foreign key with no index | `pg_snapshot` §7 |
| 3 | Medium | `idx_orders_customer` | Redundant: leftmost prefix of `idx_orders_customer_status` | `schema_audit.py` IDX002 |

For each finding worth more than a table row:

### 1. `orders` is scanned sequentially on every checkout read

**What happens.** <mechanism, in engine terms — not "it is slow">
**Evidence.**
```
->  Seq Scan on orders  (cost=0.00..980.00 rows=95) (actual rows=182440 loops=1)
      Filter: (date_trunc('day', created_at) >= '2026-01-01')
      Rows Removed by Filter: 4817560
```
**Why it is this way.** <the predicate is non-sargable / the index violates ESR / statistics are stale>
**Fix.**
```sql
CREATE INDEX CONCURRENTLY idx_orders_tenant_created ON orders (tenant_id, created_at);
-- and rewrite the predicate as a half-open range:
WHERE created_at >= '2026-01-01' AND created_at < '2026-01-02'
```
**Cost and risk.** ~4 min build on 52M rows, +1.8 GB disk, writes continue;
adds ~3% to insert latency on `orders`. Rollback: `DROP INDEX CONCURRENTLY …`.
**Verification.** Re-run the plan; expect `Index Scan`, `shared read` under 200
blocks, p95 under 50 ms.

## Recommended sequence

Ordered so each step is independently shippable and reversible.

1. `ANALYZE orders;` — free, may alone fix the plan shape. Verify before step 2.
2. Add `idx_orders_tenant_created` concurrently.
3. Deploy the predicate rewrite.
4. After one week of telemetry, drop `idx_orders_customer` (confirm zero scans
   on the primary **and** every replica first).

## Not changed, and why

State what you deliberately left alone — a denormalization that is load-bearing,
an index that looks redundant but serves a replica report, a type that cannot be
widened without an expand/contract cycle. This section prevents the next reviewer
from "fixing" something on purpose.

## Open questions

Facts you could not establish and who can answer them (row growth rate, whether
a replica serves reporting, retention policy, peak concurrency).
