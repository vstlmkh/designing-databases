# Indexing

Load whenever an index is being added, dropped, reordered, or blamed.

## Contents

- What an index costs
- B+tree mechanics and page splits
- Composite column order: the ESR rule
- The leftmost prefix rule, and skip scans
- Covering indexes and INCLUDE
- Partial indexes
- Expression indexes
- Specialized index types: hash, GIN, BRIN, GiST
- Finding unused and redundant indexes
- Index bloat and rebuilding
- The index maintenance runbook

## What an index costs

Every index trades write throughput and memory for read speed. One row insert
updates *every* index on the table inside the same transaction; one page split
can write two full pages into the WAL/redo log plus parent modifications. Before
adding an index, name the query it serves and check whether an existing index can
be widened instead.

Never add an index without checking what it makes redundant, and never drop one
without checking replica usage.

## B+tree mechanics and page splits

Keys live in sorted, fixed-size pages (8 KB PostgreSQL, 16 KB InnoDB). Branch
pages route; leaf pages hold values plus a row reference, linked horizontally so
range scans walk sideways without re-descending. With a fanout of ~500, three
levels address ~125M rows in at most three page reads.

The row reference differs by engine, and this is the root of most engine
divergence:

- **InnoDB clustered index**: the leaf page *is* the row. Secondary index leaves
  store the indexed columns followed by the primary key value; a lookup that
  needs other columns does a second descent through the clustered index
  (a "bookmark lookup").
- **PostgreSQL**: every index is secondary. Leaves store the key plus a 6-byte
  `ctid` (4-byte block number + 2-byte offset) pointing straight at the heap tuple.

**Page splits** happen when an insert targets a full leaf page: allocate a page,
move half the tuples, register the boundary in the parent, and bubble upward if
the parent is full too. The two costs are write amplification (a 32-byte key can
force two full pages into the log) and read fragmentation (logically adjacent
keys end up physically scattered, so range scans do random I/O).

Sequential keys avoid this entirely — writes land on the rightmost leaf, which
fills to 90–99% and is always in cache. `fillfactor` reserves free space per leaf
page for workloads that update indexed columns in place:

```sql
CREATE INDEX idx_accounts_balance ON accounts (balance) WITH (fillfactor = 70);
```

## Composite column order: the ESR rule

Order composite index columns **Equality, Sort, Range**:

1. **Equality** (`=`, `IN` with few values) first — these narrow the tree.
2. **Sort** (`ORDER BY`) next — the engine then reads leaves in order and skips
   the sort node entirely.
3. **Range** (`<`, `>`, `BETWEEN`) last — tree navigation *stops* at the first
   range column; everything after it is only a filter applied per entry.

```sql
-- Query
SELECT id, account_id, created_at, status
FROM transactions
WHERE account_id = 9182
  AND status IN ('PENDING','FLAGGED')
  AND created_at >= '2025-01-01'
ORDER BY created_at DESC;

-- Violates ESR: range column first; engine filters account_id row by row
CREATE INDEX idx_bad ON transactions (created_at, account_id, status);

-- ESR: two equality seeks, then a contiguous ordered range read
CREATE INDEX idx_good ON transactions (account_id, status, created_at DESC);
```

With `(tenant_id, created_at, status)` the engine seeks `tenant_id`, then reads
*every* leaf entry from the date boundary onward checking `status` by hand. With
`(tenant_id, status, created_at)` it descends on both equality columns and reads
only matching timestamps — zero wasted entries.

Direction matters for sorts: a single index can serve `ORDER BY a, b DESC` only
if it was declared that way (`CREATE INDEX … (a, b DESC)`) or the engine can walk
it backwards for *all* columns at once.

## The leftmost prefix rule, and skip scans

An index on `(org_id, status, amount)` serves:

- `WHERE org_id = ?` — full seek
- `WHERE org_id = ? AND status = ?` — full seek
- `WHERE org_id = ? AND amount = ?` — seeks `org_id`, then filters every leaf
  entry in that range for `amount` (prefix seek with leaf filter)
- `WHERE status = ?` — **cannot navigate the tree at all**; sequential scan or
  full index scan

MySQL 8.0+ can sometimes use an *index skip scan*: when the leading column has
very low cardinality, it decomposes the query into one seek per distinct leading
value. It only pays off for a handful of distinct values; with thousands it is
worse than a table scan. Do not design for it — design the column order right.

## Covering indexes and INCLUDE

A covering index holds every column the query touches (`SELECT`, `WHERE`, `JOIN`,
`GROUP BY`, `ORDER BY`), so the engine never visits the table. The win is
avoiding random heap I/O: 200,000 matching rows scattered over a 50 GB table can
mean 200,000 random page reads even though the index seek took under 1 ms.

Do **not** stuff payload columns into the key — key columns live in every branch
page, lowering fanout and deepening the tree. Use `INCLUDE` (PostgreSQL 11+, SQL
Server; MySQL gets covering for free because secondary leaves already carry the PK):

```sql
CREATE INDEX idx_invoices_covering
  ON invoices (customer_id, status)       -- navigable, sorted
  INCLUDE (total, issued_at);             -- leaf-only payload

-- Uniqueness is enforced on key columns only
CREATE UNIQUE INDEX idx_users_email ON users (email) INCLUDE (first_name, avatar_url);
```

**PostgreSQL caveat**: an `Index Only Scan` still consults the visibility map.
If autovacuum has not cleaned a heap page since its last write, the bit is clear
and the engine reads the heap anyway — visible in the plan as `Heap Fetches`.
High heap fetches mean "vacuum is behind", not "add another index".

## Partial indexes

Index only the rows queries actually touch. On a task queue where 99.5% of rows
are `COMPLETED`:

```sql
CREATE INDEX idx_pending_tasks
  ON task_queue (tenant_id, created_at)
  WHERE status IN ('QUEUED','PROCESSING');
```

Typical effect: 50M rows → 25k entries, ~3.2 GB → ~1.8 MB, and inserts of
completed rows stop touching the index at all.

The planner uses a partial index only when it can **prove** the query predicate
implies the index predicate:

```sql
WHERE tenant_id = 14 AND status = 'QUEUED'      -- matches
WHERE tenant_id = 14                            -- does NOT match
WHERE tenant_id = 14 AND status != 'COMPLETED'  -- does NOT match ('FAILED' exists)
```

Also use partial unique indexes for "unique among live rows" and partial indexes
on nullable flags.

## Expression indexes

The B-tree stores raw values, so wrapping the column in a function in the query
makes the index unusable. Index the expression instead, and call it with the
identical signature in the query:

```sql
CREATE INDEX idx_audit_date  ON audit_records ((DATE(created_at)));
CREATE INDEX idx_users_lemail ON users ((LOWER(email)));
CREATE INDEX idx_orders_tier ON orders (((metadata->'customer'->>'tier')));
```

Cost: the function runs on every insert and update of that row. Expensive
expressions (regex, hashes) show up directly as write latency. Prefer fixing the
query to be sargable (a half-open range instead of `DATE()`), and reach for an
expression index only when the transformation is genuinely required.

## Specialized index types: hash, GIN, BRIN, GiST

| Type | Good for | Cannot do |
|---|---|---|
| **B-tree** (default) | equality, ranges, sorting, prefix `LIKE 'abc%'` | containment, full-text, leading wildcards |
| **Hash** (`USING HASH`) | O(1) equality on long random strings: API keys, session tokens, hashes | ranges, `ORDER BY`, `LIKE`, multi-column |
| **GIN** | arrays, `JSONB` containment (`@>`), full-text `tsvector`; one row → many leaf entries | ranges; writes are expensive (buffered by `fastupdate=on`) |
| **BRIN** | huge append-only tables whose physical order matches the column (time series, sequential IDs): ~1.5–10 MB where a B-tree needs 11–25 GB | anything with random physical order — overlapping block ranges degrade it to a full scan |
| **GiST** | geometry, ranges, `EXCLUDE` constraints, nearest-neighbour | — |

```sql
CREATE INDEX idx_sessions_token ON user_sessions USING HASH (token);
CREATE INDEX idx_documents_tags ON documents USING GIN (tags);
CREATE INDEX idx_telemetry_brin ON telemetry_data USING BRIN (recorded_at);
```

BRIN stores only min/max per range of 128 blocks (1 MB). It is unbeatable on
append-only time-series and useless the moment rows arrive out of order.

## Finding unused and redundant indexes

```sql
-- PostgreSQL: never-scanned, non-unique indexes, largest first
SELECT schemaname || '.' || relname AS table_name,
       indexrelname AS index_name,
       idx_scan      AS scans,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes
JOIN pg_index USING (indexrelid)
WHERE indisunique IS FALSE AND idx_scan = 0
ORDER BY pg_relation_size(indexrelid) DESC;
```

```sql
-- MySQL (sys schema)
SELECT * FROM sys.schema_unused_indexes;
SELECT * FROM sys.schema_redundant_indexes;
```

Judge "unused" over a full business cycle — 30 to 60 days, so monthly reporting
is included — and check **read replicas separately**: an index idle on the
primary may serve every analytics query on a replica.

Redundancy follows from the leftmost prefix rule: given
`idx_1 (company_id)` and `idx_2 (company_id, created_at, status)`, `idx_1` is
dead weight — `idx_2` serves the same lookups and `idx_1` taxes every insert.

## Index bloat and rebuilding

Updates and deletes leave dead slots in leaf pages that are only reusable by keys
landing in the same gap, so trees grow and deepen over time.

```sql
CREATE EXTENSION IF NOT EXISTS pgstattuple;
SELECT tuple_count, dead_tuple_count, free_space, free_percent
FROM pgstatindex('idx_payments_org_status_amount');
```

Above ~35–40% free space on a low-update table, rebuild:

```sql
REINDEX INDEX CONCURRENTLY idx_payments_org_status_amount;  -- never plain REINDEX in prod
```

Plain `REINDEX` takes `ACCESS EXCLUSIVE`. The concurrent form builds a duplicate
and swaps atomically — budget up to 2x the index size in free disk. In MySQL,
`OPTIMIZE TABLE payments;` rebuilds the table and its indexes (online in
InnoDB, but it rewrites everything — treat it as a maintenance-window operation).

## The index maintenance runbook

1. **Capture the workload.** `pg_stat_statements` ordered by `total_exec_time`,
   or the MySQL slow log / `performance_schema`.
2. **Get the plan.** `EXPLAIN (ANALYZE, BUFFERS)` on the offender. Look for
   `Seq Scan`, `Rows Removed by Filter`, `Heap Fetches`, and `shared read`.
3. **Design the index**: equality columns → sort/range columns → `INCLUDE`
   payload. Prefer widening an existing index over adding a new one.
4. **Build it without locking.**
   ```sql
   CREATE INDEX CONCURRENTLY idx_optimized_events
     ON events (org_id, region, created_at) INCLUDE (payload_summary);
   ```
   ```sql
   ALTER TABLE events ADD INDEX idx_optimized_events (org_id, region, created_at),
     ALGORITHM=INPLACE, LOCK=NONE;   -- MySQL 8
   ```
5. **Verify the build succeeded.** A failed `CONCURRENTLY` build leaves an
   invalid index that still slows writes:
   ```sql
   SELECT relname, indisvalid FROM pg_index i
   JOIN pg_class c ON c.oid = i.indexrelid WHERE relname = 'idx_optimized_events';
   -- indisvalid = false → DROP INDEX CONCURRENTLY and retry
   ```
6. **Re-measure.** Same query, same plan command; confirm the node changed and
   buffer reads dropped. Check that the new index did not evict hot data from
   the buffer pool.
7. **Prune.** Drop the indexes the new one makes redundant — after confirming
   zero scans on the primary *and* every replica: `DROP INDEX CONCURRENTLY …`.
