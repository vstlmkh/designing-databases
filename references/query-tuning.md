# Query tuning

Load in Workflow B — a query, endpoint, or report that is too slow.

## Contents

- How the engine executes a query
- Getting a plan worth reading
- The five fields that matter
- Access paths
- Join algorithms and how they fail
- Sargability
- Query anti-patterns and their rewrites
- Statistics
- Memory and spills
- Parameter sniffing and plan regressions
- The diagnostic loop

## How the engine executes a query

Four phases: parse (syntax, catalog lookups, permissions) → rewrite (flatten
subqueries, fold constants, drop `WHERE 1=1`) → cost-based optimization (enumerate
physical plans, score each from catalog statistics, pick the cheapest) → execute
(pull pages from the buffer pool, read from disk on a miss).

Cost is an artificial unit, not milliseconds. PostgreSQL's formula weighs
sequential page fetches (`seq_page_cost = 1.0`), random page fetches
(`random_page_cost = 4.0`), and CPU per tuple/operator. That 4:1 ratio assumes
spinning disks; on SSD/NVMe set `random_page_cost` to 1.1–1.5, or the planner
will keep rejecting correct index plans.

## Getting a plan worth reading

```sql
-- PostgreSQL: estimates + reality + I/O + settings that influenced the plan
EXPLAIN (ANALYZE, BUFFERS, SETTINGS, WAL, FORMAT JSON) <query>;

-- MySQL 8.0.18+
EXPLAIN ANALYZE <query>;
EXPLAIN FORMAT=TREE <query>;   -- plan shape without executing
EXPLAIN FORMAT=JSON <query>;   -- machine-readable, feeds explain_audit.py
```

`EXPLAIN` alone prints estimates — the very thing that is usually wrong. Always
`ANALYZE`, and remember it **executes** the statement: wrap any `INSERT`/`UPDATE`/
`DELETE` in `BEGIN; … ROLLBACK;`.

Run `python3 scripts/explain_audit.py plan.json` on the JSON output: it does the
loop multiplication, the estimate/actual ratios, the self-time attribution and
the spill detection that are tedious and error-prone to do by eye.

## The five fields that matter

| Field | Meaning | Warning sign |
|---|---|---|
| Node Type | physical algorithm | `Seq Scan` on a multi-million row table |
| Actual Time | `startup..total` **per loop**, ms | high startup on sorts; high total on leaves |
| Actual vs Plan Rows | estimate accuracy | off by ≥10x → the plan shape is probably wrong |
| Loops | executions of this node | the multiplier everyone forgets |
| Buffers | `shared hit` / `read` / `dirtied` | high `read` = real disk I/O |

**The loop trap**: a node showing `(actual time=0.050..0.080 rows=1 loops=500000)`
did not take 0.08 ms. It took 0.080 × 500,000 = 40 seconds. Total rows likewise
multiply by `loops`. PostgreSQL prints per-loop averages; misreading this is the
single most common plan-reading error.

## Access paths

- **Seq Scan / full table scan** — reads every page, filters every row. Correct
  for tiny tables and for queries needing >20–40% of a large one (sequential I/O
  beats random navigation). Wrong when the predicate is selective: missing index,
  non-sargable predicate, or a type mismatch.
- **Index Scan** — descend the tree, then fetch each matching row from the
  heap/clustered index. Optimal for small result sets (1–100 rows); degenerates
  into random I/O when matches are many and scattered.
- **Index Only Scan** — every needed column is in the index. PostgreSQL still
  checks the visibility map; `Heap Fetches > 0` means it is not really index-only
  (run `VACUUM (ANALYZE)`).
- **Bitmap Index Scan + Bitmap Heap Scan** (PostgreSQL) — for hundreds to tens of
  thousands of matches: build a bitmap of candidate pages, sort it, read the heap
  in physical order. Converts random I/O into sequential. If the bitmap outgrows
  `work_mem` it degrades to *lossy* (page-level), and every row on those pages is
  re-checked.

## Join algorithms and how they fail

| Algorithm | Best for | Fails when |
|---|---|---|
| **Nested Loop** | small outer side + indexed inner side | the outer cardinality was underestimated — 5 expected rows become 200,000 index seeks |
| **Hash Join** | large unsorted inputs, equality conditions only | the build side exceeds `work_mem` / `join_buffer_size` and spills into multiple batches |
| **Merge Join** | inputs already sorted on the join key (via indexes) | inputs need explicit sorts; many-to-many duplicates force repeated rewinds |

Almost every pathological join plan traces back to a cardinality misestimate, not
to the algorithm choice. Fix the statistics before overriding the planner.

## Sargability

A predicate is sargable (Search-ARGument-able) when the engine can navigate the
B-tree directly to satisfy it. Transformations on the column destroy that.

```sql
-- Function on the column
WHERE LOWER(email) = 'alice@example.com'      -- scan
WHERE email = 'alice@example.com'             -- seek (or index LOWER(email))

-- Date truncation
WHERE DATE(created_at) = '2024-03-15'         -- scan
WHERE created_at >= '2024-03-15'
  AND created_at <  '2024-03-16'              -- seek, half-open range

-- Arithmetic on the column (the optimizer does not do algebra for you)
WHERE balance - 50 < 100                      -- scan
WHERE balance < 150                           -- seek

-- Type mismatch; phone_number is VARCHAR
WHERE phone_number = 5551234                  -- MySQL: silent full scan via cast
WHERE phone_number = '5551234'                -- seek   (PostgreSQL errors instead)

-- Leading wildcard
WHERE sku LIKE '%XYZ'                         -- scan; use a reversed column or pg_trgm
WHERE sku LIKE 'XYZ%'                         -- seek
```

The MySQL/PostgreSQL divergence on type mismatch is worth repeating: PostgreSQL
usually raises an error, MySQL silently casts every column value and turns a
1 ms seek into a full scan with no warning in the logs.

## Query anti-patterns and their rewrites

**`OR` across different columns** — no single B-tree can serve it; the engine
scans or does an index merge.

```sql
SELECT id, full_name FROM employees WHERE department_id = 5 OR title = 'Director';
-- Rewrite: each branch uses its own index
SELECT id, full_name FROM employees WHERE department_id = 5
UNION
SELECT id, full_name FROM employees WHERE title = 'Director';
```

**Offset pagination** — `LIMIT 20 OFFSET 500000` reads and discards 500,000 rows,
degrading linearly with page depth.

```sql
-- Keyset (seek) pagination: O(log N) regardless of depth
SELECT id, title, created_at FROM posts
WHERE (created_at, id) < ('2025-02-14 10:30:00', 98214)
ORDER BY created_at DESC, id DESC
LIMIT 20;
```
The composite tuple comparison is the part people omit; without the `id`
tie-breaker, rows sharing a timestamp are skipped or repeated.

**Correlated subquery in the SELECT list** — runs once per outer row.

```sql
SELECT u.id, u.username,
       (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) AS order_count
FROM users u;
-- Rewrite: one aggregation, joined once
SELECT u.id, u.username, COALESCE(o.order_count, 0) AS order_count
FROM users u
LEFT JOIN (SELECT user_id, COUNT(*) AS order_count FROM orders GROUP BY user_id) o
       ON o.user_id = u.id;
```

**`DISTINCT` hiding join fan-out** — materializes duplicates, then sorts them away.

```sql
SELECT DISTINCT u.id, u.email FROM users u JOIN orders o ON u.id = o.user_id;
-- Rewrite: a semijoin stops at the first match per user
SELECT u.id, u.email FROM users u
WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id);
```

**Thousands of literals in `IN (...)`** — bloats parsing and wrecks selectivity
estimates. PostgreSQL: pass an array, `WHERE id = ANY($1::bigint[])`. MySQL/SQL
Server: load the IDs into a temporary table and join.

**`SELECT *`** — defeats covering indexes and ships columns nobody reads. List
the columns; it is what makes an index-only scan possible.

**`COUNT(*)` for pagination UI** — an exact count of a large filtered set is a
full scan every page load. Use an approximate count (`reltuples`, `EXPLAIN`
estimate) or a "load more" interaction.

## Statistics

The optimizer plans from catalog samples: row counts, page counts, null
fractions, column histograms, most-common-values. After a bulk load or a large
delete they are stale, and a plan built for 10 rows runs against 500,000.

```sql
ANALYZE users;                     -- PostgreSQL
ALTER TABLE users ALTER COLUMN status SET STATISTICS 500;  -- more histogram detail
CREATE STATISTICS s_orders (dependencies) ON tenant_id, status FROM orders;  -- correlated columns
ANALYZE TABLE users;               -- MySQL
```

Correlated columns are a classic misestimate source: the planner multiplies
independent selectivities, so `country='CH' AND city='Zurich'` is estimated far
too low. Extended statistics (PostgreSQL) fix exactly this.

## Memory and spills

Sorts, hash tables and grouping need working memory. PostgreSQL's `work_mem` is
**per operation, per connection** — a query with four joins and two sorts can
allocate it six times over. MySQL uses `sort_buffer_size` and `join_buffer_size`.

In a plan, spills look like:

```
Sort Method: external merge  Disk: 4216kB
Buckets: 1024  Batches: 8  Memory Usage: …
```

Raise it for the one statement, not globally:

```sql
BEGIN;
SET LOCAL work_mem = '64MB';
SELECT * FROM large_table ORDER BY unindexed_column;
COMMIT;
```

Better still, remove the sort: an index in the required order streams rows with
no sort node at all.

## Parameter sniffing and plan regressions

Prepared statements cache a plan built from the first parameter values. If that
first value matched 2 rows, the cached Nested Loop will be reused for the value
matching 500,000 rows.

- PostgreSQL: `SET plan_cache_mode = force_custom_plan;` (session or role).
- SQL Server: `OPTION (RECOMPILE)` or `OPTIMIZE FOR`.
- MySQL: split the query into separate code paths by selectivity, or use hints.

## The diagnostic loop

1. **Capture**: `pg_stat_statements`, MySQL slow log / `performance_schema`,
   `sys.dm_exec_query_stats`.
2. **Prioritize by total impact** (`calls × mean_exec_time`), not worst single
   call. A 50 ms query running 200k times an hour outweighs a 10 s nightly report.
3. **Reproduce** with `EXPLAIN (ANALYZE, BUFFERS)` against production-like data
   volume — a plan from a 1000-row dev table proves nothing.
4. **Inspect**: unexpected sequential scans; actual vs estimated rows off by
   orders of magnitude; sorts or hashes spilling to disk; buffer reads dominating
   hits; `Rows Removed by Filter`.
5. **Apply one fix** — statistics, an index, a rewrite, or a memory setting.
6. **Measure again** and report both the before and after numbers (time *and*
   buffers). If the plan did not change shape, the fix was not the fix.
